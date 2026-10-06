"""FRAME_ANIMATION_V1 draft Preview (ANIM-003).

Renders the canonical `timeline/edit.json` onto the draft canvas using each
shot's assigned frame sequence, with LINEAR_INTERIOR_V1 weights inside
transition overlaps and a labelled placeholder wherever a shot's sequence is
unresolved (missing, changed, tampered or too short). Output is a draft MP4
muxed with the original master audio, plus the composed PNG frames and a
per-frame `frame_map.jsonl` under the new build directory.

This is a draft review artifact: it performs no provider calls, creates no
approval and does not satisfy Final. Full Build 2 inventory, subtitle
application and seal semantics arrive with their own nodes.
"""
from pathlib import Path
import copy
import json
from fractions import Fraction

from PIL import Image, ImageDraw

from .animation_assets import resolve_shot_sequence
from .animation_schema import (load_animation_timeline, require_animation_profile,
                               validate_animation_timeline)
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
                "used_start": entry["used_source_range"][0],
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
    """Verified member pixels for one local source frame, or None."""
    if resolved_entry is None:
        return None
    path = resolved_entry["members"].get(local_index)
    if path is None:
        return None
    try:
        with Image.open(path) as im:
            return im.convert("RGB")
    except Exception:
        return None


def _contributors(frame_index, layout):
    """([(row, weight)], transition) covering one output frame; max two rows."""
    for transition in layout["transitions"]:
        start, end = transition["output_range"]
        if start <= frame_index < end:
            overlap = transition["overlap_frames"]
            incoming = Fraction(frame_index - start + 1, overlap + 1)
            by_id = {r["instance_id"]: r for r in layout["entries"]}
            return ([(by_id[transition["from_instance"]], 1 - incoming),
                     (by_id[transition["to_instance"]], incoming)],
                    transition)
    row = next(r for r in layout["entries"]
               if r["output_range"][0] <= frame_index < r["output_range"][1])
    return [(row, Fraction(1))], None


def _compose(frame_index, layout, resolved, width, height):
    """One output frame plus its draft frame_map source rows."""
    pairs, transition = _contributors(frame_index, layout)
    layer, sources = None, []
    for row, weight in pairs:
        entry = resolved[row["instance_id"]]
        local = frame_index - row["output_range"][0]
        source_index = entry["used_start"] + local if entry is not None else None
        image = _member_image(entry, source_index)
        present = image is not None
        if image is None:
            image = _placeholder(width, height,
                                 f"{row['shot_id']} / NO SEQUENCE — DRAFT")
        fitted = _fit(image, width, height)
        layer = fitted if layer is None else Image.blend(layer, fitted, float(weight))
        sources.append({"instance_id": row["instance_id"], "shot_id": row["shot_id"],
                        "local_frame_index": source_index,
                        "weight": [weight.numerator, weight.denominator],
                        "resolved": present})
    operations = ["DRAFT_TAG"]
    if transition is not None:
        operations += ["CROSSFADE", "LINEAR_INTERIOR_V1"]
    if any(not s["resolved"] for s in sources):
        operations.append("PLACEHOLDER_DRAFT")
    return _tag(layer, f"DRAFT {frame_index + 1}"), sources, operations


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
        record.update({"document_type": "animation_build", "schema_version": 2,
                       "storage_profile": "LOCAL_FULL", "draft": True,
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
            frames_dir = folder / "draft_frames"
            frames_dir.mkdir()
            incomplete = 0
            frame_map = []
            for index in range(output_frames):
                if progress:
                    progress(index, output_frames, f"frame {index + 1}")
                frame, sources, operations = _compose(index, layout, resolved,
                                                      fmt["width"], fmt["height"])
                name = f"F_{index + 1:06d}.png"
                frame.save(frames_dir / name)
                incomplete += any(not s["resolved"] for s in sources)
                frame_map.append({"frame_index": index,
                                  "file": f"draft_frames/{name}",
                                  "output_sha256": digest(frames_dir / name),
                                  "sources": sources, "operations": operations})
            map_path = folder / "frame_map.jsonl"
            with map_path.open("w", encoding="utf-8", newline="\n") as stream:
                for row in frame_map:
                    stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True)
                                 + "\n")
            target = folder / "DRAFT_PREVIEW.mp4"
            _encode(frames_dir, output_frames, fmt, master, target, seconds)
            if digest(master) != config["audio"]["sha256"]:
                raise FilmError("Master changed during preview compile")
            incomplete_entries = [r["instance_id"] for r in report if not r["resolved"]]
            if incomplete_entries:
                record["warnings"].append(
                    "Unresolved sequences rendered as labelled placeholders: "
                    + ", ".join(incomplete_entries))
            record["warnings"].append(
                "Draft Preview: no subtitle overlay, no review, no approval — "
                "not a Final candidate")
            record["frames"] = {"total": output_frames,
                                "placeholder_frames": incomplete,
                                "resolved_frames": output_frames - incomplete}
            record.update(output="DRAFT_PREVIEW.mp4",
                          frame_map="frame_map.jsonl",
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
