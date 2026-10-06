"""ANIM-021 durable worker journal, resumable upload tracker and archive
commit/seal reconciliation (schema §13.1/§16, exec/storage §9-11,
evolution §4.2.1-4.2.2).

Every remote worker and every archive backend is an explicit local fake:
no network, no credentials, no paid calls. The crash injection flips are
monkeypatched side-effect raises and the fake backends' drop/partial/
expiry hooks. Everything here reports UNQUALIFIED — a fake seal never
qualifies a real Drive archive or a real remote worker.
"""
import hashlib
import json
import time

import pytest

from anim_014_kit import (CONTRACT, coordinator, drive, members_for,
                          receipt_for, remote_plan)
from anim_013_kit import make_members, make_png

from engine.animation_schema import canon_bytes
from engine.archive_commit import (ArchiveCommitter, manifest_object_id,
                                   seal_reconcile, validate_archive_seal)
from engine.core import FilmError
from engine.durable_journal import (DurableJournal, GENESIS, TAIL_CLEAN,
                                    TAIL_CORRUPT, TAIL_TRUNCATED,
                                    load_journal, record_digest,
                                    validate_record)
from engine.execution_workers import Coordinator, expected_frame_digest
from engine.execution_workers.fake_remote import FakeRemoteWorker
from engine.journal_cli import (archive_seal_reconcile, journal_reconcile,
                                journal_status)
from engine.storage_backends import ConnectionDropped
from engine.storage_backends.fake_drive import FakeDriveBackend
from engine.storage_backends.local import LocalArchiveBackend
from engine.upload_tracker import UploadTracker

SNAP = hashlib.sha256(b"anim-021-snapshot").hexdigest()
RECIPE = hashlib.sha256(b"anim-021-recipe").hexdigest()
TOOLCHAIN = hashlib.sha256(b"anim-021-toolchain").hexdigest()
SEQUENCE = hashlib.sha256(b"anim-021-sequence").hexdigest()

RETRY = {"max_retries": 3, "backoff_base_ms": 0, "max_backoff_ms": 0,
         "max_elapsed_ms": None, "max_requests": None,
         "max_transferred_bytes": None}
UPLOAD = {"chunk_bytes": 128, "resumable": True}


def events(journal):
    return [r["event"] for r in journal.records]


def crash_on_event(journal, event, times=1):
    """Raise RuntimeError (a process crash) on the event's next append."""
    real_append = journal.append
    left = {"n": times}

    def boom(scope, ev, data):
        if ev == event and left["n"]:
            left["n"] -= 1
            raise RuntimeError(f"injected crash at {event}")
        return real_append(scope, ev, data)

    journal.append = boom
    return left


def rechain(path, mutate):
    """Rewrite a journal file with `mutate(seq, record)` applied and the
    hash chain resealed — only a semantic re-check catches this forgery."""
    records = [json.loads(line)
               for line in path.read_bytes().split(b"\n") if line]
    records = [mutate(seq, r) or r for seq, r in enumerate(records)]
    prev = GENESIS
    blob = []
    for rec in records:
        rec["prev_digest"] = prev
        rec["digest"] = record_digest(rec)
        prev = rec["digest"]
        blob.append(canon_bytes(rec))
    path.write_bytes(b"".join(blob))


def running(tmp_path, **worker_kw):
    """A coordinator with one remote job submitted and RUNNING."""
    c = coordinator(tmp_path)
    worker = FakeRemoteWorker(**worker_kw)
    plan = remote_plan()
    key = c.plan_jobs(plan)[0]
    c.reserve(key)
    c.submit(worker, key, frame_contract=dict(CONTRACT))
    return c, worker, plan, key


def artifacts(*, with_pack=True):
    out = []
    if with_pack:
        out.append({"kind": "frames",
                    "members": make_members(3),
                    "snapshot_digest": SNAP,
                    "recipe_digest": RECIPE,
                    "sequence_digest": SEQUENCE})
    out.append({"kind": "preview", "data": make_png(9)})
    return out


def committer(tmp_path, backend=None, name="commit", **kw):
    backend = backend or FakeDriveBackend(provider_checksum="sha256")
    return ArchiveCommitter(tmp_path / name, backend, upload=UPLOAD,
                            retry=RETRY, **kw), backend


def begun(tmp_path, **kw):
    c, backend = committer(tmp_path, **kw)
    key = c.begin_commit("build-1", artifacts(),
                         snapshot_digest=SNAP, recipe_digest=RECIPE,
                         toolchain_digest=TOOLCHAIN,
                         required_kinds=("frames", "preview"),
                         coverage=[0, 3])
    return c, backend, key


# == durable journal mechanics ==================================================

def test_journal_records_canonical_chained_and_reload(tmp_path):
    path = tmp_path / "j" / "job_journal.jsonl"
    j = DurableJournal(path)
    j.append("coordinator", "JOB_PLANNED", {"job_key": "a", "job": {}})
    j.append("coordinator", "STATE", {"job_key": "a", "to": "RESERVED"})
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    for line in raw.split(b"\n"):
        if line:
            document = json.loads(line)
            assert canon_bytes(document) == line + b"\n"
            validate_record(document)
    again = DurableJournal(path)
    assert [r["event"] for r in again.records] == ["JOB_PLANNED", "STATE"]
    assert again.head == j.head and not again.fenced
    loaded = load_journal(path)
    assert loaded["tail"] == TAIL_CLEAN


def test_journal_truncated_tail_is_fenced_never_repaired(tmp_path):
    path = tmp_path / "job_journal.jsonl"
    j = DurableJournal(path)
    j.append("coordinator", "JOB_PLANNED", {"job_key": "a", "job": {}})
    size = path.stat().st_size
    with path.open("ab") as stream:             # torn write: no newline
        stream.write(b'{"document_type": "job_jo')
    loaded = load_journal(path)
    assert loaded["tail"] == TAIL_TRUNCATED
    assert [r["event"] for r in loaded["records"]] == ["JOB_PLANNED"]
    j2 = DurableJournal(path)
    assert j2.fenced
    with pytest.raises(FilmError, match="JOURNAL_RECONCILIATION_REQUIRED"):
        j2.append("coordinator", "STATE", {"job_key": "a", "to": "X"})
    assert path.stat().st_size > size      # the torn tail is preserved


def test_journal_corrupt_and_reordered_tails_are_fenced(tmp_path):
    for name, corrupt in (
            ("flip", lambda blob: blob[:-2] + b'"}' + b"\n"),
            ("reorder", lambda blob: b"\n".join(
                reversed(blob.split(b"\n")[:-1])) + b"\n")):
        path = tmp_path / name / "job_journal.jsonl"
        j = DurableJournal(path)
        j.append("coordinator", "JOB_PLANNED", {"job_key": "a",
                                                "job": {}})
        j.append("coordinator", "STATE",
                 {"job_key": "a", "to": "RESERVED", "why": "x"})
        path.write_bytes(corrupt(path.read_bytes()))
        loaded = load_journal(path)
        assert loaded["tail"] == TAIL_CORRUPT
        assert DurableJournal(path).fenced


# == JOB-CRASH: restart rebuilds from the journal ================================

def test_restart_rebuilds_same_job_and_attempt_identity(tmp_path):
    c, worker, plan, key = running(tmp_path)
    job = c.jobs[key]
    identity = {f: job[f] for f in
                ("request_id", "attempt_id", "nonce", "grant_digest")}
    (tmp_path / "state" / "runtime_state.json").unlink()
    c2 = Coordinator(tmp_path / "state")          # journal-only rebuild
    job2 = c2.jobs[key]
    assert job2["state"] == "RUNNING"
    assert {f: job2[f] for f in identity} == identity
    assert len(worker._jobs) == 1                 # never re-submitted
    assert job2["request_id"] in worker._jobs
    worker.complete(job2["request_id"])
    assert c2.collect(worker, key) == "OUTPUT_PENDING_VERIFY"
    assert c2.verify_outputs(key) == "VERIFIED"


def test_submit_intent_records_the_binding_before_side_effects(tmp_path):
    c, worker, plan, key = running(tmp_path)
    job = c.jobs[key]
    intents = [r for r in c.journal.records if r["event"] == "SUBMIT_INTENT"]
    assert len(intents) == 1
    data = intents[0]["data"]
    assert data["request_id"] == job["request_id"]
    assert data["attempt_id"] == job["attempt_id"]
    assert data["nonce_digest"] == job["nonce"]
    assert data["grant_digest"] == job["grant_digest"]
    assert data["snapshot_digest"] == job["snapshot_digest"]
    assert data["output_range"] == job["output_range"]
    assert data["recipe_digest"] == job["operation"]["recipe_digest"]
    assert data["runtime_contract"] == job["operation"]["runtime_contract"]
    assert data["credential_epoch"] == worker.credential_epoch
    # The booked reservation and charge terms are the plan's own values —
    # not a fixed placeholder; plan_sha alone is not the binding.
    assert data["reservations"] == plan["resource_reservations"]
    assert data["quote"]["additional_charges_approved"] is False
    assert data["quote"]["allow_additional_charges"] == \
        plan["execution"]["allow_additional_charges"]
    assert data["quote"]["terms_sha256"] == hashlib.sha256(canon_bytes({
        "execution": plan["execution"], "workspace": plan["workspace"],
        "resource_reservations":
            plan["resource_reservations"]})).hexdigest()
    # The raw nonce never persists; only its digest does.
    text = (tmp_path / "state" / "job_journal.jsonl").read_text()
    assert job["nonce"] in text
    assert c._nonces.get(key) is None or c._nonces[key] not in text


def test_crash_before_submit_side_effect_resolves_not_found(tmp_path):
    """Crash after SUBMIT_INTENT but before the worker ever saw a submit:
    the pending intent rebuilds as UNKNOWN and reconciles under the same
    request id to FAILED_CONFIRMED — never resubmitted."""
    c = coordinator(tmp_path)
    worker = FakeRemoteWorker()
    plan = remote_plan()
    key = c.plan_jobs(plan)[0]
    c.reserve(key)
    real_preflight = worker.preflight

    def boom(packet, now_ms=0):
        raise RuntimeError("crash before submit side effect")

    worker.preflight = boom
    with pytest.raises(RuntimeError):
        c.submit(worker, key, frame_contract=dict(CONTRACT))
    assert c.jobs[key]["state"] == "RESERVED"     # SUBMITTING never wrote
    c2 = Coordinator(tmp_path / "state")
    assert c2.jobs[key]["state"] == "UNKNOWN"     # pending intent fenced
    assert c2.jobs[key]["request_id"] == c.jobs[key]["request_id"]
    assert not worker._jobs                       # nothing was ever sent
    worker.preflight = real_preflight
    assert c2.reconcile(worker, key) == "FAILED_CONFIRMED"
    # Only after a confirmed failure + explicit user continue may a new
    # attempt be minted — a replacement before that is refused.
    c2.resume_failed(worker, key, user_continued=True, plan=plan)
    assert c2.jobs[key]["state"] == "RUNNING"
    assert c2.jobs[key]["request_id"] != c.jobs[key]["request_id"]
    assert len(worker._jobs) == 1


def test_lost_submit_response_restart_reconciles_same_request(tmp_path):
    c = coordinator(tmp_path)
    worker = FakeRemoteWorker(auto_complete=True)
    worker.drop_submit_ack = True
    plan = remote_plan()
    key = c.plan_jobs(plan)[0]
    c.reserve(key)
    assert c.submit(worker, key, frame_contract=dict(CONTRACT)) == "UNKNOWN"
    request_id = c.jobs[key]["request_id"]
    assert len(worker._jobs) == 1                 # worker did accept it
    assert "SUBMIT_LOST" in events(c.journal)
    c2 = Coordinator(tmp_path / "state")
    assert c2.jobs[key]["state"] == "UNKNOWN"
    assert c2.jobs[key]["request_id"] == request_id
    with pytest.raises(FilmError, match="UNKNOWN_FENCED"):
        c2.submit(worker, key, frame_contract=dict(CONTRACT))
    assert len(worker._jobs) == 1                 # still no resubmit
    assert c2.reconcile(worker, key) == "OUTPUT_PENDING_VERIFY"
    assert c2.verify_outputs(key) == "VERIFIED"
    assert c2.jobs[key]["request_id"] == request_id


def test_crash_between_acceptance_and_lost_marker(tmp_path):
    """Worker accepted, the response was lost AND the coordinator died
    before journaling SUBMIT_LOST — the pending SUBMIT_INTENT alone must
    rebuild the job as UNKNOWN under the same identity."""
    c = coordinator(tmp_path)
    worker = FakeRemoteWorker()
    worker.drop_submit_ack = True
    plan = remote_plan()
    key = c.plan_jobs(plan)[0]
    c.reserve(key)
    crash_on_event(c.journal, "SUBMIT_LOST")
    with pytest.raises(RuntimeError):
        c.submit(worker, key, frame_contract=dict(CONTRACT))
    c2 = Coordinator(tmp_path / "state")
    job = c2.jobs[key]
    assert job["state"] == "UNKNOWN"
    assert job["request_id"] in worker._jobs
    assert c2.reconcile(worker, key) == "RUNNING"


def test_crash_between_response_and_verification_reverifies(tmp_path):
    c = coordinator(tmp_path)
    worker = FakeRemoteWorker(auto_complete=True)
    plan = remote_plan()
    key = c.plan_jobs(plan)[0]
    c.reserve(key)
    c.submit(worker, key, frame_contract=dict(CONTRACT))
    assert c.collect(worker, key) == "OUTPUT_PENDING_VERIFY"
    c2 = Coordinator(tmp_path / "state")
    job = c2.jobs[key]
    assert job["state"] == "OUTPUT_PENDING_VERIFY"
    # Receipts rebuilt coverage; only the coordinator's own re-derivation
    # turns them into a verified checkpoint.
    assert not job.get("sealed")
    assert c2.verify_outputs(key) == "VERIFIED"


def test_crash_at_checkpoint_replays_coverage_then_reverifies(tmp_path):
    c = coordinator(tmp_path)
    worker = FakeRemoteWorker(auto_complete=True)
    plan = remote_plan()
    key = c.plan_jobs(plan)[0]
    c.reserve(key)
    c.submit(worker, key, frame_contract=dict(CONTRACT))
    c.collect(worker, key)
    crash_on_event(c.journal, "STATE")            # dies after CHECKPOINT
    with pytest.raises(RuntimeError):
        c.verify_outputs(key)
    c2 = Coordinator(tmp_path / "state")
    assert c2.jobs[key]["state"] == "OUTPUT_PENDING_VERIFY"
    assert c2.verify_outputs(key) == "VERIFIED"
    assert "CHECKPOINT" in events(c2.journal)


def test_sealed_coverage_survives_restart_and_late_receipts(tmp_path):
    c = coordinator(tmp_path)
    worker = FakeRemoteWorker(auto_complete=True)
    plan = remote_plan()
    key = c.plan_jobs(plan)[0]
    drive(c, worker, key, plan, seal=True)
    seal = c.seals[key]
    members = dict(c.jobs[key]["members"])
    c2 = Coordinator(tmp_path / "state")
    assert c2.jobs[key]["state"] == "ARCHIVED"
    assert c2.seals[key] == seal                  # past seal preserved
    # A late completion after the terminal state is an observation only.
    job = c2.jobs[key]
    late = c2.receive_receipt(receipt_for(
        job, "COMPLETE", job["output_range"],
        members=members_for(c2, job, job["output_range"])), worker)
    assert late["kind"] == "COMPLETE" and job["state"] == "ARCHIVED"
    assert job["members"] == members
    assert "LATE_RECEIPT" in events(c2.journal)
    c3 = Coordinator(tmp_path / "state")
    assert c3.jobs[key]["late_receipts"][0]["receipt_id"] == \
        late["receipt_id"]


def test_receipt_dedupe_survives_restart(tmp_path):
    c = coordinator(tmp_path)
    worker = FakeRemoteWorker()
    plan = remote_plan()
    key = c.plan_jobs(plan)[0]
    job = c.jobs[key]
    c.reserve(key)
    c.submit(worker, key, frame_contract=dict(CONTRACT))
    receipt = receipt_for(job, "COMPLETE", job["output_range"],
                          members=members_for(c, job, job["output_range"]))
    c.receive_receipt(receipt, worker)
    c2 = Coordinator(tmp_path / "state")
    with pytest.raises(FilmError, match="duplicate"):
        c2.receive_receipt(dict(receipt), worker)
    with pytest.raises(FilmError, match="Illegal job transition"):
        c2.submit(worker, key, frame_contract=dict(CONTRACT))


def test_unknown_fences_every_side_effect(tmp_path):
    c = coordinator(tmp_path)
    worker = FakeRemoteWorker()
    worker.drop_submit_ack = True
    plan = remote_plan()
    keys = c.plan_jobs(plan)
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    c2 = Coordinator(tmp_path / "state")
    assert c2.jobs[keys[0]]["state"] == "UNKNOWN"
    with pytest.raises(FilmError, match="JOB_IDENTITY_EXISTS"):
        c2.plan_jobs(plan)                        # never re-keyed
    with pytest.raises(FilmError, match="UNKNOWN_FENCED"):
        c2.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    with pytest.raises(FilmError, match="UNKNOWN_FENCED"):
        c2.release_reservation(keys[0])           # spend stays booked
    with pytest.raises(FilmError, match="UNKNOWN job is still fenced"):
        # A new attempt for ANY job is refused while an UNKNOWN exists.
        other = keys[1]
        c2.jobs[other]["state"] = "FAILED_CONFIRMED"
        c2.resume_failed(worker, other, user_continued=True, plan=plan)


def test_truncated_journal_fences_the_coordinator(tmp_path):
    c, worker, plan, key = running(tmp_path)
    journal_path = tmp_path / "state" / "job_journal.jsonl"
    with journal_path.open("ab") as stream:
        stream.write(b'{"document_type": "job_jo')   # torn tail
    c2 = Coordinator(tmp_path / "state")
    assert c2.journal_fenced
    assert c2.journal.status()["tail"] == TAIL_TRUNCATED
    assert c2.jobs[key]["state"] == "RUNNING"     # valid prefix rebuilt
    for call in (lambda: c2.plan_jobs(plan),
                 lambda: c2.reserve(key),
                 lambda: c2.release_reservation(key),
                 lambda: c2.reconcile(worker, key),
                 lambda: c2.collect(worker, key)):
        with pytest.raises(FilmError,
                           match="JOURNAL_RECONCILIATION_REQUIRED"):
            call()
    report = c2.journal_status()
    assert report["fenced"] is True


def test_journal_wins_over_a_stale_runtime_state(tmp_path):
    c, worker, plan, key = running(tmp_path)
    state_file = tmp_path / "state" / "runtime_state.json"
    document = json.loads(state_file.read_text())
    document["jobs"][key]["state"] = "ARCHIVED"   # forged snapshot claim
    document["jobs"][key]["sealed"] = True
    state_file.write_text(json.dumps(document))
    c2 = Coordinator(tmp_path / "state")
    assert c2.jobs[key]["state"] == "RUNNING"     # journal, not snapshot
    assert not c2.jobs[key]["sealed"]


def test_empty_journal_migrates_the_runtime_snapshot_once(tmp_path):
    """A journal file created but never written (crash before the first
    append) does not silently drop a populated runtime_state.json — the
    snapshot loads once and is then migrated into the chain."""
    c, worker, plan, key = running(tmp_path)
    state_dir = tmp_path / "state"
    state_doc = json.loads(
        (state_dir / "runtime_state.json").read_text())
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "runtime_state.json").write_text(json.dumps(state_doc))
    (legacy / "job_journal.jsonl").write_bytes(b"")   # torn creation
    c2 = Coordinator(legacy)
    assert c2.jobs[key]["state"] == "RUNNING"
    assert [r["event"] for r in c2.journal.records] == ["STATE_SNAPSHOT"]
    c3 = Coordinator(legacy)                          # now journal-built
    assert c3.jobs[key]["state"] == "RUNNING"
    assert [r["event"] for r in c3.journal.records] == ["STATE_SNAPSHOT"]


def test_tampered_checkpoint_fences_on_semantic_replay(tmp_path):
    c = coordinator(tmp_path)
    worker = FakeRemoteWorker(auto_complete=True)
    plan = remote_plan()
    key = c.plan_jobs(plan)[0]
    drive(c, worker, key, plan)
    assert c.jobs[key]["state"] == "VERIFIED"

    def tamper(seq, rec):
        if rec["event"] == "CHECKPOINT":
            rec = dict(rec)
            rec["data"] = dict(rec["data"],
                               members=dict(rec["data"]["members"],
                                            **{"0": "0" * 64}))
            return rec

    rechain(tmp_path / "state" / "job_journal.jsonl", tamper)
    c2 = Coordinator(tmp_path / "state")
    assert c2.journal_fenced                       # hash re-check failed
    assert c2.journal.status()["tail"] == TAIL_CLEAN
    assert "replay inconsistency" in \
        c2.journal.status()["fence_reason"]
    with pytest.raises(FilmError,
                       match="JOURNAL_RECONCILIATION_REQUIRED"):
        c2.seal(key)


# == JOB-CANCEL =================================================================

def test_cancel_intent_journaled_and_lost_answer_is_unknown(tmp_path):
    c, worker, plan, key = running(tmp_path)
    worker.drop_cancel_ack = True
    assert c.request_cancel(worker, key) == "UNKNOWN"
    evs = events(c.journal)
    assert evs.index("CANCEL_INTENT") < evs.index("CANCEL_LOST")
    c2 = Coordinator(tmp_path / "state")
    assert c2.jobs[key]["state"] == "UNKNOWN"
    assert c2.reconcile(worker, key) == "CANCEL_CONFIRMED"


def test_crash_between_cancel_intent_and_answer(tmp_path):
    c, worker, plan, key = running(tmp_path)
    worker.drop_cancel_ack = True
    crash_on_event(c.journal, "CANCEL_LOST")
    with pytest.raises(RuntimeError):
        c.request_cancel(worker, key)
    c2 = Coordinator(tmp_path / "state")
    assert c2.jobs[key]["state"] == "CANCEL_REQUESTED"
    with pytest.raises(FilmError, match="UNKNOWN_FENCED"):
        # While the cancel answer is unresolved a new submit is refused.
        c2.submit(worker, key, frame_contract=dict(CONTRACT))
    assert c2.reconcile(worker, key) == "CANCEL_CONFIRMED"


def test_confirmed_cancel_preserves_completion_first(tmp_path):
    c = coordinator(tmp_path)
    worker = FakeRemoteWorker(auto_complete=True)
    plan = remote_plan()
    key = c.plan_jobs(plan)[0]
    c.reserve(key)
    c.submit(worker, key, frame_contract=dict(CONTRACT))
    c.collect(worker, key)                        # completion confirmed
    assert c.request_cancel(worker, key) == "OUTPUT_PENDING_VERIFY"
    c2 = Coordinator(tmp_path / "state")
    assert c2.jobs[key]["state"] == "OUTPUT_PENDING_VERIFY"
    assert c2.verify_outputs(key) == "VERIFIED"


# == UPLOAD-RESUME: the bounded journaled tracker ================================

def test_upload_journals_intent_session_offsets_and_no_secret_uri(tmp_path):
    backend = FakeDriveBackend(provider_checksum="sha256")
    journal = DurableJournal(tmp_path / "up" / "job_journal.jsonl")
    tracker = UploadTracker(journal, backend, upload=UPLOAD, retry=RETRY)
    data = make_png(3) * 3
    result = tracker.put("obj-1", data)
    assert backend.object_info("obj-1")["byte_length"] == len(data)
    assert result["object_id"] == "obj-1"
    evs = events(journal)
    assert evs[0] == "UPLOAD_INTENT"              # intent precedes session
    assert evs.index("UPLOAD_SESSION") < evs.index("UPLOAD_OFFSET")
    assert evs[-1] == "UPLOAD_COMPLETE"
    text = journal.path.read_text()
    assert "sess-" in text                        # opaque reference only
    session_uris = [s for s in backend._sessions] or []
    for uri in session_uris:
        assert uri not in text
    import re
    assert not re.search(r"up-[0-9a-f]{12}", text)


def test_upload_response_loss_requeries_confirmed_offset(tmp_path):
    backend = FakeDriveBackend(provider_checksum="sha256")
    data = make_png(5) * 2                        # ~6 chunks of 128
    # The second upload_chunk request (requests index 2) accepts only a
    # partial write before the transfer drops — the confirmed offset must
    # come from upload_status, never from the sent length.
    backend.upload_partial = {2: 40}
    journal = DurableJournal(tmp_path / "up.jsonl")
    tracker = UploadTracker(journal, backend, upload=UPLOAD, retry=RETRY)
    tracker.put("obj-1", data)
    assert backend._objects["obj-1"] == data
    chunk_offsets = [r["offset"] for r in backend.requests
                     if r["op"] == "upload_chunk"]
    offsets = [r["data"]["offset"] for r in journal.records
               if r["event"] == "UPLOAD_OFFSET"]
    assert 40 in offsets                          # server-confirmed, real
    # Every chunk is sent from the last server-confirmed offset only: the
    # resend after the drop starts at 40, and no offset ever goes back.
    assert chunk_offsets[0] == 0 and 40 in chunk_offsets
    assert all(a <= b for a, b in zip(chunk_offsets, chunk_offsets[1:]))
    # The live session entry reports the journaled server offset too.
    assert tracker.status()["up-obj-1"]["sessions"][0]["confirmed"] == \
        len(data)


def test_upload_stall_exhausts_the_retry_allowance(tmp_path):
    backend = FakeDriveBackend(provider_checksum="sha256")
    backend.drops = {"upload_chunk": 50}          # progress never confirms
    journal = DurableJournal(tmp_path / "up.jsonl")
    retry = dict(RETRY, max_retries=2)
    tracker = UploadTracker(journal, backend, upload=UPLOAD, retry=retry)
    with pytest.raises(FilmError, match="TRANSPORT_RETRY_EXHAUSTED"):
        tracker.put("obj-1", make_png(4))
    assert "UPLOAD_COMPLETE" not in events(journal)
    assert tracker.pending()                      # intent survives restart


def test_upload_transferred_byte_cap_exhausts(tmp_path):
    backend = FakeDriveBackend(provider_checksum="sha256")
    journal = DurableJournal(tmp_path / "up.jsonl")
    retry = dict(RETRY, max_transferred_bytes=100)
    tracker = UploadTracker(journal, backend, upload=UPLOAD, retry=retry)
    with pytest.raises(FilmError, match="TRANSPORT_RETRY_EXHAUSTED"):
        tracker.put("obj-1", make_png(4) * 3)


def test_upload_request_cap_covers_the_whole_drive(tmp_path):
    """max_requests applies across session create, status queries and
    chunk sends — not reset per call."""
    backend = FakeDriveBackend(provider_checksum="sha256")
    journal = DurableJournal(tmp_path / "up.jsonl")
    retry = dict(RETRY, max_requests=3)
    tracker = UploadTracker(journal, backend, upload=UPLOAD, retry=retry)
    with pytest.raises(FilmError, match="TRANSPORT_RETRY_EXHAUSTED"):
        tracker.put("obj-1", make_png(4) * 3)
    # create + status + first chunk: the fourth request never leaves.
    assert len(backend.requests) == 3
    assert "UPLOAD_COMPLETE" not in events(journal)


def test_upload_elapsed_cap_covers_the_whole_drive(tmp_path, monkeypatch):
    """max_elapsed_ms applies across the whole upload, not per read."""
    backend = FakeDriveBackend(provider_checksum="sha256")
    journal = DurableJournal(tmp_path / "up.jsonl")
    retry = dict(RETRY, max_elapsed_ms=2500)
    ticks = iter(range(0, 100_000, 1000))       # +1s per monotonic call
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks) / 1000.0)
    tracker = UploadTracker(journal, backend, upload=UPLOAD, retry=retry)
    with pytest.raises(FilmError, match="TRANSPORT_RETRY_EXHAUSTED"):
        tracker.put("obj-1", make_png(4) * 3)
    # create + status land inside the allowance; the third request is
    # refused before it is sent.
    assert len(backend.requests) == 2
    assert "UPLOAD_COMPLETE" not in events(journal)


def test_lost_create_session_is_session_unknown_until_reconcile(tmp_path):
    """Evolution 4.2.1: one dropped create_upload_session answer never
    mints a second session on the HTTP retry allowance — the intent is
    journaled session-unknown and only an explicit reconcile moves it."""
    backend = FakeDriveBackend(provider_checksum="sha256")
    backend.drops = {"create_upload_session": 1}
    journal = DurableJournal(tmp_path / "up.jsonl")
    tracker = UploadTracker(journal, backend, upload=UPLOAD, retry=RETRY)
    data = make_png(2)
    with pytest.raises(FilmError, match="UPLOAD_SESSION_UNKNOWN"):
        tracker.put("obj-1", data)
    creates = [r for r in backend.requests
               if r["op"] == "create_upload_session"]
    assert len(creates) == 1                    # never retried
    evs = events(journal)
    assert evs.count("UPLOAD_INTENT") == 1      # one intent only
    assert "UPLOAD_SESSION" not in evs
    assert "UPLOAD_SESSION_LOST" in evs
    # Same process and across a restart the intent stays fenced.
    with pytest.raises(FilmError, match="UPLOAD_SESSION_UNKNOWN"):
        tracker.put("obj-1", data)
    t2 = UploadTracker(DurableJournal(tmp_path / "up.jsonl"), backend,
                       upload=UPLOAD, retry=RETRY)
    assert t2.status()["up-obj-1"]["unknown"] is True
    with pytest.raises(FilmError, match="UPLOAD_SESSION_UNKNOWN"):
        t2.put("obj-1", data)
    assert len([r for r in backend.requests
                if r["op"] == "create_upload_session"]) == 1
    # Explicit reconcile: nothing landed -> the phantom is fenced and the
    # SAME intent (not a new one) continues on a journaled new session.
    assert t2.reconcile("obj-1") == {
        "intent_id": "up-obj-1", "object_id": "obj-1",
        "complete": False, "reconciled": "not_stored"}
    assert t2.put("obj-1", data)["object_id"] == "obj-1"
    assert backend._objects["obj-1"] == data
    evs = events(t2.journal)
    assert evs.count("UPLOAD_INTENT") == 1
    assert evs.count("UPLOAD_SESSION") == 1


def test_lost_put_object_reconciles_stored_bytes(tmp_path):
    """A dropped whole-object put leaves the intent session-unknown; the
    reconcile's object/hash check finds the landed bytes and completes
    the same intent — never a second put."""
    backend = FakeDriveBackend(provider_checksum="sha256")
    journal = DurableJournal(tmp_path / "up.jsonl")
    tracker = UploadTracker(journal, backend,
                            upload={"resumable": False}, retry=RETRY)
    data = make_png(3)
    real_put = backend.put_object

    def lose(object_id, body):
        real_put(object_id, body)               # landed; the answer is
        raise ConnectionDropped("put answer lost")   # lost anyway

    backend.put_object = lose
    with pytest.raises(FilmError, match="UPLOAD_SESSION_UNKNOWN"):
        tracker.put("obj-9", data)
    puts = [r for r in backend.requests if r["op"] == "put_object"]
    assert len(puts) == 1
    assert tracker.reconcile("obj-9")["complete"] is True
    assert tracker.put("obj-9", data)["bytes_confirmed"] == len(data)
    assert events(journal)[-1] == "UPLOAD_COMPLETE"


class ExpireOnceBackend(FakeDriveBackend):
    """The provider forgets the resumable session after its first chunk."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._expire_armed = True
        self.expired_sessions = []

    def upload_chunk(self, session, offset, data):
        result = super().upload_chunk(session, offset, data)
        if self._expire_armed:
            self._expire_armed = False
            self.expire_session(session)
            self.expired_sessions.append(session)
        return result


def test_session_expiry_fences_and_opens_an_explicit_new_session(tmp_path):
    backend = ExpireOnceBackend(provider_checksum="sha256")
    journal = DurableJournal(tmp_path / "up.jsonl")
    tracker = UploadTracker(journal, backend, upload=UPLOAD, retry=RETRY)
    data = make_png(6) * 2
    tracker.put("obj-1", data)
    assert backend._objects["obj-1"] == data
    evs = events(journal)
    assert evs.count("UPLOAD_SESSION") == 2       # never silently reused
    assert "UPLOAD_SESSION_FENCED" in evs
    fenced = [r for r in journal.records
              if r["event"] == "UPLOAD_SESSION_FENCED"][0]
    assert "gone" in fenced["data"]["reason"]
    sessions = tracker.status()["up-obj-1"]["sessions"]
    assert sessions[0]["fenced"] and not sessions[1]["fenced"]
    # The expired session's partial bytes stay server-side, journaled as
    # fenced — nothing is deleted or silently overwritten.
    assert backend.expired_sessions


def test_restart_fences_the_session_ref_and_resumes_the_intent(tmp_path):
    path = tmp_path / "up" / "job_journal.jsonl"
    backend = FakeDriveBackend(provider_checksum="sha256")
    backend.drops = {"upload_chunk": 50}
    j1 = DurableJournal(path)
    tracker1 = UploadTracker(j1, backend, upload=UPLOAD,
                             retry=dict(RETRY, max_retries=1))
    data = make_png(7) * 2
    with pytest.raises(FilmError, match="TRANSPORT_RETRY_EXHAUSTED"):
        tracker1.put("obj-1", data)
    old_sessions = list(backend._sessions)
    assert old_sessions                           # partial bytes survive
    backend.drops = {}
    # Restart: a fresh tracker + fresh in-memory session store. The old
    # session URI is gone — its reference is fenced and a fresh session
    # with a fresh intent starts, bounded by the same retry allowance.
    j2 = DurableJournal(path)
    tracker2 = UploadTracker(j2, backend, upload=UPLOAD, retry=RETRY)
    assert tracker2.pending() == [{"intent_id": "up-obj-1",
                                   "object_id": "obj-1",
                                   "byte_length": len(data),
                                   "confirmed": 0}]
    assert tracker2.put("obj-1", data)["object_id"] == "obj-1"
    assert backend._objects["obj-1"] == data
    evs = events(j2)
    assert evs.count("UPLOAD_INTENT") == 1        # same intent, not new
    assert evs.count("UPLOAD_SESSION") == 2
    assert "UPLOAD_SESSION_FENCED" in evs
    sessions = tracker2.status()["up-obj-1"]["sessions"]
    assert len({s["ref"] for s in sessions}) == 2  # a genuinely new session
    assert sessions[0]["fenced"] and not sessions[1]["fenced"]


def test_lost_complete_answer_rechecks_then_lands_once(tmp_path):
    backend = FakeDriveBackend(provider_checksum="sha256")
    backend.drops = {"complete_upload": 1}        # answer lost, kept state
    journal = DurableJournal(tmp_path / "up.jsonl")
    tracker = UploadTracker(journal, backend, upload=UPLOAD, retry=RETRY)
    data = make_png(8)
    tracker.put("obj-1", data)
    assert backend._objects["obj-1"] == data
    completes = [r for r in backend.requests
                 if r["op"] == "complete_upload"]
    assert len(completes) == 2                    # status-confirmed resend
    assert events(journal)[-1] == "UPLOAD_COMPLETE"


# == SEAL-CRASH: archive commit / seal reconciliation ============================

def test_default_required_kinds_is_the_full_completion_set(tmp_path):
    """§9.1: the completion manifest is never published while any of the
    required inventory — source, delivery PNG sequence, clean recipe,
    frame_map, original audio, cue/font, clean and subbed MP4 — is
    missing. A lone preview refuses by default."""
    from engine.archive_commit import COMPLETION_REQUIRED_KINDS
    c, backend = committer(tmp_path)
    with pytest.raises(FilmError, match="missing required artifacts"):
        c.begin_commit("build-1",
                       [{"kind": "preview", "data": make_png(1)}],
                       snapshot_digest=SNAP, recipe_digest=RECIPE,
                       toolchain_digest=TOOLCHAIN)
    full = [{"kind": kind, "data": make_png(i + 2)}
            for i, kind in enumerate(COMPLETION_REQUIRED_KINDS)]
    key = c.begin_commit("build-1", full,
                         snapshot_digest=SNAP, recipe_digest=RECIPE,
                         toolchain_digest=TOOLCHAIN)
    assert c.run(key)["state"] == "SEALED"


def test_commit_seals_objects_then_manifest(tmp_path):
    c, backend, key = begun(tmp_path)
    result = c.run(key)
    assert result["state"] == "SEALED"
    stored = backend._objects[result["manifest_object_id"]]
    manifest = validate_archive_seal(json.loads(stored))
    assert manifest["commit_key"] == key
    assert manifest["build_id"] == "build-1"
    assert manifest["journal_ref"]["records"] > 0
    levels = {o["level"] for a in manifest["artifacts"]
              for o in a["objects"]}
    assert levels == {"UPLOAD_HASH_MATCHED"}
    assert manifest["qualification"]["qualification_state"] == \
        "UNQUALIFIED"
    evs = events(c.journal)
    assert evs[0] == "COMMIT_INTENT"
    assert evs.index("OBJECTS_VERIFIED") < evs.index("MANIFEST_INTENT")
    assert evs.index("MANIFEST_INTENT") < evs.index("SEALED")


def test_commit_restart_requires_the_bytes_then_seals(tmp_path):
    c, backend, key = begun(tmp_path)
    c2, _ = committer(tmp_path, backend, name="commit")
    assert c2.commits[key]["state"] == "OBJECTS_PENDING"
    with pytest.raises(FilmError, match="COMMIT_BYTES_REQUIRED"):
        c2.run(key)                               # bytes are not journaled
    with pytest.raises(FilmError, match="COMMIT_IDENTITY_MISMATCH"):
        c2.run(key, [{"kind": "preview", "data": b"forged"}])
    assert c2.run(key, artifacts())["state"] == "SEALED"


def test_crash_between_artifacts_reuses_only_verified(tmp_path):
    import engine.archive_commit as commit_mod
    c, backend, key = begun(tmp_path)
    real_verify = commit_mod.verify_object
    calls = []

    def boom(backend_, *a, **kw):
        calls.append(a[0])
        if len(calls) == 2:
            raise RuntimeError("crash after first object verified")
        return real_verify(backend_, *a, **kw)

    commit_mod.verify_object = boom
    try:
        with pytest.raises(RuntimeError):
            c.run(key)
    finally:
        commit_mod.verify_object = real_verify
    c2, _ = committer(tmp_path, backend, name="commit")
    commit = c2.commits[key]
    verified_before = set(commit["verified"])
    assert len(verified_before) == 1              # verified checkpoint kept
    seen = []

    def spy(backend_, *a, **kw):
        seen.append(a[0])
        return real_verify(backend_, *a, **kw)

    commit_mod.verify_object = spy
    try:
        assert c2.run(key, artifacts())["state"] == "SEALED"
    finally:
        commit_mod.verify_object = real_verify
    # Every pin is re-verified on the resumed run — the journaled
    # checkpoint is reused only after the object is proven to still
    # exist and hash, never on the record alone.
    all_pins = {o["object_id"] for a in commit["artifacts"]
                for o in a["objects"]}
    assert set(seen) == all_pins and verified_before <= set(seen)


def test_deleted_verified_object_refuses_the_checkpoint(tmp_path):
    """A previously OBJECT_VERIFIED object that was externally deleted or
    truncated is refused on re-verification — never copied into a sealed
    manifest from the journal record alone."""
    import engine.archive_commit as commit_mod
    c, backend, key = begun(tmp_path)
    real_verify = commit_mod.verify_object
    calls = []

    def boom(backend_, *a, **kw):
        calls.append(a[0])
        if len(calls) == 2:
            raise RuntimeError("crash after first object verified")
        return real_verify(backend_, *a, **kw)

    commit_mod.verify_object = boom
    try:
        with pytest.raises(RuntimeError):
            c.run(key)
    finally:
        commit_mod.verify_object = real_verify
    verified_oid = next(iter(c.commits[key]["verified"]))
    del backend._objects[verified_oid]          # externally deleted
    c2, _ = committer(tmp_path, backend, name="commit")
    with pytest.raises(FilmError):
        c2.run(key, artifacts())
    assert c2.commits[key]["state"] == "OBJECTS_PENDING"
    assert not [o for o in backend._objects if o.startswith("manifest-")]


def test_crash_before_manifest_publish_republishes_same_intent(tmp_path):
    c, backend, key = begun(tmp_path)

    def boom(object_id, data):
        if object_id.startswith("manifest-"):
            raise RuntimeError("crash at manifest publish")
        return FakeDriveBackend.put_object(backend, object_id, data)

    backend.put_object = boom
    with pytest.raises(RuntimeError):
        c.run(key)
    assert c.commits[key]["state"] == "MANIFEST_INTENT"
    assert not [o for o in backend._objects if o.startswith("manifest-")]
    c2, _ = committer(tmp_path, backend, name="commit")
    delattr(backend, "put_object")                # wrapper is instance attr
    assert c2.reconcile(key)["state"] == "SEALED"
    manifests = [o for o in backend._objects if o.startswith("manifest-")]
    assert len(manifests) == 1                    # same bytes, same id


def test_lost_publish_answer_is_seal_unknown_then_sealed(tmp_path):
    backend = FakeDriveBackend(provider_checksum="sha256")
    c, _ = committer(tmp_path, backend)
    key = c.begin_commit("build-1", artifacts(),
                         snapshot_digest=SNAP, recipe_digest=RECIPE,
                         toolchain_digest=TOOLCHAIN,
                         required_kinds=("frames", "preview"))
    real_put = backend.put_object

    def lose_answer(object_id, data):
        if object_id.startswith("manifest-"):
            real_put(object_id, data)             # applied; the answer is
            raise ConnectionDropped("publish answer lost")   # always lost
        return real_put(object_id, data)

    backend.put_object = lose_answer
    result = c.run(key)
    assert result["state"] == "SEAL_UNKNOWN"
    assert "SEAL_UNKNOWN" in events(c.journal)
    delattr(backend, "put_object")
    c2, _ = committer(tmp_path, backend, name="commit")
    assert c2.commits[key]["state"] == "SEAL_UNKNOWN"
    before = len([r for r in backend.requests if r["op"] == "put_object"])
    assert c2.reconcile(key)["state"] == "SEALED"
    # The manifest was found identical — it is not re-published.
    assert len([r for r in backend.requests if r["op"] == "put_object"]) \
        == before
    manifests = [o for o in backend._objects if o.startswith("manifest-")]
    assert len(manifests) == 1


def test_duplicate_manifests_for_one_build_are_refused(tmp_path):
    c, backend, key = begun(tmp_path)

    def boom(object_id, data):
        if object_id.startswith("manifest-"):
            raise RuntimeError("crash before publish")
        return FakeDriveBackend.put_object(backend, object_id, data)

    backend.put_object = boom
    with pytest.raises(RuntimeError):
        c.run(key)
    delattr(backend, "put_object")
    # A different commit under the same build id publishes elsewhere.
    c2, _ = committer(tmp_path, backend, name="commit-other")
    key_b = c2.begin_commit(
        "build-1", [{"kind": "preview", "data": make_png(11)}],
        snapshot_digest=SNAP, recipe_digest=RECIPE,
        toolchain_digest=TOOLCHAIN, required_kinds=("preview",))
    assert c2.run(key_b)["state"] == "SEALED"
    # Reconciling the first commit sees a foreign manifest for its build:
    # refused, preserved, never overwritten or deleted.
    c3, _ = committer(tmp_path, backend, name="commit")
    outcome = c3.reconcile(key)
    assert outcome["state"] == "SEAL_UNKNOWN"
    manifests = sorted(o for o in backend._objects
                       if o.startswith("manifest-"))
    assert len(manifests) == 1
    assert manifests[0] == c2.commits[key_b]["manifest_object_id"]
    # Inside one committer the same build with different bytes is refused.
    with pytest.raises(FilmError, match="BUILD_IDENTITY_CONFLICT"):
        c3.begin_commit("build-1", [{"kind": "x", "data": b"y"}],
                        snapshot_digest=SNAP, recipe_digest=RECIPE,
                        toolchain_digest=TOOLCHAIN,
                        required_kinds=("x",))


def test_foreign_manifest_bytes_never_seal_the_intent_id(tmp_path):
    """A manifest-{build}-* object holding byte-identical content under a
    DIFFERENT object id is not the intent publication — it stays
    SEAL_UNKNOWN and both objects are preserved."""
    c, backend, key = begun(tmp_path)

    def boom(object_id, data):
        if object_id.startswith("manifest-"):
            raise RuntimeError("crash before publish")
        return FakeDriveBackend.put_object(backend, object_id, data)

    backend.put_object = boom
    with pytest.raises(RuntimeError):
        c.run(key)
    delattr(backend, "put_object")
    target = c.commits[key]["manifest_object_id"]
    manifest_raw = canon_bytes(c.commits[key]["manifest"])
    foreign_id = "manifest-build-1-foreign-copy"
    backend.put_object(foreign_id, manifest_raw)   # same bytes, wrong id
    outcome = c.reconcile(key)
    assert outcome["state"] == "SEAL_UNKNOWN"
    assert c.commits[key]["state"] == "SEAL_UNKNOWN"
    assert backend._objects[foreign_id] == manifest_raw    # preserved
    assert target not in backend._objects                  # never sealed


def test_lost_manifest_put_never_republishes_on_the_allowance(tmp_path):
    """One dropped manifest put_object leaves SEAL_UNKNOWN; the HTTP
    allowance never mints a second put — reconcile alone resolves it."""
    c, backend, key = begun(tmp_path)
    real_put = backend.put_object
    calls = []

    def lose(object_id, data):
        if object_id.startswith("manifest-"):
            calls.append(object_id)
            raise ConnectionDropped("publish answer lost")
        return real_put(object_id, data)

    backend.put_object = lose
    result = c.run(key)
    assert result["state"] == "SEAL_UNKNOWN"
    assert calls == [c.commits[key]["manifest_object_id"]]   # exactly one
    assert "SEAL_UNKNOWN" in events(c.journal)
    delattr(backend, "put_object")
    c2, _ = committer(tmp_path, backend, name="commit")
    outcome = c2.reconcile(key)
    assert outcome["state"] == "SEALED"
    puts = [r for r in backend.requests
            if r["op"] == "put_object"
            and r["object_id"].startswith("manifest-")]
    assert len(puts) == 1                       # the single reconciled put


def test_full_readback_floor_reads_back_despite_provider_checksum(tmp_path):
    """A readback-required profile is never cleared by a provider
    SHA-256: the whole object is re-read and hashed even though the
    endpoint reports a matching checksum."""
    c, backend = committer(tmp_path)
    key = c.begin_commit(
        "build-1", [{"kind": "preview", "data": make_png(12)}],
        snapshot_digest=SNAP, recipe_digest=RECIPE,
        toolchain_digest=TOOLCHAIN, required_kinds=("preview",),
        verification_profile={"min_level": "FULL_READBACK"})
    assert c.run(key)["state"] == "SEALED"
    reads = [r for r in backend.requests
             if r["op"] == "get_object"
             and not r["object_id"].startswith("manifest-")]
    assert reads                                     # the readback ran
    manifest = [json.loads(backend._objects[o])
                for o in backend._objects
                if o.startswith("manifest-")][0]
    assert manifest["artifacts"][0]["objects"][0]["level"] == \
        "FULL_READBACK"
    # A readback that cannot run refuses — the checksum never substitutes.
    c2, backend2 = committer(tmp_path, name="commit-cap")
    key2 = c2.begin_commit(
        "build-1", [{"kind": "preview", "data": make_png(12)}],
        snapshot_digest=SNAP, recipe_digest=RECIPE,
        toolchain_digest=TOOLCHAIN, required_kinds=("preview",),
        verification_profile={"min_level": "FULL_READBACK",
                              "readback_cap_bytes": 8})
    with pytest.raises(FilmError, match="READBACK_CAP_EXCEEDED"):
        c2.run(key2)
    assert c2.commits[key2]["state"] == "OBJECTS_PENDING"
    assert not [o for o in backend2._objects if o.startswith("manifest-")]


def test_full_readback_satisfies_when_no_provider_checksum(tmp_path):
    backend = FakeDriveBackend(provider_checksum=None)
    c, _ = committer(tmp_path, backend)
    key = c.begin_commit("build-1", [{"kind": "preview",
                                    "data": make_png(13)}],
                         snapshot_digest=SNAP, recipe_digest=RECIPE,
                         toolchain_digest=TOOLCHAIN,
                         required_kinds=("preview",))
    assert c.run(key)["state"] == "SEALED"
    manifest = [json.loads(backend._objects[o])
                for o in backend._objects if o.startswith("manifest-")][0]
    assert manifest["artifacts"][0]["objects"][0]["level"] == \
        "FULL_READBACK"


def test_orphan_sweep_deletes_only_explicit_approvals(tmp_path):
    c, backend, key = begun(tmp_path)
    c.run(key)
    backend.put_object("stray-1", b"orphan bytes")
    commit = c.commits[key]
    manifest_oid = commit["manifest_object_id"]
    referenced = commit["artifacts"][0]["objects"][0]["object_id"]
    with pytest.raises(FilmError, match="ORPHAN_REFUSED"):
        c.sweep_orphans([manifest_oid])           # manifests never orphans
    with pytest.raises(FilmError, match="ORPHAN_REFUSED"):
        c.sweep_orphans([referenced])
    assert c.sweep_orphans([])["deleted"] == []   # nothing without approval
    journaled_first = []
    real_delete = backend.delete_object

    def delete(object_id):
        journaled_first.append("ORPHAN_DELETED" in events(c.journal))
        return real_delete(object_id)

    backend.delete_object = delete
    assert c.sweep_orphans(["stray-1"])["deleted"] == ["stray-1"]
    assert journaled_first == [True]          # durable before the delete
    assert "stray-1" not in backend._objects
    assert "ORPHAN_DELETED" in events(c.journal)
    # The seal and every referenced object are preserved.
    assert c.commits[key]["state"] == "SEALED"
    assert backend._objects[manifest_oid]
    assert backend._objects[referenced]


def test_journal_status_reports_unresolved_jobs(tmp_path):
    c = coordinator(tmp_path)
    worker = FakeRemoteWorker()
    worker.drop_submit_ack = True
    plan = remote_plan()
    key = c.plan_jobs(plan)[0]
    c.reserve(key)
    c.submit(worker, key, frame_contract=dict(CONTRACT))
    c2 = Coordinator(tmp_path / "state")
    report = c2.journal_status()
    assert report["jobs"][0]["state"] == "UNKNOWN"
    assert report["unresolved"] == [
        {"job_key": key, "state": "UNKNOWN",
         "request_id": c2.jobs[key]["request_id"],
         "needs": "reconcile"}]


# == CLI ======================================================================

def test_journal_status_and_reconcile_cli(tmp_path):
    c = coordinator(tmp_path)
    worker = FakeRemoteWorker()
    worker.drop_submit_ack = True
    plan = remote_plan()
    key = c.plan_jobs(plan)[0]
    c.reserve(key)
    c.submit(worker, key, frame_contract=dict(CONTRACT))
    state_dir = tmp_path / "state"
    status = journal_status(state_dir)
    assert status["journal"]["tail"] == TAIL_CLEAN
    assert status["qualification_state"] == "UNQUALIFIED"
    assert status["unresolved"][0]["job_key"] == key
    report = journal_reconcile(state_dir)
    assert report["state_file_written"] is True
    assert report["unresolved"][0]["needs"] == "reconcile"


def test_archive_seal_reconcile_cli(tmp_path):
    root = tmp_path / "archive-root"
    commit_dir = tmp_path / "commit"
    backend = LocalArchiveBackend(root)
    c = ArchiveCommitter(commit_dir, backend, upload=UPLOAD, retry=RETRY)
    key = c.begin_commit("build-1", [{"kind": "preview",
                                    "data": make_png(14)}],
                         snapshot_digest=SNAP, recipe_digest=RECIPE,
                         toolchain_digest=TOOLCHAIN,
                         required_kinds=("preview",))
    crash_on_event(c.journal, "SEALED")           # crash after publication
    with pytest.raises(RuntimeError):
        c.run(key)
    report = archive_seal_reconcile(commit_dir, root)
    assert report["qualification_state"] == "UNQUALIFIED"
    assert report["commits"][0]["state"] == "SEALED"
    manifest = [o for o in backend.list_objects()
                if o.startswith("manifest-")]
    assert len(manifest) == 1
