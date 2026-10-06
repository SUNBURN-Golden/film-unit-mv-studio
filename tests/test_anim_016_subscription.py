"""ANIM-016 AI subscription execution: entitlement records, probe ->
capability evidence, manual execution packets (shipped worker script),
result import through the existing Coordinator path, and every explicit
refusal fence (exec/storage §7, §10.2, §11; schema §15/§17).

No real subscription access exists on this box: every executed run is
the FAKE runtime actually executing the shipped stdlib script in a
subprocess inside a sandboxed temp dir, and every qualification report
stays UNQUALIFIED.
"""
import copy
import hashlib
import json

import pytest

from anim_014_kit import SNAPSHOT, RECIPE, make_plan, operation

from engine.animation_schema import (read_canon, write_canon,
                                     validate_capability_evidence)
from engine.core import FilmError
from engine.execution_packets import (RESULT_TYPE, SCRIPT_NAME,
                                      export_manual_packets,
                                      fixture_packet_dir, import_result,
                                      split_ranges,
                                      validate_subscription_packet,
                                      validate_subscription_result)
from engine.execution_workers import (Coordinator, expected_frame_digest)
from engine.execution_workers.subscription import (
    ALLOWANCE_UNITS, FakeSubscriptionRuntime, SubscriptionWorker,
    assert_same_binding, audit_worker_source, charge_check, consume,
    entitlement_path, evidence_registry_state, load_entitlement,
    make_entitlement, probe_subscription, rebind_entitlement, route_label,
    save_entitlement, save_evidence, subscription_facet_report,
    subscription_scope_covered, subscription_worker_id,
    worker_script_sha256, worker_script_version)
from engine.storage_backends import ConnectionDropped

SERVICE = "chatgpt-code-runtime"
BINDING = hashlib.sha256(b"subscription-account-digest").hexdigest()
BINDING_B = hashlib.sha256(b"other-account-digest").hexdigest()

CAPS = {"max_input_bytes": 1 << 20, "max_output_bytes": 1 << 20,
        "max_file_count": 64, "max_frames_per_packet": 4,
        "max_scratch_bytes": 1 << 20, "network": "NONE", "gpu": "NONE",
        "drive_read": "NONE", "drive_write": "NONE",
        "runtimes": ["python-deterministic-v1"],
        "cancel_method": "user abandons the run inside the service UI",
        "complete_method": "user downloads the result dir; app imports"}


def entitlement(**overrides):
    args = dict(usage_path="CODE_RUNTIME_FILE_PACKET",
                account_binding=BINDING,
                allowance={"subscription_units": 100,
                           "compute_units": 0,
                           "api_credits": 0, "usd": 0,
                           "handoff_minutes": "UNLIMITED"},
                inclusion={"execution_in_subscription": "CONFIRMED",
                           "note": "declared by the account owner"},
                service_caps=dict(CAPS))
    args.update(overrides)
    return make_entitlement(SERVICE, **args)


def setup_state(tmp_path, **kw):
    """Coordinator + saved entitlement + bound workers, ready to plan."""
    state = tmp_path / "state"
    ent = entitlement(**kw)
    save_entitlement(state, ent)
    wid = subscription_worker_id(SERVICE, BINDING)
    plan = make_plan(
        [operation("op-a", [0, 4], "SUBSCRIPTION_CODE_RUNTIME", wid),
         operation("op-b", [4, 8], "SUBSCRIPTION_CODE_RUNTIME", wid)],
        transfer_route="MANUAL_PACKET")
    coordinator = Coordinator(state)
    keys = coordinator.plan_jobs(plan)
    worker = SubscriptionWorker(load_entitlement(state, SERVICE))
    fake = FakeSubscriptionRuntime(load_entitlement(state, SERVICE))
    return state, ent, plan, coordinator, keys, worker, fake


def export_all(plan, coordinator, keys, worker, epath, out, **kw):
    exports = []
    for key in keys:
        if coordinator.jobs[key]["state"] == "PLANNED":
            coordinator.reserve(key)
        exports.append(export_manual_packets(
            coordinator, plan, key, epath, worker, out, **kw))
    return exports


def run_and_import(coordinator, fake, exports, tmp_path, state,
                   entitlement_doc=None):
    ent = entitlement_doc or load_entitlement(state, SERVICE)
    for exp in exports:
        for packet_dir in exp["packets"]:
            result_dir = tmp_path / ("result-" + packet_dir.split("/")[-1])
            manifest = fake.execute_packet(packet_dir, result_dir)
            assert manifest["document_type"] == RESULT_TYPE
            outcome = import_result(coordinator, fake, packet_dir,
                                    result_dir, entitlement=ent)
    return outcome


# -- entitlement records ------------------------------------------------------

def test_entitlement_secret_free_binding_and_separate_units():
    ent = entitlement()
    assert set(ent["allowance"]) == set(ALLOWANCE_UNITS)
    assert set(ent["used"]) == set(ALLOWANCE_UNITS)
    assert ent["account_binding"] == BINDING          # a digest, never an email
    assert "@" not in json.dumps(ent)
    with pytest.raises(FilmError, match="SHA-256"):
        entitlement(account_binding="juntae@gmail.com")   # never a raw id
    with pytest.raises(FilmError, match="exactly"):
        bad = dict(ent); bad["token"] = "x"               # no credential fields
        from engine.execution_workers.subscription import \
            validate_entitlement
        validate_entitlement(bad)


def test_entitlement_units_never_summed_and_unknown_is_not_free():
    ent = entitlement(allowance={"subscription_units": "UNKNOWN"})
    verdict = charge_check(ent, {"subscription_units": 10})
    assert verdict["verdict"] == "INCLUSION_UNKNOWN"
    assert "never" in verdict["reason"]               # never shown as free
    # every unit is tracked in its own bucket — a compute-unit cost does
    # not touch subscription units
    ent2 = entitlement()
    charge_check(ent2, {"compute_units": 0})
    consume(ent2, {"subscription_units": 7})
    assert ent2["used"]["subscription_units"] == 7
    assert ent2["used"]["compute_units"] == 0
    with pytest.raises(FilmError, match="Unknown allowance unit"):
        charge_check(ent2, {"kwh": 1})


def test_additional_charges_refused_then_need_double_approval():
    ent = entitlement()
    cost = {"api_credits": 5}
    assert charge_check(ent, cost)["verdict"] == "ADDITIONAL_CHARGE_REFUSED"
    refused = charge_check(ent, cost, allow_additional_charges=True)
    assert refused["verdict"] == "ADDITIONAL_CHARGE_REFUSED"
    assert "separate explicit approval" in refused["reason"]
    approved = charge_check(ent, cost, allow_additional_charges=True,
                            additional_charges_approved=True)
    assert approved["verdict"] == "EXTRA_CHARGE_APPROVED"


def test_allowance_exhaustion_stops_without_reroute():
    ent = entitlement(allowance={"subscription_units": 3})
    verdict = charge_check(ent, {"subscription_units": 4})
    assert verdict["verdict"] == "ALLOWANCE_EXHAUSTED"
    assert "no reroute" in verdict["reason"].lower() \
        or "no other" in verdict["reason"].lower()


def test_rebind_bumps_credential_epoch(tmp_path):
    state = tmp_path / "state"
    save_entitlement(state, entitlement())
    doc = rebind_entitlement(state, SERVICE)
    assert doc["credential_epoch"] == 2
    assert load_entitlement(state, SERVICE)["credential_epoch"] == 2


# -- probe -> capability evidence ----------------------------------------------

def test_probe_without_runtime_is_documented_only(tmp_path):
    state = tmp_path / "state"
    ent = entitlement()
    save_entitlement(state, ent)
    evidence = probe_subscription(SERVICE, entitlement=ent)
    validate_capability_evidence(evidence)
    assert evidence["registry_state"] == "DOCUMENTED_ONLY"
    assert evidence["fixture"] is None
    assert evidence["environment"]["runtime"] == "UNOBSERVED"
    assert evidence["account_binding"] == BINDING
    assert evidence["credential_epoch"] == ent["credential_epoch"]
    assert evidence["driver"] == "QUALIFIED_SERVICE"
    path = save_evidence(state, evidence)
    assert read_canon(path)["evidence_id"] == evidence["evidence_id"]


def test_fake_probe_actually_executes_shipped_script(tmp_path):
    ent = entitlement()
    fake = FakeSubscriptionRuntime(ent)
    evidence = probe_subscription(SERVICE, entitlement=ent, runtime=fake,
                                  work_dir=tmp_path / "probe")
    validate_capability_evidence(evidence)
    # the fixture really ran: packet in, manifest out, digests verified
    assert evidence["fixture"]["ran"] is True
    assert evidence["fixture"]["verified"] is True
    assert evidence["environment"]["network"] == "NONE"
    assert evidence["environment"]["gpu"] == "NONE"
    assert evidence["environment"]["fake_runtime"] is True
    assert "FAKE" in evidence["qualification"]
    # a fake probe never qualifies the real service
    assert evidence["registry_state"] == "DOCUMENTED_ONLY"
    assert evidence["scope"]["frames"] == 2         # the tiny fixture range
    assert evidence["scope"]["runtime_contract"] == \
        "python-deterministic-v1"


def test_llm_or_docs_never_qualify(tmp_path):
    evidence = probe_subscription(SERVICE, entitlement=entitlement())
    assert evidence["registry_state"] != "QUALIFIED_FOR_SCOPE"
    assert subscription_facet_report()["qualification_state"] == \
        "UNQUALIFIED"


def test_scope_check_never_infers_superset():
    ent = entitlement()
    fake = FakeSubscriptionRuntime(ent)
    evidence = probe_subscription(SERVICE, entitlement=ent, runtime=fake,
                                  work_dir=None)
    scope = evidence["scope"]
    exact = dict(scope)
    assert subscription_scope_covered(scope, exact)
    smaller = dict(exact, frames=1)                 # a smaller range fits
    assert subscription_scope_covered(scope, smaller)
    bigger = dict(exact, frames=scope["frames"] + 1)
    assert not subscription_scope_covered(scope, bigger)
    gpu = dict(exact, gpu="CUDA")                   # CPU probe != GPU scope
    assert not subscription_scope_covered(scope, gpu)
    other_service = dict(exact, service="other")
    assert not subscription_scope_covered(scope, other_service)
    assert not subscription_scope_covered(scope, {})


def test_stale_evidence_on_environment_account_route_limit_change():
    ent = entitlement()
    fake = FakeSubscriptionRuntime(ent)
    evidence = probe_subscription(SERVICE, entitlement=ent, runtime=fake,
                                  work_dir=None)
    base = {"environment": dict(evidence["environment"]),
            "account_binding": BINDING,
            "credential_epoch": ent["credential_epoch"],
            "caps": dict(evidence["caps"]),
            "route": evidence["route"],
            "transport": evidence["transport"]}
    assert evidence_registry_state(evidence, base) == ("DOCUMENTED_ONLY", [])

    def changed(**env):
        observation = copy.deepcopy(base)
        observation["environment"].update(env)
        return evidence_registry_state(evidence, observation)

    assert changed(session_id="x" * 64)[0] == "STALE"
    assert changed(device="m2-mac")[0] == "STALE"
    assert changed(os="linux-6")[0] == "STALE"
    assert changed(driver="python-stdlib-2")[0] == "STALE"
    changed_account = copy.deepcopy(base)
    changed_account["account_binding"] = BINDING_B
    assert evidence_registry_state(evidence, changed_account)[0] == "STALE"
    changed_epoch = copy.deepcopy(base)
    changed_epoch["credential_epoch"] = 9
    assert evidence_registry_state(evidence, changed_epoch)[0] == "STALE"
    changed_caps = copy.deepcopy(base)
    changed_caps["caps"] = dict(base["caps"], max_file_count=8)
    assert evidence_registry_state(evidence, changed_caps)[0] == "STALE"
    changed_route = copy.deepcopy(base)
    changed_route["route"] = "COORDINATOR_RELAY"
    assert evidence_registry_state(evidence, changed_route)[0] == "STALE"
    expired = copy.deepcopy(evidence)
    expired["environment"]["session_expires_at_ms"] = 5
    state, reasons = evidence_registry_state(expired, base, now_ms=10)
    assert state == "STALE" and "session_expired" in reasons


def test_route_labels_explicit():
    assert route_label(entitlement()) == "MANUAL"
    api = entitlement(official_execution_api=True)
    assert route_label(api) == "AUTOMATIC"
    # a consumer site is never silently automatic
    assert SubscriptionWorker(entitlement()).probe()["route_label"] \
        == "MANUAL"


# -- packet export --------------------------------------------------------------

def test_export_writes_pinned_packet_and_drives_manual_route(tmp_path):
    state, ent, plan, c, keys, worker, fake = setup_state(tmp_path)
    exports = export_all(plan, c, keys, worker,
                         entitlement_path(state, SERVICE), tmp_path / "pk")
    assert len(exports) == 2
    for exp in exports:
        assert exp["state"] == "SUBMITTING"       # attach minted the attempt
        assert exp["route_label"] == "MANUAL"
        packet_dir = exp["packets"][0]
        packet = validate_subscription_packet(
            read_canon(f"{packet_dir}/packet.json"))
        assert packet["route"]["automation"] == "NONE"
        assert packet["worker"]["sha256"] == worker_script_sha256()
        assert packet["worker"]["version"] == worker_script_version()
        assert (packet_dir + "/" + SCRIPT_NAME) and \
            hashlib.sha256(open(packet_dir + "/" + SCRIPT_NAME, "rb")
                           .read()).hexdigest() == packet["worker"]["sha256"]
        # packet binds the minted attempt/request/nonce/grant identity
        job = c.jobs[exp["job_key"]]
        assert packet["execution_unit"]["attempt_id"] == job["attempt_id"]
        assert packet["receipt"]["nonce_digest"] == job["nonce"]
        assert packet["receipt"]["grant_digest"] == job["grant_digest"]
        assert packet["limits"]["network"] == "NONE"
        assert packet["limits"]["gpu"] == "NONE"
        # declared inputs + permissions and expected output members
        members = packet["expected_output"]["members"]
        assert [m["frame_index"] for m in members] == \
            list(range(*packet["execution_unit"]["frame_range"]))
        for member in members:
            assert member["sha256"] == expected_frame_digest(
                member["frame_index"], packet["frame_contract"],
                SNAPSHOT, RECIPE)


def test_no_network_no_gpu_packet_contract(tmp_path):
    state, ent, plan, c, keys, worker, fake = setup_state(tmp_path)
    exp = export_all(plan, c, keys, worker,
                     entitlement_path(state, SERVICE), tmp_path / "pk")[0]
    packet = read_canon(f"{exp['packets'][0]}/packet.json")
    assert packet["inputs"]["permissions"]["network"] == "NONE"
    assert packet["inputs"]["permissions"]["gpu"] == "NONE"
    assert packet["limits"]["network"] == "NONE"
    assert packet["limits"]["gpu"] == "NONE"
    audit_worker_source()                     # the shipped script itself


def test_worker_script_audit_blocks_network_and_dynamic_code():
    audit_worker_source()
    with pytest.raises(FilmError, match="WORKER_SCRIPT_AUDIT"):
        audit_worker_source(b"import socket\n")
    with pytest.raises(FilmError, match="WORKER_SCRIPT_AUDIT"):
        audit_worker_source(b"import subprocess\n")
    with pytest.raises(FilmError, match="WORKER_SCRIPT_AUDIT"):
        audit_worker_source(b"eval(payload)\n")
    with pytest.raises(FilmError, match="WORKER_SCRIPT_AUDIT"):
        audit_worker_source(b"__import__('os')\n")


def test_packet_splitting_under_file_limits(tmp_path):
    state, ent, plan, c, keys, worker, fake = setup_state(tmp_path)
    small = dict(CAPS, max_frames_per_packet=2)
    # re-declare tighter caps on the stored entitlement
    doc = load_entitlement(state, SERVICE)
    doc["service_caps"] = small
    write_canon(entitlement_path(state, SERVICE), doc)
    exports = export_all(plan, c, keys, worker,
                         entitlement_path(state, SERVICE), tmp_path / "pk")
    assert all(e["packet_count"] == 2 for e in exports)
    assert len({p for e in exports for p in e["packets"]}) == 4
    for exp in exports:
        for packet_dir in exp["packets"]:
            packet = read_canon(f"{packet_dir}/packet.json")
            lo, hi = packet["execution_unit"]["frame_range"]
            assert hi - lo <= 2                    # each packet fits the cap


def test_packet_limit_exceeded_splits_or_refuses(tmp_path):
    contract = {"width": 8, "height": 6, "stride": 32, "pixel_format":
                "RGBA8", "color_space": "sRGB", "transfer": "SRGB",
                "color_range": "FULL", "alpha_policy": "STRAIGHT",
                "fps": {"num": 24, "den": 1}, "frame_range": [0, 8]}
    caps = dict(CAPS, max_frames_per_packet=2)
    assert split_ranges([0, 8], contract, caps) == \
        [[0, 2], [2, 4], [4, 6], [6, 8]]
    tiny = dict(CAPS, max_output_bytes=10)         # one frame won't fit
    with pytest.raises(FilmError, match="PACKET_LIMIT"):
        split_ranges([0, 8], contract, tiny)
    with pytest.raises(FilmError, match="CAPACITY_UNKNOWN"):
        split_ranges([0, 8], contract, {})
    partial = dict(CAPS); partial["max_file_count"] = None
    with pytest.raises(FilmError, match="CAPACITY_UNKNOWN"):
        split_ranges([0, 8], contract, partial)
    low_input = dict(CAPS, max_input_bytes=100)
    with pytest.raises(FilmError, match="PACKET_LIMIT"):
        split_ranges([0, 8], contract, low_input, input_bytes=4096)


def test_gpu_plan_refused_on_cpu_only_runtime(tmp_path):
    state = tmp_path / "state"
    ent = entitlement()
    save_entitlement(state, ent)
    wid = subscription_worker_id(SERVICE, BINDING)
    plan = make_plan(
        [operation("gpu-a", [0, 4], "SUBSCRIPTION_CODE_RUNTIME", wid),
         operation("gpu-b", [4, 8], "SUBSCRIPTION_CODE_RUNTIME", wid)],
        transfer_route="MANUAL_PACKET")
    for op in plan["operations"]:
        op["runtime_contract"] = "cuda-tensor-v1"     # a GPU contract
    c = Coordinator(state)
    keys = c.plan_jobs(plan)
    c.reserve(keys[0])
    worker = SubscriptionWorker(load_entitlement(state, SERVICE))
    with pytest.raises(FilmError, match="CAPABILITY_REFUSED"):
        export_manual_packets(c, plan, keys[0],
                              entitlement_path(state, SERVICE), worker,
                              tmp_path / "pk")


def test_extra_charge_refusal_stops_export(tmp_path):
    state, ent, plan, c, keys, worker, fake = setup_state(tmp_path)
    c.reserve(keys[0])
    with pytest.raises(FilmError, match="ADDITIONAL_CHARGE_REFUSED"):
        export_manual_packets(c, plan, keys[0],
                              entitlement_path(state, SERVICE), worker,
                              tmp_path / "pk",
                              cost={"api_credits": 10})


def test_export_allowance_exhaustion_stops(tmp_path):
    state, ent, plan, c, keys, worker, fake = setup_state(
        tmp_path, allowance={"subscription_units": 2,
                             "handoff_minutes": "UNLIMITED"})
    c.reserve(keys[0])
    with pytest.raises(FilmError, match="ALLOWANCE_EXHAUSTED"):
        export_manual_packets(c, plan, keys[0],
                              entitlement_path(state, SERVICE), worker,
                              tmp_path / "pk")


def test_unknown_acceptance_not_auto_routed(tmp_path):
    """A job that never confirmed acceptance reconciles to UNKNOWN and is
    never resubmitted under a new identity — re-planning the same plan is
    refused by the fixed job identity, and the manual route only leaves
    UNKNOWN through the user's explicit import."""
    state, ent, plan, c, keys, worker, fake = setup_state(tmp_path)
    exports = export_all(plan, c, keys, worker,
                         entitlement_path(state, SERVICE), tmp_path / "pk")
    # coordinator restart: the persisted SUBMITTING reloads as UNKNOWN
    c2 = Coordinator(state)
    assert c2.jobs[keys[0]]["state"] == "UNKNOWN"
    with pytest.raises(FilmError, match="JOB_IDENTITY_EXISTS"):
        c2.plan_jobs(plan)
    with pytest.raises(FilmError, match="UNKNOWN_FENCED"):
        c2.submit(worker, keys[0])
    # the manual worker cannot answer — reconcile leaves the fence armed
    assert c2.reconcile(worker, keys[0]) == "UNKNOWN"
    # ...until the user's explicit import of the run's real result
    packet_dir = exports[0]["packets"][0]
    result_dir = tmp_path / "res-late"
    fake.execute_packet(packet_dir, result_dir)
    outcome = import_result(c2, fake, packet_dir, result_dir,
                            entitlement=load_entitlement(state, SERVICE))
    assert c2.jobs[keys[0]]["state"] == "OUTPUT_PENDING_VERIFY"
    assert c2.verify_outputs(keys[0]) == "VERIFIED"


# -- substitute provider / account fencing --------------------------------------

def test_substitute_provider_and_account_blocked(tmp_path):
    state, ent, plan, c, keys, worker, fake = setup_state(tmp_path)
    c.reserve(keys[0])
    # a different account/provider implies a different bound worker id —
    # the plan's worker binding refuses it before any packet is written
    with pytest.raises(FilmError, match="SUBSTITUTE_WORKER_FENCED"):
        export_manual_packets(c, plan, keys[0],
                              entitlement_path(state, SERVICE),
                              SubscriptionWorker(entitlement(
                                  account_binding=BINDING_B)),
                              tmp_path / "pk")
    with pytest.raises(FilmError, match="SUBSTITUTE_WORKER_FENCED"):
        export_manual_packets(c, plan, keys[0],
                              entitlement_path(state, SERVICE),
                              SubscriptionWorker(dict(
                                  entitlement(), service="other-site")),
                              tmp_path / "pk")
    # and the entitlement binding check itself refuses each substitution
    with pytest.raises(FilmError, match="SUBSTITUTE_ACCOUNT_FENCED"):
        assert_same_binding(ent,
                            SubscriptionWorker(entitlement(
                                account_binding=BINDING_B)))
    with pytest.raises(FilmError, match="SUBSTITUTE_PROVIDER_FENCED"):
        assert_same_binding(ent, SubscriptionWorker(dict(
            entitlement(), service="other-site")))
    with pytest.raises(FilmError, match="CREDENTIAL_EPOCH_STALE"):
        assert_same_binding(ent, SubscriptionWorker(dict(
            entitlement(), credential_epoch=9)))


def test_manual_only_worker_never_auto_submits(tmp_path):
    worker = SubscriptionWorker(entitlement())
    with pytest.raises(FilmError, match="MANUAL_ONLY"):
        worker.submit({"job_key": "x"}, now_ms=0)
    with pytest.raises(FilmError, match="MANUAL_ONLY"):
        worker.preflight({"job_key": "x"}, now_ms=0)
    assert worker.status("req-x")["state"] == "AWAITING_USER"
    assert worker.collect_receipts("req-x") == []


# -- result import ----------------------------------------------------------------

def test_end_to_end_fake_execute_import_verify_seal(tmp_path):
    state, ent, plan, c, keys, worker, fake = setup_state(tmp_path)
    exports = export_all(plan, c, keys, worker,
                         entitlement_path(state, SERVICE), tmp_path / "pk")
    outcome = run_and_import(c, fake, exports, tmp_path, state)
    for key in keys:
        assert c.jobs[key]["state"] == "OUTPUT_PENDING_VERIFY"
        assert c.verify_outputs(key) == "VERIFIED"
        c.seal(key)
        assert c.jobs[key]["state"] == "ARCHIVED"
    # allowance was consumed against its own unit bucket only
    used = load_entitlement(state, SERVICE)["used"]
    assert used["subscription_units"] == 8
    assert used["handoff_minutes"] == 0           # UNLIMITED, not debited
    # fake evidence stays labelled; nothing here qualifies the real service
    receipts = c.jobs[keys[0]]["receipts"]
    assert receipts[0]["evidence"]["class"] == "SUBSCRIPTION_PACKET"
    assert c.facet_report()["qualification_state"] == "UNQUALIFIED"


def test_import_rejects_foreign_manifest(tmp_path):
    state, ent, plan, c, keys, worker, fake = setup_state(tmp_path)
    exp = export_all(plan, c, keys, worker,
                     entitlement_path(state, SERVICE), tmp_path / "pk")[0]
    packet_dir, result_dir = exp["packets"][0], tmp_path / "res"
    fake.execute_packet(packet_dir, result_dir)
    manifest = read_canon(result_dir / "result_manifest.json")
    manifest["job_key"] = "f" * 64                # foreign job
    write_canon(result_dir / "result_manifest.json", manifest)
    with pytest.raises(FilmError, match="FOREIGN_RESULT"):
        import_result(c, fake, packet_dir, result_dir,
                      entitlement=load_entitlement(state, SERVICE))


def test_import_rejects_partial_and_tampered_outputs(tmp_path):
    state, ent, plan, c, keys, worker, fake = setup_state(tmp_path)
    exp = export_all(plan, c, keys, worker,
                     entitlement_path(state, SERVICE), tmp_path / "pk")[0]
    packet_dir, result_dir = exp["packets"][0], tmp_path / "res"
    fake.execute_packet(packet_dir, result_dir)
    outputs = sorted((result_dir / "outputs").glob("*.png"))
    outputs[-1].unlink()                          # partial result
    with pytest.raises(FilmError, match="PARTIAL_RESULT"):
        import_result(c, fake, packet_dir, result_dir,
                      entitlement=load_entitlement(state, SERVICE))
    result_dir2 = tmp_path / "res2"
    fake.execute_packet(packet_dir, result_dir2)
    png = sorted((result_dir2 / "outputs").glob("*.png"))[0]
    png.write_bytes(b"corrupt")
    with pytest.raises(FilmError, match="INTEGRITY_FAILED"):
        import_result(c, fake, packet_dir, result_dir2,
                      entitlement=load_entitlement(state, SERVICE))


def test_import_rejects_wrong_worker_script_provenance(tmp_path):
    state, ent, plan, c, keys, worker, fake = setup_state(tmp_path)
    exp = export_all(plan, c, keys, worker,
                     entitlement_path(state, SERVICE), tmp_path / "pk")[0]
    packet_dir, result_dir = exp["packets"][0], tmp_path / "res"
    fake.execute_packet(packet_dir, result_dir)
    manifest = read_canon(result_dir / "result_manifest.json")
    manifest["worker_script_sha256"] = "0" * 64
    write_canon(result_dir / "result_manifest.json", manifest)
    with pytest.raises(FilmError, match="FOREIGN_RESULT"):
        import_result(c, fake, packet_dir, result_dir,
                      entitlement=load_entitlement(state, SERVICE))


def test_session_expiry_mid_run_never_reidentifies(tmp_path):
    """Mid-run session expiry: the runtime's session epoch diverges, the
    result is refused as produced under a voided session, and nothing is
    resubmitted under a new identity — re-attach is the explicit path."""
    state, ent, plan, c, keys, worker, fake = setup_state(tmp_path)
    exports = export_all(plan, c, keys, worker,
                         entitlement_path(state, SERVICE), tmp_path / "pk")
    packet_dir = exports[0]["packets"][0]
    result_dir = tmp_path / "res"
    fake.execute_packet(packet_dir, result_dir)
    fake.expire_session()                          # runtime forgot the run
    with pytest.raises(FilmError, match="SESSION_EXPIRED"):
        fake.status(c.jobs[keys[0]]["request_id"])
    # re-binding the account voids the epoch the packet was issued under
    rebind_entitlement(state, SERVICE)
    with pytest.raises(FilmError, match="SESSION_EXPIRED"):
        import_result(c, fake, packet_dir, result_dir,
                      entitlement=load_entitlement(state, SERVICE))


def test_quote_change_stales_issued_packet(tmp_path):
    state, ent, plan, c, keys, worker, fake = setup_state(tmp_path)
    exports = export_all(plan, c, keys, worker,
                         entitlement_path(state, SERVICE), tmp_path / "pk")
    packet_dir = exports[0]["packets"][0]
    result_dir = tmp_path / "res"
    fake.execute_packet(packet_dir, result_dir)
    doc = load_entitlement(state, SERVICE)
    doc["prices"] = {"subscription_units": 2500000}   # terms changed
    write_canon(entitlement_path(state, SERVICE), doc)
    with pytest.raises(FilmError, match="QUOTE_CHANGED"):
        import_result(c, fake, packet_dir, result_dir, entitlement=doc)


def test_fake_runtime_allowance_exhaustion_no_reroute(tmp_path):
    state, ent, plan, c, keys, worker, fake = setup_state(tmp_path)
    broke = FakeSubscriptionRuntime(load_entitlement(state, SERVICE),
                                    remaining_units=2)
    exports = export_all(plan, c, keys, worker,
                         entitlement_path(state, SERVICE), tmp_path / "pk")
    packet_dir = exports[0]["packets"][0]           # 4-frame range
    with pytest.raises(FilmError, match="ALLOWANCE_EXHAUSTED"):
        broke.execute_packet(packet_dir, tmp_path / "res")


def test_dead_fake_runtime_is_explicit_unknown(tmp_path):
    state, ent, plan, c, keys, worker, fake = setup_state(tmp_path)
    fake.dead = True
    packet_dir = fixture_packet_dir(tmp_path / "fix")
    with pytest.raises(ConnectionDropped):
        fake.execute_packet(packet_dir, tmp_path / "res")


# -- CLI -------------------------------------------------------------------------

def _cli(*argv):
    from engine.cli import main
    import io, contextlib
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def test_cli_register_probe_status(tmp_path):
    state = tmp_path / "state"
    code, out, err = _cli(
        "subscription-register", "--state-dir", str(state),
        "--service", SERVICE,
        "--usage-path", "CODE_RUNTIME_FILE_PACKET",
        "--account-binding", BINDING,
        "--allowance", json.dumps({"subscription_units": 50}),
        "--inclusion", "CONFIRMED")
    assert code == 0, err
    doc = json.loads(out)
    assert doc["route_label"] == "MANUAL"
    assert doc["qualification_state"] == "UNQUALIFIED"
    assert load_entitlement(state, SERVICE)["account_binding"] == BINDING
    # raw account names are refused — the binding must be a digest
    code, out, err = _cli(
        "subscription-register", "--state-dir", str(state),
        "--service", "bad", "--usage-path", "HOSTED_NOTEBOOK_UI",
        "--account-binding", "juntae@example.com")
    assert code == 1 and "SHA-256" in err
    code, out, err = _cli("subscription-probe", "--state-dir", str(state),
                          "--service", SERVICE)
    assert code == 0
    assert json.loads(out)["registry_state"] == "DOCUMENTED_ONLY"
    code, out, err = _cli("subscription-probe", "--state-dir", str(state),
                          "--service", SERVICE, "--fake",
                          "--work-dir", str(tmp_path / "probe"))
    doc = json.loads(out)
    assert code == 0 and doc["fake"] is True
    assert "FAKE" in doc["qualification"]
    code, out, err = _cli("subscription-status", "--state-dir", str(state))
    doc = json.loads(out)
    assert code == 0
    assert doc["entitlements"][0]["service"] == SERVICE
    assert doc["facets"]["release_state"] == "NOT_AUTHORIZED"
    assert len(doc["evidence"]) == 2


def test_cli_export_and_import_round_trip(tmp_path):
    state, ent, plan, c, keys, worker, fake = setup_state(tmp_path)
    plan_path = tmp_path / "plan.json"
    write_canon(plan_path, plan)
    code, out, err = _cli(
        "subscription-export", "--state-dir", str(state),
        "--plan", str(plan_path), "--service", SERVICE,
        "--out", str(tmp_path / "pk"))
    assert code == 0, err
    doc = json.loads(out)
    assert doc["route_label"] == "MANUAL"
    assert len(doc["jobs"]) == 2
    assert doc["qualification_state"] == "UNQUALIFIED"
    packet_dir = doc["jobs"][0]["packets"][0]
    result_dir = tmp_path / "cli-res"
    fake.execute_packet(packet_dir, result_dir)
    code, out, err = _cli(
        "subscription-import", "--state-dir", str(state),
        "--service", SERVICE, "--packet-dir", packet_dir,
        "--result-dir", str(result_dir))
    assert code == 0, err
    doc = json.loads(out)
    assert doc["verified"] == "VERIFIED"
    c2 = Coordinator(state)                          # state persisted
    assert c2.jobs[doc["job_key"]]["state"] == "VERIFIED"


def test_result_manifest_document_shape(tmp_path):
    packet_dir = fixture_packet_dir(tmp_path / "fix")
    fake = FakeSubscriptionRuntime(entitlement())
    result_dir = tmp_path / "res"
    manifest = fake.execute_packet(packet_dir, result_dir)
    validate_subscription_result(manifest)
    receipt = read_canon(result_dir / "receipt.json")
    assert receipt["kind"] == "COMPLETE"
    assert receipt["members"]
