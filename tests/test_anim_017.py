"""ANIM-017 core: dependency graph, invalidation, deterministic bounded
parallel compositing, prefetch, content-keyed cache reuse and the
interrupted/resume contract of engine/perf_scheduler.py.

All fixtures are synthetic and local; nothing here is an artwork review,
a production approval or a capability qualification — cache hits in
these tests only skip recomputation, never promote any state.
"""
import copy
import hashlib
import shutil
import threading
from pathlib import Path

import pytest
from PIL import Image

from engine import perf_scheduler as ps
from engine.animation_assets import (import_frame_sequence, load_registry,
                                     save_registry)
from engine.animation_schema import write_canon
from engine.core import FilmError, digest, read, safe_path, write
from engine.frame_clock import frame_filename
from test_anim_003 import animation_project, make_sequence
from test_anim_005 import timeline_doc

SIZE = (64, 48)


def perf_project(tmp_path, lengths=(8, 8, 8), overlaps=(2, 2), seconds=2,
                 seed=100):
    """Three-shot FRAME_ANIMATION_V1 project with crossfades: (8+8+8)-(2+2)
    = 20 output frames; transitions share output [6,8) and [12,14)."""
    p = animation_project(tmp_path, shot_count=len(lengths),
                          seconds=seconds)
    document = timeline_doc(list(lengths), list(overlaps))
    write_canon(p / "timeline/edit.json", document)
    config = read(p / "project.yaml")
    config["animation"]["output_frames"] = \
        sum(lengths) - sum(overlaps)
    write(p / "project.yaml", config)
    for i, length in enumerate(lengths):
        folder = tmp_path / f"seq_s{i + 1:03d}"
        make_sequence(folder, count=length, size=SIZE, seed=seed + i * 100)
        import_frame_sequence(p, f"S{i + 1:03d}", folder=folder)
    return p


def edit_member(p, shot_id, member_index):
    """A one-member edit through the normal import path; returns the import."""
    return ps.apply_one_cut_edit(
        p, shot_id, {member_index: ps._changed_member_png(
            seed=member_index + 5, size=SIZE)})


def file_shas(folder, count):
    return [hashlib.sha256((Path(folder) / frame_filename(i)).read_bytes())
            .hexdigest() for i in range(count)]


# --- dependency graph ---------------------------------------------------------

def test_dependency_graph_structure(tmp_path):
    p = perf_project(tmp_path)
    graph = ps.build_graph(p)
    dep = graph["dependency"]
    assert dep["shots"] == ["S001", "S002", "S003"]
    assert dep["instances"] == 3 and dep["frame_nodes"] == 20
    assert dep["member_nodes"] == 24
    # Transitions T1 [6,8) and T2 [12,14): each contributes two halo
    # member edges into the later op.
    assert dep["halo_member_edges"] == 4
    assert dep["frame_member_edges"] == 24
    assert dep["stages"] == ["read_verify", "compose", "subtitles",
                            "subbed", "encode", "verify"]
    ops = graph["ops"]
    assert [tuple(o["output_range"]) for o in ops] == \
        [(0, 6), (6, 12), (12, 20)]
    # Op boundaries tile the output exactly once.
    cursor = 0
    for op in ops:
        assert op["output_range"][0] == cursor
        cursor = op["output_range"][1]
    assert cursor == 20
    assert ops[1]["halo_members"] == {"I001": [6, 7]}
    assert ops[2]["halo_members"] == {"I002": [6, 7]}
    assert set(ops[0]["own_members"]) == set(range(6))
    assert set(ops[1]["own_members"]) == set(range(6))
    # Frame rows carry per-member sha256s and a content key — the graph
    # edge frame -> source member -> shot is explicit.
    row = graph["rows"][6]
    assert [s["local_frame_index"] for s in row["sources"]] == [6, 0]
    assert {s["instance_id"] for s in row["sources"]} == {"I001", "I002"}
    assert all(len(s["member_sha256"]) == 64 for s in row["sources"])
    assert len(row["key"]) == 64
    assert graph_report_is_plan_shaped(graph)


def graph_report_is_plan_shaped(graph):
    report = ps.graph_report(graph)
    return (report["snapshot_digest"] == graph["snapshot_digest"]
            and len(report["ops"]) == 3
            and report["member_counts"]["I001"] == 8)


# --- cold + warm unchanged ------------------------------------------------------

def test_cold_then_warm_unchanged_reuses_everything(tmp_path):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    cold = ps.run_pipeline(p, perf_root, encode_roles=(),
                           perf_label="cold")
    cold_c = cold["metrics"]["counters"]
    assert cold_c["frame_composed"] == 20
    assert cold_c["dirty_frames"] == 20
    assert cold_c["member_fetches"] == 24   # 8 members per cut, read once
    assert cold_c["member_decodes"] == 24
    assert cold["invalidation"]["reason"] == "cold"

    warm = ps.run_pipeline(p, perf_root, encode_roles=(),
                           perf_label="warm")
    warm_c = warm["metrics"]["counters"]
    assert warm["invalidation"]["unchanged"] is True
    assert warm_c.get("frame_composed", 0) == 0
    assert warm_c.get("frame_cache_hits", 0) == 20
    assert warm_c.get("subbed_cache_hits", 0) == 20
    # Cache hits reuse bytes only — they never recompose, never re-fetch.
    assert warm_c.get("member_fetches", 0) == 0
    # Byte-identical outputs across the reuse boundary.
    assert file_shas(warm["clean_dir"], 20) == \
        file_shas(cold["clean_dir"], 20)
    assert file_shas(warm["subbed_dir"], 20) == \
        file_shas(cold["subbed_dir"], 20)
    assert warm["clean_sequence_root"] == cold["clean_sequence_root"]
    assert warm["subbed_sequence_root"] == cold["subbed_sequence_root"]


# --- one-cut invalidation ------------------------------------------------------

def test_one_cut_edit_invalidates_only_affected(tmp_path):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    cold = ps.run_pipeline(p, perf_root, encode_roles=(),
                           perf_label="cold")

    # Change member 7 of the middle cut — it feeds output frame 13 inside
    # transition T2 (sources I002 m7 + I003 m1).
    edit_member(p, "S002", 7)
    warm = ps.run_pipeline(p, perf_root, encode_roles=(),
                           perf_label="one-cut")
    inv = warm["invalidation"]
    assert inv["dirty_frames"] == [13]
    assert inv["needed_members"] == {"I002": [7], "I003": [1]}
    assert inv["halo_members"] == {"I002": [7]}
    assert inv["affected_instances"] == ["I002", "I003"]
    assert inv["stale_transitions"] == ["T002"]
    warm_c = warm["metrics"]["counters"]
    assert warm_c["frame_composed"] == 1
    assert warm_c["frame_cache_hits"] == 19
    # Downstream: only the changed frame's subbed key went stale.
    assert warm_c["subbed_dirty"] == 1
    assert warm_c["subbed_burn_ranges"] == 1
    # A fresh cold recompile of the edited input produces identical bytes
    # — partial recompilation changes the schedule, never the picture.
    fresh = ps.run_pipeline(p, tmp_path / "perf-fresh", encode_roles=(),
                            perf_label="fresh")
    assert fresh["clean_sequence_root"] == warm["clean_sequence_root"]
    assert fresh["subbed_sequence_root"] == warm["subbed_sequence_root"]
    assert file_shas(fresh["subbed_dir"], 20) == \
        file_shas(warm["subbed_dir"], 20)
    assert fresh["clean_sequence_root"] != cold["clean_sequence_root"]


def test_own_member_edit_stays_inside_one_cut(tmp_path):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    ps.run_pipeline(p, perf_root, encode_roles=(), perf_label="cold")
    # Member 3 of S002 feeds only own output frame 9 — no halo needed.
    edit_member(p, "S002", 3)
    warm = ps.run_pipeline(p, perf_root, encode_roles=(),
                           perf_label="own-edit")
    inv = warm["invalidation"]
    assert inv["dirty_frames"] == [9]
    assert inv["needed_members"] == {"I002": [3]}
    assert inv["halo_members"] == {}
    assert warm["metrics"]["counters"]["frame_composed"] == 1


# --- locator-only change ---------------------------------------------------------

def _move_member(p, shot_id, member_index):
    """Relocate one member's bytes inside the project: same content, new
    locator — the graph's content keys must not change."""
    registry = load_registry(p)
    pin = registry["assignments"][shot_id]
    record = registry["assets"][pin["asset_id"]]["revisions"][
        str(pin["revision"])]
    member = next(f for f in record["files"]
                  if f["frame_index"] == member_index)
    old_rel = member["relative_name"]
    new_rel = f"animation/assets/moved/{shot_id}_{member_index:05d}.png"
    target = safe_path(p, new_rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(safe_path(p, old_rel)), target)
    member["relative_name"] = new_rel
    save_registry(p, registry)
    return old_rel, new_rel


def test_locator_only_change_never_recompiles(tmp_path):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    cold = ps.run_pipeline(p, perf_root, encode_roles=(),
                           perf_label="cold")
    _move_member(p, "S002", 4)
    warm = ps.run_pipeline(p, perf_root, encode_roles=(),
                           perf_label="locator")
    inv = warm["invalidation"]
    assert inv["unchanged"] is True
    assert inv["dirty_frames"] == []
    assert inv["reason"] == "unchanged"
    warm_c = warm["metrics"]["counters"]
    assert warm_c.get("frame_composed", 0) == 0
    assert warm_c.get("frame_cache_hits", 0) == 20
    assert warm["clean_sequence_root"] == cold["clean_sequence_root"]
    assert warm["snapshot_digest"] == cold["snapshot_digest"]


# --- deterministic bounded parallel compose --------------------------------------

def test_parallel_compose_byte_identical_to_serial(tmp_path):
    p = perf_project(tmp_path)
    serial = ps.run_pipeline(p, tmp_path / "perf-serial", workers=1,
                             encode_roles=(), perf_label="serial")
    parallel = ps.run_pipeline(p, tmp_path / "perf-par", workers=4,
                               encode_roles=(), perf_label="parallel")
    assert file_shas(parallel["clean_dir"], 20) == \
        file_shas(serial["clean_dir"], 20)
    assert file_shas(parallel["subbed_dir"], 20) == \
        file_shas(serial["subbed_dir"], 20)
    assert parallel["clean_sequence_root"] == serial["clean_sequence_root"]
    assert parallel["subbed_sequence_root"] == serial["subbed_sequence_root"]


def test_parallel_pool_is_bounded(tmp_path, monkeypatch):
    p = perf_project(tmp_path)
    active = [0]
    peak = [0]
    lock = threading.Lock()
    real_compose = ps._compose_one

    def metered(graph, row, images):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            return real_compose(graph, row, images)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(ps, "_compose_one", metered)
    ps.run_pipeline(p, tmp_path / "perf", workers=3, encode_roles=(),
                    perf_label="bounded")
    assert peak[0] <= 3
    with pytest.raises(FilmError):
        ps.run_pipeline(p, tmp_path / "perf2", workers=0,
                        encode_roles=())
    with pytest.raises(FilmError):
        ps.run_pipeline(p, tmp_path / "perf3",
                        workers=ps.MAX_WORKERS + 1, encode_roles=())


# --- prefetch -----------------------------------------------------------------

def test_prefetch_fetches_only_upcoming_members(tmp_path):
    p = perf_project(tmp_path)
    result = ps.run_pipeline(p, tmp_path / "perf", prefetch=3,
                             encode_roles=(), perf_label="prefetch")
    counters = result["metrics"]["counters"]
    assert counters["prefetch_issued"] > 0
    assert counters["prefetch_served"] > 0
    # Prefetch is deduped by content sha: at most the 24 members in flight.
    assert counters["prefetch_issued"] <= 24
    assert counters["member_fetches"] == 24
    # Same picture as the non-prefetch serial run.
    serial = ps.run_pipeline(p, tmp_path / "perf-serial",
                             encode_roles=(), perf_label="serial")
    assert serial["clean_sequence_root"] == result["clean_sequence_root"]


# --- interrupted then resumed ----------------------------------------------------

def test_interrupted_run_resumes_from_content_cache(tmp_path):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    with pytest.raises(ps.PerfInterrupted):
        ps.run_pipeline(p, perf_root, interrupt_after=7,
                        encode_roles=(), perf_label="interrupted")
    # The interrupted run commits no state — only content-keyed cache
    # entries survive.
    assert not (perf_root / "state.json").is_file()
    resume = ps.run_pipeline(p, perf_root, encode_roles=(),
                             perf_label="resumed")
    counters = resume["metrics"]["counters"]
    assert counters["frame_cache_hits"] == 7
    assert counters["frame_composed"] == 13
    full = ps.run_pipeline(p, tmp_path / "perf-full", encode_roles=(),
                           perf_label="full")
    assert resume["clean_sequence_root"] == full["clean_sequence_root"]
    assert resume["subbed_sequence_root"] == full["subbed_sequence_root"]


# --- preservation fences -----------------------------------------------------------

def test_audio_lyrics_lock_and_review_state_untouched(tmp_path):
    p = perf_project(tmp_path)
    config = read(p / "project.yaml")
    master = p / config["audio"]["path"]
    watched = [p / "project.yaml", master,
               p / "lyrics/lyrics_timed.json",
               p / "timeline/edit.json", p / "manifest/shots.json"]
    before = {f: digest(f) for f in watched if f.is_file()}
    ps.run_pipeline(p, tmp_path / "perf", encode_roles=(),
                    perf_label="run")
    assert {f: digest(f) for f in watched if f.is_file()} == before
    # No state file in the project itself — everything the scheduler
    # writes lives under its own perf_root.
    assert not (p / "render/perf").exists()


def test_master_encode_carries_original_audio(tmp_path):
    """Both masters encode with the verified master AAC track, never a
    re-encode — the encode record proves packet-copy audio."""
    p = perf_project(tmp_path)
    result = ps.run_pipeline(p, tmp_path / "perf", perf_label="full")
    assert set(result["encodes"]) == {"clean", "subbed"}
    subbed = result["encodes"]["subbed"]
    assert subbed["driver"] == "FFMPEG"
    assert subbed["verification"]["audio"]["matches_reference"] is True
    assert subbed["qualification_state"] == "UNQUALIFIED"
    # A warm re-run hits the artifact cache and returns byte-identical
    # masters whose recorded verification still binds them.
    perf_root = Path(result["perf_root"])
    warm = ps.run_pipeline(p, perf_root, perf_label="warm")
    assert warm["metrics"]["counters"]["encode_cache_hits"] == 2
    assert warm["encodes"]["subbed"]["cached"] is True
    assert warm["encodes"]["subbed"]["output_sha256"] == \
        subbed["output_sha256"]
    target = Path(warm["run_dir"]) / "MASTER_SUBBED.mp4"
    assert hashlib.sha256(target.read_bytes()).hexdigest() == \
        subbed["output_sha256"]


def test_workers_one_is_the_serial_default(tmp_path):
    p = perf_project(tmp_path)
    a = ps.run_pipeline(p, tmp_path / "a", encode_roles=())
    b = ps.run_pipeline(p, tmp_path / "b", workers=1, encode_roles=())
    assert a["clean_sequence_root"] == b["clean_sequence_root"]
