"""ANIM-008: native C-path compositor — layers, pivot, transforms,
replacement drawings, masks, separate subject/camera tracks.

All fixtures are synthetic Pillow-drawn RGBA layers on a 64x48 canvas and a
generated sine master. Composited sequences are local draft protocol
evidence; nothing here is artwork approval, production qualification or a
release (UNQUALIFIED / PENDING / NOT_AUTHORIZED by construction).
"""
import copy
import json

import numpy as np
import pytest
from PIL import Image

from engine import cli
from engine.animation_assets import (import_layer_rgba, import_mask,
                                     import_replacement_drawing,
                                     import_rig_spec, load_registry,
                                     resolve_asset, resolve_shot_sequence)
from engine.animation_review import record_cut_review
from engine.animation_schema import read_canon, write_canon
from engine.compositor import composite_shot
from engine.compiler import compile_final
from engine.core import FilmError, digest, read, write
from engine.exposure import make_schedule
from engine.frame_sequence import normalize_shot_sequence
from engine.motion_plan import (load_shot_plan, plan_path, save_shot_plan,
                                validate_shot_plan)
from engine.builds import verify_build
from test_anim_003 import animation_project
from test_anim_006 import CUT_METHODS, REVIEWER
from test_compiler_v03 import fixture_project, newest_build

W, H, FRAMES = 64, 48, 24


# --- drawn fixture layers --------------------------------------------------

def _plate(path):
    """Opaque 64x48 background plate with a landmark pixel at (0, 0)."""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    px = im.load()
    for y in range(H):
        for x in range(W):
            px[x, y] = (40 + x % 40, 60 + (x + y) % 60, 80, 255)
    px[0, 0] = (250, 240, 10, 255)
    im.save(path)
    return path


def _sprite(path, color=(200, 30, 30), marker=(255, 0, 255)):
    """16x16 sprite: solid interior, semi-transparent (a=128) border, marker."""
    im = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    px = im.load()
    for y in range(16):
        for x in range(16):
            if 0 < x < 15 and 0 < y < 15:
                px[x, y] = (*color, 255)
    for i in range(16):
        px[i, 0] = px[i, 15] = px[0, i] = px[15, i] = (*color, 128)
    px[3, 3] = (*marker, 255)
    im.save(path)
    return path


def _badge(path):
    """6x6 solid green child drawing."""
    Image.new("RGBA", (6, 6), (40, 200, 60, 255)).save(path)
    return path


def _occluder(path):
    """10x48 opaque grey bar used to cover subject pixels."""
    Image.new("RGBA", (10, H), (150, 150, 160, 255)).save(path)
    return path


def _mask_half(path, left=True):
    """16x16 LUMINANCE matte: white on one half, black on the other."""
    im = Image.new("L", (16, 16), 0)
    px = im.load()
    for y in range(16):
        for x in range(16):
            px[x, y] = 255 if (x < 8) == left else 0
    im.save(path)
    return path


def _mask_region(path):
    """8x16 all-white matte for a [8,0,8,16] region test."""
    Image.new("L", (8, 16), 255).save(path)
    return path


# --- project + asset helpers -------------------------------------------------

def c_project(tmp_path, seconds=1):
    """A converted single-cut project on a small 64x48 canvas.

    Changing the fixture's format re-binds the synthetic lyric review to
    the new canvas — the canvas is part of its approval fingerprint.
    """
    from engine.lyrics import save_timing
    p = animation_project(tmp_path, shot_count=1, seconds=seconds)
    config = read(p / "project.yaml")
    config["format"].update(width=W, height=H)
    write(p / "project.yaml", config)
    timed = read(p / "lyrics/lyrics_timed.json")
    save_timing(p, timed,
                reviewer="Synthetic fixture timing; not inferred from vocals")
    return p


def _pin(result):
    return {"asset_id": result["asset_id"], "revision": result["revision"],
            "content_sha256": result["content_sha256"]}


def _rig_layer(layer_id, parent, pin, z_order,
               transforms=None):
    return {"id": layer_id, "parent": parent, "asset": pin,
            "z_order": z_order,
            "transforms": transforms
            or ["translate", "rotate", "scale", "opacity"]}


def _rig(layers):
    ids = [layer["id"] for layer in layers]
    return {"layers": layers,
            "children": {lid: sorted(l["id"] for l in layers
                                     if l["parent"] == lid)
                         for lid in ids},
            "reference_points": {"subject_root": {"layer": "subject",
                                                  "point": [8, 8]}},
            "occlusion_order": [l["id"] for l in sorted(
                layers, key=lambda l: l["z_order"])]}


ALL_TRANSFORMS = ["translate", "rotate", "scale", "opacity"]


def scene(tmp_path, order=None, *, subject_pivot=(8, 8)):
    """Import the fixture rig: plate < subject < badge < occluder.

    `order` permutes the occlusion order (and matching z_order values).
    Returns (project, pins).
    """
    p = c_project(tmp_path)
    order = order or ["plate", "subject", "badge", "occluder"]
    z = {name: order.index(name) for name in order}
    plate = import_layer_rgba(p, _plate(tmp_path / "plate.png"),
                              pivot=[0, 0], z_order=z["plate"])
    subject = import_layer_rgba(p, _sprite(tmp_path / "subject.png"),
                                pivot=list(subject_pivot),
                                z_order=z["subject"])
    badge = import_layer_rgba(p, _badge(tmp_path / "badge.png"),
                              pivot=[0, 0], z_order=z["badge"])
    occluder = import_layer_rgba(p, _occluder(tmp_path / "occluder.png"),
                                 pivot=[0, 0], z_order=z["occluder"])
    alt = import_replacement_drawing(
        p, _sprite(tmp_path / "alt.png", color=(30, 30, 200),
                   marker=(0, 255, 255)),
        replaces=_pin(subject), frame=4, pivot=list(subject_pivot))
    mask = import_mask(p, _mask_half(tmp_path / "mask.png"),
                       target=_pin(subject), channel="LUMINANCE")
    rig = import_rig_spec(p, _rig([
        _rig_layer("plate", None, _pin(plate), z["plate"]),
        _rig_layer("subject", None, _pin(subject), z["subject"]),
        _rig_layer("badge", "subject", _pin(badge), z["badge"]),
        _rig_layer("occluder", None, _pin(occluder), z["occluder"])]))
    return p, {"plate": _pin(plate), "subject": _pin(subject),
               "badge": _pin(badge), "occluder": _pin(occluder),
               "alt": _pin(alt), "mask": _pin(mask), "rig": _pin(rig)}


def _exposure(track, parts):
    """One ones-stride schedule covering [(s0, L) with per-slot drawings."""
    slots = [{"start": s, "end": e, "drawing": d,
              **({"hold": True} if e - s > 1 else {})}
             for s, e, d in parts]
    return [make_schedule(track, parts[0][0], parts[-1][1], 1, slots)]


def _track(**channels):
    """translate=("LINEAR", [(frame, [x, y]), ...]), rotate=(curve, [(f, v)])"""
    body = {}
    for name, (curve, keys) in channels.items():
        body[name] = {"curve": curve,
                      "keys": [{"frame": f, "value": v} for f, v in keys]}
    return {"channels": body}


def _asset(pin, kind, role, layout=None):
    return {**pin, "kind": kind, "role": role, "layout": layout}


_LAYOUTS = {"plate": [0, 0], "subject": [24, 16], "badge": [2, 2],
            "occluder": [30, 0]}


def scene_plan(pins, *, intent="STATIC", camera=None, subject=None,
               badge=None, occluder=None, plate=None, frames=FRAMES,
               swap=None, mask=None, background=(10, 20, 30)):
    """Plan covering every layer the pins carry; `swap` is the subject
    exposure [(s,e,d), ...] with drawings[1] = the replacement."""
    subject_parts = swap or [(0, frames, 0)]
    use_alt = any(d == 1 for _s, _e, d in subject_parts)
    drawings = [pins["subject"]] + ([pins["alt"]] if use_alt else [])
    transforms = {"plate": plate, "subject": subject, "badge": badge,
                  "occluder": occluder}
    layers = {}
    for name in ("plate", "subject", "badge", "occluder"):
        if name not in pins:
            continue
        parts = subject_parts if name == "subject" else [(0, frames, 0)]
        layers[name] = {
            "drawings": drawings if name == "subject" else [pins[name]],
            "exposure": _exposure(name, parts),
            "transform": transforms[name],
            "mask": mask if name == "subject" else None}
    assets = [_asset(pins[name], "LAYER_RGBA", "layer", _LAYOUTS[name])
              for name in layers]
    if use_alt:
        assets.append(_asset(pins["alt"], "REPLACEMENT_DRAWING",
                             "replacement"))
    if mask is not None:
        assets.append(_asset(mask, "MASK", "mask"))
    camera_block = {"pivot": [W // 2, H // 2], "transform": camera}
    return {"document_type": "animation_shot_plan", "schema_version": 1,
            "shot_id": "S001", "revision": 1, "motion_intent": intent,
            "story_role": "synthetic fixture", "emotion_role": "synthetic",
            "canvas": {"width": W, "height": H}, "background": list(background),
            "rig": pins["rig"], "assets": assets, "events": [], "keyposes": [],
            "segments": [{"start": 0, "end": frames, "path": "C",
                          "capabilities": []}],
            "tracks": {"camera": camera_block, "layers": layers},
            "preparation": {"work": [], "creator": "synthetic fixture",
                            "reviewer": None, "revision_scope": "fixture"}}


def _composite(p, plan):
    save_shot_plan(p, plan)
    return composite_shot(p, "S001")


def _member(p, result, index):
    entry = load_registry(p)["assets"][result["asset_id"]]
    record = entry["revisions"][str(result["revision"])]
    path = p / record["files"][index]["relative_name"]
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.int32)


def _pts(image, pred):
    return np.argwhere(pred(image))


def _centroid(image, pred):
    pts = _pts(image, pred)
    return None if len(pts) == 0 else pts[:, ::-1].mean(axis=0)


def _red(img):
    return (img[..., 0] > 150) & (img[..., 1] < 80) & (img[..., 2] < 80)


def _blue(img):
    return (img[..., 2] > 150) & (img[..., 0] < 80) & (img[..., 1] < 80)


def _green(img):
    return (img[..., 1] > 150) & (img[..., 0] < 100) & (img[..., 2] < 100)


def _bar(img):
    return (np.abs(img[..., 0] - 150) < 25) & (np.abs(img[..., 1] - 150) < 25) \
        & (np.abs(img[..., 2] - 160) < 25)


def _landmark(img):
    return (img[..., 0] > 200) & (img[..., 1] > 200) & (img[..., 2] < 60)


# --- registry: new kinds, validation, revisions ------------------------------

def test_mask_replacement_and_rig_register_with_validation(tmp_path):
    p, pins = scene(tmp_path)
    registry = load_registry(p)
    kinds = {aid: entry["revisions"][str(entry["current_revision"])]["kind"]
             for aid, entry in registry["assets"].items()}
    assert set(kinds.values()) == {"LAYER_RGBA", "MASK",
                                   "REPLACEMENT_DRAWING", "RIG_SPEC"}
    mask = next(r for e in registry["assets"].values()
                for r in e["revisions"].values() if r["kind"] == "MASK")
    assert mask["target"] == pins["subject"]
    assert mask["channel"] == "LUMINANCE" and mask["region"] is None
    assert mask["canvas"] == {"width": 16, "height": 16}
    alt = next(r for e in registry["assets"].values()
               for r in e["revisions"].values()
               if r["kind"] == "REPLACEMENT_DRAWING")
    assert alt["replaces"] == pins["subject"] and alt["frame"] == 4
    assert alt["rig"]["pivot"] == [8, 8]
    assert alt["alpha"]["present"] is True
    rig = next(r for e in registry["assets"].values()
               for r in e["revisions"].values()
               if r["kind"] == "RIG_SPEC")
    assert rig["spec"]["occlusion_order"] == \
        ["plate", "subject", "badge", "occluder"]
    # The spec is stored canonically as a member.
    member = p / rig["files"][0]["relative_name"]
    assert read_canon(member) == rig["spec"]


def test_cross_reference_gates_reject_bad_pins(tmp_path):
    p, pins = scene(tmp_path)
    mask_png = _mask_half(tmp_path / "mask2.png")
    alt_png = _sprite(tmp_path / "alt2.png", color=(20, 20, 220))
    # Unknown target asset / stale pin digest.
    for bad in ({"asset_id": "A9999", "revision": 1,
                 "content_sha256": "0" * 64},
                {**pins["subject"], "content_sha256": "0" * 64},
                {**pins["subject"], "revision": 99}):
        with pytest.raises(FilmError):
            import_mask(p, mask_png, target=bad)
        with pytest.raises(FilmError):
            import_replacement_drawing(p, alt_png, replaces=bad, frame=4,
                                       pivot=[8, 8])
    # Region outside the target canvas / mask canvas != region.
    with pytest.raises(FilmError, match="inside"):
        import_mask(p, mask_png, target=pins["subject"],
                    region=[4, 0, 20, 16])
    narrow = Image.new("L", (4, 16), 255)
    narrow.save(tmp_path / "narrow.png")
    with pytest.raises(FilmError, match="region extent"):
        import_mask(p, tmp_path / "narrow.png",
                    target=pins["subject"], region=[8, 0, 8, 16])
    # ALPHA channel requires a real alpha channel in the mask bytes.
    with pytest.raises(FilmError, match="alpha"):
        import_mask(p, mask_png, target=pins["subject"], channel="ALPHA")
    # A non-alpha replacement, wrong pivot and wrong canvas all refuse.
    flat = Image.new("RGB", (16, 16), (30, 30, 200))
    flat.save(tmp_path / "flat.png")
    with pytest.raises(FilmError, match="alpha"):
        import_replacement_drawing(p, tmp_path / "flat.png",
                                   replaces=pins["subject"], frame=4,
                                   pivot=[8, 8])
    with pytest.raises(FilmError, match="pivot"):
        import_replacement_drawing(p, alt_png, replaces=pins["subject"],
                                   frame=4, pivot=[0, 0])
    big = Image.new("RGBA", (20, 20), (30, 30, 200, 255))
    big.save(tmp_path / "big.png")
    with pytest.raises(FilmError, match="canvas"):
        import_replacement_drawing(p, tmp_path / "big.png",
                                   replaces=pins["subject"], frame=4,
                                   pivot=[8, 8])


def test_rig_spec_rejects_cycles_and_order_lies(tmp_path):
    p, pins = scene(tmp_path)
    base = {"subject": _pin(pins["subject"]), "badge": _pin(pins["badge"])}
    # A parent cycle can never resolve a world transform.
    cyclic = _rig([_rig_layer("subject", "badge", pins["subject"], 0),
                   _rig_layer("badge", "subject", pins["badge"], 1)])
    cyclic["occlusion_order"] = ["subject", "badge"]
    with pytest.raises(FilmError, match="cycle"):
        import_rig_spec(p, cyclic)
    # Unknown parent id.
    orphan = _rig([_rig_layer("subject", None, pins["subject"], 0),
                   _rig_layer("badge", "ghost", pins["badge"], 1)])
    with pytest.raises(FilmError, match="unknown parent"):
        import_rig_spec(p, orphan)
    # children[] must mirror the parent links exactly.
    fake_children = _rig([_rig_layer("subject", None, pins["subject"], 0),
                          _rig_layer("badge", "subject", pins["badge"], 1)])
    fake_children["children"]["subject"] = []
    with pytest.raises(FilmError, match="children"):
        import_rig_spec(p, fake_children)
    # Occlusion order must be a permutation AND agree with z_order.
    lying = _rig([_rig_layer("subject", None, pins["subject"], 0),
                  _rig_layer("badge", "subject", pins["badge"], 1)])
    lying["occlusion_order"] = ["badge", "subject"]
    with pytest.raises(FilmError, match="z_order|occlusion_order"):
        import_rig_spec(p, lying)
    # Rig z_order must match the pinned layer asset's own z_order.
    wrong_z = _rig([_rig_layer("subject", None, pins["subject"], 7),
                    _rig_layer("badge", "subject", pins["badge"], 9)])
    with pytest.raises(FilmError, match="z_order"):
        import_rig_spec(p, wrong_z)


def test_revised_replacement_opens_a_new_revision(tmp_path):
    p, pins = scene(tmp_path)
    first = import_replacement_drawing(
        p, _sprite(tmp_path / "alt3.png", color=(10, 10, 240)),
        replaces=pins["subject"], frame=2, pivot=[8, 8])
    second = import_replacement_drawing(
        p, _sprite(tmp_path / "alt4.png", color=(11, 11, 240)),
        replaces=pins["subject"], frame=2, pivot=[8, 8],
        asset_id=first["asset_id"])
    assert second["revision"] == first["revision"] + 1
    assert second["content_sha256"] != first["content_sha256"]
    # Both revisions still resolve: earlier bytes were never overwritten.
    resolve_asset(p, first["asset_id"], first["revision"],
                  first["content_sha256"])
    resolve_asset(p, second["asset_id"], second["revision"],
                  second["content_sha256"])
    # Identical re-import reuses the pinned revision.
    third = import_replacement_drawing(
        p, tmp_path / "alt4.png", replaces=pins["subject"], frame=2,
        pivot=[8, 8], asset_id=first["asset_id"])
    assert third["revision"] == second["revision"]
    assert third["new_revision"] is False


def test_registry_cross_reference_pass_catches_dangling_pins(tmp_path):
    p, pins = scene(tmp_path)
    registry = load_registry(p)
    mask = next(r for e in registry["assets"].values()
                for r in e["revisions"].values() if r["kind"] == "MASK")
    mask["target"]["content_sha256"] = "0" * 64
    # The forged pin changes the record, so the content digest must be
    # re-claimed consistently before the cross-reference pass can run.
    from engine.animation_assets import content_digest
    mask["content_sha256"] = content_digest(mask)
    write_canon(p / "manifest/animation_assets.json", registry)
    with pytest.raises(FilmError, match="pin does not match"):
        load_registry(p)


# --- plan document contract --------------------------------------------------

def test_plan_rejects_unsupported_paths_curves_and_gaps(tmp_path):
    p, pins = scene(tmp_path)
    plan = scene_plan(pins, intent="ANIMATED",
                      subject=_track(translate=("LINEAR",
                                                [(0, [10, 16]),
                                                 (23, [40, 16])])))
    # Only path C exists in v1.
    bad = copy.deepcopy(plan)
    bad["segments"][0]["path"] = "A"
    with pytest.raises(FilmError, match="Path A"):
        validate_shot_plan(bad, length=FRAMES)
    # Declared-unsupported capabilities fail with their name surfaced.
    bad = copy.deepcopy(plan)
    bad["segments"][0]["capabilities"] = ["MESH_DEFORMATION"]
    with pytest.raises(FilmError, match="MESH_DEFORMATION"):
        validate_shot_plan(bad, length=FRAMES)
    bad["segments"][0]["capabilities"] = ["AUTO_LIP_SYNC"]
    with pytest.raises(FilmError, match="AUTO_LIP_SYNC"):
        validate_shot_plan(bad, length=FRAMES)
    bad["segments"][0]["capabilities"] = ["SOMETHING_ELSE"]
    with pytest.raises(FilmError, match="Unknown capabilities"):
        validate_shot_plan(bad, length=FRAMES)
    # Only step and linear curves exist.
    bad = copy.deepcopy(plan)
    bad["tracks"]["layers"]["subject"]["transform"]["channels"][
        "translate"]["curve"] = "BEZIER"
    with pytest.raises(FilmError, match="curve"):
        validate_shot_plan(bad, length=FRAMES)
    # Segments must tile the whole used range.
    bad = copy.deepcopy(plan)
    bad["segments"] = [{"start": 0, "end": 20, "path": "C",
                        "capabilities": []}]
    with pytest.raises(FilmError, match="cover"):
        validate_shot_plan(bad, length=FRAMES)
    # Every channel needs a declared value on frame 0.
    bad = copy.deepcopy(plan)
    bad["tracks"]["layers"]["subject"]["transform"]["channels"][
        "translate"]["keys"].pop(0)
    with pytest.raises(FilmError, match="frame 0"):
        validate_shot_plan(bad, length=FRAMES)
    # Unknown document fields refuse storage.
    bad = copy.deepcopy(plan)
    bad["surprise"] = True
    with pytest.raises(FilmError, match="animation_shot_plan"):
        save_shot_plan(p, bad)
    # Canvas must match the project format.
    bad = copy.deepcopy(plan)
    bad["canvas"] = {"width": 128, "height": 96}
    with pytest.raises(FilmError, match="canvas"):
        save_shot_plan(p, bad)


def test_plan_is_stored_canonically_and_reloads(tmp_path):
    p, pins = scene(tmp_path)
    plan = scene_plan(pins, intent="ANIMATED",
                      subject=_track(translate=("LINEAR",
                                                [(0, [10, 16]),
                                                 (23, [40, 16])])))
    result = save_shot_plan(p, plan)
    stored = p / plan_path("S001")
    assert result["sha256"] == digest(stored)
    assert read_canon(stored)["document_type"] == "animation_shot_plan"
    info = validate_shot_plan(load_shot_plan(p, "S001", length=FRAMES,
                                             canvas={"width": W,
                                                     "height": H}),
                              length=FRAMES)
    assert info["layers"] == ["badge", "occluder", "plate", "subject"]
    # A non-canonical rewrite is rejected.
    stored.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    with pytest.raises(FilmError, match="canonical"):
        load_shot_plan(p, "S001")


# --- composition: transforms, hierarchy, z-order -----------------------------

def test_parent_transform_carries_child_layer(tmp_path):
    p, pins = scene(tmp_path)
    centroids = []
    for tx in (12, 36):
        result = _composite(p, scene_plan(
            pins, intent="STATIC",
            subject=_track(translate=("STEP", [(0, [tx, 16])])),
            badge=_track(translate=("STEP", [(0, [2, 2])])),
            occluder=_track(translate=("STEP", [(0, [-20, 0])]))))
        image = _member(p, result, 0)
        centroids.append(_centroid(image, _green))
    # The badge rides the subject: a 24px parent move shifts the child 24px.
    assert centroids[0] is not None and centroids[1] is not None
    assert centroids[1][0] - centroids[0][0] == pytest.approx(24, abs=1.0)
    # And the child's own local offset lands it beside the subject origin.
    assert centroids[0][0] == pytest.approx(12 + 2 + 2.5, abs=1.5)


def test_pivot_controls_rotation(tmp_path):
    """Rotating about the sprite centre vs its corner moves the drawing."""
    results = {}
    for pivot, tag in (((8, 8), "centre"), ((0, 0), "corner")):
        root = tmp_path / tag
        root.mkdir()
        p, pins = scene(root, subject_pivot=pivot)
        result = _composite(p, scene_plan(
            pins, intent="STATIC",
            subject=_track(translate=("STEP", [(0, [30, 20])]),
                           rotate=("STEP", [(0, 90)])),
            badge=_track(translate=("STEP", [(0, [2, 2])])),
            occluder=_track(translate=("STEP", [(0, [-20, 0])]))))
        results[tag] = _member(p, result, 0)
    centre_c = _centroid(results["centre"], _red)
    corner_c = _centroid(results["corner"], _red)
    # Rotation about the sprite centre keeps the drawing centred in place —
    # the pivot sits at canvas (30+8, 20+8); bilinear edges plus the corner
    # marker skew the centroid a couple of pixels.
    assert centre_c == pytest.approx((38, 28), abs=2.5)
    # Rotation about the corner swings the body to (-y, x): (-8, +8) shift.
    assert corner_c == pytest.approx((30 - 8, 20 + 8), abs=2.5)
    assert not (np.abs(centre_c - corner_c) < 4).all()


def test_explicit_z_order_controls_occlusion(tmp_path):
    counts = []
    for order in (["plate", "subject", "badge", "occluder"],
                  ["plate", "occluder", "subject", "badge"]):
        root = tmp_path / f"order{len(counts)}"
        root.mkdir()
        p, pins = scene(root, order=order)
        result = _composite(p, scene_plan(
            pins, intent="STATIC",
            subject=_track(translate=("STEP", [(0, [34, 16])])),
            occluder=_track(translate=("STEP", [(0, [30, 0])]))))
        image = _member(p, result, 0)
        band = image[:, 31:39]
        counts.append(int(_red(band).sum()))
    # Occluder on top hides the sprite band; subject on top paints over it.
    assert counts[0] == 0
    assert counts[1] > 30


def test_occlusion_changes_as_subject_moves(tmp_path):
    """A sweeping subject disappears behind the bar and reappears — real
    changing occlusion, not a static slate."""
    p, pins = scene(tmp_path)
    result = _composite(p, scene_plan(
        pins, intent="ANIMATED",
        subject=_track(translate=("LINEAR", [(0, [10, 16]),
                                             (23, [50, 16])])),
        occluder=_track(translate=("STEP", [(0, [30, 0])]))))
    early = _member(p, result, 1)
    middle = _member(p, result, 12)
    late = _member(p, result, 23)
    band = lambda img: int(_red(img[:, 30:40]).sum())
    # Early: sprite left of the bar; middle: hidden inside the band;
    # late: reappeared past the bar — the visible count dips then recovers.
    assert band(early) == 0 and _red(early).sum() > 100
    assert band(middle) == 0
    assert _red(middle).sum() < _red(early).sum()
    assert _red(late).sum() > _red(middle).sum()
    assert int(_bar(middle).sum()) > 300


def test_mask_reveals_only_its_region(tmp_path):
    p, pins = scene(tmp_path)
    result = _composite(p, scene_plan(
        pins, intent="STATIC", mask=pins["mask"],
        subject=_track(translate=("STEP", [(0, [24, 16])])),
        occluder=_track(translate=("STEP", [(0, [-20, 0])]))))
    image = _member(p, result, 0)
    # Subject spans canvas x in [24, 40); the mask reveals only its left
    # half (local x < 8 -> canvas x < 32).
    assert int(_red(image[:, 25:32]).sum()) > 30
    assert int(_red(image[:, 33:]).sum()) == 0
    assert int(_red(image[:, :24]).sum()) == 0


def test_region_mask_reveals_the_other_half(tmp_path):
    p = c_project(tmp_path)
    subject = import_layer_rgba(p, _sprite(tmp_path / "subject.png"),
                                pivot=[8, 8], z_order=0)
    mask = import_mask(p, _mask_region(tmp_path / "mreg.png"),
                       target=_pin(subject), region=[8, 0, 8, 16])
    rig = import_rig_spec(p, _rig([
        _rig_layer("subject", None, _pin(subject), 0)]))
    plan = scene_plan(
        {"subject": _pin(subject), "rig": _pin(rig)},
        intent="STATIC", subject=_track(translate=("STEP", [(0, [24, 16])])),
        mask=_pin(mask), frames=FRAMES)
    result = _composite(p, plan)
    image = _member(p, result, 0)
    # Only local x in [8,16) — canvas x in [32,40) — is revealed.
    assert int(_red(image[:, :32]).sum()) == 0
    assert int(_red(image[:, 33:]).sum()) > 30


def test_replacement_drawing_swaps_on_its_exposure(tmp_path):
    p, pins = scene(tmp_path)
    result = _composite(p, scene_plan(
        pins, intent="ANIMATED",
        subject=_track(translate=("STEP", [(0, [24, 16])])),
        occluder=_track(translate=("STEP", [(0, [-20, 0])])),
        swap=[(0, 4, 0), (4, FRAMES, 1)]))
    before, after = _member(p, result, 2), _member(p, result, 6)
    assert int(_red(before).sum()) > 100 and int(_blue(before).sum()) == 0
    assert int(_blue(after).sum()) > 100 and int(_red(after).sum()) == 0
    assert result["motion"]["subject_moving"] is True


def test_replacement_declared_frame_must_match_first_use(tmp_path):
    p, pins = scene(tmp_path)
    wrong = import_replacement_drawing(
        p, _sprite(tmp_path / "alt_late.png", color=(30, 200, 30)),
        replaces=pins["subject"], frame=8, pivot=[8, 8])
    plan = scene_plan(pins, intent="ANIMATED",
                      subject=_track(translate=("STEP", [(0, [24, 16])])),
                      swap=[(0, 4, 0), (4, FRAMES, 1)])
    plan["tracks"]["layers"]["subject"]["drawings"][1] = _pin(wrong)
    plan["assets"] = [a for a in plan["assets"]
                      if a["role"] != "replacement"]
    plan["assets"].append(_asset(_pin(wrong), "REPLACEMENT_DRAWING",
                                 "replacement"))
    with pytest.raises(FilmError, match="declares frame 8"):
        _composite(p, plan)


# --- subject vs camera, transparent edges, curves, motion gate ---------------

def test_subject_motion_is_separate_from_camera(tmp_path):
    root = tmp_path
    shots = {}
    for camera, tag in ((None, "fixed"), (
            _track(translate=("STEP", [(0, [0, 0]), (8, [10, 0])])),
            "pan")):
        root_tag = root / tag
        root_tag.mkdir()
        p, pins = scene(root_tag)
        result = _composite(p, scene_plan(
            pins, intent="ANIMATED", camera=camera,
            subject=_track(translate=("LINEAR", [(0, [20, 16]),
                                                 (23, [40, 16])])),
            occluder=_track(translate=("STEP", [(0, [-20, 0])]))))
        shots[tag] = (p, result)
    p_fixed, res_fixed = shots["fixed"]
    p_pan, res_pan = shots["pan"]
    fixed12 = _member(p_fixed, res_fixed, 12)
    pan12 = _member(p_pan, res_pan, 12)
    # Subject track alone: the plate landmark never moves.
    assert _centroid(_member(p_fixed, res_fixed, 1), _landmark) \
        == pytest.approx((0, 0), abs=0.6)
    assert _centroid(fixed12, _landmark) == pytest.approx((0, 0), abs=0.6)
    # With the camera panned +10x on frame 8, the whole scene — landmark
    # included — shifts while the subject keeps its own motion.
    assert _centroid(_member(p_pan, res_pan, 2), _landmark) \
        == pytest.approx((0, 0), abs=0.6)
    assert _centroid(pan12, _landmark) == pytest.approx((10, 0), abs=1.0)
    fixed_c = _centroid(fixed12, _red)
    pan_c = _centroid(pan12, _red)
    assert pan_c[0] - fixed_c[0] == pytest.approx(10, abs=1.5)
    assert pan_c[1] - fixed_c[1] == pytest.approx(0, abs=1.5)


def test_transparent_edge_blends_without_halo(tmp_path):
    p, pins = scene(tmp_path)
    result = _composite(p, scene_plan(
        pins, intent="STATIC",
        subject=_track(translate=("STEP", [(0, [24, 16])])),
        occluder=_track(translate=("STEP", [(0, [-20, 0])]))))
    image = _member(p, result, 0)
    plate = np.asarray(Image.open(_plate(tmp_path / "plate_check.png"))
                       .convert("RGB"), dtype=np.int32)
    # Sprite top border (alpha 128) at canvas (34, 16) — clear of the
    # badge's patch — is a real blend: not the sprite's full red, not the
    # bare plate, never a black halo.
    edge = image[16, 34]
    under = plate[16, 34]
    assert under[0] < edge[0] < 200
    assert int(edge[0]) - int(under[0]) > 20
    # The sprite's fully transparent outside shows the plate unchanged.
    assert np.abs(image[13, 34] - plate[13, 34]).max() <= 2


def test_step_and_linear_curves(tmp_path):
    p, pins = scene(tmp_path)
    step = _composite(p, scene_plan(
        pins, intent="ANIMATED",
        subject=_track(translate=("STEP", [(0, [14, 16]), (12, [40, 16])])),
        occluder=_track(translate=("STEP", [(0, [-20, 0])]))))
    linear = _composite(p, scene_plan(
        pins, intent="ANIMATED",
        subject=_track(translate=("LINEAR", [(0, [14, 16]),
                                             (20, [40, 16])])),
        occluder=_track(translate=("STEP", [(0, [-20, 0])]))))
    step6, step18 = (_centroid(_member(p, step, f), _red) for f in (6, 18))
    lin10, lin18 = (_centroid(_member(p, linear, f), _red) for f in (10, 18))
    # STEP: held then jumped; LINEAR: interpolated through the midpoint.
    # The sprite's centroid sits at translate + half the 16px drawing.
    assert step6[0] == pytest.approx(14 + 8, abs=1.0)
    assert step18[0] == pytest.approx(40 + 8, abs=1.0)
    assert lin10[0] == pytest.approx(14 + (40 - 14) * 0.5 + 8, abs=1.5)
    assert lin18[0] == pytest.approx(14 + (40 - 14) * 0.9 + 8, abs=1.5)


def test_camera_only_motion_cannot_satisfy_animated_intent(tmp_path):
    p, pins = scene(tmp_path)
    plan = scene_plan(pins, intent="ANIMATED",
                      camera=_track(translate=("STEP",
                                               [(0, [0, 0]), (8, [10, 0])])))
    with pytest.raises(FilmError, match="pan/zoom"):
        _composite(p, plan)
    # The same plan is honest as an explicit STATIC hold.
    plan["motion_intent"] = "STATIC"
    result = _composite(p, plan)
    assert result["motion"]["camera_moving"] is True
    assert result["motion"]["subject_moving"] is False


def test_disallowed_channel_is_refused_by_the_rig(tmp_path):
    p, pins = scene(tmp_path)
    rigged = _rig([_rig_layer("subject", None, pins["subject"], 1,
                              transforms=["translate"])])
    rig = import_rig_spec(p, rigged)
    plan = scene_plan({"subject": pins["subject"], "rig": _pin(rig)},
                      intent="ANIMATED",
                      subject=_track(translate=("STEP", [(0, [24, 16])]),
                                     rotate=("STEP", [(0, 45)])))
    plan["tracks"]["layers"] = {"subject": plan["tracks"]["layers"]
                                ["subject"]}
    plan["assets"] = [_asset(pins["subject"], "LAYER_RGBA", "layer",
                             [24, 16])]
    with pytest.raises(FilmError, match="does not allow"):
        _composite(p, plan)


# --- recipe, sequence output, downstream consumption --------------------------

def test_composite_registers_recipe_bound_sequence(tmp_path):
    p, pins = scene(tmp_path)
    result = _composite(p, scene_plan(
        pins, intent="ANIMATED",
        subject=_track(translate=("LINEAR", [(0, [16, 16]),
                                             (23, [44, 16])]))))
    assert result["kind"] == "COMPOSITE_SEQUENCE"
    assert result["frames"] == FRAMES
    recipe = read_canon(p / result["recipe_path"])
    assert recipe["document_type"] == "composite_recipe"
    assert recipe["schema_version"] == 1
    assert recipe["plan_sha256"] == digest(p / plan_path("S001"))
    assert recipe["color_path"] == {"input": "sRGB",
                                    "working": "LINEAR_PREMULTIPLIED_ALPHA",
                                    "output": "sRGB"}
    assert recipe["camera"]["evaluation"] == "ONES"
    record = load_registry(p)["assets"][result["asset_id"]] \
        ["revisions"][str(result["revision"])]
    assert record["composite_recipe_ref"] == digest(
        p / result["recipe_path"])
    assert record["acceptance"]["state"] == "DRAFT"
    assert {d["asset_id"] for d in record["dependencies"]} >= {
        pins["rig"]["asset_id"], pins["subject"]["asset_id"]}
    # The shot's assignment pins the composite; the timeline adopted it.
    pin = load_registry(p)["assignments"]["S001"]
    assert pin["kind"] == "COMPOSITE_SEQUENCE"
    timeline = read_canon(p / "timeline/edit.json")
    assert timeline["entries"][0]["sequence_revision"] == result["revision"]
    resolved = resolve_shot_sequence(p, "S001", [0, FRAMES],
                                     {"before": 0, "after": 0},
                                     result["revision"])
    assert resolved["record"]["kind"] == "COMPOSITE_SEQUENCE"
    assert len(resolved["members"]) == FRAMES


def test_composite_is_deterministic_and_reuses_revision(tmp_path):
    p, pins = scene(tmp_path)
    plan = scene_plan(pins, intent="ANIMATED",
                      subject=_track(translate=("LINEAR",
                                                [(0, [16, 16]),
                                                 (23, [44, 16])])))
    first = _composite(p, plan)
    second = _composite(p, plan)
    assert second["new_revision"] is False
    assert second["revision"] == first["revision"]
    assert second["content_sha256"] == first["content_sha256"]


def test_composite_feeds_the_existing_final_candidate_path(tmp_path):
    """A composite + one fixture cut review satisfies the same Build 2 gate
    an imported sequence satisfies — no bypass, no fallback."""
    p, pins = scene(tmp_path)
    result = _composite(p, scene_plan(
        pins, intent="ANIMATED",
        subject=_track(translate=("LINEAR", [(0, [18, 16]),
                                             (23, [46, 16])])),
        occluder=_track(translate=("STEP", [(0, [-20, 0])])),
        swap=[(0, 4, 0), (4, FRAMES, 1)]))
    normalized = normalize_shot_sequence(p, "S001")
    assert normalized["pin"]["content_sha256"] == result["content_sha256"]
    assert normalized["entries"][0]["frames"] == FRAMES
    record_cut_review(p, "I001", reviewer=REVIEWER, methods=CUT_METHODS)
    built = compile_final(p)
    folder, record = newest_build(p)
    assert built["status"] == "COMPLETE"
    assert record["mode"] == "FINAL_CANDIDATE"
    assert verify_build(folder)["valid"]
    # The build froze the composite's member bytes into its snapshot.
    assert any(name.startswith("snapshot/animation/assets/")
               and name.endswith("f000005.png")
               for name in record["files"])
    rows = [json.loads(line) for line in
            (folder / "frame_map.jsonl").read_text().splitlines()]
    assert len(rows) == FRAMES
    assert rows[0]["sources"][0]["sequence_revision"] == result["revision"]


def test_composite_requires_the_animation_profile(tmp_path):
    p = fixture_project(tmp_path, seconds=1, shot_count=1)
    sprite = _sprite(tmp_path / "sprite.png")
    for call in (lambda: composite_shot(p, "S001"),
                 lambda: save_shot_plan(p, {"document_type":
                                            "animation_shot_plan",
                                            "schema_version": 1}),
                 lambda: import_mask(p, sprite, target={"asset_id": "A0001",
                                                        "revision": 1,
                                                        "content_sha256":
                                                        "0" * 64}),
                 lambda: import_rig_spec(p, {"layers": []}),
                 lambda: import_replacement_drawing(
                     p, sprite, replaces={"asset_id": "A0001", "revision": 1,
                                          "content_sha256": "0" * 64},
                     frame=0, pivot=[0, 0])):
        with pytest.raises(FilmError, match="LEGACY_MV"):
            call()


def test_plan_layers_must_cover_the_rig(tmp_path):
    p, pins = scene(tmp_path)
    plan = scene_plan(pins, intent="ANIMATED",
                      subject=_track(translate=("STEP", [(0, [24, 16])])))
    plan["tracks"]["layers"].pop("badge")
    with pytest.raises(FilmError, match="cover the rig"):
        _composite(p, plan)
    plan = scene_plan(pins, intent="ANIMATED",
                      subject=_track(translate=("STEP", [(0, [24, 16])])))
    ghost = copy.deepcopy(plan["tracks"]["layers"]["subject"])
    for schedule in ghost["exposure"]:
        schedule["track"] = "ghost"
    plan["tracks"]["layers"]["ghost"] = ghost
    with pytest.raises(FilmError, match="cover the rig"):
        _composite(p, plan)


def test_cli_imports_plan_and_composite(tmp_path):
    p = c_project(tmp_path)
    layer = _sprite(tmp_path / "layer.png")
    result = cli.main(["import-animation-asset", str(p), "--kind",
                       "LAYER_RGBA", "--file", str(layer), "--pivot", "8,8",
                       "--z-order", "0"])
    assert result == 0
    registry = load_registry(p)
    layer_rec = next(r for e in registry["assets"].values()
                     for r in e["revisions"].values()
                     if r["kind"] == "LAYER_RGBA")
    pin = f"{layer_rec['asset_id']}:{layer_rec['revision']}:" \
        f"{layer_rec['content_sha256']}"
    mask = _mask_half(tmp_path / "mask.png")
    assert cli.main(["import-animation-asset", str(p), "--kind", "MASK",
                     "--file", str(mask), "--target", pin,
                     "--channel", "LUMINANCE"]) == 0
    # A MASK import without --target fails honestly.
    assert cli.main(["import-animation-asset", str(p), "--kind", "MASK",
                     "--file", str(mask)]) == 1
    assert cli.main(["import-animation-asset", str(p), "--kind",
                     "REPLACEMENT_DRAWING", "--file", str(layer),
                     "--replaces", pin, "--frame", "4",
                     "--pivot", "8,8"]) == 0
    spec = _rig([_rig_layer("subject", None,
                          {"asset_id": layer_rec["asset_id"],
                           "revision": layer_rec["revision"],
                           "content_sha256": layer_rec["content_sha256"]},
                          0)])
    spec_file = tmp_path / "rig.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    assert cli.main(["import-animation-asset", str(p), "--kind", "RIG_SPEC",
                     "--file", str(spec_file)]) == 0
    registry = load_registry(p)
    pins = {"subject": {k: layer_rec[k] for k in
                        ("asset_id", "revision", "content_sha256")}}
    rig_rec = next(r for e in registry["assets"].values()
                   for r in e["revisions"].values()
                   if r["kind"] == "RIG_SPEC")
    pins["rig"] = {k: rig_rec[k] for k in
                   ("asset_id", "revision", "content_sha256")}
    plan = scene_plan(pins, intent="ANIMATED",
                      subject=_track(translate=("LINEAR",
                                                [(0, [16, 16]),
                                                 (23, [44, 16])])))
    plan["tracks"]["layers"] = {"subject": plan["tracks"]["layers"]
                                ["subject"]}
    plan["assets"] = [_asset(pins["subject"], "LAYER_RGBA", "layer",
                             [24, 16])]
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    assert cli.main(["shot-plan", str(p), "S001",
                     "--file", str(plan_file)]) == 0
    assert cli.main(["compile-shot", str(p), "S001"]) == 0
    resolved = resolve_shot_sequence(p, "S001")
    assert resolved["record"]["kind"] == "COMPOSITE_SEQUENCE"
    # Legacy projects cannot run the new commands.
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    legacy = fixture_project(legacy_root, seconds=1, shot_count=1)
    assert cli.main(["shot-plan", str(legacy), "S001",
                     "--file", str(plan_file)]) == 1
    assert cli.main(["compile-shot", str(legacy), "S001"]) == 1
