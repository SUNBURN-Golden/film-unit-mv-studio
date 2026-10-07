"""film-quality-diagnostics results screen in the real control panel.

Driven headlessly with streamlit.testing.v1.AppTest against
app/control_panel.py on a converted synthetic project (Pillow PNGs, sine
master — no real artwork, no network, no paid call). The panel runs the
diagnose_build measurement, shows findings with locations and reproduction
commands, and records a user classification with a required reason — all
under `not_an_approval`: this screen never feeds Final approval, an
aesthetic score, or the LOCK/review binding.

Browser note: AppTest exercises the real widgets (buttons, selectboxes,
radios, text inputs are all keyboard-focusable Streamlit controls), but a
real-browser visual/keyboard pass is NOT claimed here — it is recorded as
unverified in the node handoff.
"""
import shutil
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_compiler_v03 import fixture_project  # noqa: E402

from engine.animation_migrate import animation_init  # noqa: E402
from engine.builds import list_builds  # noqa: E402
from engine.core import digest, ffmpeg, read, write  # noqa: E402
from engine.quality_diagnostics import load_report  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NAME = "compiler_fixture"
REVIEWER = "UI diagnostics reviewer"
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                reason="ffmpeg is not installed")


def button(at, key):
    return next(b for b in at.button if b.key == key)


def sel(at, key):
    return next(s for s in at.selectbox if s.key == key)


def ti(at, key):
    return next(t for t in at.text_input if t.key == key)


def radio(at, key):
    return next(r for r in at.radio if r.key == key)


def errors(at):
    return [e.value for e in at.error]


def successes(at):
    return [s.value for s in at.success]


def _silent_master(project, seconds=2):
    """Re-author the synthetic master with a declared-fixture silent
    middle (0.7 s–1.3 s) and rebind the recorded digests — the fixture's
    own master, not tampering."""
    p = Path(project)
    config = read(p / "project.yaml")
    master = p / config["audio"]["path"]
    ffmpeg(["-f", "lavfi", "-i",
            f"sine=frequency=220:sample_rate=16000:duration={seconds},"
            "volume='if(between(t,0.7,1.3),0,1)':eval=frame",
            "-c:a", "pcm_s16le", master])
    config["audio"]["sha256"] = digest(master)
    write(p / "project.yaml", config)
    analysis = read(p / "analysis/audio.json")
    analysis["master_sha256"] = digest(master)
    write(p / "analysis/audio.json", analysis)


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FILM_UNIT_PROJECTS", str(tmp_path / "projects"))
    return tmp_path


@pytest.fixture
def screen(box):
    projects = box / "projects"
    projects.mkdir()
    project = fixture_project(projects, seconds=2, shot_count=1)
    _silent_master(project)
    animation_init(project)
    at = AppTest.from_file(str(ROOT / "app/control_panel.py"),
                           default_timeout=240).run()
    at.sidebar.selectbox[0].select(NAME).run()
    assert not at.exception
    return at, project


def _draft_build(at):
    """Produce a sealed draft-preview build (DRAFT_PREVIEW.mp4)."""
    button(at, "an_preview").click().run()
    assert not at.exception
    assert any("DRAFT_PREVIEW" in s for s in successes(at))


def _run_diagnostics(at, build_id):
    button(at, f"qd_run_{build_id}").click().run()
    assert not at.exception


def test_diagnostics_empty_state_then_run_and_classify(screen):
    at, project = screen
    _draft_build(at)
    build = list_builds(project)[0]
    build_id = build["build_id"]

    # Empty state: no report yet, and the screen says diagnostics are not
    # an approval.
    sel(at, "an_final_build").select(build).run()
    assert not at.exception
    assert any("아직 없습니다" in c.value for c in at.caption)
    assert any("not_an_approval" in c.value for c in at.caption)

    # Run: the master's own silent middle is an undeclared SILENCE_RUN
    # candidate with a millisecond location and a reproduction command.
    _run_diagnostics(at, build_id)
    report = load_report(project, build_id)
    assert report is not None and report["not_an_approval"] is True
    silence = [f for f in report["findings"]
               if f["kind"] == "SILENCE_RUN"
               and f["severity"] == "CANDIDATE"]
    assert silence, "the master's silent middle must measure as a " \
                    "candidate silence run"
    assert all(f["repro"].startswith("ffmpeg") for f in silence)
    assert any(f["location"]["start_ms"] for f in silence)
    assert any("진단 완료" in s for s in successes(at))
    # Findings render as expanders labelled with severity and kind.
    assert any("SILENCE_RUN" in e.label and "CANDIDATE" in e.label
               for e in at.expander)

    # Classification controls are keyboard-focusable widgets and require
    # a reason before the record button enables.
    pick = sel(at, f"qd_pick_{build_id}")
    assert f"qd_decision_{build_id}" in [r.key for r in at.radio]
    assert f"qd_reason_{build_id}" in [t.key for t in at.text_input]
    assert f"qd_reviewer_{build_id}" in [t.key for t in at.text_input]
    assert button(at, f"qd_classify_{build_id}").disabled

    pick.set_value(next(k for k in pick.options
                        if silence[0]["id"] in k)).run()
    radio(at, f"qd_decision_{build_id}").set_value(
        "ACCEPTED_INTENDED").run()
    ti(at, f"qd_reason_{build_id}").set_value(
        "fixture: the quiet bar is an intentional dynamic dip")
    ti(at, f"qd_reviewer_{build_id}").set_value(REVIEWER)
    at.run()  # re-render so the reason/reviewer enable the record button
    button(at, f"qd_classify_{build_id}").click().run()
    assert not at.exception
    assert any("ACCEPTED_INTENDED" in s and "기록" in s
               for s in successes(at))
    stored = load_report(project, build_id)
    target = next(f for f in stored["findings"]
                  if f["id"] == silence[0]["id"])
    assert target["classification"]["decision"] == "ACCEPTED_INTENDED"
    assert target["classification"]["reviewer"] == REVIEWER
    assert target["classification"]["reason"]


def test_diagnostics_error_state_and_defect_cannot_be_excused(screen):
    at, project = screen
    _draft_build(at)
    build = list_builds(project)[0]
    build_id = build["build_id"]
    sel(at, "an_final_build").select(build).run()

    # Tamper with the sealed artifact, then diagnose: a spec violation.
    target = Path(build["build_dir"]) / "DRAFT_PREVIEW.mp4"
    target.write_bytes(target.read_bytes() + b"tamper")
    _run_diagnostics(at, build_id)
    report = load_report(project, build_id)
    defect = next(f for f in report["findings"]
                  if f["kind"] == "ARTIFACT_HASH_MISMATCH")

    # Error state: a malformed intended declaration is refused visibly.
    ti(at, f"qd_intended_{build_id}").set_value("BLUR:0-500")
    _run_diagnostics(at, build_id)
    assert any("KIND:STARTMS-ENDMS" in e or "intended" in e.lower()
               for e in errors(at))
    ti(at, f"qd_intended_{build_id}").set_value("").run()

    # A DEFECT cannot be excused as ACCEPTED_INTENDED — the refusal is
    # shown, not silent.
    pick = sel(at, f"qd_pick_{build_id}")
    pick.set_value(next(k for k in pick.options
                        if defect["id"] in k)).run()
    radio(at, f"qd_decision_{build_id}").set_value(
        "ACCEPTED_INTENDED").run()
    ti(at, f"qd_reason_{build_id}").set_value(
        "fixture: attempt to excuse a sealed-artifact change")
    ti(at, f"qd_reviewer_{build_id}").set_value(REVIEWER)
    at.run()
    button(at, f"qd_classify_{build_id}").click().run()
    assert not at.exception
    assert any("면책" in e or "ACCEPTED_INTENDED" in e for e in errors(at))
    stored = load_report(project, build_id)
    assert next(f for f in stored["findings"]
                if f["id"] == defect["id"])["classification"] is None

    # Recording the same violation as DEFECT works and shows the saved
    # judgement on the finding.
    radio(at, f"qd_decision_{build_id}").set_value("DEFECT").run()
    button(at, f"qd_classify_{build_id}").click().run()
    assert not at.exception
    assert any("기록" in s for s in successes(at))
    stored = load_report(project, build_id)
    assert next(f for f in stored["findings"]
                if f["id"] == defect["id"])["classification"]["decision"] \
        == "DEFECT"
