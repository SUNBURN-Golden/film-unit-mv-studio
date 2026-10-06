"""FRAME_ANIMATION_V1 Final-candidate compile and Build 2 replay (ANIM-006).

`compile_final_candidate` is the strict path: every timeline entry resolves
to its pinned sequence revision, every cut and transition carries a current
APPROVED `animation_review` record, and the reviewed lyric cues plus font
coverage clear the same strict lyrics contract as legacy Final. There is no
placeholder and no draft fallback — a missing member, an unreviewed target
or a stale binding refuses the build before any frame is composed.

Outputs follow schema section 9: the delivery PNG sequence
`final_frames/F_000001.png...` (the composed frames with the reviewed
subtitles overlaid on the global timeline), `frame_map.jsonl` binding each
output PNG's hash to its sources and the review ids that authorized them,
`MASTER_CLEAN.mp4` encoded from the clean compose and `MASTER_SUBBED.mp4`
encoded by image2 `-framerate <fps> -start_number 1` from the delivery
sequence with the original master as AAC — same frame version, two
deliverables. Per schema section 16, only the delivery PNG sequence is
stored; the clean sequence is bound by its `clean_sequence_root` digest.

The sealed build is a `animation_build` schema 2 record with
`storage_profile: "LOCAL_FULL"`: it snapshots the timeline, the asset
registry, every used member byte, the master audio, the reviewed lyrics and
the subtitle font, so `replay_build` can re-verify the inventory, recompose
the clean sequence from the archived members and re-encode the deliveries
with the live project hidden.

A Final candidate is not an approval: `candidate_state` reports
FINAL_CANDIDATE_READY and the FINAL_FILM review is a separate explicit
record bound to the sealed manifest and deliverable bytes.
"""
from pathlib import Path
import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from fractions import Fraction

from PIL import Image

from .animation_assets import resolve_shot_sequence, validate_registry
from .animation_review import require_current_reviews
from .animation_schema import (canon_bytes, load_animation_timeline,
                               read_canon, require_animation_profile)
from .builds import allocate_build, capture, seal_build, verify_build
from .compiler import media_check
from .core import (FilmError, digest, ffmpeg, now, project_mutex,
                   read, safe_path, write)
from .frame_clock import frame_filename
from .frame_sequence import member_map_for_entry
from .lyrics import export_subtitles, validate_lyrics
from .transitions import (attach_frame_outputs, audit_timeline,
                          composite_pair, plan_frame_map,
                          verify_frame_map, write_frame_map)

CANVAS_BG = (40, 43, 48)  # 0x282b30, the shared neutral pad


def _fit(image, width, height):
    """Scale a member onto the canvas, centered, neutral background padded."""
    source = image.convert("RGB")
    source.thumbnail((width, height), Image.LANCZOS)
    canvas = Image.new("RGB", (width, height), CANVAS_BG)
    canvas.paste(source, ((width - source.width) // 2,
                          (height - source.height) // 2))
    return canvas


def image_pixel_sha256(image):
    """Digest of decoded RGB pixels — encoder-independent frame identity."""
    rgb = image.convert("RGB")
    return hashlib.sha256(
        f"{rgb.width}x{rgb.height}:".encode() + rgb.tobytes()).hexdigest()


def frame_pixel_sha256(path):
    with Image.open(path) as im:
        return image_pixel_sha256(im)


def sequence_root(role, digests):
    """One digest over the ordered (frame_index, pixel sha256) list."""
    return hashlib.sha256(canon_bytes(
        {"role": role,
         "frames": [{"frame_index": index, "sha256": value}
                    for index, value in enumerate(digests)]})).hexdigest()


def _check_frame_sequence(frames_dir, count):
    """The delivery sequence is exactly F_000001.png .. F_<count>.png."""
    expected = {frame_filename(i) for i in range(count)}
    found = {f.name for f in Path(frames_dir).glob("*.png")}
    if found != expected:
        missing = sorted(expected - found)[:3]
        extra = sorted(found - expected)[:3]
        raise FilmError(f"Delivery frame sequence is not contiguous "
                        f"F_000001..F_{count:06d} (missing {missing}, "
                        f"extra {extra})")


def _resolve_strict(p, document, audit, exposure):
    """Resolve every entry to its pinned revision; unresolved is an error."""
    entries = {e["instance_id"]: e for e in document["entries"]}
    resolved = {}
    for row in audit["entries"]:
        entry = entries[row["instance_id"]]
        found = resolve_shot_sequence(p, entry["shot_id"],
                                      entry["used_source_range"],
                                      entry["unused_handles"],
                                      entry["sequence_revision"])
        resolved[row["instance_id"]] = {
            "members": dict(found["members"]),
            "member_map": member_map_for_entry(
                entry, (exposure or {}).get(row["instance_id"])),
            "member_sha256": {f.get("frame_index"): f["sha256"]
                              for f in found["record"]["files"]},
            "used_start": entry["used_source_range"][0],
            "used_end": entry["used_source_range"][1],
            "pin": {"asset_id": found["asset_id"],
                    "revision": found["revision"],
                    "content_sha256": found["content_sha256"]}}
    return resolved


def _compose_row(row, member_files, width, height):
    """Compose one output frame strictly from its frame_map source rows."""
    layer = None
    for source in row["sources"]:
        members = member_files.get(source["instance_id"], {})
        path = members.get(source["local_frame_index"])
        if path is None or not Path(path).is_file():
            raise FilmError(
                f"{source['shot_id']} frame {row['frame_index']}: member "
                f"{source['local_frame_index']} is missing — no placeholder "
                "on the Final path")
        try:
            with Image.open(path) as im:
                image = im.convert("RGB")
        except Exception as e:
            raise FilmError(f"{source['shot_id']} member "
                            f"{source['local_frame_index']} failed to "
                            f"decode ({Path(path).name}): {e}") from e
        fitted = _fit(image, width, height)
        # Weights sum to 1 pairwise; composite_pair takes the incoming share.
        layer = fitted if layer is None \
            else composite_pair(layer, fitted, Fraction(*source["weight"]))
    if layer is None:
        raise FilmError(f"frame {row['frame_index']} has no sources")
    return layer


def _burn_frames(frames_dir, out_dir, build_dir, fps):
    """Burn the captured ASS onto a PNG sequence on the global timeline.

    The cue times come from the original reviewed lyric cues — the overlay
    never re-derives timing from cut milliseconds and adds no frames.
    """
    frames_dir, out_dir = Path(frames_dir), Path(out_dir)
    ass = Path(build_dir) / "lyrics.ass"
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".subtitle-",
                                     dir=Path(build_dir)) as work:
        work = Path(work)
        shutil.copyfile(ass, work / "lyrics.ass")
        fonts = Path(build_dir) / "subtitle_fonts"
        if fonts.is_dir():
            shutil.copytree(fonts, work / "fonts")
        (work / "fonts").mkdir(exist_ok=True)
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                   "-nostdin", "-y", "-filter_threads", "1",
                   "-framerate", str(fps), "-start_number", "1",
                   "-i", str(frames_dir / "F_%06d.png"),
                   "-vf", "ass=filename=lyrics.ass:fontsdir=fonts",
                   "-start_number", "1", str(out_dir / "F_%06d.png")]
        result = subprocess.run(
            command, cwd=work, capture_output=True, timeout=1800,
            creationflags=subprocess.CREATE_NO_WINDOW
            if os.name == "nt" else 0)
        if result.returncode:
            raise FilmError(
                result.stderr.decode(errors="replace")[-4000:])
    return out_dir


def _encode_master(frames_dir, count, fmt, master, target, seconds):
    """image2 encode with the contracted explicit framerate/start number."""
    target = Path(target)
    pending = target.with_suffix(".pending.mp4")
    ffmpeg(["-xerror", "-framerate", str(fmt["fps"]), "-start_number", "1",
            "-i", str(Path(frames_dir) / "F_%06d.png"), "-map", "0:v:0",
            "-an", "-r", str(fmt["fps"]), "-c:v", "libx264",
            "-preset", "veryfast", "-crf", str(fmt["crf"]),
            "-pix_fmt", "yuv420p", "-threads", "2", pending])
    media_check(pending, count, fmt)
    muxed = target.with_suffix(".mux.mp4")
    ffmpeg(["-xerror", "-i", pending, "-i", master,
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "320k", "-t", f"{seconds:.3f}",
            "-map_metadata", "-1", "-movflags", "+faststart", muxed])
    info = media_check(muxed, count, fmt, audio=True)
    sound = next(s for s in info["streams"] if s["codec_type"] == "audio")
    if abs(float(sound.get("duration", 0)) - seconds) > .1:
        raise FilmError("Master audio does not cover the animation timeline")
    muxed.replace(target)
    pending.unlink(missing_ok=True)
    return target


def _check_delivery(path, fmt, output_frames, seconds):
    """The delivery MP4 keeps the exact frame clock and the master audio."""
    info = media_check(path, output_frames, fmt, audio=True)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    if video.get("avg_frame_rate") != f"{fmt['fps']}/1" \
            or video.get("r_frame_rate") != f"{fmt['fps']}/1":
        raise FilmError("Delivery must run at the project frame rate")
    if abs(float(video.get("start_time", 0))) > 0.001:
        raise FilmError("Delivery PTS must start at 0")
    if abs(float(info["format"]["duration"]) - seconds) \
            > 1 / fmt["fps"] + .03:
        raise FilmError("Delivery duration exceeds one-frame tolerance")
    sound = next(s for s in info["streams"] if s["codec_type"] == "audio")
    if abs(float(sound.get("duration", 0)) - seconds) > .1:
        raise FilmError("Master audio does not cover the animation timeline")
    return info


def compile_final_candidate(project, exposure=None, progress=None):
    """Compile a reviewed FRAME_ANIMATION_V1 edit into a Final candidate.

    `exposure` maps an instance_id to that cut's drawing-track exposure
    schedules; the default is the identity (ones) map, and whatever is used
    is what the cut reviews bind.
    """
    p = Path(project).resolve()
    with project_mutex(p):
        config = require_animation_profile(p)
        fmt = copy.deepcopy(config["format"])
        fps, width, height = fmt["fps"], fmt["width"], fmt["height"]
        document = load_animation_timeline(p)
        output_frames = config["animation"]["output_frames"]
        audit = audit_timeline(document, output_frames)
        master = safe_path(p, config["audio"]["path"])
        if digest(master) != config["audio"]["sha256"]:
            raise FilmError("Master audio hash mismatch")
        analysis = read(p / "analysis/audio.json")
        if analysis.get("master_sha256", config["audio"]["sha256"]) \
                != config["audio"]["sha256"]:
            raise FilmError("Audio analysis belongs to a different master")
        duration_ms = analysis["duration_ms"]
        seconds = output_frames / fps
        if abs(duration_ms / 1000 - seconds) > .1:
            raise FilmError("Master audio does not cover the animation "
                            "timeline")
        lyric_document, lyric_warnings = validate_lyrics(
            p, duration_ms, strict=True)
        # Every entry resolves to its pinned revision and every cut and
        # transition holds a current APPROVED record — before a build is
        # allocated, matching the legacy strict gates.
        plan = plan_frame_map(p, exposure)
        rows = plan["rows"]
        reviews = require_current_reviews(p, exposure)
        folder = allocate_build(p, "FINAL_CANDIDATE")
        record = read(folder / "build.json")
        record.update({"document_type": "animation_build",
                       "schema_version": 2, "storage_profile": "LOCAL_FULL",
                       "profile": "FRAME_ANIMATION_V1",
                       "mode": "FINAL_CANDIDATE", "draft": False,
                       "format": fmt,
                       "frame_clock": {"fps": {"num": fps, "den": 1},
                                       "output_frames": output_frames},
                       "output_frames": output_frames,
                       "edit_digest": reviews["edit_digest"],
                       "audio": {"sha256": digest(master),
                                 "path": "snapshot/" + config["audio"]["path"],
                                 "codec": "aac"},
                       "entries": plan["entries"],
                       "reviews": reviews,
                       "warnings": list(lyric_warnings)})
        try:
            for relative in (config["audio"]["path"], "timeline/edit.json",
                             "timeline/derived.json",
                             "manifest/animation_assets.json",
                             "manifest/shots.json", "project.yaml",
                             "analysis/audio.json", "lyrics/lyrics_source.txt",
                             "lyrics/lyrics_timed.json", "input/lyrics.txt",
                             "input/brief.md", "bible/story.md",
                             "bible/style_bible.yaml"):
                source = safe_path(p, relative)
                if source.is_file():
                    copied = capture(source, folder / "snapshot" / relative)
                    if relative == config["audio"]["path"] \
                            and copied != config["audio"]["sha256"]:
                        raise FilmError("Master changed before snapshot")
            resolved = _resolve_strict(p, document, audit, exposure)
            # Freeze every member byte the compose can touch into the build,
            # then repoint members at the snapshot copies so a member changed
            # mid-compile cannot leak into the delivery frames.
            for entry in resolved.values():
                for frame_index, source in sorted(entry["members"].items()):
                    if not entry["used_start"] <= frame_index \
                            < entry["used_end"]:
                        continue
                    relative = source.relative_to(p)
                    copied = capture(source, folder / "snapshot" / relative)
                    if copied != entry["member_sha256"][frame_index]:
                        raise FilmError(
                            f"Resolved member changed before snapshot: "
                            f"{relative}")
                    entry["members"][frame_index] = \
                        folder / "snapshot" / relative
            member_files = {iid: entry["members"]
                            for iid, entry in resolved.items()}
            clean_dir = folder / ".clean_frames"
            clean_dir.mkdir()
            clean_digests = []
            for index, row in enumerate(rows):
                if progress:
                    progress(index, output_frames, f"frame {index + 1}")
                frame = _compose_row(row, member_files, width, height)
                clean_digests.append(image_pixel_sha256(frame))
                frame.save(clean_dir / frame_filename(index))
            for entry in resolved.values():
                for frame_index in range(entry["used_start"],
                                         entry["used_end"]):
                    snap = entry["members"].get(frame_index)
                    if snap is None \
                            or digest(snap) \
                            != entry["member_sha256"][frame_index]:
                        raise FilmError(
                            "Snapshotted member changed during Final compile")
            subs = export_subtitles(p, folder, duration_ms, strict=True)
            report_file = read(folder / "subtitle_report.json")
            final_dir = folder / "final_frames"
            _burn_frames(clean_dir, final_dir, folder, fps)
            _check_frame_sequence(final_dir, output_frames)
            subbed_digests = [frame_pixel_sha256(final_dir / frame_filename(i))
                              for i in range(output_frames)]
            subtitle_recipe = hashlib.sha256(canon_bytes({
                "type": "SUBTITLE_OVERLAY",
                "ass_sha256": digest(subs["ass"]),
                "timing_sha256": report_file["timing_sha256"],
                "font_sha256": report_file["font"].get("sha256"),
                "timeline": "global"})).hexdigest()
            for row in rows:
                fades = [o for o in row["operations"]
                         if o.get("type") == "CROSSFADE"]
                row["review_refs"] = \
                    [reviews["cuts"][s["instance_id"]]
                     for s in row["sources"]] \
                    + [reviews["transitions"][o["transition_id"]]
                       for o in fades]
                row["operations"].append(
                    {"type": "SUBTITLE_OVERLAY",
                     "recipe_digest": subtitle_recipe})
            attach_frame_outputs(rows, final_dir)
            map_path = write_frame_map(folder / "frame_map.jsonl", rows)
            verify_frame_map(rows, document, complete=True)
            clean = _encode_master(clean_dir, output_frames, fmt, master,
                                   folder / "MASTER_CLEAN.mp4", seconds)
            _check_delivery(clean, fmt, output_frames, seconds)
            subbed = _encode_master(final_dir, output_frames, fmt, master,
                                    folder / "MASTER_SUBBED.mp4", seconds)
            _check_delivery(subbed, fmt, output_frames, seconds)
            if digest(master) != config["audio"]["sha256"]:
                raise FilmError("Master changed during Final compile")
            shutil.rmtree(clean_dir)
            record["lyrics"] = {
                "source_sha256": lyric_document["source_sha256"],
                "timing_sha256": report_file["timing_sha256"],
                "lyrics_review_sha256":
                    report_file["lyrics_review_sha256"],
                "cues": len(lyric_document["cues"])}
            record["frame_map"] = "frame_map.jsonl"
            record["frame_map_sha256"] = digest(map_path)
            record["sequences"] = {
                "delivery_dir": "final_frames",
                "delivery": "subbed",
                "frame_count": output_frames,
                "naming": "F_%06d.png numbered from 1",
                "clean_sequence_root":
                    sequence_root("clean", clean_digests),
                "subbed_sequence_root":
                    sequence_root("subbed", subbed_digests),
                "clean_frames_stored": False}
            record["subtitles"] = {
                "ass": "lyrics.ass", "srt": "lyrics.srt",
                "ass_sha256": digest(subs["ass"]),
                "srt_sha256": digest(subs["srt"]),
                "recipe_digest": subtitle_recipe,
                "font": report_file["font"],
                "timeline": "global — original cue milliseconds, "
                            "never re-derived from cut frames"}
            record["outputs"] = {
                "clean": {"file": "MASTER_CLEAN.mp4",
                          "sha256": digest(clean)},
                "subbed": {"file": "MASTER_SUBBED.mp4",
                           "sha256": digest(subbed)}}
            record["candidate_state"] = "FINAL_CANDIDATE_READY"
            record["warnings"].append(
                "Final candidate: cut/transition reviews bound the current "
                "digests; the FINAL_FILM approval is a separate record bound "
                "to this sealed build")
            seal_build(folder, record)
            if progress:
                progress(output_frames, output_frames, folder.name)
            return {"status": "COMPLETE", "build_id": folder.name,
                    "build_dir": str(folder), "mode": "FINAL_CANDIDATE",
                    "candidate_state": "FINAL_CANDIDATE_READY",
                    "output": str(subbed), "clean": str(clean),
                    "frames": {"total": output_frames,
                               "placeholder_frames": 0,
                               "resolved_frames": output_frames},
                    "frame_map": str(map_path),
                    "warnings": record["warnings"]}
        except Exception as exc:
            record.update(status="FAILED", error=str(exc), failed_at=now())
            write(folder / "build.json", record)
            raise


def _read_frame_map(path):
    rows = []
    lines = Path(path).read_bytes().split(b"\n")
    if lines[-1] != b"":
        raise FilmError("frame_map.jsonl must end with a single LF")
    for index, line in enumerate(lines[:-1]):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise FilmError(f"frame_map line {index + 1} is not JSON") from e
        if line + b"\n" != canon_bytes(row):
            raise FilmError(f"frame_map line {index + 1} is not canonical")
        rows.append(row)
    return rows


def replay_local_full(build_dir, out, record):
    """Reproduce a Build 2 LOCAL_FULL delivery from the sealed archive only.

    Two paths cross-check: the stored delivery PNG sequence re-encodes to
    MASTER_SUBBED.mp4, and the archived members plus the frame_map recipe
    recompose the clean sequence — recomputed pixel roots must equal the
    digests the seal recorded. The live project is never touched.
    """
    build, out = Path(build_dir), Path(out)
    if record.get("document_type") != "animation_build" \
            or record.get("schema_version") != 2:
        raise FilmError("Only Build 2 animation_build records replay here")
    if record.get("storage_profile") != "LOCAL_FULL":
        raise FilmError(f"Build 2 storage_profile "
                        f"{record.get('storage_profile')} is not implemented")
    if not verify_build(build)["valid"]:
        raise FilmError("Build 2 archive failed its inventory check")
    snapshot = build / "snapshot"
    document = read_canon(snapshot / "timeline/edit.json")
    registry = validate_registry(
        read_canon(snapshot / "manifest/animation_assets.json"))
    fmt = record["format"]
    fps, width, height = fmt["fps"], fmt["width"], fmt["height"]
    output_frames = record["output_frames"]
    seconds = output_frames / fps
    rows = _read_frame_map(build / "frame_map.jsonl")
    verify_frame_map(rows, document, complete=True)
    final_dir = build / "final_frames"
    _check_frame_sequence(final_dir, output_frames)
    for row in rows:
        name = Path(row["file"]).name
        if digest(final_dir / name) != row["output_sha256"]:
            raise FilmError(f"Archived delivery frame {name} does not match "
                            "its frame_map hash")
    # Recompose the clean sequence from the archived members: the frame_map
    # rows carry each output frame's member index and rational weight.
    member_files = {}
    for entry in document["entries"]:
        pin = registry["assignments"].get(entry["shot_id"])
        if pin is None:
            raise FilmError(f"Archive has no assignment for "
                            f"{entry['shot_id']}")
        asset = registry["assets"][pin["asset_id"]]["revisions"][
            str(pin["revision"])]
        member_files[entry["instance_id"]] = {
            f["frame_index"]: snapshot / f["relative_name"]
            for f in asset["files"]}
    work = Path(tempfile.mkdtemp(prefix=".recompose-", dir=out))
    try:
        clean_dir = work / "clean"
        clean_dir.mkdir()
        clean_digests = []
        for row in rows:
            frame = _compose_row(row, member_files, width, height)
            clean_digests.append(image_pixel_sha256(frame))
            frame.save(clean_dir / frame_filename(row["frame_index"]))
        clean_root = sequence_root("clean", clean_digests)
        if clean_root != record["sequences"]["clean_sequence_root"]:
            raise FilmError("Recomposed clean sequence does not match the "
                            "sealed clean_sequence_root")
        # Re-burn the captured cues and prove the stored delivery PNGs are
        # the same visual version on the same timeline.
        sub_dir = work / "subbed"
        _burn_frames(clean_dir, sub_dir, build, fps)
        stored_digests = []
        for index in range(output_frames):
            name = frame_filename(index)
            stored = frame_pixel_sha256(final_dir / name)
            replayed = frame_pixel_sha256(sub_dir / name)
            if stored != replayed:
                raise FilmError(f"Delivery frame {name} does not reproduce "
                                "the archived clean frame plus the reviewed "
                                "subtitle overlay")
            stored_digests.append(stored)
        if sequence_root("subbed", stored_digests) \
                != record["sequences"]["subbed_sequence_root"]:
            raise FilmError("Stored delivery sequence does not match the "
                            "sealed subbed_sequence_root")
        master = build / record["audio"]["path"]
        if not master.is_file():
            raise FilmError("Archived master audio is missing")
        subbed = _encode_master(final_dir, output_frames, fmt, master,
                                out / "MASTER_SUBBED.mp4", seconds)
        _check_delivery(subbed, fmt, output_frames, seconds)
        clean = _encode_master(clean_dir, output_frames, fmt, master,
                               out / "MASTER_CLEAN.mp4", seconds)
        _check_delivery(clean, fmt, output_frames, seconds)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for name in ("lyrics.ass", "lyrics.srt", "lyrics_source.txt",
                 "lyrics_timed.json", "subtitle_report.json",
                 "frame_map.jsonl"):
        source = build / name
        if source.is_file():
            shutil.copyfile(source, out / name)
    fonts = build / "subtitle_fonts"
    if fonts.is_dir():
        shutil.copytree(fonts, out / "subtitle_fonts")
    result = {"build_id": record["build_id"], "mode": record["mode"],
              "replayed_from": str(build),
              "frames": output_frames,
              "verified": {"frame_map_rows": len(rows),
                           "clean_sequence_root": clean_root,
                           "subbed_sequence_root":
                               record["sequences"]["subbed_sequence_root"]},
              "output": str(subbed), "clean": str(clean)}
    write(out / "replay.json", result)
    return result
