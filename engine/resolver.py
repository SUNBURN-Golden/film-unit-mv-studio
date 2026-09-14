"""Resolve existing media only. This module never submits a provider request."""
from pathlib import Path
import shutil

from .core import (FilmError, digest, object_hash, production_fingerprint, read,
                   safe_path, visual_context_fingerprint, write)


def shot_hash(shot):
    return object_hash({k: v for k, v in shot.items() if k != "status"})


def reference_hash(project, shot):
    files = {}
    for relative in shot.get("references", []):
        path = safe_path(project, relative)
        files[relative] = digest(path) if path.is_file() else None
    return object_hash(files)


def review_binding(record, visual_context_id):
    return object_hash({"schema_version": 2, "sha256": record.get("sha256"),
                        "shot_hash": record.get("shot_hash"), "reference_hash": record.get("reference_hash"),
                        "source_in_ms": record.get("source_in_ms", 0), "visual_context_id": visual_context_id})


def clip_review_fingerprint(project, shot, sha256, source_in_ms=0, visual_context_id=None):
    context = visual_context_id or visual_context_fingerprint(project)
    return review_binding({"sha256": sha256, "shot_hash": shot_hash(shot),
                           "reference_hash": reference_hash(project, shot), "source_in_ms": source_in_ms}, context)


def register_asset(project, shot, source, kind, *, reviewer="", evidence="",
                   production_id=None, source_in_ms=0, generated=True, visual_context_id=None,
                   qc_review_binding=None, qc_source_in_ms=0):
    """Copy a take; bind review to visual context, this shot, its refs and window.

    production_id remains provenance for legacy generation callers. It does not
    authorize visual review. Legacy global-only reviews need explicit re-review.
    """
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
    context = visual_context_fingerprint(p)
    if visual_context_id is not None and visual_context_id != context:
        raise FilmError("Visual context changed since clip QC")
    record = {"path": str(dest.relative_to(p)), "sha256": sha, "shot_hash": shot_hash(shot),
              "reference_hash": reference_hash(p, shot),
              "source_in_ms": source_in_ms, "generated": generated,
              "review": {"reviewer": reviewer.strip(), "evidence": evidence.strip(),
                         "production_id": production_id, "schema_version": 2, "visual_context_id": context}}
    # QC reviewed a normalized clip corresponding to a raw source window. Check
    # all reviewed inputs before binding that same clip's local [0, duration)
    # window; shared visual context alone cannot detect a changed shot/ref/file.
    if qc_review_binding is not None:
        reviewed_record = {**record, "source_in_ms": qc_source_in_ms}
        if review_binding(reviewed_record, context) != qc_review_binding:
            raise FilmError("Shot, references or video changed since clip QC")
    if reviewer.strip() and evidence.strip():
        record["review"]["binding"] = review_binding(record, context)
    registry = read(p / "manifest/assets.json", {"schema_version": 1, "shots": {}})
    registry["shots"].setdefault(shot["id"], {})[kind] = record
    write(p / "manifest/assets.json", registry)
    return record


def candidates(project, shot, production_id=None, *, visual_context_id=None):
    # Third positional argument is kept for existing callers; global LOCK IDs
    # must never invalidate an otherwise unchanged per-shot visual review.
    p = Path(project)
    context = visual_context_id or visual_context_fingerprint(p)
    references = reference_hash(p, shot)
    selections = read(p / "manifest/assets.json", {"shots": {}}).get("shots", {}).get(shot["id"], {})
    for kind in ("final", "draft"):
        entry = selections.get(kind)
        if not entry or entry.get("shot_hash") != shot_hash(shot):
            continue
        review = entry.get("review", {})
        approved = bool(kind == "final" and review.get("schema_version") == 2 and
                        review.get("visual_context_id") == context and entry.get("reference_hash") == references and
                        review.get("binding") == review_binding(entry, context) and
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
