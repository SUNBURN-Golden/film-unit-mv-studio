"""ANIM-014 ExecutionPlan 1: document contract, gates and job identity
(schema §11, ADR §10)."""
import hashlib

import pytest

from anim_014_kit import (CONTRACT, FRAMES, RECIPE, RECIPE_B, SNAPSHOT,
                          edge, local_plan, make_plan, operation,
                          remote_plan)

from engine.animation_schema import canon_bytes, read_canon
from engine.core import FilmError
from engine.execution_plan import (assert_executable, job_key, plan_sha,
                                   validate_execution_plan, write_plan)


# -- document contract ------------------------------------------------------

def test_plan_round_trips_canonical(tmp_path):
    plan = remote_plan()
    write_plan(tmp_path / "plan.json", plan)
    loaded = read_canon(tmp_path / "plan.json")
    assert loaded == plan
    assert plan_sha(plan) == hashlib.sha256(canon_bytes(plan)).hexdigest()


def test_plan_rejects_unknown_fields_and_future_versions():
    plan = remote_plan()
    plan["schema_version"] = 2
    with pytest.raises(FilmError, match="schema_version"):
        validate_execution_plan(plan)
    plan = remote_plan()
    plan["document_type"] = "animation_timeline"
    with pytest.raises(FilmError, match="Expected"):
        validate_execution_plan(plan)
    plan = remote_plan()
    plan["surprise"] = True
    with pytest.raises(FilmError, match="Unknown"):
        validate_execution_plan(plan)


@pytest.mark.parametrize("mutate", [
    lambda d: d["execution"]["allowed_routes"].append("DIRECT_DRIVE"),
    lambda d: d["operations"][0].update(route="DIRECT_DRIVE"),
    lambda d: d.update(transfer_route="DIRECT_DRIVE"),
])
def test_direct_drive_is_never_a_candidate(mutate):
    plan = remote_plan()
    mutate(plan)
    with pytest.raises(FilmError):
        validate_execution_plan(plan)


def test_operations_must_tile_output_frames():
    # gap at [4, 6)
    with pytest.raises(FilmError, match="cover"):
        make_plan([operation("a", [0, 4], "REMOTE_CPU", "r"),
                   operation("b", [6, FRAMES], "REMOTE_CPU", "r")])
    # overlap
    with pytest.raises(FilmError, match="cover"):
        make_plan([operation("a", [0, 5], "REMOTE_CPU", "r"),
                   operation("b", [4, FRAMES], "REMOTE_CPU", "r")])
    # beyond output_frames
    with pytest.raises(FilmError, match="exceeds"):
        make_plan([operation("a", [0, FRAMES + 1], "REMOTE_CPU", "r")])


def test_operation_dag_rejects_cycles_and_unknown_deps():
    with pytest.raises(FilmError, match="cycle"):
        make_plan([operation("a", [0, 4], "REMOTE_CPU", "r",
                             depends=["b"]),
                   operation("b", [4, FRAMES], "REMOTE_CPU", "r",
                             depends=["a"])])
    with pytest.raises(FilmError, match="unknown"):
        make_plan([operation("a", [0, 4], "REMOTE_CPU", "r",
                             depends=["ghost"]),
                   operation("b", [4, FRAMES], "REMOTE_CPU", "r")])


def test_edges_validate_retry_policy_and_binding():
    plan = remote_plan()
    plan["transfer_edges"][0]["transport_retry_policy"] = {
        "max_retries": 2, "backoff_base_ms": 10}      # partial: reject
    with pytest.raises(FilmError, match="transport_retry_policy"):
        validate_execution_plan(plan)
    plan = remote_plan()
    plan["transfer_edges"][0]["transport_retry_policy"] = {
        "max_retries": 9, "backoff_base_ms": 0, "max_backoff_ms": 0,
        "max_elapsed_ms": 1, "max_requests": 1, "max_transferred_bytes": 1}
    with pytest.raises(FilmError, match="cap 3"):
        validate_execution_plan(plan)
    plan = remote_plan()
    plan["transfer_edges"][0]["snapshot_digest"] = RECIPE   # stale snapshot
    with pytest.raises(FilmError, match="snapshot"):
        validate_execution_plan(plan)
    plan = remote_plan()
    plan["transfer_edges"][0]["credential_boundary"]["oauth_token"] = "x"
    with pytest.raises(FilmError, match="credential_boundary"):
        validate_execution_plan(plan)


def test_full_retry_policy_is_accepted():
    retry = {"max_retries": 3, "backoff_base_ms": 10,
             "max_backoff_ms": 100, "max_elapsed_ms": 1000,
             "max_requests": 8, "max_transferred_bytes": 1 << 20}
    plan = remote_plan(edges=[dict(edge("e-in", "COORDINATOR", "WORKER"),
                                 transport_retry_policy=retry),
                              edge("e-out", "WORKER", "COORDINATOR")])
    assert plan["transfer_edges"][0]["transport_retry_policy"] == retry


# -- execution gates -----------------------------------------------------------

def test_nothing_executes_without_capability_evidence():
    plan = remote_plan(policy="AUTO_PERFORMANCE", evidence_required=True)
    with pytest.raises(FilmError, match="CAPABILITY_EVIDENCE_REQUIRED"):
        assert_executable(plan)
    evidence = {"REMOTE_CPU": {"state": "QUALIFIED_FOR_SCOPE"}}
    assert_executable(plan, evidence=evidence)
    stale = {"REMOTE_CPU": {"state": "STALE"}}
    with pytest.raises(FilmError, match="CAPABILITY_EVIDENCE_REQUIRED"):
        assert_executable(plan, evidence=stale)


def test_additional_charges_need_separate_approval():
    plan = remote_plan(allow_charges=True)
    with pytest.raises(FilmError, match="ADDITIONAL_CHARGES_NOT_APPROVED"):
        assert_executable(plan)
    assert_executable(plan, additional_charges_approved=True)


def test_remote_operations_need_relay_edges():
    plan = remote_plan(edges=[edge("e-in", "COORDINATOR", "WORKER")])
    with pytest.raises(FilmError, match="MISSING_TRANSFER_EDGES"):
        assert_executable(plan)


# -- job identity ---------------------------------------------------------------

def test_job_key_binds_snapshot_range_recipe_and_runtime():
    plan = remote_plan()
    key = job_key(plan, plan["operations"][0])
    assert len(key) == 64
    assert key == job_key(plan, plan["operations"][0])
    # same inputs, same key — a resubmission can never mint a new identity
    changed = remote_plan()
    changed["snapshot_digest"] = hashlib.sha256(b"other").hexdigest()
    assert job_key(changed, changed["operations"][0]) != key
    r2 = remote_plan()
    r2["operations"][0]["output_range"] = [0, 3]
    r2["operations"][1]["output_range"] = [3, FRAMES]
    assert job_key(r2, r2["operations"][0]) != key
    r3 = remote_plan()
    r3["operations"][0]["recipe_digest"] = RECIPE_B
    assert job_key(r3, r3["operations"][0]) != key
    r4 = remote_plan()
    r4["operations"][0]["runtime_contract"] = "other-runtime-v2"
    assert job_key(r4, r4["operations"][0]) != key
    # different operation in the same plan is a different job
    assert job_key(plan, plan["operations"][1]) != key


def test_local_plan_needs_no_relay_edges():
    plan = local_plan()
    assert_executable(plan)
