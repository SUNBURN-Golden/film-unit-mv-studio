"""FRAME_ANIMATION_V1 asset registry, import and hash-bound resolver (ANIM-003).

External image sequences and RGBA layers are copied into the project as
independent bytes, hashed member by member and registered under
`manifest/animation_assets.json` (`document_type: animation_asset_registry`,
`schema_version: 1`, CANON_JSON_V1). A reference pins `asset_id` + `revision` +
`content_sha256`; changed content opens a new revision and never overwrites the
bytes of a pinned one.

Imports are drafts. They create no review, LOCK or production approval, and
run only on projects explicitly converted with `animation-init`. Original
source files are read and copied, never moved or deleted.
"""
from pathlib import Path
import hashlib
import io
import json
import os
import re
import tempfile

from PIL import Image

from .animation_schema import (SHOT_ID, canon_bytes, check_document, read_canon,
                               require_animation_profile, write_canon)
from .core import FilmError, digest, now, project_mutex, read, safe_path

REGISTRY_TYPE = "animation_asset_registry"
ASSET_ROOT = "animation/assets"
ASSET_ID = re.compile(r"A[0-9]{4,}")
SHA256 = re.compile(r"[0-9a-f]{64}")
IMPLEMENTED_KINDS = {"FRAME_SEQUENCE", "COMPOSITE_SEQUENCE", "LAYER_RGBA",
                     "MASK", "REPLACEMENT_DRAWING", "RIG_SPEC"}
# Kinds that can satisfy a shot's assigned sequence (contiguous frame members).
SEQUENCE_KINDS = {"FRAME_SEQUENCE", "COMPOSITE_SEQUENCE"}
ASSIGNABLE_KINDS = set(SEQUENCE_KINDS)
# Only lossless PNG is accepted for imported frames/layers in v1: re-encoding
# would risk silent alpha or pixel loss before any recipe records it.
ALLOWED_SUFFIX = {".png"}

REGISTRY_FIELDS = {"document_type", "schema_version", "assets", "assignments"}
RECORD_COMMON = {"asset_id", "revision", "kind", "provenance", "files",
                 "content_sha256", "coordinate_space", "dependencies",
                 "preparation", "acceptance"}
SEQUENCE_FIELDS = {"canvas", "exposure_recipe_ref", "composite_recipe_ref"}
LAYER_FIELDS = {"alpha", "canvas", "crop_origin", "pivot", "z_order"}
MASK_FIELDS = {"alpha", "canvas", "target", "region", "channel"}
REPLACEMENT_FIELDS = {"alpha", "canvas", "replaces", "frame", "rig"}
RIG_FIELDS = {"spec"}
KIND_EXTRA = {"FRAME_SEQUENCE": SEQUENCE_FIELDS,
              "COMPOSITE_SEQUENCE": SEQUENCE_FIELDS,
              "LAYER_RGBA": LAYER_FIELDS,
              "MASK": MASK_FIELDS,
              "REPLACEMENT_DRAWING": REPLACEMENT_FIELDS,
              "RIG_SPEC": RIG_FIELDS}
FILE_FIELDS = {"relative_name", "sha256", "byte_length", "frame_index",
               "source_name"}
ASSIGNMENT_FIELDS = {"asset_id", "revision", "content_sha256", "kind"}
PIN_FIELDS = {"asset_id", "revision", "content_sha256"}
MASK_CHANNELS = {"ALPHA", "LUMINANCE"}
RIG_SPEC_KEYS = {"layers", "children", "reference_points",
                 "occlusion_order"}
RIG_LAYER_KEYS = {"id", "parent", "asset", "z_order", "transforms"}


def _empty_registry():
    return {"document_type": REGISTRY_TYPE, "schema_version": 1,
            "assets": {}, "assignments": {}}


def _registry_path(p):
    animation = read(Path(p) / "project.yaml").get("animation") or {}
    return safe_path(p, animation.get("assets", "manifest/animation_assets.json"))


def _int(value, what, minimum=0):
    if type(value) is not int or value < minimum:
        raise FilmError(f"{what} must be an integer >= {minimum}")
    return value


def _sha(value, what):
    if type(value) is not str or not SHA256.fullmatch(value):
        raise FilmError(f"{what} must be a lowercase SHA-256")
    return value


def _point(value, what):
    if (type(value) is not list or len(value) != 2
            or any(type(v) is not int for v in value)):
        raise FilmError(f"{what} must be an [x, y] integer pair")
    return value


def _pin(value, what):
    """An asset pin: asset_id + revision + content_sha256, nothing else."""
    if type(value) is not dict or set(value.keys()) != PIN_FIELDS:
        raise FilmError(f"{what} must be an asset pin "
                        "{asset_id, revision, content_sha256}")
    if not ASSET_ID.fullmatch(value["asset_id"]
                              if type(value["asset_id"]) is str else ""):
        raise FilmError(f"{what} asset_id must be an A0001-style identifier")
    _int(value["revision"], f"{what} revision", 1)
    _sha(value["content_sha256"], f"{what} content_sha256")
    return value


def _canvas(value, what="canvas"):
    if (type(value) is not dict or type(value.get("width")) is not int
            or type(value.get("height")) is not int
            or value["width"] < 1 or value["height"] < 1):
        raise FilmError(f"{what} must hold positive integer width/height")
    return value


def _content_fields(record):
    """The hashable identity of a revision: member hashes and content geometry.

    Locators, provenance, preparation state, review marks and timestamps are
    deliberately excluded from the content digest.
    """
    members = [{"sha256": f["sha256"], "byte_length": f["byte_length"],
                **({"frame_index": f["frame_index"]} if "frame_index" in f else {})}
               for f in record["files"]]
    content = {"kind": record["kind"], "members": members,
               "coordinate_space": record["coordinate_space"],
               "canvas": record.get("canvas")}
    if record["kind"] in SEQUENCE_KINDS:
        content.update({"exposure_recipe_ref": record["exposure_recipe_ref"],
                        "composite_recipe_ref": record["composite_recipe_ref"]})
    elif record["kind"] == "LAYER_RGBA":
        content.update({"alpha": {"present": record["alpha"]["present"]},
                        "crop_origin": record["crop_origin"],
                        "pivot": record["pivot"], "z_order": record["z_order"]})
    elif record["kind"] == "MASK":
        content.update({"alpha": {"present": record["alpha"]["present"]},
                        "target": record["target"], "region": record["region"],
                        "channel": record["channel"]})
    elif record["kind"] == "REPLACEMENT_DRAWING":
        content.update({"alpha": {"present": record["alpha"]["present"]},
                        "replaces": record["replaces"],
                        "frame": record["frame"], "rig": record["rig"]})
    elif record["kind"] == "RIG_SPEC":
        content["spec"] = record["spec"]
    return content


def content_digest(record):
    return hashlib.sha256(canon_bytes(_content_fields(record))).hexdigest()


def _check_files(files, require_frames, kind="sequence"):
    if type(files) is not list or not files:
        raise FilmError("Asset record must list at least one file")
    seen = set()
    for f in files:
        if type(f) is not dict or set(f.keys()) - FILE_FIELDS:
            raise FilmError("Malformed files[] entry")
        _sha(f.get("sha256"), "files[].sha256")
        _int(f.get("byte_length"), "files[].byte_length", 1)
        if type(f.get("relative_name")) is not str or not f["relative_name"]:
            raise FilmError("files[].relative_name must be a non-empty string")
        if require_frames:
            index = _int(f.get("frame_index"), "files[].frame_index")
            if index in seen:
                raise FilmError("Duplicate frame_index in files[]")
            seen.add(index)
    if require_frames and seen != set(range(len(files))):
        raise FilmError(f"{kind} frame_index values must be 0..N-1 without gaps")


def validate_rig_spec(spec):
    """Structural contract of a RIG_SPEC body (schema §4).

    The spec records the layer hierarchy (부모/자식), reference points
    (기준점), each layer's allowed transforms (허용 변형) and the explicit
    occlusion order (가림 순서). Registry cross-checks — pins resolving to
    LAYER_RGBA revisions, z_order agreeing with occlusion order — happen in
    `validate_registry`'s cross-reference pass.
    """
    if type(spec) is not dict or set(spec.keys()) != RIG_SPEC_KEYS:
        raise FilmError(f"RIG_SPEC spec must hold {sorted(RIG_SPEC_KEYS)}")
    layers = spec["layers"]
    if type(layers) is not list or not layers:
        raise FilmError("RIG_SPEC layers must be a non-empty list")
    ids = []
    for index, layer in enumerate(layers):
        what = f"rig layer {index}"
        if type(layer) is not dict or set(layer.keys()) != RIG_LAYER_KEYS:
            raise FilmError(f"{what} must hold {sorted(RIG_LAYER_KEYS)}")
        if type(layer["id"]) is not str or not layer["id"]:
            raise FilmError(f"{what} id must be a non-empty string")
        ids.append(layer["id"])
        if layer["parent"] is not None and type(layer["parent"]) is not str:
            raise FilmError(f"{what} parent must be a layer id or null")
        _pin(layer["asset"], f"{what} asset")
        _int(layer["z_order"], f"{what} z_order")
        from .motion_plan import TRANSFORM_CHANNELS
        allowed = layer["transforms"]
        if (type(allowed) is not list
                or any(name not in TRANSFORM_CHANNELS for name in allowed)):
            raise FilmError(f"{what} transforms must name v1 channels: "
                            f"{sorted(TRANSFORM_CHANNELS)}")
    if len(ids) != len(set(ids)):
        raise FilmError("RIG_SPEC layer ids must be unique")
    for layer in layers:
        if layer["parent"] is not None and layer["parent"] not in ids:
            raise FilmError(f"rig layer {layer['id']} names an unknown "
                            f"parent {layer['parent']}")
    # Parent links form a forest; a cycle would make world transforms
    # unresolvable.
    for layer in layers:
        seen = set()
        cursor = layer["id"]
        while True:
            parent = next(l["parent"] for l in layers if l["id"] == cursor)
            if parent is None:
                break
            if parent in seen:
                raise FilmError("RIG_SPEC hierarchy has a parent cycle")
            seen.add(parent)
            cursor = parent
    children = spec["children"]
    derived = {lid: sorted(l["id"] for l in layers if l["parent"] == lid)
               for lid in ids}
    if type(children) is not dict or set(children.keys()) != set(ids):
        raise FilmError("RIG_SPEC children must map every layer id")
    for lid in ids:
        if sorted(children[lid]) != derived[lid]:
            raise FilmError("RIG_SPEC children does not match the parent links")
    points = spec["reference_points"]
    if type(points) is not dict:
        raise FilmError("RIG_SPEC reference_points must be an object")
    for name, point in points.items():
        if type(name) is not str or not name:
            raise FilmError("RIG_SPEC reference point names must be strings")
        if (type(point) is not dict
                or set(point.keys()) != {"layer", "point"}
                or point["layer"] not in ids):
            raise FilmError(f"reference point {name} must name a rig layer")
        _point(point["point"], f"reference point {name}")
    order = spec["occlusion_order"]
    if (type(order) is not list or sorted(order) != sorted(ids)):
        raise FilmError("RIG_SPEC occlusion_order must be a permutation of "
                        "the layer ids, back to front")
    if len({l["z_order"] for l in layers}) != len(layers):
        raise FilmError("RIG_SPEC layer z_order values must be unique")
    return spec


def _validate_record(record):
    if type(record) is not dict:
        raise FilmError("Asset revision must be an object")
    if type(record.get("asset_id")) is not str or not ASSET_ID.fullmatch(record["asset_id"]):
        raise FilmError("asset_id must be an A0001-style identifier")
    _int(record.get("revision"), "revision", 1)
    kind = record.get("kind")
    if kind not in IMPLEMENTED_KINDS:
        raise FilmError(f"Unsupported asset kind: {kind}")
    extra = KIND_EXTRA[kind]
    if set(record.keys()) - RECORD_COMMON - extra:
        raise FilmError(f"Unknown fields on {kind} record")
    missing = (RECORD_COMMON | extra) - record.keys()
    if missing:
        raise FilmError(f"{kind} record missing fields: {sorted(missing)}")
    _check_files(record["files"], require_frames=kind in SEQUENCE_KINDS,
                 kind=kind)
    _sha(record["content_sha256"], "content_sha256")
    if content_digest(record) != record["content_sha256"]:
        raise FilmError("content_sha256 does not match the recorded content fields")
    if type(record["dependencies"]) is not list:
        raise FilmError("dependencies must be a list")
    if type(record["provenance"]) is not dict or type(record["preparation"]) is not dict:
        raise FilmError("provenance and preparation must be objects")
    if record["acceptance"].get("state") != "DRAFT":
        raise FilmError("Imported assets are drafts; no approval state is written by import")
    if kind != "RIG_SPEC":
        _canvas(record["canvas"])
    if kind == "LAYER_RGBA":
        if record["alpha"].get("present") is not True:
            raise FilmError("LAYER_RGBA must record a preserved alpha channel")
        _point(record["crop_origin"], "crop_origin")
        _point(record["pivot"], "pivot")
        _int(record["z_order"], "z_order")
    elif kind == "MASK":
        _pin(record["target"], "MASK target")
        region = record["region"]
        if region is not None:
            if (type(region) is not list or len(region) != 4
                    or any(type(v) is not int for v in region)
                    or region[2] < 1 or region[3] < 1):
                raise FilmError("MASK region must be null or "
                                "[x, y, width, height] with positive extent")
        if record["channel"] not in MASK_CHANNELS:
            raise FilmError(f"MASK channel must be one of {sorted(MASK_CHANNELS)}")
        if type(record["alpha"]) is not dict \
                or type(record["alpha"].get("present")) is not bool:
            raise FilmError("MASK alpha must record a present boolean")
        if record["channel"] == "ALPHA" \
                and record["alpha"]["present"] is not True:
            raise FilmError("An ALPHA-channel MASK needs a preserved alpha channel")
    elif kind == "REPLACEMENT_DRAWING":
        if record["alpha"].get("present") is not True:
            raise FilmError("REPLACEMENT_DRAWING must record a preserved "
                            "alpha channel")
        _pin(record["replaces"], "REPLACEMENT_DRAWING replaces")
        _int(record["frame"], "REPLACEMENT_DRAWING frame")
        rig = record["rig"]
        if type(rig) is not dict or set(rig.keys()) != {"pivot"}:
            raise FilmError("REPLACEMENT_DRAWING rig must hold only 'pivot'")
        _point(rig["pivot"], "REPLACEMENT_DRAWING rig.pivot")
    elif kind == "RIG_SPEC":
        validate_rig_spec(record["spec"])
    elif kind == "COMPOSITE_SEQUENCE":
        if type(record["composite_recipe_ref"]) is not str \
                or not SHA256.fullmatch(record["composite_recipe_ref"]):
            raise FilmError("COMPOSITE_SEQUENCE needs the composite recipe "
                            "SHA-256 in composite_recipe_ref")
    return record


def validate_registry(document):
    """Structural contract of `manifest/animation_assets.json` (schema §4)."""
    check_document(document, REGISTRY_TYPE)
    if set(document.keys()) - REGISTRY_FIELDS:
        raise FilmError("Unknown animation_asset_registry fields")
    assets, assignments = document["assets"], document["assignments"]
    if type(assets) is not dict or type(assignments) is not dict:
        raise FilmError("assets and assignments must be objects")
    for asset_id, entry in assets.items():
        if not ASSET_ID.fullmatch(asset_id if type(asset_id) is str else ""):
            raise FilmError("Asset keys must be A0001-style identifiers")
        if type(entry) is not dict or set(entry.keys()) - {"current_revision", "revisions"}:
            raise FilmError("Malformed asset entry")
        _int(entry.get("current_revision"), "current_revision", 1)
        revisions = entry.get("revisions")
        if type(revisions) is not dict or str(entry["current_revision"]) not in revisions:
            raise FilmError("current_revision must name an existing revision")
        for label, record in revisions.items():
            _validate_record(record)
            if record["asset_id"] != asset_id or str(record["revision"]) != label:
                raise FilmError("Revision record does not match its registry key")
    for shot_id, pin in assignments.items():
        if not SHOT_ID.fullmatch(shot_id if type(shot_id) is str else ""):
            raise FilmError("Assignment keys must be shot identifiers")
        if type(pin) is not dict or set(pin.keys()) != ASSIGNMENT_FIELDS:
            raise FilmError("Malformed assignment pin")
        if pin["kind"] not in ASSIGNABLE_KINDS:
            raise FilmError(f"Unsupported assigned kind: {pin['kind']}")
        if not ASSET_ID.fullmatch(pin["asset_id"] if type(pin["asset_id"]) is str else ""):
            raise FilmError("Assignment asset_id must be an A0001-style identifier")
        _int(pin["revision"], "assignment revision", 1)
        _sha(pin["content_sha256"], "assignment content_sha256")
    _check_cross_references(assets)
    return document


def _doc_pin_record(assets, pin, what):
    """Resolve a pin inside one registry document; every leg must verify."""
    entry = assets.get(pin["asset_id"])
    if entry is None:
        raise FilmError(f"{what} references unknown asset {pin['asset_id']}")
    record = entry["revisions"].get(str(pin["revision"]))
    if record is None:
        raise FilmError(f"{what} references missing revision "
                        f"{pin['asset_id']} r{pin['revision']}")
    if record["content_sha256"] != pin["content_sha256"]:
        raise FilmError(f"{what} pin does not match "
                        f"{pin['asset_id']} r{pin['revision']} content")
    return record


def _check_cross_references(assets):
    """Asset-to-asset pins: mask targets, replacement targets, rig layers.

    A record's own fields are already validated; this pass rejects pins that
    dangle, lie about the target's bytes, or contradict the geometry the
    target revision pins.
    """
    for entry in assets.values():
        for record in entry["revisions"].values():
            kind = record["kind"]
            if kind == "MASK":
                target = _doc_pin_record(assets, record["target"],
                                         "MASK target")
                if target["kind"] != "LAYER_RGBA":
                    raise FilmError("MASK target must be a LAYER_RGBA revision")
                region = record["region"]
                if region is None:
                    if record["canvas"] != target["canvas"]:
                        raise FilmError("A full-canvas MASK must match its "
                                        "target layer's canvas")
                else:
                    x, y, w, h = region
                    bounds = target["canvas"]
                    if (x < 0 or y < 0 or x + w > bounds["width"]
                            or y + h > bounds["height"]):
                        raise FilmError("MASK region must stay inside its "
                                        "target layer's canvas")
                    if record["canvas"] != {"width": w, "height": h}:
                        raise FilmError("MASK canvas must equal its "
                                        "declared region extent")
            elif kind == "REPLACEMENT_DRAWING":
                target = _doc_pin_record(assets, record["replaces"],
                                         "REPLACEMENT_DRAWING replaces")
                if target["kind"] != "LAYER_RGBA":
                    raise FilmError("REPLACEMENT_DRAWING replaces must be a "
                                    "LAYER_RGBA revision")
                if record["canvas"] != target["canvas"]:
                    raise FilmError("A replacement drawing must share its "
                                    "target layer's canvas")
                if record["rig"]["pivot"] != target["pivot"]:
                    raise FilmError("A replacement drawing's rig pivot must "
                                    "match its target layer's pivot")
            elif kind == "RIG_SPEC":
                spec = record["spec"]
                by_z = sorted(spec["layers"], key=lambda l: l["z_order"])
                if [l["id"] for l in by_z] != spec["occlusion_order"]:
                    raise FilmError("RIG_SPEC occlusion_order must agree "
                                    "with the layer z_order values")
                for layer in spec["layers"]:
                    target = _doc_pin_record(assets, layer["asset"],
                                             f"rig layer {layer['id']} asset")
                    if target["kind"] != "LAYER_RGBA":
                        raise FilmError(f"rig layer {layer['id']} must pin "
                                        "a LAYER_RGBA revision")
                    if target["z_order"] != layer["z_order"]:
                        raise FilmError(f"rig layer {layer['id']} z_order "
                                        "must match its pinned layer asset")


def load_registry(p):
    path = _registry_path(p)
    if not path.exists():
        return _empty_registry()
    return validate_registry(read_canon(path))


def save_registry(p, document):
    validate_registry(document)
    write_canon(_registry_path(p), document)
    return document


def _read_source(path):
    """Read external bytes for import; symlinks and non-files are refused."""
    if type(path) in (bytes, bytearray):
        return bytes(path)
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise FilmError(f"Not a regular file: {path}")
    return path.read_bytes()


def _decode_png(data, name):
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if im.format != "PNG":
                raise FilmError(f"Only PNG is accepted for animation assets: {name}")
            return im.size, im.mode
    except FilmError:
        raise
    except Exception as e:
        raise FilmError(f"Unreadable or corrupt PNG: {name}") from e


def _inside(root, relative):
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise FilmError(f"Disallowed path in index: {relative}")
    candidate = root
    for part in Path(relative).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise FilmError(f"Index member passes through a symlink: {relative}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise FilmError(f"Path escapes the index directory: {relative}")
    return resolved


def _index_members(index_path):
    """An index must name an explicit order and a hash for every member."""
    path = Path(index_path)
    if not path.is_file():
        raise FilmError(f"Missing index file: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise FilmError(f"Invalid index JSON: {path}") from e
    frames = document.get("frames") if type(document) is dict else None
    if type(frames) is not list or not frames:
        raise FilmError("Index must list a non-empty frames array")
    root = path.parent.resolve()
    members = []
    for position, item in enumerate(frames):
        if type(item) is not dict or set(item.keys()) - {"file", "sha256"}:
            raise FilmError("Index members carry only file and sha256")
        name = item.get("file")
        if type(name) is not str or not name:
            raise FilmError("Index member file must be a relative path")
        if Path(name).suffix.lower() not in ALLOWED_SUFFIX:
            raise FilmError(f"Unsupported image type in index: {name}")
        source = _inside(root, name)
        data = _read_source(source)
        sha = hashlib.sha256(data).hexdigest()
        if sha != item.get("sha256"):
            raise FilmError(f"Index hash mismatch: {name}")
        members.append({"source": source, "sha256": sha, "byte_length": len(data),
                        "source_name": name})
    return members


def _folder_members(folder):
    """Folder import orders members lexically by filename."""
    root = Path(folder)
    if not root.is_dir():
        raise FilmError(f"Missing folder: {folder}")
    candidates = sorted((f for f in root.iterdir()
                         if f.suffix.lower() in ALLOWED_SUFFIX),
                        key=lambda f: f.name)
    if any(f.is_symlink() for f in candidates):
        raise FilmError("Folder contains a symlinked member; refusing import")
    sources = [f for f in candidates if f.is_file()]
    if not sources:
        raise FilmError(f"Folder has no PNG files: {folder}")
    members = []
    for source in sources:
        data = _read_source(source)
        members.append({"source": source, "sha256": hashlib.sha256(data).hexdigest(),
                        "byte_length": len(data), "source_name": source.name})
    return members


def _sequence_canvas(members):
    """Decode every member once; a frame sequence needs one uniform canvas."""
    canvas = None
    for member in members:
        size, _mode = _decode_png(_read_source(member["source"]),
                                  member["source_name"])
        if canvas is None:
            canvas = size
        elif size != canvas:
            raise FilmError("Frame sequence members must share one canvas size")
    return {"width": canvas[0], "height": canvas[1]}


def _coordinate_space(canvas):
    return {"system": "ASSET_PIXELS", "origin": "TOP_LEFT",
            "width": canvas["width"], "height": canvas["height"]}


def _allocate_id(registry):
    used = {int(a[1:]) for a in registry["assets"]}
    number = 1
    while number in used:
        number += 1
    return f"A{number:04d}"


def _store_member(p, relative, data, expected_sha):
    """Exclusive independent copy inside the project; verify after writing."""
    dest = safe_path(p, relative)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        if dest.is_file() and not dest.is_symlink() and digest(dest) == expected_sha:
            return  # identical bytes already stored; reuse them
        raise FilmError(f"Stored member does not match this revision: {relative}")
    fd, temp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temp, dest)
        if digest(dest) != expected_sha:
            raise FilmError("Copied member changed during import")
    finally:
        Path(temp).unlink(missing_ok=True)


def _commit_revision(p, registry, asset_id, kind, staged, record):
    """Store member bytes under a fresh revision, then register the record.

    If an existing revision already pins the same content it is reused and no
    bytes are rewritten; content that differs opens a new revision and old
    revision bytes stay untouched.
    """
    asset = registry["assets"].get(asset_id) if asset_id else None
    if asset is not None:
        if next(iter(asset["revisions"].values()))["kind"] != kind:
            raise FilmError(f"{asset_id} is already registered as a different kind")
        for existing in asset["revisions"].values():
            if content_digest(existing) == content_digest(record):
                # An identical content digest does not prove the stored bytes
                # survived; re-verify before reporting the revision as reused.
                _verify_member_bytes(p, existing)
                asset["current_revision"] = existing["revision"]
                return existing, False
        revision = max(int(r) for r in asset["revisions"]) + 1
    else:
        asset_id = asset_id or _allocate_id(registry)
        asset = {"current_revision": 0, "revisions": {}}
        revision = 1
    record["asset_id"], record["revision"] = asset_id, revision
    files = []
    for position, member in enumerate(staged):
        relative = f"{ASSET_ROOT}/{asset_id}/r{revision}/{member['stored_name']}"
        data = _read_source(member["source"])
        if hashlib.sha256(data).hexdigest() != member["sha256"]:
            raise FilmError(f"Source changed during import: {member['source_name']}")
        _store_member(p, relative, data, member["sha256"])
        files.append({"relative_name": relative, "sha256": member["sha256"],
                      "byte_length": len(data),
                      "source_name": member["source_name"],
                      **({"frame_index": position}
                         if record["kind"] in SEQUENCE_KINDS else {})})
    record["files"] = files
    record["content_sha256"] = content_digest(record)
    _validate_record(record)
    asset["revisions"][str(revision)] = record
    asset["current_revision"] = revision
    registry["assets"][asset_id] = asset
    return record, True


def import_frame_sequence(project, shot_id, *, index=None, folder=None,
                          asset_id=None, note=""):
    """Import an externally produced frame sequence for one shot, as a draft.

    Exactly one of `index` (a JSON file with an explicit order and per-member
    SHA-256) or `folder` (PNG members ordered lexically by filename) supplies
    the members. The shot's assignment is pinned to the resulting revision.
    """
    p = Path(project)
    with project_mutex(p):
        require_animation_profile(p)
        if not SHOT_ID.fullmatch(shot_id if type(shot_id) is str else ""):
            raise FilmError("shot_id must be an S001-style identifier")
        shots = read(p / "manifest/shots.json")
        if not any(s["id"] == shot_id for s in shots):
            raise FilmError(f"Unknown shot: {shot_id}")
        if (index is None) == (folder is None):
            raise FilmError("Provide exactly one of index or folder")
        if asset_id is not None and not ASSET_ID.fullmatch(asset_id):
            raise FilmError("asset_id must be an A0001-style identifier")
        members = _index_members(index) if index is not None else _folder_members(folder)
        canvas = _sequence_canvas(members)
        staged = [{"stored_name": f"f{position:06d}.png", **member}
                  for position, member in enumerate(members)]
        registry = load_registry(p)
        if asset_id is None:
            pin = registry["assignments"].get(shot_id)
            asset_id = pin["asset_id"] if pin else None
        record = {"asset_id": asset_id or "A0000", "revision": 0,
                  "kind": "FRAME_SEQUENCE",
                  "provenance": {"type": "EXTERNAL_IMPORT",
                                 "via": "index" if index is not None else "folder",
                                 "source": str(index or folder),
                                 "imported_at": now(), "note": note.strip()},
                  "files": [], "coordinate_space": _coordinate_space(canvas),
                  "dependencies": [],
                  "preparation": {"state": "IMPORTED_DRAFT",
                                  "checks": {"member_hashes_verified": True,
                                             "decoded_png": True,
                                             "uniform_canvas": True},
                                  "not_verified": ["human_review", "motion_approval"]},
                  "acceptance": {"state": "DRAFT"}, "canvas": canvas,
                  "exposure_recipe_ref": None, "composite_recipe_ref": None}
        record["files"] = [{"relative_name": "", "sha256": m["sha256"],
                            "byte_length": m["byte_length"], "frame_index": i}
                           for i, m in enumerate(members)]
        record["content_sha256"] = content_digest(record)
        record, created = _commit_revision(p, registry, asset_id,
                                           "FRAME_SEQUENCE", staged, record)
        registry["assignments"][shot_id] = {"asset_id": record["asset_id"],
                                            "revision": record["revision"],
                                            "content_sha256": record["content_sha256"],
                                            "kind": "FRAME_SEQUENCE"}
        save_registry(p, registry)
        # Adopting this revision for the shot pins it in the edit document.
        _adopt_revision(p, shot_id, record["revision"])
        return {"asset_id": record["asset_id"], "revision": record["revision"],
                "content_sha256": record["content_sha256"],
                "frames": len(record["files"]), "assigned_to": shot_id,
                "state": "DRAFT", "new_revision": created}


def _adopt_revision(p, shot_id, revision):
    """Pin the adopted sequence revision into the shot's timeline entries."""
    timeline_path = safe_path(
        p, (read(p / "project.yaml").get("animation") or {})
        .get("timeline", "timeline/edit.json"))
    if timeline_path.exists():
        timeline = read_canon(timeline_path)
        for entry in timeline["entries"]:
            if entry["shot_id"] == shot_id:
                entry["sequence_revision"] = revision
        write_canon(timeline_path, timeline)


def import_layer_rgba(project, file, *, asset_id=None, pivot=(0, 0),
                      crop_origin=(0, 0), z_order=0, note=""):
    """Import one RGBA layer on its own path. Alpha, crop origin and pivot are
    preserved on the stored bytes — the image is never flattened or re-encoded.
    """
    p = Path(project)
    with project_mutex(p):
        require_animation_profile(p)
        if asset_id is not None and not ASSET_ID.fullmatch(asset_id):
            raise FilmError("asset_id must be an A0001-style identifier")
        _point(list(pivot), "pivot")
        _point(list(crop_origin), "crop_origin")
        _int(z_order, "z_order")
        data = _read_source(file)
        size, mode = _decode_png(data, Path(file).name)
        if "A" not in mode:
            raise FilmError("LAYER_RGBA requires a source alpha channel; refusing to flatten")
        with Image.open(io.BytesIO(data)) as im:
            alpha_min, alpha_max = im.getchannel("A").getextrema()
        canvas = {"width": size[0], "height": size[1]}
        staged = [{"stored_name": "layer.png", "source": file,
                   "sha256": hashlib.sha256(data).hexdigest(),
                   "source_name": Path(file).name}]
        registry = load_registry(p)
        record = {"asset_id": asset_id or "A0000", "revision": 0,
                  "kind": "LAYER_RGBA",
                  "provenance": {"type": "EXTERNAL_IMPORT", "via": "file",
                                 "source": str(file), "imported_at": now(),
                                 "note": note.strip()},
                  "files": [{"relative_name": "", "sha256": staged[0]["sha256"],
                             "byte_length": len(data)}],
                  "coordinate_space": _coordinate_space(canvas),
                  "dependencies": [],
                  "preparation": {"state": "IMPORTED_DRAFT",
                                  "checks": {"member_hashes_verified": True,
                                             "decoded_png": True,
                                             "alpha_channel": True},
                                  "not_verified": ["human_review"]},
                  "acceptance": {"state": "DRAFT"},
                  "alpha": {"present": True, "mode": mode,
                            "min": alpha_min, "max": alpha_max},
                  "canvas": canvas, "crop_origin": list(crop_origin),
                  "pivot": list(pivot), "z_order": z_order}
        record["content_sha256"] = content_digest(record)
        record, created = _commit_revision(p, registry, asset_id,
                                           "LAYER_RGBA", staged, record)
        save_registry(p, registry)
        return {"asset_id": record["asset_id"], "revision": record["revision"],
                "content_sha256": record["content_sha256"], "kind": "LAYER_RGBA",
                "state": "DRAFT", "new_revision": created}


def import_mask(project, file, *, target, region=None, channel="LUMINANCE",
                asset_id=None, note=""):
    """Import a MASK asset: a single-channel matte pinned to a layer revision.

    `target` pins the LAYER_RGBA revision the mask clips; `region` is the
    [x, y, w, h] extent of the target canvas the mask covers (null = the
    whole target canvas), and `channel` names the meaning of its samples —
    "ALPHA" requires a real alpha channel, "LUMINANCE" reads luma.
    """
    p = Path(project)
    with project_mutex(p):
        require_animation_profile(p)
        if asset_id is not None and not ASSET_ID.fullmatch(asset_id):
            raise FilmError("asset_id must be an A0001-style identifier")
        _pin(target, "MASK target")
        if channel not in MASK_CHANNELS:
            raise FilmError(f"MASK channel must be one of {sorted(MASK_CHANNELS)}")
        if region is not None:
            if (type(region) is not list or len(region) != 4
                    or any(type(v) is not int for v in region)
                    or region[2] < 1 or region[3] < 1):
                raise FilmError("MASK region must be null or "
                                "[x, y, width, height] with positive extent")
        registry = load_registry(p)
        target_rec = _resolve_pin(registry, target["asset_id"],
                                  target["revision"], target["content_sha256"])
        if target_rec["kind"] != "LAYER_RGBA":
            raise FilmError("MASK target must be a LAYER_RGBA revision")
        data = _read_source(file)
        size, mode = _decode_png(data, Path(file).name)
        canvas = {"width": size[0], "height": size[1]}
        bounds = target_rec["canvas"]
        if region is None:
            if canvas != bounds:
                raise FilmError("A full-canvas MASK must match its target "
                                "layer's canvas")
        else:
            x, y, w, h = region
            if (x < 0 or y < 0 or x + w > bounds["width"]
                    or y + h > bounds["height"]):
                raise FilmError("MASK region must stay inside its target "
                                "layer's canvas")
            if canvas != {"width": w, "height": h}:
                raise FilmError("MASK canvas must equal its declared "
                                "region extent")
        if channel == "ALPHA" and "A" not in mode:
            raise FilmError("An ALPHA-channel MASK needs a source alpha "
                            "channel")
        staged = [{"stored_name": "mask.png", "source": file,
                   "sha256": hashlib.sha256(data).hexdigest(),
                   "byte_length": len(data), "source_name": Path(file).name}]
        record = {"asset_id": asset_id or "A0000", "revision": 0,
                  "kind": "MASK",
                  "provenance": {"type": "EXTERNAL_IMPORT", "via": "file",
                                 "source": str(file), "imported_at": now(),
                                 "note": note.strip()},
                  "files": [{"relative_name": "", "sha256": staged[0]["sha256"],
                             "byte_length": len(data)}],
                  "coordinate_space": _coordinate_space(canvas),
                  "dependencies": [],
                  "preparation": {"state": "IMPORTED_DRAFT",
                                  "checks": {"member_hashes_verified": True,
                                             "decoded_png": True},
                                  "not_verified": ["human_review"]},
                  "acceptance": {"state": "DRAFT"},
                  "alpha": {"present": "A" in mode, "mode": mode},
                  "canvas": canvas, "target": dict(target),
                  "region": list(region) if region is not None else None,
                  "channel": channel}
        record["content_sha256"] = content_digest(record)
        record, created = _commit_revision(p, registry, asset_id,
                                           "MASK", staged, record)
        save_registry(p, registry)
        return {"asset_id": record["asset_id"], "revision": record["revision"],
                "content_sha256": record["content_sha256"], "kind": "MASK",
                "state": "DRAFT", "new_revision": created}


def import_replacement_drawing(project, file, *, replaces, frame, pivot,
                               asset_id=None, note=""):
    """Import a REPLACEMENT_DRAWING: alternate art for one LAYER_RGBA.

    `replaces` pins the base layer revision; `frame` declares the cut-local
    frame at which the replacement first appears (the shot plan's exposure
    must agree); `pivot` is the rig anchor the drawing was authored for and
    must equal the target layer's pivot — a mismatched canvas or pivot is
    not a compatible replacement.
    """
    p = Path(project)
    with project_mutex(p):
        require_animation_profile(p)
        if asset_id is not None and not ASSET_ID.fullmatch(asset_id):
            raise FilmError("asset_id must be an A0001-style identifier")
        _pin(replaces, "REPLACEMENT_DRAWING replaces")
        _int(frame, "REPLACEMENT_DRAWING frame")
        _point(list(pivot), "REPLACEMENT_DRAWING pivot")
        registry = load_registry(p)
        target = _resolve_pin(registry, replaces["asset_id"],
                              replaces["revision"], replaces["content_sha256"])
        if target["kind"] != "LAYER_RGBA":
            raise FilmError("REPLACEMENT_DRAWING replaces must be a "
                            "LAYER_RGBA revision")
        if list(pivot) != target["pivot"]:
            raise FilmError("A replacement drawing's rig pivot must match "
                            "its target layer's pivot")
        data = _read_source(file)
        size, mode = _decode_png(data, Path(file).name)
        if "A" not in mode:
            raise FilmError("REPLACEMENT_DRAWING requires a source alpha "
                            "channel; refusing to flatten")
        canvas = {"width": size[0], "height": size[1]}
        if canvas != target["canvas"]:
            raise FilmError("A replacement drawing must share its target "
                            "layer's canvas")
        with Image.open(io.BytesIO(data)) as im:
            alpha_min, alpha_max = im.getchannel("A").getextrema()
        staged = [{"stored_name": "drawing.png", "source": file,
                   "sha256": hashlib.sha256(data).hexdigest(),
                   "byte_length": len(data), "source_name": Path(file).name}]
        record = {"asset_id": asset_id or "A0000", "revision": 0,
                  "kind": "REPLACEMENT_DRAWING",
                  "provenance": {"type": "EXTERNAL_IMPORT", "via": "file",
                                 "source": str(file), "imported_at": now(),
                                 "note": note.strip()},
                  "files": [{"relative_name": "", "sha256": staged[0]["sha256"],
                             "byte_length": len(data)}],
                  "coordinate_space": _coordinate_space(canvas),
                  "dependencies": [],
                  "preparation": {"state": "IMPORTED_DRAFT",
                                  "checks": {"member_hashes_verified": True,
                                             "decoded_png": True,
                                             "alpha_channel": True},
                                  "not_verified": ["human_review"]},
                  "acceptance": {"state": "DRAFT"},
                  "alpha": {"present": True, "mode": mode,
                            "min": alpha_min, "max": alpha_max},
                  "canvas": canvas, "replaces": dict(replaces),
                  "frame": frame, "rig": {"pivot": list(pivot)}}
        record["content_sha256"] = content_digest(record)
        record, created = _commit_revision(p, registry, asset_id,
                                           "REPLACEMENT_DRAWING", staged,
                                           record)
        save_registry(p, registry)
        return {"asset_id": record["asset_id"], "revision": record["revision"],
                "content_sha256": record["content_sha256"],
                "kind": "REPLACEMENT_DRAWING", "state": "DRAFT",
                "new_revision": created}


def import_rig_spec(project, spec, *, asset_id=None, note=""):
    """Register a RIG_SPEC: hierarchy, anchors, allowed transforms, z-order.

    The spec dict is stored canonically as `rig.json`; every layer's pinned
    LAYER_RGBA revision must already exist in the registry.
    """
    p = Path(project)
    with project_mutex(p):
        require_animation_profile(p)
        if asset_id is not None and not ASSET_ID.fullmatch(asset_id):
            raise FilmError("asset_id must be an A0001-style identifier")
        validate_rig_spec(spec)
        registry = load_registry(p)
        for layer in spec["layers"]:
            target = _resolve_pin(registry, layer["asset"]["asset_id"],
                                  layer["asset"]["revision"],
                                  layer["asset"]["content_sha256"])
            if target["kind"] != "LAYER_RGBA":
                raise FilmError(f"rig layer {layer['id']} must pin a "
                                "LAYER_RGBA revision")
            if target["z_order"] != layer["z_order"]:
                raise FilmError(f"rig layer {layer['id']} z_order must "
                                "match its pinned layer asset")
        if [l["id"] for l in sorted(spec["layers"],
                                    key=lambda l: l["z_order"])] \
                != spec["occlusion_order"]:
            raise FilmError("RIG_SPEC occlusion_order must agree with the "
                            "layer z_order values")
        data = canon_bytes(spec)
        staged = [{"stored_name": "rig.json", "source": data,
                   "sha256": hashlib.sha256(data).hexdigest(),
                   "byte_length": len(data), "source_name": "rig.json"}]
        record = {"asset_id": asset_id or "A0000", "revision": 0,
                  "kind": "RIG_SPEC",
                  "provenance": {"type": "EXTERNAL_IMPORT", "via": "spec",
                                 "imported_at": now(), "note": note.strip()},
                  "files": [{"relative_name": "",
                             "sha256": staged[0]["sha256"],
                             "byte_length": len(data)}],
                  "coordinate_space": {"system": "CUT_CANVAS_PIXELS",
                                       "origin": "TOP_LEFT"},
                  "dependencies": [],
                  "preparation": {"state": "IMPORTED_DRAFT",
                                  "checks": {"hierarchy_valid": True,
                                             "pins_resolved": True},
                                  "not_verified": ["human_review"]},
                  "acceptance": {"state": "DRAFT"},
                  "spec": spec}
        record["content_sha256"] = content_digest(record)
        record, created = _commit_revision(p, registry, asset_id,
                                           "RIG_SPEC", staged, record)
        save_registry(p, registry)
        return {"asset_id": record["asset_id"], "revision": record["revision"],
                "content_sha256": record["content_sha256"], "kind": "RIG_SPEC",
                "layers": [l["id"] for l in spec["layers"]],
                "state": "DRAFT", "new_revision": created}


def register_composite_sequence(p, shot_id, *, canvas, members, dependencies,
                                composite_recipe_ref, plan_sha256):
    """Register locally composited frames as a COMPOSITE_SEQUENCE revision.

    Runs inside the compositor's project mutex. `members` are staged PNG
    paths plus their digests; `dependencies` pin every input asset the
    composite consumed. The shot's assignment is pinned to the resulting
    revision and the timeline adopts it — the revision stays a draft.
    """
    registry = load_registry(p)
    pin = registry["assignments"].get(shot_id)
    asset_id = None
    if pin is not None:
        existing = registry["assets"].get(pin["asset_id"])
        if existing is not None and next(iter(existing["revisions"].values()))\
                ["kind"] == "COMPOSITE_SEQUENCE":
            asset_id = pin["asset_id"]
    record = {"asset_id": asset_id or "A0000", "revision": 0,
              "kind": "COMPOSITE_SEQUENCE",
              "provenance": {"type": "LOCAL_COMPOSITE",
                             "plan_sha256": plan_sha256,
                             "recipe_sha256": composite_recipe_ref,
                             "composited_at": now()},
              "files": [{"relative_name": "", "sha256": m["sha256"],
                         "byte_length": m["byte_length"], "frame_index": i}
                        for i, m in enumerate(members)],
              "coordinate_space": _coordinate_space(canvas),
              "dependencies": list(dependencies),
              "preparation": {"state": "COMPOSITED_DRAFT",
                              "checks": {"member_hashes_verified": True,
                                         "recipe_bound": True},
                              "not_verified": ["human_review",
                                               "motion_approval"]},
              "acceptance": {"state": "DRAFT"}, "canvas": dict(canvas),
              "exposure_recipe_ref": None,
              "composite_recipe_ref": composite_recipe_ref}
    record["content_sha256"] = content_digest(record)
    record, created = _commit_revision(p, registry, asset_id,
                                       "COMPOSITE_SEQUENCE", members, record)
    registry["assignments"][shot_id] = {"asset_id": record["asset_id"],
                                        "revision": record["revision"],
                                        "content_sha256":
                                            record["content_sha256"],
                                        "kind": "COMPOSITE_SEQUENCE"}
    save_registry(p, registry)
    _adopt_revision(p, shot_id, record["revision"])
    return record, created


def _verify_member_bytes(p, record):
    """Re-hash every member file; missing, moved or changed bytes fail."""
    members = []
    for f in record["files"]:
        path = safe_path(p, f["relative_name"])
        if path.is_symlink() or not path.is_file():
            raise FilmError(f"Missing asset member: {f['relative_name']}")
        if path.stat().st_size != f["byte_length"]:
            raise FilmError(f"Asset member length changed: {f['relative_name']}")
        if digest(path) != f["sha256"]:
            raise FilmError(f"Asset member hash mismatch: {f['relative_name']}")
        members.append((f.get("frame_index"), path))
    return members


def _resolve_pin(registry, asset_id, revision, content_sha256):
    entry = registry["assets"].get(asset_id)
    if entry is None:
        raise FilmError(f"Unknown asset: {asset_id}")
    record = entry["revisions"].get(str(revision))
    if record is None:
        raise FilmError(f"{asset_id} has no revision {revision}")
    if record["content_sha256"] != content_sha256:
        raise FilmError(f"{asset_id} r{revision} content no longer matches its pin")
    return record


def resolve_asset(p, asset_id, revision, content_sha256):
    """Resolve one pinned asset revision to verified member paths."""
    registry = load_registry(p)
    record = _resolve_pin(registry, asset_id, revision, content_sha256)
    members = _verify_member_bytes(p, record)
    if content_digest(record) != content_sha256:
        raise FilmError(f"{asset_id} r{revision} content hash changed")
    return {"asset_id": asset_id, "revision": revision,
            "content_sha256": content_sha256, "record": record,
            "members": members}


def resolve_shot_sequence(p, shot_id, used_range=None, handles=None,
                          expected_revision=None):
    """Resolve a shot's assigned sequence and check it covers the used range.

    `used_range` is the timeline entry's `[start, end)` source interval and
    `handles` its `unused_handles`; the required source extent is
    `[start - before, end + after)` inside the imported member indices.
    `expected_revision` is the entry's adopted `sequence_revision`; a pin that
    names a different revision does not silently satisfy the edit.
    """
    registry = load_registry(p)
    pin = registry["assignments"].get(shot_id)
    if pin is None:
        raise FilmError(f"{shot_id} has no assigned animation sequence")
    if expected_revision is not None and pin["revision"] != expected_revision:
        raise FilmError(
            f"{shot_id} timeline adopts sequence revision {expected_revision} "
            f"but the registry assignment pins revision {pin['revision']}")
    resolved = resolve_asset(p, pin["asset_id"], pin["revision"], pin["content_sha256"])
    if resolved["record"]["kind"] not in SEQUENCE_KINDS:
        raise FilmError(f"{shot_id} is not assigned a frame sequence "
                        f"(found {resolved['record']['kind']})")
    if used_range is not None:
        handles = handles or {}
        before = _int(handles.get("before", 0), "unused_handles.before")
        after = _int(handles.get("after", 0), "unused_handles.after")
        start, end = used_range
        count = len(resolved["record"]["files"])
        if start - before < 0 or end + after > count:
            raise FilmError(
                f"{shot_id} used range plus handles [{start - before}, {end + after}) "
                f"exceeds the {count} imported frames")
    return resolved


def asset_status(project):
    """Summary of registered assets and shot assignments for inspection."""
    p = Path(project)
    require_animation_profile(p)
    registry = load_registry(p)
    assets = []
    for asset_id in sorted(registry["assets"]):
        entry = registry["assets"][asset_id]
        current = entry["revisions"][str(entry["current_revision"])]
        assets.append({"asset_id": asset_id,
                       "current_revision": entry["current_revision"],
                       "kind": current["kind"], "state": current["acceptance"]["state"],
                       "revisions": sorted(int(r) for r in entry["revisions"])})
    return {"assets": assets, "assignments": registry["assignments"]}
