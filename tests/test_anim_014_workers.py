"""ANIM-014 WorkerProtocol 1: job identity, state machine, receipts,
UNKNOWN fences, cancellation races, reconciliation, seal and the relay
auth boundary (schema §13, exec/storage §5.3-5.4, evolution §2.1.1).

All remote execution is the explicit fake — no network, no credentials.
Fake receipts are labelled FAKE_REMOTE and the route stays UNQUALIFIED.
"""
import hashlib
import threading

import pytest

from anim_014_kit import (CONTRACT, FRAMES, INPUT_MEMBERS, OBJECT, RECIPE,
                          RECIPE_B, SNAPSHOT, coordinator, drive,
                          local_plan, make_plan, members_for, operation,
                          receipt_for, remote_plan)

from engine.core import FilmError
from engine.execution_workers import (Coordinator, TRANSITIONS,
                                      check_grant, check_packet,
                                      expected_frame_digest, issue_grant,
                                      make_receipt)
from engine.execution_workers.fake_remote import FakeRemoteWorker
from engine.execution_workers.local import LocalWorker


def remote(tmp_path, **kwargs):
    c = coordinator(tmp_path, **kwargs)
    worker = FakeRemoteWorker()
    plan = remote_plan()
    keys = c.plan_jobs(plan)
    return c, worker, plan, keys


# -- happy path: same frame contract on local and remote ---------------------

def test_local_job_runs_to_archived(tmp_path):
    c = coordinator(tmp_path)
    worker = LocalWorker()
    plan = local_plan()
    keys = c.plan_jobs(plan)
    for key in keys:
        drive(c, worker, key, plan, seal=True)
    assert all(c.jobs[k]["state"] == "ARCHIVED" for k in keys)


def test_local_and_fake_remote_produce_identical_frame_digests(tmp_path):
    plan = remote_plan()
    c_local = Coordinator(tmp_path / "local")
    c_remote = Coordinator(tmp_path / "remote")
    local, remote = LocalWorker("remote-1"), FakeRemoteWorker()
    keys_l = c_local.plan_jobs(plan)
    keys_r = c_remote.plan_jobs(plan)
    # same plan -> same job keys, and the same frame digests on both
    # workers — one frame contract across local and remote execution.
    assert keys_l == keys_r
    for kl, kr in zip(keys_l, keys_r):
        remote.auto_complete = True
        drive(c_remote, remote, kr, plan)
        drive(c_local, local, kl, plan)
        assert c_local.jobs[kl]["members"] == c_remote.jobs[kr]["members"]
        for index in range(*c_local.jobs[kl]["output_range"]):
            assert c_local.jobs[kl]["members"][str(index)] == \
                expected_frame_digest(index, CONTRACT, SNAPSHOT, RECIPE)


def test_fake_receipts_are_labelled_and_route_unqualified(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    worker.auto_complete = True
    job = drive(c, worker, keys[0], plan)
    complete = [r for r in job["receipts"] if r["kind"] == "COMPLETE"]
    assert complete[0]["evidence"]["class"] == "FAKE_REMOTE"
    assert complete[0]["evidence"]["qualification_state"] == "UNQUALIFIED"
    assert c.facet_report() == {
        "node_state": "IN_PROGRESS",
        "qualification_state": "UNQUALIFIED",
        "acceptance_state": "PENDING",
        "release_state": "NOT_AUTHORIZED"}


def test_worker_complete_never_promoted_without_verification(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    worker.auto_complete = True
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    c.collect(worker, keys[0])
    assert c.jobs[keys[0]]["state"] == "OUTPUT_PENDING_VERIFY"
    with pytest.raises(FilmError, match="VERIFIED"):
        c.seal(keys[0])
    assert c.verify_outputs(keys[0]) == "VERIFIED"


def test_corrupt_worker_output_fails_independent_verification(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    job = c.jobs[keys[0]]
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    bad = [{"frame_index": i, "sha256": RECIPE}
           for i in range(*job["output_range"])]
    c.receive_receipt(receipt_for(job, "COMPLETE", job["output_range"],
                                  members=bad), worker)
    assert c.jobs[keys[0]]["state"] == "OUTPUT_PENDING_VERIFY"
    assert c.verify_outputs(keys[0]) == "FAILED_CONFIRMED"


# -- receipts: order, duplicates, staleness ------------------------------------

def test_out_of_order_receipts_assemble_coverage(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    job = c.jobs[keys[0]]                        # output range [0, 4)
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    for rng in ([2, 3], [3, 4], [0, 2]):         # deliberately out of order
        c.receive_receipt(
            receipt_for(job, "COMPLETE", rng,
                        members=members_for(c, job, rng)), worker)
    assert c.coverage(keys[0])["complete"]
    assert c.jobs[keys[0]]["state"] == "OUTPUT_PENDING_VERIFY"
    assert c.verify_outputs(keys[0]) == "VERIFIED"


def test_duplicate_and_overlapping_receipts_rejected(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    job = c.jobs[keys[0]]
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    first = receipt_for(job, "COMPLETE", [0, 4],
                        members=members_for(c, job, [0, 4]))
    c.receive_receipt(first, worker)
    with pytest.raises(FilmError, match="duplicate"):
        c.receive_receipt(dict(first), worker)
    overlap = receipt_for(job, "COMPLETE", [2, 4],
                          members=members_for(c, job, [2, 4]))
    with pytest.raises(FilmError, match="overlapping"):
        c.receive_receipt(overlap, worker)


def test_missing_range_is_reported_and_not_complete(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    job = c.jobs[keys[0]]
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    c.receive_receipt(receipt_for(job, "COMPLETE", [0, 1],
                                  members=members_for(c, job, [0, 1])),
                      worker)
    c.receive_receipt(receipt_for(job, "COMPLETE", [3, 4],
                                  members=members_for(c, job, [3, 4])),
                      worker)
    cov = c.coverage(keys[0])
    assert cov["missing"] == [[1, 3]] and not cov["complete"]
    assert c.jobs[keys[0]]["state"] == "RUNNING"
    with pytest.raises(FilmError, match="requires OUTPUT_PENDING_VERIFY"):
        c.verify_outputs(keys[0])


def test_replayed_receipt_dedupes_without_double_coverage(tmp_path):
    """At-least-once transport replay is skipped by receipt_id; the strict
    single-delivery path still reports it as a duplicate."""
    c, worker, plan, keys = remote(tmp_path)
    job = c.jobs[keys[0]]
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    complete = receipt_for(job, "COMPLETE", job["output_range"],
                           members=members_for(c, job, job["output_range"]))
    worker_job = worker._jobs[job["request_id"]]
    worker_job["receipts"].append(complete)
    c.collect(worker, keys[0])
    assert job["state"] == "OUTPUT_PENDING_VERIFY"
    worker_job["receipts"].append(dict(complete))      # identical replay
    c.collect(worker, keys[0])
    assert job["state"] == "OUTPUT_PENDING_VERIFY"
    assert job["covered"] == [job["output_range"]]
    with pytest.raises(FilmError, match="duplicate"):
        c.receive_receipt(dict(complete), worker)


def test_receipts_bound_to_snapshot_revision_nonce_and_actor(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    job = c.jobs[keys[0]]
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    rng = job["output_range"]
    with pytest.raises(FilmError, match="stale snapshot"):
        c.receive_receipt(receipt_for(job, "COMPLETE", rng,
                                      snapshot=RECIPE), worker)
    with pytest.raises(FilmError, match="nonce"):
        c.receive_receipt(receipt_for(job, "COMPLETE", rng,
                                      nonce="forged-nonce"), worker)
    with pytest.raises(FilmError, match="attempt"):
        c.receive_receipt(receipt_for(job, "COMPLETE", rng,
                                      attempt=job["attempt_id"] + 1),
                          worker)
    with pytest.raises(FilmError, match="plan revision"):
        c.receive_receipt(receipt_for(job, "COMPLETE", rng,
                                      revision=job["plan_revision"] + 1),
                          worker)
    with pytest.raises(FilmError, match="unauthorised|actor"):
        c.receive_receipt(receipt_for(job, "COMPLETE", rng,
                                      actor="stranger"), worker)
    with pytest.raises(FilmError, match="grant"):
        c.receive_receipt(receipt_for(job, "COMPLETE", rng,
                                      grant=SNAPSHOT), worker)


def test_request_nonce_stored_only_as_digest(tmp_path):
    """Receipts and runtime_state.json carry only the request nonce's
    sha256 digest — the raw nonce stays in memory for the live
    attempt."""
    c, worker, plan, keys = remote(tmp_path)
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    job = c.jobs[keys[0]]
    raw = c._nonces[keys[0]]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert job["nonce"] == digest and job["nonce"] != raw
    worker.complete(job["request_id"])
    c.collect(worker, keys[0])
    for receipt in job["receipts"]:
        assert receipt["nonce"] == digest
    text = (tmp_path / "state" / "runtime_state.json").read_text()
    assert raw not in text
    assert digest in text


def test_unknown_job_key_cannot_be_rekeyed(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    worker.drop_submit_ack = True
    c.reserve(keys[0])
    assert c.submit(worker, keys[0], frame_contract=dict(CONTRACT)) \
        == "UNKNOWN"
    with pytest.raises(FilmError, match="JOB_IDENTITY_EXISTS"):
        c.plan_jobs(plan)


# -- UNKNOWN lifecycle ------------------------------------------------------

def test_lost_submit_ack_is_unknown_and_fenced(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    worker.drop_submit_ack = True
    c.reserve(keys[0])
    assert c.submit(worker, keys[0], frame_contract=dict(CONTRACT)) \
        == "UNKNOWN"
    # every fence holds while UNKNOWN
    with pytest.raises(FilmError, match="UNKNOWN_FENCED"):
        c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    with pytest.raises(FilmError, match="UNKNOWN_FENCED"):
        c.release_reservation(keys[0])
    # and no substitute worker may take a range overlapping the UNKNOWN job
    other_worker = FakeRemoteWorker("remote-2")
    plan2 = make_plan(
        [operation("sub-a", [0, 4], "REMOTE_CPU", "remote-2",
                   recipe=RECIPE_B),
         operation("sub-b", [4, 8], "REMOTE_CPU", "remote-2",
                   recipe=RECIPE_B)])
    keys2 = c.plan_jobs(plan2)
    c.reserve(keys2[0])
    with pytest.raises(FilmError, match="UNKNOWN_FENCED"):
        c.submit(other_worker, keys2[0], frame_contract=dict(CONTRACT))
    # explicit reconcile on the same identity: the job was actually
    # accepted and is RUNNING on the worker.
    assert c.reconcile(worker, keys[0]) == "RUNNING"


def test_late_completion_reconciles_after_runtime_restart(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    worker.drop_submit_ack = True
    c.reserve(keys[0])
    assert c.submit(worker, keys[0], frame_contract=dict(CONTRACT)) \
        == "UNKNOWN"
    worker.complete(c.jobs[keys[0]]["request_id"])   # worker kept running
    # coordinator restart: a fresh instance reloads the same identity
    c2 = Coordinator(tmp_path / "state")
    assert c2.jobs[keys[0]]["state"] == "UNKNOWN"
    assert c2.jobs[keys[0]]["request_id"] == c.jobs[keys[0]]["request_id"]
    assert c2.reconcile(worker, keys[0]) == "OUTPUT_PENDING_VERIFY"
    assert c2.verify_outputs(keys[0]) == "VERIFIED"
    c2.seal(keys[0])
    assert c2.jobs[keys[0]]["state"] == "ARCHIVED"


def test_reconcile_confirms_never_accepted_as_failed(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    worker.drop_submit_ack = True
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    worker.lose(c.jobs[keys[0]]["request_id"])       # runtime forgot it
    c2 = Coordinator(tmp_path / "state")
    assert c2.reconcile(worker, keys[0]) == "FAILED_CONFIRMED"


def test_unreachable_worker_keeps_unknown_fenced(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    worker.drop_submit_ack = True
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    worker.dead = True
    assert c.reconcile(worker, keys[0]) == "UNKNOWN"
    with pytest.raises(FilmError, match="UNKNOWN_FENCED"):
        c.submit(worker, keys[0], frame_contract=dict(CONTRACT))


def test_dead_running_job_goes_unknown_then_reconciles(tmp_path):
    """runtime 종료·상태 불명: a worker that dies mid-run moves the job to
    UNKNOWN, the overlapping substitute stays fenced, and a later
    COMPLETE status reconciles to OUTPUT_PENDING_VERIFY."""
    c, worker, plan, keys = remote(tmp_path)
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    job = c.jobs[keys[0]]
    assert job["state"] == "RUNNING"
    worker.dead = True
    assert c.reconcile(worker, keys[0]) == "UNKNOWN"
    # the UNKNOWN fence blocks a substitute submit over the range
    other_worker = FakeRemoteWorker("remote-2")
    plan2 = make_plan(
        [operation("sub-a", [0, 4], "REMOTE_CPU", "remote-2",
                   recipe=RECIPE_B),
         operation("sub-b", [4, 8], "REMOTE_CPU", "remote-2",
                   recipe=RECIPE_B)])
    keys2 = c.plan_jobs(plan2)
    c.reserve(keys2[0])
    with pytest.raises(FilmError, match="UNKNOWN_FENCED"):
        c.submit(other_worker, keys2[0], frame_contract=dict(CONTRACT))
    # the original attempt actually finished; reconcile confirms it
    worker.revive()
    worker.complete(job["request_id"])
    assert c.reconcile(worker, keys[0]) == "OUTPUT_PENDING_VERIFY"
    assert c.verify_outputs(keys[0]) == "VERIFIED"


def test_dead_pending_verify_job_goes_unknown(tmp_path):
    """OUTPUT_PENDING_VERIFY -> UNKNOWN on an unreachable worker; the
    already-confirmed coverage reconciles straight back."""
    c, worker, plan, keys = remote(tmp_path)
    worker.auto_complete = True
    drive(c, worker, keys[0], plan, verify=False)
    assert c.jobs[keys[0]]["state"] == "OUTPUT_PENDING_VERIFY"
    worker.dead = True
    assert c.reconcile(worker, keys[0]) == "UNKNOWN"
    worker.revive()
    assert c.reconcile(worker, keys[0]) == "OUTPUT_PENDING_VERIFY"
    assert c.verify_outputs(keys[0]) == "VERIFIED"


def test_reconcile_not_found_keeps_pending_verify_coverage(tmp_path):
    """An UNKNOWN job holding a confirmed completion (it fell back from
    OUTPUT_PENDING_VERIFY) keeps its stored coverage when the worker
    answers NOT_FOUND — it returns to OUTPUT_PENDING_VERIFY, never
    FAILED_CONFIRMED, and a resume cannot discard the coverage."""
    c, worker, plan, keys = remote(tmp_path)
    worker.auto_complete = True
    drive(c, worker, keys[0], plan, verify=False)
    job = c.jobs[keys[0]]
    assert job["state"] == "OUTPUT_PENDING_VERIFY"
    worker.dead = True
    assert c.reconcile(worker, keys[0]) == "UNKNOWN"
    worker.revive()
    worker.lose(job["request_id"])            # runtime forgot the job
    assert c.reconcile(worker, keys[0]) == "OUTPUT_PENDING_VERIFY"
    assert job["covered"] == [job["output_range"]]
    with pytest.raises(FilmError, match="FAILED_CONFIRMED"):
        c.resume_failed(worker, keys[0], user_continued=True, plan=plan)
    assert job["covered"] == [job["output_range"]]
    assert c.verify_outputs(keys[0]) == "VERIFIED"


def test_resume_never_discards_pending_verify_coverage(tmp_path):
    """A failure confirmed after the completion was confirmed leaves the
    stored coverage pending verification — resume_failed refuses rather
    than clearing it."""
    c, worker, plan, keys = remote(tmp_path)
    worker.auto_complete = True
    drive(c, worker, keys[0], plan, verify=False)
    job = c.jobs[keys[0]]
    worker.fail(job["request_id"])
    c.collect(worker, keys[0])
    assert job["state"] == "FAILED_CONFIRMED"
    assert job["covered"] == [job["output_range"]]
    with pytest.raises(FilmError, match="RESUME_REFUSED"):
        c.resume_failed(worker, keys[0], user_continued=True, plan=plan)
    assert job["covered"] == [job["output_range"]]


def test_verify_failed_attempt_resume_clears_coverage(tmp_path):
    """Coverage whose verification already failed is not a pending
    completion — the bounded resume re-runs the range."""
    c, worker, plan, keys = remote(tmp_path)
    job = c.jobs[keys[0]]
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    bad = [{"frame_index": i, "sha256": RECIPE}
           for i in range(*job["output_range"])]
    c.receive_receipt(receipt_for(job, "COMPLETE", job["output_range"],
                                  members=bad), worker)
    assert c.verify_outputs(keys[0]) == "FAILED_CONFIRMED"
    assert c.resume_failed(worker, keys[0], user_continued=True,
                           plan=plan) == "RUNNING"
    assert job["covered"] == [] and job["members"] == {}


def test_restart_with_submitting_job_reloads_as_unknown(tmp_path):
    """A coordinator that dies after the submission intent was persisted
    reloads the job as UNKNOWN and reconciles the same job key, attempt
    id and request id — never a re-key, never a resubmit."""
    c, worker, plan, keys = remote(tmp_path)
    c.reserve(keys[0])
    real_submit = worker.submit

    def crash(packet, now_ms=0):
        real_submit(packet, now_ms=now_ms)   # worker accepted the job
        raise FilmError("coordinator died mid-submit")

    worker.submit = crash
    try:
        c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    except FilmError:
        pass
    finally:
        worker.submit = real_submit
    job = c.jobs[keys[0]]
    assert job["state"] == "SUBMITTING"
    # restart: the persisted SUBMITTING reloads as UNKNOWN
    c2 = Coordinator(tmp_path / "state")
    job2 = c2.jobs[keys[0]]
    assert job2["state"] == "UNKNOWN"
    assert job2["job_key"] == job["job_key"]
    assert job2["attempt_id"] == job["attempt_id"]
    assert job2["request_id"] == job["request_id"]
    # the attempt was actually accepted; reconcile confirms the same job
    assert c2.reconcile(worker, keys[0]) == "RUNNING"
    worker.complete(job2["request_id"])
    assert c2.reconcile(worker, keys[0]) == "OUTPUT_PENDING_VERIFY"
    job2 = c2.jobs[keys[0]]
    assert job2["attempt_id"] == job["attempt_id"]
    assert job2["request_id"] == job["request_id"]
    assert c2.verify_outputs(keys[0]) == "VERIFIED"


# -- cancel -------------------------------------------------------------------

def test_cancel_before_submit_confirms_immediately(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    c.reserve(keys[0])
    assert c.request_cancel(worker, keys[0]) == "CANCEL_CONFIRMED"


def test_completion_confirmed_before_cancel_goes_to_verify(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    worker.complete(c.jobs[keys[0]]["request_id"])
    # completion was already confirmed on the worker when cancel landed
    assert c.request_cancel(worker, keys[0]) == "OUTPUT_PENDING_VERIFY"
    assert c.verify_outputs(keys[0]) == "VERIFIED"
    c.seal(keys[0])
    assert c.jobs[keys[0]]["state"] == "ARCHIVED"


def test_cancel_after_receipts_collected_returns_to_verify(tmp_path):
    """The completion was already drained into OUTPUT_PENDING_VERIFY when
    the cancel landed; COMPLETED_FIRST moves it back so the confirmed
    output is verified and sealed, never stranded in CANCEL_REQUESTED."""
    c, worker, plan, keys = remote(tmp_path)
    worker.auto_complete = True
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    c.collect(worker, keys[0])
    assert c.jobs[keys[0]]["state"] == "OUTPUT_PENDING_VERIFY"
    assert c.request_cancel(worker, keys[0]) == "OUTPUT_PENDING_VERIFY"
    assert c.verify_outputs(keys[0]) == "VERIFIED"
    c.seal(keys[0])
    assert c.jobs[keys[0]]["state"] == "ARCHIVED"


def test_running_cancel_confirms_termination(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    assert c.request_cancel(worker, keys[0]) == "CANCEL_CONFIRMED"


def test_lost_cancel_ack_is_unknown_then_reconciled(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    worker.drop_cancel_ack = True
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    assert c.request_cancel(worker, keys[0]) == "UNKNOWN"
    with pytest.raises(FilmError, match="UNKNOWN_FENCED"):
        c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    assert c.reconcile(worker, keys[0]) == "CANCEL_CONFIRMED"


def test_verified_job_cannot_be_cancelled(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    worker.auto_complete = True
    drive(c, worker, keys[0], plan)
    with pytest.raises(FilmError, match="VERIFIED|seal"):
        c.request_cancel(worker, keys[0])


# -- bounded resume --------------------------------------------------------------

def test_failed_confirmed_resume_needs_user_and_allowance(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    worker.fail(c.jobs[keys[0]]["request_id"])
    c.collect(worker, keys[0])
    job = c.jobs[keys[0]]
    assert job["state"] == "FAILED_CONFIRMED"
    with pytest.raises(FilmError, match="explicit"):
        c.resume_failed(worker, keys[0], user_continued=False, plan=plan)
    attempt, request = job["attempt_id"], job["request_id"]
    assert c.resume_failed(worker, keys[0], user_continued=True,
                           plan=plan) == "RUNNING"
    job = c.jobs[keys[0]]
    assert job["attempt_id"] != attempt
    assert job["request_id"] != request
    # fail the second attempt: the bounded allowance is exhausted
    worker.fail(job["request_id"])
    c.collect(worker, keys[0])
    assert job["state"] == "FAILED_CONFIRMED"
    with pytest.raises(FilmError, match="exhausted"):
        c.resume_failed(worker, keys[0], user_continued=True, plan=plan)


def test_resume_refused_when_plan_binding_changed(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    c.reserve(keys[0])
    c.submit(worker, keys[0], frame_contract=dict(CONTRACT))
    worker.fail(c.jobs[keys[0]]["request_id"])
    c.collect(worker, keys[0])
    changed = remote_plan(revision=2)
    with pytest.raises(FilmError, match="plan binding"):
        c.resume_failed(worker, keys[0], user_continued=True,
                        plan=changed)


# -- concurrent seal --------------------------------------------------------------

def test_concurrent_seal_is_blocked(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    worker.auto_complete = True
    drive(c, worker, keys[0], plan)
    entered, release = threading.Event(), threading.Event()
    outcome = {}

    def first_seal():
        try:
            outcome["first"] = c.seal(keys[0], _entered=entered.set,
                                      _wait=release)
        except FilmError as e:
            outcome["first_error"] = e

    t = threading.Thread(target=first_seal)
    t.start()
    assert entered.wait(5)
    with pytest.raises(FilmError, match="SEAL_IN_PROGRESS"):
        c.seal(keys[0])
    release.set()
    t.join(5)
    assert "first" in outcome and outcome["first"]["job_key"] == keys[0]
    # a repeat seal is idempotent — the same record, never a second one
    assert c.seal(keys[0]) is outcome["first"] or \
        c.seal(keys[0]) == outcome["first"]
    assert len(c.seals) == 1


# -- manual packet route ---------------------------------------------------------

def test_manual_packet_waits_for_user_and_imports(tmp_path):
    c = coordinator(tmp_path)
    worker = FakeRemoteWorker()
    plan = remote_plan(transfer_route="MANUAL_PACKET", edges=[])
    key = c.plan_jobs(plan)[0]
    c.reserve(key)
    assert c.waiting_user(key) == "WAITING_USER"
    # no automatic attach: the user's explicit import moves it
    assert c.attach_resume(key, frame_contract=dict(CONTRACT)) \
        == "SUBMITTING"
    job = c.jobs[key]
    c.receive_receipt(receipt_for(job, "ACCEPTED", [0, 1]), worker)
    assert job["state"] == "RUNNING"
    c.receive_receipt(receipt_for(
        job, "COMPLETE", job["output_range"],
        members=members_for(c, job, job["output_range"])), worker)
    assert job["state"] == "OUTPUT_PENDING_VERIFY"
    assert c.verify_outputs(key) == "VERIFIED"


# -- bounded same-attempt transport retry -------------------------------------------

def test_edge_transport_retry_is_bounded_same_attempt():
    """Evolution §4.2.1: idempotent reads retry within the edge policy;
    auth errors and exhausted caps are never silently retried."""
    from types import SimpleNamespace
    from engine.archive_manifest import bounded_read
    from engine.storage_backends import ArchiveRequestError

    retry = {"max_retries": 3, "backoff_base_ms": 0, "max_backoff_ms": 0,
             "max_elapsed_ms": 60_000, "max_requests": 5,
             "max_transferred_bytes": 1 << 20}
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) <= 2:
            raise ArchiveRequestError(429, "rate_limit", "slow down")
        return SimpleNamespace(body=b"ok")

    assert bounded_read(flaky, retry, sleep_fn=lambda ms: None).body == b"ok"
    assert len(calls) == 3

    calls.clear()

    def denied():
        calls.append(1)
        raise ArchiveRequestError(403, "permission_denied", "no")

    with pytest.raises(ArchiveRequestError):
        bounded_read(denied, retry, sleep_fn=lambda ms: None)
    assert len(calls) == 1          # never retried an auth refusal

    calls.clear()

    def dead():
        calls.append(1)
        raise ArchiveRequestError(503, "unavailable", "down")

    with pytest.raises(ArchiveRequestError):
        bounded_read(dead, retry, sleep_fn=lambda ms: None)
    assert len(calls) == 4          # 1 attempt + max_retries=3


# -- relay auth -----------------------------------------------------------------

def captured_packet(c, worker, key):
    """Submit while capturing the packet the relay would carry."""
    seen = {}

    def spy(packet, now_ms=0):
        seen["packet"] = packet
        return {"skipped": True}
    original = worker.submit
    worker.submit = spy
    try:
        c.reserve(key)
        try:
            c.submit(worker, key, frame_contract=dict(CONTRACT))
        except Exception:
            pass
    finally:
        worker.submit = original
    return seen["packet"]


def test_grant_tamper_is_denied(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    packet = captured_packet(c, worker, keys[0])
    for field, value in (("audience", "other-worker"),
                         ("peer", "rogue-peer"),
                         ("credential_epoch", 2),
                         ("max_bytes", 1),
                         ("snapshot_digest", RECIPE)):
        grant = dict(packet["grant"])
        grant[field] = value
        forged = dict(packet)
        forged["grant"] = grant
        with pytest.raises(FilmError, match="RELAY_AUTH_DENIED"):
            worker.submit(forged, now_ms=0)


def test_expired_and_wrong_endpoint_denied(tmp_path):
    c = coordinator(tmp_path, now_ms=lambda: 5_000)
    worker = FakeRemoteWorker()
    plan = remote_plan()
    key = c.plan_jobs(plan)[0]
    packet = captured_packet(c, worker, key)
    with pytest.raises(FilmError, match="expired"):
        worker.submit(packet, now_ms=5_000 + 60_000)
    wrong_endpoint = dict(packet, endpoint="https://elsewhere.invalid/")
    with pytest.raises(FilmError, match="cross-host"):
        worker.submit(wrong_endpoint, now_ms=0)


def test_worker_packet_never_carries_user_tokens_or_fetch(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    packet = captured_packet(c, worker, keys[0])
    for key in ("oauth_token", "refresh_token", "access_token",
                "bearer", "fetch_url", "redirect_url", "callback_url"):
        forged = dict(packet)
        forged[key] = "x"
        with pytest.raises(FilmError, match="PACKET_SECRET_FORBIDDEN|Malformed"):
            check_packet(forged)
        nested = dict(packet)
        nested["grant"] = dict(packet["grant"], **{key: "x"})
        with pytest.raises(FilmError):
            check_packet(nested)


def test_scope_outside_grant_is_denied(tmp_path):
    c, worker, plan, keys = remote(tmp_path)
    packet = captured_packet(c, worker, keys[0])
    widened = dict(packet, output_range=[0, FRAMES + 4])
    with pytest.raises(FilmError, match="outside"):
        worker.submit(widened, now_ms=0)
    oversized = dict(packet, input_bytes=1 << 40)
    with pytest.raises(FilmError, match="max_bytes"):
        worker.submit(oversized, now_ms=0)


def test_grant_binds_objects_members_and_connection(tmp_path):
    """§17 / evolution §2.1.1: the grant pins the exact input object
    digests / member ids and the connection; a request outside any of
    them is denied."""
    c, worker, plan, keys = remote(tmp_path)
    packet = captured_packet(c, worker, keys[0])
    grant = packet["grant"]
    assert grant["object_digests"] == [OBJECT]
    assert grant["member_ids"] == sorted(INPUT_MEMBERS)
    assert grant["connection_digest"] == worker.connection_digest
    # a request naming an object outside the grant is denied
    forged = dict(packet, input_object_digests=[RECIPE])
    with pytest.raises(FilmError, match="RELAY_AUTH_DENIED"):
        worker.submit(forged, now_ms=0)
    # a request naming a member outside the grant is denied
    forged = dict(packet, input_members=["not-a-granted-member"])
    with pytest.raises(FilmError, match="RELAY_AUTH_DENIED"):
        worker.submit(forged, now_ms=0)
    # the same packet on a different connection is denied
    other_conn = FakeRemoteWorker("remote-1", connection_digest=RECIPE_B)
    with pytest.raises(FilmError, match="RELAY_AUTH_DENIED"):
        other_conn.submit(packet, now_ms=0)
    # the bound request still passes
    assert worker.submit(packet, now_ms=0)


def test_grant_revoked_at_completion_confirmation(tmp_path):
    """exec/storage §3.1 / evolution §2.1.1: the grant is voided the
    moment completion is confirmed — the transition into
    OUTPUT_PENDING_VERIFY, not only at terminal states."""
    c, worker, plan, keys = remote(tmp_path)
    packet = captured_packet(c, worker, keys[0])
    assert worker.submit(packet, now_ms=0)          # grant live
    worker.complete(packet["request_id"])
    assert c.reconcile(worker, keys[0]) == "OUTPUT_PENDING_VERIFY"
    with pytest.raises(FilmError, match="RELAY_AUTH_DENIED"):
        worker.submit(packet, now_ms=0)


def test_grant_revoked_after_completion(tmp_path):
    """Completion voids the grant: relay reuse after VERIFIED is denied.
    The worker consults the issuer's registry bound at submit — no
    manual set aliasing."""
    c, worker, plan, keys = remote(tmp_path)
    packet = captured_packet(c, worker, keys[0])
    assert worker.submit(packet, now_ms=0)          # grant live
    worker.complete(packet["request_id"])
    assert c.reconcile(worker, keys[0]) == "OUTPUT_PENDING_VERIFY"
    assert c.verify_outputs(keys[0]) == "VERIFIED"
    with pytest.raises(FilmError, match="RELAY_AUTH_DENIED"):
        worker.submit(packet, now_ms=0)


def test_grant_revoked_after_cancel_confirmation(tmp_path):
    """Cancel confirmation voids the grant: relay reuse is denied."""
    c, worker, plan, keys = remote(tmp_path)
    packet = captured_packet(c, worker, keys[0])
    assert worker.submit(packet, now_ms=0)
    assert c.request_cancel(worker, keys[0]) == "CANCEL_CONFIRMED"
    with pytest.raises(FilmError, match="RELAY_AUTH_DENIED"):
        worker.submit(packet, now_ms=0)


def test_new_attempt_revokes_prior_grant(tmp_path):
    """A new attempt voids the previous grant: the old packet never
    authenticates again."""
    c, worker, plan, keys = remote(tmp_path)
    packet = captured_packet(c, worker, keys[0])
    assert worker.submit(packet, now_ms=0)
    worker.fail(packet["request_id"])
    assert c.reconcile(worker, keys[0]) == "FAILED_CONFIRMED"
    with pytest.raises(FilmError, match="RELAY_AUTH_DENIED"):
        worker.submit(packet, now_ms=0)
    assert c.resume_failed(worker, keys[0], user_continued=True,
                           plan=plan) == "RUNNING"
    # the superseded grant stays void under the new attempt
    with pytest.raises(FilmError, match="RELAY_AUTH_DENIED"):
        worker.submit(packet, now_ms=0)


def test_revocation_rebinds_after_coordinator_restart(tmp_path):
    """A restarted coordinator reloads the issuer's revocation set from
    the state file and the worker re-binds to it through the normal
    protocol surface — the old packet stays denied."""
    c, worker, plan, keys = remote(tmp_path)
    packet = captured_packet(c, worker, keys[0])
    assert worker.submit(packet, now_ms=0)
    worker.complete(packet["request_id"])
    assert c.reconcile(worker, keys[0]) == "OUTPUT_PENDING_VERIFY"
    assert c.verify_outputs(keys[0]) == "VERIFIED"
    # coordinator restart: a fresh instance reloads the same identity
    # and the persisted revocations
    c2 = Coordinator(tmp_path / "state")
    assert c2.reconcile(worker, keys[0]) == "VERIFIED"
    assert worker.revocations is c2.revoked_grants
    with pytest.raises(FilmError, match="RELAY_AUTH_DENIED"):
        worker.submit(packet, now_ms=0)


def test_grant_digest_is_secret_free_binding():
    grant = issue_grant(issuer="coordinator", audience="remote-1",
                        peer="coordinator-user-desktop",
                        connection_digest=SNAPSHOT, credential_epoch=1,
                        job_key=RECIPE, attempt_id=1,
                        snapshot_digest=SNAPSHOT, ranges=[[0, 4]],
                        max_bytes=1024, expires_at_ms=1000, nonce="n")
    assert "token" not in " ".join(grant.keys())
    assert len(grant["grant_digest"]) == 64
    worker = FakeRemoteWorker(connection_digest=SNAPSHOT)
    assert check_grant(grant, worker=worker, job_key=RECIPE, attempt_id=1,
                       snapshot_digest=SNAPSHOT, output_range=[0, 4],
                       needed_bytes=512, now_ms=500)


# -- state machine completeness -------------------------------------------------

def test_state_machine_allows_only_listed_transitions():
    for state, allowed in TRANSITIONS.items():
        for target in allowed:
            assert target in {
                "RESERVED", "WAITING_USER", "SUBMITTING", "RUNNING",
                "UNKNOWN", "CANCEL_REQUESTED", "CANCEL_CONFIRMED",
                "OUTPUT_PENDING_VERIFY", "FAILED_CONFIRMED", "VERIFIED",
                "ARCHIVED"}
    terminal = {"CANCEL_CONFIRMED", "ARCHIVED"}
    # §5.3 text: the only edge out of FAILED_CONFIRMED is the explicit
    # user resume — a new attempt, never an automatic retry.
    assert TRANSITIONS["FAILED_CONFIRMED"] == {"RESERVED"}
    assert not TRANSITIONS.keys() - {
        "PLANNED", "RESERVED", "WAITING_USER", "SUBMITTING", "RUNNING",
        "OUTPUT_PENDING_VERIFY", "CANCEL_REQUESTED", "UNKNOWN",
        "VERIFIED", "FAILED_CONFIRMED"}
    for state in terminal:
        assert state not in TRANSITIONS or not TRANSITIONS[state]
