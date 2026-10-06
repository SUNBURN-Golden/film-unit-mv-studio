"""FRAME_ANIMATION_V1 draft Preview (ANIM-003).

Renders the canonical `timeline/edit.json` onto the draft canvas using each
shot's assigned frame sequence, with LINEAR_INTERIOR_V1 weights inside
transition overlaps and a labelled placeholder wherever a shot's sequence is
unresolved (missing, changed, tampered or too short). Output is a draft MP4
muxed with the original master audio, plus the composed PNG frames and a
per-frame `draft_frame_map.jsonl` under the new build directory.

The build record is `document_type: animation_draft_preview`, `schema_version`
1 — deliberately not a Build 2 `animation_build` and carrying no
`storage_profile`. Every resolved asset member the compose can touch is
captured under `snapshot/` before the first frame is drawn and the compose
reads those frozen copies, so the draft is reproducible from the build folder
and a member changed mid-compile cannot leak in. `draft_frame_map.jsonl` is a
draft-only map (one CANON_JSON_V1 document per line); its per-source
`resolved` flag marks draft placeholder substitution and is not part of the
schema section 9 `frame_map.jsonl` contract.

This is a draft review artifact: it performs no provider calls, creates no
approval and does not satisfy Final. Build 2 inventory and replay, subtitle
application and seal semantics arrive with their own nodes (ANIM-006 onward).
"""
from pathlib import Path
import copy

from PIL import Image, ImageDraw

from .animation_assets import resolve_shot_sequence
from .animation_schema import (canon_bytes, load_animation_timeline,
                               require_animation_profile,
                               validate_animation_timeline)
from .frame_clock import frame_filename
from .frame_sequence import member_map_for_entry
from .transitions import composite_pair, frame_contributors
from .builds import allocate_build, capture, seal_build
from .compiler import media_check
from .core import (FilmError, digest, ffmpeg, now, project_mutex, read,
                   safe_path, write)

CANVAS_BG = (40, 43, 48)  # 0x282b30, same neutral dark as legacy placeholders


def _fit(image, width, height):
    """Scale a source frame onto the canvas, centered, background padded."""
    source = image.convert("RGB")
    source.thumbnail((width, height), Image.LANCZOS)
    canvas = Image.new("RGB", (width, height), CANVAS_BG)
    canvas.paste(source, ((width - source.width) // 2, (height - source.height) // 2))
    return canvas


def _placeholder(width, height, label):
    im = Image.new("RGB", (width, height), CANVAS_BG)
    draw = ImageDraw.Draw(im)
    margin = max(4, width // 24)
    draw.rectangle((margin, margin, width - margin - 1, height - margin - 1),
                   outline=(115, 121, 131), width=1)
    draw.text((margin * 2, height // 2 - 6), label, fill=(230, 230, 230))
    return im


def _tag(frame, text):
    draw = ImageDraw.Draw(frame)
    draw.rectangle((0, 0, 4 + 7 * len(text), 12), fill=(20, 20, 20))
    draw.text((2, 2), text, fill=(250, 210, 90))
    return frame


def _frame_resolvers(document, layout, p):
    """Resolve every entry's assigned sequence once, before rendering."""
    entries = {e["instance_id"]: e for e in document["entries"]}
    resolved, report = {}, []
    for row in layout["entries"]:
        entry = entries[row["instance_id"]]
        try:
            found = resolve_shot_sequence(p, entry["shot_id"],
                                          entry["used_source_range"],
                                          entry["unused_handles"],
                                          entry["sequence_revision"])
            resolved[row["instance_id"]] = {
                "members": dict(found["members"]),
                "member_map": member_map_for_entry(entry),
                "member_sha256": {f.get("frame_index"): f["sha256"]
                                  for f in found["record"]["files"]},
                "used_start": entry["used_source_range"][0],
                "used_end": entry["used_source_range"][1],
                "pin": {"asset_id": found["asset_id"],
                        "revision": found["revision"],
                        "content_sha256": found["content_sha256"]}}
            report.append({"instance_id": row["instance_id"],
                           "shot_id": entry["shot_id"],
                           "output_range": row["output_range"], "resolved": True,
                           **resolved[row["instance_id"]]["pin"]})
        except FilmError as e:
            resolved[row["instance_id"]] = None
            report.append({"instance_id": row["instance_id"],
                           "shot_id": entry["shot_id"],
                           "output_range": row["output_range"], "resolved": False,
                           "reason": str(e)})
    return resolved, report


def _member_image(resolved_entry, local_index):
    """(Member pixels, None) on success, (None, reason) on decode failure."""
    if resolved_entry is None:
        return None, None
    path = resolved_entry["members"].get(local_index)
    if path is None:
        return None, None
    try:
        with Image.open(path) as im:
            return im.convert("RGB"), None
    except Exception as e:
        return None, f"member failed to decode ({path.name}): {e}"


def _compose(frame_index, layout, resolved, width, height):
    """One output frame plus its draft frame-map source rows and warnings."""
    pairs, transition = frame_contributors(layout, frame_index)
    layer, sources, warnings = None, [], []
    for row, weight in pairs:
        entry = resolved[row["instance_id"]]
        local = frame_index - row["output_range"][0]
        source_index = (entry["member_map"][local]["member"]
                        if entry is not None else None)
        image, error = _member_image(entry, source_index)
        present = image is not None
        if error is not None:
            warnings.append(f"{row['shot_id']} output frame {frame_index}: "
                            f"{error}; a labelled placeholder was used")
        if image is None:
            image = _placeholder(width, height,
                                 f"{row['shot_id']} / NO SEQUENCE — DRAFT")
        fitted = _fit(image, width, height)
        layer = fitted if layer is None else composite_pair(layer, fitted, weight)
        sources.append({"instance_id": row["instance_id"], "shot_id": row["shot_id"],
                        "local_frame_index": source_index,
                        "sequence_revision": entry["pin"]["revision"] if present else None,
                        "weight": [weight.numerator, weight.denominator],
                        "resolved": present})
    operations = ["DRAFT_TAG"]
    if transition is not None:
        operations += ["CROSSFADE", "LINEAR_INTERIOR_V1"]
    if any(not s["resolved"] for s in sources):
        operations.append("PLACEHOLDER_DRAFT")
    return _tag(layer, f"DRAFT {frame_index + 1}"), sources, operations, warnings


def _encode(frames_dir, count, fmt, master, target, seconds):
    """image2 encode with the contracted explicit framerate/start number."""
    pending = target.with_suffix(".pending.mp4")
    ffmpeg(["-xerror", "-framerate", str(fmt["fps"]), "-start_number", "1",
            "-i", str(frames_dir / "F_%06d.png"), "-map", "0:v:0", "-an",
            "-r", str(fmt["fps"]), "-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(fmt["crf"]), "-pix_fmt", "yuv420p", "-threads", "2",
            pending])
    media_check(pending, count, fmt)
    muxed = target.with_suffix(".mux.mp4")
    ffmpeg(["-xerror", "-i", pending, "-i", master,
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "320k", "-t", f"{seconds:.3f}",
            "-map_metadata", "-1", "-movflags", "+faststart", muxed])
    info = media_check(muxed, count, fmt, audio=True)
    sound = next(s for s in info["streams"] if s["codec_type"] == "audio")
    if abs(float(sound.get("duration", 0)) - seconds) > .1:
        raise FilmError("Master audio does not cover the draft preview timeline")
    muxed.replace(target)
    pending.unlink(missing_ok=True)


def compile_draft_preview(project, quality="draft", progress=None):
    """Draft Preview for an explicitly converted FRAME_ANIMATION_V1 project."""
    p = Path(project).resolve()
    with project_mutex(p):
        config = require_animation_profile(p)
        if quality not in {"draft", "final"}:
            raise FilmError("Quality must be draft or final")
        fmt = copy.deepcopy(config["format"])
        if quality == "draft" and fmt["width"] > 960:
            fmt["height"] = round(fmt["height"] * 960 / fmt["width"] / 2) * 2
            fmt["width"] = 960
        document = load_animation_timeline(p)
        output_frames = config["animation"]["output_frames"]
        layout = validate_animation_timeline(document, output_frames)
        master = safe_path(p, config["audio"]["path"])
        if digest(master) != config["audio"]["sha256"]:
            raise FilmError("Master audio hash mismatch")
        analysis = read(p / "analysis/audio.json")
        if analysis.get("master_sha256", config["audio"]["sha256"]) != config["audio"]["sha256"]:
            raise FilmError("Audio analysis belongs to a different master")
        seconds = output_frames / fmt["fps"]
        resolved, report = _frame_resolvers(document, layout, p)
        folder = allocate_build(p, "PREVIEW")
        record = read(folder / "build.json")
        record.update({"document_type": "animation_draft_preview",
                       "schema_version": 1, "draft": True,
                       "profile": "FRAME_ANIMATION_V1", "format": fmt,
                       "output_frames": output_frames, "warnings": [],
                       "audio": {"sha256": digest(master),
                                 "path": "snapshot/" + config["audio"]["path"]},
                       "entries": report, "frames": {}})
        try:
            for relative in (config["audio"]["path"], "timeline/edit.json",
                             "manifest/animation_assets.json",
                             "manifest/shots.json", "project.yaml"):
                source = safe_path(p, relative)
                if source.is_file():
                    copied = capture(source, folder / "snapshot" / relative)
                    if relative == config["audio"]["path"] and copied != config["audio"]["sha256"]:
                        raise FilmError("Master changed before snapshot")
            # Freeze every member byte the compose can touch into the build,
            # then repoint members at the snapshot copies so a member changed
            # mid-compile cannot leak into the draft frames.
            for entry in resolved.values():
                if entry is None:
                    continue
                for frame_index, source in sorted(entry["members"].items()):
                    if not entry["used_start"] <= frame_index < entry["used_end"]:
                        continue
                    relative = source.relative_to(p)
                    copied = capture(source, folder / "snapshot" / relative)
                    if copied != entry["member_sha256"][frame_index]:
                        raise FilmError(
                            f"Resolved member changed before snapshot: {relative}")
                    entry["members"][frame_index] = folder / "snapshot" / relative
            frames_dir = folder / "draft_frames"
            frames_dir.mkdir()
            incomplete = 0
            frame_map = []
            member_warnings = {}
            for index in range(output_frames):
                if progress:
                    progress(index, output_frames, f"frame {index + 1}")
                frame, sources, operations, notes = _compose(
                    index, layout, resolved, fmt["width"], fmt["height"])
                for note in notes:
                    member_warnings.setdefault(note)
                name = frame_filename(index)
                frame.save(frames_dir / name)
                incomplete += any(not s["resolved"] for s in sources)
                frame_map.append({"frame_index": index,
                                  "file": f"draft_frames/{name}",
                                  "output_sha256": digest(frames_dir / name),
                                  "sources": sources, "operations": operations})
            for entry in resolved.values():
                if entry is None:
                    continue
                for frame_index in range(entry["used_start"], entry["used_end"]):
                    snap = entry["members"].get(frame_index)
                    if snap is None or digest(snap) != entry["member_sha256"][frame_index]:
                        raise FilmError("Snapshotted member changed during draft compile")
            map_path = folder / "draft_frame_map.jsonl"
            with map_path.open("wb") as stream:
                for row in frame_map:
                    stream.write(canon_bytes(row))
            target = folder / "DRAFT_PREVIEW.mp4"
            _encode(frames_dir, output_frames, fmt, master, target, seconds)
            if digest(master) != config["audio"]["sha256"]:
                raise FilmError("Master changed during preview compile")
            incomplete_entries = [r["instance_id"] for r in report if not r["resolved"]]
            if incomplete_entries:
                record["warnings"].append(
                    "Unresolved sequences rendered as labelled placeholders: "
                    + ", ".join(incomplete_entries))
            record["warnings"].extend(member_warnings)
            record["warnings"].append(
                "Draft Preview: no subtitle overlay, no review, no approval — "
                "not a Final candidate")
            record["frames"] = {"total": output_frames,
                                "placeholder_frames": incomplete,
                                "resolved_frames": output_frames - incomplete}
            record.update(output="DRAFT_PREVIEW.mp4",
                          draft_frame_map="draft_frame_map.jsonl",
                          toolchain_note="PNG sequence -> image2 -framerate "
                          f"{fmt['fps']} -start_number 1 -> master AAC mux")
            seal_build(folder, record)
            if progress:
                progress(output_frames, output_frames, folder.name)
            return {"status": "COMPLETE", "build_id": folder.name,
                    "build_dir": str(folder), "output": str(target),
                    "mode": "PREVIEW", "draft": True,
                    "resolved_entries": sum(1 for r in report if r["resolved"]),
                    "incomplete_entries": incomplete_entries,
                    "frames": record["frames"], "warnings": record["warnings"]}
        except Exception as exc:
            record.update(status="FAILED", error=str(exc), failed_at=now())
            write(folder / "build.json", record)
            raise
