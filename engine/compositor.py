"""FRAME_ANIMATION_V1 native C-path layer compositor (ANIM-008, design 8.4).

`composite_shot` binds a shot's `animation_shot_plan` to the hash-pinned
registry revisions it names — a RIG_SPEC for the layer hierarchy, LAYER_RGBA
base drawings, REPLACEMENT_DRAWING alternates and MASK mattes — and renders
the cut's output frames:

- each layer is transformed in its own asset coordinate space
  (translate/rotate/scale about its recorded pivot, times opacity), then
  mapped through its parent chain into the cut canvas — parent transforms
  carry children rigidly;
- layers composite back-to-front in the rig's explicit occlusion order over
  the declared background, in linear-light premultiplied alpha; masks
  multiply layer alpha in layer space before the transform;
- replacement drawings enter through the layer's exposure schedule: the
  drawing table index selects base or alternate art per output frame, and
  each replacement's declared `frame` must match its first exposed frame;
- the camera is a separate track evaluated on ones and applied to the
  assembled cut canvas last — subject motion and camera movement never share
  a track, and a camera-only pan/zoom cannot satisfy `motion_intent:
  ANIMATED`;
- filtering runs on premultiplied alpha (bilinear); the recipe records the
  colour path sRGB -> linear working space -> sRGB.

The result is registered as a COMPOSITE_SEQUENCE revision with a
`composite_recipe` document binding the plan, pins, tracks and colour path —
the shot's assignment and adopted revision follow the same draft discipline
as imports, so the existing cut-review and Final-candidate paths consume it
unchanged. Nothing here is a motion or artwork approval.
"""
from bisect import bisect_right
from fractions import Fraction
from pathlib import Path
import hashlib
import math
import shutil
import tempfile

import numpy as np
from PIL import Image

from .animation_assets import (_resolve_pin, _verify_member_bytes,
                               load_registry, register_composite_sequence)
from .animation_schema import (canon_bytes, load_animation_timeline,
                               require_animation_profile, write_canon)
from .core import FilmError, digest, project_mutex, safe_path
from .exposure import expand_track
from .motion_plan import load_shot_plan, plan_path

RECIPE_TYPE = "composite_recipe"
RECIPE_PATH_TEMPLATE = "animation/shots/{shot_id}/composite_recipe.json"

# The working colour path recorded in every composite recipe.
COLOR_PATH = {"input": "sRGB", "working": "LINEAR_PREMULTIPLIED_ALPHA",
              "output": "sRGB"}
FILTER = "BILINEAR_ON_PREMULTIPLIED_ALPHA"


# --- colour management ---------------------------------------------------

def _srgb_to_linear_table():
    def decode(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return np.array([decode(v / 255.0) for v in range(256)],
                    dtype=np.float32)


_S2L = _srgb_to_linear_table()


def _linear_to_srgb(lin):
    """Inverse sRGB EOTF on a clipped float array, values in [0, 1]."""
    lin = np.clip(lin, 0.0, 1.0)
    return np.where(lin <= 0.0031308,
                    lin * 12.92,
                    1.055 * np.power(lin, 1.0 / 2.4) - 0.055)


# --- transform evaluation -------------------------------------------------

def _rat_fraction(value):
    return Fraction(value) if type(value) is int \
        else Fraction(value["num"], value["den"])


def _eval_channel(channel, frame):
    """One channel's value at `frame` (step hold or linear interpolation)."""
    keys = channel["keys"]
    index = bisect_right([k["frame"] for k in keys], frame) - 1
    if index < 0:
        raise FilmError("Transform channel has no value before its first key")
    key = keys[index]
    if channel["curve"] == "STEP" or index == len(keys) - 1:
        value = key["value"]
        if type(value) is list:
            return [float(_rat_fraction(v)) for v in value]
        return float(_rat_fraction(value))
    nxt = keys[index + 1]
    t = Fraction(frame - key["frame"], nxt["frame"] - key["frame"])
    if type(key["value"]) is list:
        return [float(_rat_fraction(a) + (_rat_fraction(b)
                                         - _rat_fraction(a)) * t)
                for a, b in zip(key["value"], nxt["value"])]
    return float(_rat_fraction(key["value"])
                 + (_rat_fraction(nxt["value"])
                    - _rat_fraction(key["value"])) * t)


def _eval_transform(track, frame):
    """(tx, ty, rot_degrees, sx, sy, opacity) for one frame; None = identity."""
    values = {"translate": [0.0, 0.0], "rotate": 0.0, "scale": [1.0, 1.0],
              "opacity": 1.0}
    if track is not None:
        for name, channel in track["channels"].items():
            values[name] = _eval_channel(channel, frame)
    tx, ty = values["translate"]
    sx, sy = values["scale"]
    return tx, ty, values["rotate"], sx, sy, values["opacity"]


def _local_matrix(t, pivot):
    """T(translate) · T(pivot) · R(rotate) · S(scale) · T(-pivot).

    Asset space -> parent space: the drawing maps 1:1 at identity, and
    rotation/scale pivot about the recorded asset point — a null or zero
    transform leaves the layer exactly where it was authored.
    """
    tx, ty, degrees, sx, sy, _opacity = t
    rad = math.radians(degrees)
    cos_v, sin_v = math.cos(rad), math.sin(rad)
    px, py = pivot
    return np.array([
        [cos_v * sx, -sin_v * sy,
         tx + px - cos_v * sx * px + sin_v * sy * py],
        [sin_v * sx, cos_v * sy,
         ty + py - sin_v * sx * px - cos_v * sy * py],
        [0.0, 0.0, 1.0]], dtype=np.float64)


def _channel_varies(channel):
    values = [canon_bytes(k["value"]) for k in channel["keys"]]
    return len(set(values)) > 1


def _track_varies(track):
    if track is None:
        return False
    return any(_channel_varies(channel)
               for channel in track["channels"].values())


# --- pixel work ------------------------------------------------------------

def _premult(image, mask=None):
    """sRGB PNG -> linear premultiplied-alpha float array, mask folded in."""
    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    straight = rgba.astype(np.float32) / 255.0
    alpha = straight[..., 3:4]
    if mask is not None:
        alpha = alpha * mask
    rgb = _S2L[rgba[..., :3]]
    return np.concatenate([rgb * alpha, alpha], axis=-1)


def _mask_grid(image, channel, region, width, height):
    """Sample the mask into a layer-space alpha multiplier in [0, 1]."""
    if channel == "ALPHA":
        values = np.asarray(image.getchannel("A"), dtype=np.float32) / 255.0
    else:
        values = np.asarray(image.convert("L"), dtype=np.float32) / 255.0
    grid = np.zeros((height, width), dtype=np.float32)
    if region is None:
        grid[:, :] = values
    else:
        x, y, w, h = region
        grid[y:y + h, x:x + w] = values[:h, :w]
    return grid[..., np.newaxis]


def _warp(premult, matrix, width, height):
    """Affine-warp premultiplied floats into the output canvas."""
    inverse = np.linalg.inv(matrix)
    coeffs = (float(inverse[0, 0]), float(inverse[0, 1]),
              float(inverse[0, 2]), float(inverse[1, 0]),
              float(inverse[1, 1]), float(inverse[1, 2]))
    out = np.zeros((height, width, 4), dtype=np.float32)
    for channel in range(4):
        image = Image.fromarray(premult[..., channel], mode="F")
        warped = image.transform((width, height), Image.AFFINE,
                                 data=coeffs, resample=Image.BILINEAR,
                                 fillcolor=0.0)
        out[..., channel] = np.asarray(warped, dtype=np.float32)
    return out


def _over(dst, src):
    """Porter-Duff source-over on premultiplied floats: src lands on top."""
    return src + dst * (1.0 - src[..., 3:4])


def _flatten(premult, background_linear):
    """Premultiplied linear canvas over the background -> sRGB uint8 RGB."""
    rgb = premult[..., :3] \
        + background_linear * (1.0 - premult[..., 3:4])
    srgb = _linear_to_srgb(rgb)
    return Image.fromarray(np.round(srgb * 255.0).astype(np.uint8), "RGB")


# --- plan binding -----------------------------------------------------------

def _resolve_member(registry, p, pin, want_kind, what):
    record = _resolve_pin(registry, pin["asset_id"], pin["revision"],
                          pin["content_sha256"])
    if record["kind"] != want_kind:
        raise FilmError(f"{what} must pin a {want_kind} revision "
                        f"(found {record['kind']})")
    members = _verify_member_bytes(p, record)
    return record, [path for _index, path in members]


def _bind_layers(p, registry, plan, spec, length):
    """Resolve every layer's artwork, mask and schedules against the rig."""
    rig_layers = {layer["id"]: layer for layer in spec["layers"]}
    tracks = plan["tracks"]["layers"]
    if set(tracks.keys()) != set(rig_layers):
        missing = set(rig_layers) - set(tracks)
        extra = set(tracks) - set(rig_layers)
        raise FilmError("Plan layer tracks must cover the rig exactly "
                        f"(missing {sorted(missing)}, unknown {sorted(extra)})")
    bound = {}
    for layer_id in spec["occlusion_order"]:
        rig_layer = rig_layers[layer_id]
        base_record, base_paths = _resolve_member(
            registry, p, rig_layer["asset"], "LAYER_RGBA",
            f"rig layer {layer_id} asset")
        track = tracks[layer_id]
        what = f"layer {layer_id}"
        if track["drawings"][0] != rig_layer["asset"]:
            raise FilmError(f"{what} drawing table must start with the rig "
                            "layer's pinned drawing")
        # Exposure expansion first: drawing indices and declared frames bind
        # against the same rows the composite will read.
        rows = expand_track(track["exposure"], length)
        drawings = []
        for index, pin in enumerate(track["drawings"]):
            if index == 0:
                record, paths = base_record, base_paths
            else:
                record, paths = _resolve_member(
                    registry, p, pin, "REPLACEMENT_DRAWING",
                    f"{what} drawing {index}")
                if record["replaces"] != track["drawings"][0]:
                    raise FilmError(f"{what} drawing {index} does not "
                                    "replace the layer's base drawing")
                if record["canvas"] != base_record["canvas"]:
                    raise FilmError(f"{what} drawing {index} does not share "
                                    "the base drawing's canvas")
                if record["rig"]["pivot"] != base_record["pivot"]:
                    raise FilmError(f"{what} drawing {index} rig pivot "
                                    "mismatches the base drawing's pivot")
                used = [row["frame"] for row in rows
                        if row["drawing"] == index]
                if not used:
                    raise FilmError(f"{what} drawing {index} is declared "
                                    "but never exposed")
                if record["frame"] != min(used):
                    raise FilmError(f"{what} drawing {index} declares "
                                    f"frame {record['frame']} but the "
                                    f"exposure first shows it on {min(used)}")
            drawings.append({"pin": pin, "record": record, "path": paths[0]})
        for row in rows:
            if row["drawing"] is None:
                raise FilmError(f"{what} exposure leaves a state-only slot; "
                                "layer tracks must name a drawing")
            if row["drawing"] >= len(drawings):
                raise FilmError(f"{what} exposure drawing {row['drawing']} "
                                f"exceeds the {len(drawings)}-entry "
                                "drawing table")
        transform = track["transform"]
        if transform is not None:
            for name in transform["channels"]:
                if name not in rig_layer["transforms"]:
                    raise FilmError(f"{what} animates {name}, which the rig "
                                    "does not allow for this layer")
        mask = None
        mask_pin = track["mask"]
        if mask_pin is not None:
            mask_record, mask_paths = _resolve_member(
                registry, p, mask_pin, "MASK", f"{what} mask")
            if mask_record["target"] != track["drawings"][0]:
                raise FilmError(f"{what} mask must target the layer's "
                                "base drawing")
            with Image.open(mask_paths[0]) as image:
                mask = _mask_grid(image, mask_record["channel"],
                                  mask_record["region"],
                                  base_record["canvas"]["width"],
                                  base_record["canvas"]["height"])
        bound[layer_id] = {
            "rig": rig_layer, "record": base_record, "track": track,
            "pivot": base_record["pivot"], "drawings": drawings,
            "exposure": rows, "mask": mask}
    return bound


def _check_declared_assets(plan, bound):
    """plan.assets must name exactly the artwork the cut consumes."""
    declared = {(a["asset_id"], a["revision"], a["content_sha256"]): a["role"]
                for a in plan["assets"]}
    used = {}
    for entry in bound.values():
        for index, drawing in enumerate(entry["drawings"]):
            pin = drawing["pin"]
            key = (pin["asset_id"], pin["revision"], pin["content_sha256"])
            used[key] = "layer" if index == 0 else "replacement"
        mask = entry["track"]["mask"]
        if mask is not None:
            key = (mask["asset_id"], mask["revision"], mask["content_sha256"])
            used[key] = "mask"
    missing = set(used) - set(declared)
    extra = set(declared) - set(used)
    if missing or extra:
        raise FilmError("plan assets must name exactly the artwork the cut "
                        f"consumes (missing {sorted(missing)}, unused "
                        f"{sorted(extra)})")
    wrong = [key for key in used if declared[key] != used[key]]
    if wrong:
        raise FilmError(f"plan asset roles mismatch their use: {sorted(wrong)}")


def _dependencies(plan, bound):
    pins = [dict(plan["rig"])]
    for layer in bound.values():
        pins += [dict(d["pin"]) for d in layer["drawings"]]
        if layer["track"]["mask"] is not None:
            pins.append(dict(layer["track"]["mask"]))
    return pins


def _world_matrix(bound, layer_id, frame):
    """The layer's asset-space -> cut-canvas matrix through its parent chain."""
    entry = bound[layer_id]
    local = _local_matrix(_eval_transform(
        entry["track"]["transform"], frame), entry["pivot"])
    parent = entry["rig"]["parent"]
    if parent is None:
        return local
    return _world_matrix(bound, parent, frame) @ local


def _render_frames(bound, plan, order, length, member_count, used_start,
                   width, height, stage, progress):
    """Render member indices [0, member_count) as PNGs."""
    canvas_bg = np.array(
        [_S2L[c] for c in plan["background"]], dtype=np.float32)
    camera = plan["tracks"]["camera"]
    premult = {}
    for layer_id, entry in bound.items():
        premult[layer_id] = []
        for drawing in entry["drawings"]:
            with Image.open(drawing["path"]) as image:
                premult[layer_id].append(_premult(image, entry["mask"]))
    names = []
    for member in range(member_count):
        if progress:
            progress(member, member_count, f"member {member + 1}")
        frame = min(max(member - used_start, 0), length - 1)
        accumulated = np.zeros((height, width, 4), dtype=np.float32)
        for layer_id in order:
            entry = bound[layer_id]
            drawing = entry["exposure"][frame]["drawing"]
            layer = premult[layer_id][drawing]
            opacity = _eval_transform(entry["track"]["transform"], frame)[5]
            if opacity != 1.0:
                layer = layer * np.float32(opacity)
            world = _world_matrix(bound, layer_id, frame)
            accumulated = _over(accumulated,
                                _warp(layer, world, width, height))
        camera_matrix = _local_matrix(
            _eval_transform(camera["transform"], frame), camera["pivot"])
        if not np.allclose(camera_matrix, np.eye(3)):
            accumulated = _warp(accumulated, camera_matrix, width, height)
        name = f"f{member:06d}.png"
        _flatten(accumulated, canvas_bg).save(stage / name)
        names.append(name)
    return names


def composite_shot(project, shot_id, *, instance_id=None, progress=None):
    """Composite one shot's declared C-path plan into a COMPOSITE_SEQUENCE.

    The rendered members cover the timeline entry's used range plus its
    unused handles as contiguous member indices: member `m` evaluates the
    plan at cut-local frame `m - used_start`, clamped to the plan span for
    handle members. The sequence is registered as a draft bound to the
    recipe digest; no review or approval is created.
    """
    p = Path(project)
    with project_mutex(p):
        config = require_animation_profile(p)
        timeline = load_animation_timeline(p)
        entries = [e for e in timeline["entries"] if e["shot_id"] == shot_id]
        if not entries:
            raise FilmError(f"{shot_id} has no timeline entry")
        if instance_id is not None:
            found = [e for e in entries if e["instance_id"] == instance_id]
            if not found:
                raise FilmError(f"{shot_id} has no timeline entry "
                                f"{instance_id}")
            entries = found
        if len(entries) > 1:
            raise FilmError(f"{shot_id} appears in {len(entries)} timeline "
                            "entries; a v1 composite supports one entry "
                            "per shot")
        entry = entries[0]
        used_start, used_end = entry["used_source_range"]
        length = used_end - used_start
        fmt = config["format"]
        width, height = fmt["width"], fmt["height"]
        plan = load_shot_plan(p, shot_id, length=length,
                              canvas={"width": width, "height": height})
        plan_sha256 = digest(safe_path(p, plan_path(shot_id)))
        registry = load_registry(p)
        rig_record, _rig_paths = _resolve_member(
            registry, p, plan["rig"], "RIG_SPEC", "plan rig")
        spec = rig_record["spec"]
        bound = _bind_layers(p, registry, plan, spec, length)
        _check_declared_assets(plan, bound)
        order = spec["occlusion_order"]
        subject_moving = any(
            _track_varies(entry["track"]["transform"])
            or len({row["drawing"] for row in entry["exposure"]}) > 1
            for entry in bound.values())
        camera = plan["tracks"]["camera"]
        camera_moving = _track_varies(camera["transform"])
        if plan["motion_intent"] == "ANIMATED" and not subject_moving:
            raise FilmError(
                "A whole-image pan/zoom alone is not ANIMATED: the plan "
                "declares motion_intent ANIMATED but no subject layer "
                "moves or changes drawing — camera tracks cannot satisfy "
                "subject motion")
        handles = entry["unused_handles"]
        member_count = used_end + handles["after"]
        asset_root = safe_path(p, "animation/assets")
        asset_root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".composite-", dir=asset_root))
        try:
            names = _render_frames(bound, plan, order, length,
                                   member_count, used_start, width,
                                   height, stage, progress)
            recipe = {
                "document_type": RECIPE_TYPE, "schema_version": 1,
                "shot_id": shot_id, "instance_id": entry["instance_id"],
                "plan_sha256": plan_sha256,
                "rig": dict(plan["rig"]),
                "layers": {lid: {"drawings": [dict(d["pin"])
                                            for d in e["drawings"]],
                                 "mask": e["track"]["mask"],
                                 "pivot": e["pivot"]}
                           for lid, e in bound.items()},
                "tracks": plan["tracks"],
                "canvas": {"width": width, "height": height},
                "background": plan["background"],
                "camera": {"pivot": camera["pivot"],
                           "evaluation": "ONES"},
                "color_path": dict(COLOR_PATH), "filter": FILTER,
                "members": {"first": 0, "count": member_count,
                            "used": [used_start, used_end],
                            "handles": dict(handles)}}
            recipe_path = safe_path(
                p, RECIPE_PATH_TEMPLATE.format(shot_id=shot_id))
            write_canon(recipe_path, recipe)
            members = []
            for name in names:
                data = (stage / name).read_bytes()
                members.append({"stored_name": name, "source": stage / name,
                                "sha256": hashlib.sha256(data).hexdigest(),
                                "byte_length": len(data),
                                "source_name": name})
            record, created = register_composite_sequence(
                p, shot_id, canvas={"width": width, "height": height},
                members=members,
                dependencies=_dependencies(plan, bound),
                composite_recipe_ref=digest(recipe_path),
                plan_sha256=plan_sha256)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return {"shot_id": shot_id, "instance_id": entry["instance_id"],
                "asset_id": record["asset_id"],
                "revision": record["revision"],
                "content_sha256": record["content_sha256"],
                "kind": "COMPOSITE_SEQUENCE", "frames": member_count,
                "state": "DRAFT", "new_revision": created,
                "plan_sha256": plan_sha256,
                "recipe_sha256": digest(recipe_path),
                "recipe_path": RECIPE_PATH_TEMPLATE.format(shot_id=shot_id),
                "motion": {"intent": plan["motion_intent"],
                           "subject_moving": subject_moving,
                           "camera_moving": camera_moving},
                "color_path": dict(COLOR_PATH), "filter": FILTER,
                "note": "Locally composited draft; creates no review, LOCK "
                        "or approval — qualification UNQUALIFIED"}
