"""ANIM-007: PLAN/WAVE/FINAL scope locks, W00 checkpoint and route decisions.

All fixtures are synthetic (Pillow PNG frames, a generated sine master). The
`animation_locks`, `animation_waves`, `route_decision` and review records
written here are protocol fixtures exercising the engine boundary — they are
not evidence of real artwork production or approval, and nothing here is a
production qualification, a paid generation or a release. Actual W00 artwork
production and human acceptance remain outside this task.
"""
import json
from pathlib import Path

import pytest

from engine import cli
from engine.animation_assets import import_frame_sequence
from engine.animation_compiler import compile_final_candidate
from engine.animation_locks import (LOCKS_PATH, ROUTES_PATH, WAVES_PATH,
                                    declare_waves, latest_route_decision,
                                    load_waves, lock_status,
                                    record_final_lock, record_plan_lock,
                                    record_route_decision, record_wave_lock,
                                    route_status)
from engine.animation_review import (record_cut_review, record_film_review,
                                     record_transition_review, review_status)
from engine.animation_schema import canon_bytes, read_canon, write_canon
from engine.autopilot import autopilot
from engine.core import FilmError, read, write
from test_anim_003 import animation_project, make_sequence
from test_compiler_v03 import fixture_project, newest_build

SIZE = (64, 48)
REVIEWER = "Synthetic fixture reviewer"
APPROVER = "Synthetic fixture scope approver"
LEAD = "Synthetic fixture production lead"
CUT_METHODS = ["CUT_FULL_SPEED_PLAYBACK"]
TRANSITION_METHODS = ["TRANSITION_FULL_SPEED_PLAYBACK"]
FILM_METHODS = ["FULL_SPEED_WHOLE_FILM", "TECHNICAL_VALIDATION"]
HARD_TYPES = [{"type": "FACE_TURN", "reason": "fixture: face turn exposes route limits"},
              {"type": "CONTACT", "reason": "fixture: hand contact overlaps"}]
FRAMES = 48  # 2s per shot at 24fps


def _waves(leading="W00"):
    return [{"wave": leading, "shots": ["S001", "S002"],
             "difficulty": list(HARD_TYPES), "note": "embedded pilot cuts"},
            {"wave": "W01", "shots": ["S003"], "difficulty": [],
             "note": "remaining scope"}]


def waved_project(tmp_path, waves=None):
    """Converted 3-cut project with the production order declared, no assets."""
    p = animation_project(tmp_path, shot_count=3, seconds=6)
    declare_waves(p, _waves() if waves is None else waves)
    return p


def import_shot(p, tmp_path, shot_id, seed):
    folder = tmp_path / f"seq_{shot_id.lower()}_{seed}"
    make_sequence(folder, count=FRAMES, size=SIZE, seed=seed)
    return import_frame_sequence(p, shot_id, folder=folder)


def w00_project(tmp_path):
    """W00's two cuts imported; W01 (S003) deliberately has no assets yet."""
    p = waved_project(tmp_path)
    import_shot(p, tmp_path, "S001", 10)
    import_shot(p, tmp_path, "S002", 20)
    return p


def adopt_w00(p):
    record_cut_review(p, "I001", reviewer=REVIEWER, methods=CUT_METHODS)
    record_cut_review(p, "I002", reviewer=REVIEWER, methods=CUT_METHODS)


def decided_w00(p, tmp_path):
    """W00 adopted and a KEEP route decision opening W01."""
    record_plan_lock(p, APPROVER)
    record_wave_lock(p, "W00", APPROVER)
    adopt_w00(p)
    return record_route_decision(
        p, "W00", decision="KEEP", decider=LEAD, approver=APPROVER,
        checked_types=[d["type"] for d in HARD_TYPES], unchecked_types=[],
        apply_scope=["W01"],
        reviewed_conditions=["fixture: both W00 cuts played at speed"],
        observations=["fixture: no route limit observed"],
        cost_time_impact="none")


def produced_project(p, tmp_path):
    """A fully produced project: every wave locked and every cut reviewed."""
    import_shot(p, tmp_path, "S003", 30)
    decided_w00(p, tmp_path)
    record_wave_lock(p, "W01", APPROVER)
    record_cut_review(p, "I003", reviewer=REVIEWER, methods=CUT_METHODS)
    return p


def review_transitions(p):
    for transition_id in ("T001", "T002"):
        record_transition_review(p, transition_id, reviewer=REVIEWER,
                                 methods=TRANSITION_METHODS)


# --- wave declaration (design 5.3/9.2; schema §7 waves) --------------------

def test_waves_declaration_and_validation(tmp_path):
    p = waved_project(tmp_path)
    document = load_waves(p)
    assert document["document_type"] == "animation_waves"
    assert document["schema_version"] == 1
    assert [w["wave"] for w in document["waves"]] == ["W00", "W01"]
    # Stored canonically; a redeclared order fails validation.
    path = p / WAVES_PATH
    assert path.read_bytes() == canon_bytes(document)

    for index, bad in enumerate((
            [{"wave": "W00", "shots": ["S001"], "difficulty": HARD_TYPES}],           # incomplete coverage
            [{"wave": "W00", "shots": ["S001", "S002", "S003", "S999"],
              "difficulty": HARD_TYPES}],                                            # unknown shot
            [{"wave": "W00", "shots": ["S001", "S002"], "difficulty": HARD_TYPES},
             {"wave": "W01", "shots": ["S002", "S003"], "difficulty": []}],           # shot twice
            [{"wave": "W01", "shots": ["S001", "S002"], "difficulty": HARD_TYPES},
             {"wave": "W00", "shots": ["S003"], "difficulty": []}],                   # wrong order
            [{"wave": "W00", "shots": ["S001", "S002"], "difficulty": []},
             {"wave": "W01", "shots": ["S003"], "difficulty": []}],                   # no hard types
    )):
        case_dir = tmp_path / "bad" / f"case{index}"
        case_dir.mkdir(parents=True)
        p = animation_project(case_dir, shot_count=3, seconds=6)
        with pytest.raises(FilmError):
            declare_waves(p, bad)


def test_waves_require_animation_profile(tmp_path):
    p = fixture_project(tmp_path, seconds=6, shot_count=3)
    with pytest.raises(FilmError, match="LEGACY_MV"):
        declare_waves(p, _waves())
    with pytest.raises(FilmError, match="LEGACY_MV"):
        record_plan_lock(p, APPROVER)


# --- PLAN/WAVE/FINAL lock semantics (schema §7.1) --------------------------

def test_plan_lock_binds_plan_inputs_not_detail_assets(tmp_path):
    p = waved_project(tmp_path)
    record = record_plan_lock(p, APPROVER)
    assert record["scope"] == "PLAN_LOCK" and record["lock_id"] == "L0001"
    status = lock_status(p)
    assert status["plan"]["state"] == "CURRENT"
    assert status["plan"]["binding_sha256"] == record["binding_sha256"]

    # Producing detail assets does not stale the plan scope.
    import_shot(p, tmp_path, "S001", 10)
    assert lock_status(p)["plan"]["state"] == "CURRENT"

    # A timeline edit stales the plan binding and names the changed field.
    timeline = read_canon(p / "timeline/edit.json")
    timeline["entries"][0]["transition_out"] = {
        "id": "T001", "type": "CROSSFADE", "to_instance": "I002",
        "overlap_frames": 2, "curve": "LINEAR_INTERIOR_V1"}
    timeline["target_frames"] -= 2
    write_canon(p / "timeline/edit.json", timeline)
    config = read(p / "project.yaml")
    config["animation"]["output_frames"] = timeline["target_frames"]
    write(p / "project.yaml", config)
    status = lock_status(p)
    assert status["plan"]["state"] == "STALE"
    assert "edit_sha256" in status["plan"]["changed"]


def test_plan_lock_recovers_and_reports_intent_change(tmp_path):
    p = waved_project(tmp_path)
    record_plan_lock(p, APPROVER)
    write(p / "bible/story.md", "Synthetic compiler regression. amended\n")
    status = lock_status(p)
    assert status["plan"]["state"] == "STALE"
    assert "intent_sha256" in status["plan"]["changed"]
    again = record_plan_lock(p, APPROVER)
    assert again["revision"] == 2
    assert lock_status(p)["plan"]["state"] == "CURRENT"


def test_wave_lock_binds_exact_wave_assets(tmp_path):
    p = w00_project(tmp_path)
    record_plan_lock(p, APPROVER)
    record = record_wave_lock(p, "W00", APPROVER)
    assert record["wave"] == "W00"
    pins = {c["shot_id"]: c["content_sha256"] for c in record["binding"]["cuts"]}
    registry = read(p / "manifest/animation_assets.json")
    for shot in ("S001", "S002"):
        assert pins[shot] == registry["assignments"][shot]["content_sha256"]
    assert lock_status(p)["waves"]["W00"]["state"] == "CURRENT"
    # W01 is not locked; its shots are absent from the W00 binding.
    assert "S003" not in pins


def test_wave_lock_needs_plan_and_resolved_assets(tmp_path):
    p = w00_project(tmp_path)
    with pytest.raises(FilmError, match="PLAN_LOCK"):
        record_wave_lock(p, "W00", APPROVER)
    record_plan_lock(p, APPROVER)
    with pytest.raises(FilmError, match="not a declared production wave"):
        record_wave_lock(p, "W99", APPROVER)

    empty = tmp_path / "empty"
    empty.mkdir()
    p2 = waved_project(empty)
    record_plan_lock(p2, APPROVER)
    with pytest.raises(FilmError, match="unresolved inputs"):
        record_wave_lock(p2, "W00", APPROVER)


# --- scope enforcement: production outside the locked scope is refused ------

def test_scope_outside_production_is_refused(tmp_path):
    p = w00_project(tmp_path)
    record_plan_lock(p, APPROVER)
    # Cut review (adoption) before the wave is locked is refused.
    with pytest.raises(FilmError, match="locked wave scope"):
        record_cut_review(p, "I001", reviewer=REVIEWER, methods=CUT_METHODS)
    record_wave_lock(p, "W00", APPROVER)
    record_cut_review(p, "I001", reviewer=REVIEWER, methods=CUT_METHODS)
    # A cut in a wave that is not yet open/locked stays refused.
    import_shot(p, tmp_path, "S003", 30)
    with pytest.raises(FilmError, match="locked wave scope"):
        record_cut_review(p, "I003", reviewer=REVIEWER, methods=CUT_METHODS)
    # A later wave cannot lock before the route decision opens it.
    with pytest.raises(FilmError, match="route decision"):
        record_wave_lock(p, "W01", APPROVER)
    # Final candidate production refuses without a FINAL_LOCK.
    with pytest.raises(FilmError, match="FINAL_LOCK"):
        compile_final_candidate(p)


def test_transition_review_needs_both_endpoint_waves_locked(tmp_path):
    p = produced_project(w00_project(tmp_path), tmp_path)
    # T002 spans the W00/W01 boundary; once W01 unlocked after re-import of
    # S003, the boundary transition cannot be reviewed.
    import_shot(p, tmp_path, "S003", 31)
    assert lock_status(p)["waves"]["W01"]["state"] == "STALE"
    with pytest.raises(FilmError, match="locked wave scope"):
        record_transition_review(p, "T002", reviewer=REVIEWER,
                                 methods=TRANSITION_METHODS)
    # T001 is entirely inside the still-locked W00 wave.
    record_transition_review(p, "T001", reviewer=REVIEWER,
                             methods=TRANSITION_METHODS)


# --- W00 checkpoint and the route decision ---------------------------------

def test_w00_adoption_stops_at_route_decision(tmp_path):
    p = w00_project(tmp_path)
    # The selected main-film cuts produce with no W01 detail assets present.
    record_plan_lock(p, APPROVER)
    record_wave_lock(p, "W00", APPROVER)
    adopt_w00(p)
    stage = autopilot(p)
    assert stage["stage"] == "NEEDS_ROUTE_DECISION"
    assert stage["wave"] == "W00"
    assert set(stage["adopted"]) == {"I001", "I002"}
    # The checkpoint stays put on further runs — no automatic wave advance.
    assert autopilot(p)["stage"] == "NEEDS_ROUTE_DECISION"
    assert autopilot(p)["stage"] == "NEEDS_ROUTE_DECISION"
    assert not list(p.glob("builds/B*"))
    # Honest evidence facets: fixture flow never claims real approval.
    assert stage["facets"] == {"qualification_state": "UNQUALIFIED",
                             "acceptance_state": "PENDING",
                             "release_state": "NOT_AUTHORIZED"}


def test_autopilot_walks_the_scope_gates(tmp_path):
    p = animation_project(tmp_path, shot_count=3, seconds=6)
    assert autopilot(p)["stage"] == "NEEDS_PLAN_LOCK"
    record_plan_lock(p, APPROVER)
    stage = autopilot(p)
    assert stage["stage"] == "NEEDS_PRODUCTION_INPUTS"
    assert stage["missing"] == [WAVES_PATH]
    declare_waves(p, _waves())
    stage = autopilot(p)
    assert stage["stage"] == "NEEDS_PRODUCTION_INPUTS"
    assert stage["wave"] == "W00"
    assert {m["shot_id"] for m in stage["missing"]} == {"S001", "S002"}
    import_shot(p, tmp_path, "S001", 10)
    import_shot(p, tmp_path, "S002", 20)
    assert autopilot(p)["stage"] == "NEEDS_WAVE_LOCK"
    record_wave_lock(p, "W00", APPROVER)
    stage = autopilot(p)
    assert stage["stage"] == "NEEDS_CUT_REVIEW"
    assert stage["pending"] == ["I001", "I002"]
    adopt_w00(p)
    assert autopilot(p)["stage"] == "NEEDS_ROUTE_DECISION"


def test_keep_decision_opens_the_next_wave(tmp_path):
    p = w00_project(tmp_path)
    decided_w00(p, tmp_path)
    status = route_status(p)
    assert status["checkpoint"] == "DECIDED"
    assert status["decision"]["decision"] == "KEEP"
    assert status["open_waves"] == ["W01"]
    assert status["facets"]["acceptance_state"] == "PENDING"

    # The next wave now walks its own gates; S003 still has no assets.
    stage = autopilot(p)
    assert stage["stage"] == "NEEDS_PRODUCTION_INPUTS"
    assert stage["wave"] == "W01"
    import_shot(p, tmp_path, "S003", 30)
    assert autopilot(p)["stage"] == "NEEDS_WAVE_LOCK"
    record_wave_lock(p, "W01", APPROVER)
    stage = autopilot(p)
    assert stage["stage"] == "NEEDS_CUT_REVIEW"
    assert stage["pending"] == ["I003"]


def test_route_decision_needs_adoption_or_grounds(tmp_path):
    p = w00_project(tmp_path)
    record_plan_lock(p, APPROVER)
    record_wave_lock(p, "W00", APPROVER)
    record_cut_review(p, "I001", reviewer=REVIEWER, methods=CUT_METHODS)
    # I002 is unadopted: a normal decision cannot be recorded.
    with pytest.raises(FilmError, match="adopted review"):
        record_route_decision(
            p, "W00", decision="CHANGE", decider=LEAD, approver=APPROVER,
            checked_types=["FACE_TURN"], unchecked_types=["CONTACT"],
            apply_scope=["W01"], changes=["route revision"],
            reviewed_conditions=["c"], observations=["o"])
    # Early decision needs recorded grounds.
    with pytest.raises(FilmError, match="grounds"):
        record_route_decision(
            p, "W00", decision="CHANGE", decider=LEAD, approver=APPROVER,
            checked_types=["FACE_TURN"], unchecked_types=["CONTACT"],
            apply_scope=["W01"], changes=["route revision"], early=True)
    # KEEP is only honest when every cut was adopted and every type checked.
    with pytest.raises(FilmError, match="adopted"):
        record_route_decision(
            p, "W00", decision="KEEP", decider=LEAD, approver=APPROVER,
            checked_types=list(t["type"] for t in HARD_TYPES),
            unchecked_types=[], apply_scope=["W01"],
            reviewed_conditions=["c"], observations=["o"])

    record_cut_review(p, "I002", reviewer=REVIEWER, methods=CUT_METHODS)
    with pytest.raises(FilmError, match="checked"):
        record_route_decision(
            p, "W00", decision="KEEP", decider=LEAD, approver=APPROVER,
            checked_types=["FACE_TURN"], unchecked_types=["CONTACT"],
            apply_scope=["W01"],
            reviewed_conditions=["c"], observations=["o"])
    with pytest.raises(FilmError, match="every later wave"):
        record_route_decision(
            p, "W00", decision="KEEP", decider=LEAD, approver=APPROVER,
            checked_types=[t["type"] for t in HARD_TYPES], unchecked_types=[],
            apply_scope=[],
            reviewed_conditions=["c"], observations=["o"])
    with pytest.raises(FilmError, match="initial wave"):
        record_route_decision(
            p, "W01", decision="KEEP", decider=LEAD, approver=APPROVER,
            checked_types=[], unchecked_types=[], apply_scope=[],
            reviewed_conditions=["c"], observations=["o"])


def test_early_failure_decision_is_recorded(tmp_path):
    p = w00_project(tmp_path)
    record_plan_lock(p, APPROVER)
    record_wave_lock(p, "W00", APPROVER)
    # A structural failure means S002 never reaches an adopted version: the
    # production lead brings the decision point forward with evidence.
    record_cut_review(p, "I001", reviewer=REVIEWER, methods=CUT_METHODS)
    record = record_route_decision(
        p, "W00", decision="CHANGE", decider=LEAD, approver=APPROVER,
        checked_types=["FACE_TURN"], unchecked_types=["CONTACT"],
        apply_scope=["W01"], changes=["S002 motion route replaced"],
        early=True, grounds="fixture: S002 contact pass never converged",
        cost_time_impact="fixture: one cut re-rig")
    assert record["early"] is True
    cuts = {c["instance_id"]: c for c in record["cuts"]}
    assert cuts["I001"]["adopted"] is True
    assert cuts["I002"]["adopted"] is False
    assert cuts["I002"]["review_id"] is None
    # The decision opens its declared scope only; W00's unadopted cut stays a
    # production gate — the decision never marks it produced.
    stage = autopilot(p)
    assert stage["stage"] == "NEEDS_CUT_REVIEW" and stage["pending"] == ["I002"]
    status = route_status(p)
    assert status["checkpoint"] == "DECIDED"
    assert status["open_waves"] == ["W01"]


def test_apply_scope_bounds_later_waves(tmp_path):
    p = animation_project(tmp_path, shot_count=4, seconds=8)
    declare_waves(p, [
        {"wave": "W00", "shots": ["S001", "S002"], "difficulty": list(HARD_TYPES)},
        {"wave": "W01", "shots": ["S003"], "difficulty": []},
        {"wave": "W02", "shots": ["S004"], "difficulty": []}])
    for shot, seed in (("S001", 10), ("S002", 20), ("S003", 30), ("S004", 40)):
        import_shot(p, tmp_path, shot, seed)
    record_plan_lock(p, APPROVER)
    record_wave_lock(p, "W00", APPROVER)
    adopt_w00(p)
    # MIX opens only the waves it names; W02 stays closed.
    record_route_decision(
        p, "W00", decision="MIX", decider=LEAD, approver=APPROVER,
        checked_types=[t["type"] for t in HARD_TYPES], unchecked_types=[],
        apply_scope=["W01"], changes=["fixture: mixed route"],
        reviewed_conditions=["c"], observations=["o"])
    record_wave_lock(p, "W01", APPROVER)
    with pytest.raises(FilmError, match="apply scope"):
        record_wave_lock(p, "W02", APPROVER)
    record_cut_review(p, "I003", reviewer=REVIEWER, methods=CUT_METHODS)
    stage = autopilot(p)
    assert stage["stage"] == "WAVE_SCOPE_CLOSED"
    assert stage["wave"] == "W02"


def test_route_decision_log_is_append_only_and_tamper_evident(tmp_path):
    p = w00_project(tmp_path)
    decided_w00(p, tmp_path)
    path = p / ROUTES_PATH
    raw = path.read_bytes()
    lines = raw.split(b"\n")
    assert lines[-1] == b"" and len(lines) == 2
    record = latest_route_decision(p, "W00")
    assert record["decision_id"] == "RD0001"
    assert lines[0] + b"\n" == canon_bytes(record)
    # Editing a stored field breaks the binding digest.
    stored = json.loads(lines[0])
    stored["decision"] = "MIX"
    path.write_bytes(canon_bytes(stored))
    with pytest.raises(FilmError, match="binding hash"):
        route_status(p)
    # A second recorded decision gets the next revision.
    path.write_bytes(raw)
    again = record_route_decision(
        p, "W00", decision="MIX", decider=LEAD, approver=APPROVER,
        checked_types=[t["type"] for t in HARD_TYPES], unchecked_types=[],
        apply_scope=["W01"], changes=["mix fixture"],
        reviewed_conditions=["c"], observations=["o"])
    assert again["decision_id"] == "RD0002" and again["revision"] == 2
    assert latest_route_decision(p, "W00")["decision"] == "MIX"


def test_locks_document_is_canonical_and_tamper_evident(tmp_path):
    p = w00_project(tmp_path)
    record_plan_lock(p, APPROVER)
    record_wave_lock(p, "W00", APPROVER)
    document = read_canon(p / LOCKS_PATH)
    assert [r["scope"] for r in document["locks"]] == ["PLAN_LOCK", "WAVE_LOCK"]
    # Mutating a bound field invalidates the stored binding hash.
    document["locks"][0]["binding"]["edit_sha256"] = "0" * 64
    write_canon(p / LOCKS_PATH, document)
    with pytest.raises(FilmError, match="binding hash"):
        lock_status(p)


# --- selective staleness -----------------------------------------------------

def test_changed_input_stales_only_affected_cuts(tmp_path):
    p = produced_project(w00_project(tmp_path), tmp_path)
    review_transitions(p)
    before = review_status(p)["targets"]
    assert all(row["state"] == "CURRENT" for row in before.values())

    # A new revision of S001 moves the adopted pin.
    import_shot(p, tmp_path, "S001", 11)
    status = review_status(p)["targets"]
    assert status["I001"]["state"] == "STALE"
    assert status["T001"]["state"] == "STALE"     # binds the I001 sequence
    assert status["I002"]["state"] == "CURRENT"
    assert status["I003"]["state"] == "CURRENT"
    assert status["T002"]["state"] == "CURRENT"

    locks = lock_status(p)
    assert locks["waves"]["W00"]["state"] == "STALE"
    assert locks["waves"]["W00"]["changed"] == ["cuts:I001"]
    assert locks["waves"]["W01"]["state"] == "CURRENT"
    # The plan scope binds composition, not adoption revisions: it stays.
    assert locks["plan"]["state"] == "CURRENT"

    # Recovery re-runs the same human gates for the affected scope only.
    with pytest.raises(FilmError, match="locked wave scope"):
        record_cut_review(p, "I001", reviewer=REVIEWER, methods=CUT_METHODS)
    record_wave_lock(p, "W00", APPROVER)
    record_cut_review(p, "I001", reviewer=REVIEWER, methods=CUT_METHODS)
    record_transition_review(p, "T001", reviewer=REVIEWER,
                             methods=TRANSITION_METHODS)
    status = review_status(p)["targets"]
    assert all(row["state"] == "CURRENT" for row in status.values())


def test_plan_change_stales_locks_not_adoptions(tmp_path):
    p = w00_project(tmp_path)
    record_plan_lock(p, APPROVER)
    record_wave_lock(p, "W00", APPROVER)
    adopt_w00(p)
    # A plan edit (transition recipe) stales the plan scope and the affected
    # transition's review basis — the adopted cut bytes are untouched.
    timeline = read_canon(p / "timeline/edit.json")
    timeline["entries"][0]["transition_out"] = {
        "id": "T001", "type": "CROSSFADE", "to_instance": "I002",
        "overlap_frames": 2, "curve": "LINEAR_INTERIOR_V1"}
    timeline["target_frames"] -= 2
    write_canon(p / "timeline/edit.json", timeline)
    config = read(p / "project.yaml")
    config["animation"]["output_frames"] = timeline["target_frames"]
    write(p / "project.yaml", config)
    # S003 is still unimported; the tolerant view reports per-target state.
    status = review_status(p, strict=False)["targets"]
    assert status["I001"]["state"] == "CURRENT"
    assert status["I002"]["state"] == "CURRENT"
    assert lock_status(p)["plan"]["state"] == "STALE"
    # The checkpoint does not silently continue under a stale plan scope.
    assert autopilot(p)["stage"] == "NEEDS_PLAN_LOCK"


# --- the rest of the run: FINAL_LOCK, candidate, final review ----------------

def test_final_chain_lock_candidate_review(tmp_path):
    p = produced_project(w00_project(tmp_path), tmp_path)
    stage = autopilot(p)
    assert stage["stage"] == "NEEDS_CUT_REVIEW"     # transitions remain
    review_transitions(p)
    stage = autopilot(p)
    assert stage["stage"] == "NEEDS_FINAL_LOCK"
    with pytest.raises(FilmError, match="FINAL_LOCK"):
        compile_final_candidate(p)
    lock = record_final_lock(p, APPROVER)
    assert lock["scope"] == "FINAL_LOCK"
    assert lock_status(p)["final"]["state"] == "CURRENT"

    stage = autopilot(p)
    assert stage["stage"] == "FINAL_CANDIDATE_READY"
    build_id = stage["build_id"]
    folder, record = newest_build(p)
    assert record["build_id"] == build_id
    assert record["mode"] == "FINAL_CANDIDATE"
    # The candidate is revisited, not rebuilt, while the film is unreviewed.
    stage = autopilot(p)
    assert stage["stage"] == "NEEDS_FINAL_REVIEW"
    assert stage["build_id"] == build_id
    assert len(list(p.glob("builds/B*"))) == 1
    review = record_film_review(p, build_id, reviewer=REVIEWER,
                                methods=FILM_METHODS)
    stage = autopilot(p)
    assert stage["stage"] == "FINAL_APPROVED"
    assert stage["review_id"] == review["review_id"]
    # The stage name reports the software state; real artwork acceptance is
    # still not established by fixture records.
    assert stage["facets"]["acceptance_state"] == "PENDING"
    assert stage["facets"]["release_state"] == "NOT_AUTHORIZED"


def test_final_lock_stales_when_a_reviewed_input_changes(tmp_path):
    p = produced_project(w00_project(tmp_path), tmp_path)
    review_transitions(p)
    record_final_lock(p, APPROVER)
    import_shot(p, tmp_path, "S003", 31)
    locks = lock_status(p)
    assert locks["final"]["state"] == "STALE"
    assert locks["waves"]["W01"]["state"] == "STALE"
    assert review_status(p)["targets"]["I003"]["state"] == "STALE"
    assert review_status(p)["targets"]["I001"]["state"] == "CURRENT"


# --- legacy / migration boundaries ------------------------------------------

def test_legacy_project_keeps_the_legacy_autopilot(tmp_path):
    p = fixture_project(tmp_path, seconds=6, shot_count=3)
    stage = autopilot(p)
    assert stage["stage"] == "NEEDS_MODEL_CHOICE"
    assert "facets" not in stage


def test_animation_init_creates_no_scope_files(tmp_path):
    p = animation_project(tmp_path, shot_count=3, seconds=6)
    assert not (p / LOCKS_PATH).exists()
    assert not (p / WAVES_PATH).exists()
    assert not (p / ROUTES_PATH).exists()
    # Without waves, ANIM-006 style reviews keep working ungated.
    import_shot(p, tmp_path, "S001", 10)
    record_cut_review(p, "I001", reviewer=REVIEWER, methods=CUT_METHODS)
    assert review_status(p, strict=False)["targets"]["I001"]["state"] == "CURRENT"


def test_cli_lock_and_route_decision(tmp_path, capsys):
    p = w00_project(tmp_path)
    assert cli.main(["animation-locks", str(p)]) == 0
    capsys.readouterr()
    assert cli.main(["animation-lock", str(p), "--scope", "PLAN_LOCK",
                     "--approver", APPROVER]) == 0
    assert cli.main(["animation-lock", str(p), "--scope", "WAVE_LOCK",
                     "--wave", "W00", "--approver", APPROVER]) == 0
    # W01 cannot lock from the CLI either before the route decision.
    assert cli.main(["animation-lock", str(p), "--scope", "WAVE_LOCK",
                     "--wave", "W01", "--approver", APPROVER]) == 1
    out = capsys.readouterr()
    assert "route decision" in out.err
    assert cli.main(["review-cut", str(p), "I001", "--reviewer", REVIEWER,
                     "--methods", CUT_METHODS[0]]) == 0
    assert cli.main(["review-cut", str(p), "I002", "--reviewer", REVIEWER,
                     "--methods", CUT_METHODS[0]]) == 0
    assert cli.main(["route-decision", str(p), "W00", "--decision", "keep",
                     "--decider", LEAD, "--approver", APPROVER,
                     "--apply-scope", "W01",
                     "--checked", ",".join(t["type"] for t in HARD_TYPES),
                     "--conditions", "fixture conditions",
                     "--observations", "fixture observations"]) == 0
    assert latest_route_decision(p, "W00")["decision"] == "KEEP"
    assert cli.main(["animation-waves", str(p)]) == 0
