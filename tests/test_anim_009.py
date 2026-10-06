"""ANIM-009: A-path control images, detailed work packets, manual hand-off
and packet-verified draft frame import.

Fixtures are real Pillow-drawn PNGs whose pixel content differs by frame
seed; nothing here contacts a provider, automates a consumer UI, approves
artwork or qualifies production (DRAFT / UNQUALIFIED by construction).
"""
import copy
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from engine import cli
from engine.animation_assets import (commit_draft_frames, content_digest,
                                     import_draft_image, import_layer_rgba,
                                     import_mask, import_replacement_drawing,
                                     load_draft_frames, load_registry,
                                     resolve_shot_sequence)
from engine.animation_schema import read_canon, write_canon
from engine.compiler import compile_preview
from engine.compositor import composite_shot
from engine.core import FilmError, digest, safe_path
from engine.frame_sequence import (animation_validate,
                                   normalize_shot_sequence)
from engine.motion_plan import (UNIMPLEMENTED_PATHS, plan_path,
                                save_shot_plan, validate_shot_plan)
from engine.packets import (export_work_packets, load_work_packet,
                            validate_work_packet)
from test_anim_008 import (_exposure, _mask_half, _sprite, c_project, scene,
                           scene_plan)
from test_compiler_v03 import fixture_project, newest_build

W, H, FRAMES = 64, 48, 24


def _frame_png(path, seed):
    """A real 64x48 PNG; every seed produces different pixels and bytes."""
    im = Image.new("RGB", (W, H))
    px = im.load()
    for y in range(H):
        for x in range(W):
            px[x, y] = ((x * 5 + seed * 29) % 256,
                        (y * 7 + seed * 11) % 256,
                        (x + y + seed * 3) % 256)
    im.save(path)
    return path


def _pin(result):
    if "asset_pin" in result:
        return dict(result["asset_pin"])
    return {"asset_id": result["asset_id"], "revision": result["revision"],
            "content_sha256": result["content_sha256"]}


def _asset(pin, kind, role):
    return {**pin, "kind": kind, "role": role, "layout": None}


def a_plan(pins, **overrides):
    """A pure-A shot plan over [0, 24): rig-free, track-free by contract."""
    plan = {"document_type": "animation_shot_plan", "schema_version": 1,
            "shot_id": "S001", "revision": 1, "motion_intent": "ANIMATED",
            "story_role": "synthetic fixture", "emotion_role": "synthetic",
            "canvas": {"width": W, "height": H}, "background": [10, 20, 30],
            "rig": None,
            "assets": [_asset(pins["master"], "CONTROL_IMAGE", "master"),
                       _asset(pins["layout"], "CONTROL_IMAGE", "layout"),
                       _asset(pins["guide"], "CONTROL_IMAGE", "control"),
                       _asset(pins["mask"], "MASK", "mask"),
                       _asset(pins["replacement"], "REPLACEMENT_DRAWING",
                              "replacement")],
            "events": [],
            "keyposes": [{"kind": "KEYPOSE", "frame": 0, "ref": None},
                         {"kind": "POSE", "frame": 6, "ref": None},
                         {"kind": "BREAKDOWN", "frame": 12, "ref": None},
                         {"kind": "KEYPOSE", "frame": 18, "ref": None}],
            "segments": [{"start": 0, "end": FRAMES, "path": "A",
                          "capabilities": ["REFERENCE_IMAGES",
                                           "LAYOUT_GUIDE", "POSE_GUIDE",
                                           "MASK_REGION",
                                           "PREVIOUS_FRAME_AUX"]}],
            "tracks": {"camera": {"pivot": [W // 2, H // 2],
                                  "transform": None},
                       "layers": {}},
            "preparation": {"work": [], "creator": "synthetic fixture",
                            "reviewer": None, "revision_scope": "fixture"}}
    plan.update(overrides)
    return plan


FULL_TARGET = {"id": "fake-image-app",
               "accepts": ["prompt_text", "reference_image", "layout_image",
                           "pose_image", "mask_region"],
               "max_images": None}


def a_scene(tmp_path, target=FULL_TARGET, *, with_packet=True):
    """Project + A conditioning assets + saved plan (+ written packet)."""
    p = c_project(tmp_path)
    master = import_draft_image(p, "S001",
                                _frame_png(tmp_path / "master.png", 1),
                                role="layout", frame=0)
    layout = import_draft_image(p, "S001",
                                _frame_png(tmp_path / "layout.png", 2),
                                role="layout", frame=0)
    guide = import_draft_image(p, "S001",
                               _frame_png(tmp_path / "guide.png", 3),
                               role="layout", frame=0)
    layer = import_layer_rgba(p, _sprite(tmp_path / "layer.png"),
                              pivot=[8, 8], z_order=1)
    mask = import_mask(p, _mask_half(tmp_path / "mask.png"),
                       target=_pin(layer))
    repl = import_replacement_drawing(
        p, _sprite(tmp_path / "alt.png", color=(30, 30, 200)),
        replaces=_pin(layer), frame=4, pivot=[8, 8])
    pins = {"master": _pin(master), "layout": _pin(layout),
            "guide": _pin(guide), "layer": _pin(layer), "mask": _pin(mask),
            "replacement": _pin(repl)}
    save_shot_plan(p, a_plan(pins))
    packet = (export_work_packets(p, target=target) if with_packet else None)
    return p, pins, packet


def _role_for(frame):
    return {0: "keypose", 6: "pose", 12: "breakdown",
            18: "keypose"}.get(frame, "inbetween")


# --- CONTROL_IMAGE registration ---------------------------------------------

def test_control_image_registers_revision_canvas_and_pins(tmp_path):
    p = c_project(tmp_path)
    master_png = _frame_png(tmp_path / "master.png", 5)
    result = import_draft_image(p, "S001", master_png, role="layout",
                                frame=0)
    assert result["state"] == "DRAFT" and result["new_revision"] is True
    assert result["qualification_state"] == "UNQUALIFIED"
    pin = result["asset_pin"]
    record = load_registry(p)["assets"][pin["asset_id"]]["revisions"]["1"]
    assert record["kind"] == "CONTROL_IMAGE"
    assert record["control_role"] == "LAYOUT" and record["frame"] == 0
    assert record["shot_id"] == "S001" and record["references"] == []
    assert record["canvas"] == {"width": W, "height": H}
    assert record["content_sha256"] == pin["content_sha256"] \
        == content_digest(record)
    member = p / record["files"][0]["relative_name"]
    assert member.read_bytes() == master_png.read_bytes()
    # The stored copy survives deletion of the external source.
    master_png.unlink()
    assert digest(member) == record["files"][0]["sha256"]
    # Identical bytes reuse the revision; different art opens a new one.
    again = import_draft_image(p, "S001", member, role="layout", frame=0,
                               asset_id=pin["asset_id"])
    assert again["asset_pin"]["revision"] == 1
    assert again["new_revision"] is False
    newer = import_draft_image(p, "S001",
                               _frame_png(tmp_path / "master2.png", 6),
                               role="layout", frame=0,
                               asset_id=pin["asset_id"])
    assert newer["asset_pin"]["revision"] == 2
    assert newer["new_revision"] is True
    revisions = load_registry(p)["assets"][pin["asset_id"]]["revisions"]
    assert set(revisions) == {"1", "2"}  # pinned r1 bytes untouched


def test_control_image_reference_pins_must_resolve(tmp_path):
    p, pins, _packet = a_scene(tmp_path, with_packet=False)
    result = import_draft_image(
        p, "S001", _frame_png(tmp_path / "pose_guide.png", 9),
        role="layout", frame=0, references=[pins["master"]])
    record = load_registry(p)["assets"][result["asset_pin"]["asset_id"]][
        "revisions"]["1"]
    assert record["references"] == [pins["master"]]
    stale = dict(pins["master"])
    stale["content_sha256"] = "0" * 64
    with pytest.raises(FilmError, match="REFERENCE_UNKNOWN"):
        import_draft_image(p, "S001", _frame_png(tmp_path / "x.png", 10),
                           role="layout", frame=0, references=[stale])
    with pytest.raises(FilmError, match="REFERENCE_UNKNOWN"):
        import_draft_image(p, "S001", _frame_png(tmp_path / "y.png", 11),
                           role="layout", frame=0,
                           references=[{"asset_id": "A9999", "revision": 1,
                                        "content_sha256": "f" * 64}])


def test_control_image_rejects_bad_role_frame_and_canvas(tmp_path):
    p = c_project(tmp_path)
    png = _frame_png(tmp_path / "ok.png", 1)
    with pytest.raises(FilmError, match="role must be one of"):
        import_draft_image(p, "S001", png, role="storyboard", frame=0)
    with pytest.raises(FilmError, match="FRAME_OUT_OF_RANGE"):
        import_draft_image(p, "S001", png, role="layout", frame=FRAMES)
    with pytest.raises(FilmError, match="CANVAS_MISMATCH"):
        Image.new("RGB", (16, 16), (1, 2, 3)).save(tmp_path / "small.png")
        import_draft_image(p, "S001", tmp_path / "small.png", role="layout",
                           frame=0)
    # Only PNG is accepted.
    bad = tmp_path / "not.png"
    bad.write_bytes(b"this is not png data")
    with pytest.raises(FilmError, match="PNG"):
        import_draft_image(p, "S001", bad, role="layout", frame=0)


# --- path A / B motion plan boundaries --------------------------------------

def test_path_a_accepted_path_b_still_unimplemented(tmp_path):
    p, pins, _ = a_scene(tmp_path, with_packet=False)
    plan = a_plan(pins)
    assert validate_shot_plan(copy.deepcopy(plan), length=FRAMES)
    assert "A" not in UNIMPLEMENTED_PATHS and "B" in UNIMPLEMENTED_PATHS
    bad = copy.deepcopy(plan)
    bad["segments"][0]["path"] = "B"
    with pytest.raises(FilmError, match="Path B"):
        validate_shot_plan(bad, length=FRAMES)


def test_path_a_plan_contract_gates(tmp_path):
    p, pins, _ = a_scene(tmp_path, with_packet=False)
    plan = a_plan(pins)
    bad = copy.deepcopy(plan)
    bad["segments"][0]["capabilities"] = ["RGBA_LAYER"]
    with pytest.raises(FilmError, match="Unknown path-A"):
        validate_shot_plan(bad, length=FRAMES)
    bad = copy.deepcopy(plan)
    bad["rig"] = pins["layer"]
    with pytest.raises(FilmError, match="rig must be null"):
        validate_shot_plan(bad, length=FRAMES)
    bad = copy.deepcopy(plan)
    bad["tracks"]["layers"] = {
        "subject": {"drawings": [pins["layer"]],
                    "exposure": _exposure("subject", [(0, FRAMES, 0)]),
                    "transform": None, "mask": None}}
    with pytest.raises(FilmError, match="tracks.layers"):
        validate_shot_plan(bad, length=FRAMES)
    bad = copy.deepcopy(plan)
    bad["keyposes"] = [{"kind": "INBETWEEN", "frame": 3, "ref": None}]
    with pytest.raises(FilmError, match="keyposes"):
        validate_shot_plan(bad, length=FRAMES)
    bad = copy.deepcopy(plan)
    bad["keyposes"] = [{"kind": "KEYPOSE", "frame": FRAMES, "ref": None}]
    with pytest.raises(FilmError, match="outside"):
        validate_shot_plan(bad, length=FRAMES)


def test_role_gating_between_paths(tmp_path):
    p, pins, _ = a_scene(tmp_path, with_packet=False)
    # master/layout/control roles need an A segment on the plan.
    plan = a_plan(pins)
    bad = copy.deepcopy(plan)
    bad["segments"][0]["path"] = "C"
    bad["segments"][0]["capabilities"] = ["RGBA_LAYER"]
    bad["rig"] = pins["layer"]  # pin shape only; validation reaches roles
    with pytest.raises(FilmError, match="needs a path-A"):
        validate_shot_plan(bad, length=FRAMES)
    # On a pure-A plan a layer role is rejected the same way.
    bad = a_plan(pins)
    bad["assets"].append(_asset(pins["layer"], "LAYER_RGBA", "layer"))
    with pytest.raises(FilmError, match="needs a path-C"):
        validate_shot_plan(bad, length=FRAMES)


def test_compositor_refuses_non_c_plans(tmp_path):
    p, pins, _ = a_scene(tmp_path, with_packet=False)
    with pytest.raises(FilmError, match="path-C"):
        composite_shot(p, "S001")


# --- work packet ------------------------------------------------------------

def test_work_packet_document_contract(tmp_path):
    p, pins, packet = a_scene(tmp_path)
    path = safe_path(p, "animation/packets/S001.json")
    assert path.is_file()
    document = read_canon(path)
    assert validate_work_packet(document) is document
    assert document["shot_id"] == "S001"
    assert document["instance_id"] == "I001"
    assert document["plan_sha256"] == digest(safe_path(p, plan_path("S001")))
    assert document["plan_revision"] == 1
    assert document["canvas"] == {"width": W, "height": H}
    assert document["fps"] == 24
    assert document["used_source_range"] == [0, FRAMES]
    assert document["unused_handles"] == {"before": 0, "after": 0}
    assert document["segments"] == [
        {"start": 0, "end": FRAMES, "path": "A",
         "capabilities": ["REFERENCE_IMAGES", "LAYOUT_GUIDE", "POSE_GUIDE",
                          "MASK_REGION", "PREVIOUS_FRAME_AUX"]}]
    # Control slots ride the plan's keypose times, still awaiting production.
    assert document["controls"] == [
        {"role": "KEYPOSE", "frame": 0, "pin": None},
        {"role": "POSE", "frame": 6, "pin": None},
        {"role": "BREAKDOWN", "frame": 12, "pin": None},
        {"role": "KEYPOSE", "frame": 18, "pin": None}]
    inputs = {i["id"]: i for i in document["inputs"]}
    for input_id in ("prompt", "previous_frame", "master", "layout",
                     "guide", "mask", "replacement",
                     "control:KEYPOSE:0", "control:POSE:6",
                     "control:BREAKDOWN:12", "control:KEYPOSE:18"):
        assert input_id in inputs, input_id
    assert inputs["master"]["pin"] == pins["master"]
    assert inputs["layout"]["pin"] == pins["layout"]
    assert inputs["mask"]["pin"] == pins["mask"]
    assert inputs["control:KEYPOSE:0"]["pin"] is None
    assert inputs["control:KEYPOSE:0"]["disposition"] == \
        "PENDING_PRODUCTION"


def test_work_packet_requests_have_distinct_kinds_and_previous_frame_aux(
        tmp_path):
    p, pins, _ = a_scene(tmp_path)
    document = load_work_packet(p, "S001")
    kinds = {r["kind"] for r in document["requests"]}
    assert kinds == {"keypose", "pose", "breakdown", "inbetween"}
    assert len(document["requests"]) == FRAMES
    for request in document["requests"]:
        assert "previous_frame" not in request["inputs"]
        assert "previous_frame" not in request["carries"]
        assert set(request["carries"]) <= set(request["inputs"])
        if request["aux"]:
            assert request["aux"] == ["previous_frame"]
    at = {r["frame"]: r for r in document["requests"]}
    assert at[0]["kind"] == "keypose" and at[6]["kind"] == "pose"
    assert at[12]["kind"] == "breakdown" and at[18]["kind"] == "keypose"
    assert at[5]["kind"] == "inbetween"
    # Frame 1 is sequenced behind the declared produced controls.
    assert set(at[1]["depends_on"]) == {"S001:keypose:0", "S001:pose:6",
                                       "S001:breakdown:12",
                                       "S001:keypose:18"}
    assert at[5]["depends_on"] == at[1]["depends_on"]
    # Only controls before the request matter to a control request.
    assert set(at[18]["depends_on"]) == {"S001:keypose:0", "S001:pose:6",
                                         "S001:breakdown:12"}


def test_work_packet_records_only_what_the_target_accepts(tmp_path):
    p, pins, _ = a_scene(tmp_path)
    document = load_work_packet(p, "S001")
    inputs = {i["id"]: i for i in document["inputs"]}
    # FULL_TARGET accepts every token, so only pending controls stay out.
    assert inputs["master"]["disposition"] == "INCLUDE"
    assert inputs["mask"]["disposition"] == "INCLUDE"
    at5 = next(r for r in document["requests"] if r["frame"] == 5)
    assert set(at5["carries"]) == {"prompt", "master", "layout", "guide",
                                   "mask", "replacement"}

    # A target that cannot take layout/pose/mask inputs marks them NOT_SENT.
    limited = {"id": "prompt-and-refs",
               "accepts": ["prompt_text", "reference_image"],
               "max_images": 1}
    export_work_packets(p, target=limited)
    doc2 = load_work_packet(p, "S001")
    inputs2 = {i["id"]: i for i in doc2["inputs"]}
    assert inputs2["layout"]["disposition"] == "NOT_SENT"
    assert inputs2["mask"]["disposition"] == "NOT_SENT"
    # An unproduced control the target cannot take is NOT_SENT like any
    # other unaccepted input, never left silently pending.
    assert inputs2["control:KEYPOSE:0"]["disposition"] == "NOT_SENT"
    assert {i["input"] for i in doc2["transport"]["not_sent"]} >= \
        {"layout", "guide", "mask"}
    at5b = next(r for r in doc2["requests"] if r["frame"] == 5)
    # max_images 1: only the master reference image is carried.
    assert set(at5b["carries"]) == {"prompt", "master"}
    # layout is required but cannot be sent: the request is honestly blocked.
    assert any("layout" in b for b in at5b["blocked"])
    assert doc2["transport"]["mode"] == "MANUAL_PACKET"
    assert doc2["transport"]["target"]["id"] == "prompt-and-refs"

    # A target that accepts layout but caps images still cannot take it:
    # required references are never trimmed silently to fit the limit.
    capped = {"id": "one-image-app",
              "accepts": ["prompt_text", "reference_image", "layout_image",
                          "pose_image", "mask_region"],
              "max_images": 1}
    export_work_packets(p, target=capped)
    doc3 = load_work_packet(p, "S001")
    inputs3 = {i["id"]: i for i in doc3["inputs"]}
    assert inputs3["layout"]["disposition"] == "INCLUDE"
    at5c = next(r for r in doc3["requests"] if r["frame"] == 5)
    assert set(at5c["carries"]) == {"prompt", "master"}
    assert any("layout" in b and "OVER_MAX_IMAGES" in b
               for b in at5c["blocked"])
    dropped = {i["input"]: i for i in doc3["transport"]["not_sent"]
               if "OVER_MAX_IMAGES" in i["reason"]}
    assert "layout" in dropped
    assert "S001:inbetween:5" in dropped["layout"]["requests"]


def test_work_packet_reference_request_when_no_master(tmp_path):
    p, pins, _ = a_scene(tmp_path, with_packet=False)
    plan = a_plan(pins)
    plan["assets"] = [a for a in plan["assets"] if a["role"] != "master"]
    save_shot_plan(p, plan)
    export_work_packets(p, target=FULL_TARGET)
    document = load_work_packet(p, "S001")
    references = [r for r in document["requests"] if r["kind"] == "reference"]
    assert len(references) == 1
    assert references[0]["request_id"] == "S001:reference:master"
    assert references[0]["produces"]["role"] == "master"
    assert references[0]["produces"]["fills_input"] == "master"
    assert references[0]["produces"]["fills_member"] is False
    assert references[0]["carries"] == ["prompt"]
    assert any("master" in w for w in document["warnings"])
    # Frame requests wait on the reference request and stay blocked until
    # the master is pinned in the plan and the packet regenerated.
    frames = [r for r in document["requests"] if r["kind"] != "reference"]
    assert frames
    for request in frames:
        assert "S001:reference:master" in request["depends_on"]
        assert any("master" in b for b in request["blocked"])


def test_missing_layout_blocks_frames_and_refuses_import_and_commit(
        tmp_path):
    p, pins, _ = a_scene(tmp_path, with_packet=False)
    plan = a_plan(pins)
    plan["assets"] = [a for a in plan["assets"] if a["role"] != "layout"]
    save_shot_plan(p, plan)
    export_work_packets(p, target=FULL_TARGET)
    document = load_work_packet(p, "S001")
    ref = next(r for r in document["requests"] if r["kind"] == "reference")
    assert ref["request_id"] == "S001:reference:layout"
    assert ref["produces"]["role"] == "layout"
    request = next(r for r in document["requests"] if r["frame"] == 5)
    assert "S001:reference:layout" in request["depends_on"]
    assert request["blocked"]
    # A member frame cannot import while its request is blocked.
    refs = [pins[k] for k in ("master", "guide", "mask", "replacement")]
    with pytest.raises(FilmError, match="REQUEST_BLOCKED"):
        import_draft_image(p, "S001", _frame_png(tmp_path / "f5.png", 5),
                           role="inbetween", frame=5, references=refs)
    # Even a hand-filled complete ledger cannot commit: the plan pins no
    # layout reference (schema 6 path-A rejection).
    packet_sha = digest(safe_path(p, "animation/packets/S001.json"))
    entries = []
    for frame in range(FRAMES):
        png = _frame_png(tmp_path / f"f{frame}.png", 100 + frame)
        member = f"animation/shots/S001/drafts/m{frame:06d}.png"
        stored = safe_path(p, member)
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(png.read_bytes())
        entries.append({"frame": frame, "role": _role_for(frame).upper(),
                        "member": member, "sha256": digest(png),
                        "byte_length": png.stat().st_size,
                        "source_name": png.name, "imported_at": "fixture",
                        "packet_sha256": packet_sha, "references": refs,
                        "asset_pin": None, "state": "DRAFT"})
    write_canon(safe_path(p, "animation/shots/S001/draft_frames.json"),
                {"document_type": "animation_draft_frames",
                 "schema_version": 1, "shot_id": "S001",
                 "entries": entries})
    with pytest.raises(FilmError, match="MISSING_REFERENCE"):
        commit_draft_frames(p, "S001")


def test_over_cap_required_input_blocks_request_and_import(tmp_path):
    p, pins, _ = a_scene(tmp_path, with_packet=False)
    capped = {"id": "one-image-app",
              "accepts": ["prompt_text", "reference_image", "layout_image",
                          "pose_image", "mask_region"],
              "max_images": 1}
    export_work_packets(p, target=capped)
    document = load_work_packet(p, "S001")
    request = next(r for r in document["requests"] if r["frame"] == 5)
    # The required layout pin never left the project: absent from
    # carries, named in blocked and recorded in transport.not_sent.
    assert "layout" not in request["carries"]
    assert any("layout" in b and "OVER_MAX_IMAGES" in b
               for b in request["blocked"])
    sent = [i for i in document["transport"]["not_sent"]
            if i["input"] == "layout"]
    assert sent and "OVER_MAX_IMAGES" in sent[0]["reason"]
    # Carrying only what fit the cap is not enough to import the frame.
    with pytest.raises(FilmError, match="REQUEST_BLOCKED"):
        import_draft_image(p, "S001", _frame_png(tmp_path / "f5.png", 5),
                           role="inbetween", frame=5,
                           references=[pins["master"]])


def test_work_packet_is_deterministic_and_manual_only(tmp_path):
    p, pins, _ = a_scene(tmp_path)
    path = safe_path(p, "animation/packets/S001.json")
    first = path.read_bytes()
    export_work_packets(p, target=FULL_TARGET)
    assert path.read_bytes() == first  # canonical, pinned, repeatable
    page = (p / "animation/packets/index.html").read_text(encoding="utf-8")
    assert "S001" in page and "가져오기" in page
    # Manual hand-off: a copy-paste aid, never a driver of any site.
    for token in ("<form", "fetch(", "XMLHttpRequest", "https://",
                  "http://"):
        assert token not in page


def test_work_packet_skips_and_wave_selection(tmp_path):
    p, pins, _ = a_scene(tmp_path, with_packet=False)
    result = export_work_packets(p, target=FULL_TARGET)
    # Plan saved inside a_scene; S001 has an A segment — nothing skipped.
    assert result["skipped"] == [] and len(result["packets"]) == 1
    from engine.animation_locks import declare_waves
    declare_waves(p, [{"wave": "W00", "shots": ["S001"],
                       "difficulty": [{"type": "A_PATH",
                                       "reason": "synthetic fixture"}]}])
    by_wave = export_work_packets(p, wave="W00", target=FULL_TARGET)
    assert [x["shot_id"] for x in by_wave["packets"]] == ["S001"]
    with pytest.raises(FilmError, match="no timeline shots"):
        export_work_packets(p, wave="W99", target=FULL_TARGET)
    # A shot with no plan and a shot without A segments are reported.
    (tmp_path / "other").mkdir()
    p2 = c_project(tmp_path / "other")
    result2 = export_work_packets(p2, target=FULL_TARGET)
    assert result2["skipped"] == [{"shot_id": "S001", "reason": "NO_PLAN"}]


# --- draft import verification ----------------------------------------------

def test_draft_import_requires_current_packet(tmp_path):
    p, pins, _ = a_scene(tmp_path, with_packet=False)
    png = _frame_png(tmp_path / "f5.png", 5)
    with pytest.raises(FilmError, match="PACKET_MISSING"):
        import_draft_image(p, "S001", png, role="inbetween", frame=5,
                           references=[])
    export_work_packets(p, target=FULL_TARGET)
    plan = read_canon(safe_path(p, plan_path("S001")))
    plan["revision"] = 2
    plan["events"] = [{"type": "NOTE", "frame": 1, "note": "changed"}]
    save_shot_plan(p, plan)  # plan changed; the stored packet is stale
    with pytest.raises(FilmError, match="PACKET_STALE"):
        import_draft_image(p, "S001", png, role="inbetween", frame=5,
                           references=[])


def test_draft_import_role_frame_and_reference_checks(tmp_path):
    p, pins, _ = a_scene(tmp_path)
    refs = [pins[k] for k in ("master", "layout", "guide", "mask",
                            "replacement")]
    with pytest.raises(FilmError, match="FRAME_OUT_OF_RANGE"):
        import_draft_image(p, "S001", _frame_png(tmp_path / "a.png", 1),
                           role="inbetween", frame=FRAMES,
                           references=refs)
    # Frame 6 is requested as a pose, not an inbetween.
    with pytest.raises(FilmError, match="ROLE_MISMATCH"):
        import_draft_image(p, "S001", _frame_png(tmp_path / "b.png", 2),
                           role="inbetween", frame=6, references=refs)
    with pytest.raises(FilmError, match="REFERENCE_MISSING"):
        import_draft_image(p, "S001", _frame_png(tmp_path / "c.png", 3),
                           role="inbetween", frame=5,
                           references=refs[:-1])
    with pytest.raises(FilmError, match="REFERENCE_UNEXPECTED"):
        import_draft_image(p, "S001", _frame_png(tmp_path / "d.png", 4),
                           role="inbetween", frame=5,
                           references=refs + [pins["layer"]])
    bogus = {"asset_id": "A9999", "revision": 1,
             "content_sha256": "e" * 64}
    with pytest.raises(FilmError, match="REFERENCE_UNKNOWN"):
        import_draft_image(p, "S001", _frame_png(tmp_path / "e.png", 5),
                           role="inbetween", frame=5,
                           references=refs + [bogus])


def test_member_imports_record_draft_ledger_and_control_assets(tmp_path):
    p, pins, _ = a_scene(tmp_path)
    refs = [pins[k] for k in ("master", "layout", "guide", "mask",
                            "replacement")]
    keypose_png = _frame_png(tmp_path / "keypose0.png", 40)
    result = import_draft_image(p, "S001", keypose_png, role="keypose",
                                frame=0, references=refs)
    assert result["state"] == "DRAFT"
    assert {c["code"] for c in result["checks"]} == {"REQUEST_MATCHED",
                                                   "LEDGER_RECORDED"}
    record = load_registry(p)["assets"][result["asset_pin"]["asset_id"]][
        "revisions"]["1"]
    assert record["kind"] == "CONTROL_IMAGE"
    assert record["control_role"] == "KEYPOSE" and record["frame"] == 0
    assert record["references"] == refs
    ledger = load_draft_frames(p, "S001")
    assert ledger["document_type"] == "animation_draft_frames"
    entry = ledger["entries"][0]
    assert entry["frame"] == 0 and entry["role"] == "KEYPOSE"
    assert entry["sha256"] == digest(keypose_png)
    assert entry["byte_length"] == len(keypose_png.read_bytes())
    assert entry["source_name"] == "keypose0.png"
    assert entry["packet_sha256"] == digest(
        safe_path(p, "animation/packets/S001.json"))
    assert entry["references"] == refs
    assert entry["asset_pin"] == result["asset_pin"]
    assert entry["state"] == "DRAFT"
    member = safe_path(p, entry["member"])
    assert member.is_file() and p.resolve() in member.resolve().parents
    # A second produced image for the same frame is refused outright.
    with pytest.raises(FilmError, match="DUPLICATE_FRAME"):
        import_draft_image(p, "S001", _frame_png(tmp_path / "dup.png", 41),
                           role="keypose", frame=0, references=refs)


def test_filename_order_never_assigns_frames(tmp_path):
    p, pins, _ = a_scene(tmp_path)
    refs = [pins[k] for k in ("master", "layout", "guide", "mask",
                            "replacement")]
    # Names and order are deliberately misleading: z_first imports frame 7.
    order = [("z_first.png", 7), ("a_later.png", 3), ("m_mid.png", 19)]
    stored = {}
    for name, frame in order:
        path = _frame_png(tmp_path / name, 60 + frame)
        stored[frame] = digest(path)
        import_draft_image(p, "S001", path, role="inbetween", frame=frame,
                           references=refs)
    ledger = {e["frame"]: e for e in load_draft_frames(p, "S001")["entries"]}
    for frame, _name in ((f, n) for n, f in order):
        assert ledger[frame]["sha256"] == stored[frame]


def test_symlinked_and_missing_sources_are_refused(tmp_path):
    p, pins, _ = a_scene(tmp_path)
    refs = [pins[k] for k in ("master", "layout", "guide", "mask",
                            "replacement")]
    real = _frame_png(tmp_path / "real.png", 70)
    link = tmp_path / "link.png"
    os.symlink(real, link)
    with pytest.raises(FilmError, match="regular file"):
        import_draft_image(p, "S001", link, role="inbetween", frame=1,
                           references=refs)
    with pytest.raises(FilmError, match="regular file"):
        import_draft_image(p, "S001", tmp_path / "gone.png",
                           role="inbetween", frame=1, references=refs)
    # Controls are refused symlinks the same way.
    with pytest.raises(FilmError, match="regular file"):
        import_draft_image(p, "S001", link, role="layout", frame=0)


# --- commit: draft sequence assembly -----------------------------------------

def _import_all(tmp_path, p, pins, frames=None):
    refs = [pins[k] for k in ("master", "layout", "guide", "mask",
                            "replacement")]
    for frame in (frames if frames is not None else range(FRAMES)):
        import_draft_image(p, "S001",
                           _frame_png(tmp_path / f"f{frame}.png",
                                      100 + frame),
                           role=_role_for(frame), frame=frame,
                           references=refs)


def test_commit_requires_every_produced_frame(tmp_path):
    p, pins, _ = a_scene(tmp_path)
    _import_all(tmp_path, p, pins, frames=range(20))
    with pytest.raises(FilmError, match="MISSING_FRAMES"):
        commit_draft_frames(p, "S001")
    _import_all(tmp_path, p, pins, frames=range(20, FRAMES))
    result = commit_draft_frames(p, "S001")
    assert result["frames"] == FRAMES and result["state"] == "DRAFT"


def test_commit_detects_tampered_members(tmp_path):
    p, pins, _ = a_scene(tmp_path)
    _import_all(tmp_path, p, pins)
    ledger = load_draft_frames(p, "S001")
    member = safe_path(p, ledger["entries"][3]["member"])
    member.write_bytes(member.read_bytes() + b"corrupt")
    with pytest.raises(FilmError, match="DRAFT_MEMBER_CHANGED"):
        commit_draft_frames(p, "S001")


def test_commit_refuses_symlinked_member(tmp_path):
    p, pins, _ = a_scene(tmp_path)
    _import_all(tmp_path, p, pins)
    ledger = load_draft_frames(p, "S001")
    member = safe_path(p, ledger["entries"][2]["member"])
    payload = member.read_bytes()
    member.unlink()
    outside = tmp_path / "outside.png"
    outside.write_bytes(payload)
    os.symlink(outside, member)
    with pytest.raises(FilmError, match="DRAFT_MEMBER_MISSING"):
        commit_draft_frames(p, "S001")


def test_commit_refuses_ledger_entries_from_another_packet(tmp_path):
    p, pins, _ = a_scene(tmp_path)
    _import_all(tmp_path, p, pins)
    # A regenerated packet binds the same plan but different bytes; the
    # ledger entries were verified against the old one, so commit refuses.
    export_work_packets(p, target={**FULL_TARGET, "id": "other-app"})
    with pytest.raises(FilmError, match="PACKET_STALE"):
        commit_draft_frames(p, "S001")


def test_commit_assembles_sequence_consumed_by_normal_paths(tmp_path):
    p, pins, _ = a_scene(tmp_path)
    _import_all(tmp_path, p, pins)
    result = commit_draft_frames(p, "S001")
    assert result["state"] == "DRAFT"
    assert result["qualification_state"] == "UNQUALIFIED"
    registry = load_registry(p)
    record = registry["assets"][result["asset_id"]]["revisions"]["1"]
    assert record["kind"] == "FRAME_SEQUENCE"
    assert len(record["files"]) == FRAMES
    # The committed sequence pins every conditioning asset it was made with.
    dep_ids = {d["asset_id"] for d in record["dependencies"]}
    assert {pins[k]["asset_id"] for k in
            ("master", "layout", "guide", "mask", "replacement")} <= dep_ids
    # Adopted into the edit and resolvable by the ordinary machinery.
    timeline = read_canon(p / "timeline/edit.json")
    assert timeline["entries"][0]["sequence_revision"] == 1
    resolved = resolve_shot_sequence(p, "S001", [0, FRAMES],
                                     {"before": 0, "after": 0})
    assert len(resolved["members"]) == FRAMES
    normalized = normalize_shot_sequence(p, "S001")
    assert normalized["entries"][0]["frames"] == FRAMES
    assert animation_validate(p)["ok"] is True
    # Frame members stay distinct: every frame's bytes differ by seed.
    member_shas = {f["sha256"] for f in record["files"]}
    assert len(member_shas) == FRAMES
    # The draft preview consumes the assembled sequence like any import.
    build = compile_preview(p)
    folder, rec = newest_build(p)
    assert rec["document_type"] == "animation_draft_preview"
    assert rec["draft"] is True
    assert rec["frames"]["placeholder_frames"] == 0
    rows = [json.loads(line) for line in
            (folder / "draft_frame_map.jsonl").read_text().splitlines()]
    assert len(rows) == FRAMES
    assert all(r["sources"][0]["resolved"] for r in rows)


def test_commit_uses_frame_assignment_not_ledger_order(tmp_path):
    p, pins, _ = a_scene(tmp_path)
    refs = [pins[k] for k in ("master", "layout", "guide", "mask",
                            "replacement")]
    seeds = {}
    # Import in a scrambled order; explicit frame assignment is what counts.
    for frame in [18, 0, 23, 6, 12, *range(1, 6), *range(7, 12),
                  *range(13, 18), *range(19, 23)]:
        seeds[frame] = 200 + frame
        import_draft_image(p, "S001",
                           _frame_png(tmp_path / f"out{frame}.png",
                                      seeds[frame]),
                           role=_role_for(frame), frame=frame,
                           references=refs)
    result = commit_draft_frames(p, "S001")
    record = load_registry(p)["assets"][result["asset_id"]]["revisions"]["1"]
    for f in record["files"]:
        member = safe_path(p, f["relative_name"])
        expected = _frame_png(tmp_path / f"check{f['frame_index']}.png",
                              200 + f["frame_index"])
        assert digest(member) == digest(expected)


# --- boundaries --------------------------------------------------------------

def test_mixed_path_plan_packets_only_a_and_commit_refuses(tmp_path):
    p, pins = scene(tmp_path)  # full C fixture: rig, layers, mask, alt
    master = import_draft_image(p, "S001",
                                _frame_png(tmp_path / "master.png", 1),
                                role="layout", frame=0)
    layout = import_draft_image(p, "S001",
                                _frame_png(tmp_path / "layout.png", 2),
                                role="layout", frame=0)
    plan = scene_plan(pins)
    plan["assets"].append(_asset(_pin(master), "CONTROL_IMAGE", "master"))
    plan["assets"].append(_asset(_pin(layout), "CONTROL_IMAGE", "layout"))
    plan["keyposes"] = [{"kind": "KEYPOSE", "frame": 2, "ref": None}]
    plan["segments"] = [
        {"start": 0, "end": 12, "path": "A",
         "capabilities": ["REFERENCE_IMAGES"]},
        {"start": 12, "end": FRAMES, "path": "C", "capabilities": []}]
    save_shot_plan(p, plan)
    export_work_packets(p, target=FULL_TARGET)
    document = load_work_packet(p, "S001")
    assert document["segments"] == [plan["segments"][0]]
    assert {r["frame"] for r in document["requests"]
            if r["frame"] is not None} == set(range(12))
    assert any("mixed-path" in w for w in document["warnings"])
    # The carried inputs are the master and layout pins.
    refs = [_pin(master), _pin(layout)]
    with pytest.raises(FilmError, match="UNKNOWN_FRAME"):
        import_draft_image(p, "S001", _frame_png(tmp_path / "x.png", 30),
                           role="inbetween", frame=15, references=refs)
    import_draft_image(p, "S001", _frame_png(tmp_path / "y.png", 31),
                       role="inbetween", frame=5, references=refs)
    with pytest.raises(FilmError, match="MIXED_PATH_UNSUPPORTED"):
        commit_draft_frames(p, "S001")


def test_import_and_commit_leave_storyboard_lyrics_locks_untouched(tmp_path):
    p, pins, _ = a_scene(tmp_path)
    storyboard = digest(p / "storyboard/S001.png")
    timing = digest(p / "lyrics/lyrics_timed.json")
    _import_all(tmp_path, p, pins)
    commit_draft_frames(p, "S001")
    assert digest(p / "storyboard/S001.png") == storyboard
    assert digest(p / "lyrics/lyrics_timed.json") == timing
    # No review approvals, no lock records, no production authorization.
    assert not (p / "production/approvals.jsonl").exists()
    assert not (p / "manifest/animation_locks.json").exists()


def test_legacy_projects_are_rejected_and_untouched(tmp_path):
    p = fixture_project(tmp_path, seconds=1, shot_count=1)
    png = _frame_png(tmp_path / "f.png", 1)
    for call in (lambda: import_draft_image(p, "S001", png, role="inbetween",
                                          frame=0),
                 lambda: commit_draft_frames(p, "S001"),
                 lambda: export_work_packets(p)):
        with pytest.raises(FilmError, match="LEGACY_MV"):
            call()
    assert cli.main(["animation-packets", str(p)]) == 1
    assert not (p / "animation").exists()


# --- CLI surface -------------------------------------------------------------

def test_cli_packet_import_and_commit(tmp_path, capsys):
    p, pins, _ = a_scene(tmp_path, with_packet=False)
    target = tmp_path / "target.json"
    target.write_text(json.dumps(FULL_TARGET), encoding="utf-8")
    assert cli.main(["animation-packets", str(p),
                     "--target", str(target)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["qualification_state"] == "UNQUALIFIED"
    refs = ",".join(f"{pins[k]['asset_id']}:{pins[k]['revision']}:"
                    f"{pins[k]['content_sha256']}"
                    for k in ("master", "layout", "guide", "mask",
                              "replacement"))
    png = _frame_png(tmp_path / "f5.png", 5)
    assert cli.main(["import-control", str(p), "S001", "--file", str(png),
                     "--role", "inbetween", "--frame", "5",
                     "--references", refs]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "DRAFT" and out["frame"] == 5
    with pytest.raises(FilmError, match="MISSING_FRAMES"):
        commit_draft_frames(p, "S001")
    _import_all(tmp_path, p, pins,
                frames=[f for f in range(FRAMES) if f != 5])
    assert cli.main(["commit-draft-frames", str(p), "S001"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["frames"] == FRAMES and out["state"] == "DRAFT"
