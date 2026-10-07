"""Node film-execution-console — the execution workbench read model.

Every worker/backend here is a local fake (FAKE_REMOTE / fake Drive):
no network, no credentials, no paid calls — every report stays
UNQUALIFIED. The read model derives only from the durable journal /
Coordinator / UploadTracker / ArchiveCommitter / committed perf state;
it never mints a second source of truth, never polls, never auto-retries.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from anim_013_kit import make_members, make_png
from anim_014_kit import (CONTRACT, coordinator, members_for, receipt_for,
                          remote_plan)

from engine.archive_commit import ArchiveCommitter
from engine.cli import main as cli_main
from engine.core import FilmError
from engine.durable_journal import DurableJournal, TAIL_CLEAN
from engine.execution_console import (STAGES, ExecutionConsole,
                                      execution_explain, execution_status)
from engine.execution_workers import Coordinator
from engine.execution_workers.fake_remote import FakeRemoteWorker
from engine.storage_backends import ConnectionDropped
from engine.storage_backends.fake_drive import FakeDriveBackend
from engine.upload_tracker import UploadTracker

SNAP = hashlib.sha256(b"console-snapshot").hexdigest()
RECIPE = hashlib.sha256(b"console-recipe").hexdigest()
TOOLCHAIN = hashlib.sha256(b"console-toolchain").hexdigest()
SEQ = hashlib.sha256(b"console-sequence").hexdigest()

UPLOAD = {"chunk_bytes": 128, "resumable": True}
RETRY = {"max_retries": 0, "backoff_base_ms": 0, "max_backoff_ms": 0,
         "max_elapsed_ms": None, "max_requests": None,
         "max_transferred_bytes": None}


class CountingWorker(FakeRemoteWorker):
    """A fake submitter that counts every remote submission."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.submit_calls = 0

    def submit(self, packet, now_ms=0):
        self.submit_calls += 1
        return super().submit(packet, now_ms=now_ms)


class SlowCancelWorker(FakeRemoteWorker):
    """Cancel is a request; the remote keeps running until it later
    confirms termination."""

    def cancel(self, request_id):
        self._check_live()
        if request_id not in self._jobs:
            return {"outcome": "NOT_FOUND", "receipts": []}
        return {"outcome": "RUNNING", "receipts": []}


def running(tmp_path, worker=None, **worker_kw):
    c = coordinator(tmp_path)
    worker = worker or FakeRemoteWorker(**worker_kw)
    plan = remote_plan()
    key = c.plan_jobs(plan)[0]
    c.reserve(key)
    c.submit(worker, key, frame_contract=dict(CONTRACT))
    return c, worker, plan, key


def item(report, ref):
    for i in report["items"]:
        if i["ref"] == ref or i["item_id"] == ref:
            return i
    raise AssertionError(
        f"no item {ref}: {[i['item_id'] for i in report['items']]}")


def console_for(c, **kw):
    return ExecutionConsole(coordinator=c, **kw)


def artifacts():
    return [{"kind": "frames", "members": make_members(3),
             "snapshot_digest": SNAP, "recipe_digest": RECIPE,
             "sequence_digest": SEQ},
            {"kind": "preview", "data": make_png(9)}]


def make_commit(tmp_path, name="commit", backend=None):
    backend = backend or FakeDriveBackend(provider_checksum="sha256")
    c = ArchiveCommitter(tmp_path / name, backend, upload=UPLOAD,
                         retry=RETRY)
    key = c.begin_commit("build-1", artifacts(), snapshot_digest=SNAP,
                         recipe_digest=RECIPE, toolchain_digest=TOOLCHAIN,
                         required_kinds=("frames", "preview"),
                         coverage=[0, 3])
    return c, backend, key


# -- 구현 범위 1: the six stages -------------------------------------------------

def test_job_item_distinguishes_six_stages(tmp_path):
    c, worker, plan, key = running(tmp_path)
    report = console_for(c).status()
    i = item(report, key)
    assert set(i["stages"]) == set(STAGES)
    assert i["stages"]["compose"]["status"] == "RUNNING"
    assert i["stages"]["encode"]["status"] == "NOT_APPLICABLE"
    assert i["stages"]["mux"]["status"] == "NOT_APPLICABLE"
    assert i["stages"]["verify"]["status"] == "PENDING"
    assert i["stages"]["upload"]["status"] == "NOT_APPLICABLE"
    assert i["stages"]["seal"]["status"] == "PENDING"
    # network loss / remote running / upload verify are three
    # independent indicators
    assert i["indicators"]["network"]["state"] == "NONE"
    assert i["indicators"]["remote"]["last_report"] == "ACCEPTED"
    assert i["indicators"]["remote"]["basis"] == "receipt"
    assert i["qualification_state"] == "UNQUALIFIED"


def test_pipeline_item_maps_committed_snapshot_to_six_stages(tmp_path):
    perf = tmp_path / "perf"
    perf.mkdir()
    enc_sha = hashlib.sha256(b"artifact").hexdigest()
    state = {"state_version": 1,
             "input": {"snapshot_digest": SNAP},
             "clean": {"sequence_root": "c" * 64, "frames": [{"key": "k"}]},
             "subbed": {"sequence_root": "s" * 64},
             "encodes": {"clean": {"artifact_sha256": enc_sha,
                                   "verification": {"ok": True},
                                   "encode_digest": "e" * 64},
                         "subbed": {"artifact_sha256": enc_sha,
                                    "verification": {"ok": True},
                                    "encode_digest": "e" * 64}},
             "generated_at": "2026-01-01T00:00:00Z"}
    (perf / "state.json").write_text(json.dumps(state), encoding="utf-8")
    sd = tmp_path / "state"
    sd.mkdir()
    console = ExecutionConsole(state_dir=sd, perf_roots=[perf])
    i = item(console.status(), f"pipeline:{perf.name}")
    assert i["stages"]["compose"]["status"] == "DONE"
    assert i["stages"]["encode"]["status"] == "DONE"
    assert i["stages"]["mux"]["status"] == "DONE"
    assert i["stages"]["verify"]["status"] == "VERIFIED"
    assert i["stages"]["upload"]["status"] == "PENDING"
    assert i["stages"]["seal"]["status"] == "PENDING"


def test_pipeline_item_links_archive_commit_by_snapshot(tmp_path):
    cc, backend, ck = make_commit(tmp_path)
    assert cc.run(ck)["state"] == "SEALED"
    perf = tmp_path / "perf"
    perf.mkdir()
    (perf / "state.json").write_text(json.dumps({
        "state_version": 1, "input": {"snapshot_digest": SNAP},
        "clean": {"sequence_root": "c" * 64, "frames": []},
        "subbed": {"sequence_root": "s" * 64},
        "encodes": {"clean": {"artifact_sha256": "a" * 64,
                              "verification": {"ok": True}},
                    "subbed": {"artifact_sha256": "b" * 64,
                               "verification": {"ok": True}}},
        "generated_at": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    sd = tmp_path / "state"
    sd.mkdir()
    console = ExecutionConsole(state_dir=sd, journal_dirs=[tmp_path / "commit"],
                             perf_roots=[perf])
    i = item(console.status(), f"pipeline:{perf.name}")
    assert i["stages"]["upload"]["status"] == "UPLOADED"
    assert i["stages"]["seal"]["status"] == "SEALED"


# -- 완료 판정 1: CANCEL_REQUESTED is never terminal -------------------------------

def test_cancel_requested_is_never_terminal(tmp_path):
    worker = SlowCancelWorker()
    c, worker, plan, key = running(tmp_path, worker=worker)
    state = c.request_cancel(worker, key)
    assert state == "CANCEL_REQUESTED"
    console = console_for(c)
    i = item(console.status(), key)
    own = i["stages"]["compose"]
    assert own["status"] == "CANCEL_REQUESTED"
    assert "not terminal" in own["detail"] or "미확정" in own["detail"]
    assert not i["closed"]
    assert i["in_flight"]
    assert i["cancel"]["requested"] and not i["cancel"]["confirmed"]
    d = console.explain(key)
    assert d["new_attempt"]["allowed"] is False
    assert any("not terminal" in n or "미확정" in n
               for n in d["notes"])
    recovery = {e["action"]: e for e in d["recovery"]}
    assert recovery["status_query"]["allowed"]
    assert recovery["new_attempt"]["allowed"] is False


def test_cancel_complete_race_shows_completion_first(tmp_path):
    c, worker, plan, key = running(tmp_path, auto_complete=True)
    c.collect(worker, key)
    assert c.jobs[key]["state"] == "OUTPUT_PENDING_VERIFY"
    state = c.request_cancel(worker, key)
    assert state == "OUTPUT_PENDING_VERIFY"      # completion confirmed first
    console = console_for(c)
    i = item(console.status(), key)
    assert i["state"] == "OUTPUT_PENDING_VERIFY"
    assert i["stages"]["verify"]["status"] == "READY"
    assert i["cancel"]["requested"]
    assert i["cancel"]["completion_confirmed"]
    d = console.explain(key)
    assert any("completion was confirmed first" in n for n in d["notes"])
    # the surviving offer is verify — never a blind drop of the output
    assert console.run_action(key, "verify")["result"] == "OK"
    assert c.jobs[key]["state"] == "VERIFIED"


# -- network loss / disconnect -----------------------------------------------------

def test_disconnect_moves_running_to_unknown(tmp_path):
    c, worker, plan, key = running(tmp_path)
    worker.dead = True
    console = console_for(c, workers={"remote-1": worker})
    out = console.run_action(key, "status_query")
    assert out["result"] == "OK" and out["state"] == "UNKNOWN"
    i = item(console.status(), key)
    assert i["state"] == "UNKNOWN"
    assert i["stages"]["compose"]["status"] == "UNKNOWN"
    assert i["indicators"]["remote"]["detail"]
    # revived runtime: the same request id reconciles back
    worker.revive()
    out = console.run_action(key, "status_query")
    assert out["state"] == "RUNNING"
    assert c.jobs[key]["state"] == "RUNNING"


def test_lost_submit_ack_fences_and_reconciles_same_identity(tmp_path):
    c = coordinator(tmp_path)
    worker = CountingWorker()
    worker.drop_submit_ack = True
    plan = remote_plan()
    key = c.plan_jobs(plan)[0]
    c.reserve(key)
    assert c.submit(worker, key, frame_contract=dict(CONTRACT)) == "UNKNOWN"
    assert worker.submit_calls == 1
    console = console_for(c, workers={"remote-1": worker})
    i = item(console.status(), key)
    assert i["state"] == "UNKNOWN"
    assert i["indicators"]["network"]["state"] == "LOST"
    assert i["indicators"]["network"]["lost"] == ["SUBMIT_LOST"]
    # every resubmit-shaped action is refused — reconcile is the only path
    for action in ("resume", "retry", "submit", "resubmit", "new_attempt"):
        out = console.run_action(key, action)
        assert out["result"] == "REFUSED", (action, out)
        assert worker.submit_calls == 1
    out = console.run_action(key, "status_query")
    assert out["result"] == "OK" and out["state"] == "RUNNING"
    assert worker.submit_calls == 1                    # no duplicate submit
    assert c.jobs[key]["request_id"] is not None


def test_unknown_fence_blocks_other_jobs_new_attempt(tmp_path):
    c = coordinator(tmp_path)
    worker = CountingWorker()
    plan = remote_plan()
    key, key2 = c.plan_jobs(plan)
    c.reserve(key)
    c.submit(worker, key, frame_contract=dict(CONTRACT))
    worker.fail(c.jobs[key]["request_id"])
    c.collect(worker, key)
    assert c.jobs[key]["state"] == "FAILED_CONFIRMED"
    # a sibling job loses its submit ack -> UNKNOWN; that fences every
    # new attempt, even on a different (failed) job
    worker.drop_submit_ack = True
    c.reserve(key2)
    assert c.submit(worker, key2,
                    frame_contract=dict(CONTRACT)) == "UNKNOWN"
    console = console_for(c, workers={"remote-1": worker})
    d = console.explain(key)
    na = d["new_attempt"]
    assert na["required"] is True and na["allowed"] is False
    assert "UNKNOWN" in na["reason"]


# -- uploads: partial resume + uploaded-vs-verified ---------------------------------

def test_partial_upload_resumes_same_session_in_process(tmp_path):
    """A live (injected) tracker keeps the session URI in memory —
    resume continues the SAME session from the confirmed offset."""
    backend = FakeDriveBackend(provider_checksum="sha256")
    up_dir = tmp_path / "up"
    journal = DurableJournal(up_dir / "job_journal.jsonl")
    tracker = UploadTracker(journal, backend, upload=UPLOAD, retry=RETRY)
    data = b"D" * 1000                       # 8 chunks of 128
    # request 2 = first chunk: accepts 40 bytes then drops mid-write;
    # request 4 = next chunk (3 is the status re-query): accepts nothing
    # and drops -> the drive aborts at the server-confirmed offset 40
    backend.upload_partial = {2: 40, 4: 0}
    with pytest.raises(FilmError, match="TRANSPORT_RETRY_EXHAUSTED"):
        tracker.put("obj-1", data)
    sd = tmp_path / "state"
    sd.mkdir()
    console = ExecutionConsole(state_dir=sd, journal_dirs=[up_dir],
                             trackers={str(up_dir): tracker},
                             backends={str(up_dir): backend})
    i = item(console.status(), "up-obj-1")
    assert i["stages"]["upload"]["status"] == "PARTIAL"
    assert i["stages"]["upload"]["confirmed"] == 40
    assert i["state"] == "PARTIAL"
    d = console.explain("up-obj-1")
    assert d["stop_point"]["stage"] == "upload"
    rec = {e["action"]: e for e in d["recovery"]}
    assert rec["resume_upload"]["allowed"]
    backend.upload_partial.clear()
    out = console.run_action("up-obj-1", "resume_upload", data=data)
    assert out["result"] == "OK" and out["state"] == "COMPLETE"
    assert backend.object_info("obj-1")["byte_length"] == len(data)
    sent_offsets = [r["offset"] for r in backend.requests
                    if r["op"] == "upload_chunk"]
    assert 40 in sent_offsets             # resend starts at confirmed 40
    sessions = [r for r in console._journals[str(up_dir)].records
                if r["event"] == "UPLOAD_SESSION"]
    assert len(sessions) == 1             # same session, not a new one
    # a re-POST of the same bytes under a different intent is refused
    rec = {e["action"]: e for e in console.explain("up-obj-1")["recovery"]}
    assert rec["new_intent"]["allowed"] is False


def test_partial_upload_new_session_after_restart(tmp_path):
    """A restarted console holds no session URI — resume opens an
    explicit new session for the same intent, fenced + journaled."""
    backend = FakeDriveBackend(provider_checksum="sha256")
    up_dir = tmp_path / "up"
    journal = DurableJournal(up_dir / "job_journal.jsonl")
    # the request budget stops the drive after one confirmed chunk
    tracker = UploadTracker(journal, backend, upload=UPLOAD,
                            retry=dict(RETRY, max_requests=4))
    data = b"D" * 1000
    with pytest.raises(FilmError, match="TRANSPORT_RETRY_EXHAUSTED"):
        tracker.put("obj-1", data)
    sd = tmp_path / "state"
    sd.mkdir()
    console = ExecutionConsole(state_dir=sd, journal_dirs=[up_dir],
                             backends={str(up_dir): backend},
                             upload=dict(UPLOAD),
                             retry=dict(RETRY, max_retries=3))
    i = item(console.status(), "up-obj-1")
    assert i["stages"]["upload"]["confirmed"] == 128
    out = console.run_action("up-obj-1", "resume_upload", data=data)
    assert out["result"] == "OK" and out["state"] == "COMPLETE"
    assert backend.object_info("obj-1")["byte_length"] == len(data)
    journal = DurableJournal(up_dir / "job_journal.jsonl")
    sessions = [r for r in journal.records
                if r["event"] == "UPLOAD_SESSION"]
    fenced = [r for r in journal.records
              if r["event"] == "UPLOAD_SESSION_FENCED"]
    assert len(sessions) == 2          # explicit new session, same intent
    assert fenced                      # the restart-orphaned one is fenced
    assert sessions[1]["data"]["session_seq"] == 2
    intents = [r for r in journal.records if r["event"] == "UPLOAD_INTENT"]
    assert len(intents) == 1           # the intent itself was continued


def test_uploaded_and_verified_are_distinct_facts(tmp_path):
    backend = FakeDriveBackend(provider_checksum="sha256")
    up_dir = tmp_path / "up"
    journal = DurableJournal(up_dir / "job_journal.jsonl")
    tracker = UploadTracker(journal, backend, upload=UPLOAD, retry=RETRY)
    data = make_png(6)
    tracker.put("obj-1", data)
    sd = tmp_path / "state"
    sd.mkdir()
    console = ExecutionConsole(state_dir=sd, journal_dirs=[up_dir],
                             backends={str(up_dir): backend})
    i = item(console.status(), "up-obj-1")
    # transfer fact: UPLOADED; verification: still not done
    assert i["stages"]["upload"]["status"] == "UPLOADED"
    assert i["stages"]["verify"]["status"] == "UPLOADED_UNVERIFIED"
    assert i["indicators"]["upload_verify"]["level"] == "UPLOADED_UNVERIFIED"
    assert i["indicators"]["upload_verify"]["verified"] is False


def test_committed_objects_report_verify_levels(tmp_path):
    cc, backend, ck = make_commit(tmp_path)
    assert cc.run(ck)["state"] == "SEALED"
    sd = tmp_path / "state"
    sd.mkdir()
    console = ExecutionConsole(state_dir=sd,
                             journal_dirs=[tmp_path / "commit"],
                             backends={str(tmp_path / "commit"): backend})
    report = console.status()
    commit = item(report, ck)
    assert commit["stages"]["seal"]["status"] == "SEALED"
    assert commit["stages"]["verify"]["status"] == "VERIFIED"
    assert commit["stages"]["upload"]["status"] == "UPLOADED"
    assert commit["closed"]
    # per-object upload items inherit the commit's recorded verify level
    ups = [i for i in report["items"] if i["kind"] == "upload"]
    assert ups and all(i["stages"]["verify"]["status"] == "VERIFIED"
                       for i in ups)
    d = console.explain(ck)
    assert d["reusable_artifacts"]
    assert all(a["basis"] == "OBJECT_VERIFIED"
               for a in d["reusable_artifacts"])
    assert all(a["sha256"] for a in d["reusable_artifacts"])


def test_lost_session_create_reconciles_then_continues(tmp_path):
    backend = FakeDriveBackend(provider_checksum="sha256")
    # the create answer is lost — the tracker must not mint a second
    # session on its own; reconcile decides what actually landed
    backend.drops["create_upload_session"] = 1
    up_dir = tmp_path / "up"
    journal = DurableJournal(up_dir / "job_journal.jsonl")
    tracker = UploadTracker(journal, backend, upload=UPLOAD, retry=RETRY)
    data = make_png(3)
    with pytest.raises(FilmError, match="UPLOAD_SESSION_UNKNOWN"):
        tracker.put("obj-1", data)
    sd = tmp_path / "state"
    sd.mkdir()
    console = ExecutionConsole(state_dir=sd, journal_dirs=[up_dir],
                             backends={str(up_dir): backend})
    i = item(console.status(), "up-obj-1")
    assert i["stages"]["upload"]["status"] == "UNKNOWN"
    assert i["indicators"]["network"]["state"] == "LOST"
    # resume on an unknown session is refused — reconcile first
    out = console.run_action("up-obj-1", "resume_upload", data=data)
    assert out["result"] == "REFUSED"
    assert "UPLOAD_SESSION_UNKNOWN" in out["reason"]
    # reconcile: the phantom session never stored anything -> the same
    # intent may continue on an explicit new session
    out = console.run_action("up-obj-1", "reconcile_upload")
    assert out["result"] == "OK"
    assert out["answer"]["reconciled"] == "not_stored"
    out = console.run_action("up-obj-1", "resume_upload", data=data)
    assert out["result"] == "OK" and out["state"] == "COMPLETE"
    i = item(console.status(), "up-obj-1")
    assert i["stages"]["upload"]["status"] == "UPLOADED"
    assert i["stages"]["verify"]["status"] == "UPLOADED_UNVERIFIED"


# -- verified checkpoints only are reusable ------------------------------------------

def test_interrupted_job_has_no_reusable_checkpoint(tmp_path):
    c, worker, plan, key = running(tmp_path)
    job = c.jobs[key]
    # partial coverage confirmed by receipt — still not a checkpoint
    c.receive_receipt(receipt_for(
        job, "COMPLETE", [0, 2],
        members=members_for(c, job, [0, 2])), worker)
    assert c.jobs[key]["state"] == "RUNNING"
    d = console_for(c).explain(key)
    assert d["reusable_artifacts"] == []
    assert any("receipt-level" in n for n in d["notes"])
    assert d["stop_point"]["coverage"]["covered"] == [[0, 2]]
    assert d["stop_point"]["coverage"]["missing"] == [[2, 4]]


def test_verified_checkpoint_is_reusable_with_hashes(tmp_path):
    c, worker, plan, key = running(tmp_path, auto_complete=True)
    c.collect(worker, key)
    c.verify_outputs(key)
    assert c.jobs[key]["state"] == "VERIFIED"
    d = console_for(c).explain(key)
    assert len(d["reusable_artifacts"]) == 1
    a = d["reusable_artifacts"][0]
    assert a["type"] == "frame_checkpoint"
    assert a["basis"] == "JOURNAL_CHECKPOINT"
    assert a["covered"] == [[0, 4]]
    assert len(a["members"]) == 4
    assert all(len(h) == 64 for h in a["members"].values())
    assert d["stages"]["verify"]["status"] == "VERIFIED"
    # restart replay: the same checkpoint is still the only reusable fact
    c2 = Coordinator(tmp_path / "state")
    d2 = console_for(c2).explain(key)
    assert d2["reusable_artifacts"][0]["members"] == a["members"]


def _crash_on_nth(journal, event, n):
    """Raise RuntimeError (a process crash) on the nth append of `event`."""
    real_append = journal.append
    seen = {"n": 0}

    def boom(scope, ev, data):
        if ev == event:
            seen["n"] += 1
            if seen["n"] == n:
                raise RuntimeError(f"injected crash at {event}")
        return real_append(scope, ev, data)

    journal.append = boom


def test_commit_reuse_is_only_object_verified(tmp_path):
    cc, backend, ck = make_commit(tmp_path)
    # crash mid-run so only the first object is verified and journaled —
    # the commit dies before OBJECTS_VERIFIED
    _crash_on_nth(cc.journal, "OBJECT_VERIFIED", 2)
    with pytest.raises(RuntimeError):
        cc.run(ck)
    sd = tmp_path / "state"
    sd.mkdir()
    console = ExecutionConsole(state_dir=sd,
                             journal_dirs=[tmp_path / "commit"],
                             backends={str(tmp_path / "commit"): backend})
    report = console.status()
    commit = item(report, ck)
    assert commit["state"] == "OBJECTS_PENDING"
    assert commit["stages"]["verify"]["status"] == "PARTIAL"
    verified_ids = {o["object_id"] for o in commit["objects"]
                    if o["verified"]}
    assert len(verified_ids) == 1
    d = console.explain(ck)
    assert {a["object_id"] for a in d["reusable_artifacts"]} == \
        verified_ids
    assert all(a["sha256"] and a["basis"] == "OBJECT_VERIFIED"
               for a in d["reusable_artifacts"])
    # the same-intent resume is the offered recovery, never a new commit
    rec = {e["action"]: e for e in d["recovery"]}
    assert rec["resume_upload"]["allowed"]


# -- verify mismatch: new attempt, never relabel --------------------------------------

def test_verify_mismatch_needs_new_attempt(tmp_path):
    c, worker, plan, key = running(tmp_path)
    job = c.jobs[key]
    bad = [{"frame_index": i, "sha256": "0" * 64}
           for i in range(*job["output_range"])]
    c.receive_receipt(receipt_for(job, "COMPLETE", job["output_range"],
                                  members=bad), worker)
    assert c.jobs[key]["state"] == "OUTPUT_PENDING_VERIFY"
    assert c.verify_outputs(key) == "FAILED_CONFIRMED"
    console = console_for(c, workers={"remote-1": worker})
    i = item(console.status(), key)
    assert i["verify_failed"]
    assert i["stages"]["verify"]["status"] == "VERIFY_MISMATCH"
    d = console.explain(key)
    assert d["new_attempt"]["required"] is True
    assert d["new_attempt"]["allowed"] is True
    assert "never relabelled" in d["new_attempt"]["reason"]
    assert d["reusable_artifacts"] == []          # the bad output is not
    # the bounded attempt needs the explicit user continue + same plan
    out = console.run_action(key, "new_attempt")
    assert out["result"] == "REFUSED"            # plan missing
    out = console.run_action(key, "new_attempt",
                             plan=plan, user_continued=True)
    assert out["result"] == "OK"
    assert c.jobs[key]["attempt_id"] == 2


# -- journal fencing -----------------------------------------------------------------

def test_fenced_journal_reports_and_refuses(tmp_path):
    c, worker, plan, key = running(tmp_path)
    path = c.journal.path
    raw = path.read_bytes()
    path.write_bytes(raw[:-10])                   # torn tail
    c2 = Coordinator(tmp_path / "state")
    assert c2.journal_fenced
    console = console_for(c2, workers={"remote-1": worker})
    report = console.status()
    assert report["fenced"]
    out = console.run_action(key, "verify")
    assert out["result"] == "REFUSED"
    assert "RECONCIL" in out["reason"].upper()


# -- empty + CLI ----------------------------------------------------------------------

def test_empty_state_dir_reports_empty_queue(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()
    report = execution_status(sd)
    assert report["items"] == []
    assert report["qualification_state"] == "UNQUALIFIED"


def test_missing_state_dir_is_a_clean_error(tmp_path):
    with pytest.raises(FilmError, match="No execution state dir"):
        execution_status(tmp_path / "nope")


def test_cli_execution_status_and_explain(tmp_path, capsys):
    c, worker, plan, key = running(tmp_path)
    sd = tmp_path / "state"
    cli_main(["execution-status", str(sd)])
    report = json.loads(capsys.readouterr().out)
    assert report["console"] == "execution_console"
    assert item(report, key)["stages"]["compose"]["status"] == "RUNNING"
    cli_main(["execution-explain", str(sd), key])
    detail = json.loads(capsys.readouterr().out)
    assert detail["stop_point"]["stage"] == "compose"
    assert detail["stop_point"]["state"] == "RUNNING"
    assert "reusable_artifacts" in detail
    assert "new_attempt" in detail
