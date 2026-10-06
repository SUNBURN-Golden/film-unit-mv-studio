"""Append-only build directories, captured inputs, and offline integrity checks."""
from pathlib import Path
import re
import shutil
import platform

from .core import FilmError, digest, now, read, run, safe_path, write


def allocate_build(project, mode):
    root = Path(project) / "builds"
    root.mkdir(exist_ok=True)
    ids = [int(p.name[1:]) for p in root.iterdir() if re.fullmatch(r"B[0-9]{4,}", p.name)]
    number = max(ids, default=0) + 1
    while True:
        p = root / f"B{number:04d}"
        try:
            p.mkdir()
            break
        except FileExistsError:
            number += 1
    write(p / "build.json", {"schema_version": 1, "build_id": p.name, "mode": mode,
                            "status": "BUILDING", "created_at": now()})
    return p


def capture(source, destination):
    """Independent byte copy: never hard-link mutable project assets into a build."""
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    before = digest(source)
    shutil.copyfile(source, destination)
    if digest(destination) != before or digest(source) != before:
        raise FilmError("Input changed while creating the build snapshot")
    return before


def seal_build(folder, record):
    folder = Path(folder)
    record.update(status="COMPLETE", completed_at=now(), toolchain={
        "python": platform.python_version(),
        "ffmpeg": run(["ffmpeg", "-version"]).decode().splitlines()[0],
        "compiler": "0.3.0",
    })
    record["files"] = {str(p.relative_to(folder)): digest(p) for p in sorted(folder.rglob("*"))
                       if p.is_file() and p.name != "build.json"}
    write(folder / "build.json", record)
    return record


def list_builds(project):
    result = []
    records = [p for p in (Path(project) / "builds").glob("B*/build.json")
               if re.fullmatch(r"B[0-9]{4,}", p.parent.name) and p.is_file()]
    for p in sorted(records, key=lambda p: int(p.parent.name[1:]), reverse=True):
        row = read(p)
        result.append({**row, "build_dir": str(p.parent)})
    return result


def verify_build(build_dir):
    p = Path(build_dir)
    record = read(p / "build.json")
    errors = []
    if record.get("status") != "COMPLETE" or not record.get("files"):
        errors.append("Build is incomplete or has no inventory")
    for relative, expected in record.get("files", {}).items():
        try:
            asset = safe_path(p, relative)
            if not asset.is_file() or digest(asset) != expected:
                errors.append(relative)
        except (FilmError, OSError):
            errors.append(relative)
    return {"valid": not errors, "errors": errors, "build_id": record["build_id"]}


def replay_build(build_dir, output_dir):
    """Replay from captured normalized clips/master/ASS, without live project files.

    Captured exports are byte-exact historical artifacts. A fresh encode uses the
    installed FFmpeg/fonts; cross-version bit-for-bit encoding is not promised.
    """
    from .compiler import mux_timeline
    from .lyrics import burn_subtitles
    p, out = Path(build_dir).resolve(), Path(output_dir).resolve()
    if out == p or out.is_relative_to(p):
        raise FilmError("Replay must not write into a saved build")
    result = verify_build(p)
    if not result["valid"]:
        raise FilmError("Build integrity failed: " + ", ".join(result["errors"]))
    if out.exists():
        raise FilmError("Replay output already exists; choose a new directory")
    data = read(p / "build.json")
    if data.get("document_type") is not None or data.get("schema_version") != 1:
        raise FilmError("Draft previews are not replayable; Build 2 replay "
                        "arrives with ANIM-006")
    out.mkdir(parents=True)
    clips = [safe_path(p, s["clip_path"]) for s in data["shots"]]
    clean = out / "MASTER_CLEAN.mp4"
    mux_timeline(clips, safe_path(p, data["audio"]["path"]), clean,
                 data["duration_ms"], data["format"])
    for name in ("lyrics.ass", "lyrics.srt"):
        capture(p / name, out / name)
    if (p / "subtitle_fonts").exists():
        shutil.copytree(p / "subtitle_fonts", out / "subtitle_fonts")
    subbed = out / "MASTER_SUBBED.mp4"
    burn_subtitles(clean, out / "lyrics.ass", subbed, data["format"])
    return {"output": str(subbed), "clean": str(clean), "replayed_from": data["build_id"]}
