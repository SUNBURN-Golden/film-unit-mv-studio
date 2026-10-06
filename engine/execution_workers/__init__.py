"""FRAME_ANIMATION_V1 WorkerProtocol 1 (ANIM-014, schema §13, exec §5.3).

One coordinator owns selection, receipts, coverage and seal writes. A job
key is the hash of (snapshot, operation, output range, recipe,
runtime/driver contract); attempt id and submission request id are
managed separately, and an UNKNOWN submission is never resolved by
minting a new job key.

State machine is exactly schema §13 — only the listed transitions exist:

    PLANNED -> RESERVED
    RESERVED -> WAITING_USER | SUBMITTING | CANCEL_CONFIRMED
    WAITING_USER -> SUBMITTING | CANCEL_CONFIRMED
    SUBMITTING -> RUNNING | UNKNOWN | CANCEL_REQUESTED
    RUNNING -> OUTPUT_PENDING_VERIFY | FAILED_CONFIRMED | UNKNOWN
               | CANCEL_REQUESTED
    OUTPUT_PENDING_VERIFY -> VERIFIED | FAILED_CONFIRMED | UNKNOWN
                             | CANCEL_REQUESTED
    CANCEL_REQUESTED -> CANCEL_CONFIRMED | OUTPUT_PENDING_VERIFY | UNKNOWN
    UNKNOWN -> RUNNING | FAILED_CONFIRMED | OUTPUT_PENDING_VERIFY
               | CANCEL_CONFIRMED          (existing job confirmed only)
    VERIFIED -> ARCHIVED
    FAILED_CONFIRMED -> RESERVED          (explicit user resume only —
                                           §5.3 text: a new attempt starts
                                           after the user sees the failure
                                           and continues; bounded allowance)

UNKNOWN fences reservation release, substitute workers and new attempts.
A completion confirmed before a cancel lands in OUTPUT_PENDING_VERIFY —
cancel is never a reason to drop output or reservations. More generally,
once a complete receipt set is held no later worker answer (cancel,
NOT_FOUND, failure) confirms a cancel or failure or discards coverage;
only the coordinator's verify_outputs ends it. A worker's
COMPLETE statement is never promoted to VERIFIED without the
coordinator's independent verification. A new attempt after
FAILED_CONFIRMED starts only through an explicit user resume inside the
bounded retry allowance. Once an attempt is closed (FAILED_CONFIRMED,
CANCEL_CONFIRMED, VERIFIED, ARCHIVED) a later receipt for it is recorded
in `late_receipts` (digests only) and never changes coverage or state.

Worker authentication is a scoped grant (evolution §2.1.1 / schema §17):
issuer, audience, peer, credential epoch, job, attempt, snapshot,
object/member, range, max_bytes, expiry and nonce — all secret-free
digests. User OAuth/refresh tokens never enter a worker packet; this
package runs no listener, no inbound port, no arbitrary URL fetch.

The coordinator keeps one minimal durable state file (the full
append-only journal is ANIM-021): after a runtime termination a
restarted coordinator reloads the same job identity and resolves
UNKNOWN, late completion and cancel-unknown through explicit
reconciliation — never by resubmitting.
"""
from pathlib import Path
import hashlib
import json
import logging
import threading
import time
import uuid

from ..animation_schema import canon_bytes
from ..core import FilmError, atomic_text
from ..execution_plan import assert_executable, job_key, plan_sha
from ..frame_stream import check_frame, contract_buffer_bytes
from ..storage_backends import ConnectionDropped

JOB_STATES = {"PLANNED", "RESERVED", "WAITING_USER", "SUBMITTING",
              "RUNNING", "UNKNOWN", "CANCEL_REQUESTED", "CANCEL_CONFIRMED",
              "OUTPUT_PENDING_VERIFY", "FAILED_CONFIRMED", "VERIFIED",
              "ARCHIVED"}

TRANSITIONS = {
    "PLANNED": {"RESERVED"},
    "RESERVED": {"WAITING_USER", "SUBMITTING", "CANCEL_CONFIRMED"},
    "WAITING_USER": {"SUBMITTING", "CANCEL_CONFIRMED"},
    "SUBMITTING": {"RUNNING", "UNKNOWN", "CANCEL_REQUESTED"},
    "RUNNING": {"OUTPUT_PENDING_VERIFY", "FAILED_CONFIRMED", "UNKNOWN",
                "CANCEL_REQUESTED"},
    "OUTPUT_PENDING_VERIFY": {"VERIFIED", "FAILED_CONFIRMED", "UNKNOWN",
                              "CANCEL_REQUESTED"},
    "CANCEL_REQUESTED": {"CANCEL_CONFIRMED", "OUTPUT_PENDING_VERIFY",
                         "UNKNOWN"},
    "UNKNOWN": {"RUNNING", "FAILED_CONFIRMED", "OUTPUT_PENDING_VERIFY",
                "CANCEL_CONFIRMED"},
    "VERIFIED": {"ARCHIVED"},
    # §5.3 text: FAILED_CONFIRMED allows a new attempt only as the user's
    # explicit resume — the FAILED_CONFIRMED -> RESERVED edge exists solely
    # for resume_failed; it is never an automatic retry.
    "FAILED_CONFIRMED": {"RESERVED"},
}

RECEIPT_KINDS = {"ACCEPTED", "RUNNING", "COMPLETE", "FAILED_CONFIRMED",
                 "CANCEL_CONFIRMED"}
RECEIPT_FIELDS = {"receipt_id", "actor", "job_key", "attempt_id",
                  "request_id", "snapshot_digest", "plan_revision",
                  "covered_range", "nonce", "kind", "members",
                  "grant_digest", "evidence"}
MEMBER_FIELDS = {"frame_index", "sha256"}
GRANT_FIELDS = {"issuer", "audience", "peer", "connection_digest",
                "credential_epoch", "job_key", "attempt_id", "snapshot_digest",
                "object_digests", "member_ids", "ranges", "max_bytes",
                "expires_at_ms", "nonce", "grant_digest"}
PACKET_FIELDS = {"job_key", "attempt_id", "request_id", "snapshot_digest",
                 "plan_revision", "operation", "frame_contract", "grant",
                 "endpoint", "input_members", "input_object_digests",
                 "output_range", "receipt_nonce", "input_bytes"}
# A worker packet is coordinator-mediated: no user credential, no
# arbitrary URL, no inbound callback. These keys can never appear.
FORBIDDEN_PACKET_KEYS = {"oauth_token", "refresh_token", "access_token",
                         "authorization_code", "bearer", "session_cookie",
                         "upload_uri", "fetch_url", "redirect_url",
                         "callback_url", "token"}

UNKNOWN_FENCED = {"submit", "release_reservation", "substitute",
                  "new_attempt"}

# Worker answers that would cancel or fail a job. Once the completion is
# confirmed first they are observations only — never a state change.
ADVERSE_ANSWERS = {"CANCELLED", "NOT_FOUND", "FAILED", "FAILED_CONFIRMED",
                   "CANCEL_CONFIRMED"}

# States in which the job's current attempt is still open. In every other
# state (terminal FAILED_CONFIRMED / CANCEL_CONFIRMED / VERIFIED /
# ARCHIVED, or RESERVED / WAITING_USER before the next attempt is minted)
# the attempt is closed: a later receipt bound to it is a late
# observation only and never changes coverage or state.
LIVE_ATTEMPT_STATES = {"SUBMITTING", "RUNNING", "UNKNOWN", "CANCEL_REQUESTED",
                       "OUTPUT_PENDING_VERIFY"}

log = logging.getLogger(__name__)


def _sha(value, what):
    if type(value) is not str or len(value) != 64 \
            or any(c not in "0123456789abcdef" for c in value):
        raise FilmError(f"{what} must be a lowercase SHA-256")
    return value


def _range(value, what):
    if type(value) is not list or len(value) != 2 \
            or any(type(v) is not int or v < 0 for v in value) \
            or value[1] <= value[0]:
        raise FilmError(f"{what} must be a non-empty [start, end) range")
    return value


def _covered_by(rng, ranges):
    return any(r[0] <= rng[0] and rng[1] <= r[1] for r in ranges)


# -- relay grants (secret-free binding; RELAY-AUTH) ----------------------------

def _grant_digest(binding):
    return hashlib.sha256(canon_bytes(binding)).hexdigest()


def issue_grant(*, issuer, audience, peer, connection_digest,
                credential_epoch, job_key, attempt_id, snapshot_digest,
                object_digests=(), member_ids=(), ranges=(), max_bytes,
                expires_at_ms, nonce):
    """A scoped transfer grant: digests only, never a bearer or token."""
    binding = {"issuer": issuer, "audience": audience, "peer": peer,
               "connection_digest": connection_digest,
               "credential_epoch": credential_epoch, "job_key": job_key,
               "attempt_id": attempt_id, "snapshot_digest": snapshot_digest,
               "object_digests": list(object_digests),
               "member_ids": list(member_ids),
               "ranges": [list(r) for r in ranges],
               "max_bytes": max_bytes, "expires_at_ms": expires_at_ms,
               "nonce": nonce}
    grant = dict(binding)
    grant["grant_digest"] = _grant_digest(binding)
    return grant


def check_grant(grant, *, worker, job_key, attempt_id, snapshot_digest,
                output_range, needed_bytes, now_ms,
                object_digests=(), member_ids=(), revoked=None):
    """RELAY-AUTH: pin the peer, the scope and the lifetime.

    Any tampered field breaks the grant digest; anything outside the
    bound job/attempt/snapshot/range/bytes/expiry is denied. A request
    naming an object or member outside the grant's bound
    `object_digests`/`member_ids`, or arriving on a different
    connection than `connection_digest`, is denied. Grant reuse after
    expiry, an epoch change or a coordinator revocation (`revoked`,
    the issuer-published digest set) is denied, and a grant is never a
    new compute submit by itself.
    """
    if type(grant) is not dict or set(grant.keys()) != GRANT_FIELDS:
        raise FilmError("RELAY_AUTH_DENIED: malformed grant")
    binding = {k: grant[k] for k in GRANT_FIELDS - {"grant_digest"}}
    if grant["grant_digest"] != _grant_digest(binding):
        raise FilmError("RELAY_AUTH_DENIED: grant digest mismatch")
    if grant["issuer"] not in worker.trusted_issuers:
        raise FilmError("RELAY_AUTH_DENIED: unknown grant issuer")
    if grant["audience"] != worker.worker_id:
        raise FilmError("RELAY_AUTH_DENIED: grant audience is not this "
                        "worker")
    if grant["peer"] != worker.expected_peer:
        raise FilmError("RELAY_AUTH_DENIED: unexpected coordinator peer")
    if grant["connection_digest"] != worker.connection_digest:
        raise FilmError("RELAY_AUTH_DENIED: grant is bound to a different "
                        "connection")
    if grant["credential_epoch"] != worker.credential_epoch:
        raise FilmError("RELAY_AUTH_DENIED: credential epoch changed; the "
                        "grant is void")
    if grant["job_key"] != job_key \
            or grant["attempt_id"] != attempt_id \
            or grant["snapshot_digest"] != snapshot_digest:
        raise FilmError("RELAY_AUTH_DENIED: grant is bound to a different "
                        "job/attempt/snapshot")
    if any(digest not in grant["object_digests"]
           for digest in object_digests):
        raise FilmError("RELAY_AUTH_DENIED: request object is outside the "
                        "granted objects")
    if any(member not in grant["member_ids"] for member in member_ids):
        raise FilmError("RELAY_AUTH_DENIED: request member is outside the "
                        "granted members")
    if now_ms >= grant["expires_at_ms"]:
        raise FilmError("RELAY_AUTH_DENIED: grant expired")
    if revoked and grant["grant_digest"] in revoked:
        raise FilmError("RELAY_AUTH_DENIED: grant was revoked")
    if needed_bytes > grant["max_bytes"]:
        raise FilmError("RELAY_AUTH_DENIED: request exceeds grant "
                        "max_bytes")
    if not _covered_by(output_range, grant["ranges"]):
        raise FilmError("RELAY_AUTH_DENIED: output range outside the "
                        "granted ranges")
    return True


def check_packet(packet):
    """No user credential, no arbitrary fetch, no callback URL — ever."""
    if type(packet) is not dict or set(packet.keys()) - PACKET_FIELDS:
        raise FilmError("Malformed worker packet fields")

    def _scan(node):
        if type(node) is dict:
            for key, value in node.items():
                if key in FORBIDDEN_PACKET_KEYS:
                    raise FilmError(
                        f"PACKET_SECRET_FORBIDDEN: worker packets never "
                        f"carry {key}; the relay is coordinator-mediated")
                _scan(value)
        elif type(node) is list:
            for item in node:
                _scan(item)
    _scan(packet)
    return packet


# -- deterministic render recipe (shared frame contract) ------------------------

def render_frame_bytes(frame_index, contract, snapshot_digest, recipe_digest):
    """The deterministic frame a job's recipe pins.

    Both the local worker and the fake remote run this same recipe over the
    same FrameStream contract, so identical plans produce identical frame
    digests — the "same frame contract across local and remote" check.
    A worker that produces anything else fails the coordinator's
    independent verification.
    """
    size = contract_buffer_bytes(contract)
    seed = hashlib.sha256(canon_bytes({
        "recipe_digest": recipe_digest,
        "snapshot_digest": snapshot_digest,
        "frame_index": frame_index,
        "width": contract["width"], "height": contract["height"],
        "pixel_format": contract["pixel_format"],
        "color_space": contract["color_space"],
        "transfer": contract["transfer"]})).digest()
    out = bytearray()
    counter = 0
    while len(out) < size:
        out.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(out[:size])


def expected_frame_digest(frame_index, contract, snapshot_digest,
                          recipe_digest):
    """The digest the coordinator recomputes — independent of the
    worker's COMPLETE statement."""
    return hashlib.sha256(render_frame_bytes(
        frame_index, contract, snapshot_digest, recipe_digest)).hexdigest()


# -- receipts ------------------------------------------------------------------

def make_receipt(*, actor, job_key, attempt_id, request_id,
                 snapshot_digest, plan_revision, covered_range, nonce, kind,
                 grant_digest, members=(), evidence):
    if kind not in RECEIPT_KINDS:
        raise FilmError(f"Unknown receipt kind: {kind}")
    # Receipts carry the request nonce's sha256 digest — never the raw
    # nonce.
    _sha(nonce, "receipt nonce")
    return {"receipt_id": f"rcpt-{uuid.uuid4().hex[:16]}",
            "actor": actor, "job_key": job_key, "attempt_id": attempt_id,
            "request_id": request_id, "snapshot_digest": snapshot_digest,
            "plan_revision": plan_revision,
            "covered_range": list(covered_range), "nonce": nonce,
            "kind": kind, "members": [dict(m) for m in members],
            "grant_digest": grant_digest, "evidence": evidence}


def check_receipt(receipt):
    if type(receipt) is not dict or set(receipt.keys()) != RECEIPT_FIELDS:
        raise FilmError("Receipt must hold exactly the contracted fields")
    if receipt["kind"] not in RECEIPT_KINDS:
        raise FilmError(f"Unknown receipt kind: {receipt['kind']}")
    for field in ("actor", "job_key", "request_id"):
        if type(receipt[field]) is not str or not receipt[field]:
            raise FilmError(f"receipt.{field} must be a non-empty string")
    _range(receipt["covered_range"], "receipt.covered_range")
    for field in ("snapshot_digest", "grant_digest"):
        _sha(receipt[field], f"receipt.{field}")
    if type(receipt["attempt_id"]) is not int or receipt["attempt_id"] < 1:
        raise FilmError("receipt.attempt_id must be a positive integer")
    if type(receipt["plan_revision"]) is not int \
            or receipt["plan_revision"] < 1:
        raise FilmError("receipt.plan_revision must be a positive integer")
    _sha(receipt["nonce"], "receipt.nonce")
    if type(receipt["members"]) is not list:
        raise FilmError("receipt.members must be a list")
    for member in receipt["members"]:
        if type(member) is not dict or set(member.keys()) != MEMBER_FIELDS:
            raise FilmError("receipt members hold frame_index and sha256")
        if type(member["frame_index"]) is not int \
                or member["frame_index"] < 0:
            raise FilmError("member frame_index must be >= 0")
        _sha(member["sha256"], "member sha256")
        if not receipt["covered_range"][0] <= member["frame_index"] \
                < receipt["covered_range"][1]:
            raise FilmError("member frame_index outside covered_range")
    return receipt


class Worker:
    """The WorkerProtocol 1 surface every worker implements.

    probe / preflight / submit / attach_resume / cancel /
    verify_artifact / release_workspace. There is no polling loop: the
    coordinator calls status/collect explicitly, once per decision.
    """

    kind = "LOCAL_NATIVE"
    evidence_class = "LOCAL_EXECUTION"
    qualification_state = "UNQUALIFIED"

    def __init__(self, worker_id, *, expected_peer="coordinator-user-desktop",
                 trusted_issuers=("coordinator",), credential_epoch=1,
                 endpoint=None, connection_digest=None, revocations=None):
        self.worker_id = worker_id
        self.expected_peer = expected_peer
        self.trusted_issuers = set(trusted_issuers)
        self.credential_epoch = credential_epoch
        # The pinned endpoint identity; there is no inbound listener and a
        # packet naming any other endpoint is a refused redirect.
        self.endpoint = endpoint or worker_id
        # Digest of this worker's authenticated connection/session — a
        # grant bound to any other connection is void.
        self.connection_digest = connection_digest or hashlib.sha256(
            f"conn:{worker_id}:{self.endpoint}".encode()).hexdigest()
        # The issuer-published RevocationRegistry the relay consults —
        # injected when a coordinator attaches this worker, never
        # hand-assembled per job.
        self.revocations = revocations

    def authenticate(self, packet, needed_bytes, now_ms):
        check_packet(packet)
        if packet["endpoint"] != self.endpoint:
            raise FilmError("RELAY_AUTH_DENIED: cross-host redirect refused; "
                            "packets go only to the pinned endpoint")
        if self.revocations is None:
            # Fail closed: a worker not bound to its issuer's revocation
            # registry cannot tell whether a grant was revoked.
            raise FilmError("RELAY_AUTH_DENIED: worker is not bound to the "
                            "issuer's revocation registry")
        return check_grant(packet["grant"], worker=self,
                           job_key=packet["job_key"],
                           attempt_id=packet["attempt_id"],
                           snapshot_digest=packet["snapshot_digest"],
                           output_range=packet["output_range"],
                           object_digests=packet.get(
                               "input_object_digests", ()),
                           member_ids=packet.get("input_members", ()),
                           needed_bytes=needed_bytes, now_ms=now_ms,
                           revoked=self.revocations)

    def probe(self):
        return {"worker_id": self.worker_id, "kind": self.kind,
                "qualification_state": self.qualification_state,
                "evidence_class": self.evidence_class}

    def preflight(self, packet, now_ms=0):
        raise NotImplementedError

    def submit(self, packet, now_ms=0):
        raise NotImplementedError

    def attach_resume(self, request_id):
        raise NotImplementedError

    def status(self, request_id):
        """One explicit status query — never a standing poll."""
        raise NotImplementedError

    def collect_receipts(self, request_id):
        raise NotImplementedError

    def cancel(self, request_id):
        raise NotImplementedError

    def verify_artifact(self, request_id):
        raise NotImplementedError

    def release_workspace(self, job_key):
        raise NotImplementedError


# -- the single coordinator -----------------------------------------------------

def _new_nonce():
    return uuid.uuid4().hex


def _nonce_digest(nonce):
    """Receipts, grants and runtime_state.json carry only this digest;
    the raw nonce stays in memory for the live attempt."""
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


class RevocationRegistry:
    """The issuer-published grant revocation view (RELAY-AUTH).

    One registry object is shared between the issuing coordinator and
    every attached worker: a revocation is visible to the relay the
    moment the coordinator records it — no per-worker push and no
    hand-assembled aliasing. `_load` replaces the contents in place so
    worker bindings survive a coordinator restart.
    """

    def __init__(self, digests=()):
        self._digests = set(digests)

    def add(self, digest):
        self._digests.add(digest)

    def restore(self, digests):
        self._digests = set(digests)

    def __contains__(self, digest):
        return digest in self._digests

    def __iter__(self):
        return iter(self._digests)

    def __len__(self):
        return len(self._digests)


class Coordinator:
    """Serializes job selection, receipts, coverage and the seal.

    Jobs live under their job key — the same identity survives a runtime
    restart through the durable state file, and reconciliation is the only
    way an UNKNOWN job moves. No automatic retry, no standing polling.
    """

    def __init__(self, state_dir=None, *, now_ms=None):
        self.state_dir = Path(state_dir) if state_dir else None
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.RLock()
        self.jobs = {}
        self.seals = {}
        # Grant digests the coordinator has voided — completion,
        # termination, failure confirmation or a new attempt revokes;
        # the relay's check_grant consults this issuer-published
        # registry, shared with every attached worker.
        self.revoked_grants = RevocationRegistry()
        # Workers bound to this coordinator's revocation registry —
        # attached on the first protocol call and re-bound after _load.
        self.workers = {}
        # Raw request nonces for live attempts, keyed by job key —
        # memory only; persisted jobs and receipts carry digests.
        self._nonces = {}
        if self.state_dir:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self._load()

    # -- durable state (minimal; the append-only journal is ANIM-021) -------
    @property
    def _state_file(self):
        return self.state_dir / "runtime_state.json"

    def _persist(self):
        if not self.state_dir:
            return
        document = {"record_type": "coordinator_runtime_state",
                    "format": 1,
                    "jobs": self.jobs,
                    "seals": self.seals,
                    "revoked_grants": sorted(self.revoked_grants)}
        atomic_text(self._state_file, json.dumps(document, indent=2,
                                                 ensure_ascii=False) + "\n")

    def _load(self):
        path = self._state_file
        if not path.exists():
            return
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("record_type") != "coordinator_runtime_state":
            raise FilmError("Not a coordinator runtime state file")
        self.jobs = document["jobs"]
        self.seals = document.get("seals", {})
        # In-place restore keeps the registry object — every attached
        # worker sees the revocations that survived the restart.
        self.revoked_grants.restore(document.get("revoked_grants", ()))
        for worker in self.workers.values():
            worker.revocations = self.revoked_grants
        for job in self.jobs.values():
            job["sealing"] = False        # a crashed seal never held
            if job["state"] == "SUBMITTING":
                # 응답 불명: a persisted SUBMITTING never confirmed
                # acceptance — it reloads as UNKNOWN and reconciles under
                # the same job key / attempt / request id.
                job["state"] = "UNKNOWN"

    # -- helpers ------------------------------------------------------------
    def _job(self, key):
        job = self.jobs.get(key)
        if job is None:
            raise FilmError(f"Unknown job key: {key}")
        return job

    def _set_state(self, job, target, why):
        allowed = TRANSITIONS.get(job["state"], set())
        if target not in allowed:
            raise FilmError(
                f"Illegal job transition {job['state']} -> {target} "
                f"({why})")
        if target in {"CANCEL_CONFIRMED", "FAILED_CONFIRMED"} \
                and self._completion_confirmed(job):
            # Backstop for the completion-confirmed-first rule: only the
            # coordinator's own failed verification (verify_failed) ends
            # confirmed coverage, so FAILED_CONFIRMED never wedges.
            raise FilmError(
                f"COMPLETION_CONFIRMED_FIRST: {job['state']} -> {target} "
                f"refused ({why}); the confirmed completion goes to "
                f"OUTPUT_PENDING_VERIFY")
        job["state"] = target
        # Completion confirmation (exec/storage §3.1, evolution
        # §2.1.1), termination and failure confirmations void the
        # attempt's grant — a relay reuse after them is denied.
        if target in {"OUTPUT_PENDING_VERIFY", "VERIFIED", "ARCHIVED",
                      "CANCEL_CONFIRMED",
                      "FAILED_CONFIRMED"} and job.get("grant_digest"):
            self.revoked_grants.add(job["grant_digest"])
            self._nonces.pop(job["job_key"], None)

    def _completion_confirmed(self, job):
        """A complete, valid receipt set for this still-open attempt is
        held and the coordinator's own verification has not rejected it.
        A closed (terminal) attempt never counts."""
        return job["state"] in LIVE_ATTEMPT_STATES and self._complete(job) \
            and not job.get("verify_failed")

    def _hold_confirmed_completion(self, job, answer):
        """The completion-confirmed-first rule, for every worker answer.

        Returns False when no completion is confirmed — the caller applies
        the answer normally. Otherwise the job goes to (or stays in)
        OUTPUT_PENDING_VERIFY through the listed transitions, coverage is
        kept, a cancel/failure answer is recorded only as an observation,
        and True tells the caller to apply nothing else: only
        verify_outputs decides VERIFIED or FAILED_CONFIRMED.
        """
        if not self._completion_confirmed(job):
            return False
        why = f"completion confirmed first; worker answered {answer}"
        if job["state"] == "SUBMITTING":
            self._set_state(job, "RUNNING", why)
        if job["state"] in {"RUNNING", "UNKNOWN", "CANCEL_REQUESTED"}:
            self._set_state(job, "OUTPUT_PENDING_VERIFY", why)
        if answer in ADVERSE_ANSWERS:
            job.setdefault("worker_observations", []).append(
                {"answer": answer, "state": job["state"],
                 "attempt_id": job["attempt_id"]})
            log.warning("job %s attempt %s: worker answered %s after its "
                        "completion was confirmed; kept in %s",
                        job["job_key"], job["attempt_id"], answer,
                        job["state"])
        return True

    def _fence_unknown(self, job, action):
        if action in UNKNOWN_FENCED and job["state"] in {
                "UNKNOWN", "CANCEL_REQUESTED"}:
            raise FilmError(
                f"UNKNOWN_FENCED: {action} is refused while the job is "
                f"{job['state']}; reconcile the existing identity first")

    def _attach(self, worker):
        """Bind the worker to this coordinator's revocation registry.

        The relay consults the issuer-published set itself — a revoke is
        enforced the moment it is recorded, and re-binding on every
        protocol call lets a restarted coordinator take over the same
        workers without any manual set aliasing.
        """
        self.workers[worker.worker_id] = worker
        worker.revocations = self.revoked_grants

    # -- planning -------------------------------------------------------------
    def plan_jobs(self, plan, *, evidence=None,
                  additional_charges_approved=False, max_attempts=2):
        """Register every operation; refuses to run without a valid plan."""
        assert_executable(plan, evidence=evidence,
                          additional_charges_approved=
                          additional_charges_approved)
        sha = plan_sha(plan)
        # The relay input scope the plan declares for workers — grants
        # bind exactly these object digests / member ids and nothing else.
        input_objects = sorted({e["object_digest"]
                                for e in plan["transfer_edges"]
                                if e["to_role"] == "WORKER"
                                and e["object_digest"]})
        input_members = sorted({m for e in plan["transfer_edges"]
                                if e["to_role"] == "WORKER"
                                for m in e["member_ids"]})
        made = []
        with self._lock:
            for operation in plan["operations"]:
                key = job_key(plan, operation)
                if key in self.jobs:
                    raise FilmError(
                        "JOB_IDENTITY_EXISTS: a job with this snapshot/"
                        "operation/range/recipe/runtime already exists; "
                        "an UNKNOWN submission is reconciled, never "
                        "re-keyed")
                job = {"job_key": key, "state": "PLANNED",
                       "plan_sha": sha,
                       "plan_revision": plan["plan_revision"],
                       "snapshot_digest": plan["snapshot_digest"],
                       "operation": dict(operation),
                       "output_range": list(operation["output_range"]),
                       "route": operation["route"],
                       "worker_id": operation["worker"],
                       "input_object_digests": input_objects,
                       "input_member_ids": input_members,
                       "attempt_id": 0, "request_id": None, "nonce": None,
                       "grant_digest": None,
                       "attempts_used": 0, "max_attempts": max_attempts,
                       "covered": [], "receipts": [],
                       "members": {}, "sealing": False, "sealed": False,
                       "qualification_state": "UNQUALIFIED"}
                self.jobs[key] = job
                made.append(key)
            self._persist()
        return made

    def reserve(self, key):
        with self._lock:
            job = self._job(key)
            self._fence_unknown(job, "reserve")
            self._set_state(job, "RESERVED", "resource reservation")
            self._persist()
        return job["state"]

    def release_reservation(self, key):
        """Fenced while UNKNOWN: the spend stays booked until reconcile."""
        with self._lock:
            job = self._job(key)
            self._fence_unknown(job, "release_reservation")
            if job["state"] in {"CANCEL_CONFIRMED", "FAILED_CONFIRMED",
                                "ARCHIVED"}:
                job["reservation_released"] = True
            else:
                raise FilmError("Reservations release only after a "
                                "confirmed terminal/verified state")
            self._persist()

    # -- manual packet route ---------------------------------------------------
    def waiting_user(self, key):
        """MANUAL_PACKET: the reservation waits for the user's own run."""
        with self._lock:
            job = self._job(key)
            self._fence_unknown(job, "waiting_user")
            self._set_state(job, "WAITING_USER",
                            "manual packet waits for the user")
            self._persist()
            return job["state"]

    def attach_resume(self, key, *, frame_contract=None):
        """WAITING_USER -> SUBMITTING on the user's explicit import.

        A manual packet has no automatic attach: this call mints the
        attempt and request identity the imported receipts bind to. The
        pseudo grant digest ties them to this exact attempt.
        """
        with self._lock:
            job = self._job(key)
            self._set_state(job, "SUBMITTING",
                            "user attached a manual packet run")
            if frame_contract is not None:
                job["frame_contract"] = dict(frame_contract)
            self._new_attempt(job)
            job["grant_digest"] = hashlib.sha256(canon_bytes({
                "manual_packet": job["request_id"], "nonce": job["nonce"],
                "job_key": key,
                "attempt_id": job["attempt_id"]})).hexdigest()
            self._persist()
            return job["state"]

    # -- submit ----------------------------------------------------------------
    def _new_attempt(self, job):
        if job.get("grant_digest"):
            # Issuing a new attempt voids the previous grant.
            self.revoked_grants.add(job["grant_digest"])
        job["attempt_id"] += 1
        job["attempts_used"] += 1
        job["request_id"] = f"req-{uuid.uuid4().hex[:16]}"
        # The raw nonce stays in memory for the live attempt; the job,
        # grant, packet and receipts carry only its sha256 digest.
        raw_nonce = _new_nonce()
        self._nonces[job["job_key"]] = raw_nonce
        job["nonce"] = _nonce_digest(raw_nonce)
        job["grant_digest"] = None

    def _issue_grant(self, job, worker, input_bytes):
        grant = issue_grant(
            issuer="coordinator", audience=worker.worker_id,
            peer=worker.expected_peer,
            connection_digest=worker.connection_digest,
            credential_epoch=worker.credential_epoch,
            job_key=job["job_key"], attempt_id=job["attempt_id"],
            snapshot_digest=job["snapshot_digest"],
            object_digests=job.get("input_object_digests", ()),
            member_ids=job.get("input_member_ids", ()),
            ranges=[job["output_range"]] +
                   [list(r) for r in job["operation"]["halo_ranges"]],
            max_bytes=max(input_bytes, 1) + 8 * contract_buffer_bytes(
                job["frame_contract"]),
            expires_at_ms=self.now_ms() + 60_000,
            nonce=job["nonce"])
        job["grant_digest"] = grant["grant_digest"]
        return grant

    def _drain(self, receipts, worker):
        """Re-delivered receipts (at-least-once transport) are dropped by
        receipt_id; forged or conflicting receipts still raise inside
        receive_receipt."""
        for receipt in receipts:
            job = self._job(receipt.get("job_key", ""))
            if self._seen(job, receipt.get("receipt_id")):
                continue
            self.receive_receipt(receipt, worker)

    @staticmethod
    def _seen(job, receipt_id):
        return any(r["receipt_id"] == receipt_id
                   for r in job["receipts"] + job.get("late_receipts", []))

    def collect(self, worker, key):
        """Explicit drain of the worker's pending receipts — one call,
        no standing poll."""
        self._attach(worker)
        job = self._job(key)
        self._drain(worker.collect_receipts(job["request_id"]), worker)
        return self._job(key)["state"]

    def submit(self, worker, key, *, input_bytes=1024, frame_contract=None):
        """Drive RESERVED -> SUBMITTING -> RUNNING / UNKNOWN.

        A lost submission response is UNKNOWN — the attempt and request id
        are kept and the job is reconciled, never re-submitted under a new
        identity.
        """
        with self._lock:
            job = self._job(key)
            self._attach(worker)
            self._fence_unknown(job, "submit")
            if worker.worker_id != job["worker_id"]:
                raise FilmError("SUBSTITUTE_WORKER_FENCED: the plan binds "
                                "this range to a different worker")
            for other in self.jobs.values():
                if other is not job and other["state"] == "UNKNOWN" \
                        and other["output_range"][0] < job["output_range"][1] \
                        and job["output_range"][0] < other["output_range"][1]:
                    raise FilmError(
                        "UNKNOWN_FENCED: an UNKNOWN job overlaps this "
                        "range; a substitute submission is refused")
            if "SUBMITTING" not in TRANSITIONS.get(job["state"], set()):
                raise FilmError(f"Illegal job transition {job['state']} -> "
                                f"SUBMITTING (submit)")
            saved = {k: job.get(k) for k in (
                "frame_contract", "attempt_id", "attempts_used",
                "request_id", "nonce", "grant_digest")}
            saved_nonce = self._nonces.get(key)
            if frame_contract is not None:
                job["frame_contract"] = dict(frame_contract)
            if job.get("frame_contract") is None:
                raise FilmError("submit requires the job's FrameStream "
                                "contract")
            self._new_attempt(job)
            grant = self._issue_grant(job, worker, input_bytes)
            packet = {"job_key": job["job_key"],
                      "attempt_id": job["attempt_id"],
                      "request_id": job["request_id"],
                      "snapshot_digest": job["snapshot_digest"],
                      "plan_revision": job["plan_revision"],
                      "operation": dict(job["operation"]),
                      "frame_contract": dict(job["frame_contract"]),
                      "grant": grant,
                      "endpoint": getattr(worker, "endpoint",
                                          worker.worker_id),
                      "input_members": list(
                          job.get("input_member_ids", ())),
                      "input_object_digests": list(
                          job.get("input_object_digests", ())),
                      "output_range": job["output_range"],
                      "receipt_nonce": job["nonce"],
                      "input_bytes": input_bytes}
            check_packet(packet)
            try:
                worker.preflight(packet, now_ms=self.now_ms())
            except FilmError:
                # Refused before the worker accepted anything (auth,
                # scratch cap, unreachable): no SUBMITTING is recorded,
                # the attempt is not consumed and the minted grant is
                # void. Another submit is the user's explicit call.
                self.revoked_grants.add(job["grant_digest"])
                job.update(saved)
                if saved_nonce is None:
                    self._nonces.pop(key, None)
                else:
                    self._nonces[key] = saved_nonce
                self._persist()
                raise
            self._set_state(job, "SUBMITTING", "submit")
            self._persist()
        try:
            receipt = worker.submit(packet, now_ms=self.now_ms())
        except ConnectionDropped:
            with self._lock:
                self._set_state(job, "UNKNOWN",
                                "submission response lost")
                self._persist()
            return "UNKNOWN"
        with self._lock:
            self.receive_receipt(receipt, worker)
            self._persist()
            return job["state"]

    # -- receipts --------------------------------------------------------------
    def receive_receipt(self, receipt, worker):
        """Bind and gate one callback. Out-of-order is fine; forged is not."""
        check_receipt(receipt)
        with self._lock:
            self._attach(worker)
            job = self._job(receipt["job_key"])
            if receipt["actor"] != worker.worker_id:
                raise FilmError("RECEIPT_REJECTED: actor is not the "
                                "submitting worker")
            if receipt["actor"] != job["worker_id"]:
                raise FilmError("RECEIPT_REJECTED: receipt from an "
                                "unauthorised actor for this job")
            if receipt["grant_digest"] != job["grant_digest"]:
                raise FilmError("RECEIPT_REJECTED: grant digest does not "
                                "match the issued grant")
            if receipt["nonce"] != job["nonce"]:
                raise FilmError("RECEIPT_REJECTED: wrong request nonce")
            if receipt["attempt_id"] != job["attempt_id"]:
                raise FilmError("RECEIPT_REJECTED: stale attempt revision")
            if receipt["plan_revision"] != job["plan_revision"]:
                raise FilmError("RECEIPT_REJECTED: stale plan revision")
            if receipt["snapshot_digest"] != job["snapshot_digest"]:
                raise FilmError("RECEIPT_REJECTED: stale snapshot")
            if self._seen(job, receipt["receipt_id"]):
                raise FilmError("RECEIPT_REJECTED: duplicate receipt")
            rng = receipt["covered_range"]
            out = job["output_range"]
            if rng[0] < out[0] or rng[1] > out[1]:
                raise FilmError("RECEIPT_REJECTED: covered range outside "
                                "the job's output range")
            if job["state"] not in LIVE_ATTEMPT_STATES:
                # The attempt is closed: the receipt is kept as a late
                # observation (digests only) and never touches coverage,
                # members or state — a terminal job is never reopened.
                late = {"receipt_id": receipt["receipt_id"],
                        "kind": receipt["kind"],
                        "attempt_id": receipt["attempt_id"],
                        "request_id": receipt["request_id"],
                        "covered_range": list(rng),
                        "state": job["state"],
                        "receipt_sha256": hashlib.sha256(
                            canon_bytes(receipt)).hexdigest()}
                job.setdefault("late_receipts", []).append(late)
                log.warning("job %s attempt %s: late %s receipt after %s "
                            "recorded as an observation", job["job_key"],
                            receipt["attempt_id"], receipt["kind"],
                            job["state"])
                self._persist()
                return late
            # Range overlap is a coverage property: only COMPLETE receipts
            # claim frame coverage; outcome receipts describe the job.
            if receipt["kind"] == "COMPLETE" and any(
                    c[0] < rng[1] and rng[0] < c[1]
                    for c in job["covered"]):
                raise FilmError("RECEIPT_REJECTED: overlapping range "
                                "already covered")
            record = dict(receipt)
            job["receipts"].append(record)
            kind = receipt["kind"]
            if kind == "COMPLETE":
                job["covered"].append(rng)
                for member in receipt["members"]:
                    job["members"][str(member["frame_index"])] = member["sha256"]
            if self._hold_confirmed_completion(job, kind):
                pass
            elif kind in {"ACCEPTED", "RUNNING"}:
                if job["state"] == "SUBMITTING":
                    self._set_state(job, "RUNNING", "worker accepted")
                elif job["state"] == "UNKNOWN":
                    self._set_state(job, "RUNNING",
                                    "existing job confirmed")
            elif kind == "FAILED_CONFIRMED":
                if job["state"] in {"RUNNING", "UNKNOWN"}:
                    self._set_state(job, "FAILED_CONFIRMED",
                                    "failure confirmed")
            elif kind == "CANCEL_CONFIRMED":
                if job["state"] in {"CANCEL_REQUESTED", "UNKNOWN"}:
                    self._set_state(job, "CANCEL_CONFIRMED",
                                    "termination confirmed")
            self._persist()
            return record

    def _complete(self, job):
        start, end = job["output_range"]
        covered = sorted(job["covered"])
        cursor = start
        for lo, hi in covered:
            if lo != cursor:
                return False
            cursor = hi
        return cursor == end

    def coverage(self, key):
        """Assembled coverage plus the missing ranges, in order."""
        with self._lock:
            job = self._job(key)
            covered = sorted(tuple(r) for r in job["covered"])
            missing = []
            cursor = job["output_range"][0]
            for lo, hi in covered:
                if lo > cursor:
                    missing.append([cursor, lo])
                cursor = max(cursor, hi)
            if cursor < job["output_range"][1]:
                missing.append([cursor, job["output_range"][1]])
            return {"covered": [list(r) for r in covered],
                    "missing": missing,
                    "complete": not missing}

    # -- verification and seal ---------------------------------------------------
    def verify_outputs(self, key):
        """Independent verification: the worker's COMPLETE is re-derived
        from pinned inputs, never trusted. Mismatch -> FAILED_CONFIRMED."""
        with self._lock:
            job = self._job(key)
            if job["state"] != "OUTPUT_PENDING_VERIFY":
                raise FilmError("verify_outputs requires "
                                "OUTPUT_PENDING_VERIFY")
            contract = job.get("frame_contract")
            if contract is None:
                raise FilmError("Job has no frame contract to verify "
                                "against")
            start, end = job["output_range"]
            ok = True
            for index in range(start, end):
                expected = expected_frame_digest(
                    index, contract, job["snapshot_digest"],
                    job["operation"]["recipe_digest"])
                if job["members"].get(str(index)) != expected:
                    ok = False
                    break
            if not ok:
                # Verification ran and failed the confirmed output — a
                # later resume re-runs the range; the coverage is not a
                # completion still pending verification.
                job["verify_failed"] = True
            self._set_state(job, "VERIFIED" if ok else "FAILED_CONFIRMED",
                            "coordinator re-derived frame digests")
            self._persist()
            return job["state"]

    def seal(self, key, *, _entered=None, _wait=None):
        """VERIFIED -> ARCHIVED, compare-and-swap under one coordinator.

        A concurrent seal is refused while one is in progress; a repeat
        after completion returns the same seal record (idempotent by
        identity, never duplicated).
        """
        with self._lock:
            job = self._job(key)
            if job["sealed"]:
                return self.seals[key]
            if job["sealing"]:
                raise FilmError("SEAL_IN_PROGRESS: a concurrent seal "
                                "attempt is blocked")
            if job["state"] != "VERIFIED":
                raise FilmError("seal requires VERIFIED coverage")
            if not self._complete(job):
                raise FilmError("seal requires complete verified coverage")
            job["sealing"] = True
        try:
            if _entered is not None:
                _entered()
            if _wait is not None:
                _wait.wait(10)
            with self._lock:
                seal = {"job_key": key,
                        "snapshot_digest": job["snapshot_digest"],
                        "output_range": job["output_range"],
                        "members": dict(job["members"]),
                        "plan_sha": job["plan_sha"],
                        "qualification_state": "UNQUALIFIED"}
                self._set_state(job, "ARCHIVED", "coverage sealed")
                job["sealed"] = True
                self.seals[key] = seal
                self._persist()
                return seal
        finally:
            with self._lock:
                job["sealing"] = False

    # -- cancel ------------------------------------------------------------------
    def request_cancel(self, worker, key):
        """Cancel is a request until termination is confirmed.

        RESERVED/WAITING_USER cancel before submit; SUBMITTING/RUNNING/
        OUTPUT_PENDING_VERIFY go to CANCEL_REQUESTED. A completion that was
        confirmed first still lands in OUTPUT_PENDING_VERIFY — the output
        is verified, not dropped. A lost cancel answer is UNKNOWN.
        """
        with self._lock:
            job = self._job(key)
            self._attach(worker)
            if job["state"] == "PLANNED":
                raise FilmError("Cannot cancel a PLANNED job; reserve and "
                                "submit boundaries have not started")
            if job["state"] in {"RESERVED", "WAITING_USER"}:
                self._set_state(job, "CANCEL_CONFIRMED",
                                "cancelled before submit")
                self._persist()
                return job["state"]
            if job["state"] in {"VERIFIED", "ARCHIVED", "CANCEL_CONFIRMED",
                                "FAILED_CONFIRMED"}:
                raise FilmError(f"Cannot cancel a job in {job['state']}; "
                                "past seals are not rewritten")
            self._set_state(job, "CANCEL_REQUESTED", "cancel requested")
            self._persist()
        try:
            outcome = worker.cancel(job["request_id"])
        except ConnectionDropped:
            with self._lock:
                self._set_state(job, "UNKNOWN", "cancel outcome unknown")
                self._persist()
            return job["state"]
        with self._lock:
            self._drain(outcome.get("receipts", ()), worker)
            answered = outcome.get("outcome")
            if self._hold_confirmed_completion(job, answered):
                # 완료가 먼저 확정: a completion confirmed before the
                # cancel is verified, never dropped — whatever the worker
                # answers and whether the receipts just drained or were
                # already collected.
                pass
            elif job["state"] == "CANCEL_REQUESTED" \
                    and answered in {"CANCELLED", "NOT_FOUND"}:
                # 종료 또는 미접수 확인: termination or never-accepted
                # is a confirmed cancel, not a refund record.
                self._set_state(job, "CANCEL_CONFIRMED",
                                "worker confirmed termination or "
                                "never-accepted")
            # RUNNING: cancel still in flight; explicit reconcile.
            self._persist()
            return job["state"]

    # -- reconciliation -------------------------------------------------------------
    def reconcile(self, worker, key):
        """One explicit status check for an UNKNOWN / in-flight job.

        The only path out of UNKNOWN: the existing identity is queried,
        late receipts are ingested, and the result moves the job. Nothing
        is resubmitted and no reservation is released here.
        """
        with self._lock:
            job = self._job(key)
            self._attach(worker)
            if job["state"] == "SUBMITTING":
                # 응답 불명: acceptance was never confirmed — the existing
                # identity reconciles as UNKNOWN, never a resubmit.
                self._set_state(job, "UNKNOWN", "submission outcome "
                                "unknown")
                self._persist()
            if job["state"] not in {"UNKNOWN", "CANCEL_REQUESTED",
                                    "RUNNING", "OUTPUT_PENDING_VERIFY"}:
                return job["state"]
            request_id = job["request_id"]
        if worker.worker_id != job["worker_id"]:
            raise FilmError("reconcile must query the bound worker")
        try:
            report = worker.status(request_id)
        except ConnectionDropped:
            with self._lock:
                job = self._job(key)
                # runtime 종료·상태 불명: unreachable moves RUNNING /
                # OUTPUT_PENDING_VERIFY / CANCEL_REQUESTED to UNKNOWN and
                # keeps the fence armed; nothing is resubmitted.
                if job["state"] != "UNKNOWN":
                    self._set_state(job, "UNKNOWN",
                                    "worker unreachable; status unknown")
                self._persist()
                return job["state"]
        with self._lock:
            job = self._job(key)
            self._drain(report.get("receipts", ()), worker)
            state = report.get("state")
            current = job["state"]
            if self._hold_confirmed_completion(job, state):
                # Stored receipts confirm the completion: whatever the
                # worker now says (COMPLETE, CANCELLED, NOT_FOUND, FAILED)
                # the coverage is kept for the coordinator's verification.
                pass
            elif state == "COMPLETE":
                # A COMPLETE statement promotes only on coverage confirmed
                # by stored receipts — never on the worker's word alone.
                pass
            elif state == "NOT_FOUND":
                if current == "CANCEL_REQUESTED":
                    self._set_state(job, "CANCEL_CONFIRMED",
                                    "reconciled: never accepted")
                elif current in {"UNKNOWN", "RUNNING"}:
                    self._set_state(job, "FAILED_CONFIRMED",
                                    "reconciled: never accepted or lost")
            elif state == "RUNNING" and current == "UNKNOWN":
                self._set_state(job, "RUNNING",
                                "reconciled: existing job confirmed")
            elif state == "CANCELLED" and current in {
                    "UNKNOWN", "CANCEL_REQUESTED"}:
                self._set_state(job, "CANCEL_CONFIRMED",
                                "reconciled: termination confirmed")
            elif state == "FAILED" and current in {"RUNNING", "UNKNOWN"}:
                self._set_state(job, "FAILED_CONFIRMED",
                                "reconciled: failure confirmed")
            self._persist()
            return job["state"]

    # -- bounded resume ---------------------------------------------------------
    def resume_failed(self, worker, key, *, user_continued, plan):
        """The only path from FAILED_CONFIRMED: explicit user resume.

        Re-checks the plan identity (quote/price/permission binding), the
        bounded attempt allowance and the UNKNOWN fence before minting a
        new attempt id and submission request id.
        """
        with self._lock:
            job = self._job(key)
            self._attach(worker)
            if job["state"] != "FAILED_CONFIRMED":
                raise FilmError("resume_failed requires FAILED_CONFIRMED")
            if not user_continued:
                raise FilmError("RESUME_REFUSED: a new attempt needs the "
                                "user's explicit continue")
            if plan_sha(plan) != job["plan_sha"]:
                raise FilmError("RESUME_REFUSED: the plan binding changed; "
                                "quote and permission must be re-approved")
            if job["attempts_used"] >= job["max_attempts"]:
                raise FilmError("RESUME_REFUSED: bounded retry allowance "
                                "is exhausted")
            for other in self.jobs.values():
                if other["state"] == "UNKNOWN":
                    raise FilmError("RESUME_REFUSED: an UNKNOWN job is "
                                    "still fenced")
            # The decision rests on the attempt's own FAILED_CONFIRMED
            # state only: receipts arriving after it are late observations
            # and never counted, and the new attempt starts with fresh
            # coverage.
            # §5.3: the explicit resume restarts the attempt lifecycle
            # through the FAILED_CONFIRMED -> RESERVED edge only.
            self._set_state(job, "RESERVED",
                            "explicit user resume: new attempt")
            job["covered"] = []
            job["members"] = {}
            job.pop("verify_failed", None)
            self._persist()
        return self.submit(worker, key)

    # -- reporting -------------------------------------------------------------
    def facet_report(self):
        """Draft completion facets — fake results never qualify a route."""
        return {"node_state": "IN_PROGRESS",
                "qualification_state": "UNQUALIFIED",
                "acceptance_state": "PENDING",
                "release_state": "NOT_AUTHORIZED"}
