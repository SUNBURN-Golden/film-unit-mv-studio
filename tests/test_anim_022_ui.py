"""ANIM-022 UI: the W00 pilot gate and sealed delivery approvals.

Driven headlessly with streamlit.testing.v1.AppTest against
app/control_panel.py on synthetic fixture projects. Every record written
through the UI here is a protocol fixture: SYNTHETIC_FIXTURE reviewers are
declared as such, and nothing claims real artwork approval, qualification or
release (facets stay UNQUALIFIED / PENDING / NOT_AUTHORIZED).
"""
import pytest
from streamlit.testing.v1 import AppTest

from engine.builds import list_builds
from engine.w00_gate import (deliverable_status, gate_status, load_pilots)
from test_anim_011_ui import (NAME, ROOT, adopted_w00, button, errors,
                              produced_project, route_decision, sel,
                              successes, ti, transition_review)
from test_compiler_v03 import fixture_project
from engine.animation_migrate import animation_init


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


def pilot_record(at, target, decision="KEEP", reviewer="UI fixture pilot",
                 kind="SYNTHETIC_FIXTURE", reason="fixture: hard target"):
    sel(at, "an_pilot_target").select(target)
    next(r for r in at.radio
         if r.key == "an_pilot_decision").set_value(decision)
    ti(at, "an_pilot_reviewer").set_value(reviewer)
    sel(at, "an_pilot_kind").select(kind)
    ti(at, "an_pilot_reason").set_value(reason)
    button(at, "an_pilot_record").click().run()


def test_w00_gate_lists_pilots_blocks_then_opens(screen):
    at, project = screen
    adopted_w00(at)
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


def test_w00_gate_refuses_an_unauthorized_human(screen):
    at, project = screen
    adopted_w00(at)
    pilot_record(at, "I001", reviewer="unauthorized person", kind="HUMAN")
    assert not at.exception
    assert any("박준태" in e for e in errors(at))
    assert load_pilots(project) == []
    pilot_record(at, "I001")  # declared synthetic evidence is allowed
    assert any("파일럿" in s for s in successes(at))
    record = load_pilots(project)[0]
    assert record["reviewer_kind"] == "SYNTHETIC_FIXTURE"


def test_pilot_issue_disposition_is_recorded_and_listed(screen):
    at, project = screen
    adopted_w00(at)
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


def test_clean_and_subbed_approve_separately_in_ui(screen):
    at, project = screen
    produced_project(at)
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
