"""ANIM-022 UI: the W00 pilot gate and sealed delivery approvals.

Driven headlessly with streamlit.testing.v1.AppTest against
app/control_panel.py on synthetic fixture projects. Every record written
through the UI here is a protocol fixture: SYNTHETIC_FIXTURE reviewers are
declared as such, and nothing claims real artwork approval, qualification or
release (facets stay UNQUALIFIED / PENDING / NOT_AUTHORIZED).

The states these tests start from (waves declared, W00 imported, locked and
adopted; the whole film produced) are the ones test_anim_011_ui drives
through the panel. Here they are prepared once per module through the
engine and copied per test, so each test drives only the W00 gate and
delivery widgets.
"""
import shutil
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from engine.animation_assets import import_frame_sequence
from engine.animation_locks import (declare_waves, record_plan_lock,
                                    record_route_decision, record_wave_lock)
from engine.animation_migrate import animation_init
from engine.animation_review import (record_cut_review,
                                     record_transition_review)
from engine.builds import list_builds
from engine.w00_gate import (deliverable_status, gate_status, load_pilots)
from test_anim_003 import make_sequence
from test_anim_011_ui import (APPROVER, FRAMES, LEAD, NAME, REVIEWER, ROOT,
                              button, errors, route_decision, sel, successes,
                              ti, transition_review)
from test_compiler_v03 import fixture_project


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FILM_UNIT_PROJECTS", str(tmp_path / "projects"))
    return tmp_path


# --- project templates (engine-side, built once per module) ----------------

_TEMPLATE_ROOT = None


@pytest.fixture(scope="module", autouse=True)
def _template_root(tmp_path_factory):
    global _TEMPLATE_ROOT
    _TEMPLATE_ROOT = tmp_path_factory.mktemp("anim022_ui_templates")
    yield
    _TEMPLATE_ROOT = None


def _import(p, root, shot_id, seed):
    folder = make_sequence(root / f"seq_{shot_id.lower()}_{seed}",
                           count=FRAMES, size=(64, 48), seed=seed)
    import_frame_sequence(p, shot_id, folder=folder)


def _adopted_w00(root):
    """What test_anim_011_ui.adopted_w00 reaches through the panel: waves
    declared, PLAN_LOCK, W00 imported and locked, both W00 cuts adopted."""
    (root / "projects").mkdir()
    p = fixture_project(root / "projects", seconds=3, shot_count=3)
    animation_init(p)
    declare_waves(p, [
        {"wave": "W00", "shots": ["S001", "S002"],
         "difficulty": [{"type": "FACE_TURN",
                         "reason": "fixture: the hard cut type W00 verifies"}],
         "note": "fixture"},
        {"wave": "W01", "shots": ["S003"], "difficulty": [],
         "note": "fixture"}])
    record_plan_lock(p, APPROVER)
    _import(p, root, "S001", 10)
    _import(p, root, "S002", 20)
    record_wave_lock(p, "W00", APPROVER)
    for instance in ("I001", "I002"):
        record_cut_review(p, instance, reviewer=REVIEWER,
                          methods=["CUT_FULL_SPEED_PLAYBACK"])
    return p


def _produced(root):
    """What test_anim_011_ui.produced_project reaches: the whole film
    produced — route decided, W01 imported/locked/reviewed, T001/T002 too."""
    p = _adopted_w00(root)
    record_route_decision(
        p, "W00", decision="KEEP", decider=LEAD, approver=APPROVER,
        checked_types=["FACE_TURN"], unchecked_types=[], apply_scope=["W01"],
        reviewed_conditions=["fixture: both W00 cuts played"],
        observations=["fixture: no route limit"])
    _import(p, root, "S003", 30)
    record_wave_lock(p, "W01", APPROVER)
    record_cut_review(p, "I003", reviewer=REVIEWER,
                      methods=["CUT_FULL_SPEED_PLAYBACK"])
    for transition in ("T001", "T002"):
        record_transition_review(p, transition, reviewer=REVIEWER,
                                 methods=["TRANSITION_FULL_SPEED_PLAYBACK"])
    return p


def _screen(box, kind, build):
    """Copy the `kind` template project into this test's projects dir and
    open the control panel on it."""
    root = _TEMPLATE_ROOT / kind
    source = root / "projects" / NAME
    if not source.is_dir():
        root.mkdir()
        build(root)
    project = Path(shutil.copytree(source, box / "projects" / NAME))
    at = AppTest.from_file(str(ROOT / "app/control_panel.py"),
                           default_timeout=180).run()
    at.sidebar.selectbox[0].select(NAME).run()
    assert not at.exception
    return at, project


@pytest.fixture
def adopted_screen(box):
    return _screen(box, "adopted_w00", _adopted_w00)


@pytest.fixture
def produced_screen(box):
    return _screen(box, "produced", _produced)


def pilot_record(at, target, decision="KEEP", reviewer="UI fixture pilot",
                 kind="SYNTHETIC_FIXTURE", reason="fixture: hard target"):
    sel(at, "an_pilot_target").select(target)
    next(r for r in at.radio
         if r.key == "an_pilot_decision").set_value(decision)
    ti(at, "an_pilot_reviewer").set_value(reviewer)
    sel(at, "an_pilot_kind").select(kind)
    ti(at, "an_pilot_reason").set_value(reason)
    button(at, "an_pilot_record").click().run()


def test_w00_gate_lists_pilots_blocks_then_opens(adopted_screen):
    at, project = adopted_screen
    transition_review(at, "T001")
    # Every W00 target is listed; none has a pilot record — the gate is
    # blocked with explicit reasons, and synthetic evidence is labelled.
    assert any("BLOCKED" in w.value for w in at.warning)
    assert any("SYNTHETIC_FIXTURE" in c.value for c in at.caption)
    for target in ("I001", "I002", "T001"):
        pilot_record(at, target)
        assert not at.exception
    assert [r["pilot_id"] for r in load_pilots(project)] \
        == ["WP0001", "WP0002", "WP0003"]
    # Still blocked: the wave route decision is pending.
    gate = gate_status(project)
    assert gate["gate"]["state"] == "BLOCKED"
    assert gate["gate"]["reasons"] == ["the W00 route decision is pending"]
    route_decision(at)
    assert not at.exception
    assert any("게이트 OPEN" in s for s in successes(at))
    gate = gate_status(project)
    assert gate["gate"]["state"] == "OPEN"
    assert gate["open_waves"] == ["W01"]
    assert all(r["synthetic"] for r in gate["pilots"])
    assert gate["facets"] == {"qualification_state": "UNQUALIFIED",
                              "acceptance_state": "PENDING",
                              "release_state": "NOT_AUTHORIZED"}


def test_w00_gate_refuses_an_unauthorized_human(adopted_screen):
    at, project = adopted_screen
    pilot_record(at, "I001", reviewer="unauthorized person", kind="HUMAN")
    assert not at.exception
    assert any("박준태" in e for e in errors(at))
    assert load_pilots(project) == []
    pilot_record(at, "I001")  # declared synthetic evidence is allowed
    assert any("파일럿" in s for s in successes(at))
    record = load_pilots(project)[0]
    assert record["reviewer_kind"] == "SYNTHETIC_FIXTURE"


def test_pilot_issue_disposition_is_recorded_and_listed(adopted_screen):
    at, project = adopted_screen
    # A limitation disposition without reason/scope is refused, not saved.
    sel(at, "an_pilot_issue").select("ACCEPTED_LIMITATION")
    ti(at, "an_pilot_issue_note").set_value("fixture: soft edge at hold")
    pilot_record(at, "I001")
    assert not at.exception
    assert any("reason" in e for e in errors(at))
    assert load_pilots(project) == []
    ti(at, "an_pilot_issue_reason").set_value("fixture: style choice")
    ti(at, "an_pilot_issue_scope").set_value("hold span")
    pilot_record(at, "I001")
    assert not at.exception
    issue = {"disposition": "ACCEPTED_LIMITATION",
             "note": "fixture: soft edge at hold",
             "reason": "fixture: style choice", "scope": "hold span"}
    assert load_pilots(project)[0]["issues"] == [issue]
    assert {"source": "WP0001", "target": "I001", **issue} \
        in gate_status(project)["issues"]
    assert any("fixture: soft edge at hold" in df.value.to_string()
               for df in at.dataframe)


def test_clean_and_subbed_approve_separately_in_ui(produced_screen):
    at, project = produced_screen
    ti(at, "an_lock_approver").set_value(APPROVER)
    button(at, "an_lock_final").click().run()
    button(at, "an_final_make").click().run()
    assert any("FINAL_CANDIDATE" in s for s in successes(at))
    candidate = next(b for b in list_builds(project)
                     if b.get("mode") == "FINAL_CANDIDATE")
    status = deliverable_status(project, candidate["build_id"])
    assert all(r["state"] == "UNREVIEWED"
               for r in status["deliverables"].values())
    # The clean file is approved on its own hash as synthetic evidence.
    sel(at, "an_del_pick").select("MASTER_CLEAN.mp4")
    ti(at, "an_del_approver").set_value("ui fixture approver")
    button(at, "an_del_ok").click().run()
    assert not at.exception
    status = deliverable_status(project, candidate["build_id"])
    assert status["deliverables"]["MASTER_CLEAN.mp4"]["state"] == "CURRENT"
    assert status["deliverables"]["MASTER_CLEAN.mp4"]["reviewer_kind"] \
        == "SYNTHETIC_FIXTURE"
    # The subbed file stays unapproved — one approval never covers both.
    assert status["deliverables"]["MASTER_SUBBED.mp4"]["state"] \
        == "UNREVIEWED"
    # An unauthorized human approver is refused by the UI too.
    sel(at, "an_del_kind").select("HUMAN")
    sel(at, "an_del_pick").select("MASTER_SUBBED.mp4")
    ti(at, "an_del_approver").set_value("someone else")
    button(at, "an_del_ok").click().run()
    assert not at.exception
    assert any("박준태" in e for e in errors(at))
    assert deliverable_status(project, candidate["build_id"]) \
        ["deliverables"]["MASTER_SUBBED.mp4"]["state"] == "UNREVIEWED"
