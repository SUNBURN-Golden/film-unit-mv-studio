"""ANIM-012 engine coverage: path-B cancel request/confirmation split and
the cancel/completion race, driven by the deterministic FAKE provider.

A cancel is a request until the provider confirms termination (schema 13):
an in-flight job moves to CANCEL_REQUESTED and no resubmission, substitute
submission or reservation release is allowed while the outcome is
unconfirmed. A completion confirmed before the cancel lands keeps the
returned clip for verification — it is never dropped. A lost cancel
acknowledgement fences the job UNKNOWN, resolvable only by an explicit
same-identity reconcile. A cancel is never recorded as a refund.

Everything here is local protocol evidence only — FAKE/UNQUALIFIED, no real
provider call, no paid generation.
"""
import pytest

from engine.core import FilmError, read
from engine.segment_fake import configure_fake, fake_state, make_adapter
from engine.segment_gen import (load_job_journal, segment_cancel,
                                segment_import, segment_jobs, segment_quote,
                                segment_reconcile)
from test_anim_010 import FRAMES, _job_record, _submit, b_scene


def test_cancel_running_job_confirms_termination(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    sub = _submit(p)
    assert sub["status"] == "RUNNING"
    result = segment_cancel(p, sub["job_id"])
    assert result["status"] == "CANCEL_CONFIRMED"
    assert result["qualification_state"] == "UNQUALIFIED"
    # Request and observation are distinct journaled acts bound to the
    # same request identity and toolchain.
    journal = load_job_journal(p, "S001", sub["job_id"])
    assert [r["event"] for r in journal][-2:] == [
        "CANCEL_INTENT", "CANCEL_OBSERVED"]
    intent = journal[-2]["data"]
    assert intent["request_id"] == sub["request_id"]
    assert intent["toolchain"]["id"] == "fake_segment"
    assert journal[-1]["data"]["outcome"] == "CANCELLED"
    # The reservation is not silently refunded: it stays in the ledger and
    # the charge reflects what the provider reported, not "cancel = free".
    ledger = read(p / "render/ledger.json")
    assert sub["reservation"]["ledger_key"] in ledger["jobs"]
    job = _job_record(p, sub["job_id"])
    assert job["charge_state"] == "CONFIRMED_BILLED"
    # A confirmed-terminal job accepts neither a second cancel nor a
    # resubmission under the same input identity.
    with pytest.raises(FilmError, match="TERMINAL_JOB"):
        segment_cancel(p, sub["job_id"])
    with pytest.raises(FilmError, match="JOB_TERMINATED"):
        _submit(p)
    with pytest.raises(FilmError, match="NOTHING_TO_RECONCILE"):
        segment_reconcile(p, sub["job_id"])
    assert len(fake_state(p)["requests"]) == 1


def test_cancel_race_completion_confirmed_first_keeps_result(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    sub = _submit(p)
    configure_fake(p, behaviors={"complete_before_cancel": True})
    result = segment_cancel(p, sub["job_id"])
    # The completion was confirmed before the cancel landed: the returned
    # clip is preserved for verification, not dropped.
    assert result["status"] == "OUTPUT_PENDING_VERIFY"
    journal = load_job_journal(p, "S001", sub["job_id"])
    observed = [r for r in journal if r["event"] == "CANCEL_OBSERVED"]
    assert observed[-1]["data"]["outcome"] == "COMPLETED_FIRST"
    imported = segment_import(p, sub["job_id"])
    assert imported["status"] == "VERIFIED"
    assert imported["member_range"] == [0, FRAMES]
    assert _job_record(p, sub["job_id"])["charge_state"] == \
        "CONFIRMED_BILLED"


def test_cancel_pending_stays_requested_until_same_identity_reconcile(
        tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    sub = _submit(p)
    configure_fake(p, behaviors={"cancel_pending": True})
    result = segment_cancel(p, sub["job_id"])
    # 요청과 확인 분리: delivered but not yet a confirmed termination.
    assert result["status"] == "CANCEL_REQUESTED"
    # Fenced: no resubmission, no duplicate cancel while unconfirmed.
    with pytest.raises(FilmError, match="JOB_FENCED"):
        _submit(p)
    with pytest.raises(FilmError, match="CANCEL_IN_FLIGHT"):
        segment_cancel(p, sub["job_id"])
    assert len(segment_jobs(p)["jobs"]) == 1
    assert len(fake_state(p)["requests"]) == 1
    # The explicit same-identity reconcile observes termination.
    rec = segment_reconcile(p, sub["job_id"])
    assert rec["status"] == "CANCEL_CONFIRMED"
    ledger = read(p / "render/ledger.json")
    assert sub["reservation"]["ledger_key"] in ledger["jobs"]


def test_lost_cancel_ack_fences_unknown_then_reconcile_confirms(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    sub = _submit(p)
    configure_fake(p, behaviors={"lost_cancel_ack": True})
    result = segment_cancel(p, sub["job_id"])
    # The answer never arrived: UNKNOWN, reservation held, identity fenced.
    assert result["status"] == "UNKNOWN"
    with pytest.raises(FilmError, match="JOB_FENCED"):
        _submit(p)
    # A cancel cannot decide an already-ambiguous acceptance either.
    with pytest.raises(FilmError, match="JOB_FENCED"):
        segment_cancel(p, sub["job_id"])
    journal = load_job_journal(p, "S001", sub["job_id"])
    observed = [r for r in journal if r["event"] == "CANCEL_OBSERVED"]
    assert observed[-1]["data"]["outcome"] == "LOST"
    rec = segment_reconcile(p, sub["job_id"])
    assert rec["status"] == "CANCEL_CONFIRMED"
    assert len(fake_state(p)["requests"]) == 1


def test_cancel_only_cancels_in_flight_or_reserved_work(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    quote = segment_quote(p, "S001", 0, FRAMES)
    # PLANNED holds no reservation and no submission — nothing to cancel.
    with pytest.raises(FilmError, match="NOTHING_TO_CANCEL"):
        segment_cancel(p, quote["job_id"])
    # A confirmed failure is terminal: no cancel, and resubmission needs the
    # explicit reviewed retry instead.
    configure_fake(p, behaviors={"reject": True})
    sub = _submit(p, quote=segment_quote(p, "S001", 0, FRAMES))
    assert sub["status"] == "FAILED_CONFIRMED"
    with pytest.raises(FilmError, match="TERMINAL_JOB"):
        segment_cancel(p, sub["job_id"])


def test_cancel_on_unknown_acceptance_is_fenced(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    configure_fake(p, behaviors={"lost_ack": True,
                                 "lost_ack_delivered": True})
    sub = _submit(p)
    assert sub["status"] == "UNKNOWN"
    with pytest.raises(FilmError, match="JOB_FENCED"):
        segment_cancel(p, sub["job_id"])
    # Only the explicit reconcile on the same identity resolves it.
    rec = segment_reconcile(p, sub["job_id"])
    assert rec["status"] == "OUTPUT_PENDING_VERIFY"


def test_declared_adapter_cancel_refuses_like_submit(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    adapter = make_adapter(p, "gemini_video")
    with pytest.raises(FilmError, match="UNQUALIFIED"):
        adapter.cancel("req-none")


def test_cli_segment_cancel(tmp_path, capsys):
    from engine import cli
    import json
    p, _, _, _ = b_scene(tmp_path)
    assert cli.main(["segment-quote", str(p), "S001",
                     "--start", "0", "--end", str(FRAMES)]) == 0
    quote = json.loads(capsys.readouterr().out)
    assert cli.main(["segment-submit", str(p), "S001",
                     "--start", "0", "--end", str(FRAMES),
                     "--approver", "tester", "--quote-id", quote["quote_id"],
                     "--cap", "10000"]) == 0
    sub = json.loads(capsys.readouterr().out)
    assert cli.main(["segment-cancel", str(p), sub["job_id"]]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "CANCEL_CONFIRMED"
    assert out["qualification_state"] == "UNQUALIFIED"
