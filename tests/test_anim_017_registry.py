"""ANIM-017 extended: PACK-SPARSE warm one-cut accounting, Range-
unsupported fallback accounting, DIRECT_DRIVE refusal, the secret-free
benchmark report and the ANIM-019 capability-registry gate for
AUTO_PERFORMANCE candidacy — plus the perf-plan/perf-benchmark CLI.

The fake archive backend is a protocol double only; its PASS is never
qualification evidence. Registered measurements bind only the exact
input the benchmark actually ran — anything unproven stays UNKNOWN.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from engine import cli
from engine import perf_scheduler as ps
from engine.capability_registry import (evaluate_candidate,
                                        measurement_missing,
                                        plan_preflight)
from engine.core import FilmError, read
from engine.execution_plan import validate_execution_plan
from engine.storage_backends.fake_drive import FakeDriveBackend
from test_anim_017 import (SIZE, edit_member, file_shas, perf_project)


def counting_backend(**kwargs):
    inner = FakeDriveBackend(**kwargs)
    return inner, ps.CountingBackend(inner)


def packed_sources(p, backend, **kw):
    return ps.pack_sources_for_project(p, backend, **kw)


def request_log(backend, since=0):
    return backend.requests[since:]


def get_ranges(backend, since=0):
    return [r for r in request_log(backend, since)
            if r["op"] == "get_range"]


def member_request_targets(backend, since=0):
    """The pack objects any member range read hit (excludes index fetches)."""
    return {r["object_id"] for r in get_ranges(backend, since)}


def index_of(backend, index_object_id):
    result = backend.get_object(index_object_id)
    return json.loads(result.body.decode("utf-8"))


# --- PACK-SPARSE warm one-cut ---------------------------------------------------

def test_pack_cold_fetches_members_sparsely(tmp_path):
    p = perf_project(tmp_path)
    _, backend = counting_backend()
    packed = packed_sources(p, backend)
    perf_root = tmp_path / "perf"
    result = ps.run_pipeline(
        p, perf_root, member_sources=packed["member_sources"],
        encode_roles=(), perf_label="pack-cold")
    counters = result["metrics"]["counters"]
    # Cold: every needed member once, each pack index once — the backend
    # log is the accounting, not an estimate.
    assert counters["member_fetches"] == 24
    ranges = get_ranges(backend)
    assert len(ranges) == 24
    assert all(r["status"] == 206 for r in ranges)
    index_gets = [r for r in request_log(backend)
                  if r["op"] == "get_object"
                  and r["object_id"].startswith("index-")]
    assert len(index_gets) == 3
    # Per-edge transfer accounting: real bytes per relay edge.
    edges = result["metrics"]["edges"]
    member_edges = {k: v for k, v in edges.items()
                    if k.startswith("pack_members:")}
    assert len(member_edges) == 3
    assert sum(e["requests"] for e in member_edges.values()) == 24
    index_edges = {k: v for k, v in edges.items()
                   if k.startswith("pack_index:")}
    assert len(index_edges) == 3
    assert not any(k.startswith("pack_fallback:")
                   for k in edges)


def test_pack_warm_one_cut_reads_only_needed_and_halo(tmp_path):
    p = perf_project(tmp_path)
    _, backend = counting_backend()
    packed = packed_sources(p, backend)
    perf_root = tmp_path / "perf"
    cold = ps.run_pipeline(
        p, perf_root, member_sources=packed["member_sources"],
        encode_roles=(), perf_label="pack-cold")
    since = len(backend.requests)

    # One-member edit inside S002's tail: output 13 needs the changed
    # member plus the untouched I003 halo member.
    edit_member(p, "S002", 7)
    repacked = packed_sources(p, backend)
    warm = ps.run_pipeline(
        p, perf_root, member_sources=repacked["member_sources"],
        encode_roles=(), perf_label="warm-cut")
    inv = warm["invalidation"]
    assert inv["dirty_frames"] == [13]
    assert inv["needed_members"] == {"I002": [7], "I003": [1]}
    assert inv["halo_members"] == {"I002": [7]}

    # The warm run transfers exactly one member: the changed one. The
    # untouched halo member comes back from the content-keyed member
    # cache — reported as reuse, not transfer.
    ranges = get_ranges(backend, since)
    assert len(ranges) == 1
    changed_pack = repacked["objects"]["S002"]["pack_object_id"]
    assert ranges[0]["object_id"] == changed_pack
    assert ranges[0]["status"] == 206
    # The fetched range is exactly member m00007 of the new pack.
    index = index_of(backend,
                     repacked["objects"]["S002"]["index_object_id"])
    member = next(m for m in index["members"]
                  if m["member_id"] == "m00007")
    assert ranges[0]["offset"] == member["byte_offset"]
    assert ranges[0]["length"] == member["byte_length"]
    # No member of the untouched packs was re-read.
    untouched = {packed["objects"]["S001"]["pack_object_id"],
                 packed["objects"]["S003"]["pack_object_id"]}
    assert member_request_targets(backend, since) & untouched == set()
    counters = warm["metrics"]["counters"]
    assert counters["member_cache_hits"] >= 1
    assert counters["member_decodes"] == 2   # m7 (changed) + m1 (halo)
    assert counters["member_fetches"] == 1
    assert counters["frame_composed"] == 1
    # Output identical to a full recompile of the edited input.
    fresh = ps.run_pipeline(
        p, tmp_path / "perf-fresh",
        member_sources=repacked["member_sources"], encode_roles=(),
        perf_label="fresh")
    assert fresh["subbed_sequence_root"] == warm["subbed_sequence_root"]
    assert file_shas(fresh["subbed_dir"], 20) == \
        file_shas(warm["subbed_dir"], 20)


# --- Range-unsupported whole-pack fallback ----------------------------------------

def test_range_fallback_counted_separately(tmp_path):
    p = perf_project(tmp_path)
    inner, backend = counting_backend()
    inner.range_supported = False
    cap = 1 << 24          # declared cap permits the whole-pack fallback
    packed = packed_sources(p, backend, whole_pack_cap=cap)
    result = ps.run_pipeline(
        p, tmp_path / "perf", member_sources=packed["member_sources"],
        encode_roles=(), perf_label="fallback")
    counters = result["metrics"]["counters"]
    assert counters["member_fetches"] == 24
    # Every range request was answered 200 whole-body — the pack_fallback
    # edges hold that whole-pack transfer separately from sparse reads.
    ranges = get_ranges(backend)
    assert ranges and all(r["status"] == 200 for r in ranges)
    pack_ids = {v["pack_object_id"]
                for v in packed["objects"].values()}
    fallback_requests = {r["object_id"] for r in ranges}
    assert fallback_requests == pack_ids
    edges = result["metrics"]["edges"]
    fallback_edges = {k: v for k, v in edges.items()
                      if k.startswith("pack_fallback:")}
    assert set(k.split(":", 1)[1] for k in fallback_edges) == pack_ids
    # Whole-pack transfer is counted: each fallback edge moved the entire
    # pack body once, not the sum of member lengths.
    for pid in pack_ids:
        assert fallback_edges[f"pack_fallback:{pid}"]["requests"] == 1
        assert fallback_edges[f"pack_fallback:{pid}"]["bytes"] > 0
    assert not any(k.startswith("pack_members:")
                   for k in edges)


def test_range_fallback_without_declared_cap_is_refused(tmp_path):
    p = perf_project(tmp_path)
    inner, backend = counting_backend()
    inner.range_supported = False
    packed = packed_sources(p, backend)          # no cap declared
    with pytest.raises(FilmError, match="RANGE_FALLBACK_DENIED"):
        ps.run_pipeline(
            p, tmp_path / "perf",
            member_sources=packed["member_sources"],
            encode_roles=(), perf_label="denied")


# --- DIRECT_DRIVE refusal ------------------------------------------------------------

def test_direct_drive_is_never_a_candidate(tmp_path):
    p = perf_project(tmp_path)
    with pytest.raises(FilmError):
        ps.refuse_direct_drive({"route": "DIRECT_DRIVE"})
    plan = ps.make_perf_plan(p)
    validate_execution_plan(plan)
    assert plan["execution"]["allowed_routes"] == ["LOCAL_NATIVE"]
    assert all(o["route"] == "LOCAL_NATIVE"
               for o in plan["operations"])
    # A forged plan pinning DIRECT_DRIVE is refused by the shared
    # validator, not only by this module.
    forged = copy.deepcopy(plan)
    forged["operations"][0]["route"] = "DIRECT_DRIVE"
    with pytest.raises(FilmError):
        validate_execution_plan(forged)
    # And a forged selection claiming DIRECT_DRIVE is refused too.
    with pytest.raises(FilmError):
        ps.select_candidates({
            "candidates": [{"candidate_id": "op-x",
                            "route": "DIRECT_DRIVE"}],
            "selections": [{"candidate_id": "op-x"}]})


# --- the benchmark --------------------------------------------------------------------

@pytest.fixture(scope="module")
def benchmark(tmp_path_factory):
    """One shared benchmark run; the work copy lives outside the project."""
    project_root = tmp_path_factory.mktemp("bench-src")
    p = perf_project(project_root)
    work_root = tmp_path_factory.mktemp("bench-work")
    report = ps.run_benchmark(p, work_root=work_root, runs=1)
    return p, report


def test_benchmark_report_is_complete_and_secret_free(benchmark):
    _, report = benchmark
    phases = report["phases"]
    assert set(phases) >= {"cold", "warm_unchanged", "warm_one_cut",
                           "interrupted_resume"}
    for name, phase in phases.items():
        assert type(phase["end_to_end_ms"]) is int
        stages = {s["stage"] for s in phase["stage_timeline"]}
        assert {"read_verify", "compose", "subbed", "encode"} <= stages
        assert type(phase["edges"]) is dict
        assert phase["peaks"]["vram_bytes"] == "UNKNOWN"
    assert report["vram_bytes"] == "UNKNOWN"
    assert report["facets"] == {
        "qualification_state": "UNQUALIFIED",
        "acceptance_state": "PENDING",
        "release_state": "NOT_AUTHORIZED",
        "node_state": "IN_PROGRESS"}
    # Same input/quality: cold and warm produce identical roots.
    assert phases["warm_unchanged"]["subbed_sequence_root"] == \
        phases["cold"]["subbed_sequence_root"]
    # Cold did all the work; warm reused it.
    assert phases["cold"]["cache"]["frame_hits"] == 0
    assert phases["warm_unchanged"]["cache"]["frame_hits"] == 20
    # The one-cut edit dirtied only its frame + downstream work.
    assert phases["warm_one_cut"]["frames"]["dirty"] == 1
    assert phases["warm_one_cut"]["frames"]["recomputed"] == 1
    # Interrupt/resume finished with the same content as a full run.
    assert phases["interrupted_resume"]["frames"]["recomputed"] \
        + phases["interrupted_resume"]["cache"]["frame_hits"] == 20
    samples = report["samples"]
    assert samples["count"] == 1
    assert samples["min_cold_ms"] <= samples["max_cold_ms"]
    assert "single run is never reported" in samples["note"]
    # Secret-free: the serialized report carries no secret-ish keys.
    text = json.dumps(report)
    for token in ("api_key", "password", "access_token",
                  "credential_value", "bearer"):
        assert token not in text.lower()
    assert report["secrets"].startswith("none")
    report_file = Path(report["report_path"])
    assert report_file.is_file()
    assert json.loads(report_file.read_text())["report_type"] == \
        "perf_benchmark"


def test_benchmark_registers_selectable_compose_candidates(benchmark):
    """The complete bound measurement makes the LOCAL_NATIVE op
    candidates selectable through ANIM-019's gate — and nothing else."""
    _, report = benchmark
    state_dir = report["project"] + "/perf-registry"
    registration = ps.register_benchmark(state_dir, report)
    assert len(registration["registered"]) == 3
    preflight_report = registration["preflight"]
    selected = {s["candidate_id"] for s in
                ps.select_candidates(preflight_report)}
    assert selected == {r["operation_id"]
                        for r in registration["registered"]}
    # The encode axis got no evidence — its candidate is UNKNOWN and
    # unselectable, never borrowed from compose timing.
    encode = next(c for c in preflight_report["candidates"]
                  if c["kind"] == "encoder")
    assert encode["selectable"] is False
    assert encode["estimate"] == "UNKNOWN"
    assert "encode:FFMPEG" in preflight_report["unknown_estimate"]
    # The per-operation gate map the executor consumes is all-green for
    # compose ops.
    gate = preflight_report["evidence"]["operations"]
    assert all(v["state"] == "QUALIFIED_FOR_SCOPE" for v in gate.values())


def test_incomplete_or_unbound_measurement_stays_unknown(benchmark):
    _, report = benchmark
    state_dir = report["project"] + "/perf-registry2"
    registration = ps.register_benchmark(state_dir, report)
    state = Path(state_dir)
    registered = registration["registered"][0]
    one = state / "capability" / "measurements" / \
        f"{registered['measurement_id']}.json"
    record = json.loads(one.read_text())
    # A structurally valid but empty measurement body — schema-complete,
    # evidence-incomplete — leaves the estimate UNKNOWN.
    empty = copy.deepcopy(record)
    for section in ("cold", "warm", "peaks", "transfer", "cache",
                    "manual", "usage", "samples"):
        empty[section] = {}
    empty["stage_timeline"] = []
    empty["shared_edge"] = {}
    assert measurement_missing(empty)
    # A measurement bound to different input digests never attaches.
    other = copy.deepcopy(record)
    other["input_digest"] = "0" * 64
    other["evidence_digest"] = "0" * 64
    from engine.capability_registry import measurements, entries
    listed = entries(state)
    candidates = registration["preflight"]["candidates"]
    op_candidate = next(c for c in candidates
                        if c["candidate_id"]
                        == registered["operation_id"])
    verdict = evaluate_candidate(
        op_candidate, listed, [empty, other] +
        [m for m in measurements(state)
         if m["measurement_id"] != record["measurement_id"]],
        observation=ps._observation_for(
            next(e for e in listed
                 if e["evidence_id"] == record["evidence_id"])))
    assert verdict["estimate"] == "UNKNOWN"
    assert verdict["selectable"] is False


def test_stale_and_fake_evidence_never_select(benchmark):
    """Without a current observation stored QUALIFIED goes STALE; a fake
    probe is DOCUMENTED_ONLY — both stay unselectable."""
    _, report = benchmark
    state_dir = report["project"] + "/perf-registry3"
    registration = ps.register_benchmark(state_dir, report)
    stale = plan_preflight(registration["plan"], state_dir=state_dir,
                           observation=None)
    assert stale["selections"] == []
    assert all(not c["selectable"] for c in stale["candidates"])
    # A fake test-double probe never qualifies, even claiming PASS.
    from engine.animation_schema import validate_capability_evidence
    from engine.capability_registry import register
    fake_probe = {
        "registry_state": "QUALIFIED_FOR_SCOPE",
        "qualification": "FAKE-synthetic",
        "reason": "test double — protocol only",
        "environment": {**ps._worker_environment(),
                        "fake_runtime": True},
        "operation": "compose_frame_range",
        "scope": {"operation": "compose_frame_range"},
        "caps": {}}
    from engine.encoder_backends import capability_evidence
    register(state_dir, validate_capability_evidence(
        capability_evidence("QUALIFIED_SERVICE", fake_probe)))
    candidates = plan_preflight(
        registration["plan"], state_dir=state_dir,
        observation=ps._observation_for(
            validate_capability_evidence(
                capability_evidence("QUALIFIED_SERVICE", fake_probe))))
    fake_verdicts = [c for c in candidates["candidates"]
                     if "fake_evidence" in
                     ",".join(str(r) for r in c["reasons"])]
    assert fake_verdicts or all(
        not c["selectable"] for c in candidates["candidates"])


# --- perf-plan / perf-benchmark CLI ----------------------------------------------------

def test_perf_plan_reports_graph_and_preflight(tmp_path, capsys):
    p = perf_project(tmp_path)
    result = ps.perf_plan_command(p)
    assert result["graph"]["nodes"]["frame_nodes"] == 20
    assert result["preflight"] is None
    assert "EXCLUDED" in result["route_policy"]["direct_drive"]
    assert result["facets"]["release_state"] == "NOT_AUTHORIZED"
    assert cli.main(["perf-plan", str(p)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["graph"]["snapshot_digest"] == \
        result["graph"]["snapshot_digest"]


def test_perf_benchmark_command_writes_report(tmp_path, capsys):
    p = perf_project(tmp_path)
    work = tmp_path / "work"
    assert cli.main(["perf-benchmark", str(p), "--no-register",
                     "--work-dir", str(work)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert Path(out["report_path"]).is_file()
    assert "warm_one_cut" in out["phases"]
    assert out["vram_bytes"] == "UNKNOWN"
    # The CLI never touched the source project's files beyond reads.
    assert not (p / "render/perf").exists()


def test_perf_plan_after_registration_shows_stale(tmp_path):
    """A benchmark's evidence registered into the project's perf
    registry makes perf-plan's preflight report real verdicts — and
    without a fresh observation they stay STALE, not assumed."""
    p = perf_project(tmp_path)
    report = ps.run_benchmark(
        p, work_root=tmp_path / "work", runs=1)
    state_dir = ps.perf_state_dir(p)
    ps.register_benchmark(state_dir, report)
    result = ps.perf_plan_command(p)
    assert result["preflight"] is not None
    assert result["preflight"]["selections"] == []
    assert all(not c["selectable"]
               for c in result["preflight"]["candidates"])
