"""Whole-song local compiler. Generation and its spending guards remain separate."""
from collections import Counter
from pathlib import Path
import copy
import shutil

from PIL import Image, ImageDraw

from .core import (FilmError, atomic_text, digest, ffmpeg, frame_at, now, object_hash,
                   probe, production_fingerprint, project_mutex, read, require_lock,
                   safe_path, validate_manifest, write)
from .builds import allocate_build, capture, seal_build
from .resolver import candidates, checked_source, register_asset, shot_hash


def import_asset(project, shot_id, source, kind="draft", reviewer="", evidence="", source_in_ms=0):
    """Explicit local take selection. Named final imports require human attestation.

    Labels alone do not approve a take. Reviewer/evidence are checked again against
    the exact bytes, shot definition and current production at final compile time.
    """
    p = Path(project)
    with project_mutex(p):
        shots = read(p / "manifest/shots.json")
        shot = next((s for s in shots if s["id"] == shot_id), None)
        if shot is None:
            raise FilmError("Unknown shot ID")
        return register_asset(p, shot, source, kind, reviewer=reviewer, evidence=evidence,
                              source_in_ms=source_in_ms)


def media_check(path, frames, fmt, audio=False):
    info = probe(path)
    videos = [s for s in info["streams"] if s["codec_type"] == "video"]
    sounds = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if len(videos) != 1 or len(sounds) != int(audio):
        raise FilmError("Unexpected number of media streams")
    v = videos[0]
    if int(v.get("nb_frames", 0)) != frames or [v["width"], v["height"]] != [fmt["width"], fmt["height"]]:
        raise FilmError("Decoded frame count or dimensions do not match timeline")
    from fractions import Fraction
    if abs(float(Fraction(v["avg_frame_rate"])) - fmt["fps"]) > 0.001:
        raise FilmError("Frame rate does not match timeline")
    return info


def mux_timeline(clips, master, target, duration_ms, fmt):
    """Map only visual streams and the original master, with no time stretching."""
    target = Path(target)
    work = target.parent / "assembly"
    work.mkdir(exist_ok=True)
    lines = ["ffconcat version 1.0"]
    for i, clip in enumerate(clips):
        name = f"clip_{i:05d}.mp4"
        # Work copies may be hardlinks; immutable snapshots themselves never are.
        dest = work / name
        try:
            dest.hardlink_to(Path(clip).resolve())
        except OSError:
            shutil.copyfile(clip, dest)
        lines.append(f"file {name}")
    listing = work / "timeline.ffconcat"
    atomic_text(listing, "\n".join(lines) + "\n")
    pending = target.with_suffix(".pending.mp4")
    ffmpeg(["-xerror", "-f", "concat", "-safe", "1", "-i", listing, "-i", master,
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "320k",
            "-t", f"{duration_ms / 1000:.3f}", "-map_metadata", "-1", "-movflags", "+faststart", pending])
    info = media_check(pending, frame_at(duration_ms, fmt["fps"]), fmt, audio=True)
    if abs(float(info["format"]["duration"]) - duration_ms / 1000) > 1 / fmt["fps"] + .03:
        raise FilmError("Muxed duration exceeds one-frame tolerance")
    sound = next(s for s in info["streams"] if s["codec_type"] == "audio")
    if abs(float(sound.get("duration", 0)) - duration_ms / 1000) > .1:
        raise FilmError("Master audio does not cover the requested timeline")
    pending.replace(target)
    shutil.rmtree(work)
    return target


def _placeholder(target, shot):
    im = Image.new("RGB", (640, 480), "#282b30")
    draw = ImageDraw.Draw(im)
    draw.rectangle((40, 40, 600, 440), outline="#737983", width=2)
    draw.text((65, 170), f"{shot['id']} / MISSING VISUAL", fill="white", font_size=25)
    draw.text((65, 225), f"{shot['in_ms']} - {shot['out_ms']} ms", fill="#b9bfc8", font_size=20)
    im.save(target)


def _render_source(source, target, shot, candidate, fmt):
    frames = frame_at(shot["out_ms"], fmt["fps"]) - frame_at(shot["in_ms"], fmt["fps"])
    w, h, fps = fmt["width"], fmt["height"], fmt["fps"]
    vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x282b30,setsar=1"
    if candidate["kind"] in {"final", "draft"}:
        start = candidate.get("source_in_ms", 0)
        args = ["-xerror", "-ss", f"{start / 1000:.6f}", "-i", source]
        vf += f",fps={fps},trim=end_frame={frames},setpts=PTS-STARTPTS"
    else:
        args = ["-loop", "1", "-framerate", fps, "-i", source]
        effect = shot.get("motion", {}).get("local_effect", "hold")
        if candidate["kind"] != "placeholder" and effect in {"pan", "zoom"}:
            if shot.get("camera", {}).get("movement") == "none":
                raise FilmError("Pan/zoom conflicts with a locked camera")
            zoom = f"1+0.035*on/{frames}" if effect == "zoom" else "1.04"
            x = "iw/2-iw/zoom/2" if effect == "zoom" else f"(iw-iw/zoom)*on/{frames}"
            vf += f",zoompan=z='{zoom}':x='{x}':y='ih/2-ih/zoom/2':d=1:s={w}x{h}:fps={fps}"
    ffmpeg([*args, "-map", "0:v:0", "-an", "-vf", vf, "-frames:v", frames, "-r", fps,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", fmt["crf"], "-pix_fmt", "yuv420p",
            "-threads", "2", target])
    media_check(target, frames, fmt)


def _resolve_clip(p, build, shot, fmt, production_id, strict):
    failures = []
    cache_dir = p / "render/compile_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for candidate in candidates(p, shot, production_id):
        if strict and not candidate["approved"]:
            failures.append(f"{candidate['kind']}: not approved for Final")
            continue
        source_snapshot = None
        try:
            if candidate["kind"] == "placeholder":
                source_snapshot = build / "assets" / f"{shot['id']}_placeholder.png"
                source_snapshot.parent.mkdir(exist_ok=True)
                _placeholder(source_snapshot, shot)
                source_sha = digest(source_snapshot)
            else:
                original, source_sha = checked_source(p, candidate)
                source_snapshot = build / "assets" / (source_sha + original.suffix.lower())
                if capture(original, source_snapshot) != source_sha:
                    raise FilmError("Selected source changed before snapshot")
            key = object_hash({"compiler": "0.3.0", "shot": shot_hash(shot), "source": source_sha,
                               "kind": candidate["kind"], "offset": candidate.get("source_in_ms", 0), "format": fmt})
            cached = cache_dir / (key + ".mp4")
            metadata = read(cached.with_suffix(".json"), {})
            cache_ok = cached.exists() and metadata.get("sha256") == digest(cached)
            if not cache_ok:
                pending = cached.with_suffix(".pending.mp4")
                _render_source(source_snapshot, pending, shot, candidate, fmt)
                pending.replace(cached)
                write(cached.with_suffix(".json"), {"sha256": digest(cached)})
            else:
                media_check(cached, frame_at(shot["out_ms"], fmt["fps"]) - frame_at(shot["in_ms"], fmt["fps"]), fmt)
            clip = build / "timeline" / (shot["id"] + ".mp4")
            clip_sha = capture(cached, clip)
            return {"id": shot["id"], "kind": candidate["kind"], "approved": candidate["approved"],
                    "in_ms": shot["in_ms"], "out_ms": shot["out_ms"], "shot_hash": shot_hash(shot),
                    "source_path": str(source_snapshot.relative_to(build)), "source_sha256": source_sha,
                    "source_in_ms": candidate.get("source_in_ms", 0), "selection": candidate,
                    "clip_path": str(clip.relative_to(build)), "clip_sha256": clip_sha,
                    "fallback_reasons": failures, "cache_reused": cache_ok}
        except (FilmError, OSError, ValueError, KeyError) as exc:
            failures.append(f"{candidate['kind']}: {str(exc)[-300:]}")
    raise FilmError(f"{shot['id']} has no eligible Final asset: " + "; ".join(failures))


def compile_preview(project, quality="draft", progress=None):
    return _compile(project, False, quality, progress)


def compile_final(project, quality="final", progress=None):
    return _compile(project, True, quality, progress)


def _compile(project, strict, quality, progress):
    from .lyrics import export_subtitles, burn_subtitles, validate_lyrics
    p = Path(project).resolve()
    with project_mutex(p):
        config, audio = read(p / "project.yaml"), read(p / "analysis/audio.json")
        shots = read(p / "manifest/shots.json")
        fmt = copy.deepcopy(config["format"])
        if quality not in {"draft", "final"}:
            raise FilmError("Quality must be draft or final")
        if quality == "draft" and fmt["width"] > 960:
            fmt["height"] = round(fmt["height"] * 960 / fmt["width"] / 2) * 2
            fmt["width"] = 960
        duration = audio["duration_ms"]
        validate_manifest(shots, duration, fmt["fps"])
        master = safe_path(p, config["audio"]["path"])
        if digest(master) != config["audio"]["sha256"]:
            raise FilmError("Master audio hash mismatch")
        if audio.get("master_sha256", config["audio"]["sha256"]) != config["audio"]["sha256"]:
            raise FilmError("Audio analysis belongs to a different master")
        measured = float(probe(master)["format"]["duration"])
        # MP3 container padding differs slightly from decoded sample duration.
        if abs(measured - duration / 1000) > .1:
            raise FilmError("Analysis duration does not match the actual master audio")
        validate_lyrics(p, duration, strict=strict)
        lock = read(p / "manifest/locks.json", {})
        try:
            production_id = production_fingerprint(p)
        except (FilmError, OSError, KeyError):
            production_id = None
        if strict:
            lock = require_lock(p, "manual")
        folder = allocate_build(p, "FINAL" if strict else "PREVIEW")
        record = read(folder / "build.json")
        try:
            record.update(duration_ms=duration, format=fmt, quality=quality,
                          production_fingerprint=production_id, production_lock=lock,
                          lock_valid=bool(production_id and lock.get("fingerprint") == production_id),
                          audio={"sha256": digest(master), "path": "snapshot/" + config["audio"]["path"]},
                          warnings=[], shots=[], paid_generations=0,
                          audio_policy="Original master timeline; AAC 320 kbps is lossy. Source bytes unchanged.")
            # Capture the creative documents and references needed to understand a past cut.
            relatives = {"project.yaml", "analysis/audio.json", "input/brief.md", "input/lyrics.txt",
                         "manifest/shots.json", "manifest/sequence.json", "manifest/locks.json", config["audio"]["path"]}
            for directory in ("bible", "characters", "locations"):
                relatives.update(str(f.relative_to(p)) for f in (p / directory).rglob("*") if f.is_file())
            for shot in shots:
                relatives.update(shot.get("references", []))
            for relative in sorted(relatives):
                source = safe_path(p, relative)
                if source.is_file():
                    copied_sha = capture(source, folder / "snapshot" / relative)
                    if relative == config["audio"]["path"] and copied_sha != config["audio"]["sha256"]:
                        raise FilmError("Master changed before snapshot")
            subtitles = export_subtitles(p, folder, duration, strict=strict)
            record["warnings"].extend(subtitles["warnings"])
            if not record["lock_valid"]:
                record["warnings"].append("Production is not locked for this Preview revision")
            record["lyrics"] = {"timing_sha256": digest(subtitles["timed"]),
                                "source_sha256": digest(subtitles["source"]),
                                "font_report": subtitles.get("font_report", {})}
            for i, shot in enumerate(shots):
                if progress:
                    progress(i, len(shots), shot["id"])
                row = _resolve_clip(p, folder, shot, fmt, production_id, strict)
                record["shots"].append(row)
            record["asset_counts"] = dict(Counter(s["kind"] for s in record["shots"]))
            record["preview_incomplete_shots"] = [s["id"] for s in record["shots"] if not s["approved"]]
            clean = folder / "MASTER_CLEAN.mp4"
            mux_timeline([folder / s["clip_path"] for s in record["shots"]],
                         folder / record["audio"]["path"], clean, duration, fmt)
            subbed = folder / "MASTER_SUBBED.mp4"
            burn_subtitles(clean, subtitles["ass"], subbed, fmt)
            media_check(subbed, frame_at(duration, fmt["fps"]), fmt, audio=True)
            if strict:
                if require_lock(p, "manual")["fingerprint"] != production_id:
                    raise FilmError("Production revision changed during Final compile")
            if digest(master) != config["audio"]["sha256"]:
                raise FilmError("Master changed during compile")
            record.update(output="MASTER_SUBBED.mp4", clean="MASTER_CLEAN.mp4")
            seal_build(folder, record)
            if progress:
                progress(len(shots), len(shots), folder.name)
            return {"status": "COMPLETE", "build_id": folder.name, "build_dir": str(folder),
                    "output": str(subbed), "clean": str(clean), "asset_counts": record["asset_counts"],
                    "warnings": record["warnings"], "mode": record["mode"]}
        except Exception as exc:
            record.update(status="FAILED", error=str(exc), failed_at=now())
            write(folder / "build.json", record)
            raise
