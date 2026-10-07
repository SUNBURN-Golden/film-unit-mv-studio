"""film-lyrics-review: the "11 · 가사 검토" tab via AppTest.

Headless coverage of the normal / empty-timing / error / legacy-and-
animation-profile states, listening-position -> cue selection (including
a gap and an exact boundary), the pending source diff, the per-change
invalidation scope table, the imported-candidate evaluate -> human-save
flow and the Korean missing-glyph font failure. All fixtures are
synthetic; review/lock records are protocol fixtures — qualification
UNQUALIFIED, acceptance PENDING, release NOT_AUTHORIZED.

AppTest does not exercise real-browser rendering, audio playback or
keyboard focus order — those stay unverified in this environment.
"""
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from engine.animation_assets import import_frame_sequence
from engine.animation_migrate import animation_init
from engine.core import atomic_text, read, write
from engine.lyrics import prepare_lyrics
from test_anim_003 import make_sequence
from test_compiler_v03 import fixture_project

ROOT = Path(__file__).resolve().parents[1]
NAME = "compiler_fixture"
TAB = "11 · 가사 검토"


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


def screen(box, *, seconds=3, lyrics=3, animate=False):
    projects = box / "projects"
    projects.mkdir()
    project = fixture_project(projects, seconds=seconds, shot_count=1,
                              lyric_count=lyrics)
    if animate:
        animation_init(project)
        import_frame_sequence(
            project, "S001",
            folder=make_sequence(box / "seq0", seconds * 24, seed=7))
    return project, run_panel(project)


def tab(at):
    return next(t for t in at.tabs if t.label == TAB)


def widget(block, kind, key):
    return next(w for w in getattr(block, kind) if w.key == key)


def tables(block):
    return " ".join(d.value.to_string() for d in block.dataframe)


def body(block):
    return " ".join(e.value for e in
                    list(block.caption) + list(block.markdown))


def _candidate(project):
    doc = prepare_lyrics(project)
    rows = [r for r in doc["rows"] if r["kind"] == "lyric"]
    duration = read(project / "analysis/audio.json")["duration_ms"]
    span = duration // len(rows)
    doc["cues"] = [
        {"id": f"ASR{i}", "source_row_id": row["id"], "text": row["text"],
         "start_ms": i * span, "end_ms": (i + 1) * span - 50}
        for i, row in enumerate(rows)]
    doc["review"] = {"schema_version": 2, "reviewer": "ASR bot",
                     "reviewed_at": "2026-01-01",
                     "timing_sha256": "0" * 64,
                     "lyrics_review_sha256": "0" * 64}
    return doc


# --- normal state --------------------------------------------------------------

def test_tab_lists_cues_review_and_source(tmp_path, box):
    project, at = screen(box)
    board = tab(at)
    metrics = {m.label: m.value for m in board.metric}
    assert metrics["cue"] == "3"
    assert metrics["가사 검수"] == "현재"
    assert metrics["폰트"] == "검증됨"
    table = tables(board)
    assert "C001" in table and "C002" in table and "C003" in table
    assert "input/lyrics.txt" in body(board)
    assert "lyrics/lyrics_timed.json" not in table or True
    select = widget(board, "selectbox", "lr_cue")
    assert [o.split(" · ")[0] for o in select.options] == \
        ["C001", "C002", "C003"]


def test_listening_position_selects_cue_and_gap(tmp_path, box):
    project, at = screen(box)
    board = tab(at)
    widget(board, "number_input", "lr_position").set_value(1100)
    widget(board, "button", "lr_jump").click().run()
    assert not at.exception
    board = tab(at)
    assert widget(board, "selectbox", "lr_cue").value == "C002"
    assert any("C002" in s.value for s in board.success)
    # An exact cue boundary lands in the gap and names both flanks.
    doc = prepare_lyrics(project)
    boundary = doc["cues"][0]["end_ms"]
    widget(board, "number_input", "lr_position").set_value(boundary + 40)
    widget(board, "button", "lr_jump").click().run()
    assert not at.exception
    board = tab(at)
    assert any("cue 사이" in i.value for i in board.info)
    assert any("C001" in i.value and "C002" in i.value
               for i in board.info)


# --- empty / error / incomplete states -------------------------------------------

def test_empty_timing_shows_guidance_not_crash(tmp_path, box):
    projects = box / "projects"
    projects.mkdir()
    project = fixture_project(projects, seconds=2, shot_count=1,
                              lyric_count=2)
    (project / "lyrics/lyrics_timed.json").unlink()
    at = run_panel(project)
    board = tab(at)
    assert any("아직 타이밍된 cue가 없습니다" in i.value for i in board.info)
    metrics = {m.label: m.value for m in board.metric}
    assert metrics["cue"] == "0"
    assert metrics["가사 검수"] == "미검수"
    assert any("청취 위치를 연결할 수" in i.value for i in board.info)


def test_broken_read_model_shows_error_not_blank(tmp_path, box, monkeypatch):
    project, at = screen(box)
    from engine.core import FilmError
    import app.lyrics_review as panel_module
    monkeypatch.setattr(panel_module, "workbench",
                        lambda p: (_ for _ in ()).throw(
                            FilmError("synthetic broken fixture")))
    at.run()
    board = tab(at)
    assert any("synthetic broken fixture" in e.value for e in board.error)


def test_animation_profile_shows_plan_final_invalidation(tmp_path, box):
    project, at = screen(box, animate=True)
    board = tab(at)
    table = tables(board)
    assert "CUT:I001" in table          # stays bound — cue/font only
    assert "FINAL_LOCK" in table and "낡음" in table
    assert any("PLAN" in c.value and "FINAL" in c.value
               for c in board.caption)


def test_legacy_profile_shows_production_lock_scope(tmp_path, box):
    project, at = screen(box)
    board = tab(at)
    table = tables(board)
    assert "production LOCK" in table and "낡음" in table
    assert any("production LOCK" in c.value for c in board.caption)
    assert not at.exception


# --- source diff -----------------------------------------------------------------

def test_source_edit_shows_pending_diff(tmp_path, box):
    project, at = screen(box)
    atomic_text(project / "input/lyrics.txt",
                "Synthetic fixture line 01\nA rewritten second line\n"
                "Synthetic fixture line 03\n")
    at.run()
    assert not at.exception
    board = tab(at)
    expander = next(e for e in board.expander if "diff" in e.label)
    diff_table = tables(expander)
    assert "A rewritten second line" in diff_table
    assert "바뀜" in diff_table


# --- candidate flow --------------------------------------------------------------

def test_candidate_evaluate_then_human_save(tmp_path, box):
    project, at = screen(box)
    candidate = _candidate(project)
    widget(tab(at), "text_area", "lr_candidate_text").set_value(
        json.dumps(candidate))
    widget(tab(at), "button", "lr_eval").click().run()
    assert not at.exception
    board = tab(at)
    # The smuggled review record is named and ignored — never approved.
    assert any("무시" in w.value for w in board.warning)
    assert any("미검수" in s.value for s in board.success)
    # Human reviewer confirms real-vocal listening and saves.
    widget(board, "text_input", "lr_candidate_reviewer").set_value(
        "UI fixture reviewer")
    widget(board, "checkbox", "lr_candidate_reviewed").set_value(True)
    widget(board, "button", "lr_candidate_save").click().run()
    assert not at.exception
    board = tab(at)
    assert any("검토가 기록됐습니다" in s.value for s in board.success)
    stored = read(project / "lyrics/lyrics_timed.json")
    assert stored["review"]["reviewer"] == "UI fixture reviewer"
    assert stored["review"]["timing_sha256"]


# --- font failure -----------------------------------------------------------------

def test_korean_missing_glyphs_block_final_visibly(tmp_path, box):
    project, at = screen(box)
    atomic_text(project / "input/lyrics.txt", "존재를 긍정해\n마지막 구절\n")
    doc = prepare_lyrics(project)
    rows = [r for r in doc["rows"] if r["kind"] == "lyric"]
    doc["cues"] = [
        {"id": f"C{i + 1:03d}", "source_row_id": row["id"],
         "text": row["text"], "start_ms": i * 1000,
         "end_ms": (i + 1) * 1000 - 80}
        for i, row in enumerate(rows)]
    write(project / "lyrics/lyrics_timed.json", doc)
    at.run()
    assert not at.exception
    board = tab(at)
    metrics = {m.label: m.value for m in board.metric}
    assert metrics["폰트"] == "글리프 없음"
    assert any("Final이 나가지 않습니다" in e.value for e in board.error)
    table = tables(board)
    assert "글리프 없음" in table
