"""FRAME_ANIMATION_V1 shot plans (ANIM-008, schema section 5, design 8.4).

`animation/shots/<shot_id>/plan.json` is the `animation_shot_plan` schema 1
document (CANON_JSON_V1). For the v1 native path it declares the shot's
C-path: the adopted RIG_SPEC pin, the artwork assets the cut consumes, the
contiguous path segments (only `"C"` is implemented — A/B stay declared
unsupported), and the layer/camera schedules.

Tracks follow the ExposureSchedule contract from `engine/exposure.py`: a
layer's `exposure` tiles `[0, cut_length)` and each slot's `drawing` indexes
that layer's drawing table (`drawings[0]` is the rig layer's base
LAYER_RGBA; later entries are REPLACEMENT_DRAWING pins). The camera is its
own track — it is evaluated on ones and carries only a transform, so subject
motion never hides inside camera pan/zoom. Transform tracks are step or
linear keyframe channels over translation/rotation/scale/opacity; there are
no other curve types in v1.

This module validates and stores the document; `engine/compositor.py` binds
it to resolved registry bytes and renders the cut. A stored plan is not an
approval — the composited sequence it produces stays a draft.
"""
from pathlib import Path

from .animation_schema import (SHOT_ID, check_document,
                               load_animation_timeline, read_canon,
                               require_animation_profile, write_canon)
from .core import FilmError, digest, project_mutex, safe_path
from .exposure import validate_track

PLAN_TYPE = "animation_shot_plan"
PLAN_FIELDS = {"document_type", "schema_version", "shot_id", "revision",
               "motion_intent", "story_role", "emotion_role", "canvas",
               "background", "rig", "assets", "events", "keyposes",
               "segments", "tracks", "preparation"}
PIN_FIELDS = {"asset_id", "revision", "content_sha256"}
ASSET_FIELDS = PIN_FIELDS | {"kind", "role", "layout"}
EVENT_FIELDS = {"type", "frame", "note"}
KEYPOSE_FIELDS = {"kind", "frame", "ref"}
SEGMENT_FIELDS = {"start", "end", "path", "capabilities"}
CAMERA_FIELDS = {"pivot", "transform"}
LAYER_TRACK_FIELDS = {"drawings", "exposure", "transform", "mask"}
PREP_FIELDS = {"work", "creator", "reviewer", "revision_scope"}

ASSET_ROLES = {"layer", "mask", "replacement"}
EVENT_TYPES = {"CONTACT", "DIRECTION_CHANGE", "OCCLUSION", "REAPPEARANCE",
               "NOTE"}
MOTION_INTENTS = {"STATIC", "ANIMATED"}

CURVES = {"STEP", "LINEAR"}
TRANSFORM_CHANNELS = {"translate", "rotate", "scale", "opacity"}

# v1 native C scope (design 8.4): everything a segment may honestly declare.
NATIVE_C_CAPABILITIES = {"RGBA_LAYER", "PARENT_TRANSFORM", "PIVOT",
                         "TRANSLATE", "ROTATE", "SCALE", "OPACITY",
                         "Z_ORDER", "MASK", "REPLACEMENT_DRAWING",
                         "CAMERA_TRANSFORM", "CURVE_STEP", "CURVE_LINEAR"}
# Named in the design as explicitly out of v1 scope; a plan declaring one is
# refused with the unsupported name surfaced, not silently dropped.
UNSUPPORTED_V1_CAPABILITIES = {"MESH_DEFORMATION", "INVERSE_KINEMATICS",
                               "3D_TRANSFORM", "AUTO_LIP_SYNC"}
UNIMPLEMENTED_PATHS = {"A", "B"}


def _int(value, what, minimum=0):
    if type(value) is not int or value < minimum:
        raise FilmError(f"{what} must be an integer >= {minimum}")
    return value


def _pin(value, what):
    if type(value) is not dict or set(value.keys()) != PIN_FIELDS:
        raise FilmError(f"{what} must be an asset pin "
                        "{asset_id, revision, content_sha256}")
    if type(value["asset_id"]) is not str or not value["asset_id"]:
        raise FilmError(f"{what} asset_id must be a non-empty string")
    _int(value["revision"], f"{what} revision", 1)
    sha = value["content_sha256"]
    if type(sha) is not str or len(sha) != 64:
        raise FilmError(f"{what} content_sha256 must be a SHA-256 hex")
    return value


def _point(value, what):
    if (type(value) is not list or len(value) != 2
            or any(type(v) is not int for v in value)):
        raise FilmError(f"{what} must be an [x, y] integer pair")
    return value


def _rational(value, what):
    """A CANON_JSON_V1 rational: an integer or {"num", "den"} with den > 0."""
    if type(value) is int:
        return value
    if (type(value) is dict and set(value.keys()) == {"num", "den"}
            and type(value["num"]) is int and type(value["den"]) is int
            and value["den"] > 0):
        return value
    raise FilmError(f"{what} must be an integer or a canonical "
                    "{num, den} rational")


def _key_value(channel, value, what):
    if channel in {"translate", "scale"}:
        if type(value) is not list or len(value) != 2:
            raise FilmError(f"{what} needs an [x, y] value pair")
        for item in value:
            _rational(item, what)
        if channel == "scale" and (value[0] == 0 or value[1] == 0):
            raise FilmError(f"{what} scale cannot be zero")
        return value
    _rational(value, what)
    if channel == "opacity":
        number = value if type(value) is int \
            else value["num"] / value["den"]
        if not 0 <= number <= 1:
            raise FilmError(f"{what} opacity must stay inside [0, 1]")
    return value


def validate_transform_track(track, length, what="transform"):
    """One track's keyframed channels over [0, length) cut-local frames.

    Only step and linear curves exist in v1. The first key must sit on
    frame 0 so every evaluated frame has a declared value; after the last
    key the value holds. `track` may be null, meaning static identity.
    """
    if track is None:
        return None
    if type(track) is not dict or set(track.keys()) != {"channels"}:
        raise FilmError(f"{what} must be an object holding only 'channels'")
    channels = track["channels"]
    if type(channels) is not dict or not channels:
        raise FilmError(f"{what} channels must be a non-empty object")
    for name, channel in channels.items():
        if name not in TRANSFORM_CHANNELS:
            raise FilmError(f"{what} has unknown channel {name}; v1 "
                            f"supports {sorted(TRANSFORM_CHANNELS)}")
        if type(channel) is not dict or set(channel.keys()) != {"curve", "keys"}:
            raise FilmError(f"{what}.{name} must hold curve and keys")
        if channel["curve"] not in CURVES:
            raise FilmError(f"{what}.{name} curve {channel['curve']} is not "
                            f"in v1 {sorted(CURVES)}")
        keys = channel["keys"]
        if type(keys) is not list or not keys:
            raise FilmError(f"{what}.{name} needs a non-empty key list")
        frames = []
        for index, key in enumerate(keys):
            if type(key) is not dict or set(key.keys()) != {"frame", "value"}:
                raise FilmError(f"{what}.{name} key {index} must hold "
                                "frame and value")
            _int(key["frame"], f"{what}.{name} key {index} frame")
            if key["frame"] >= length:
                raise FilmError(f"{what}.{name} key {index} sits outside "
                                f"[0, {length})")
            _key_value(name, key["value"], f"{what}.{name} key {index} value")
            frames.append(key["frame"])
        if frames != sorted(set(frames)):
            raise FilmError(f"{what}.{name} keys must be strictly increasing")
        if frames[0] != 0:
            raise FilmError(f"{what}.{name} must declare a value on frame 0; "
                            "frames before the first key have no value")
    return track


def _check_assets(assets):
    if type(assets) is not list:
        raise FilmError("assets must be a list")
    for index, item in enumerate(assets):
        what = f"assets[{index}]"
        if type(item) is not dict or set(item.keys()) != ASSET_FIELDS:
            raise FilmError(f"{what} must hold {sorted(ASSET_FIELDS)}")
        _pin({k: item[k] for k in PIN_FIELDS}, what)
        if type(item["kind"]) is not str or not item["kind"]:
            raise FilmError(f"{what} kind must be a non-empty string")
        if item["role"] not in ASSET_ROLES:
            raise FilmError(f"{what} role must be one of {sorted(ASSET_ROLES)}")
        if item["layout"] is not None:
            _point(item["layout"], f"{what} layout")


def _check_events(events):
    if type(events) is not list:
        raise FilmError("events must be a list")
    for index, event in enumerate(events):
        if type(event) is not dict or set(event.keys()) != EVENT_FIELDS:
            raise FilmError(f"events[{index}] must hold "
                            f"{sorted(EVENT_FIELDS)}")
        if event["type"] not in EVENT_TYPES:
            raise FilmError(f"events[{index}] type must be one of "
                            f"{sorted(EVENT_TYPES)}")
        _int(event["frame"], f"events[{index}] frame")
        if type(event["note"]) is not str:
            raise FilmError(f"events[{index}] note must be a string")


def _check_keyposes(keyposes):
    if type(keyposes) is not list:
        raise FilmError("keyposes must be a list")
    for index, pose in enumerate(keyposes):
        if type(pose) is not dict or set(pose.keys()) != KEYPOSE_FIELDS:
            raise FilmError(f"keyposes[{index}] must hold "
                            f"{sorted(KEYPOSE_FIELDS)}")
        if type(pose["kind"]) is not str or not pose["kind"]:
            raise FilmError(f"keyposes[{index}] kind must be a non-empty string")
        _int(pose["frame"], f"keyposes[{index}] frame")
        if pose["ref"] is not None and type(pose["ref"]) is not str:
            raise FilmError(f"keyposes[{index}] ref must be a string or null")


def _check_segments(segments, length):
    if type(segments) is not list or not segments:
        raise FilmError("segments must be a non-empty list")
    cursor, paths = 0, []
    for index, segment in enumerate(segments):
        if type(segment) is not dict or set(segment.keys()) != SEGMENT_FIELDS:
            raise FilmError(f"segments[{index}] must hold "
                            f"{sorted(SEGMENT_FIELDS)}")
        start = _int(segment["start"], f"segments[{index}] start")
        end = _int(segment["end"], f"segments[{index}] end", 1)
        if end <= start or start != cursor:
            raise FilmError("Path segments must tile [0, length) contiguously")
        cursor = end
        path = segment["path"]
        if path in UNIMPLEMENTED_PATHS:
            raise FilmError(f"Path {path} is declared in the contract but not "
                            "implemented in v1; only path C composites")
        if path != "C":
            raise FilmError(f"Unknown production path {path}; v1 implements C")
        paths.append(path)
        capabilities = segment["capabilities"]
        if type(capabilities) is not list \
                or any(type(c) is not str for c in capabilities):
            raise FilmError(f"segments[{index}] capabilities must be a "
                            "list of strings")
        unsupported = [c for c in capabilities
                       if c in UNSUPPORTED_V1_CAPABILITIES]
        if unsupported:
            raise FilmError("Capabilities declared unsupported in v1: "
                            + ", ".join(sorted(unsupported)))
        unknown = [c for c in capabilities
                   if c not in NATIVE_C_CAPABILITIES]
        if unknown:
            raise FilmError("Unknown capabilities: "
                            + ", ".join(sorted(unknown)))
    if cursor != length:
        raise FilmError(f"Path segments must cover [0, {length}) exactly")


def _check_tracks(tracks, length):
    if type(tracks) is not dict or set(tracks.keys()) != {"camera", "layers"}:
        raise FilmError("tracks must hold camera and layers")
    camera = tracks["camera"]
    if type(camera) is not dict or set(camera.keys()) != CAMERA_FIELDS:
        raise FilmError(f"tracks.camera must hold {sorted(CAMERA_FIELDS)}")
    _point(camera["pivot"], "tracks.camera.pivot")
    validate_transform_track(camera["transform"], length, "tracks.camera")
    layers = tracks["layers"]
    if type(layers) is not dict:
        raise FilmError("tracks.layers must be an object keyed by layer id")
    info = {}
    for layer_id, track in layers.items():
        if type(layer_id) is not str or not layer_id:
            raise FilmError("tracks.layers keys must be non-empty layer ids")
        what = f"tracks.layers.{layer_id}"
        if type(track) is not dict or set(track.keys()) != LAYER_TRACK_FIELDS:
            raise FilmError(f"{what} must hold {sorted(LAYER_TRACK_FIELDS)}")
        drawings = track["drawings"]
        if type(drawings) is not list or not drawings:
            raise FilmError(f"{what}.drawings must be a non-empty pin list")
        for index, pin in enumerate(drawings):
            _pin(pin, f"{what}.drawings[{index}]")
        info[layer_id] = {"drawings": len(drawings)}
        schedules = track["exposure"]
        if type(schedules) is not list or not schedules:
            raise FilmError(f"{what}.exposure needs at least one schedule")
        for schedule in schedules:
            if type(schedule) is dict and schedule.get("track") != layer_id:
                raise FilmError(f"{what}.exposure schedules must be named "
                                f"after the layer ({layer_id})")
        validate_track(schedules, length)
        validate_transform_track(track["transform"], length,
                                 f"{what}.transform")
        if track["mask"] is not None:
            _pin(track["mask"], f"{what}.mask")
    return info


def validate_shot_plan(document, *, length, shot_id=None, canvas=None):
    """Structural contract of `animation_shot_plan` 1 over [0, length).

    `length` is the timeline entry's used range; `shot_id` and `canvas`, when
    given, must match the document's declarations. Cross-checks that need the
    registry (pins resolving, layer ids matching the RIG_SPEC, drawing tables)
    belong to `engine/compositor.composite_shot`.
    """
    check_document(document, PLAN_TYPE)
    if set(document.keys()) != PLAN_FIELDS:
        raise FilmError(f"animation_shot_plan must hold exactly "
                        f"{sorted(PLAN_FIELDS)}")
    _int(length, "length", 1)
    if not SHOT_ID.fullmatch(document["shot_id"]
                             if type(document["shot_id"]) is str else ""):
        raise FilmError("shot_id must be an S001-style identifier")
    if shot_id is not None and document["shot_id"] != shot_id:
        raise FilmError(f"Plan names {document['shot_id']}, not {shot_id}")
    _int(document["revision"], "plan revision", 1)
    if document["motion_intent"] not in MOTION_INTENTS:
        raise FilmError(f"motion_intent must be one of {sorted(MOTION_INTENTS)}")
    for key in ("story_role", "emotion_role"):
        if type(document[key]) is not str or not document[key].strip():
            raise FilmError(f"{key} must be a non-empty string")
    plan_canvas = document["canvas"]
    if (type(plan_canvas) is not dict
            or type(plan_canvas.get("width")) is not int
            or type(plan_canvas.get("height")) is not int
            or plan_canvas["width"] < 1 or plan_canvas["height"] < 1):
        raise FilmError("canvas must hold positive integer width/height")
    if canvas is not None and plan_canvas != \
            {"width": canvas["width"], "height": canvas["height"]}:
        raise FilmError("Plan canvas does not match the project format")
    background = document["background"]
    if (type(background) is not list or len(background) != 3
            or any(type(v) is not int or not 0 <= v <= 255
                   for v in background)):
        raise FilmError("background must be three 0..255 integers")
    _pin(document["rig"], "rig")
    _check_assets(document["assets"])
    _check_events(document["events"])
    _check_keyposes(document["keyposes"])
    _check_segments(document["segments"], length)
    layers = _check_tracks(document["tracks"], length)
    preparation = document["preparation"]
    if type(preparation) is not dict or set(preparation.keys()) != PREP_FIELDS:
        raise FilmError(f"preparation must hold {sorted(PREP_FIELDS)}")
    if (type(preparation["work"]) is not list
            or any(type(w) is not str for w in preparation["work"])):
        raise FilmError("preparation.work must be a list of strings")
    if type(preparation["creator"]) is not str \
            or not preparation["creator"].strip():
        raise FilmError("preparation.creator is required")
    if preparation["reviewer"] is not None \
            and type(preparation["reviewer"]) is not str:
        raise FilmError("preparation.reviewer must be a string or null")
    if type(preparation["revision_scope"]) is not str:
        raise FilmError("preparation.revision_scope must be a string")
    return {"shot_id": document["shot_id"], "layers": sorted(layers),
            "frames": length, "segments": len(document["segments"]),
            "motion_intent": document["motion_intent"]}


def plan_path(shot_id):
    return f"animation/shots/{shot_id}/plan.json"


def _cut_entry(timeline, shot_id, what="shot plan"):
    entries = [e for e in timeline["entries"] if e["shot_id"] == shot_id]
    if not entries:
        raise FilmError(f"{shot_id} has no timeline entry")
    if len(entries) > 1:
        raise FilmError(f"{shot_id} appears in {len(entries)} timeline "
                        f"entries; a v1 {what} supports one entry per shot")
    return entries[0]


def save_shot_plan(project, document):
    """Validate and store a shot plan; refuses legacy and malformed plans."""
    p = Path(project)
    with project_mutex(p):
        config = require_animation_profile(p)
        check_document(document, PLAN_TYPE)
        shot_id = document.get("shot_id")
        timeline = load_animation_timeline(p)
        entry = _cut_entry(timeline, shot_id, "shot plan")
        start, end = entry["used_source_range"]
        validate_shot_plan(document, length=end - start, shot_id=shot_id,
                           canvas=config["format"])
        target = safe_path(p, plan_path(shot_id))
        write_canon(target, document)
        return {"shot_id": shot_id, "path": plan_path(shot_id),
                "frames": end - start, "sha256": digest(target)}


def load_shot_plan(p, shot_id, *, length=None, canvas=None):
    """Read and re-validate the stored plan; stale or malformed plans fail."""
    p = Path(p)
    document = read_canon(safe_path(p, plan_path(shot_id)))
    validate_shot_plan(document, length=length if length is not None
                       else _plan_span(document), shot_id=shot_id,
                       canvas=canvas)
    return document


def _plan_span(document):
    """Coverage length declared by the plan's own segments."""
    segments = document.get("segments") if type(document) is dict else None
    if type(segments) is not list or not segments:
        raise FilmError("Plan declares no path segments")
    return max(int(s["end"]) for s in segments)
