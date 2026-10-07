"""film-shot-board: the "10 · 컷 작업대" tab via AppTest.

Headless coverage of the normal / empty-resolution / error / legacy states,
instance-id selection across a timeline reorder, jump-to-frame navigation,
the boundary-frame + transition-ownership fixture, and the draft → compare →
apply loop. Synthetic protocol records only: qualification UNQUALIFIED,
acceptance PENDING, release NOT_AUTHORIZED. Real-browser rendering and
keyboard focus order are not exercised by AppTest and stay unverified.
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from engine.animation_assets import import_frame_sequence
from engine.animation_migrate import animation_init
from engine.animation_schema import read_canon
from engine.shot_board import apply_edit, propose_edit
from test_anim_003 import make_sequence
from test_compiler_v03 import fixture_project

ROOT = Path(__file__).resolve().parents[1]
NAME = "compiler_fixture"
TAB = "10 · 컷 작업대"


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FILM_UNIT_PROJECTS", str(tmp_path / "projects"))
    return tmp_path


def run_panel(project):
    at = AppTest.from_file(str(ROOT / "app/control_panel.py"),
                           default_timeout=180).run()
    at.sidebar.selectbox[0].select(NAME).run()
    assert not at.exception
    return at


def screen(box, *, animate=True, imports=3, spare=6):
    projects = box / "projects"
    projects.mkdir()
    project = fixture_project(projects, seconds=3, shot_count=3)
    if animate:
        animation_init(project)
        for index in range(imports):
            shot = f"S{index + 1:03d}"
            import_frame_sequence(
                project, shot,
                folder=make_sequence(box / f"seq{index}",
                                     24 + spare if index == 0 else 24,
                                     seed=index * 100))
    return project, run_panel(project)


def button(at, key):
    return next(b for b in at.button if b.key == key)


def sel(at, key):
    return next(s for s in at.selectbox if s.key == key)


def number(at, key):
    return next(n for n in at.number_input if n.key == key)


def tab(at):
    return next(t for t in at.tabs if t.label == TAB)


def texts(at):
    return " ".join(e.value for e in
                    list(at.caption) + list(at.markdown))


def _reorder_first_two(p):
    path = p / "timeline/edit.json"
    document = read_canon(path)
    document["entries"][0], document["entries"][1] = \
        document["entries"][1], document["entries"][0]
    for left, right in zip(document["entries"], document["entries"][1:]):
        left["transition_out"]["to_instance"] = right["instance_id"]
    from engine.animation_schema import write_canon
    write_canon(path, document)


# --- normal / error / legacy states ---------------------------------------------

def test_workbench_lists_cuts_transitions_and_clock(tmp_path, box):
    project, at = screen(box)
    board = tab(at)
    metrics = {m.label: m.value for m in board.metric}
    assert metrics["컷"] == "3" and metrics["출력 프레임"] == "72"
    assert metrics["정수 클럭"] == "24fps"
    select = next(s for s in board.selectbox if s.key == "sb_instance")
    assert [o.split(" · ")[0] for o in select.options] == \
        ["I001", "I002", "I003"]
    assert select.value == "I001"
    tables = " ".join(d.value.to_string() for d in board.dataframe)
    assert "T001" in tables and "F_000024.png" in tables
    assert "F_000025.png" in tables and "I001" in tables
    assert "기록 소유" in texts(at) or "기록 소유" in tables
    assert any("컷 작업대" in c.value or "정수 프레임" in c.value
               for c in board.caption)


def test_selection_keyed_by_instance_survives_reorder(tmp_path, box):
    project, at = screen(box)
    sel(at, "sb_instance").select("I002").run()
    assert not at.exception
    _reorder_first_two(project)
    at.run()                                   # reload the project bytes
    assert not at.exception
    assert sel(at, "sb_instance").value == "I002"
    board = tab(at)
    body = " ".join(m.value for m in board.markdown)
    # The detail block still shows S002, now at position 0 — same cut.
    assert "I002 · S002" in body and "[0, 24)" in body


def test_jump_to_display_frame_picks_the_covering_cut(tmp_path, box):
    project, at = screen(box)
    number(at, "sb_jump").set_value(30)
    button(at, "sb_jump_button").click().run()
    assert not at.exception
    assert sel(at, "sb_instance").value == "I002"


def test_jump_into_overlap_picks_the_compose_owner(tmp_path, box):
    project, at = screen(box)
    draft = propose_edit(project, "I001", transition={
        "type": "CROSSFADE", "overlap_frames": 4, "fund": "OUTGOING"})
    assert draft["valid"], draft["errors"]
    apply_edit(project, draft)
    at.run()                                # reload the project bytes
    # 표시 26 → frame 25 ∈ [24, 28): covered by both cuts, composed by I002.
    number(at, "sb_jump").set_value(26)
    button(at, "sb_jump_button").click().run()
    assert not at.exception
    assert sel(at, "sb_instance").value == "I002"


def test_edit_inputs_do_not_leak_across_cut_selection(tmp_path, box):
    project, at = screen(box)
    number(at, "sb_shift_I001").set_value(4)
    at.run()
    sel(at, "sb_instance").select("I002").run()
    assert not at.exception
    # Edit widgets key on the instance — I001's leftover 4 cannot reach I002.
    assert number(at, "sb_shift_I002").value == 0
    assert not [n for n in at.number_input if n.key == "sb_shift_I001"]
    button(at, "sb_propose").click().run()
    assert not at.exception
    assert any("지정하세요" in e.value for e in tab(at).error)


def test_crossfade_boundary_fixture_shows_ownership(tmp_path, box):
    projects = box / "projects"
    projects.mkdir()
    project = fixture_project(projects, seconds=3, shot_count=3)
    animation_init(project)
    for index in range(3):
        import_frame_sequence(
            project, f"S{index + 1:03d}",
            folder=make_sequence(box / f"seq{index}",
                                 30 if index == 0 else 24,
                                 seed=index * 100))
    draft = propose_edit(project, "I001", transition={
        "type": "CROSSFADE", "overlap_frames": 4, "fund": "OUTGOING"})
    apply_edit(project, draft)
    at = run_panel(project)
    board = tab(at)
    tables = " ".join(d.value.to_string() for d in board.dataframe)
    assert "CROSSFADE" in tables or "크로스페이드" in tables
    assert "T001" in tables and "×4/5" in tables and "×1/5" in tables
    # Record owner vs compose-op owner are both named, honestly.
    assert "기록 소유" in " ".join(m.value for m in board.markdown)


def test_draft_compare_then_apply_through_the_panel(tmp_path, box):
    project, at = screen(box)
    number(at, "sb_shift_I001").set_value(4)
    button(at, "sb_propose").click().run()
    assert not at.exception
    board = tab(at)
    tables = " ".join(d.value.to_string() for d in board.dataframe)
    assert "[0, 24]" in tables and "[0, 28]" in tables   # adopted vs draft
    assert "[4, 24]" in tables
    button(at, "sb_apply").click().run()
    assert not at.exception
    board = tab(at)
    assert any("적용됨" in s.value for s in board.success)
    document = read_canon(project / "timeline/edit.json")
    assert document["entries"][0]["used_source_range"] == [0, 28]


def test_invalid_draft_is_an_error_not_an_apply(tmp_path, box):
    project, at = screen(box)
    number(at, "sb_shift_I001").set_value(24)
    button(at, "sb_propose").click().run()
    assert not at.exception
    board = tab(at)
    assert any("유효하지 않은 초안" in c.value for c in board.caption)
    assert list(board.error)
    assert next(b for b in board.button
                if b.key == "sb_apply").disabled


def test_unresolved_cut_reports_missing_sequence(tmp_path, box):
    project, at = screen(box, imports=0)
    board = tab(at)
    assert any("해결되지 않은 컷" in w.value for w in board.warning)


def test_broken_timeline_shows_error_state(tmp_path, box):
    projects = box / "projects"
    projects.mkdir()
    project = fixture_project(projects, seconds=2, shot_count=2)
    animation_init(project)
    (project / "timeline/edit.json").write_text("{not canon")
    at = run_panel(project)
    assert not at.exception
    assert list(tab(at).error)


def test_legacy_mv_has_no_shot_board_tab(tmp_path, box):
    project, at = screen(box, animate=False)
    assert not any(t.label == TAB for t in at.tabs)
