"""FRAME_ANIMATION_V1 sequence normalization (ANIM-004, design 6.1-6.3, 7).

A timeline entry's `used_source_range` owns `[start, end)` output frames on
the 24fps frame clock. Normalization binds that range to the shot's pinned
FRAME_SEQUENCE revision and produces the per-output-frame member map the
compiler samples:

- without an exposure schedule the mapping is the identity (ones): output
  local frame `k` reads member `start + k`;
- with exposure schedules the track must tile `[0, L)` exactly, every slot
  must be filled (BLOCK-EMPTY-EXPOSURE), and each slot's drawing is an
  absolute member index that must stay inside the declared used range — a
  96-frame cut on twos exposes drawing 94 on output frames 94 and 95;
- handles stay unused: members in `unused_handles` regions are never mapped;
- a source shorter than the owned range, or a member index outside it, is
  rejected (BLOCK-SHORT-SOURCE) — never stretched, padded or speed-changed;
- external video at another FPS maps by `FLOOR_CONTAINMENT_V1`: output frame
  `i` reads the source frame whose rational interval contains `i/output_fps`.

The normalized map is a derived view: member bytes stay hashed in the asset
registry, and this module writes no approval state.
"""
import hashlib

from fractions import Fraction
from pathlib import Path

from .animation_assets import resolve_shot_sequence
from .animation_schema import (canon_bytes, load_animation_timeline,
                               require_animation_profile)
from .core import FilmError, read
from .exposure import expand_track
from .frame_clock import check_fps, exposure_window

SOURCE_MAP_RULE = "FLOOR_CONTAINMENT_V1"


def member_map_for_entry(entry, schedules=None):
    """Per-output-frame member indices for one validated timeline entry.

    `schedules` is the exposure track list for the shot's drawing track, or
    None for the default ones mapping. Returns a list of length `L` whose
    values are member indices, or None where a slot carries only a state.
    """
    start, end = entry["used_source_range"]
    length = end - start
    if schedules is None:
        return [{"local": k, "member": start + k, "slot": k, "state": None,
                 "hold": False, "continued": False, "transform": None}
                for k in range(length)]
    rows = expand_track(schedules, length)
    mapping = []
    for local, row in enumerate(rows):
        drawing = row["drawing"]
        member = None
        if drawing is not None:
            if drawing < start or drawing >= end:
                raise FilmError(
                    f"Exposure slot {row['slot']} references member {drawing} "
                    f"outside the declared used range [{start}, {end})")
            member = drawing
        mapping.append({"local": local, "member": member, "slot": row["slot"],
                        "hold": row["hold"], "continued": row["continued"],
                        "state": row["state"], "transform": row["transform"]})
    return mapping


def _normalized_digest(shot_id, entry, mapping, members):
    return hashlib.sha256(canon_bytes({
        "shot_id": shot_id, "instance_id": entry["instance_id"],
        "used_source_range": entry["used_source_range"],
        "members": [{"local": row["local"], "member": row["member"],
                     "sha256": (members[row["member"]]["sha256"]
                                if row["member"] is not None else None)}
                    for row in mapping],
    })).hexdigest()


def normalize_shot_sequence(project, shot_id, *, schedules=None):
    """Normalize a shot's pinned sequence onto its timeline entries.

    Resolves the assigned revision's member bytes (hash-checked) and returns
    the derived member map per timeline entry using that shot. The result is
    a draft view: it creates no review, LOCK or approval.
    """
    from .core import read
    from pathlib import Path
    p = Path(project)
    require_animation_profile(p)
    timeline = load_animation_timeline(p)
    entries = [e for e in timeline["entries"] if e["shot_id"] == shot_id]
    if not entries:
        raise FilmError(f"{shot_id} has no timeline entry")
    fps = read(p / "project.yaml")["format"]["fps"]
    check_fps(fps)
    out_entries = []
    for entry in entries:
        resolved = resolve_shot_sequence(p, shot_id, entry["used_source_range"],
                                         entry["unused_handles"],
                                         entry["sequence_revision"])
        members = {f["frame_index"]: f for f in resolved["record"]["files"]}
        mapping = member_map_for_entry(entry, schedules)
        out_entries.append({
            "instance_id": entry["instance_id"],
            "used_source_range": entry["used_source_range"],
            "asset_id": resolved["asset_id"],
            "sequence_revision": resolved["revision"],
            "content_sha256": resolved["content_sha256"],
            "frames": len(mapping),
            "member_map": mapping,
            "exposure_window": [exposure_window(row["local"], fps)
                                for row in mapping],
            "normalized_sha256": _normalized_digest(shot_id, entry, mapping,
                                                    members),
        })
    pin = {"asset_id": out_entries[0]["asset_id"],
           "revision": out_entries[0]["sequence_revision"],
           "content_sha256": out_entries[0]["content_sha256"]}
    return {"shot_id": shot_id, "state": "DRAFT", "fps": fps,
            "pin": pin, "entries": out_entries}


def map_source_frames(source_fps, output_fps, output_frames,
                      source_start=0, source_count=None):
    """Map output frames onto a source video's frame indices.

    Output frame `i` owns `[i/output_fps, (i+1)/output_fps)`; the rule reads
    the source frame whose interval contains the output frame's start.
    `source_start` is the first usable source frame; `source_count`, when
    given, is the number of frames the provider actually returned — asking
    beyond it is a short-source rejection, never padded.
    """
    check_fps(source_fps)
    check_fps(output_fps)
    if type(output_frames) is not int or output_frames < 1:
        raise FilmError("output_frames must be a positive integer")
    if type(source_start) is not int or source_start < 0:
        raise FilmError("source_start must be a non-negative integer")
    rate = Fraction(source_fps, output_fps)
    mapping = []
    for i in range(output_frames):
        source_index = source_start + int(Fraction(i) * rate)
        if source_count is not None and source_index >= source_start + source_count:
            raise FilmError(
                f"Source is shorter than the owned range: output frame {i} "
                f"needs source frame {source_index} but only {source_count} "
                "frames were returned")
        mapping.append(source_index)
    return {"rule": SOURCE_MAP_RULE, "source_fps": source_fps,
            "output_fps": output_fps, "source_start": source_start,
            "frames": mapping}


def animation_validate(project):
    """Plan check: validate the edit timeline and each entry's sequence."""
    p = Path(project)
    require_animation_profile(p)
    timeline = load_animation_timeline(p)
    entries = []
    for entry in timeline["entries"]:
        try:
            resolved = resolve_shot_sequence(p, entry["shot_id"],
                                             entry["used_source_range"],
                                             entry["unused_handles"],
                                             entry["sequence_revision"])
            entries.append({"instance_id": entry["instance_id"],
                            "shot_id": entry["shot_id"], "resolved": True,
                            "asset_id": resolved["asset_id"],
                            "revision": resolved["revision"],
                            "content_sha256": resolved["content_sha256"],
                            "members": len(resolved["record"]["files"]),
                            "used_source_range": entry["used_source_range"]})
        except FilmError as e:
            entries.append({"instance_id": entry["instance_id"],
                            "shot_id": entry["shot_id"], "resolved": False,
                            "reason": str(e)})
    return {"target_frames": timeline["target_frames"],
            "entries": entries,
            "ok": all(e["resolved"] for e in entries),
            "unresolved": [e["instance_id"] for e in entries
                           if not e["resolved"]],
            "note": "Structural plan check only; unresolved sequences remain "
                    "drafts and create no review or approval"}
