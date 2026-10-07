"""Node film-resource-forecast: pre-execution resource forecast.

Every acceptance item of the node: per-line measurement provenance
(environment + scope) or UNKNOWN, past-session measurements never
confused with the current device, expired capability/entitlement, disk
shortfall, quote change after issuance, unknown inclusion never shown as
free, unselected paid fallbacks never auto-run, and the chunk / quality /
route alternatives offered instead. All fixtures are synthetic and local;
facets stay UNQUALIFIED / PENDING / NOT_AUTHORIZED.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from anim_014_kit import make_plan, operation
from engine import cli
from engine import perf_scheduler as ps
from engine.capability_registry import (make_measurement, plan_candidates,
                                        register, save_measurement)
from engine.animation_schema import write_canon
from engine.core import FilmError
from engine.encoder_backends import capability_evidence
from engine.execution_plan import validate_execution_plan
from engine.execution_workers.subscription import (
    make_entitlement, quote_digest, save_entitlement,
    subscription_worker_id)
from engine.resource_forecast import (_load_entitlements, _workspace_check,
                                      forecast_plan)
from test_anim_017 import perf_project

SERVICE = "chatgpt-code-runtime"
BINDING = hashlib.sha256(b"forecast-account-digest").hexdigest()
WID = subscription_worker_id(SERVICE, BINDING)

CAPS = {"max_input_bytes": 1 << 20, "max_output_bytes": 1 << 20,
        "max_file_count": 64, "max_frames_per_packet": 4,
        "max_scratch_bytes": 1 << 20, "network": "NONE", "gpu": "NONE",
        "runtimes": ["python-deterministic-v1"],
        "cancel_method": "user abandons the run inside the service UI",
        "complete_method": "user downloads the result dir; app imports"}


def entitlement(**overrides):
    args = dict(usage_path="CODE_RUNTIME_FILE_PACKET",
                account_binding=BINDING,
                allowance={"subscription_units": 100,
                           "compute_units": 0, "api_credits": 0,
                           "usd": 0, "handoff_minutes": "UNLIMITED"},
                inclusion={"execution_in_subscription": "CONFIRMED",
                           "note": "declared by the account owner"},
                service_caps=dict(CAPS))
    args.update(overrides)
    return make_entitlement(SERVICE, **args)


def subscription_plan(**kw):
    return make_plan(
        [operation("op-a", [0, 4], "SUBSCRIPTION_CODE_RUNTIME", WID),
         operation("op-b", [4, 8], "SUBSCRIPTION_CODE_RUNTIME", WID)],
        transfer_route="MANUAL_PACKET", **kw)


def subscription_evidence(scope, *, worker=WID, env_extra=None,
                          state="QUALIFIED_FOR_SCOPE"):
    env = {"worker_id": worker, "service": SERVICE,
           "session_id": "sess-forecast", "device": "box-forecast",
           "os": "linux-test", "runtime": "python-deterministic-v1",
           "network": "NONE", "gpu": "NONE"}
    env.update(env_extra or {})
    probe = {"registry_state": state,
             "qualification": "forecast-fixture",
             "reason": "synthetic probe for forecast tests",
             "environment": env, "operation": "compose_frame_range",
             "scope": dict(scope), "caps": dict(CAPS)}
    return capability_evidence("QUALIFIED_SERVICE", probe)


def complete_measurement(evidence, plan, candidate, *, usage=None):
    return make_measurement(
        evidence, input_digest=plan["snapshot_digest"],
        quality_digest=candidate.get("recipe_digest") or
        plan["snapshot_digest"],
        scope=dict(candidate["scope"]),
        cold={"end_to_end_ms": 1200, "startup_ms": 40,
              "auth_ms": 0, "queue_ms": 0},
        warm={"end_to_end_ms": 120},
        stage_timeline=[{"stage": "compose", "ms": 1000},
                        {"stage": "encode", "ms": 200}],
        shared_edge={"edge_id": "e-in", "bytes": 4096, "ms": 12},
        peaks={"disk_bytes": 1 << 20, "ram_bytes": 1 << 21,
               "vram_bytes": 0},
        transfer={"read_bytes": 4096, "write_bytes": 8192,
                  "requests": 3},
        cache={"hits": 2, "misses": 4},
        manual={"minutes": 1},
        usage=usage or {"subscription_units":
                        candidate["scope"]["frames"],
                        "compute_units": 0, "api_credits": 0, "usd": 0,
                        "handoff_minutes": 1},
        samples={"count": 1, "window_ms": 1200,
                 "min_ms": 1200, "max_ms": 1200})


def value(row, section, key):
    return row[section][key]["value"]


# --- breakdown rows + provenance ------------------------------------------------

def test_forecast_rows_label_every_line(tmp_path):
    """Each forecast line carries the measurement environment and scope
    it came from — or UNKNOWN."""
    plan = subscription_plan()
    candidates = {c["candidate_id"]: c for c in plan_candidates(plan)}
    state_dir = tmp_path / "state"
    evidence, measurements = {}, {}
    for op in plan["operations"]:
        cand = candidates[op["operation_id"]]
        ev = subscription_evidence(cand["scope"])
        register(state_dir, ev)
        record = complete_measurement(ev, plan, cand)
        save_measurement(state_dir, record)
        evidence[op["operation_id"]] = ev
        measurements[op["operation_id"]] = record
    forecast = forecast_plan(
        plan, state_dir=state_dir,
        observation=ps._observation_for(evidence["op-a"]))
    assert forecast["document_type"] == "resource_forecast"
    assert forecast["plan"]["policy"] == "FIXED_ROUTE"
    for row in forecast["operations"]:
        if row["kind"] != "operation":
            continue
        cold = row["time_ms"]["cold"]
        assert cold["basis"] == "MEASURED" and cold["value"] == 1200
        source = cold["source"]
        assert source["kind"] == "CAP-MEASURE"
        # Both ops measured the same scope under one evidence record —
        # the bound id is one of the registered records, never a guess.
        assert source["measurement_id"] in {
            m["measurement_id"] for m in measurements.values()}
        assert source["environment"]["device"] == "box-forecast"
        assert source["scope"]["frames"] == 4
        assert value(row, "peaks", "ram_bytes") == 1 << 21
        assert value(row, "transfer", "read_bytes") == 4096
        assert value(row, "output", "decoded_frame_bytes") == 4 * 8 * 6 * 4
        assert row["output"]["decoded_frame_bytes"]["basis"] == "DERIVED"
        assert value(row, "output", "encoded_bytes") == 8192
    totals = forecast["totals"]
    # The encode candidate was never measured: totals stay UNKNOWN with
    # the measured part reported, never rounded into a number.
    assert totals["cpu_ms"]["value"] == "UNKNOWN"
    assert totals["cpu_ms"]["measured"] == 2400
    assert totals["cpu_ms"]["missing"] == ["encode:FFMPEG"]
    assert totals["transfer"]["requests"]["value"] == "UNKNOWN"
    assert totals["transfer"]["requests"]["measured"] == 6
    assert totals["gpu_ms"]["value"] == "UNKNOWN"
    assert forecast["facets"] == {
        "qualification_state": "UNQUALIFIED",
        "acceptance_state": "PENDING",
        "release_state": "NOT_AUTHORIZED",
        "node_state": "IN_PROGRESS"}
    assert "nothing executes" in forecast["boundary"]


def test_no_measurement_means_unknown_not_guess(tmp_path):
    plan = subscription_plan()
    forecast = forecast_plan(plan, evidence_list=[], observation=None)
    for row in forecast["operations"]:
        assert value(row, "time_ms", "cold") == "UNKNOWN"
        assert value(row, "peaks", "ram_bytes") == "UNKNOWN"
        assert row["selectable"] is False
    assert set(forecast["unknowns"]) == {
        r["candidate_id"] for r in forecast["operations"]}


# --- stale vs current device -----------------------------------------------------

def test_stale_measurement_is_never_the_current_device(tmp_path):
    """A bound measurement's numbers are reported only while its
    evidence is current under the observation; STALE ⇒ UNKNOWN lines
    with the stale provenance still shown."""
    plan = subscription_plan()
    candidates = {c["candidate_id"]: c for c in plan_candidates(plan)}
    state_dir = tmp_path / "state"
    cand = candidates["op-a"]
    ev = subscription_evidence(cand["scope"])
    register(state_dir, ev)
    save_measurement(state_dir, complete_measurement(ev, plan, cand))

    stale = forecast_plan(plan, state_dir=state_dir, observation=None)
    row = next(r for r in stale["operations"]
               if r["candidate_id"] == "op-a")
    assert row["registry_state"] == "STALE"
    assert row["selectable"] is False
    cold = row["time_ms"]["cold"]
    assert cold["value"] == "UNKNOWN" and cold["basis"] == "STALE"
    # Provenance is still shown — the record exists, it is just not the
    # current device/session.
    assert cold["source"]["measurement_id"].startswith("CM-")
    assert cold["source"]["environment"]["device"] == "box-forecast"
    assert cold["source"]["environment"]["session_id"] == "sess-forecast"

    # A different device/session never inherits the past measurement.
    other = copy.deepcopy(ev["environment"])
    other["device"] = "other-box"
    other["session_id"] = "sess-other"
    moved = forecast_plan(plan, state_dir=state_dir,
                          observation={"environment": other})
    assert value(next(r for r in moved["operations"]
                      if r["candidate_id"] == "op-a"),
                 "time_ms", "cold") == "UNKNOWN"

    # Under the matching current observation the same record is usable.
    current = forecast_plan(plan, state_dir=state_dir,
                            observation=ps._observation_for(ev))
    assert value(next(r for r in current["operations"]
                      if r["candidate_id"] == "op-a"),
                 "time_ms", "cold") == 1200


def test_stale_sibling_measurement_never_reads_as_measured():
    """A QUALIFIED probe with no bound measurement lifts the verdict to
    QUALIFIED_FOR_SCOPE; a sibling evidence that is not current under
    the observation may still supply its record as provenance, but its
    numbers are STALE — never MEASURED, never a COVERED usage map."""
    plan = subscription_plan()
    candidates = {c["candidate_id"]: c for c in plan_candidates(plan)}
    scope = candidates["op-a"]["scope"]
    current_ev = subscription_evidence(scope)
    old_ev = subscription_evidence(
        scope, env_extra={"device": "old-box", "session_id": "sess-old"})
    assert old_ev["evidence_id"] != current_ev["evidence_id"]
    usage = {"subscription_units": 2, "compute_units": 0,
             "api_credits": 0, "usd": 0, "handoff_minutes": 1}
    stale_records = [complete_measurement(old_ev, plan, candidates[op],
                                          usage=usage)
                     for op in ("op-a", "op-b")]
    forecast = forecast_plan(
        plan, evidence_list=[old_ev, current_ev],
        measurement_list=stale_records, entitlements=[entitlement()],
        observation=ps._observation_for(current_ev))
    for row in forecast["operations"]:
        if row["kind"] != "operation":
            continue
        assert row["registry_state"] == "QUALIFIED_FOR_SCOPE"
        assert row["evidence_id"] == current_ev["evidence_id"]
        assert row["measurement_id"] == stale_records[0]["measurement_id"]
        assert row["selectable"] is False
        lines = [row["time_ms"]["cold"], row["time_ms"]["warm"],
                 row["manual_minutes"], *row["peaks"].values(),
                 *row["transfer"].values(), *row["cache"].values(),
                 *row["usage"].values()]
        assert set(row["usage"]) == set(usage)
        for line in lines:
            assert line["basis"] == "STALE" and line["value"] == "UNKNOWN"
            assert line["source"]["evidence_id"] == old_ev["evidence_id"]
            assert line["source"]["environment"]["device"] == "old-box"
    totals = forecast["totals"]
    assert totals["cpu_ms"]["value"] == "UNKNOWN"
    assert totals["usage"]["subscription_units"]["value"] == "UNKNOWN"
    section = forecast["subscription"][SERVICE]
    assert section["charge"]["verdict"] == "USAGE_UNKNOWN"
    assert section["cost_estimate"]["basis"] != "MEASURED"
    assert "USAGE_UNKNOWN" in {r["code"] for r in forecast["refusals"]}

    # The verdict's own QUALIFIED evidence with its complete measurement
    # is still the MEASURED path.
    own = [complete_measurement(current_ev, plan, candidates[op],
                                usage=usage) for op in ("op-a", "op-b")]
    measured = forecast_plan(
        plan, evidence_list=[old_ev, current_ev],
        measurement_list=stale_records + own,
        entitlements=[entitlement()],
        observation=ps._observation_for(current_ev))
    for row in measured["operations"]:
        if row["kind"] != "operation":
            continue
        assert row["measurement_id"] == own[0]["measurement_id"]
        assert row["time_ms"]["cold"]["basis"] == "MEASURED"
        assert value(row, "time_ms", "cold") == 1200
        assert row["peaks"]["disk_bytes"]["basis"] == "MEASURED"
        assert row["usage"]["subscription_units"]["basis"] == "MEASURED"
        assert row["time_ms"]["cold"]["source"]["environment"][
            "device"] == "box-forecast"
    assert measured["subscription"][SERVICE]["charge"]["verdict"] == \
        "COVERED"


def test_expired_capability_is_stale(tmp_path):
    plan = subscription_plan()
    candidates = {c["candidate_id"]: c for c in plan_candidates(plan)}
    state_dir = tmp_path / "state"
    cand = candidates["op-a"]
    ev = subscription_evidence(
        cand["scope"], env_extra={"expires_at_ms": 1000})
    register(state_dir, ev)
    save_measurement(state_dir, complete_measurement(ev, plan, cand))
    forecast = forecast_plan(plan, state_dir=state_dir, now_ms=2000,
                             observation=ps._observation_for(ev))
    row = next(r for r in forecast["operations"]
               if r["candidate_id"] == "op-a")
    assert row["registry_state"] == "STALE"
    assert any("expired" in str(r) for r in row["reasons"])
    assert value(row, "time_ms", "cold") == "UNKNOWN"


# --- disk shortfall --------------------------------------------------------------

def test_disk_shortfall_refuses_and_proposes(tmp_path):
    plan = make_plan(
        [operation("op-a", [0, 4], "LOCAL_NATIVE", "w1"),
         operation("op-b", [4, 8], "LOCAL_NATIVE", "w1")],
        edges=[])
    per_frame = 8 * 6 * 4          # 192 derived bytes/frame
    forecast = forecast_plan(
        plan, evidence_list=[],
        disk={"USER_DESKTOP": {"free_bytes": per_frame * 2,
                               "source": "fixture observation"}})
    assert forecast["workspace"]["shortfalls"]
    refusal = next(r for r in forecast["refusals"]
                   if r["code"] == "DISK_SHORTFALL")
    assert "free_bytes" in refusal["reason"]
    kinds = [a["kind"] for a in forecast["alternatives"]]
    assert "CHUNK_RANGES" in kinds and "LOWER_QUALITY" in kinds
    chunk = next(a for a in forecast["alternatives"]
                 if a["kind"] == "CHUNK_RANGES")
    # Two derived frames fit per chunk: [0,2) [2,4) [4,6) [6,8).
    assert [c["frame_range"] for c in chunk["ranges"]] == \
        [[0, 2], [2, 4], [4, 6], [6, 8]]
    assert all(a["executes"].startswith("NEVER")
               for a in forecast["alternatives"])


def test_unobserved_disk_is_unknown_not_assumed():
    forecast = forecast_plan(make_plan(
        [operation("op-a", [0, 4], "LOCAL_NATIVE", "w1")], edges=[],
        output_frames=4),
        evidence_list=[], disk=None)
    check = forecast["workspace"]["reservations"][0]
    assert check["sufficient"] == "UNKNOWN"
    assert check["free_bytes"]["value"] == "UNKNOWN"
    assert "DISK_SHORTFALL" not in {
        r["code"] for r in forecast["refusals"]}


def test_measured_zero_disk_peak_stays_measured(tmp_path):
    """A measured disk peak of 0 is a real measurement — only a
    missing one is UNKNOWN."""
    plan = subscription_plan()
    candidates = {c["candidate_id"]: c for c in plan_candidates(plan)}
    state_dir = tmp_path / "state"
    last = None
    for op in plan["operations"]:
        cand = candidates[op["operation_id"]]
        ev = subscription_evidence(cand["scope"])
        register(state_dir, ev)
        record = complete_measurement(ev, plan, cand)
        record["peaks"]["disk_bytes"] = 0
        save_measurement(state_dir, record)
        last = ev
    forecast = forecast_plan(plan, state_dir=state_dir,
                             observation=ps._observation_for(last))
    capacity = forecast["workspace"]["capacity"]
    assert capacity["measured_peak_disk_bytes"]["value"] == 0
    assert capacity["measured_peak_disk_bytes"]["basis"] == "MEASURED"
    # The encode candidate was never measured, so the cap check over a
    # partial set of peaks stays UNKNOWN.
    assert capacity["measured_peak_disk_bytes"]["missing"] == \
        ["encode:FFMPEG"]
    assert capacity["peak_within_declared_caps"] == "UNKNOWN"


def test_peak_within_caps_needs_every_disk_peak():
    """peak_within_declared_caps is only true/false when every candidate
    row holds a measured disk peak — a partial set is UNKNOWN."""
    plan = subscription_plan()
    cap = plan["workspace"]["pc_cache_limit_bytes"] \
        + plan["workspace"]["worker_scratch_limit_bytes"]

    def rows(*peaks):
        return [{"candidate_id": f"c{i}",
                 "peaks": {"disk_bytes": {"value": peak}}}
                for i, peak in enumerate(peaks)]

    def flag(*peaks):
        return _workspace_check(plan, rows(*peaks), None)[
            "capacity"]["peak_within_declared_caps"]

    assert flag(0, cap) is True
    assert flag(0, cap + 1) is False
    assert flag(0, "UNKNOWN") == "UNKNOWN"
    assert flag(cap + 1, "UNKNOWN") == "UNKNOWN"
    assert flag("UNKNOWN") == "UNKNOWN"


def test_decoded_frame_bytes_counts_encoder_input_once():
    """The plan-level decoded total is the frames the compose
    operations produce — the encode candidate consumes those same
    frames and must not count them twice."""
    plan = subscription_plan()
    forecast = forecast_plan(plan, evidence_list=[])
    per_frame = 8 * 6 * 4
    encode = next(r for r in forecast["operations"]
                  if r["kind"] == "encoder")
    # The per-row value is still the encoder's full input — the total
    # is the two 4-frame operations' output, counted once.
    assert encode["output"]["decoded_frame_bytes"]["value"] == \
        8 * per_frame
    assert forecast["totals"]["output"]["decoded_frame_bytes"][
        "value"] == 8 * per_frame


# --- subscription: inclusion / quote / extra charge ------------------------------

def test_subscription_covered_and_allowance_exhausted(tmp_path):
    plan = subscription_plan()
    candidates = {c["candidate_id"]: c for c in plan_candidates(plan)}
    state_dir = tmp_path / "state"
    last = None
    for op in plan["operations"]:
        cand = candidates[op["operation_id"]]
        ev = subscription_evidence(cand["scope"])
        register(state_dir, ev)
        save_measurement(state_dir, complete_measurement(
            ev, plan, cand,
            usage={"subscription_units": 2, "compute_units": 0,
                   "api_credits": 0, "usd": 0, "handoff_minutes": 1}))
        last = ev
    covered = forecast_plan(
        plan, state_dir=state_dir, entitlements=[entitlement()],
        observation=ps._observation_for(last))
    section = covered["subscription"][SERVICE]
    assert section["inclusion"] == "CONFIRMED"
    assert section["charge"]["verdict"] == "COVERED"
    assert section["cost_estimate"]["basis"] == "MEASURED"
    assert section["cost_estimate"]["units"] == {
        "subscription_units": 4, "compute_units": 0, "api_credits": 0,
        "usd": 0, "handoff_minutes": 2}
    assert section["cost_estimate"]["unmeasured_paid_units"] == []
    assert section["quote"]["status"] == "CURRENT"

    # With no measured usage the declared frame estimate can never
    # prove there is no extra charge — the verdict is USAGE_UNKNOWN
    # and the forecast refuses.
    unknown = forecast_plan(plan, evidence_list=[],
                            entitlements=[entitlement()])
    section = unknown["subscription"][SERVICE]
    assert section["charge"]["verdict"] == "USAGE_UNKNOWN"
    assert section["cost_estimate"]["basis"] == "DECLARED"
    assert section["charge"]["declared_estimate_verdict"]["verdict"] \
        == "COVERED"
    assert "not measured" in section["charge"]["reason"]
    assert "USAGE_UNKNOWN" in {
        r["code"] for r in unknown["refusals"]}

    exhausted = forecast_plan(
        plan, evidence_list=[],
        entitlements=[entitlement(
            allowance={"subscription_units": 4, "compute_units": 0,
                       "api_credits": 0, "usd": 0,
                       "handoff_minutes": "UNLIMITED"})])
    section = exhausted["subscription"][SERVICE]
    assert section["charge"]["verdict"] == "ALLOWANCE_EXHAUSTED"
    assert "no reroute" in section["charge"]["reason"]
    # A refusal verdict stays as-is; its reason still notes that the
    # usage itself was never measured.
    assert "not measured" in section["charge"]["reason"]
    assert "ALLOWANCE_EXHAUSTED" in {
        r["code"] for r in exhausted["refusals"]}
    alt = next(a for a in exhausted["alternatives"]
               if a["kind"] == "ROUTE_OR_CHUNK")
    assert alt["paid_fallback_auto_run"] is False
    assert alt["executes"].startswith("NEVER")


def test_stale_paid_usage_is_usage_unknown(tmp_path):
    """A stale measurement's paid units are unaccounted for — the
    declared frame estimate can never read as COVERED."""
    plan = subscription_plan()
    candidates = {c["candidate_id"]: c for c in plan_candidates(plan)}
    state_dir = tmp_path / "state"
    for op in plan["operations"]:
        cand = candidates[op["operation_id"]]
        ev = subscription_evidence(cand["scope"])
        register(state_dir, ev)
        save_measurement(state_dir, complete_measurement(
            ev, plan, cand,
            usage={"subscription_units": 1, "compute_units": 0,
                   "api_credits": 3, "usd": 0, "handoff_minutes": 1}))
    # No current observation: the bound measurements are STALE, so the
    # usage values are UNKNOWN — every paid unit included.
    forecast = forecast_plan(plan, state_dir=state_dir,
                             entitlements=[entitlement()],
                             observation=None)
    section = forecast["subscription"][SERVICE]
    assert section["charge"]["verdict"] == "USAGE_UNKNOWN"
    assert "STALE" in section["charge"]["reason"]
    assert section["charge"]["declared_estimate_verdict"]["verdict"] \
        == "COVERED"
    assert section["cost_estimate"]["unmeasured_paid_units"] == \
        ["api_credits", "usd"]
    assert "USAGE_UNKNOWN" in {
        r["code"] for r in forecast["refusals"]}


def register_ops(state_dir, plan, usage=None):
    """Register evidence + a measurement for every subscription op;
    returns the last evidence for a current observation."""
    candidates = {c["candidate_id"]: c for c in plan_candidates(plan)}
    ev = None
    for op in plan["operations"]:
        cand = candidates[op["operation_id"]]
        ev = subscription_evidence(cand["scope"])
        register(state_dir, ev)
        save_measurement(state_dir, complete_measurement(
            ev, plan, cand, usage=usage))
    return ev


def test_partial_usage_units_are_usage_unknown(tmp_path):
    """A usage map that omits allowance units is an incomplete record —
    the absent paid units are unmeasured, never an implied 0, so the
    section can never read as COVERED."""
    plan = subscription_plan()
    state_dir = tmp_path / "state"
    ev = register_ops(state_dir, plan,
                      usage={"subscription_units": 2,
                             "handoff_minutes": 1})
    forecast = forecast_plan(
        plan, state_dir=state_dir, entitlements=[entitlement()],
        observation=ps._observation_for(ev))
    section = forecast["subscription"][SERVICE]
    assert section["charge"]["verdict"] == "USAGE_UNKNOWN"
    assert section["cost_estimate"]["basis"] == "DECLARED"
    assert section["cost_estimate"]["unmeasured_paid_units"] == \
        ["api_credits", "usd"]
    assert "missing" in section["charge"]["reason"]
    assert "USAGE_UNKNOWN" in {
        r["code"] for r in forecast["refusals"]}


def test_measured_paid_units_refused_on_fixed_route(tmp_path):
    """A complete map with paid units reaches charge_check, which
    refuses them while the plan keeps allow_additional_charges=false."""
    plan = subscription_plan()
    assert plan["execution"]["policy"] == "FIXED_ROUTE"
    assert plan["execution"]["allow_additional_charges"] is False
    state_dir = tmp_path / "state"
    ev = register_ops(state_dir, plan,
                      usage={"subscription_units": 2, "compute_units": 0,
                             "api_credits": 3, "usd": 0,
                             "handoff_minutes": 1})
    forecast = forecast_plan(
        plan, state_dir=state_dir, entitlements=[entitlement()],
        observation=ps._observation_for(ev))
    section = forecast["subscription"][SERVICE]
    assert section["cost_estimate"]["basis"] == "MEASURED"
    assert section["cost_estimate"]["units"]["api_credits"] == 6
    assert section["paid_units_in_cost"] == ["api_credits"]
    assert section["charge"]["verdict"] == "ADDITIONAL_CHARGE_REFUSED"
    assert "ADDITIONAL_CHARGE_REFUSED" in {
        r["code"] for r in forecast["refusals"]}


def test_totals_usage_reports_missing_unit_unknown(tmp_path):
    """Every allowance unit appears in totals.usage; a unit missing in
    any row is UNKNOWN — never an implied 0, never omitted."""
    plan = subscription_plan()
    state_dir = tmp_path / "state"
    ev = register_ops(state_dir, plan,
                      usage={"subscription_units": 2,
                             "handoff_minutes": 1})
    forecast = forecast_plan(plan, state_dir=state_dir,
                             observation=ps._observation_for(ev))
    usage = forecast["totals"]["usage"]
    assert set(usage) == {"subscription_units", "compute_units",
                          "api_credits", "usd", "handoff_minutes"}
    credits = usage["api_credits"]
    assert credits["value"] == "UNKNOWN"
    assert credits["basis"] == "UNKNOWN"
    assert {"op-a", "op-b"} <= set(credits["missing"])
    assert credits["unmeasured_bases"] == ["missing"]
    # The ops measured subscription_units; only the never-measured
    # encode candidate keeps that total UNKNOWN.
    units = usage["subscription_units"]
    assert units["value"] == "UNKNOWN"
    assert units["measured"] == 4
    assert units["missing"] == ["encode:FFMPEG"]


def test_measured_zero_peak_is_not_never_measured(tmp_path):
    """A measured peak of 0 alongside unmeasured candidates is a partial
    max of 0 — not "never measured for any candidate"."""
    plan = subscription_plan()
    state_dir = tmp_path / "state"
    ev = register_ops(state_dir, plan)
    forecast = forecast_plan(plan, state_dir=state_dir,
                             observation=ps._observation_for(ev))
    vram = forecast["totals"]["peaks"]["vram_bytes"]
    assert vram["value"] == "UNKNOWN"
    assert vram["measured_max"] == 0
    assert vram["partial"] is True
    assert vram["missing"] == ["encode:FFMPEG"]


def test_partial_usage_map_is_usage_unknown(tmp_path):
    """One measured sibling row does not cover a sibling with no usage
    map at all — the section is still USAGE_UNKNOWN."""
    # op-b's scope (6 frames) is not covered by op-a's measurement
    # (4 frames), so its row carries no usage map at all.
    plan = make_plan(
        [operation("op-a", [0, 4], "SUBSCRIPTION_CODE_RUNTIME", WID),
         operation("op-b", [4, 10], "SUBSCRIPTION_CODE_RUNTIME", WID)],
        transfer_route="MANUAL_PACKET", output_frames=10)
    candidates = {c["candidate_id"]: c for c in plan_candidates(plan)}
    state_dir = tmp_path / "state"
    cand = candidates["op-a"]
    ev = subscription_evidence(cand["scope"])
    register(state_dir, ev)
    save_measurement(state_dir, complete_measurement(ev, plan, cand))
    forecast = forecast_plan(
        plan, state_dir=state_dir, entitlements=[entitlement()],
        observation=ps._observation_for(ev))
    section = forecast["subscription"][SERVICE]
    assert section["charge"]["verdict"] == "USAGE_UNKNOWN"
    assert "missing" in section["charge"]["reason"]
    assert section["charge"]["declared_estimate_verdict"]["verdict"] \
        == "COVERED"
    assert "USAGE_UNKNOWN" in {
        r["code"] for r in forecast["refusals"]}


def test_unknown_inclusion_is_never_free(tmp_path):
    plan = subscription_plan()
    forecast = forecast_plan(
        plan, evidence_list=[],
        entitlements=[entitlement(inclusion={
            "execution_in_subscription": "UNKNOWN",
            "note": "not yet confirmed"})])
    section = forecast["subscription"][SERVICE]
    assert section["charge"]["verdict"] == "INCLUSION_UNKNOWN"
    assert "never" in section["charge"]["reason"]
    assert "INCLUSION_UNKNOWN" in {
        r["code"] for r in forecast["refusals"]}
    # An unregistered service is also never shown as covered.
    other = make_plan(
        [operation("op-x", [0, 4], "SUBSCRIPTION_CODE_RUNTIME",
                   "subscription:ghost-svc:deadbeef")],
        transfer_route="MANUAL_PACKET", output_frames=4)
    ghost = forecast_plan(other, evidence_list=[])
    assert ghost["subscription"]["ghost-svc"]["entitlement"] == \
        "UNREGISTERED"
    assert ghost["subscription"]["ghost-svc"]["charge"]["verdict"] == \
        "INCLUSION_UNKNOWN"


def test_paid_fallback_never_auto_runs(tmp_path):
    """Measured paid usage + allow_additional_charges still refuses
    without a separate approval — and the forecast consumes nothing."""
    plan = subscription_plan(allow_charges=True)
    candidates = {c["candidate_id"]: c for c in plan_candidates(plan)}
    state_dir = tmp_path / "state"
    for op in plan["operations"]:
        cand = candidates[op["operation_id"]]
        ev = subscription_evidence(cand["scope"])
        register(state_dir, ev)
        save_measurement(state_dir, complete_measurement(
            ev, plan, cand,
            usage={"subscription_units": 1, "compute_units": 0,
                   "api_credits": 3, "usd": 0, "handoff_minutes": 1}))
    ent = entitlement()
    before = copy.deepcopy(ent)
    forecast = forecast_plan(
        plan, state_dir=state_dir, entitlements=[ent],
        observation=ps._observation_for(ev))
    section = forecast["subscription"][SERVICE]
    assert section["cost_estimate"]["basis"] == "MEASURED"
    assert section["cost_estimate"]["units"]["api_credits"] == 6
    assert section["paid_units_in_cost"] == ["api_credits"]
    assert section["charge"]["verdict"] == "ADDITIONAL_CHARGE_REFUSED"
    assert "separate explicit approval" in section["charge"]["reason"]
    alt = next(a for a in forecast["alternatives"]
               if a["kind"] == "EXPLICIT_CHARGE_APPROVAL")
    assert alt["paid_fallback_auto_run"] is False
    # Nothing was consumed or executed.
    assert ent == before


def test_quote_change_after_issuance(tmp_path):
    plan = subscription_plan()
    ent = entitlement()
    issued = {"packet_id": "pkt-fixture",
              "entitlement": {"service": SERVICE,
                              "quote_digest": "0" * 64}}
    forecast = forecast_plan(plan, evidence_list=[],
                             entitlements=[ent],
                             issued_packets=[issued])
    quote = forecast["subscription"][SERVICE]["quote"]
    assert quote["status"] == "CHANGED"
    assert quote["quote_digest"] == quote_digest(ent)
    assert "QUOTE_CHANGED" in {r["code"] for r in forecast["refusals"]}
    assert "REQUOTE" in {a["kind"] for a in forecast["alternatives"]}
    # The digest alone (no change) reports CURRENT.
    same = forecast_plan(plan, evidence_list=[], entitlements=[ent],
                         prior_quotes={SERVICE: quote_digest(ent)})
    assert same["subscription"][SERVICE]["quote"]["status"] == "CURRENT"


def test_expired_entitlement_refuses():
    plan = subscription_plan()
    forecast = forecast_plan(
        plan, evidence_list=[], now_ms=2 ** 62,
        entitlements=[entitlement(expires_at_ms=1000)])
    section = forecast["subscription"][SERVICE]
    assert section["entitlement"] == "EXPIRED"
    assert "ENTITLEMENT_EXPIRED" in {
        r["code"] for r in forecast["refusals"]}
    assert "RENEW_ENTITLEMENT" in {
        a["kind"] for a in forecast["alternatives"]}


def test_load_entitlements_skips_symlinks(tmp_path):
    """Only real files inside subscriptions/ are read — a symlink or a
    path resolving outside the directory is never followed."""
    state_dir = tmp_path / "state"
    save_entitlement(state_dir, entitlement())
    ghost = make_entitlement(
        "ghost-svc", usage_path="CODE_RUNTIME_FILE_PACKET",
        account_binding=hashlib.sha256(b"other-account").hexdigest())
    write_canon(tmp_path / "ghost.json", ghost)
    (state_dir / "subscriptions" / "ghost-svc.json").symlink_to(
        tmp_path / "ghost.json")
    loaded = _load_entitlements(state_dir)
    assert [e["service"] for e in loaded] == [SERVICE]


def test_load_entitlements_skips_symlinked_dir(tmp_path):
    """A symlinked subscriptions/ dir is never followed — its target's
    files are not read."""
    real = tmp_path / "elsewhere"
    save_entitlement(real, entitlement())
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "subscriptions").symlink_to(real / "subscriptions")
    assert _load_entitlements(real) != []
    assert _load_entitlements(state_dir) == []


# --- AUTO / FIXED_ROUTE and the preview --------------------------------------------

def test_auto_plan_refuses_without_current_measurement(tmp_path):
    p = perf_project(tmp_path)
    plan = ps.make_perf_plan(p)
    forecast = forecast_plan(plan, evidence_list=[], observation=None)
    assert forecast["plan"]["policy"] == "AUTO_PERFORMANCE"
    assert "AUTO_NO_CANDIDATES" in {
        r["code"] for r in forecast["refusals"]}
    alt = next(a for a in forecast["alternatives"]
               if a["kind"] == "RE_MEASURE")
    assert "perf-benchmark" in alt["proposal"]
    # The plan preview carries identity + route even with no evidence.
    assert forecast["plan"]["snapshot_digest"] == \
        plan["snapshot_digest"]
    assert len(forecast["operations"]) == \
        len(plan["operations"]) + len(
            plan["encoding"]["allowed_drivers"])


def test_measured_rows_from_real_benchmark(tmp_path):
    """A real registered benchmark feeds MEASURED lines under a current
    observation; VRAM was never measured and stays UNKNOWN."""
    p = perf_project(tmp_path)
    report = ps.run_benchmark(
        p, work_root=tmp_path / "work", runs=1)
    registration = ps.register_benchmark(
        tmp_path / "registry", report)
    plan = registration["plan"]
    forecast = forecast_plan(
        plan, state_dir=tmp_path / "registry",
        observation=ps._observation_for(
            __import__("engine.capability_registry",
                       fromlist=["entries"]).entries(
                           tmp_path / "registry")[0]))
    measured = [r for r in forecast["operations"]
                if r["time_ms"]["cold"]["basis"] == "MEASURED"]
    assert len(measured) == 3          # the three compose ops
    for row in measured:
        assert value(row, "peaks", "vram_bytes") == "UNKNOWN"
        assert row["selectable"] is False   # incomplete measurement
        source = row["time_ms"]["cold"]["source"]
        assert source["environment"]["worker_id"] == "perf-local"
        assert source["scope"]["operation"] == "compose_frame_range"
    # The encoder was never benchmarked: the total stays UNKNOWN but the
    # measured part is reported honestly, never rounded into a number.
    assert forecast["totals"]["cpu_ms"]["value"] == "UNKNOWN"
    assert forecast["totals"]["cpu_ms"]["measured"] > 0
    encode = next(r for r in forecast["operations"]
                  if r["kind"] == "encoder")
    assert value(encode, "time_ms", "cold") == "UNKNOWN"


# --- CLI ----------------------------------------------------------------------------

def test_cli_resource_forecast(tmp_path, capsys):
    p = perf_project(tmp_path)
    assert cli.main(["resource-forecast", str(p),
                     "--no-observe"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["document_type"] == "resource_forecast"
    assert out["plan"]["policy"] == "AUTO_PERFORMANCE"
    assert len(out["operations"]) == 4    # 3 ops + 1 encode candidate
    assert out["facets"]["release_state"] == "NOT_AUTHORIZED"
    check = out["workspace"]["reservations"][0]
    assert check["sufficient"] is True    # real disk observed
    # --plan reads an explicit ExecutionPlan file.
    plan_path = tmp_path / "plan.json"
    write_canon(plan_path, validate_execution_plan(
        ps.make_perf_plan(p)))
    assert cli.main(["resource-forecast", str(p), "--plan",
                     str(plan_path), "--no-observe"]) == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2["plan"]["plan_sha"] == out["plan"]["plan_sha"]


def test_cli_forecast_secret_free(tmp_path, capsys):
    p = perf_project(tmp_path)
    assert cli.main(["resource-forecast", str(p),
                     "--no-observe"]) == 0
    text = capsys.readouterr().out.lower()
    for token in ("api_key", "password", "access_token",
                  "credential_value", "bearer"):
        assert token not in text


def test_cli_forecast_refuses_legacy_project(tmp_path, capsys):
    """LEGACY_MV has no execution graph — the command refuses rather
    than inventing a forecast."""
    from test_compiler_v03 import fixture_project
    p = fixture_project(tmp_path)
    assert cli.main(["resource-forecast", str(p)]) != 0
    assert "LEGACY_MV" in capsys.readouterr().err
