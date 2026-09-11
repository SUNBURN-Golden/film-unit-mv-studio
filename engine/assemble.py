from pathlib import Path
import shutil
from .core import FilmError, atomic_text, digest, ffmpeg, probe, read, write


def assemble(project, clips, duration_ms, fmt, kind, output_name):
    p = Path(project)
    config = read(p / "project.yaml")
    master = p / config["audio"]["path"]
    if digest(master) != config["audio"]["sha256"]:
        raise FilmError("Master audio hash mismatch")
    work = p / "render/assembly"
    work.mkdir(exist_ok=True)
    # Controlled basenames keep ffconcat paths independent of user path quoting.
    lines = ["ffconcat version 1.0"]
    for i, clip in enumerate(clips):
        target = work / f"clip_{i:04d}.mp4"
        if target.exists():
            target.unlink()
        try:
            target.hardlink_to(Path(clip).resolve())
        except OSError:
            shutil.copyfile(clip, target)
        lines.append(f"file clip_{i:04d}.mp4")
    listing = work / "timeline.ffconcat"
    atomic_text(listing, "\n".join(lines) + "\n")
    silent = work / "video.mp4"
    ffmpeg(["-f", "concat", "-safe", "1", "-i", listing, "-map", "0:v:0", "-an", "-c:v", "copy", silent])
    out = p / "output" / output_name
    pending = out.with_name(out.stem + ".pending.mp4")
    ffmpeg(["-i", silent, "-i", master, "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "320k", "-t", f"{duration_ms/1000:.3f}",
        "-map_metadata", "-1", "-metadata", f"title=FILM UNIT | {kind}", "-movflags", "+faststart", pending])
    info = probe(pending)
    streams = info["streams"]
    if len([s for s in streams if s["codec_type"] == "audio"]) != 1 or len([s for s in streams if s["codec_type"] == "video"]) != 1:
        raise FilmError("Final output must have exactly one video and one master-audio track")
    if abs(float(info["format"]["duration"]) - duration_ms/1000) > 1/fmt["fps"] + 0.03:
        raise FilmError("Final duration outside one-frame tolerance")
    if digest(master) != config["audio"]["sha256"]:
        raise FilmError("Master modified unexpectedly")
    pending.replace(out)
    write(out.with_suffix(".json"), {"kind": kind, "requested_duration_ms": duration_ms, "format": fmt,
        "actual_duration_ms": round(float(info["format"]["duration"])*1000), "master_sha256": digest(master),
        "output_sha256": digest(out), "audio_policy": "Original master timeline, no normalization or time stretch. AAC 320 kbps encoding is lossy; source bytes remain unchanged.",
        "source_is_synthetic_test": config["audio"]["synthetic_test_audio"]})
    return out


def exports(project, master):
    p, master = Path(project), Path(master)
    folder = master.parent
    shutil.copyfile(master, folder / "YOUTUBE.mp4")
    ffmpeg(["-i", master, "-t", "30", "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-movflags", "+faststart", folder / "teaser_30s.mp4"])
    ffmpeg(["-i", master, "-frames:v", "1", "-q:v", "2", folder / "poster.jpg"])
    ffmpeg(["-i", master, "-frames:v", "1", "-vf", "scale=960:720", "-q:v", "2", folder / "thumbnail.jpg"])
    report = read(master.with_suffix(".json"))
    atomic_text(folder / "metadata.md", f"# FILM UNIT export\n\nType: {report['kind']}\n\nDuration: {report['actual_duration_ms']} ms\n\nSource: {'synthetic pipeline test signal' if report['source_is_synthetic_test'] else 'user supplied master'}\n\nVideo: H.264 / 4:3 / 1440×1080 / 24 fps / CRF 18 by default. See the JSON sidecar for actual settings.\n\nAudio: original master segment encoded once to AAC 320 kbps; no normalization, tempo change or generated audio.\n\nMock outputs are timing animatics, not completed generative music videos.\n")
