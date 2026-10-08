"""film-workspace-accessibility: the "13 · 작업실" tab via AppTest.

Headless stand-in for the browser. Real Tab order, arrow keys and a
resized desktop window are not observable here and stay unverified.
Synthetic protocol records only — qualification UNQUALIFIED.
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from engine.animation_migrate import animation_init
from engine.workspace_accessibility import NOTE_LIMIT, load_prefs
from test_compiler_v03 import fixture_project

ROOT = Path(__file__).resolve().parents[1]
NAME = "compiler_fixture"
TAB = "13 · 작업실"


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FILM_UNIT_PROJECTS", str(tmp_path / "projects"))
    return tmp_path


def run_panel():
    at = AppTest.from_file(str(ROOT / "app/control_panel.py"),
                           default_timeout=180).run()
    assert not at.exception
    return at


def open_project(box, *, animate=True):
    projects = box / "projects"
    projects.mkdir()
    project = fixture_project(projects, seconds=2, shot_count=2)
    if animate:
        animation_init(project)
    at = run_panel()
    at.sidebar.selectbox[0].select(NAME).run()
    assert not at.exception
    return project, at


def button(at, key):
    return next(b for b in at.button if b.key == key)


def number(at, key):
    return next(n for n in at.number_input if n.key == key)


def sel(at, key):
    return next(s for s in at.selectbox if s.key == key)


def area(at, key):
    return next(t for t in at.text_area if t.key == key)


def check(at, key):
    return next(c for c in at.checkbox if c.key == key)


def errors(at):
    return [e.value for e in at.error]


def texts(at):
    return " ".join(getattr(item, "value", "") for item in
                    list(at.caption) + list(at.markdown) + list(at.info))


def test_empty_project_form_explains_the_blank_workspace(box):
    at = run_panel()
    body = texts(at)
    assert "작업실이 비어 있습니다" in body
    assert "JSON" in body and "편집하지 않습니다" in body
    assert any(b.label == "프로젝트 만들기" for b in at.button)


def test_legacy_has_no_workspace_tab(box):
    _project, at = open_project(box, animate=False)
    assert not any(t.label == TAB for t in at.tabs)
    assert not any(getattr(w, "key", None) == "wa_frame"
                   for w in at.number_input)
    assert any(t.label == "11 · 가사 검토" for t in at.tabs)


def test_workspace_shows_next_action_help_and_text_status(box):
    _project, at = open_project(box)
    assert any(t.label == TAB for t in at.tabs)
    body = texts(at)
    assert "다음은 준비입니다" in body
    assert "상태 글자:" in body
    assert "✓ 검수 완료" in body and "✕ 실패" in body and "… 진행 중" in body
    assert "○ 미검수" in body
    assert "색만이 아니라" in body
    assert "자격 없음" in body and "UNQUALIFIED" in body
    assert "로그인이나 네트워크" in body
    assert "자동 재시도: 아니오" in body
    assert "240초" in body and "24fps" in body
    assert "edit.json" not in body
    ours = [w for w in list(at.text_area) + list(at.text_input)
            + list(at.number_input) if str(w.key or "").startswith("wa_")]
    assert ours and not any("JSON" in (w.label or "") or "edit.json" in (w.label or "")
                            for w in ours)
    assert "SECRET" not in body
    board = next(t for t in at.tabs if t.label == TAB)
    assert any(m.label == "준비" and "진행 중" in m.value for m in board.metric)
    tables = [d.value for d in at.dataframe
              if {"프레임", "상태", "선택"} <= set(getattr(d.value, "columns", []))]
    assert tables
    status = " ".join(" ".join(map(str, table["상태"])) for table in tables)
    picked = " ".join(" ".join(map(str, table["선택"])) for table in tables)
    # No sequence yet, so the cut is unresolved — still a word plus a mark.
    assert "미해결" in status and "▶ 선택" in picked


def test_keyboard_controls_move_frame_without_writing_until_save(box):
    project, at = open_project(box)
    assert number(at, "wa_frame").value == 0
    button(at, "wa_next").click().run()
    assert not at.exception
    assert number(at, "wa_frame").value == 1
    assert not (project / "workspace" / "accessibility.json").exists()
    button(at, "wa_zoom_in").click().run()
    assert not at.exception
    assert sel(at, "wa_zoom").value == "close"
    button(at, "wa_end").click().run()
    assert not at.exception
    end = int(number(at, "wa_frame").value)
    assert end > 1
    button(at, "wa_next").click().run()
    assert int(number(at, "wa_frame").value) == end
    button(at, "wa_home").click().run()
    assert int(number(at, "wa_frame").value) == 0
    area(at, "wa_note").set_value("키보드로 고른 프레임")
    button(at, "wa_save").click().run()
    assert not at.exception
    assert any("기억했습니다" in s.value for s in at.success)
    saved = load_prefs(project)
    assert saved["selected_frame"] == 0
    assert saved["zoom"] == "close"
    assert saved["draft_note"] == "키보드로 고른 프레임"
    assert "저장된 선택 프레임: 0" in texts(at)


def test_failed_save_keeps_typed_input_and_saved_frame(box):
    project, at = open_project(box)
    number(at, "wa_frame").set_value(4)
    area(at, "wa_note").set_value("남겨 둘 메모")
    button(at, "wa_save").click().run()
    assert not at.exception
    assert load_prefs(project)["selected_frame"] == 4
    number(at, "wa_frame").set_value(9)
    area(at, "wa_note").set_value("가" * (NOTE_LIMIT + 1))
    button(at, "wa_save").click().run()
    assert any("그대로입니다" in err for err in errors(at))
    kept = load_prefs(project)
    assert kept["selected_frame"] == 4
    assert kept["draft_note"] == "남겨 둘 메모"
    assert number(at, "wa_frame").value == 9
    assert area(at, "wa_note").value == "가" * (NOTE_LIMIT + 1)
    assert "저장된 선택 프레임: 4" in texts(at)


def test_reduced_motion_and_narrow_layout(box):
    project, at = open_project(box)
    body = texts(at)
    assert "prefers-reduced-motion" in body
    assert "max-width: 880px" in body
    assert "화면 배치: 넓게" in body
    board = next(t for t in at.tabs if t.label == TAB)
    assert any(m.label == "준비" for m in board.metric)
    check(at, "wa_reduced").set_value(True)
    check(at, "wa_narrow").set_value(True)
    at.run()
    assert not at.exception
    body = texts(at)
    assert "동작 줄이기 켜짐" in body
    assert "화면 배치: 좁게" in body
    assert "data-layout=\"좁게\"" in body or "data-layout=\"좁게\"" in body
    board = next(t for t in at.tabs if t.label == TAB)
    assert not any(m.label == "준비" for m in board.metric)
    assert "준비" in body and "출력" in body
    number(at, "wa_frame").set_value(2)
    button(at, "wa_save").click().run()
    assert not at.exception
    saved = load_prefs(project)
    assert saved["reduced_motion"] is True
    assert saved["narrow_layout"] is True
    assert saved["selected_frame"] == 2


def test_restart_and_error_keep_the_note(box):
    project, at = open_project(box)
    area(at, "wa_note").set_value("오류 뒤에도")
    number(at, "wa_frame").set_value(5)
    button(at, "wa_save").click().run()
    assert load_prefs(project)["draft_note"] == "오류 뒤에도"
    journal = project / "render" / "execution" / "job_journal.jsonl"
    journal.parent.mkdir(parents=True)
    raw = b'{"truncated":true'
    journal.write_bytes(raw)
    at.run()
    assert not at.exception
    body = texts(at)
    assert "이어서" in body
    assert "자동으로 다시 보내지 않습니다" in body
    button(at, "wa_resume").click().run()
    assert not at.exception
    assert journal.read_bytes() == raw
    assert load_prefs(project)["resume_ack"] is True
    assert load_prefs(project)["selected_frame"] == 5
    assert load_prefs(project)["draft_note"] == "오류 뒤에도"
    timeline = project / "timeline" / "edit.json"
    timeline.write_text("{", encoding="utf-8")
    at.run()
    assert any("타임라인을 읽지 못했습니다" in err for err in errors(at))
    assert area(at, "wa_note").value == "오류 뒤에도"
    assert number(at, "wa_frame").value == 5
    assert "저장된 선택 프레임: 5" in texts(at)
