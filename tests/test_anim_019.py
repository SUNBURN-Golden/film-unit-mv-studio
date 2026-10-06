"""ANIM-019: scope-bound capability registry — CAP-SCOPE, CAP-STALE,
CAP-MEASURE, axis separation, preflight and the ExecutionPlan gate.

Synthetic records only: no network, no credentials, no paid calls, no
real probes. Fixture/fake evidence stays DOCUMENTED_ONLY and facets stay
UNQUALIFIED/PENDING/NOT_AUTHORIZED.
"""
import copy
import hashlib
import json

import pytest

from anim_014_kit import local_plan, remote_plan

from engine import cli
from engine.animation_schema import (canon_bytes, read_canon,
                                     validate_capability_evidence,
                                     validate_capability_measurement)
from engine.capability_registry import (
    ALLOWANCE_UNITS, allowance_missing, axis_of, bound_measurement,
    current_state, entries, evidence_digest, is_fake, make_measurement,
    measurement_missing, measurements, plan_preflight, preflight,
    register, registry_status, save_measurement, scope_covers)
from engine.core import FilmError
from engine.encoder_backends import capability_evidence
from engine.execution_plan import assert_executable, write_plan

ACCOUNT = hashlib.sha256(b"anim-019-account-a").hexdigest()
ACCOUNT_B = hashlib.sha256(b"anim-019-account-b").hexdigest()
ADAPTER = hashlib.sha256(b"anim-019-adapter-1").hexdigest()
ADAPTER_B = hashlib.sha256(b"anim-019-adapter-2").hexdigest()
INPUT_SHA = hashlib.sha256(b"anim-019-input").hexdigest()
QUALITY_SHA = hashlib.sha256(b"anim-019-quality").hexdigest()

COMPOSE_SCOPE = {"operation": "compose_frame_range",
                 "runtime_contract": "python-deterministic-v1",
                 "route": "relay", "width": 8, "height": 6,
                 "pixel_format": "RGBA8", "frames": 8}

ENCODE_SCOPE = {"width": 8, "height": 6, "fps": {"num": 24, "den": 1},
                "frames": 8, "codec": "h264", "pixel_format": "yuv420p",
                "container": "mp4"}


def make_evidence(*, operation="compose_frame_range",
                  driver="QUALIFIED_SERVICE", scope=None, env=None,
                  state="QUALIFIED_FOR_SCOPE", account=ACCOUNT, epoch=1,
                  adapter=ADAPTER, route="relay", transport="local-spawn",
                  allowance=None, caps=None, fixture=None,
                  qualification="real-fixture", reason="probe passed"):
    """A fully-formed CapabilityEvidence 1 for tests."""
    environment = {"service": "svc-a", "worker_id": "w-1",
                   "session_id": "sess-1", "session_epoch": 1,
                   "device": "box-1", "os": "linux-6",
                   "runtime": "python-deterministic-v1",
                   "driver": "drv-1", "network": "NONE", "gpu": "NONE"}
    environment.update(env or {})
    doc = {
        "document_type": "capability_evidence", "schema_version": 1,
        "evidence_id": "", "driver": driver, "adapter_digest": adapter,
        "account_binding": account, "credential_epoch": epoch,
        "environment": environment, "operation": operation,
        "scope": scope if scope is not None else dict(COMPOSE_SCOPE),
        "caps": caps if caps is not None else {"job": {}},
        "route": route, "transport": transport, "fixture": fixture,
        "observed_at": "2026-10-01T00:00:00+00:00",
        "entitlement_basis": "test binding — secret-free",
        "allowance": allowance if allowance is not None
        else {unit: 0 for unit in ALLOWANCE_UNITS},
        "expiry": "STALE on any bound-field change or expiry",
        "registry_state": state, "qualification": qualification,
        "reason": reason}
    doc["evidence_id"] = "CE-" + hashlib.sha256(
        canon_bytes(doc)).hexdigest()[:16].upper()
    return validate_capability_evidence(doc)


def observation_for(evidence):
    """The fresh probe-shaped observation that changes nothing."""
    env = copy.deepcopy(evidence["environment"])
    obs = {"environment": env}
    for field in ("account_binding", "credential_epoch",
                  "adapter_digest", "driver", "operation", "route",
                  "transport", "caps", "allowance"):
        obs[field] = copy.deepcopy(evidence[field])
    return obs


def full_measurement(evidence, **patch):
    """A complete CAP-MEASURE record bound to `evidence`."""
    sections = {
        "cold": {"end_to_end_ms": 5000, "startup_ms": 1000,
                 "auth_ms": 200, "queue_ms": 300},
        "warm": {"end_to_end_ms": 3000},
        "stage_timeline": [{"stage": "decode", "ms": 100},
                           {"stage": "compose", "ms": 1500},
                           {"stage": "encode", "ms": 1400}],
        "shared_edge": {"edge_id": "e-out", "bytes": 4096, "ms": 120},
        "peaks": {"disk_bytes": 1 << 20, "ram_bytes": 1 << 22,
                  "vram_bytes": 0},
        "transfer": {"read_bytes": 4096, "write_bytes": 8192,
                     "requests": 3},
        "cache": {"hits": 2, "misses": 1},
        "manual": {"minutes": 0},
        "usage": {unit: 0 for unit in ALLOWANCE_UNITS},
        "samples": {"count": 3, "window_ms": 60000, "min_ms": 2900,
                    "max_ms": 3100}}
    for key, value in patch.items():
        sections[key] = value
    return make_measurement(evidence, input_digest=INPUT_SHA,
                            quality_digest=QUALITY_SHA,
                            scope=evidence["scope"], **sections)


def encode_evidence(scope=None, state="QUALIFIED_FOR_SCOPE",
                    driver="FFMPEG", env=None):
    """An ENCODE-axis record via the encoder_backends evidence builder."""
    probe_result = {
        "registry_state": state, "qualification": "real-fixture",
        "reason": "fixture encode+mux+verify passed",
        "environment": env if env is not None else
        {"worker_id": "w-enc", "session_id": "sess-e",
         "device": "box-1", "os": "linux-6",
         "driver": "ffmpeg-7.1", "network": "NONE", "gpu": "NONE"},
        "scope": scope if scope is not None else dict(ENCODE_SCOPE),
        "caps": {}, "fixture": {"input_sequence_root": INPUT_SHA,
                                "output_sha256": QUALITY_SHA,
                                "verification_sha256": QUALITY_SHA}}
    evidence = capability_evidence(driver, probe_result)
    return validate_capability_evidence(evidence)


def compose_candidate(scope=None, **kwargs):
    return {"candidate_id": "c1", "axis": "COMPOSE", "worker": "w-1",
            "scope": scope if scope is not None else dict(COMPOSE_SCOPE),
            **kwargs}


# --- registry storage + status (CAP-SCOPE) ------------------------------------

def test_register_roundtrip_and_status(tmp_path):
    state = tmp_path / "state"
    ev = make_evidence()
    path = register(state, ev)
    assert read_canon(path) == ev
    loaded = entries(state)
    assert loaded == [ev]
    status = registry_status(state)
    entry = status["entries"][0]
    assert entry["state"] == "QUALIFIED_FOR_SCOPE"
    assert entry["axis"] == "COMPOSE"
    assert entry["reasons"] == []
    assert status["facets"]["qualification_state"] == "UNQUALIFIED"
    assert status["facets"]["acceptance_state"] == "PENDING"
    assert status["facets"]["release_state"] == "NOT_AUTHORIZED"


def test_registry_states_and_reasons(tmp_path):
    state = tmp_path / "state"
    documented = make_evidence(state="DOCUMENTED_ONLY",
                               env={"worker_id": "w-doc"})
    unavailable = make_evidence(state="UNAVAILABLE", env={"worker_id":
                                                          "w-un"})
    for ev in (documented, unavailable):
        register(state, ev)
    status = registry_status(state)
    states = {e["evidence_id"]: e for e in status["entries"]}
    assert states[documented["evidence_id"]]["state"] == "DOCUMENTED_ONLY"
    assert "documented_only" in states[documented["evidence_id"]]["reasons"]
    assert states[unavailable["evidence_id"]]["state"] == "UNAVAILABLE"
    assert "probe_unavailable" in \
        states[unavailable["evidence_id"]]["reasons"]


def test_scope_cover_no_superset():
    ev = make_evidence(scope=dict(COMPOSE_SCOPE, frames=2))
    exact = dict(ev["scope"])
    assert scope_covers("COMPOSE", ev["scope"], exact)
    assert scope_covers("COMPOSE", ev["scope"], dict(exact, frames=1))
    # a 2-frame fixture never covers an 8-frame job (no superset)
    assert not scope_covers("COMPOSE", ev["scope"], dict(exact, frames=8))
    # a 720p fixture never covers 1080p
    assert not scope_covers("COMPOSE", ev["scope"],
                            dict(exact, width=1920, height=1080))
    # a key the probe never recorded fails closed
    assert not scope_covers("COMPOSE", ev["scope"],
                            dict(exact, scratch_bytes=1))


def test_encode_scope_requires_exact_codec_and_rate():
    small = dict(ENCODE_SCOPE, frames=2)
    ev = encode_evidence(scope=small)
    assert scope_covers("ENCODE", ev["scope"], dict(ENCODE_SCOPE,
                                                    frames=2))
    assert not scope_covers("ENCODE", ev["scope"], ENCODE_SCOPE)  # 8>2
    assert not scope_covers("ENCODE", ev["scope"],
                            dict(ENCODE_SCOPE, frames=2,
                                 codec="hevc"))
    assert not scope_covers("ENCODE", ev["scope"],
                            dict(ENCODE_SCOPE, frames=2,
                                 fps={"num": 30, "den": 1}))
    # rational equivalence still holds
    assert scope_covers("ENCODE", ev["scope"],
                        dict(ENCODE_SCOPE, frames=2,
                             fps={"num": 48, "den": 2}))


# --- CAP-STALE -----------------------------------------------------------------

@pytest.mark.parametrize("field", [
    "session_id", "session_epoch", "device", "os", "runtime", "driver",
    "worker_id", "network", "gpu"])
def test_env_binding_change_stales(field):
    ev = make_evidence()
    obs = observation_for(ev)
    obs["environment"][field] = "changed-" + field
    state = current_state(ev, obs)
    assert state["state"] == "STALE"
    assert f"environment.{field} changed" in state["reasons"]


@pytest.mark.parametrize("field", [
    "adapter_digest", "driver", "operation", "route", "transport"])
def test_document_binding_change_stales(field):
    ev = make_evidence()
    obs = observation_for(ev)
    obs[field] = "changed-" + field
    state = current_state(ev, obs)
    assert state["state"] == "STALE"
    assert f"{field} changed" in state["reasons"]


def test_account_binding_change_stales():
    ev = make_evidence()
    obs = observation_for(ev)
    obs["account_binding"] = ACCOUNT_B
    assert current_state(ev, obs)["state"] == "STALE"
    assert "account_binding changed" in current_state(ev, obs)["reasons"]


def test_credential_epoch_bump_stales():
    ev = make_evidence(epoch=1)
    obs = observation_for(ev)
    obs["credential_epoch"] = 2          # logout/revoke/rebind bumped it
    state = current_state(ev, obs)
    assert state["state"] == "STALE"
    assert "credential_epoch changed" in state["reasons"]


def test_caps_and_allowance_change_stales():
    ev = make_evidence(caps={"job": {"max_frames": 8}},
                       allowance={"api_credits": 5})
    obs = observation_for(ev)
    obs["caps"] = {"job": {"max_frames": 4}}
    assert "limits changed" in current_state(ev, obs)["reasons"]
    obs = observation_for(ev)
    obs["allowance"] = {"api_credits": 4}
    state = current_state(ev, obs)
    assert state["state"] == "STALE"
    assert "allowance changed" in state["reasons"]


def test_session_expiry_stales():
    ev = make_evidence(
        env={"session_expires_at_ms": 1000})
    assert current_state(ev, now_ms=999)["state"] == \
        "QUALIFIED_FOR_SCOPE"
    state = current_state(ev, now_ms=1000)
    assert state["state"] == "STALE"
    assert "session_expired" in state["reasons"]


def test_entitlement_expiry_stales():
    ev = make_evidence()
    obs = observation_for(ev)
    obs["expires_at_ms"] = 500
    assert current_state(ev, obs, now_ms=499)["state"] == \
        "QUALIFIED_FOR_SCOPE"
    state = current_state(ev, obs, now_ms=500)
    assert state["state"] == "STALE"
    assert "entitlement_expired" in state["reasons"]


def test_old_pass_never_extended_after_change(tmp_path):
    """An expired/changed PASS cannot slide a candidate back in."""
    state = tmp_path / "state"
    ev = make_evidence()
    register(state, ev)
    save_measurement(state, full_measurement(ev))
    candidate = compose_candidate()
    report = preflight([candidate], entries(state),
                       measurements(state), policy="AUTO_PERFORMANCE")
    assert report["candidates"][0]["selectable"]

    obs = observation_for(ev)
    obs["environment"]["device"] = "other-box"
    report = preflight([candidate], entries(state),
                       measurements(state), policy="AUTO_PERFORMANCE",
                       observation=obs)
    verdict = report["candidates"][0]
    assert not verdict["eligible"] and not verdict["selectable"]
    assert verdict["registry_state"] == "STALE"
    assert report["selections"] == []
    assert verdict["estimate"] == "UNKNOWN"


def test_fake_evidence_never_qualifies(tmp_path):
    state = tmp_path / "state"
    ev = make_evidence(state="QUALIFIED_FOR_SCOPE",
                       env={"fake_runtime": True},
                       qualification="FAKE — test double",
                       reason="fixture ran through the shipped script")
    register(state, ev)
    current = current_state(ev, observation_for(ev))
    assert current["state"] == "DOCUMENTED_ONLY"
    assert "fake_evidence" in current["reasons"]
    assert is_fake(ev)
    report = preflight([compose_candidate()], entries(state),
                       policy="AUTO_PERFORMANCE")
    assert report["candidates"][0]["registry_state"] == \
        "DOCUMENTED_ONLY"
    assert report["selections"] == []


# --- CAP-MEASURE ---------------------------------------------------------------

def test_complete_measurement_yields_estimate(tmp_path):
    state = tmp_path / "state"
    ev = make_evidence()
    register(state, ev)
    record = full_measurement(ev)
    path = save_measurement(state, record)
    assert read_canon(path) == record
    validate_capability_measurement(record)
    assert measurement_missing(record) == []
    assert bound_measurement(compose_candidate(), ev,
                             measurements(state)) == record
    report = preflight([compose_candidate()], entries(state),
                       measurements(state), policy="AUTO_PERFORMANCE")
    verdict = report["candidates"][0]
    assert verdict["eligible"] and verdict["selectable"]
    assert verdict["estimate"]["cold_ms"] == 5000
    assert verdict["estimate"]["warm_ms"] == 3000
    assert report["selections"][0]["label"] == "AUTO_QUALIFIED"


@pytest.mark.parametrize("cut", [
    {"cold": {"queue_ms": None}},
    {"warm": {}},
    {"stage_timeline": []},
    {"shared_edge": {"ms": 10}},                     # bytes missing
    {"peaks": {"disk_bytes": 1, "ram_bytes": 1}},    # vram missing
    {"transfer": {"read_bytes": 1, "write_bytes": 1}},
    {"cache": {"hits": 0}},                          # misses missing
    {"manual": {}},
    {"usage": {"subscription_units": 0}},            # other units absent
    {"samples": {"count": 0, "window_ms": 1, "min_ms": 1,
                 "max_ms": 1}},
])
def test_partial_measurement_stays_unknown(tmp_path, cut):
    state = tmp_path / "state"
    ev = make_evidence()
    register(state, ev)
    record = full_measurement(ev, **cut)
    save_measurement(state, record)
    missing = measurement_missing(record)
    assert missing
    report = preflight([compose_candidate()], entries(state),
                       measurements(state), policy="AUTO_PERFORMANCE")
    verdict = report["candidates"][0]
    assert verdict["eligible"]            # evidence is fine …
    assert not verdict["selectable"]      # … but timing is UNKNOWN
    assert verdict["estimate"] == "UNKNOWN"
    assert report["selections"] == []
    assert verdict["missing_measurement"] == missing


def test_measurement_detached_from_evidence_is_unusable(tmp_path):
    state = tmp_path / "state"
    ev = make_evidence()
    other = make_evidence(env={"worker_id": "w-9"})
    register(state, ev)
    register(state, other)
    # bound to `other`: never binds ev — id and digest must both match
    record = full_measurement(other)
    save_measurement(state, record)
    assert bound_measurement(compose_candidate(), ev,
                             measurements(state)) is None
    # a record that names ev but digests `other` is detached — unusable
    stolen = full_measurement(other)
    stolen["evidence_id"] = ev["evidence_id"]
    validate_capability_measurement(stolen)
    save_measurement(state, stolen)
    assert bound_measurement(compose_candidate(), ev,
                             measurements(state)) is None
    report = preflight([compose_candidate()], entries(state),
                       measurements(state), policy="AUTO_PERFORMANCE")
    assert report["candidates"][0]["estimate"] == "UNKNOWN"
    assert report["selections"] == []


def test_measurement_bound_to_smaller_scope_does_not_cover(tmp_path):
    state = tmp_path / "state"
    ev = make_evidence()
    register(state, ev)
    record = full_measurement(ev)
    record["scope"] = dict(ev["scope"], frames=2)   # measured 2 frames
    save_measurement(state, record)
    assert bound_measurement(compose_candidate(), ev,
                             measurements(state)) is None
    report = preflight([compose_candidate()], entries(state),
                       measurements(state), policy="AUTO_PERFORMANCE")
    assert report["candidates"][0]["missing_measurement"] == \
        ["measurement"]


# --- hard axis separation --------------------------------------------------------

def test_compose_evidence_never_covers_encode(tmp_path):
    """CPU compose capability is not encode capability."""
    state = tmp_path / "state"
    ev = make_evidence(scope=dict(COMPOSE_SCOPE, cpu="8-core"))
    register(state, ev)
    encode_candidate = {"candidate_id": "enc", "axis": "ENCODE",
                        "scope": dict(ENCODE_SCOPE)}
    report = preflight([encode_candidate], entries(state),
                       policy="AUTO_PERFORMANCE")
    verdict = report["candidates"][0]
    assert not verdict["eligible"]
    assert verdict["registry_state"] == "NO_EVIDENCE"
    assert axis_of(ev) == "COMPOSE"


def test_cuda_visibility_never_implies_nvenc(tmp_path):
    """A CUDA line in an environment is not NVIDIA_NATIVE encode."""
    state = tmp_path / "state"
    gpu_env = {"gpu": "CUDA", "worker_id": "w-1"}
    compose_gpu = make_evidence(env=gpu_env)
    register(state, compose_gpu)
    nvenc_documented = encode_evidence(driver="NVIDIA_NATIVE",
                                       state="DOCUMENTED_ONLY",
                                       env=dict(gpu_env,
                                                worker_id="w-enc2"))
    register(state, nvenc_documented)
    request = {"candidate_id": "enc", "axis": "ENCODE",
               "driver": "NVIDIA_NATIVE", "scope": dict(ENCODE_SCOPE)}
    report = preflight([request], entries(state),
                       policy="AUTO_PERFORMANCE")
    verdict = report["candidates"][0]
    assert not verdict["eligible"]
    assert verdict["registry_state"] == "DOCUMENTED_ONLY"
    # and the COMPOSE-axis evidence with a GPU env never even matches
    assert all("CE-" not in r or nvenc_documented["evidence_id"] in r
               for r in verdict["reasons"])


def test_llm_subscription_never_covers_api_credits():
    """A subscription entitlement never pays api_credits or GPU time."""
    ev = make_evidence(
        operation="account_entitlement",
        scope={"service": "chatgpt", "kind": "llm_subscription"},
        allowance={"subscription_units": 480, "compute_units": 0,
                   "api_credits": 0, "usd": 0, "handoff_minutes": 60})
    candidate = {"candidate_id": "gpu-job", "axis": "ENTITLEMENT",
                 "scope": {"service": "chatgpt"},
                 "requires": {"api_credits": 5, "compute_units": 1}}
    report = preflight([candidate], [ev], policy="AUTO_PERFORMANCE")
    verdict = report["candidates"][0]
    assert not verdict["eligible"]
    assert any("allowance_missing" in r for r in verdict["reasons"])
    assert set(allowance_missing(ev, {"api_credits": 5})) == \
        {"api_credits"}
    # units are never exchanged: subscription_units do not cover credits
    assert allowance_missing(ev, {"api_credits": 1}) == ["api_credits"]
    assert allowance_missing(ev, {"subscription_units": 1}) == []


def test_drive_connector_never_covers_sandbox_network():
    """A Drive connector grant is not sandbox network access."""
    ev = make_evidence(
        operation="drive_connector_grant",
        scope={"backend": "drive", "grant": "drive.file",
               "service": "google"})
    network_candidate = {"candidate_id": "net", "axis": "NETWORK",
                         "scope": {"network": "HTTPS"}}
    storage_candidate = {"candidate_id": "store", "axis": "STORAGE",
                         "scope": {"backend": "drive"}}
    report = preflight([network_candidate, storage_candidate], [ev],
                       policy="AUTO_PERFORMANCE")
    net, store = report["candidates"]
    assert not net["eligible"]
    assert net["registry_state"] == "NO_EVIDENCE"
    assert store["eligible"]          # the grant covers its own axis only


# --- preflight / ExecutionPlan gate --------------------------------------------

def test_preflight_filters_candidates(tmp_path):
    state = tmp_path / "state"
    good = make_evidence()
    stale = make_evidence(env={"worker_id": "w-2"})
    fake = make_evidence(env={"worker_id": "w-3",
                              "fake_runtime": True},
                         qualification="FAKE — test double")
    for ev in (good, stale, fake):
        register(state, ev)
    save_measurement(state, full_measurement(good))
    # the current environment matches `good`; `stale` drifted (worker_id)
    obs = observation_for(good)
    candidates = [compose_candidate(),
                  compose_candidate(candidate_id="c2", worker="w-2"),
                  compose_candidate(candidate_id="c3", worker="w-3"),
                  compose_candidate(candidate_id="c4", worker="w-9")]
    report = preflight(candidates, entries(state), measurements(state),
                       policy="AUTO_PERFORMANCE", observation=obs)
    by_id = {v["candidate_id"]: v for v in report["candidates"]}
    assert by_id["c1"]["selectable"]
    assert by_id["c2"]["registry_state"] == "STALE"
    assert not by_id["c2"]["eligible"]
    # under this observation the fake's environment drifted too — STALE;
    # with a matching observation it would report DOCUMENTED_ONLY
    assert by_id["c3"]["registry_state"] == "STALE"
    assert not by_id["c3"]["eligible"]
    assert current_state(fake)["state"] == "DOCUMENTED_ONLY"
    assert by_id["c4"]["registry_state"] == "NO_EVIDENCE"
    assert [s["candidate_id"] for s in report["selections"]] == ["c1"]


def test_auto_performance_needs_bound_measurement(tmp_path):
    """Qualified evidence without a measurement selects nothing."""
    state = tmp_path / "state"
    ev = make_evidence()
    register(state, ev)
    report = preflight([compose_candidate()], entries(state),
                       policy="AUTO_PERFORMANCE")
    verdict = report["candidates"][0]
    assert verdict["eligible"] and not verdict["selectable"]
    assert verdict["estimate"] == "UNKNOWN"
    assert "no_bound_measurement" in verdict["reasons"]
    assert report["selections"] == []


def test_fixed_route_is_labelled_user_choice(tmp_path):
    state = tmp_path / "state"
    ev = make_evidence()
    register(state, ev)
    report = preflight([compose_candidate()], entries(state),
                       policy="FIXED_ROUTE")
    selection = report["selections"][0]
    assert selection["label"] == "FIXED_ROUTE_USER_CHOICE"
    assert selection["eligible"]                       # qualified
    assert selection["estimate"] == "UNKNOWN"          # unmeasured
    # an unqualified fixed-route choice is still labelled, never upgraded
    report = preflight([compose_candidate(worker="w-9")],
                       entries(state), policy="FIXED_ROUTE")
    assert report["selections"][0]["label"] == \
        "FIXED_ROUTE_USER_CHOICE"
    assert report["selections"][0]["eligible"] is False


def test_plan_preflight_and_execution_gate(tmp_path):
    """Scheduler-facing preflight over a real ExecutionPlan 1."""
    state = tmp_path / "state"
    plan = remote_plan(policy="AUTO_PERFORMANCE", evidence_required=True)
    ev = make_evidence(env={"worker_id": "remote-1"})
    register(state, ev)

    report = plan_preflight(plan, state_dir=state)
    ops = {v["candidate_id"]: v for v in report["candidates"]
           if v["candidate_id"].startswith("op-")}
    assert all(v["eligible"] for v in ops.values())
    assert all(v["estimate"] == "UNKNOWN" for v in ops.values())
    # unmeasured: nothing auto-selectable, the gate stays empty
    assert report["selections"] == []
    assert report["evidence"] == {}
    with pytest.raises(FilmError, match="CAPABILITY_EVIDENCE_REQUIRED"):
        assert_executable(plan, evidence=report["evidence"])

    save_measurement(state, full_measurement(ev))
    report = plan_preflight(plan, state_dir=state)
    assert all(s["label"] == "AUTO_QUALIFIED"
               for s in report["selections"])
    assert_executable(plan, evidence=report["evidence"])

    # the ENCODE axis is reported separately — no encoder probe ran
    enc = {v["candidate_id"]: v for v in report["candidates"]
           if v["kind"] == "encoder"}
    assert enc["encode:FFMPEG"]["registry_state"] == "NO_EVIDENCE"


def test_fixed_route_plan_gate_admits_scope_eligible_choice(tmp_path):
    state = tmp_path / "state"
    plan = local_plan(policy="FIXED_ROUTE", evidence_required=True)
    ev = make_evidence(env={"worker_id": "local-1"},
                       scope=dict(COMPOSE_SCOPE, route="local"))
    register(state, ev)
    report = plan_preflight(plan, state_dir=state)
    assert all(s["label"] == "FIXED_ROUTE_USER_CHOICE"
               for s in report["selections"])
    # FIXED_ROUTE admits the user choice on scope evidence alone —
    # measurement stays UNKNOWN and is reported, never hidden
    assert report["evidence"]["LOCAL_NATIVE"]["state"] == \
        "QUALIFIED_FOR_SCOPE"
    assert_executable(plan, evidence=report["evidence"])


# --- CLI ------------------------------------------------------------------------

def test_cli_capability_status_and_preflight(tmp_path, capsys):
    state = tmp_path / "state"
    ev = make_evidence()
    register(state, ev)
    assert cli.main(["capability-status", "--state-dir",
                     str(state)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["entries"][0]["state"] == "QUALIFIED_FOR_SCOPE"
    assert out["facets"]["release_state"] == "NOT_AUTHORIZED"

    obs_file = tmp_path / "obs.json"
    obs = observation_for(ev)
    obs["environment"]["device"] = "other-box"
    obs_file.write_text(json.dumps(obs))
    assert cli.main(["capability-status", "--state-dir", str(state),
                     "--observation", str(obs_file)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["entries"][0]["state"] == "STALE"
    assert "environment.device changed" in \
        out["entries"][0]["reasons"]

    plan = remote_plan(policy="AUTO_PERFORMANCE", evidence_required=True)
    worker_ev = make_evidence(env={"worker_id": "remote-1"})
    register(state, worker_ev)
    save_measurement(state, full_measurement(worker_ev))
    plan_path = tmp_path / "plan.json"
    write_plan(plan_path, plan)
    assert cli.main(["capability-preflight", str(plan_path),
                     "--state-dir", str(state)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["policy"] == "AUTO_PERFORMANCE"
    assert all(s["label"] == "AUTO_QUALIFIED"
               for s in out["selections"])
    # a project dir containing execution/plan.json works too
    project = tmp_path / "project"
    (project / "execution").mkdir(parents=True)
    write_plan(project / "execution" / "plan.json", plan)
    assert cli.main(["capability-preflight", str(project),
                     "--state-dir", str(state)]) == 0
    json.loads(capsys.readouterr().out)
    assert cli.main(["capability-preflight",
                     str(tmp_path / "empty"),
                     "--state-dir", str(state)]) == 1
    assert "no ExecutionPlan 1" in capsys.readouterr().err
