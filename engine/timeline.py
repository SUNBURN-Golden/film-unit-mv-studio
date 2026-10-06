"""Edit the visual timeline without moving the independent lyric timeline."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil

from .core import (FilmError, now, project_mutex, read, require_legacy_profile,
                   safe_path, validate_manifest, write)


def _load(project):
    p = Path(project)
    require_legacy_profile(p)
    shots = read(p / "manifest/shots.json")
    audio = read(p / "analysis/audio.json")
    fps = read(p / "project.yaml")["format"]["fps"]
    validate_manifest(shots, audio["duration_ms"], fps)
    return p, shots, audio, fps


def _index(shots, shot_id):
    for index, shot in enumerate(shots):
        if shot["id"] == shot_id:
            return index
    raise FilmError(f"Unknown shot: {shot_id}")


def _integer(value):
    if type(value) is not int:
        raise FilmError("Cut position must be integer milliseconds")
    return value


def _commit(p, shots, audio, fps, affected, operation):
    validate_manifest(shots, audio["duration_ms"], fps)
    for shot in shots:
        if shot["id"] in affected:
            shot["visual_revision"] = shot.get("visual_revision", 0) + 1
            shot["status"] = "storyboard"
            for key in ("final_video", "draft_video", "clip_path", "raw_path", "selected_take"):
                shot.pop(key, None)

    # Derive sequence endpoints from the edited, still-contiguous shot groups.
    sequences = read(p / "manifest/sequence.json", [])
    for sequence in sequences:
        members = [s for s in shots if s["sequence"] == sequence["id"]]
        if members:
            sequence["in_ms"] = members[0]["in_ms"]
            sequence["out_ms"] = members[-1]["out_ms"]

    # A shot hash changes first, so a process interrupted before the registry
    # cleanup cannot accidentally reuse a clip authorized for the old range.
    write(p / "manifest/shots.json", shots)
    if sequences:
        write(p / "manifest/sequence.json", sequences)
    assets_path = p / "manifest/assets.json"
    if assets_path.exists():
        assets = read(assets_path)
        for shot_id in affected:
            assets.get("shots", {}).pop(shot_id, None)
        write(assets_path, assets)
    report_path = p / "qc/report.json"
    if report_path.exists():
        report = read(report_path)
        for record in report.get("shots", []):
            if record.get("shot_id") in affected:
                record.update(status="STALE_TIMELINE", timeline_stale=True)
        write(report_path, report)
    event = {"operation": operation, "affected_shots": sorted(affected), "at": now(),
             "lyrics_changed": False, "production_lock": "requires review"}
    history = read(p / "manifest/timeline_edits.json", {"schema_version": 1, "events": []})
    history["events"].append(event)
    write(p / "manifest/timeline_edits.json", history)
    return {**event, "shots": shots}


def split_shot(project, shot_id, at_ms):
    """Keep the original ID on the left and allocate a new stable ID on the right."""
    _integer(at_ms)
    with project_mutex(project):
        p, shots, audio, fps = _load(project)
        index = _index(shots, shot_id)
        left = shots[index]
        if not left["in_ms"] < at_ms < left["out_ms"]:
            raise FilmError("Split must be strictly inside the shot")
        right = deepcopy(left)
        next_id = max(int(s["id"][1:]) for s in shots) + 1
        # A removed shot may still have artwork. Never overwrite that asset when
        # allocating a new ID, including dangling symlinks or reserved references.
        reserved = {r for shot in shots for r in shot.get("references", [])}
        while next_id <= 99999:
            reference = f"storyboard/S{next_id:03d}.png"
            destination = p / reference
            if (not destination.exists() and not destination.is_symlink()
                    and reference not in reserved):
                safe_path(p, reference)
                break
            next_id += 1
        else:
            raise FilmError("No remaining shot IDs")
        right.update(id=f"S{next_id:03d}", in_ms=at_ms,
                     duration_ms=right["out_ms"] - at_ms)
        left.update(out_ms=at_ms, duration_ms=at_ms - left["in_ms"])
        shots.insert(index + 1, right)
        # Validate frame-sized ranges before any asset is created. Only the first
        # reference is owned by a shot; character/location references stay shared.
        validate_manifest(shots, audio["duration_ms"], fps)
        references = right.get("references", [])
        source = safe_path(p, references[0]) if references else None
        right["references"] = [reference, *references[1:]]
        if source is not None and source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as target:
                try:
                    with source.open("rb") as original:
                        shutil.copyfileobj(original, target)
                except Exception:
                    destination.unlink(missing_ok=True)
                    raise
        else:
            # Resolver will produce a placeholder; no artwork is fabricated here.
            right["storyboard_kind"] = "placeholder"
        return _commit(p, shots, audio, fps, {left["id"], right["id"]}, "split")


def merge_shots(project, left_id, right_id):
    """Merge neighboring shots in the same sequence, retaining the left direction."""
    with project_mutex(project):
        p, shots, audio, fps = _load(project)
        index = _index(shots, left_id)
        if index + 1 >= len(shots) or shots[index + 1]["id"] != right_id:
            raise FilmError("Merge requires adjacent shots in left-to-right order")
        left, right = shots[index:index + 2]
        if left["sequence"] != right["sequence"]:
            raise FilmError("Merge requires shots in the same sequence")
        left.update(out_ms=right["out_ms"], duration_ms=right["out_ms"] - left["in_ms"])
        shots.pop(index + 1)
        return _commit(p, shots, audio, fps, {left_id, right_id}, "merge")


def move_cut(project, left_id, at_ms):
    """Move the outgoing cut of a shot while preserving total audio coverage."""
    _integer(at_ms)
    with project_mutex(project):
        p, shots, audio, fps = _load(project)
        index = _index(shots, left_id)
        if index + 1 >= len(shots):
            raise FilmError("The end of the song cannot be moved")
        left, right = shots[index:index + 2]
        if not left["in_ms"] < at_ms < right["out_ms"]:
            raise FilmError("Cut must stay inside the two neighboring shots")
        left.update(out_ms=at_ms, duration_ms=at_ms - left["in_ms"])
        right.update(in_ms=at_ms, duration_ms=right["out_ms"] - at_ms)
        return _commit(p, shots, audio, fps, {left["id"], right["id"]}, "move_cut")


def snap_cut(project, left_id, target="beat", around_ms=None):
    """Move a cut to the nearest measured beat/onset that preserves both shots."""
    if target not in {"beat", "onset"}:
        raise FilmError("Snap target must be beat or onset")
    with project_mutex(project):
        p, shots, audio, fps = _load(project)
        index = _index(shots, left_id)
        if index + 1 >= len(shots):
            raise FilmError("The end of the song cannot be moved")
        left, right = shots[index:index + 2]
        anchor = left["out_ms"] if around_ms is None else _integer(around_ms)
        key = "beat_times_ms" if target == "beat" else "onsets_ms"
        candidates = audio.get(key, [])
        if target == "onset" and not candidates:
            candidates = audio.get("onset_times_ms", [])
        for candidate in sorted(set(candidates), key=lambda ms: (abs(ms - anchor), ms)):
            if type(candidate) is not int or not left["in_ms"] < candidate < right["out_ms"]:
                continue
            proposed = deepcopy(shots)
            proposed[index].update(out_ms=candidate, duration_ms=candidate - left["in_ms"])
            proposed[index + 1].update(in_ms=candidate, duration_ms=right["out_ms"] - candidate)
            try:
                validate_manifest(proposed, audio["duration_ms"], fps)
            except FilmError:
                continue
            return _commit(p, proposed, audio, fps, {left["id"], right["id"]}, f"snap_{target}")
        raise FilmError(f"No measured {target} can form a valid neighboring cut")
