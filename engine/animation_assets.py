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
IMPLEMENTED_KINDS = {"FRAME_SEQUENCE", "LAYER_RGBA"}
# Only lossless PNG is accepted for imported frames/layers in v1: re-encoding
# would risk silent alpha or pixel loss before any recipe records it.
ALLOWED_SUFFIX = {".png"}

REGISTRY_FIELDS = {"document_type", "schema_version", "assets", "assignments"}
RECORD_COMMON = {"asset_id", "revision", "kind", "provenance", "files",
                 "content_sha256", "coordinate_space", "dependencies",
                 "preparation", "acceptance"}
SEQUENCE_FIELDS = {"canvas", "exposure_recipe_ref", "composite_recipe_ref"}
LAYER_FIELDS = {"alpha", "canvas", "crop_origin", "pivot", "z_order"}
FILE_FIELDS = {"relative_name", "sha256", "byte_length", "frame_index",
               "source_name"}
ASSIGNMENT_FIELDS = {"asset_id", "revision", "content_sha256", "kind"}


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
    if record["kind"] == "FRAME_SEQUENCE":
        content.update({"exposure_recipe_ref": record["exposure_recipe_ref"],
                        "composite_recipe_ref": record["composite_recipe_ref"]})
    elif record["kind"] == "LAYER_RGBA":
        content.update({"alpha": {"present": record["alpha"]["present"]},
                        "crop_origin": record["crop_origin"],
                        "pivot": record["pivot"], "z_order": record["z_order"]})
    return content


def content_digest(record):
    return hashlib.sha256(canon_bytes(_content_fields(record))).hexdigest()


def _check_files(files, require_frames):
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
        raise FilmError("FRAME_SEQUENCE frame_index values must be 0..N-1 without gaps")


def _validate_record(record):
    if type(record) is not dict:
        raise FilmError("Asset revision must be an object")
    if type(record.get("asset_id")) is not str or not ASSET_ID.fullmatch(record["asset_id"]):
        raise FilmError("asset_id must be an A0001-style identifier")
    _int(record.get("revision"), "revision", 1)
    kind = record.get("kind")
    if kind not in IMPLEMENTED_KINDS:
        raise FilmError(f"Unsupported asset kind: {kind}")
    extra = {"FRAME_SEQUENCE": SEQUENCE_FIELDS, "LAYER_RGBA": LAYER_FIELDS}[kind]
    if set(record.keys()) - RECORD_COMMON - extra:
        raise FilmError(f"Unknown fields on {kind} record")
    missing = (RECORD_COMMON | extra) - record.keys()
    if missing:
        raise FilmError(f"{kind} record missing fields: {sorted(missing)}")
    _check_files(record["files"], require_frames=kind == "FRAME_SEQUENCE")
    _sha(record["content_sha256"], "content_sha256")
    if content_digest(record) != record["content_sha256"]:
        raise FilmError("content_sha256 does not match the recorded content fields")
    if type(record["dependencies"]) is not list:
        raise FilmError("dependencies must be a list")
    if type(record["provenance"]) is not dict or type(record["preparation"]) is not dict:
        raise FilmError("provenance and preparation must be objects")
    if record["acceptance"].get("state") != "DRAFT":
        raise FilmError("Imported assets are drafts; no approval state is written by import")
    canvas = record["canvas"]
    if (type(canvas) is not dict or type(canvas.get("width")) is not int
            or type(canvas.get("height")) is not int
            or canvas["width"] < 1 or canvas["height"] < 1):
        raise FilmError("canvas must hold positive integer width/height")
    if kind == "LAYER_RGBA":
        if record["alpha"].get("present") is not True:
            raise FilmError("LAYER_RGBA must record a preserved alpha channel")
        _point(record["crop_origin"], "crop_origin")
        _point(record["pivot"], "pivot")
        _int(record["z_order"], "z_order")
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
        if pin["kind"] not in IMPLEMENTED_KINDS:
            raise FilmError(f"Unsupported assigned kind: {pin['kind']}")
        if not ASSET_ID.fullmatch(pin["asset_id"] if type(pin["asset_id"]) is str else ""):
            raise FilmError("Assignment asset_id must be an A0001-style identifier")
        _int(pin["revision"], "assignment revision", 1)
        _sha(pin["content_sha256"], "assignment content_sha256")
    return document


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
                      **({"frame_index": position} if record["kind"] == "FRAME_SEQUENCE"
                         else {})})
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
        timeline_path = safe_path(
            p, (read(p / "project.yaml").get("animation") or {})
            .get("timeline", "timeline/edit.json"))
        if timeline_path.exists():
            timeline = read_canon(timeline_path)
            for entry in timeline["entries"]:
                if entry["shot_id"] == shot_id:
                    entry["sequence_revision"] = record["revision"]
            write_canon(timeline_path, timeline)
        return {"asset_id": record["asset_id"], "revision": record["revision"],
                "content_sha256": record["content_sha256"],
                "frames": len(record["files"]), "assigned_to": shot_id,
                "state": "DRAFT", "new_revision": created}


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
    if resolved["record"]["kind"] != "FRAME_SEQUENCE":
        raise FilmError(f"{shot_id} is not assigned a FRAME_SEQUENCE")
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
