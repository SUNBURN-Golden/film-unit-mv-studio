"""film-review-diff: the "12 · 검수 대조" tab via AppTest.

Covers the empty board, a keyboard-entered finding bound to a cut and
frame range, the before/after closure with finding resolution kept apart
from full playback, and the stale-PASS / other-cut refusal buttons.
Author self-check is shown apart from independent review and director
approval.

AppTest does not exercise real-browser rendering, the spinner frame, or
keyboard focus order — those stay unverified in this environment.
"""
from pathlib import Path

from streamlit.testing.v1 import AppTest

from engine.animation_assets import import_frame_sequence
from engine.animation_migrate import animation_init
from engine.animation_schema import read_canon
from engine.review_diff import compare_finding
from test_anim_003 import make_sequence
from test_compiler_v03 import fixture_project
from test_film_review_diff import _reimport

ROOT = Path(__file__).resolve().parents[1]
NAME = "compiler_fixture"
TAB = "12 · 검수 대조"


def _home(monkeypatch, box):
    monkeypatch.setenv("FILM_UNIT_HOME", str(box / "home"))
    monkeypatch.setenv("FILM_UNIT_PROJECTS", str(box / "projects"))
    projects = box / "projects"
    projects.mkdir(exist_ok=True)
    return projects


def _panel(monkeypatch, box):
    _home(monkeypatch, box)
    at = AppTest.from_file(str(ROOT / "app/control_panel.py"),
                           default_timeout=180).run()
    if at.sidebar.selectbox[0].value != NAME:
        at.sidebar.selectbox[0].select(NAME).run()
    assert at.sidebar.selectbox[0].value == NAME
    assert not at.exception
    return at


def _animated(monkeypatch, box, shots=2):
    projects = _home(monkeypatch, box)
    project = fixture_project(projects, seconds=1, shot_count=shots)
    animation_init(project)
    document = read_canon(project / "timeline/edit.json")
    for entry in document["entries"]:
        count = entry["used_source_range"][1] - entry["used_source_range"][0]
        import_frame_sequence(
            project, entry["shot_id"],
            folder=make_sequence(box / entry["shot_id"], count, size=(32, 24),
                                 seed=int(entry["shot_id"][1:]) * 40))
    at = AppTest.from_file(str(ROOT / "app/control_panel.py"),
                           default_timeout=180).run()
    if at.sidebar.selectbox[0].value != NAME:
        at.sidebar.selectbox[0].select(NAME).run()
    assert not at.exception
    return project, at


def tab(at):
    return next(t for t in at.tabs if t.label == TAB)


def widget(block, kind, key):
    return next(w for w in getattr(block, kind) if w.key == key)


def body(block):
    return " ".join(e.value for e in
                    list(block.caption) + list(block.markdown)
                    + list(block.info))


def tables(block):
    return " ".join(d.value.to_string() for d in block.dataframe)


def test_legacy_project_has_no_review_diff_tab(tmp_path, monkeypatch):
    projects = _home(monkeypatch, tmp_path)
    fixture_project(projects, seconds=1, shot_count=1)
    at = _panel(monkeypatch, tmp_path)
    assert not any(t.label == TAB for t in at.tabs)
    assert any(t.label == "11 · 가사 검토" for t in at.tabs)


def test_empty_board_waits_without_findings(tmp_path, monkeypatch):
    project, at = _animated(monkeypatch, tmp_path)
    board = tab(at)
    metrics = {m.label: m.value for m in board.metric}
    assert metrics["화면"] == "대기"
    assert metrics["지적"] == "0"
    assert metrics["전체 재생"] == "없음"
    assert "기록이 없습니다" in body(board)
    assert "24fps" in body(board)
    assert "UNQUALIFIED" in body(board)
    assert "최종 승인 아님" in body(board)
    # Empty reviewer is an error, not a stored finding.
    widget(board, "button", "rd_open").click().run()
    assert not at.exception
    board = tab(at)
    assert any("검토자 이름" in e.value for e in board.error)
    assert not (project / "production/review_diff.jsonl").exists()


def test_keyboard_entry_binds_the_finding(tmp_path, monkeypatch):
    project, at = _animated(monkeypatch, tmp_path)
    board = tab(at)
    widget(board, "number_input", "rd_start").set_value(0)
    widget(board, "number_input", "rd_end").set_value(2)
    widget(board, "selectbox", "rd_problem").select("CONTACT")
    widget(board, "text_input", "rd_open_reviewer").set_value("UI reviewer")
    widget(board, "text_input", "rd_open_note").set_value(
        "hands overlap the prop")
    widget(board, "button", "rd_open").click().run()
    assert not at.exception
    board = tab(at)
    metrics = {m.label: m.value for m in board.metric}
    assert metrics["화면"] == "대조 완료"
    assert metrics["지적"] == "1"
    assert metrics["전체 재생"] == "없음"
    text = body(board) + tables(board)
    assert "CONTACT" in text
    assert "I001" in text and "S001" in text
    assert "[0, 2)" in text
    view = compare_finding(project, "RF0001")
    assert view["finding"]["frame_range"] == [0, 2]
    assert view["finding"]["problem_type"] == "CONTACT"
    assert view["finding"]["artifact_sha256"] in text
    assert "작성자 자기 확인: 아님" in text
    assert "독립 검토: 아님" in text
    assert "감독 승인: 아님" in text
    assert "CUT:I002" in tables(board) and "KEPT" in tables(board)


def test_revision_screen_splits_resolution_and_refuses_stale_passes(
        tmp_path, monkeypatch):
    project, at = _animated(monkeypatch, tmp_path)
    board = tab(at)
    widget(board, "text_input", "rd_open_reviewer").set_value("UI reviewer")
    widget(board, "text_input", "rd_open_note").set_value("flicker")
    widget(board, "button", "rd_open").click().run()
    assert not at.exception
    _reimport(project, tmp_path, "I001", seed=77)
    at.run()
    assert not at.exception
    board = tab(at)
    view = compare_finding(project, "RF0001")
    text = body(board)
    assert view["before"]["artifact_sha256"] in text
    assert view["after"]["artifact_sha256"] in text
    assert view["before"]["artifact_sha256"] != view["after"]["artifact_sha256"]
    metrics = {m.label: m.value for m in board.metric}
    assert metrics["전체 재생"] == "필요"
    # The selected-finding metrics repeat the split.
    assert any(m.label == "지적 해소" and m.value == "아니오" for m in board.metric)
    assert any(m.label == "전체 재생" and m.value == "필요" for m in board.metric)
    closure = tables(next(e for e in board.expander if "closure" in e.label))
    assert "CUT:I001" in closure and "STALE" in closure
    assert "CUT:I002" in closure and "KEPT" in closure
    widget(board, "text_input", "rd_rev_reviewer").set_value("UI author")
    widget(board, "text_input", "rd_rev_note").set_value("replaced the frames")
    widget(board, "button", "rd_record").click().run()
    assert not at.exception
    board = tab(at)
    assert any("대조를 기록했습니다" in s.value for s in board.success)
    assert "PASS는 승계되지 않았습니다" in " ".join(s.value for s in board.success)
    # Author self-check is not a pass.
    widget(board, "selectbox", "rd_ack_role").select("AUTHOR_SELF_CHECK")
    widget(board, "selectbox", "rd_ack_decision").select("CHECKED")
    widget(board, "text_input", "rd_ack_reviewer").set_value("UI author")
    widget(board, "text_input", "rd_ack_note").set_value("I looked at my fix")
    widget(board, "button", "rd_ack").click().run()
    assert not at.exception
    board = tab(at)
    text = body(board)
    assert "작성자 자기 확인: 기록됨" in text
    assert "독립 검토: 아님" in text
    assert "감독 승인: 아님" in text
    assert any(m.label == "지적 해소" and m.value == "아니오" for m in board.metric)
    assert any(m.label == "전체 재생" and m.value == "필요" for m in board.metric)
    widget(board, "selectbox", "rd_ack_decision").select("CUT_PASS")
    widget(board, "button", "rd_ack").click().run()
    board = tab(at)
    assert any("작성자 자기 확인" in e.value for e in board.error)
    # Independent review resolves the finding and still leaves playback.
    widget(board, "selectbox", "rd_ack_role").select("INDEPENDENT_REVIEW")
    widget(board, "selectbox", "rd_ack_decision").select("FINDING_RESOLVED")
    widget(board, "text_input", "rd_ack_reviewer").set_value("UI reviewer")
    widget(board, "text_input", "rd_ack_note").set_value(
        "the cited frames are fixed")
    widget(board, "button", "rd_ack").click().run()
    assert not at.exception
    board = tab(at)
    text = body(board)
    assert "독립 검토: 기록됨" in text
    assert "감독 승인: 아님" in text
    assert any(m.label == "지적 해소" and m.value == "예" for m in board.metric)
    assert any(m.label == "전체 재생" and m.value == "필요" for m in board.metric)
    assert any(m.label == "컷 PASS" and m.value == "승계 안 됨" for m in board.metric)
    # Stale PASS and the other cut's revision are refused.
    widget(board, "button", "rd_carry").click().run()
    board = tab(at)
    assert any("승계되지 않습니다" in e.value for e in board.error)
    widget(board, "button", "rd_other").click().run()
    board = tab(at)
    assert any("다른 컷" in e.value for e in board.error)
    assert not at.exception
