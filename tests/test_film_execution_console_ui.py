"""film-execution-console UI — AppTest coverage of the queue/status panel.

Drives `tests/exec_console_fixture.py` (a real `streamlit run` script)
through streamlit.testing.v1.AppTest. Every state is produced by the
local fakes only — UNQUALIFIED, no network, no credentials.
"""
import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "exec_console_fixture.py"


def run_panel(tmp_path, monkeypatch, scenario):
    monkeypatch.setenv("EXEC_FIXTURE_DIR", str(tmp_path))
    monkeypatch.setenv("EXEC_FIXTURE_SCENARIO", scenario)
    at = AppTest.from_file(str(FIXTURE), default_timeout=60).run()
    assert not at.exception
    return at


def texts(at):
    out = [m.value for m in at.markdown]
    out += [c.value for c in at.caption]
    out += [i.body for i in at.info]
    out += [w.body for w in at.warning]
    out += [e.value for e in at.error]
    return out


def labels(at):
    return [b.label for b in at.button]


# -- normal / in-progress --------------------------------------------------------

def test_running_job_shows_queue_and_stages(tmp_path, monkeypatch):
    at = run_panel(tmp_path, monkeypatch, "running")
    assert any("실행 작업실" in h.value for h in at.subheader)
    assert any("RUNNING" in m.value for m in at.markdown)
    assert any("op-a" in m.value for m in at.markdown)
    # the six-stage table is rendered
    assert at.dataframe
    stage_rows = str(at.dataframe[0].value)
    for stage in ("compose", "encode", "mux", "verify", "upload", "seal"):
        assert stage in stage_rows
    # independent indicators caption: network / remote / upload verify
    assert any("네트워크" in c.value and "원격" in c.value
               and "업로드 검증" in c.value for c in at.caption)
    # in-flight job -> recovery expander with real (keyboard) buttons
    assert any("중단 위치" in e.label for e in at.expander)
    assert "상태 조회 — reconcile" in labels(at)
    assert "취소 요청" in labels(at)


def test_reserved_job_offers_cancel_before_submit(tmp_path, monkeypatch):
    at = run_panel(tmp_path, monkeypatch, "reserved")
    assert any("RESERVED" in m.value for m in at.markdown)
    assert any("중단 위치" in e.label for e in at.expander)
    cancel = next(b for b in at.button if b.label == "취소 요청")
    assert not cancel.disabled
    cancel.click().run()
    assert not at.exception
    assert any("CANCEL_CONFIRMED" in m.value for m in at.markdown)


def test_cancel_requested_never_shown_as_terminal(tmp_path, monkeypatch):
    at = run_panel(tmp_path, monkeypatch, "cancel_requested")
    assert any("CANCEL_REQUESTED" in m.value for m in at.markdown)
    assert not any("CANCELLED" in m.value or "취소 확정" in m.value
                   for m in at.markdown)
    stage_rows = str(at.dataframe[0].value)
    assert "CANCEL_REQUESTED" in stage_rows
    # a status query button is offered; nothing marks it finished
    assert "상태 조회 — reconcile" in labels(at)


def test_cancel_complete_race_is_shown_truthfully(tmp_path, monkeypatch):
    at = run_panel(tmp_path, monkeypatch, "race")
    assert any("OUTPUT_PENDING_VERIFY" in m.value for m in at.markdown)
    bodies = texts(at)
    assert any("completion was confirmed first" in b for b in bodies)
    # verify button runs the coordinator's own verification
    assert "출력 검증 실행" in labels(at)
    verify = next(b for b in at.button if b.label == "출력 검증 실행")
    assert not verify.disabled
    verify.click().run()
    assert not at.exception
    assert any("VERIFIED" in m.value for m in at.markdown)


def test_unknown_reconcile_via_button_never_resubmits(tmp_path, monkeypatch):
    at = run_panel(tmp_path, monkeypatch, "unknown")
    assert any("UNKNOWN" in m.value for m in at.markdown)
    assert any("미해결" in w.body for w in at.warning)
    q = next(b for b in at.button if b.label == "상태 조회 — reconcile")
    assert not q.disabled
    # every resubmit-shaped action is refused in the model — the panel
    # only ever offers the reconcile button
    assert not any("새 attempt" == b.label for b in at.button)
    q.click().run()
    assert not at.exception
    assert any("RUNNING" in m.value for m in at.markdown)


def test_disconnect_lights_network_loss_not_last_receipt(
        tmp_path, monkeypatch):
    at = run_panel(tmp_path, monkeypatch, "disconnect")
    assert any("UNKNOWN" in m.value for m in at.markdown)
    # the dropped status answer lights the network lamp, and the remote
    # caption carries the undetermined outcome, not just the stale
    # ACCEPTED receipt
    assert any("네트워크 **LOST**" in c.value and "STATUS_LOST" in c.value
               for c in at.caption)
    assert any("undetermined" in c.value for c in at.caption)


def test_failed_verify_offers_new_attempt_not_a_done_toast(
        tmp_path, monkeypatch):
    at = run_panel(tmp_path, monkeypatch, "verify_fails")
    assert any("OUTPUT_PENDING_VERIFY" in m.value for m in at.markdown)
    verify = next(b for b in at.button if b.label == "출력 검증 실행")
    assert not verify.disabled
    verify.click().run()
    assert not at.exception
    # the toast reports the real outcome — FAILED_CONFIRMED is not 완료,
    # and the closed job still opens its stop-point/new-attempt controls
    assert any("FAILED_CONFIRMED" in s.value for s in at.success)
    assert any("FAILED_CONFIRMED" in m.value for m in at.markdown)
    assert any((df.value.astype(str) == "VERIFY_MISMATCH").any().any()
               for df in at.dataframe)
    assert "새 attempt (사용자 확인 필요)" in labels(at)


# -- empty / error ---------------------------------------------------------------

def test_empty_queue_state(tmp_path, monkeypatch):
    at = run_panel(tmp_path, monkeypatch, "empty")
    assert any("실행 작업이 없습니다" in i.body for i in at.info)


def test_missing_state_dir_is_info_not_error(tmp_path, monkeypatch):
    at = run_panel(tmp_path, monkeypatch, "nodir")
    assert any("실행 상태 폴더가 없습니다" in i.body for i in at.info)
    assert not at.error


# -- control-panel shape (project=, no explicit state_dir) ------------------------

def test_project_panel_reads_render_execution_without_a_path_field(
        tmp_path, monkeypatch):
    at = run_panel(tmp_path, monkeypatch, "project_running")
    # the project's own execution state is shown ...
    assert any("RUNNING" in m.value for m in at.markdown)
    assert not any("실행 상태 폴더가 없습니다" in i.body for i in at.info)
    # ... and no free-text path field exists — a path typed into the
    # browser never becomes the folder this tab reads
    assert not at.text_input


def test_project_panel_without_state_dir_is_info_not_error(
        tmp_path, monkeypatch):
    at = run_panel(tmp_path, monkeypatch, "project_nodir")
    assert any("실행 상태 폴더가 없습니다" in i.body for i in at.info)
    assert not at.error
    assert not at.text_input


def test_fenced_journal_is_an_error_state(tmp_path, monkeypatch):
    at = run_panel(tmp_path, monkeypatch, "fenced")
    assert any("fence" in e.value.lower() for e in at.error)


# -- archive commit (upload vs verify) ---------------------------------------------

def test_pipeline_opens_its_stop_point_and_reads_not_applicable(
        tmp_path, monkeypatch):
    at = run_panel(tmp_path, monkeypatch, "pipeline")
    # the pipeline row is the only item and it is work in hand — its
    # stop-point expander opens (it was never opened before)
    assert any("파이프라인" in m.value for m in at.markdown)
    assert len([e for e in at.expander if "중단 위치" in e.label]) == 1
    # beside the VERIFIED verify cell the upload-verification caption
    # says not-applicable — nothing is archived for this snapshot —
    # instead of reading unfinished
    caps = [c.value for c in at.caption]
    assert any("업로드 검증" in c and "해당 없음" in c for c in caps)
    assert not any("없음/미완료" in c for c in caps)


def test_sealed_commit_shows_uploaded_and_verified(tmp_path, monkeypatch):
    at = run_panel(tmp_path, monkeypatch, "commit")
    bodies = texts(at)
    assert any("SEALED" in m.value for m in at.markdown)
    # upload item: transfer complete AND readback/hash verified are
    # separate visible facts
    assert any("VERIFIED" in str(df.value) for df in at.dataframe)
    assert any("UPLOADED" in str(df.value) for df in at.dataframe)
    assert any("UNQUALIFIED" in b for b in bodies)
