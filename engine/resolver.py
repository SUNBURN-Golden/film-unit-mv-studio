"""Resolve existing media only. This module never submits a provider request."""
from pathlib import Path
import shutil

from .core import FilmError, digest, object_hash, production_fingerprint, read, safe_path, write


def shot_hash(shot):
    return object_hash({k: v for k, v in shot.items() if k != "status"})


def review_binding(record, production_id):
    return object_hash({"sha256": record.get("sha256"), "shot_hash": record.get("shot_hash"),
                        "source_in_ms": record.get("source_in_ms", 0), "production_id": production_id})


def register_asset(project, shot, source, kind, *, reviewer="", evidence="",
                   production_id=None, source_in_ms=0, generated=True):
    """Copy a selected take. A final review is bound to its bytes and production."""
    p, source = Path(project), Path(source)
    if kind not in {"final", "draft"}:
        raise FilmError("Asset kind must be final or draft")
    if type(source_in_ms) is not int or source_in_ms < 0:
        raise FilmError("Source start must be nonnegative integer milliseconds")
    sha = digest(source)
    dest = p / "render/assets" / (sha + ".mp4")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        temporary = dest.with_suffix(".pending")
        shutil.copyfile(source, temporary)
        if digest(temporary) != sha:
            raise FilmError("Source changed during asset import")
        temporary.replace(dest)
    elif digest(dest) != sha:
        raise FilmError("Stored take hash mismatch")
    if production_id is None:
        try:
            production_id = production_fingerprint(p)
        except (FilmError, OSError, KeyError):
            production_id = None
    record = {"path": str(dest.relative_to(p)), "sha256": sha, "shot_hash": shot_hash(shot),
              "source_in_ms": source_in_ms, "generated": generated,
              "review": {"reviewer": reviewer.strip(), "evidence": evidence.strip(),
                         "production_id": production_id}}
    if reviewer.strip() and evidence.strip():
        record["review"]["binding"] = review_binding(record, production_id)
    registry = read(p / "manifest/assets.json", {"schema_version": 1, "shots": {}})
    registry["shots"].setdefault(shot["id"], {})[kind] = record
    write(p / "manifest/assets.json", registry)
    return record


def candidates(project, shot, production_id=None):
    p = Path(project)
    selections = read(p / "manifest/assets.json", {"shots": {}}).get("shots", {}).get(shot["id"], {})
    for kind in ("final", "draft"):
        entry = selections.get(kind)
        if not entry or entry.get("shot_hash") != shot_hash(shot):
            continue
        review = entry.get("review", {})
        approved = bool(kind == "final" and production_id and
                        review.get("production_id") == production_id and
                        review.get("binding") == review_binding(entry, production_id) and
                        review.get("reviewer") and review.get("evidence"))
        yield {**entry, "kind": kind, "approved": approved}
    reference = shot.get("references", [None])[0] if shot.get("references") else f"storyboard/{shot['id']}.png"
    if reference:
        yield {"path": reference, "kind": "storyboard", "source_in_ms": 0,
               "approved": shot.get("storyboard_kind") == "imported" and shot["render_mode"] == "STATIC"}
    yield {"path": None, "kind": "placeholder", "source_in_ms": 0, "approved": False}


def checked_source(project, candidate):
    source = safe_path(project, candidate["path"])
    if not source.is_file():
        raise FilmError("Asset is missing")
    sha = digest(source)
    if candidate.get("sha256") and candidate["sha256"] != sha:
        raise FilmError("Asset hash mismatch")
    return source, sha
