"""Node film-execution-console — the execution workbench read model.

긴 작업의 실제 상태를 보고 안전하게 멈추거나 이어간다.

This module is a *derived view*, never a second source of truth. Every fact
it reports comes from the same authorities the rest of the program uses:

- durable journals (``job_journal.jsonl`` — ANIM-021 hash-chained records)
- the Coordinator replay/state machine (`engine/execution_workers/`)
- the UploadTracker (`engine/upload_tracker.py`)
- the ArchiveCommitter (`engine/archive_commit.py`)
- committed perf state + render provenance (`engine/perf_scheduler.py`,
  `engine/render_provenance.py`)

It adds no new journal records for reads, does not poll, and does not retry
anything on its own. The only side effects live behind the explicitly
requested actions in `run_action`, and those just call the existing
Coordinator / UploadTracker / ArchiveCommitter methods — the console fences
exactly what those machines fence:

- ``UNKNOWN`` and ``CANCEL_REQUESTED`` never mint a new remote job; the only
  offered path is an explicit status query / reconcile on the existing
  request identity.
- ``CANCEL_REQUESTED`` is a request, not a terminal state — it is displayed
  as such until termination is confirmed (or completion wins the race).
- ``UPLOADED`` (bytes confirmed / offset) and ``VERIFIED`` (independent
  readback/hash) are reported as two separate facts; only verified
  checkpoints are offered as reusable artifacts.

Everything reachable from this box is a local fake — qualification is
always reported ``UNQUALIFIED``.
"""
from pathlib import Path

from .archive_commit import ArchiveCommitter
from .core import FilmError, now
from .durable_journal import DurableJournal
from .execution_workers import Coordinator
from .upload_tracker import UploadTracker

CONSOLE_VERSION = 1

# The pipeline's fixed encode roles (perf_scheduler `run_pipeline`
# encode_roles default; render_provenance ENCODE_ROLES).
_ENCODE_ROLES = ("clean", "subbed")

# The six console stages every item reports against (spec 구현 범위 1).
STAGES = ("compose", "encode", "mux", "verify", "upload", "seal")

# Stage status vocabulary:
#   NOT_APPLICABLE — this item has no such stage
#   PENDING        — upstream not complete / nothing recorded yet
#   READY          — all inputs exist; the stage can run now
#   RUNNING        — in progress
#   WAITING_USER   — manual packet handoff pending (Coordinator route)
#   SUBMITTING     — dispatch in flight; on restart it reconciles as UNKNOWN
#   PARTIAL        — some units done, work interrupted (upload offsets,
#                    commit objects, encode roles)
#   DONE           — finished, awaiting independent verification
#   UPLOADED       — transfer fact only (offset/bytes confirmed)
#   UPLOADED_UNVERIFIED — transfer complete but readback/hash not recorded
#   VERIFIED       — independent check passed
#   CANCEL_REQUESTED — stop requested; NOT terminal
#   CANCELLED      — termination confirmed (terminal)
#   FAILED_CONFIRMED — failure confirmed
#   VERIFY_MISMATCH — independent verification rejected the output
#   SEAL_UNKNOWN   — manifest publish answer lost
#   UNKNOWN        — outcome undetermined; fenced
#   SEALED         — archive seal confirmed
#   NOT_REACHED    — the item ended before reaching this stage

# Job state -> producing-stage status. The producing stage is whichever of
# compose/encode/mux the operation kind names (default compose).
_PRODUCE_STATUS = {
    "PLANNED": "PENDING",
    "RESERVED": "READY",
    "WAITING_USER": "WAITING_USER",
    "SUBMITTING": "SUBMITTING",
    "RUNNING": "RUNNING",
    "UNKNOWN": "UNKNOWN",
    "CANCEL_REQUESTED": "CANCEL_REQUESTED",
    "OUTPUT_PENDING_VERIFY": "DONE",
    "VERIFIED": "DONE",
    "ARCHIVED": "DONE",
    "FAILED_CONFIRMED": "FAILED_CONFIRMED",
    "CANCEL_CONFIRMED": "CANCELLED",
}

# Job states that mean the work is in flight or fenced — "interrupted".
_IN_FLIGHT = {"SUBMITTING", "RUNNING", "UNKNOWN",
              "CANCEL_REQUESTED", "OUTPUT_PENDING_VERIFY", "WAITING_USER"}

_CLOSED = {"CANCEL_CONFIRMED", "FAILED_CONFIRMED", "ARCHIVED"}

# Any action name that would mint a new remote job is refused while a job is
# fenced — and is never offered as "resume" anywhere. Reconcile/status-query
# is the only path out of UNKNOWN.
_SUBMIT_ACTIONS = {"submit", "resume", "retry", "resubmit"}

_ENCODE_ROLE_SET = frozenset(_ENCODE_ROLES)


def _stage(status, **fields):
    row = {"status": status}
    row.update({k: v for k, v in fields.items() if v is not None})
    return row


def _produce_stage_name(kind):
    """Map an operation kind onto one of the six console stages."""
    k = (kind or "").upper()
    for token, stage in (("ENCODE", "encode"), ("MUX", "mux"),
                         ("UPLOAD", "upload"), ("VERIFY", "verify"),
                         ("SEAL", "seal")):
        if token in k:
            return stage
    return "compose"


class ExecutionConsole:
    """Read model + explicitly-gated recovery actions over execution state.

    Parameters
    ----------
    coordinator : Coordinator, optional
        A live coordinator (tests / in-process UI). If omitted, one is
        replayed from ``state_dir``.
    state_dir : path-like, optional
        The coordinator state dir. Must already exist — the console never
        creates directories on a read.
    journal_dirs : iterable
        Extra dirs holding a ``job_journal.jsonl`` to scan for upload and
        commit scopes (``state_dir`` is always scanned too).
    perf_roots : iterable
        Dirs holding a committed ``state.json`` (ANIM-017/020 pipeline run).
    workers : dict
        ``{worker_id: worker}`` for actions that touch a remote identity.
        Unbound means the action is reported as unavailable, never silently
        retried.
    backends : dict
        ``{str(dir): storage backend}`` for upload/commit actions.
    trackers : dict
        ``{str(dir): UploadTracker}`` — caller-bound live trackers keep
        the in-memory session URIs, so a resume in the same process can
        continue the same session; a fresh console (restart) resumes on
        an explicitly journaled new session instead.
    upload, retry : dict
        The UploadTracker's own declared transfer/retry policy — passed
        through unchanged; the console never weakens the machine's
        session/retry allowance.
    """

    def __init__(self, coordinator=None, *, state_dir=None,
                 journal_dirs=(), perf_roots=(), workers=None,
                 backends=None, upload=None, retry=None, trackers=None):
        self.workers = dict(workers or {})
        self.backends = {str(d): b for d, b in dict(backends or {}).items()}
        self._upload_kw = {"upload": upload, "retry": retry}
        self._injected_trackers = {str(d): t
                                   for d, t in dict(trackers or {}).items()}
        if coordinator is None:
            if state_dir is None:
                raise FilmError("An execution state dir or a coordinator "
                                "is required")
            path = Path(state_dir)
            if not path.is_dir():
                raise FilmError(f"No execution state dir: {state_dir}")
            coordinator = Coordinator(path)
        self.coordinator = coordinator
        self.state_dir = (Path(state_dir) if state_dir
                          else coordinator.state_dir)
        dirs = []
        for d in [self.state_dir, *(journal_dirs or ())]:
            if d is None:
                continue
            d = Path(d)
            if d not in dirs:
                dirs.append(d)
        self._dirs = dirs
        self.perf_roots = [Path(d) for d in (perf_roots or ())]
        # Per-dir replay of the non-coordinator scopes.
        self._journals = {}       # str(dir) -> DurableJournal|None
        self._trackers = {}       # str(dir) -> UploadTracker|error
        self._committers = {}     # str(dir) -> ArchiveCommitter
        self._replay_errors = {}  # str(dir) -> str
        for d in self._dirs:
            self._scan_dir(d)

    # -- directory scan ---------------------------------------------------

    def _scan_dir(self, d):
        key = str(d)
        path = d / "job_journal.jsonl"
        injected = self._injected_trackers.get(key)
        if not path.is_file():
            self._journals[key] = None
            if injected is not None:
                self._trackers[key] = injected
            return
        journal = DurableJournal(path)
        self._journals[key] = journal
        scopes = {r["scope"] for r in journal.records}
        backend = self.backends.get(key)
        # A bound backend makes the tracker live even before the first
        # upload record exists — resume_upload can then drive the same
        # session in this session (the URIs never persist to the journal).
        if injected is not None:
            # One file, one writer: a caller-bound tracker holding its own
            # DurableJournal instance of this same file is rebound to the
            # shared one — a second instance would append with a stale
            # head/seq and corrupt the chain for every writer after it.
            # Its in-memory intents are replaced by a replay of that shared
            # journal too, so upload records another writer (a previous
            # committer) appended are applied before it reports or writes
            # — never a duplicate UPLOAD_INTENT or a stale PENDING row.
            if getattr(injected.journal, "path", None) is not None \
                    and Path(injected.journal.path).resolve() \
                    == path.resolve():
                if getattr(injected.journal, "fence_reason", None):
                    journal.fence(injected.journal.fence_reason)
                try:
                    replayed = UploadTracker(journal).uploads
                except FilmError as exc:
                    self._trackers[key] = None
                    self._replay_errors[key] = str(exc)
                    injected = None
                else:
                    with injected._lock:
                        injected.journal = journal
                        injected.uploads = replayed
            if injected is not None:
                self._trackers[key] = injected
        elif "upload" in scopes or backend is not None:
            try:
                self._trackers[key] = UploadTracker(
                    journal, backend, upload=self._upload_kw["upload"],
                    retry=self._upload_kw["retry"])
            except FilmError as exc:  # semantic replay error -> fenced view
                self._trackers[key] = None
                self._replay_errors[key] = str(exc)
        if "commit" in scopes:
            try:
                committer = ArchiveCommitter(
                    d, backend, upload=self._upload_kw["upload"],
                    retry=self._upload_kw["retry"])
            except FilmError as exc:
                self._replay_errors[key] = str(exc)
            else:
                # Same one-writer rule for the committer and the tracker
                # it re-drives on a resume: they share this dir's journal
                # and the console's tracker so an action's appends are
                # visible to every later write and read on this console.
                if committer.journal.fence_reason:
                    journal.fence(committer.journal.fence_reason)
                committer.journal = journal
                tracker = self._trackers.get(key)
                if tracker is not None:
                    committer.tracker = tracker
                else:
                    committer.tracker.journal = journal
                self._committers[key] = committer

    # -- journal record helpers --------------------------------------------

    def _records(self, job_key):
        journal = getattr(self.coordinator, "journal", None)
        if journal is None:
            return []
        return [r for r in journal.records
                if r["scope"] == "coordinator"
                and r["data"].get("job_key") == job_key]

    def _unknown_exists(self):
        return any(j["state"] == "UNKNOWN"
                   for j in self.coordinator.jobs.values())

    def _fence_reason(self):
        journal = getattr(self.coordinator, "journal", None)
        if journal is None:
            return None
        return journal.fence_reason or journal.tail_reason

    # -- job items ----------------------------------------------------------

    def _job_item(self, key, job):
        state = job["state"]
        records = self._records(key)
        own = _produce_stage_name(job["operation"].get("kind"))
        cancel_intent = any(r["event"] == "CANCEL_INTENT" for r in records)
        # The network lamp is the *current* loss only: the one that put
        # the job into its present UNKNOWN. A loss a later reconcile
        # resolved (UNKNOWN -> RUNNING, ...) is history, not a lit lamp.
        lost, recovered, cause = [], [], None
        for r in records:
            if r["event"] in ("SUBMIT_LOST", "CANCEL_LOST"):
                cause = r["event"]
            elif r["event"] == "STATE":
                if r["data"].get("to") == "UNKNOWN":
                    # the reconcile-time disconnect journals only
                    # STATE -> UNKNOWN (no *_LOST event for a dropped
                    # status query)
                    lost = [cause or "STATUS_LOST"]
                else:
                    recovered += lost
                    lost = []
                cause = None
        if state != "UNKNOWN":
            recovered += lost
            lost = []
        elif not lost:
            # a restart leaves the pending SUBMIT_INTENT as the marker
            lost = [cause or "SUBMIT_LOST"]
        cov = self.coordinator.coverage(key)
        covered, missing = cov["covered"], cov["missing"]

        stages = {name: _stage("NOT_APPLICABLE") for name in STAGES}
        detail = {"SUBMITTING": "dispatch in flight — a restart reconciles "
                                "it as UNKNOWN, never a resubmit",
                  "UNKNOWN": "outcome undetermined — reconciled only by an "
                             "explicit status query on the existing request",
                  "CANCEL_REQUESTED": "stop requested — not confirmed, "
                                      "not terminal",
                  "OUTPUT_PENDING_VERIFY": "completion confirmed; "
                                           "independent verification owes",
                  "WAITING_USER": "manual packet handoff pending",
                  "FAILED_CONFIRMED": "failure confirmed by the worker or "
                                      "the verifier",
                  "CANCEL_CONFIRMED": "termination confirmed — terminal",
                  "VERIFIED": "outputs verified; archive seal may follow",
                  "ARCHIVED": "verified outputs sealed by journal digest"}.get(state)
        stages[own] = _stage(_PRODUCE_STATUS[state],
                             covered=covered,
                             detail=detail)

        if state in ("VERIFIED", "ARCHIVED"):
            stages["verify"] = _stage("VERIFIED",
                                      detail="coordinator re-derived every "
                                             "member digest (CHECKPOINT)")
        elif state == "OUTPUT_PENDING_VERIFY":
            stages["verify"] = _stage("READY",
                                      detail="receipt confirmed; run verify")
        elif state == "FAILED_CONFIRMED" and job.get("verify_failed"):
            stages["verify"] = _stage("VERIFY_MISMATCH",
                                      detail="verification failed — output "
                                             "is not a reusable checkpoint")
        elif state in _CLOSED:
            stages["verify"] = _stage("NOT_REACHED")
        else:
            stages["verify"] = _stage("PENDING")

        if state == "ARCHIVED" or job.get("sealed"):
            stages["seal"] = _stage("SEALED",
                                    detail="seal record journal-digest bound")
        elif state == "VERIFIED":
            stages["seal"] = _stage("READY")
        elif state in _CLOSED:
            stages["seal"] = _stage("NOT_REACHED")
        else:
            stages["seal"] = _stage("PENDING")

        reports = ([("receipt", r["kind"]) for r in job["receipts"]] +
                   [("observation", o["answer"])
                    for o in job.get("worker_observations", [])])
        remote = {"last_report": reports[-1][1],
                  "basis": reports[-1][0]} if reports else \
            {"last_report": None, "basis": "none"}
        if state == "UNKNOWN":
            remote["detail"] = ("remote outcome undetermined — explicit "
                                "status query required; never resubmit")
        elif state == "CANCEL_REQUESTED":
            remote["detail"] = ("cancel requested but not confirmed — the "
                                "worker may still be running or already "
                                "finished")

        return {
            "item_id": f"job:{key}",
            "kind": "job",
            "ref": key,
            "label": job["operation"].get("id") or key[:16],
            "operation_kind": job["operation"].get("kind"),
            "route": job["route"],
            "worker_id": job["worker_id"],
            "output_range": list(job["output_range"]),
            "state": state,
            "attempt_id": job["attempt_id"],
            "request_id": job["request_id"],
            "attempts_used": job["attempts_used"],
            "max_attempts": job["max_attempts"],
            "stages": stages,
            "coverage": {"covered": covered, "missing": missing},
            "indicators": {
                "network": {"state": "LOST" if lost else "NONE",
                            "lost": lost, "recovered": recovered},
                "remote": remote,
                "upload_verify": {"level": None,
                                  "state": "NOT_APPLICABLE"},
            },
            "cancel": {"requested": cancel_intent,
                       "confirmed": state == "CANCEL_CONFIRMED",
                       "completion_confirmed": bool(reports)
                       and state == "OUTPUT_PENDING_VERIFY"},
            "verify_failed": job.get("verify_failed", False),
            "sealed": job.get("sealed", False),
            "closed": state in _CLOSED,
            "in_flight": state in _IN_FLIGHT,
            "qualification_state": "UNQUALIFIED",
        }

    def _job_recovery(self, key, job):
        """The per-state recovery offers — mirrors the Coordinator's fences."""
        state = job["state"]
        out = []
        if state in {"UNKNOWN", "CANCEL_REQUESTED", "SUBMITTING",
                     "RUNNING", "OUTPUT_PENDING_VERIFY"}:
            out.append({
                "action": "status_query",
                "allowed": True, "needs": ["worker"],
                "reason": "one explicit status query on the existing "
                          f"request {job['request_id']} — never a resubmit"})
        if state in {"RESERVED", "WAITING_USER", "SUBMITTING", "RUNNING",
                     "OUTPUT_PENDING_VERIFY"}:
            out.append({
                "action": "cancel", "allowed": True, "needs": ["worker"],
                "reason": "send a cancel request — it is a request, not a "
                          "confirmed termination"})
        if state == "OUTPUT_PENDING_VERIFY":
            out.append({
                "action": "verify", "allowed": True, "needs": [],
                "reason": "coordinator re-derives member digests — a "
                          "COMPLETE receipt is never trusted on its own"})
        if state == "VERIFIED" and not job.get("sealed"):
            out.append({"action": "seal", "allowed": True, "needs": [],
                        "reason": "seal the verified checkpoint into the "
                                  "journal"})
        if state == "FAILED_CONFIRMED":
            bounded = job["attempts_used"] < job["max_attempts"]
            allowed = bounded and not self._unknown_exists()
            out.append({
                "action": "new_attempt",
                "allowed": allowed,
                "needs": ["worker", "plan", "user_continued"],
                "reason": ("explicit user continuation mints the next "
                           "bounded attempt (N/M)"
                           if allowed else
                           "attempt budget spent or an UNKNOWN fence is "
                           "active — a new attempt stays refused")})
        if state in {"UNKNOWN", "CANCEL_REQUESTED"}:
            out.append({
                "action": "new_attempt", "allowed": False, "needs": [],
                "reason": "outcome undetermined — reconcile the existing "
                          "request first; a duplicate remote job is never "
                          "submitted while the old one may still exist"})
        if state in _CLOSED and not job.get("reservation_released"):
            out.append({
                "action": "release_reservation", "allowed": True,
                "needs": [],
                "reason": "release the closed job's reservation back to "
                          "the scheduler"})
        return out

    def _job_explain(self, item):
        key, job = item["ref"], self.coordinator.jobs[item["ref"]]
        state = job["state"]
        own = _produce_stage_name(job["operation"].get("kind"))
        records = self._records(key)

        reusable = []
        checkpoints = [r for r in records if r["event"] == "CHECKPOINT"]
        for r in checkpoints:
            reusable.append({
                "type": "frame_checkpoint",
                "covered": r["data"]["covered"],
                "members": r["data"]["members"],
                "record_seq": r["seq"],
                "record_digest": r["digest"],
                "basis": "JOURNAL_CHECKPOINT",
            })
        if not checkpoints and state in ("VERIFIED", "ARCHIVED"):
            reusable.append({
                "type": "frame_checkpoint",
                "covered": sorted(job["members"]),
                "members": job["members"],
                "basis": "VERIFIED_STATE",
            })

        if state == "FAILED_CONFIRMED":
            bounded = job["attempts_used"] < job["max_attempts"]
            unknown = self._unknown_exists()
            if unknown:
                reason = ("an UNKNOWN job is still fenced — no new "
                          "attempt until it reconciles on its existing "
                          "identity")
            elif not bounded:
                reason = "the bounded attempt allowance (N/M) is spent"
            else:
                reason = ("the worker confirmed the failure — only an "
                          "explicit user continuation mints the next "
                          "bounded attempt (N/M)")
            if job.get("verify_failed"):
                reason += (" — verification rejected the output; the "
                           "mismatched artifact is never relabelled "
                           "verified")
            new_attempt = {"required": True,
                           "allowed": bounded and not unknown,
                           "reason": reason}
        elif state in ("UNKNOWN", "CANCEL_REQUESTED"):
            new_attempt = {
                "required": None, "allowed": False,
                "reason": "outcome undetermined — reconcile the existing "
                          "request identity; a new attempt is fenced until "
                          "then (no duplicate remote submit)"}
        elif state == "CANCEL_CONFIRMED":
            new_attempt = {
                "required": False, "allowed": False,
                "reason": "termination is confirmed and final — a new "
                          "attempt id cannot revive it; only a new planned "
                          "job re-does the work"}
        else:
            new_attempt = {"required": False, "allowed": False,
                           "reason": "the job is not in a state that needs "
                                     "a new attempt"}

        notes = []
        if item["cancel"]["requested"] and state == "OUTPUT_PENDING_VERIFY":
            notes.append(
                "cancel requested but completion was confirmed first — the "
                "race resolved to keep the outputs; verify them or cancel "
                "again, the receipt is never silently dropped")
        if state == "CANCEL_REQUESTED":
            notes.append("CANCEL_REQUESTED is a request, not terminal — "
                         "termination is confirmed only by a terminal "
                         "receipt or a status query")
        if state in _IN_FLIGHT and not reusable:
            notes.append(
                "received coverage is receipt-level evidence only — it "
                "becomes reusable only after the coordinator's own "
                "verification writes a checkpoint")
        if self.coordinator.journal_fenced:
            notes.append(f"coordinator journal fenced: "
                         f"{self._fence_reason()}")

        item = dict(item)
        item.update({
            "stop_point": {
                "stage": own,
                "state": state,
                "attempt_id": job["attempt_id"],
                "request_id": job["request_id"],
                "coverage": item["coverage"],
                "detail": item["stages"][own].get("detail"),
            },
            "reusable_artifacts": reusable,
            "new_attempt": new_attempt,
            "recovery": self._job_recovery(key, job),
            "notes": notes,
        })
        return item

    # -- upload items ---------------------------------------------------------

    def _verified_objects(self):
        """object_id -> {level, commit_key} from every commit replay."""
        out = {}
        for key, committer in self._committers.items():
            for ck, commit in committer.commits.items():
                for oid, level in commit.get("verified", {}).items():
                    out[oid] = {"level": level, "commit_key": ck,
                                "source": key}
        return out

    def _upload_items(self, verified):
        items = []
        for dkey, tracker in self._trackers.items():
            if tracker is None:
                continue
            for intent_id, row in tracker.status().items():
                oid = row["object_id"]
                confirmed = max((s["confirmed"] for s in row["sessions"]),
                                default=0)
                live = [s for s in row["sessions"] if not s["fenced"]]
                ver = verified.get(oid)
                if ver:
                    vstage = _stage("VERIFIED", level=ver["level"],
                                    detail=f"readback/hash verified "
                                           f"({ver['level']}) by commit "
                                           f"{ver['commit_key']}")
                elif row["complete"]:
                    vstage = _stage(
                        "UPLOADED_UNVERIFIED",
                        detail="transfer fact only — no readback/hash "
                               "verification recorded")
                else:
                    vstage = _stage("PENDING")
                if row["complete"]:
                    ustage = _stage("UPLOADED",
                                    confirmed=row["byte_length"],
                                    detail="all bytes confirmed landed")
                elif row["unknown"]:
                    ustage = _stage(
                        "UNKNOWN", confirmed=confirmed,
                        detail="a create/complete answer was lost — "
                               "reconcile the object before resuming")
                elif confirmed:
                    ustage = _stage("PARTIAL", confirmed=confirmed,
                                    detail=f"interrupted — server-confirmed "
                                           f"offset {confirmed}/"
                                           f"{row['byte_length']}")
                else:
                    ustage = _stage("PENDING",
                                    detail="intent recorded; no bytes "
                                           "confirmed yet")
                stages = {name: _stage("NOT_APPLICABLE") for name in STAGES}
                stages["upload"] = ustage
                stages["verify"] = vstage
                items.append({
                    "item_id": f"upload:{intent_id}",
                    "kind": "upload",
                    "ref": intent_id,
                    "source": dkey,
                    "object_id": oid,
                    "byte_length": row["byte_length"],
                    "state": ("COMPLETE" if row["complete"] else
                              "UNKNOWN" if row["unknown"] else
                              "PARTIAL" if confirmed else "PENDING"),
                    "stages": stages,
                    "confirmed_bytes": confirmed,
                    "sessions": row["sessions"],
                    "live_session": live[-1]["ref"] if live else None,
                    "indicators": {
                        # `unknown` is set by UPLOAD_SESSION_LOST and
                        # cleared by the reconcile's fence or a complete —
                        # a resolved past loss does not light the lamp.
                        "network": {"state": "LOST" if row["unknown"]
                                    else "NONE",
                                    "lost": ["session"] if row["unknown"]
                                    else []},
                        "remote": {"last_report": None,
                                   "basis": "inapplicable",
                                   "detail": "archive offset is the truth, "
                                             "not a worker"},
                        "upload_verify": {
                            "level": ver["level"] if ver else
                            ("UPLOADED_UNVERIFIED" if row["complete"]
                             else None),
                            "verified": bool(ver)},
                    },
                    "closed": row["complete"] and bool(ver),
                    "in_flight": not row["complete"] or not ver,
                    "qualification_state": "UNQUALIFIED",
                })
        return items

    def _upload_explain(self, item, verified):
        oid = item["object_id"]
        tracker = self._trackers[item["source"]]
        reusable = []
        ver = verified.get(oid)
        if ver:
            commit = self._committers[item["source"]].commits[
                ver["commit_key"]]
            obj = next(o for a in commit["artifacts"] for o in a["objects"]
                       if o["object_id"] == oid)
            reusable.append({
                "type": "verified_object", "object_id": oid,
                "level": ver["level"], "sha256": obj["sha256"],
                "byte_length": obj["byte_length"],
                "basis": "OBJECT_VERIFIED",
                "note": "on resume the committer re-verifies the object "
                        "live — a deleted or altered object refuses"})

        recovery = []
        if item["state"] == "UNKNOWN":
            recovery.append({
                "action": "reconcile_upload", "allowed": True,
                "needs": ["backend"],
                "reason": "the create/complete answer was lost — check "
                          "whether the object landed, then either mark the "
                          "same intent complete or continue it explicitly"})
        if item["state"] in ("PARTIAL", "PENDING", "UNKNOWN"):
            recovery.append({
                "action": "resume_upload", "allowed": True,
                "needs": ["backend", "data"],
                "reason": "continue the same intent — resume the live "
                          "session from its server-confirmed offset or, if "
                          "it is fenced/expired, open an explicit new "
                          "session; never a blind re-POST of all bytes"})
        recovery.append({
            "action": "new_intent", "allowed": False, "needs": [],
            "reason": "an upload intent is bound to exact bytes — a new "
                      "intent for the same object_id is refused (identity "
                      "conflict)"})

        notes = []
        if item["state"] == "PARTIAL":
            notes.append(
                "the server-confirmed offset is the only truth — chunks are "
                "re-sent only after a same-session status query, and a "
                "dropped chunk is never assumed written")
        if not item["sessions"] and item["state"] != "PENDING":
            notes.append("no live session survives a restart — the next "
                         "resume journals an explicit new session for the "
                         "same intent")

        item = dict(item)
        item.update({
            "stop_point": {"stage": "upload", "state": item["state"],
                           "confirmed_bytes": item["confirmed_bytes"],
                           "byte_length": item["byte_length"],
                           "sessions": item["sessions"]},
            "reusable_artifacts": reusable,
            "new_attempt": {"required": False, "allowed": False,
                            "reason": "the intent continues the same bytes; "
                                      "nothing here mints jobs"},
            "recovery": recovery,
            "notes": notes,
        })
        return item

    # -- commit items ---------------------------------------------------------

    def _commit_items(self):
        items = []
        for dkey, committer in self._committers.items():
            tracker = self._trackers.get(dkey)
            uploads = ({u["object_id"]: u
                        for u in tracker.status().values()}
                       if tracker else {})
            for ck, commit in committer.commits.items():
                state = commit["state"]
                objects = []
                done = 0
                session_lost = False
                for art in commit["artifacts"]:
                    for o in art["objects"]:
                        u = uploads.get(o["object_id"], {})
                        session_lost |= bool(u.get("unknown"))
                        objects.append({
                            "object_id": o["object_id"],
                            "kind": art["kind"],
                            "sha256": o["sha256"],
                            "byte_length": o["byte_length"],
                            "min_level": o["min_level"],
                            "uploaded": bool(u.get("complete")),
                            "verified": commit["verified"].get(
                                o["object_id"]),
                        })
                        done += bool(u.get("complete"))
                total = len(objects)
                stages = {name: _stage("NOT_APPLICABLE") for name in STAGES}
                stages["upload"] = _stage(
                    "UPLOADED" if total and done == total else
                    "PARTIAL" if done else "PENDING",
                    detail=f"{done}/{total} objects transferred")
                vdone = sum(1 for o in objects if o["verified"])
                stages["verify"] = _stage(
                    "VERIFIED" if state in ("OBJECTS_VERIFIED",
                                            "MANIFEST_INTENT", "SEALED",
                                            "SEAL_UNKNOWN")
                    else "PARTIAL" if vdone else "PENDING",
                    detail=(f"{vdone}/{total} objects readback/hash "
                            f"verified"))
                stages["seal"] = _stage(
                    {"OBJECTS_PENDING": "PENDING",
                     "OBJECTS_VERIFIED": "READY",
                     "MANIFEST_INTENT": "PENDING",
                     "SEAL_UNKNOWN": "UNKNOWN",
                     "SEALED": "SEALED"}[state],
                    detail={"MANIFEST_INTENT": "manifest intent journaled — "
                                               "publish/reconcile pending",
                            "SEAL_UNKNOWN": "publish answer lost — "
                                            "reconcile, never republish "
                                            "blindly"}.get(state))
                items.append({
                    "item_id": f"commit:{ck}",
                    "kind": "commit",
                    "ref": ck,
                    "source": dkey,
                    "label": commit["build_id"],
                    "build_id": commit["build_id"],
                    "snapshot_digest": commit["snapshot_digest"],
                    "state": state,
                    "stages": stages,
                    "objects": objects,
                    "manifest_object_id": commit.get("manifest_object_id"),
                    "seal": commit.get("seal"),
                    "indicators": {
                        "network": {"state": "LOST" if state == "SEAL_UNKNOWN"
                                    or session_lost else "NONE",
                                    "lost": (["seal"] if state
                                             == "SEAL_UNKNOWN" else []) +
                                    (["session"] if session_lost else [])},
                        "remote": {"last_report": None,
                                   "basis": "inapplicable",
                                   "detail": "archive journal is the truth"},
                        "upload_verify": {
                            "levels": sorted({o["verified"]
                                              for o in objects
                                              if o["verified"]}),
                            "verified": bool(total)
                            and vdone == total},
                    },
                    "closed": state == "SEALED",
                    "in_flight": state != "SEALED",
                    "qualification_state": "UNQUALIFIED",
                })
        return items

    def _commit_explain(self, item):
        commit = self._committers[item["source"]].commits[item["ref"]]
        state = commit["state"]
        reusable = [{
            "type": "verified_object",
            "object_id": o["object_id"],
            "level": o["verified"],
            "sha256": o["sha256"],
            "byte_length": o["byte_length"],
            "basis": "OBJECT_VERIFIED",
            "note": "re-verified live on resume; a deleted/altered object "
                    "refuses the commit",
        } for o in item["objects"] if o["verified"]]

        stop = {"OBJECTS_PENDING": ("upload/verify",
                                    "interrupted before every object was "
                                    "verified"),
                "OBJECTS_VERIFIED": ("seal",
                                     "objects verified; manifest publish "
                                     "not yet journaled"),
                "MANIFEST_INTENT": ("seal",
                                    "manifest intent journaled — publish "
                                    "outcome not recorded"),
                "SEAL_UNKNOWN": ("seal",
                                 "manifest publish answer lost — reconcile "
                                 "on the same commit identity"),
                "SEALED": ("seal", "seal confirmed — terminal")}
        recovery = []
        if state in ("MANIFEST_INTENT", "SEAL_UNKNOWN"):
            recovery.append({
                "action": "reconcile_commit", "allowed": True,
                "needs": ["backend"],
                "reason": "re-read the manifest object id and the seal "
                          "record — resolve the true state before any "
                          "re-publish"})
        if state == "OBJECTS_PENDING":
            recovery.append({
                "action": "resume_upload", "allowed": True,
                "needs": ["backend", "data"],
                "reason": "re-drive the same commit — the upload tracker "
                          "resumes each object from its confirmed offset; "
                          "the artifact declarations must be re-supplied "
                          "(the journal pins bytes by hash, never stores "
                          "them)"})

        item = dict(item)
        item.update({
            "stop_point": {"stage": "seal" if state != "OBJECTS_PENDING"
                           else "upload",
                           "state": state,
                           "detail": stop[state][1]},
            "reusable_artifacts": reusable,
            "new_attempt": {"required": False, "allowed": False,
                            "reason": "the commit identity is fixed — "
                                      "resume re-drives the same commit, "
                                      "nothing is duplicated"},
            "recovery": recovery,
            "notes": ["a sealed commit is journal-digest bound; "
                      "SEAL_UNKNOWN never auto-retries"],
        })
        return item

    # -- pipeline (perf state) items -------------------------------------------

    def _pipeline_items(self, commits):
        # Lazy like every other perf_scheduler caller (capability_ui,
        # animation_ui, cli): perf_scheduler imports `resource`, which does
        # not exist on Windows — the desktop app must still import this
        # module and render coordinator/journal items with no perf roots.
        if not self.perf_roots:
            return []
        from .perf_scheduler import _load_state
        items = []
        for root in self.perf_roots:
            state = _load_state(root)
            if not state:
                continue
            snap = state["input"]["snapshot_digest"]
            stages = {name: _stage("NOT_APPLICABLE") for name in STAGES}
            clean = state.get("clean") or {}
            subbed = state.get("subbed") or {}
            frames = clean.get("frames") or []
            stages["compose"] = _stage(
                "DONE" if clean.get("sequence_root") else "PENDING",
                frames=len(frames),
                clean_root=clean.get("sequence_root"),
                subbed_root=subbed.get("sequence_root"),
                detail="committed clean/subbed sequence roots"
                if clean.get("sequence_root") else None)
            encodes = state.get("encodes") or {}
            have = {r: e for r, e in encodes.items()
                    if e.get("artifact_sha256")}
            stages["encode"] = _stage(
                "DONE" if _ENCODE_ROLE_SET <= set(have) else
                "PARTIAL" if have else "PENDING",
                detail=", ".join(
                    f"{r}:{e['artifact_sha256'][:12]}"
                    for r, e in sorted(have.items())))
            missing_roles = sorted(_ENCODE_ROLE_SET - set(have))
            stages["mux"] = _stage(
                "PARTIAL" if have and missing_roles else
                "DONE" if have else "PENDING",
                detail=("mux ran inside encode_delivery (stream-copy); the "
                        "artifact is the muxed MP4" if not missing_roles
                        else f"muxed MP4 only for {', '.join(sorted(have))}"
                             f"; no mux yet for "
                             f"{', '.join(missing_roles)}") if have
                else None)
            # Only a verification report that actually passed counts —
            # `verify_delivery` writes {"valid": True, ...} or raises.
            verified_roles = [r for r, e in have.items()
                              if (e.get("verification") or {})
                              .get("valid")]
            stages["verify"] = _stage(
                "VERIFIED" if have and
                set(verified_roles) >= set(have) and
                _ENCODE_ROLE_SET <= set(verified_roles) else
                "PARTIAL" if verified_roles else "PENDING",
                detail=f"{len(verified_roles)}/{len(_ENCODE_ROLE_SET)} "
                       f"encode roles verified")
            linked = [c for c in commits
                      if c["snapshot_digest"] == snap]
            if linked:
                adv = sorted(linked, key=lambda c: c["state"] != "SEALED")
                stages["upload"] = _stage(
                    adv[0]["stages"]["upload"]["status"],
                    detail=f"archive commit(s): "
                           f"{', '.join(c['ref'] for c in linked)}")
                stages["seal"] = _stage(
                    adv[0]["stages"]["seal"]["status"],
                    detail="see linked archive commit")
            else:
                stages["upload"] = _stage(
                    "PENDING",
                    detail="no archive commit recorded for this snapshot")
                stages["seal"] = _stage(
                    "PENDING",
                    detail="no archive commit recorded for this snapshot")
            items.append({
                "item_id": f"pipeline:{root.name}",
                "kind": "pipeline",
                "ref": str(root),
                "label": root.name,
                "snapshot_digest": snap,
                "state": "COMMITTED" if clean.get("sequence_root")
                        else "EMPTY",
                "stages": stages,
                "indicators": {
                    "network": {"state": "NONE", "lost": []},
                    "remote": {"last_report": None, "basis": "inapplicable"},
                    "upload_verify": {"level": None,
                                      "state": "NOT_APPLICABLE"},
                },
                "closed": all(stages[s]["status"] in
                              ("SEALED", "DONE", "VERIFIED",
                               "NOT_APPLICABLE") for s in STAGES),
                "in_flight": False,
                "qualification_state": "UNQUALIFIED",
            })
        return items

    # -- the read model ---------------------------------------------------------

    def _items(self):
        verified = self._verified_objects()
        items = [self._job_item(k, j)
                 for k, j in sorted(self.coordinator.jobs.items())]
        items += self._upload_items(verified)
        commits = self._commit_items()
        items += commits
        items += self._pipeline_items(commits)
        return items

    def status(self):
        """Full read-model report. Pure read — no journal writes."""
        items = self._items()
        journals = []
        for d in self._dirs:
            journal = self._journals.get(str(d))
            if journal is None:
                journals.append({"dir": str(d), "journal": None})
                continue
            journals.append({
                "dir": str(d),
                "records": len(journal.records),
                "scopes": sorted({r["scope"] for r in journal.records}),
                "head": journal.head,
                "tail": journal.tail,
                "tail_reason": journal.tail_reason,
                "fenced": journal.fenced,
                "replay_error": self._replay_errors.get(str(d)),
            })
        unresolved = []
        for item in items:
            if item["kind"] == "job" and item["state"] in (
                    "UNKNOWN", "CANCEL_REQUESTED"):
                unresolved.append({"item_id": item["item_id"],
                                   "state": item["state"],
                                   "needs": "status_query"})
            elif item["kind"] == "job" and \
                    item["state"] == "OUTPUT_PENDING_VERIFY":
                unresolved.append({"item_id": item["item_id"],
                                   "state": item["state"],
                                   "needs": "verify"})
            elif item["kind"] == "upload" and item["state"] == "UNKNOWN":
                unresolved.append({"item_id": item["item_id"],
                                   "state": "UPLOAD_SESSION_UNKNOWN",
                                   "needs": "reconcile_upload"})
            elif item["kind"] == "commit" and item["state"] in (
                    "MANIFEST_INTENT", "SEAL_UNKNOWN"):
                unresolved.append({"item_id": item["item_id"],
                                   "state": item["state"],
                                   "needs": "reconcile_commit"})
        report = {
            "console": "execution_console",
            "version": CONSOLE_VERSION,
            "generated_at": now(),
            "fenced": self.coordinator.journal_fenced
            or any(j.get("fenced") for j in journals)
            or bool(self._replay_errors),
            "coordinator": {
                "state_dir": str(self.coordinator.state_dir)
                if self.coordinator.state_dir else None,
                "jobs": len(self.coordinator.jobs),
                "snapshot_fallback": self.coordinator.snapshot_fallback,
                "journal_fenced": self.coordinator.journal_fenced,
                "fence_reason": self._fence_reason(),
            },
            "journals": journals,
            "items": items,
            "unresolved": unresolved,
            "qualification_state": "UNQUALIFIED",
        }
        return report

    # -- explain ----------------------------------------------------------------

    def _find(self, item_id):
        for item in self._items():
            if item["item_id"] == item_id or item["ref"] == item_id:
                return item
        raise FilmError(f"Unknown execution item: {item_id}")

    def explain(self, item_id):
        """Deeper read for one item: stop point, reusable verified
        artifacts, whether a new attempt is required and why."""
        item = self._find(item_id)
        verified = self._verified_objects()
        if item["kind"] == "job":
            return self._job_explain(item)
        if item["kind"] == "upload":
            return self._upload_explain(item, verified)
        if item["kind"] == "commit":
            return self._commit_explain(item)
        # pipeline items are already their own explanation
        item = dict(item)
        item.update({
            "stop_point": {"stage": "seal",
                           "state": item["state"],
                           "detail": "a committed pipeline snapshot; "
                                     "compose/encode/mux are idempotent "
                                     "replays from content keys"},
            "reusable_artifacts": [],
            "new_attempt": {"required": False, "allowed": False,
                            "reason": "nothing remote is outstanding for a "
                                      "committed snapshot"},
            "recovery": [],
            "notes": ["replay re-derives from recorded content keys; it "
                      "never re-calls a generator"],
        })
        return item

    # -- actions ----------------------------------------------------------------

    def run_action(self, item_id, action, *, user_continued=False,
                   plan=None, data=None):
        """Run one explicitly requested action.

        Every refused path returns ``{"result": "REFUSED", ...}`` instead of
        raising, so the UI can render the reason. The console adds no retries
        and never submits a second remote job — it calls the existing
        machines once.
        """
        item = self._find(item_id)
        if item["kind"] == "job":
            if self.coordinator.journal_fenced:
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": f"JOURNAL_RECONCILIATION_REQUIRED: "
                                  f"{self._fence_reason()}"}
            return self._run_job(item, action, user_continued, plan)
        if item["kind"] == "upload":
            journal = self._journals.get(item["source"])
            if journal is not None and journal.fenced:
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": f"JOURNAL_RECONCILIATION_REQUIRED: "
                                  f"{journal.fence_reason or journal.tail_reason}"}
            return self._run_upload(item, action, data)
        if item["kind"] == "commit":
            committer = self._committers.get(item["source"])
            if committer is not None and committer.fenced:
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": "JOURNAL_RECONCILIATION_REQUIRED: the "
                                  "commit journal is fenced"}
            return self._run_commit(item, action, data)
        return {"action": action, "item_id": item["item_id"],
                "result": "REFUSED",
                "reason": "a committed pipeline snapshot has no recovery "
                          "actions"}

    def _run_job(self, item, action, user_continued, plan):
        key = item["ref"]
        job = self.coordinator.jobs[key]
        if action in _SUBMIT_ACTIONS:
            return {"action": action, "item_id": item["item_id"],
                    "result": "REFUSED",
                    "reason": "the console never re-submits a job — the "
                              "offered recovery actions are reconcile / "
                              "status_query on the existing identity, or "
                              "a bounded new_attempt after FAILED_CONFIRMED"}
        if action == "status_query":
            worker = self.workers.get(job["worker_id"])
            if worker is None:
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": f"worker {job['worker_id']} is not bound "
                                  f"to this console — reconcile needs the "
                                  f"live worker or the CLI"}
            state = self.coordinator.reconcile(worker, key)
            return {"action": action, "item_id": item["item_id"],
                    "result": "OK", "state": state}
        if action == "cancel":
            worker = self.workers.get(job["worker_id"])
            if worker is None:
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": f"worker {job['worker_id']} is not bound"}
            state = self.coordinator.request_cancel(worker, key)
            return {"action": action, "item_id": item["item_id"],
                    "result": "OK", "state": state}
        if action == "verify":
            if job["state"] != "OUTPUT_PENDING_VERIFY":
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": f"verify runs only on "
                                  f"OUTPUT_PENDING_VERIFY, not "
                                  f"{job['state']}"}
            state = self.coordinator.verify_outputs(key)
            return {"action": action, "item_id": item["item_id"],
                    "result": "OK", "state": state}
        if action == "seal":
            if job["state"] != "VERIFIED" or job.get("sealed"):
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": "seal requires a verified, unsealed job"}
            seal = self.coordinator.seal(key)
            return {"action": action, "item_id": item["item_id"],
                    "result": "OK", "seal": seal}
        if action == "new_attempt":
            if job["state"] in ("UNKNOWN", "CANCEL_REQUESTED"):
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": "outcome undetermined — reconcile the "
                                  "existing request identity first; a "
                                  "duplicate remote submit is never "
                                  "minted"}
            if job["state"] != "FAILED_CONFIRMED":
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": f"new attempts mint only on "
                                  f"FAILED_CONFIRMED, not {job['state']}"}
            if self._unknown_exists():
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": "an UNKNOWN fence is active"}
            worker = self.workers.get(job["worker_id"])
            if worker is None:
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": f"worker {job['worker_id']} is not bound"}
            if plan is None:
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": "a new attempt needs the continuation "
                                  "plan (ranges/paths) — none supplied"}
            state = self.coordinator.resume_failed(
                worker, key, user_continued=user_continued, plan=plan)
            return {"action": action, "item_id": item["item_id"],
                    "result": "OK", "state": state,
                    "attempt_id": job["attempt_id"]}
        if action == "release_reservation":
            self.coordinator.release_reservation(key)
            return {"action": action, "item_id": item["item_id"],
                    "result": "OK", "state": job["state"]}
        return {"action": action, "item_id": item["item_id"],
                "result": "REFUSED",
                "reason": f"unknown job action {action!r}"}

    def _run_upload(self, item, action, data):
        tracker = self._trackers.get(item["source"])
        if tracker is None:
            return {"action": action, "item_id": item["item_id"],
                    "result": "REFUSED",
                    "reason": "upload journal replay error — reconcile the "
                              "journal first"}
        if action == "reconcile_upload":
            if tracker.backend is None:
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": "no archive backend bound for "
                                  f"{item['source']}"}
            answer = tracker.reconcile(item["object_id"])
            return {"action": action, "item_id": item["item_id"],
                    "result": "OK", "answer": answer}
        if action == "resume_upload":
            if tracker.backend is None:
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": "no archive backend bound"}
            if data is None:
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": "resume needs the object's bytes to "
                                  "re-drive the intent"}
            intent = tracker.uploads.get(item["ref"])
            if intent and intent["unknown"]:
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": "UPLOAD_SESSION_UNKNOWN — the lost "
                                  "answer is reconciled first, never "
                                  "resumed on a second session"}
            record = tracker.put(item["object_id"], data)
            intent = tracker.uploads[record["intent_id"]]
            return {"action": action, "item_id": item["item_id"],
                    "result": "OK", "resumed": record["resumed"],
                    "state": "COMPLETE" if intent["complete"]
                            else "PARTIAL",
                    "confirmed": record["bytes_confirmed"]}
        return {"action": action, "item_id": item["item_id"],
                "result": "REFUSED",
                "reason": f"unknown upload action {action!r}"}

    def _run_commit(self, item, action, data):
        if action not in ("reconcile_commit", "resume_upload"):
            return {"action": action, "item_id": item["item_id"],
                    "result": "REFUSED",
                    "reason": f"unknown commit action {action!r}"}
        committer = self._committers.get(item["source"])
        if committer is None:
            return {"action": action, "item_id": item["item_id"],
                    "result": "REFUSED",
                    "reason": "commit journal replay error — reconcile "
                              "the journal first"}
        backend = self.backends.get(item["source"])
        if backend is None:
            return {"action": action, "item_id": item["item_id"],
                    "result": "REFUSED",
                    "reason": "no archive backend bound — the action "
                              "needs the live backend"}
        # The bound committer writes through the same DurableJournal the
        # console's tracker uses — a second instance would stamp stale
        # seq/prev_digest and corrupt the chain for later appends.
        committer.backend = backend
        committer.tracker.backend = backend
        if action == "reconcile_commit":
            answer = committer.reconcile(item["ref"])
        else:
            if item["state"] != "OBJECTS_PENDING":
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED",
                        "reason": f"resume_upload re-drives an "
                                  f"OBJECTS_PENDING commit, not "
                                  f"{item['state']}"}
            try:
                # `data` re-supplies the artifact declarations — the
                # journal pins them by hash and never stores the bytes.
                answer = committer.run(item["ref"], artifacts=data)
            except FilmError as exc:
                return {"action": action, "item_id": item["item_id"],
                        "result": "REFUSED", "reason": str(exc)}
        return {"action": action, "item_id": item["item_id"],
                "result": "OK", "answer": answer}


# --- CLI-facing payloads -------------------------------------------------------

def execution_status(state_dir, *, journal_dirs=(), perf_roots=()):
    """`execution-status` — JSON read model over a state dir."""
    console = ExecutionConsole(state_dir=state_dir,
                               journal_dirs=journal_dirs,
                               perf_roots=perf_roots)
    return console.status()


def execution_explain(state_dir, item, *, journal_dirs=(), perf_roots=()):
    """`execution-explain <item>` — JSON explanation of one item."""
    console = ExecutionConsole(state_dir=state_dir,
                               journal_dirs=journal_dirs,
                               perf_roots=perf_roots)
    return console.explain(item)
