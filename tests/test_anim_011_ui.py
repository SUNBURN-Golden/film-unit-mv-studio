"""ANIM-011: the FRAME_ANIMATION_V1 one-cut flow in the real control panel.

Driven headlessly with streamlit.testing.v1.AppTest against
app/control_panel.py, on a temp FILM_UNIT_PROJECTS dir holding a converted
synthetic project (Pillow PNGs, sine master). Every import stays DRAFT and
every lock/review/decision written here is a protocol fixture exercising the
engine boundary through the UI — no real artwork approval, qualification,
paid call or release. See docs/ANIM_011_UI_FLOW.md.
"""
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from engine.animation_migrate import animation_init
from engine.animation_review import film_review_status, review_status
from engine.animation_locks import lock_status, route_status
from engine.builds import list_builds
from test_compiler_v03 import fixture_project

ROOT = Path(__file__).resolve().parents[1]
NAME = "compiler_fixture"
REVIEWER = "UI fixture reviewer"
APPROVER = "UI fixture approver"
LEAD = "UI fixture production lead"
FILM_REVIEWER = "UI fixture film reviewer"
FRAMES = 24  # one second per shot at 24fps


def png(seed=0, size=(64, 48)):
    """Small deterministic PNG; distinct seeds give distinct bytes."""
    im = Image.new("RGBA", size)
    px = im.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = ((x * 5 + seed * 29) % 256,
                        (y * 7 + seed * 11) % 256,
                        (x + y + seed * 3) % 256, 255)
    buffer = io.BytesIO()
    im.save(buffer, "PNG")
    return buffer.getvalue()


def seq_files(count=FRAMES, seed=0, size=(64, 48)):
    return [(f"f{i:04d}.png", png(seed + i, size), "image/png")
            for i in range(count)]


def button(at, key):
    return next(b for b in at.button if b.key == key)


def uploader(at, key):
    return next(f for f in at.file_uploader if f.key == key)


def sel(at, key):
    return next(s for s in at.selectbox if s.key == key)


def ti(at, key):
    return next(t for t in at.text_input if t.key == key)


def multi(at, key):
    return next(m for m in at.multiselect if m.key == key)


def errors(at):
    return [e.value for e in at.error]


def successes(at):
    return [s.value for s in at.success]


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FILM_UNIT_PROJECTS", str(tmp_path / "projects"))
    return tmp_path


@pytest.fixture
def screen(box):
    projects = box / "projects"
    projects.mkdir()
    project = fixture_project(projects, seconds=3, shot_count=3)
    animation_init(project)
    at = AppTest.from_file(str(ROOT / "app/control_panel.py"),
                           default_timeout=180).run()
    at.sidebar.selectbox[0].select(NAME).run()
    assert not at.exception
    return at, project


# --- UI drivers --------------------------------------------------------------


def declare_waves(at):
    multi(at, "an_w00_shots").select("S002")          # W00 = S001 + S002
    ti(at, "an_w00_types").set_value("FACE_TURN")
    ti(at, "an_w00_reason").set_value("fixture: the hard cut type W00 verifies")
    button(at, "an_waves_declare").click().run()


def plan_lock(at):
    ti(at, "an_lock_approver").set_value(APPROVER)
    button(at, "an_lock_plan").click().run()


def wave_lock(at, wave):
    sel(at, "an_lock_wave_pick").select(wave)
    ti(at, "an_lock_approver").set_value(APPROVER)
    button(at, "an_lock_wave").click().run()


def import_sequence(at, instance, seed, count=FRAMES):
    sel(at, "an_shot").select(instance)
    uploader(at, "an_seq_files").set_value(seq_files(count, seed))
    at.run()                                   # the upload enables the button
    button(at, "an_seq_import").click().run()


def cut_review(at, instance, key="an_cut_ok"):
    sel(at, "an_shot").select(instance)
    ti(at, "an_reviewer").set_value(REVIEWER)
    button(at, key).click().run()


def transition_review(at, transition_id):
    sel(at, "an_tr_pick").select(transition_id)
    ti(at, "an_reviewer").set_value(REVIEWER)
    button(at, "an_tr_ok").click().run()


def route_decision(at, decision="keep"):
    next(r for r in at.radio if r.key == "an_rd_decision").set_value(decision)
    ti(at, "an_rd_decider").set_value(LEAD)
    ti(at, "an_rd_approver").set_value(APPROVER)
    ti(at, "an_rd_conditions").set_value("fixture: both W00 cuts played")
    ti(at, "an_rd_observations").set_value("fixture: no route limit")
    button(at, "an_rd_record").click().run()


def adopted_w00(at):
    """Declare waves, PLAN_LOCK, import+lock W00, adopt its two cuts."""
    declare_waves(at)
    assert any("제작 순서" in s for s in successes(at))
    plan_lock(at)
    import_sequence(at, "I001", seed=10)
    import_sequence(at, "I002", seed=20)
    wave_lock(at, "W00")
    cut_review(at, "I001")
    cut_review(at, "I002")


def produced_project(at):
    """The whole film produced: W01 imported/locked/reviewed, T001/T002 too."""
    adopted_w00(at)
    route_decision(at)
    assert any("경로 결정" in s for s in successes(at))
    import_sequence(at, "I003", seed=30)
    wave_lock(at, "W01")
    cut_review(at, "I003")
    transition_review(at, "T001")
    transition_review(at, "T002")


# --- the success flow --------------------------------------------------------


def test_one_cut_flow_to_final_approval(screen):
    at, project = screen
    # 준비: the total-frame check immediately names the unresolved cuts.
    button(at, "an_validate").click().run()
    assert not at.exception
    assert any("해결되지 않는 컷" in e and "I001" in e for e in errors(at))

    adopted_w00(at)

    # ANIM-012 wired the path-B section to the dev/test fake adapter; the
    # ANIM-011 placeholder is gone and the live section is labelled.
    assert not any(b.key == "an_path_b" for b in at.button)
    assert any("경로 B" in m.value for m in at.markdown)
    assert any("fake adapter" in c.value and "UNQUALIFIED" in c.value
               for c in at.caption)

    # 검토: whole-film draft preview plays in the app.
    button(at, "an_preview").click().run()
    assert not at.exception
    assert any("DRAFT_PREVIEW" in s for s in successes(at))
    preview = list_builds(project)[0]
    assert preview["document_type"] == "animation_draft_preview"

    # 검사: after S003's import the check passes for every cut.
    route_decision(at)
    import_sequence(at, "I003", seed=30)
    button(at, "an_validate").click().run()
    assert any("총 프레임이 일치" in s for s in successes(at))
    wave_lock(at, "W01")
    cut_review(at, "I003")
    transition_review(at, "T001")
    transition_review(at, "T002")
    assert all(r["state"] == "CURRENT"
               for r in review_status(project)["targets"].values())

    # 출력·승인: FINAL_LOCK, sealed candidate, exact-build approval.
    button(at, "an_lock_final").click().run()
    assert lock_status(project)["final"]["state"] == "CURRENT"
    button(at, "an_final_make").click().run()
    assert not at.exception
    assert any("FINAL_CANDIDATE" in s for s in successes(at))
    candidate = next(b for b in list_builds(project)
                     if b.get("mode") == "FINAL_CANDIDATE")
    sel(at, "an_final_build").select(candidate)
    ti(at, "an_film_reviewer").set_value(FILM_REVIEWER)
    button(at, "an_final_ok").click().run()
    assert not at.exception
    assert any("최종 승인" in s for s in successes(at))
    assert film_review_status(project, candidate["build_id"])["state"] \
        == "CURRENT"
    # The panel keeps reporting honest facets — never a release.
    assert any("UNQUALIFIED" in c.value and "NOT_AUTHORIZED" in c.value
               for c in at.caption)


def test_shot_plan_upload_and_control_image(screen):
    at, project = screen
    sel(at, "an_shot").select("I001")
    plan = {"document_type": "animation_shot_plan", "schema_version": 1,
            "shot_id": "S001", "revision": 1, "motion_intent": "STATIC",
            "story_role": "fixture", "emotion_role": "fixture",
            "canvas": {"width": 320, "height": 240}, "background": [0, 0, 0],
            "rig": None, "assets": [], "events": [],
            "keyposes": [{"kind": "KEYPOSE", "frame": 0, "ref": None}],
            "segments": [{"start": 0, "end": FRAMES, "path": "A",
                          "capabilities": ["REFERENCE_IMAGES"]}],
            "tracks": {"camera": {"pivot": [160, 120], "transform": None},
                       "layers": {}},
            "preparation": {"work": [], "creator": "fixture",
                            "reviewer": None, "revision_scope": "fixture"}}
    uploader(at, "an_plan_file").set_value(
        [("plan.json", json.dumps(plan).encode(), "application/json")])
    at.run()
    button(at, "an_plan_save").click().run()
    assert any("계획을 저장" in s for s in successes(at))
    uploader(at, "an_ctrl_file").set_value(
        [("layout.png", png(3, (320, 240)), "image/png")])
    sel(at, "an_ctrl_role").select("layout")
    at.run()
    button(at, "an_ctrl_import").click().run()
    assert any("DRAFT로 기록" in s for s in successes(at))


# --- the error states --------------------------------------------------------


def test_invalid_plan_is_refused_with_a_message(screen):
    at, _ = screen
    sel(at, "an_shot").select("I001")
    bad = {"document_type": "animation_shot_plan", "schema_version": 1,
           "shot_id": "S001", "revision": 1, "motion_intent": "STATIC",
           "story_role": "fixture", "emotion_role": "fixture",
           "canvas": {"width": 320, "height": 240}, "background": [0, 0, 0],
           "rig": None, "assets": [], "events": [],
           "keyposes": [{"kind": "KEYPOSE", "frame": FRAMES, "ref": None}],
           "segments": [{"start": 0, "end": FRAMES, "path": "A",
                         "capabilities": ["REFERENCE_IMAGES"]}],
           "tracks": {"camera": {"pivot": [160, 120], "transform": None},
                      "layers": {}},
           "preparation": {"work": [], "creator": "fixture",
                           "reviewer": None, "revision_scope": "fixture"}}
    uploader(at, "an_plan_file").set_value(
        [("plan.json", json.dumps(bad).encode(), "application/json")])
    at.run()
    button(at, "an_plan_save").click().run()
    assert not at.exception                       # a message, not a traceback
    assert any("outside" in e for e in errors(at))


def test_frame_count_mismatch_is_named_by_the_check(screen):
    at, _ = screen
    sel(at, "an_shot").select("I001")
    uploader(at, "an_seq_files").set_value(seq_files(10, seed=5))  # 10 < 24
    at.run()
    button(at, "an_seq_import").click().run()
    assert any("DRAFT로 가져왔습니다" in s for s in successes(at))
    button(at, "an_validate").click().run()
    assert any("해결되지 않는 컷" in e and "I001" in e and "exceeds" in e
               for e in errors(at))


def test_missing_asset_reference_is_refused(screen):
    at, _ = screen
    sel(at, "an_shot").select("I001")
    uploader(at, "an_ctrl_file").set_value(
        [("c.png", png(1, (320, 240)), "image/png")])
    sel(at, "an_ctrl_role").select("layout")
    ti(at, "an_ctrl_refs").set_value("A9999:1:" + "0" * 64)
    at.run()
    button(at, "an_ctrl_import").click().run()
    assert not at.exception
    assert any("REFERENCE_UNKNOWN" in e for e in errors(at))


def test_cut_review_refused_outside_a_locked_wave(screen):
    at, _ = screen
    declare_waves(at)
    plan_lock(at)
    import_sequence(at, "I001", seed=10)
    import_sequence(at, "I002", seed=20)
    cut_review(at, "I001")                      # W00 is not locked yet
    assert any("locked wave scope" in e for e in errors(at))


def test_replacement_stales_reviews_and_lock_then_recovers(screen):
    at, project = screen
    adopted_w00(at)
    # 수정: replacing the cut's frames marks its review and wave lock stale.
    sel(at, "an_shot").select("I001")
    uploader(at, "an_fix_files").set_value(seq_files(FRAMES, seed=99))
    at.run()
    button(at, "an_fix_import").click().run()
    assert not at.exception
    assert any("재검수 필요" in w.value and "I001" in w.value
               and "WAVE_LOCK W00" in w.value for w in at.warning)
    assert review_status(project, strict=False)[
        "targets"]["I001"]["state"] == "STALE"
    assert lock_status(project)["waves"]["W00"]["state"] == "STALE"
    # A cut review inside the now-stale scope is refused with a message.
    cut_review(at, "I001")
    assert any("locked wave scope" in e for e in errors(at))
    # Re-lock the wave and re-review the exact new version to recover.
    wave_lock(at, "W00")
    cut_review(at, "I001")
    assert review_status(project, strict=False)[
        "targets"]["I001"]["state"] == "CURRENT"


def test_final_approval_refuses_a_foreign_build(screen):
    at, _ = screen
    button(at, "an_preview").click().run()          # draft preview build only
    assert any("DRAFT_PREVIEW" in s for s in successes(at))
    ti(at, "an_film_reviewer").set_value(FILM_REVIEWER)
    button(at, "an_final_ok").click().run()
    assert not at.exception
    assert any("최종 승인 대상이 아닙니다" in e for e in errors(at))


def test_final_approval_blocked_when_the_build_is_stale(screen):
    at, project = screen
    produced_project(at)
    button(at, "an_lock_final").click().run()
    button(at, "an_final_make").click().run()
    assert any("FINAL_CANDIDATE" in s for s in successes(at))
    # 수정: replacing a cut after the build leaves it bound to an old edit.
    sel(at, "an_shot").select("I001")
    uploader(at, "an_fix_files").set_value(seq_files(FRAMES, seed=42))
    at.run()
    button(at, "an_fix_import").click().run()
    ti(at, "an_film_reviewer").set_value(FILM_REVIEWER)
    button(at, "an_final_ok").click().run()
    assert not at.exception
    assert any("stale" in e or "검수" in e for e in errors(at))


def test_path_b_section_is_live_but_fake_only(screen):
    """ANIM-012 replaced the disabled placeholder: the section renders its
    real controls against fake_segment only, and nothing is submitted."""
    at, project = screen
    sel(at, "an_shot").select("I001")
    at.run()
    assert not at.exception
    assert not any(b.key == "an_path_b" for b in at.button)
    assert any("경로 B" in m.value for m in at.markdown)
    assert any("개발·시험용 fake adapter" in c.value
               and "UNQUALIFIED" in c.value for c in at.caption)
    # No plan saved for S001 yet — the section asks for one, no job exists.
    assert not any(b.key == "an_b_submit" for b in at.button)
    assert not (project / "animation/segment_jobs").exists()


def test_legacy_mv_project_shows_no_animation_section(box):
    projects = box / "projects"
    projects.mkdir()
    fixture_project(projects, seconds=2, shot_count=1)   # stays LEGACY_MV
    at = AppTest.from_file(str(ROOT / "app/control_panel.py"),
                           default_timeout=120).run()
    at.sidebar.selectbox[0].select(NAME).run()
    assert not at.exception
    assert not any(t.label == "08 · ANIMATION" for t in at.tabs)
    assert not any(b.key and b.key.startswith("an_") for b in at.button)
    assert not any("FRAME_ANIMATION_V1" in s.value for s in at.subheader)


def test_upload_names_are_confined_to_a_basename():
    from types import SimpleNamespace
    from app.animation_ui import _upload_name
    for raw, want in [("/etc/passwd", "passwd"), ("../../x.png", "x.png"),
                      ("..\\..\\y.png", "y.png"), ("..", "upload.bin"),
                      ("", "upload.bin"), (".hidden", "upload.bin"),
                      ("cut.png", "cut.png")]:
        assert _upload_name(SimpleNamespace(name=raw)) == want
