"""REDESIGN_W00 R3: approvals are settings.

`project.yaml` `production` holds the approver, the per-shot attempt cap, the
subscription services and `gates: minimal|full`. These tests use synthetic
fixtures only (generated audio, Pillow frame sequences); the approvals,
delegations and reviews they write are protocol fixtures, not human approval.
"""
import json
import time

import pytest

from engine import cli
from engine.animation_locks import wave_scope_gate
from engine.animation_review import (_final_film_governor, record_cut_review,
                                     record_transition_review)
from engine.autopilot import autopilot
from engine.core import FilmError, gates, production_block, read, set_production, write
from engine.settings import set_settings
from engine.w00_gate import (LEGACY_APPROVER, _approver_verdict, approver_source,
                             gate_status, load_delegations, primary_approver,
                             record_delegation)
from test_anim_003 import animation_project
from test_anim_007 import (CUT_METHODS, REVIEWER, TRANSITION_METHODS,
                           import_shot, waved_project)
from test_compiler_v03 import fixture_project


@pytest.fixture(autouse=True)
def user_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))


def _in_an_hour():
    return int(time.time() * 1000) + 3_600_000


# ---- the production block --------------------------------------------------------------

def test_gates_default_follows_the_profile(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    legacy = fixture_project(tmp_path / "a", seconds=6, shot_count=3)
    assert read(legacy / "project.yaml")["production"]["gates"] == "minimal"
    assert gates(legacy) == "minimal"
    config = read(legacy / "project.yaml")
    del config["production"]
    write(legacy / "project.yaml", config)
    assert gates(legacy) == "minimal", "a LEGACY project that predates the setting is minimal"
    converted = animation_project(tmp_path / "b", shot_count=3, seconds=6)
    assert gates(converted) == "full", "converting to the ceremony profile pins gates: full"
    config = read(converted / "project.yaml")
    del config["production"]
    write(converted / "project.yaml", config)
    assert gates(converted) == "full", "a FRAME_ANIMATION_V1 project with no setting keeps its ladder"


def test_set_production_validates_before_it_writes(tmp_path):
    p = fixture_project(tmp_path, seconds=6, shot_count=3)
    before = (p / "project.yaml").read_bytes()
    for bad in ({"gates": "ful"}, {"max_attempts_per_shot": 0}, {"max_attempts_per_shot": True},
                {"max_attempts_per_shot": "3"}, {"services": "flow"}, {"approver_name": "x"}):
        with pytest.raises(FilmError):
            set_production(p, **bad)
    assert (p / "project.yaml").read_bytes() == before
    block = set_production(p, approver="  김감독 ", max_attempts_per_shot=2, gates="full",
                           services={"video": "flow"})
    assert block["approver"] == "김감독" and block["max_attempts_per_shot"] == 2 and gates(p) == "full"
    assert block["services"] == {"image": "", "video": "flow"}, "services merge per key"
    config = read(p / "project.yaml")
    config["production"]["gates"] = "sometimes"
    write(p / "project.yaml", config)
    with pytest.raises(FilmError, match="production.gates"):
        gates(p)


def test_production_command_shows_and_sets(tmp_path, capsys):
    p = fixture_project(tmp_path, seconds=6, shot_count=3)
    assert cli.main(["production", str(p), "--approver", "김감독", "--max-attempts", "4",
                     "--gates", "full", "--service", "video=flow"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["approver"] == {"name": "김감독", "source": "project"} and out["gates"] == "full"
    assert out["production"]["max_attempts_per_shot"] == 4 and out["production"]["services"]["video"] == "flow"
    assert cli.main(["production", str(p), "--service", "video"]) != 0, "KIND=NAME is required"
    assert cli.main(["production", str(p)]) == 0
    assert json.loads(capsys.readouterr().out)["production"]["max_attempts_per_shot"] == 4


# ---- the approver comes from settings --------------------------------------------------

def test_approver_order_project_then_user_then_legacy(tmp_path):
    p = animation_project(tmp_path, shot_count=3, seconds=6)
    assert approver_source(p) == (LEGACY_APPROVER, "legacy_default")
    assert primary_approver() == LEGACY_APPROVER
    set_settings("w00_gate", {"primary_approver": "이감독"})
    assert approver_source(p) == ("이감독", "user_setting")
    set_production(p, approver="김감독")
    assert approver_source(p) == ("김감독", "project") and primary_approver(p) == "김감독"
    assert gate_status(p)["approver"] == {"name": "김감독", "source": "project"}


def test_delegations_survive_an_approver_change_but_stop_granting(tmp_path):
    p = animation_project(tmp_path, shot_count=3, seconds=6)
    old = record_delegation(p, delegate="delegate x", scope=["ALL"], expires_at_ms=_in_an_hour())
    assert old["delegator"] == LEGACY_APPROVER
    assert _approver_verdict(p, "delegate x", "W00_PILOT")[0] is True
    set_production(p, approver="김감독")
    assert [d["delegation_id"] for d in load_delegations(p)] == [old["delegation_id"]], "old records stay readable"
    ok, why, _ = _approver_verdict(p, "delegate x", "W00_PILOT")
    assert not ok and "김감독" in why, "a delegation by a former approver grants nothing"
    assert _approver_verdict(p, "김감독", "W00_PILOT")[0] is True
    assert _approver_verdict(p, LEGACY_APPROVER, "W00_PILOT")[0] is False
    with pytest.raises(FilmError, match="김감독"):
        record_delegation(p, delegate="y", scope=["ALL"], delegator=LEGACY_APPROVER, expires_at_ms=_in_an_hour())
    new = record_delegation(p, delegate="delegate y", scope=["W00_PILOT"], expires_at_ms=_in_an_hour())
    assert new["delegator"] == "김감독" and new["delegation_id"] == "DG0002"
    assert _approver_verdict(p, "delegate y", "W00_PILOT")[0] is True


# ---- gates: minimal vs full on the ceremony profile ---------------------------------------

def test_minimal_gates_drop_the_ladder_full_keeps_it(tmp_path):
    p = waved_project(tmp_path)
    assert gates(p) == "full"
    assert autopilot(p)["stage"] == "NEEDS_PLAN_LOCK"
    gate = gate_status(p)["gate"]
    assert gate["state"] == "BLOCKED" and gate["gates"] == "full"
    assert any("PLAN_LOCK" in r for r in gate["reasons"])
    for shot, seed in (("S001", 10), ("S002", 20), ("S003", 30)):
        import_shot(p, tmp_path, shot, seed)
    with pytest.raises(FilmError, match="PLAN_LOCK"):
        wave_scope_gate(p, ["S001"])

    set_production(p, gates="minimal")
    wave_scope_gate(p, ["S001"])  # no PLAN/WAVE lock needed
    gate = gate_status(p)["gate"]
    assert gate == {"state": "OPEN", "reasons": [], "gates": "minimal"}
    stage = autopilot(p)
    assert stage["stage"] == "NEEDS_CUT_REVIEW" and stage["pending"] == ["I001", "I002", "I003", "T001", "T002"]
    for instance in ("I001", "I002", "I003"):
        record_cut_review(p, instance, reviewer=REVIEWER, methods=CUT_METHODS)
    stage = autopilot(p)
    assert stage["stage"] == "NEEDS_CUT_REVIEW" and stage["pending"] == ["T001", "T002"], "transitions are still reviewed"
    for transition in ("T001", "T002"):
        record_transition_review(p, transition, reviewer=REVIEWER, methods=TRANSITION_METHODS)
    stage = autopilot(p)
    assert stage["stage"] == "FINAL_CANDIDATE_READY", "no PLAN/WAVE/FINAL lock and no route decision on the way"
    assert stage["build_id"]
    set_production(p, gates="full")
    assert autopilot(p)["stage"] == "NEEDS_PLAN_LOCK", "switching back restores the ladder"


def test_minimal_gates_keep_missing_inputs_visible(tmp_path):
    p = waved_project(tmp_path)
    set_production(p, gates="minimal")
    stage = autopilot(p)
    assert stage["stage"] == "NEEDS_PRODUCTION_INPUTS"
    assert {m["shot_id"] for m in stage["missing"]} == {"S001", "S002", "S003"}


def test_final_film_approval_names_a_reviewer_under_minimal_only(tmp_path):
    p = animation_project(tmp_path, shot_count=3, seconds=6)
    ok, why, _ = _final_film_governor(p, "Some Reviewer")
    assert not ok and LEGACY_APPROVER in why
    set_production(p, gates="minimal")
    assert _final_film_governor(p, "Some Reviewer")[0] is True
    assert _final_film_governor(p, "  ")[0] is False, "a reviewer name is still required"
    set_production(p, gates="full", approver="김감독")
    assert _final_film_governor(p, "김감독")[0] is True
    assert _final_film_governor(p, LEGACY_APPROVER)[0] is False
