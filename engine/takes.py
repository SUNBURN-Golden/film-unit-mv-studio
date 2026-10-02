"""Reuse paid source clips across output sizes; local trims never call a provider."""
from pathlib import Path
import math
import shutil
from .core import FilmError, digest, object_hash, probe, read, require_legacy_profile, safe_path, write
from .renderers import RenderBlocked, build_prompt


def key_for(renderer, shot, attempt, correction):
    settings = renderer.generation_config(shot)
    if settings is None:
        return None
    return object_hash({"shot": shot["id"], "settings": settings,
        "prompt": build_prompt(renderer.project, shot), "attempt": attempt, "correction": correction,
        "references": [digest(safe_path(renderer.project, r)) for r in shot["references"]]})


def restore(project, key, target):
    if not key:
        return False
    p = Path(project)
    meta = read(p / f"render/takes/{key}.json", {})
    if not meta:
        return False
    clip = safe_path(p, meta["path"])
    if not clip.exists() or digest(clip) != meta["sha256"]:
        raise RenderBlocked("Saved paid take is missing or changed; recover it before spending again")
    shutil.copyfile(clip, target)
    return True


def remember(project, key, source, provider, job_id):
    if not key:
        return
    p = Path(project)
    dest = p / f"render/takes/{key}.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".pending.mp4")
    shutil.copyfile(source, temp)
    temp.replace(dest)
    write(dest.with_suffix(".json"), {"key": key, "path": str(dest.relative_to(p)),
        "sha256": digest(dest), "provider": provider, "job_id": job_id})


def edit_start(project, job_id, raw):
    decision = read(Path(project) / "render/edit_decisions.json", {}).get(job_id)
    if not decision:
        return 0
    if decision["raw_sha256"] != digest(raw):
        raise RenderBlocked("Local edit belongs to a different take; inspect the source first")
    value = decision["source_in_ms"]
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise FilmError("Source start must be a finite nonnegative millisecond value")
    return value


def select_window(project, shot_id, source_in_ms, notes):
    """Select a later clean window, bound to the current take. No retiming/freeze."""
    p = Path(project)
    require_legacy_profile(p)
    if type(source_in_ms) is not int or source_in_ms < 0 or not notes.strip():
        raise FilmError("Supply a nonnegative integer source-in and concrete edit notes")
    report = read(p / "qc/report.json")
    record = next((s for s in report["shots"] if s["shot_id"] == shot_id), None)
    if not record or not record.get("raw_path"):
        raise FilmError("No downloaded take is available for this shot")
    raw = safe_path(p, record["raw_path"])
    video = next(s for s in probe(raw)["streams"] if s["codec_type"] == "video")
    if source_in_ms + record["selected_duration_ms"] > float(video["duration"]) * 1000 + 1:
        raise FilmError("Selected window is too short for the locked shot; no freeze or time stretch is allowed")
    path = p / "render/edit_decisions.json"
    edits = read(path, {})
    edits[record["job_id"]] = {"raw_sha256": digest(raw), "source_in_ms": source_in_ms, "notes": notes}
    write(path, edits)
    return {"shot": shot_id, "status": "LOCAL_EDIT_READY", "generation_cost": 0,
        "next": "Resume compile; the edited clip needs a fresh visual review"}
