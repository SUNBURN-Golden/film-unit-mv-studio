"""ANIM-020: canonical render provenance, invalidate closure, partial rebuild.

Covers REBUILD-CLOSURE / REBUILD-COLD-WARM / MANIFEST-CYCLE of
engine/render_provenance.py on top of the ANIM-017 scheduler: layered content
keys and the render_manifest, the change-closure rebuild plan (actual
read/decode/compose/encode scope per change class), the source/member/halo
read plan, the stale-approval projection and cold/warm equality. Every
fixture is synthetic and local — no artwork review, no production
qualification, no release authority.
"""
import copy
import hashlib
import json

import pytest

from engine import cli
from engine import perf_scheduler as ps
from engine import render_provenance as rp
from engine.animation_assets import import_layer_rgba
from engine.animation_locks import (declare_waves, lock_status,
                                    record_final_lock, record_plan_lock,
                                    record_route_decision, record_wave_lock)
from engine.animation_review import (record_cut_review,
                                     record_transition_review, review_status)
from engine.animation_schema import canon_bytes, read_canon, write_canon
from engine.core import FilmError, digest, read, safe_path, write
from engine.lyrics import save_timing
from test_anim_003 import rgba_frame
from test_anim_017 import _move_member, edit_member, file_shas, perf_project
from test_compiler_v03 import fixture_project


# --- MANIFEST-CYCLE ---------------------------------------------------------------

def test_manifest_canonical_cycle(tmp_path):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    result = ps.run_pipeline(p, perf_root, perf_label="cold")
    manifest = rp.build_manifest(p, state=ps._load_state(perf_root))
    assert manifest["document_type"] == "render_manifest"
    assert manifest["schema_version"] == 1
    raw = canon_bytes(manifest)
    assert raw == canon_bytes(json.loads(raw))       # canonical round trip
    assert rp.manifest_digest(manifest) == hashlib.sha256(raw).hexdigest()
    path = tmp_path / "manifest.json"
    write_canon(path, manifest)
    assert path.read_bytes() == raw
    assert read_canon(path) == manifest
    # The committed roots and encode digests land in the manifest.
    clean = manifest["sequences"]["clean"]
    subbed = manifest["sequences"]["subbed"]
    assert clean["sequence_root"] == result["clean_sequence_root"]
    assert subbed["sequence_root"] == result["subbed_sequence_root"]
    assert clean["sequence_root"] != subbed["sequence_root"]  # never merged
    assert clean["rendered"] is True and clean["frame_count"] == 20
    assert manifest["encodes"]["subbed"]["encode_digest"] == \
        result["encodes"]["subbed"]["encode_digest"]
    assert manifest["encodes"]["clean"]["driver"] == "FFMPEG"
    assert manifest["dependency_order"] == rp.DEPENDENCY_ORDER
    # Without a committed state the manifest is still valid, roots null.
    empty = rp.build_manifest(p)
    assert empty["sequences"]["clean"]["sequence_root"] is None
    assert empty["sequences"]["clean"]["rendered"] is False
    assert empty["encodes"]["clean"] is None


def test_manifest_rejects_contamination(tmp_path):
    p = perf_project(tmp_path)
    manifest = rp.build_manifest(p)
    # A self digest inside the document is an unknown field, not a digest.
    tampered = copy.deepcopy(manifest)
    tampered["manifest_digest"] = "0" * 64
    with pytest.raises(FilmError):
        rp.validate_render_manifest(tampered)
    # Approval state cannot be smuggled into the manifest either.
    tampered = copy.deepcopy(manifest)
    tampered["approval"] = {"state": "APPROVED"}
    with pytest.raises(FilmError):
        rp.validate_render_manifest(tampered)
    tampered = copy.deepcopy(manifest)
    tampered["sources"][0]["status"] = "CURRENT"
    with pytest.raises(FilmError):
        rp.validate_render_manifest(tampered)
    # Floats/NaN fail the canonical serializer check inside validation.
    tampered = copy.deepcopy(manifest)
    tampered["frame_count"] = 20.0
    with pytest.raises(FilmError):
        rp.validate_render_manifest(tampered)
    # clean/subbed sharing one root is refused.
    tampered = copy.deepcopy(manifest)
    for role in ("clean", "subbed"):
        tampered["sequences"][role].update(
            {"sequence_root": "a" * 64, "rendered": True, "frame_count": 20})
    with pytest.raises(FilmError, match="sequence_root"):
        rp.validate_render_manifest(tampered)
    # Unknown/future schema versions are rejected.
    tampered = copy.deepcopy(manifest)
    tampered["schema_version"] = 2
    with pytest.raises(FilmError, match="Unsupported"):
        rp.validate_render_manifest(tampered)


def test_manifest_file_rejects_noncanonical_and_duplicate_keys(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_bytes(b'{"document_type":"render_manifest","schema_version":1,'
                     b'"schema_version":1}\n')
    with pytest.raises(FilmError):
        read_canon(path)
    # Canonical content written in a non-canonical byte layout is also refused.
    path.write_text(json.dumps({"document_type": "render_manifest",
                                "schema_version": 1}, indent=2),
                    encoding="utf-8")
    with pytest.raises(FilmError):
        read_canon(path)


def test_content_material_excludes_locator_status_approval_self_digest():
    for key in ("locator", "status", "state", "approval", "accepted",
                "reviewer", "digest", "manifest_digest", "self_digest",
                "path"):
        with pytest.raises(FilmError, match="content key"):
            rp.content_digest({"members": [{"index": 0, key: "x"}]})
    # Floats, NaN and out-of-range integers fail the canonical serializer.
    for bad in (1.5, float("nan"), 2**63):
        with pytest.raises(FilmError):
            rp.content_digest({"v": bad})
    # Integers and reduced rationals hash deterministically.
    base = rp.content_digest({"weight": [7, 13], "offset": -5})
    assert base == rp.content_digest({"weight": [7, 13], "offset": -5})
    assert base != rp.content_digest({"weight": [6, 13], "offset": -5})
    # Ordered member/frame/weight data is order-sensitive.
    members = [{"index": 0, "sha256": "a" * 64},
               {"index": 1, "sha256": "b" * 64}]
    assert rp.content_digest({"members": members}) != \
        rp.content_digest({"members": members[::-1]})


def test_approvals_never_enter_content_keys(tmp_path):
    p = perf_project(tmp_path)
    before = rp.build_manifest(p)
    record_cut_review(p, "I001", reviewer="fixture",
                      methods=["CUT_FULL_SPEED_PLAYBACK"])
    after = rp.build_manifest(p)
    assert after == before
    assert rp.manifest_digest(after) == rp.manifest_digest(before)


def test_toolchain_digest_is_fixed_and_qualified():
    a = rp.toolchain_digest(ffmpeg_version="ffmpeg 7.1-test")
    assert a == rp.toolchain_digest(ffmpeg_version="ffmpeg 7.1-test")
    assert a != rp.toolchain_digest(ffmpeg_version="ffmpeg 6.1-test")
    assert len(rp.toolchain_digest()) == 64   # environment probe, may be UNKNOWN


def test_frame_map_digest_binds_ordered_frames_sources_weights(tmp_path):
    p = perf_project(tmp_path)
    rows = ps.build_graph(p)["rows"]
    base = rp.frame_map_digest(rows)
    assert base == rp.frame_map_digest(rows)
    swapped = copy.deepcopy(rows)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    assert rp.frame_map_digest(swapped) != base        # frame order binds
    tampered = copy.deepcopy(rows)
    tampered[6]["sources"][0]["weight"] = [5, 13]      # was [2,3]
    assert rp.frame_map_digest(tampered) != base       # rational weights bind
    reversed_src = copy.deepcopy(rows)
    reversed_src[6]["sources"].reverse()
    assert rp.frame_map_digest(reversed_src) != base   # source order binds
    # Rendered output hashes only participate in the complete digest.
    complete = copy.deepcopy(rows)
    for row in complete:
        row["output_sha256"] = "0" * 64
    assert rp.frame_map_digest(complete) == base
    assert rp.frame_map_digest(complete, complete=True) != base


def test_manifest_tracks_content_not_locator_or_state(tmp_path):
    p = perf_project(tmp_path)
    first = rp.build_manifest(p)
    _move_member(p, "S002", 4)
    second = rp.build_manifest(p)
    assert second == first            # a locator move changes no content key
    edit_member(p, "S002", 4)
    third = rp.build_manifest(p)
    sources = {s["instance_id"]: s["source_digest"]
               for s in third["sources"]}
    old = {s["instance_id"]: s["source_digest"] for s in second["sources"]}
    assert sources["I002"] != old["I002"]
    assert sources["I001"] == old["I001"]
    assert sources["I003"] == old["I003"]
    assert third["source_digest"] != second["source_digest"]
    assert third["snapshot_digest"] != second["snapshot_digest"]
    assert third["edit_digest"] != second["edit_digest"]  # adopted revision moved
    assert third["recipe_digest"] == second["recipe_digest"]
    assert third["toolchain_digest"] == second["toolchain_digest"]


# --- REBUILD-CLOSURE ---------------------------------------------------------------

def test_picture_change_plan_matches_actual_scope(tmp_path):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    ps.run_pipeline(p, perf_root, perf_label="cold")
    plan = rp.plan_rebuild(
        p, [{"class": "PICTURE", "instance_id": "I002", "member_index": 7}])
    closure = plan["closure"]
    assert closure["dirty_frames"] == [13]
    assert closure["compose_ranges"] == [[13, 14]]
    assert closure["needed_members"] == {"I002": [7], "I003": [1]}
    assert closure["halo_members"] == {"I002": [7]}
    assert closure["stale_transitions"] == ["T002"]
    assert closure["encode_roles"] == ["clean", "subbed"]
    reads = plan["read_plan"]
    assert [m["member_index"] for m in reads["I002"]["members"]] == [7]
    assert reads["I002"]["members"][0]["halo"] is True
    assert reads["I002"]["members"][0]["byte_length"] > 0
    assert [m["member_index"] for m in reads["I003"]["members"]] == [1]
    assert reads["I003"]["members"][0]["halo"] is False
    proj = plan["stale_approval_projection"]
    assert proj["cuts"]["I002"]["state"] == "STALE"
    assert proj["cuts"]["I001"]["state"] == "KEPT"
    assert proj["cuts"]["I003"]["state"] == "KEPT"
    assert proj["transitions"]["T001"]["state"] == "STALE"  # binds I002 digest
    assert proj["transitions"]["T002"]["state"] == "STALE"
    assert proj["locks"]["PLAN_LOCK"]["state"] == "KEPT"
    assert proj["locks"]["FINAL_LOCK"]["state"] == "STALE"
    assert proj["final_film"]["state"] == "STALE"
    assert "CUT:I002" in proj["stale"]
    assert "CUT:I001" in proj["kept"]

    edit_member(p, "S002", 7)
    warm = ps.run_pipeline(p, perf_root, perf_label="warm")
    actual = rp.actual_scope(warm)
    assert actual["dirty_frames"] == closure["dirty_frames"]
    assert actual["compose_ranges"] == closure["compose_ranges"]
    assert actual["needed_members"] == closure["needed_members"]
    assert actual["halo_members"] == closure["halo_members"]
    assert actual["stale_transitions"] == closure["stale_transitions"]
    assert actual["member_reads"] == 2 and actual["member_decodes"] == 2
    assert actual["frame_composed"] == 1 and actual["subbed_dirty"] == 1
    assert actual["encode_runs"] == 2          # both MP4s re-encoded


def test_whole_sequence_change_closes_the_cut(tmp_path):
    p = perf_project(tmp_path)
    plan = rp.plan_rebuild(p, [{"class": "SEQUENCE", "instance_id": "I002"}])
    closure = plan["closure"]
    # Every output frame an I002 member feeds — own range plus both halos.
    assert closure["dirty_frames"] == [6, 7, 8, 9, 10, 11, 12, 13]
    assert closure["needed_members"]["I002"] == list(range(8))
    assert closure["needed_members"]["I001"] == [6, 7]
    assert closure["needed_members"]["I003"] == [0, 1]
    proj = plan["stale_approval_projection"]
    assert proj["cuts"]["I002"]["state"] == "STALE"
    assert proj["cuts"]["I001"]["state"] == "KEPT"
    assert proj["transitions"]["T001"]["state"] == "STALE"
    assert proj["transitions"]["T002"]["state"] == "STALE"


def test_transition_change_plan_and_actual_scope(tmp_path):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    ps.run_pipeline(p, perf_root, perf_label="cold")
    plan = rp.plan_rebuild(p, [{"class": "TRANSITION",
                                "transition_id": "T001",
                                "set": {"overlap_frames": 4}}])
    closure = plan["closure"]
    # (8+8+8) - (4+2) = 18; a length change invalidates the whole map, as
    # the scheduler's invalidation reports it.
    assert closure["output_frames"] == {"before": 20, "after": 18}
    assert closure["dirty_frames"] == list(range(18))
    proj = plan["stale_approval_projection"]
    assert proj["transitions"]["T001"]["state"] == "STALE"
    assert proj["transitions"]["T002"]["state"] == "STALE"  # its range moved
    assert all(c["state"] == "KEPT" for c in proj["cuts"].values())
    assert proj["locks"]["PLAN_LOCK"]["state"] == "STALE"
    assert proj["locks"]["FINAL_LOCK"]["state"] == "STALE"
    assert proj["final_film"]["state"] == "STALE"
    # Apply the same edit through the canonical document and run it.
    doc = read_canon(p / "timeline/edit.json")
    doc["entries"][0]["transition_out"]["overlap_frames"] = 4
    doc["target_frames"] = 18
    write_canon(p / "timeline/edit.json", doc)
    config = read(p / "project.yaml")
    config["animation"]["output_frames"] = 18
    write(p / "project.yaml", config)
    warm = ps.run_pipeline(p, perf_root, perf_label="transition")
    actual = rp.actual_scope(warm)
    assert actual["dirty_frames"] == closure["dirty_frames"]
    assert actual["needed_members"] == closure["needed_members"]
    assert actual["stale_transitions"] == ["T001", "T002"]


def test_font_change_rebuilds_subbed_only(tmp_path):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    cold = ps.run_pipeline(p, perf_root, perf_label="cold")
    plan = rp.plan_rebuild(p, [{"class": "FONT"}])
    closure = plan["closure"]
    assert closure["dirty_frames"] == []
    assert closure["unchanged"] is True
    assert closure["subbed_dirty_frames"] == list(range(20))
    assert closure["encode_roles"] == ["subbed"]   # clean MP4 is reusable
    proj = plan["stale_approval_projection"]
    # Font/cue edits keep the clean cut motion review — only subbed and
    # the deliverable bindings go stale.
    assert all(c["state"] == "KEPT" for c in proj["cuts"].values())
    assert all(t["state"] == "KEPT" for t in proj["transitions"].values())
    assert proj["locks"]["PLAN_LOCK"]["state"] == "KEPT"
    assert proj["locks"]["FINAL_LOCK"]["state"] == "STALE"
    assert proj["final_film"]["state"] == "STALE"

    config = read(p / "project.yaml")
    config["subtitles"] = dict(config.get("subtitles") or {}, font_size=12)
    write(p / "project.yaml", config)
    # A font change stales the lyric-review binding; the honest re-approval
    # step re-binds it before the subbed layer can rebuild.
    save_timing(p, read(p / "lyrics/lyrics_timed.json"),
                reviewer="fixture font re-review")
    warm = ps.run_pipeline(p, perf_root, perf_label="font")
    counters = warm["metrics"]["counters"]
    assert counters.get("frame_composed", 0) == 0
    assert counters.get("frame_cache_hits", 0) == 20   # clean PNGs reused
    assert counters["subbed_dirty"] == 20              # all reburned
    assert counters.get("subbed_cache_hits", 0) == 0
    assert counters["encode_runs"] == 1                # only MASTER_SUBBED
    assert counters["encode_cache_hits"] == 1          # clean MP4 reused
    assert warm["clean_sequence_root"] == cold["clean_sequence_root"]
    assert warm["subbed_sequence_root"] != cold["subbed_sequence_root"]
    status = review_status(p)
    assert all(r["state"] != "STALE" for r in status["targets"].values())


def test_cue_change_rebuilds_subbed_only_and_keeps_source(tmp_path):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    ps.run_pipeline(p, perf_root, perf_label="cold")
    source_sha = digest(p / "input/lyrics.txt")
    plan = rp.plan_rebuild(p, [{"class": "CUE"}])
    assert plan["closure"]["dirty_frames"] == []
    assert plan["closure"]["encode_roles"] == ["subbed"]
    proj = plan["stale_approval_projection"]
    assert all(c["state"] == "KEPT" for c in proj["cuts"].values())
    assert proj["final_film"]["state"] == "STALE"

    # An allowed cue change: re-time a cue without touching its text.
    # Ending cue 1 at 400ms visibly changes the subbed frames inside the
    # 20-frame output window.
    document = read(p / "lyrics/lyrics_timed.json")
    document["cues"][0]["end_ms"] = 400
    save_timing(p, document, reviewer="fixture re-time")
    assert digest(p / "input/lyrics.txt") == source_sha
    # An unallowed cue change — rewritten lyric text — is refused outright.
    forged = read(p / "lyrics/lyrics_timed.json")
    forged["cues"][0]["text"] = "forged lyric text"
    with pytest.raises(FilmError):
        save_timing(p, forged, reviewer="x")
    before = ps._load_state(perf_root)
    warm = ps.run_pipeline(p, perf_root, perf_label="cue")
    counters = warm["metrics"]["counters"]
    assert counters.get("frame_composed", 0) == 0
    assert counters["subbed_dirty"] == 20
    assert counters["encode_runs"] == 1            # only MASTER_SUBBED
    assert counters["encode_cache_hits"] == 1      # clean MP4 reused
    assert warm["subbed_sequence_root"] != before["subbed"]["sequence_root"]
    assert warm["clean_sequence_root"] == before["clean"]["sequence_root"]
    assert digest(p / "input/lyrics.txt") == source_sha


def test_encoder_change_reuses_pngs_but_needs_new_mp4(tmp_path):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    cold = ps.run_pipeline(p, perf_root, perf_label="cold")
    plan = rp.plan_rebuild(
        p, [{"class": "ENCODER", "output_spec_changed": True}])
    closure = plan["closure"]
    assert closure["dirty_frames"] == []
    assert closure["subbed_dirty_frames"] == []
    assert closure["encode_roles"] == ["clean", "subbed"]
    proj = plan["stale_approval_projection"]
    assert proj["final_film"]["state"] == "STALE"
    assert proj["locks"]["FINAL_LOCK"]["state"] == "STALE"  # output spec bound
    assert proj["locks"]["PLAN_LOCK"]["state"] == "STALE"
    assert all(c["state"] == "KEPT" for c in proj["cuts"].values())

    config = read(p / "project.yaml")
    config["format"]["crf"] = 28
    write(p / "project.yaml", config)
    warm = ps.run_pipeline(p, perf_root, perf_label="encoder")
    counters = warm["metrics"]["counters"]
    assert counters.get("frame_composed", 0) == 0
    assert counters.get("frame_cache_hits", 0) == 20
    assert counters.get("subbed_cache_hits", 0) == 20   # PNG reuse
    assert counters.get("subbed_dirty", 0) == 0
    assert counters["encode_runs"] == 2                 # new MP4s required
    assert counters.get("encode_cache_hits", 0) == 0
    assert warm["clean_sequence_root"] == cold["clean_sequence_root"]
    assert warm["subbed_sequence_root"] == cold["subbed_sequence_root"]
    assert warm["encodes"]["subbed"]["encode_digest"] != \
        cold["encodes"]["subbed"]["encode_digest"]
    assert warm["encodes"]["subbed"]["artifact_sha256"] != \
        cold["encodes"]["subbed"]["artifact_sha256"]
    # The new MP4 needs a fresh final approval; nothing is auto-promoted.
    assert warm["encodes"]["subbed"]["qualification_state"] == "UNQUALIFIED"


def test_locator_change_is_read_plan_only(tmp_path):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    cold = ps.run_pipeline(p, perf_root, perf_label="cold")
    plan = rp.plan_rebuild(p, [{"class": "LOCATOR", "instance_id": "I002"}])
    assert plan["closure"]["dirty_frames"] == []
    assert plan["closure"]["encode_roles"] == []
    assert plan["stale_approval_projection"]["stale"] == []
    _move_member(p, "S002", 4)
    warm = ps.run_pipeline(p, perf_root, perf_label="locator")
    actual = rp.actual_scope(warm)
    assert actual["unchanged"] is True
    assert actual["frame_composed"] == 0
    assert actual["member_reads"] == 0
    assert warm["clean_sequence_root"] == cold["clean_sequence_root"]
    assert warm["snapshot_digest"] == cold["snapshot_digest"]


def test_unadopted_candidate_stays_outside_the_closure(tmp_path):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    ps.run_pipeline(p, perf_root, perf_label="cold")
    import_layer_rgba(p, rgba_frame(tmp_path / "candidate.png"))
    plan = rp.plan_rebuild(p, [{"class": "UNADOPTED_CANDIDATE"}])
    assert plan["closure"]["dirty_frames"] == []
    assert plan["closure"]["encode_roles"] == []
    assert plan["stale_approval_projection"]["stale"] == []
    warm = ps.run_pipeline(p, perf_root, perf_label="candidate")
    assert warm["invalidation"]["unchanged"] is True
    # Review memos and preparation records are metadata-only too.
    for cls in ("REVIEW_MEMO", "PREPARATION"):
        plan = rp.plan_rebuild(p, [{"class": cls}])
        assert plan["closure"]["dirty_frames"] == []
        assert plan["stale_approval_projection"]["stale"] == []


def test_used_range_change_closes_whole_map(tmp_path):
    p = perf_project(tmp_path)
    plan = rp.plan_rebuild(
        p, [{"class": "USED_RANGE", "instance_id": "I002",
             "used_source_range": [0, 6]}])
    closure = plan["closure"]
    # (8+6+8) - (2+2) = 18; a length change invalidates the whole map.
    assert closure["output_frames"] == {"before": 20, "after": 18}
    assert closure["dirty_frames"] == list(range(18))
    proj = plan["stale_approval_projection"]
    assert proj["cuts"]["I002"]["state"] == "STALE"
    assert proj["cuts"]["I001"]["state"] == "KEPT"
    assert proj["transitions"]["T001"]["state"] == "STALE"
    assert proj["transitions"]["T002"]["state"] == "STALE"
    assert proj["locks"]["PLAN_LOCK"]["state"] == "STALE"


# --- REBUILD-COLD-WARM ---------------------------------------------------------------

def test_cold_warm_equality_after_picture_change(tmp_path):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    cold = ps.run_pipeline(p, perf_root, perf_label="cold")
    cold_shas = file_shas(cold["clean_dir"], 20)
    config = read(p / "project.yaml")
    audio_sha = digest(safe_path(p, config["audio"]["path"]))
    timing_sha = digest(p / "lyrics/lyrics_timed.json")
    source_sha = digest(p / "input/lyrics.txt")

    edit_member(p, "S002", 7)
    warm = ps.run_pipeline(p, perf_root, perf_label="warm")
    fresh = ps.run_pipeline(p, tmp_path / "fresh", perf_label="fresh")
    # Same env/input/quality: warm and fresh-cold impact hashes and the
    # frame_map must match.
    comparison = rp.compare_runs(warm, fresh)
    assert comparison["equal"], comparison["checks"]
    graph = ps.build_graph(p)
    comparison = rp.compare_runs(warm, fresh,
                               rows=(graph["rows"], graph["rows"]))
    assert comparison["equal"], comparison["checks"]
    manifest_warm = rp.build_manifest(p, state=ps._load_state(perf_root))
    manifest_fresh = rp.build_manifest(
        p, state=ps._load_state(tmp_path / "fresh"))
    assert manifest_warm["frame_map_digest"] == \
        manifest_fresh["frame_map_digest"]
    assert manifest_warm["sequences"] == manifest_fresh["sequences"]
    assert manifest_warm["source_digest"] == manifest_fresh["source_digest"]
    # Unaffected bytes are untouched; exactly frame 13 moved.
    warm_shas = file_shas(warm["clean_dir"], 20)
    changed = [i for i, pair in enumerate(zip(cold_shas, warm_shas))
               if pair[0] != pair[1]]
    assert changed == [13]
    # Original audio, lyric source text and approved cue timing never move.
    assert digest(safe_path(p, config["audio"]["path"])) == audio_sha
    assert digest(p / "lyrics/lyrics_timed.json") == timing_sha
    assert digest(p / "input/lyrics.txt") == source_sha


def test_warm_equals_cold_when_nothing_changed(tmp_path):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    cold = ps.run_pipeline(p, perf_root, perf_label="cold")
    warm = ps.run_pipeline(p, perf_root, perf_label="warm")
    assert rp.compare_runs(warm, cold)["equal"]
    actual = rp.actual_scope(warm)
    assert actual["unchanged"] is True
    assert actual["member_reads"] == 0
    assert actual["frame_composed"] == 0
    assert actual["encode_runs"] == 0
    assert actual["encode_cache_hits"] == 2


# --- stale-approval projection vs the engine ------------------------------------------

def _locked_and_reviewed(p):
    declare_waves(p, [
        {"wave": "W00", "shots": ["S001"],
         "difficulty": [{"type": "transition_density",
                         "reason": "fixture"}], "note": ""},
        {"wave": "W01", "shots": ["S002", "S003"],
         "difficulty": [], "note": ""}])
    record_plan_lock(p, "planner")
    record_wave_lock(p, "W00", "planner")
    record_cut_review(p, "I001", reviewer="r",
                      methods=["CUT_FULL_SPEED_PLAYBACK"])
    record_route_decision(p, "W00", decision="KEEP", decider="lead",
                          approver="director",
                          checked_types=["transition_density"],
                          unchecked_types=[], apply_scope=["W01"],
                          reviewed_conditions=["fixture conditions"],
                          observations=["fixture observation"])
    record_wave_lock(p, "W01", "planner")
    for iid in ("I002", "I003"):
        record_cut_review(p, iid, reviewer="r",
                          methods=["CUT_FULL_SPEED_PLAYBACK"])
    for tid in ("T001", "T002"):
        record_transition_review(p, tid, reviewer="r",
                                 methods=["TRANSITION_FULL_SPEED_PLAYBACK"])
    record_final_lock(p, "director")


def test_projection_matches_engine_statuses_after_edit(tmp_path):
    p = perf_project(tmp_path)
    _locked_and_reviewed(p)
    plan = rp.plan_rebuild(
        p, [{"class": "PICTURE", "instance_id": "I002", "member_index": 3}])
    proj = plan["stale_approval_projection"]
    edit_member(p, "S002", 3)
    status = review_status(p)
    locks = lock_status(p)
    for iid, scope in proj["cuts"].items():
        expected = "STALE" if scope["state"] == "STALE" else "CURRENT"
        assert status["targets"][iid]["state"] == expected, iid
    for tid, scope in proj["transitions"].items():
        expected = "STALE" if scope["state"] == "STALE" else "CURRENT"
        assert status["targets"][tid]["state"] == expected, tid
    assert proj["locks"]["WAVE_LOCK:W00"]["state"] == "KEPT"
    assert proj["locks"]["WAVE_LOCK:W01"]["state"] == "STALE"
    assert locks["waves"]["W00"]["state"] == "CURRENT"
    assert locks["waves"]["W01"]["state"] == "STALE"
    assert locks["plan"]["state"] == "CURRENT"
    assert locks["final"]["state"] == "STALE"
    assert proj["locks"]["PLAN_LOCK"]["state"] == "KEPT"
    assert proj["locks"]["FINAL_LOCK"]["state"] == "STALE"


# --- reports, CLI, scope honesty ---------------------------------------------------------

def test_provenance_report_shape(tmp_path):
    p = perf_project(tmp_path)
    report = rp.provenance_report(p, perf_root=tmp_path / "perf")
    assert report["kind"] == "render_provenance_report"
    assert report["render_state"] == "PENDING_NO_RENDER_STATE"
    assert report["manifest"]["document_type"] == "render_manifest"
    assert len(report["manifest_digest"]) == 64
    assert report["graph"]["output_frames"] == 20
    proj = report["stale_approval_projection"]
    assert proj["stale"] == [] and proj["current"] == []
    assert "CUT:I001" in proj["pending"]
    assert report["facets"]["qualification_state"] == "UNQUALIFIED"
    assert report["facets"]["release_state"] == "NOT_AUTHORIZED"
    perf_root = tmp_path / "perf"
    ps.run_pipeline(p, perf_root, perf_label="cold")
    report = rp.provenance_report(p, perf_root=perf_root)
    assert report["render_state"] == "COMMITTED"
    assert report["manifest"]["sequences"]["clean"]["rendered"] is True


def test_scope_notes_claim_no_gpu_replay_or_generation_recall(tmp_path):
    p = perf_project(tmp_path)
    plan = rp.plan_rebuild(p, [{"class": "FONT"}])
    notes = " ".join(plan["scope_notes"])
    assert "GPU" in notes and "never claimed" in notes
    assert "generation re-call" in notes and "replay" in notes
    assert plan["facets"]["qualification_state"] == "UNQUALIFIED"
    assert plan["facets"]["acceptance_state"] == "PENDING"
    assert plan["facets"]["release_state"] == "NOT_AUTHORIZED"


def test_cli_provenance_report_and_rebuild_plan(tmp_path, capsys):
    p = perf_project(tmp_path)
    perf_root = tmp_path / "perf"
    ps.run_pipeline(p, perf_root, perf_label="cold")
    assert cli.main(["provenance-report", str(p),
                     "--perf-root", str(perf_root)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["kind"] == "render_provenance_report"
    assert report["render_state"] == "COMMITTED"
    changes = tmp_path / "changes.json"
    changes.write_text(json.dumps(
        [{"class": "PICTURE", "instance_id": "I002", "member_index": 7}]),
        encoding="utf-8")
    assert cli.main(["rebuild-plan", str(p), "--changes", str(changes)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["kind"] == "rebuild_plan"
    assert plan["closure"]["dirty_frames"] == [13]
    assert "CUT:I002" in plan["stale_approval_projection"]["stale"]


def test_rebuild_plan_rejects_bad_changes(tmp_path):
    p = perf_project(tmp_path)
    with pytest.raises(FilmError, match="unknown change class"):
        rp.plan_rebuild(p, [{"class": "MAGIC"}])
    with pytest.raises(FilmError, match="member_index"):
        rp.plan_rebuild(p, [{"class": "PICTURE", "instance_id": "I002"}])
    with pytest.raises(FilmError, match="not in the adopted"):
        rp.plan_rebuild(p, [{"class": "PICTURE", "instance_id": "I002",
                             "member_index": 99}])
    with pytest.raises(FilmError, match="unknown transition"):
        rp.plan_rebuild(p, [{"class": "TRANSITION", "transition_id": "T099"}])
    with pytest.raises(FilmError, match="unknown instance"):
        rp.plan_rebuild(p, [{"class": "PICTURE", "shot_id": "S999",
                             "member_index": 0}])
    with pytest.raises(FilmError, match="object"):
        rp.plan_rebuild(p, ["not a dict"])


def test_legacy_mv_projects_are_rejected(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1)
    with pytest.raises(FilmError, match="LEGACY_MV"):
        rp.provenance_report(p)
    with pytest.raises(FilmError, match="LEGACY_MV"):
        rp.plan_rebuild(p, [{"class": "FONT"}])
    assert cli.main(["provenance-report", str(p)]) == 1
    changes = tmp_path / "c.json"
    changes.write_text("[]", encoding="utf-8")
    assert cli.main(["rebuild-plan", str(p), "--changes", str(changes)]) == 1
