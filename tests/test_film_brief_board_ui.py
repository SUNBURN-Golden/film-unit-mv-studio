"""film-brief-board: the 제작 준비 screen in the real control panel.

Driven headlessly with streamlit.testing.v1.AppTest against
app/control_panel.py on a temp FILM_UNIT_PROJECTS dir. Synthetic fixtures
only — no real artwork approval, service qualification or paid call. Real
browser keyboard checks are recorded by the maintainer; every widget here is
a standard labelled Streamlit control.
"""
from pathlib import Path

import pytest
from PIL import Image
import io

from streamlit.testing.v1 import AppTest

from engine import brief
from engine.audio import synth_test_audio
from engine.core import digest, lock_production, read
from test_compiler_v03 import fixture_project

ROOT = Path(__file__).resolve().parents[1]
NAME = "compiler_fixture"


def png_bytes(color=(40, 90, 140), size=(64, 48)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def button(at, key):
    return next(b for b in at.button if b.key == key)


def uploader(at, key):
    return next(f for f in at.file_uploader if f.key == key)


def ti(at, key):
    return next(t for t in at.text_input if t.key == key)


def ta(at, key):
    return next(t for t in at.text_area if t.key == key)


def errors(at):
    return [e.value for e in at.error]


def successes(at):
    return [s.value for s in at.success]


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FILM_UNIT_PROJECTS", str(tmp_path / "projects"))
    return tmp_path


def app(box):
    return AppTest.from_file(str(ROOT / "app/control_panel.py"),
                             default_timeout=120).run()


@pytest.fixture
def screen(box):
    projects = box / "projects"
    projects.mkdir()
    p = fixture_project(projects, seconds=2, shot_count=2)
    at = app(box)
    at.sidebar.selectbox[0].select(NAME).run()
    assert not at.exception
    return at, p


# --- normal flow -------------------------------------------------------------

def test_board_tab_lists_adopted_inputs_limits_and_slate_state(screen):
    at, p = screen
    assert any(t.label == "01 · 제작 준비" for t in at.tabs)
    assert any("MP3" in c.value and "WAV" in c.value and "240" in c.value
               for c in at.caption)
    assert any("input/master.wav" in m.value and "합성" in m.value
               for m in at.markdown)
    assert any("brief" in m.value for m in at.markdown)
    assert any("가사 원문" in m.value for m in at.markdown)


def test_stage_then_adopt_reference(screen):
    at, p = screen
    uploader(at, "bb_stage_files").set_value(
        [("mood.png", png_bytes(), "image/png")])
    at.run()
    button(at, "bb_stage").click().run()
    assert not at.exception
    assert any("임시 자료로 보관" in s for s in successes(at))
    assert any("`임시`" in m.value and "mood.png" in m.value
               for m in at.markdown)
    button(at, "bb_ref_M0001").click().run()
    assert (p / "input/references/mood.png").is_file()
    board = brief.load_board(p)
    assert board["staged"][0]["state"] == "adopted"
    assert board["adopted"]["references"][0]["sha256"] == \
        digest(p / "input/references/mood.png")
    assert any("`채택됨`" in m.value for m in at.markdown)


def test_brief_emotion_and_lyrics_adoption_from_the_board(screen):
    at, p = screen
    ta(at, "bb_brief_text").set_value("A rewritten brief for the film")
    button(at, "bb_adopt_brief").click().run()
    assert (p / "input/brief.md").read_text() == "A rewritten brief for the film"
    ti(at, "bb_emotion").set_value("잔잔하고 쓸쓸한")
    button(at, "bb_adopt_emotion").click().run()
    assert read(p / "project.yaml")["direction"]["emotion"] == "잔잔하고 쓸쓸한"
    ta(at, "bb_lyrics_text").set_value("첫째 줄\n둘째 줄\n")
    button(at, "bb_adopt_lyrics").click().run()
    assert not at.exception
    assert (p / "input/lyrics.txt").read_text() == "첫째 줄\n둘째 줄\n"
    document = read(p / "lyrics/lyrics_timed.json")
    assert document["cues"] == []
    assert len(document["unresolved_row_ids"]) == 2


def test_lyrics_adoption_never_invents_timing_and_shows_impact(box):
    projects = box / "projects"
    projects.mkdir()
    p = fixture_project(projects, seconds=2, shot_count=1)
    lock_production(p, "Director")
    at = app(box)
    at.sidebar.selectbox[0].select(NAME).run()
    ta(at, "bb_lyrics_text").set_value("새 원문 첫 행\n새 원문 둘째 행\n")
    button(at, "bb_adopt_lyrics").click().run()
    assert not at.exception
    document = read(p / "lyrics/lyrics_timed.json")
    # No vocal timing is derived from lyric length: rows stay untimed.
    assert document["cues"] == []
    assert all("start_ms" not in row for row in document["rows"])
    assert list((p / "lyrics/history").glob("timing_*.json"))
    # The impact display names what became stale and needs re-review.
    assert any("재검수 필요" in w.value and "production LOCK" in w.value
               for w in at.warning)
    assert any("재검수 필요" in w.value and "lyric" in w.value
               for w in at.warning)
    metric = next(m for m in at.metric if m.label == "제작 LOCK")
    assert metric.value == "재검수 필요"
    # The LOCK record itself is preserved, not rewritten.
    assert read(p / "manifest/locks.json")["reviewer"] == "Director"


def test_memo_is_staged_as_temporary(screen):
    at, p = screen
    ta(at, "bb_memo").set_value("참고: 분위기 링크 https://example.invalid/x")
    button(at, "bb_memo_add").click().run()
    assert not at.exception
    record = brief.load_board(p)["staged"][0]
    assert record["kind"] == "note" and record["state"] == "temporary"
    assert (p / record["path"]).read_text().startswith("참고")


# --- error and empty states --------------------------------------------------

def test_new_project_form_success_then_name_collision(box):
    at = app(box)                                      # empty projects → form
    wav = synth_test_audio(box / "a.wav", seconds=2).read_bytes()
    next(t for t in at.text_input if t.label == "프로젝트 ID").set_value("ui_new")
    next(f for f in at.file_uploader).set_value(("a.wav", wav, "audio/wav"))
    next(t for t in at.text_area if "어떤 영상" in t.label).set_value("UI brief")
    next(t for t in at.text_input if "감정" in t.label).set_value("차분함")
    next(b for b in at.button if b.label == "프로젝트 만들기").click().run()
    assert not at.exception
    assert any("생성됐습니다" in s for s in successes(at))
    p = box / "projects" / "ui_new"
    assert (p / "project.yaml").is_file()
    board = brief.load_board(p)
    assert board["adopted"]["audio"]["sha256"] == digest(p / "input/master.wav")
    assert read(p / "project.yaml")["direction"]["emotion"] == "차분함"
    package = (p / "input/brief.md").read_bytes()
    # The same name cannot overwrite the existing project. (run once more so
    # the sidebar rescans the projects dir after the in-run creation)
    at.run()
    at.sidebar.selectbox[0].select("새 프로젝트").run()
    next(t for t in at.text_input if t.label == "프로젝트 ID").set_value("ui_new")
    next(f for f in at.file_uploader).set_value(("a.wav", wav, "audio/wav"))
    next(t for t in at.text_area if "어떤 영상" in t.label).set_value("UI brief")
    next(b for b in at.button if b.label == "프로젝트 만들기").click().run()
    assert any("Project exists" in e for e in errors(at))
    assert (p / "input/brief.md").read_bytes() == package


def test_empty_form_shows_a_message(box):
    at = app(box)
    next(b for b in at.button if b.label == "프로젝트 만들기").click().run()
    assert not at.exception
    assert any("음원과 짧은 설명" in e for e in errors(at))
    assert not (box / "projects" / "project_001").exists()


def test_name_collision_never_overwrites_an_existing_package(box):
    projects = box / "projects"
    projects.mkdir()
    p = fixture_project(projects, seconds=2, shot_count=1)
    manifest = (p / "manifest/shots.json").read_bytes()
    wav = synth_test_audio(box / "b.wav", seconds=2).read_bytes()
    at = app(box)
    at.sidebar.selectbox[0].select("새 프로젝트").run()
    next(t for t in at.text_input if t.label == "프로젝트 ID").set_value(NAME)
    next(f for f in at.file_uploader).set_value(("b.wav", wav, "audio/wav"))
    next(t for t in at.text_area if "어떤 영상" in t.label).set_value("collision")
    next(b for b in at.button if b.label == "프로젝트 만들기").click().run()
    assert any("Project exists" in e for e in errors(at))
    assert (p / "manifest/shots.json").read_bytes() == manifest


def test_stage_duplicate_import_shows_a_message(screen):
    at, _ = screen
    uploader(at, "bb_stage_files").set_value(
        [("one.txt", b"same payload", "text/plain")])
    at.run()
    button(at, "bb_stage").click().run()
    assert any("임시 자료로 보관" in s for s in successes(at))
    button(at, "bb_stage").click().run()    # uploader still holds the file
    assert any("Duplicate" in e for e in errors(at))


def test_unsupported_and_corrupt_audio_adoption_is_refused(screen):
    at, p = screen
    uploader(at, "bb_stage_files").set_value(
        [("track.flac", b"not really flac", "audio/flac")])
    at.run()
    button(at, "bb_stage").click().run()
    button(at, "bb_audio_M0001").click().run()
    assert any("MP3 or WAV" in e for e in errors(at))
    uploader(at, "bb_stage_files").set_value(
        [("broken.mp3", b"garbage bytes", "audio/mpeg")])
    at.run()
    button(at, "bb_stage").click().run()
    button(at, "bb_audio_M0002").click().run()
    assert any("Cannot read" in e for e in errors(at))
    # A different song is refused once the measured timeline exists.
    other = synth_test_audio(p.parents[0] / "other.wav", seconds=3).read_bytes()
    uploader(at, "bb_stage_files").set_value(
        [("other.wav", other, "audio/wav")])
    at.run()
    button(at, "bb_stage").click().run()
    master = (p / "input/master.wav").read_bytes()
    button(at, "bb_audio_M0003").click().run()
    assert any("new project" in e for e in errors(at))
    assert (p / "input/master.wav").read_bytes() == master


def test_missing_master_is_shown_on_the_board(box):
    projects = box / "projects"
    projects.mkdir()
    audio = synth_test_audio(box / "lost.wav", seconds=2)
    p = brief.create_project(projects, "lost_song", audio, "brief", "la\n")
    (p / "input/master.wav").unlink()
    at = app(box)
    at.sidebar.selectbox[0].select("lost_song").run()
    assert not at.exception
    assert any("음원이 없습니다" in e for e in errors(at))


def test_adopting_identical_text_shows_duplicate(screen):
    at, p = screen
    current = (p / "input/lyrics.txt").read_text(encoding="utf-8")
    ta(at, "bb_lyrics_text").set_value(current)
    button(at, "bb_adopt_lyrics").click().run()
    assert any("Duplicate" in e for e in errors(at))
    ta(at, "bb_brief_text").set_value("   ")
    button(at, "bb_adopt_brief").click().run()
    assert any("Brief text is required" in e for e in errors(at))
