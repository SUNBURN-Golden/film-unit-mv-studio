from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

import yaml
from .schema import schema_metadata


class FilmError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def object_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read(path, default=None):
    path = Path(path)
    if not path.exists():
        if default is not None:
            return default
        raise FilmError(f"Missing file: {path}")
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text) if path.suffix in {".yaml", ".yml"} else json.loads(text)


def write(path, value):
    path = Path(path)
    text = yaml.safe_dump(value, allow_unicode=True, sort_keys=False) if path.suffix in {".yaml", ".yml"} else json.dumps(value, indent=2, ensure_ascii=False)
    atomic_text(path, text + "\n")


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run(args, timeout=600):
    result = subprocess.run([str(a) for a in args], capture_output=True, timeout=timeout,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise FilmError(result.stderr.decode(errors="replace")[-4000:])
    return result.stdout


def ffmpeg(args):
    return run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-filter_threads", "1", *args])


def probe(path):
    return json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path]))


def frame_at(ms, fps=24):
    return (int(ms) * int(fps) + 500) // 1000


def timecode(ms):
    return f"{ms // 60000:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def safe_path(project, relative):
    root = Path(project).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise FilmError("Asset paths must stay inside this project")
    return path


@contextlib.contextmanager
def project_mutex(project):
    # Both implementations fail fast and release the OS lock after a crash.
    lock = Path(project) / ".compile.lock"
    with lock.open("a+b") as f:
        if os.name == "nt":
            import msvcrt
            # Windows byte-range locks require a byte at the locked position.
            if f.seek(0, os.SEEK_END) == 0:
                f.write(b"\0")
                f.flush()
            f.seek(0)
            acquire = lambda: msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: (f.seek(0), msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl
            acquire = lambda: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(f, fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as e:
            raise FilmError("This project is already rendering in another process") from e
        try:
            yield
        finally:
            release()


# Output canvas per aspect ratio. Veo generates 16:9 or 9:16 only.
FORMATS = {"4:3": (1440, 1080), "16:9": (1920, 1080), "9:16": (1080, 1920)}


def init_project(root, name, audio, brief, lyrics="", synthetic=False, aspect="4:3"):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name):
        raise FilmError("Use a project ID with letters, digits, underscores or hyphens")
    if aspect not in FORMATS:
        raise FilmError("Aspect ratio must be one of: " + ", ".join(FORMATS))
    width, height = FORMATS[aspect]
    p = Path(root).resolve() / name
    if p.exists():
        raise FilmError("Project exists; select it or use a new name")
    audio = Path(audio)
    if audio.suffix.lower() not in {".mp3", ".wav"}:
        raise FilmError("Please upload MP3 or WAV")
    metadata = probe(audio)
    if not any(s["codec_type"] == "audio" for s in metadata["streams"]):
        raise FilmError("No audio stream")
    seconds = float(metadata["format"].get("duration", 0))
    if not 1 <= seconds <= 600.1:
        raise FilmError("Audio must be between 1 second and 10 minutes")
    for d in ["input", "analysis", "bible", "characters", "locations", "storyboard", "manifest", "lyrics", "builds", "render/draft", "render/final", "render/manual", "render/requests", "render/responses", "qc/reviews", "output"]:
        (p / d).mkdir(parents=True, exist_ok=True)
    dest = p / "input" / ("master" + audio.suffix.lower())
    shutil.copyfile(audio, dest)
    atomic_text(p / "input/brief.md", brief)
    atomic_text(p / "input/lyrics.txt", lyrics)
    write(p / "project.yaml", {
        **schema_metadata(), "name": name, "created_at": now(),
        "audio": {"path": str(dest.relative_to(p)), "sha256": digest(dest), "synthetic_test_audio": synthetic},
        "format": {"width": width, "height": height, "fps": 24, "aspect_ratio": aspect, "crf": 18},
        "budget": {"max_credits": 10000, "max_usd": 0, "max_retry_per_shot": 2, "draft_resolution": "720p", "final_resolution": "1080p"},
        "qc": {"threshold": 85},
        "renderer": {"default": "mock", "mode": "AUTO"},
    })
    return p


def validate_manifest(shots, duration_ms, fps=24):
    if not shots:
        raise FilmError("Shot manifest is empty")
    cursor, ids = 0, set()
    required = {"id", "sequence", "in_ms", "out_ms", "duration_ms", "description", "characters", "locations", "composition", "camera", "motion", "references", "render_mode", "renderer", "status"}
    for s in shots:
        if required - s.keys():
            raise FilmError(f"Missing shot fields: {required - s.keys()}")
        if not re.fullmatch(r"S[0-9]{3,5}", s["id"]) or s["id"] in ids:
            raise FilmError("Shot IDs must be unique S001-style values")
        if type(s["in_ms"]) is not int or type(s["out_ms"]) is not int:
            raise FilmError("Shot boundaries must be integer milliseconds")
        if s["in_ms"] != cursor or s["out_ms"] <= cursor:
            raise FilmError(f"Gap, overlap, or reversed range at {s['id']}")
        if s["duration_ms"] != s["out_ms"] - s["in_ms"]:
            raise FilmError(f"Duration mismatch at {s['id']}")
        if frame_at(s["out_ms"], fps) <= frame_at(cursor, fps):
            raise FilmError(f"Shot {s['id']} is shorter than one frame")
        if s["render_mode"] not in {"STATIC", "LIMITED_MOTION", "FULL_GENERATIVE"}:
            raise FilmError("Unknown render mode")
        ids.add(s["id"])
        cursor = s["out_ms"]
    if cursor != duration_ms:
        raise FilmError("Manifest must cover the full measured audio duration")


def visual_context_fingerprint(p):
    """Shared visual intent; no cut positions, shot images or subtitle timing.

    Source lyric wording can change the meaning of a scene, so it participates.
    Per-shot reference bytes belong to the shot review, not this shared context.
    Missing optional bibles are represented explicitly so their addition changes
    context. Full production LOCK still requires all mandatory inputs below.
    """
    p = Path(p)
    config = read(p / "project.yaml")
    paths = {"input/brief.md", "input/lyrics.txt", "bible/story.md", "bible/style_bible.yaml",
             "bible/characters.yaml", "bible/locations.yaml", "bible/directing.yaml"}
    for folder in ("characters", "locations"):
        paths.update(str(f.relative_to(p)) for f in (p / folder).rglob("*") if f.is_file())
    files = {}
    for relative in sorted(paths):
        path = safe_path(p, relative)
        files[relative] = digest(path) if path.is_file() else None
    return object_hash({"schema_version": 1, "format": config["format"], "files": files})


def production_fingerprint(p):
    """Whole-build authorization, intentionally broader than individual reviews."""
    p = Path(p)
    config = read(p / "project.yaml")
    paths = [config["audio"]["path"], "input/brief.md", "input/lyrics.txt", "analysis/audio.json", "manifest/sequence.json", "manifest/shots.json", "bible/style_bible.yaml", "bible/story.md", "bible/characters.yaml", "bible/locations.yaml"]
    # New caption revisions and optional directing bibles participate in LOCK.
    # Existing projects without these files retain their original fingerprint.
    paths += [r for r in ["lyrics/lyrics_timed.json", "bible/directing.yaml"] if (p / r).is_file()]
    if config.get("subtitles", {}).get("font_file"):
        paths.append(config["subtitles"]["font_file"])
    for s in read(p / "manifest/shots.json"):
        paths += s["references"]
    for folder in ["characters", "locations"]:
        paths += [str(f.relative_to(p)) for f in sorted((p / folder).rglob("*")) if f.is_file()]
    inputs = {"format": config["format"], "files": {r: digest(safe_path(p, r)) for r in sorted(set(paths))}}
    if "subtitles" in config:
        inputs["subtitles"] = config["subtitles"]
    return object_hash(inputs)


def lock_production(p, reviewer, mock_only=False):
    p = Path(p)
    shots = read(p / "manifest/shots.json")
    audio = read(p / "analysis/audio.json")
    validate_manifest(shots, audio["duration_ms"], read(p / "project.yaml")["format"]["fps"])
    if not reviewer.strip():
        raise FilmError("Reviewer is required")
    if not mock_only and any(s.get("storyboard_kind") == "placeholder" for s in shots):
        raise FilmError("Replace placeholder frames before approving paid generation")
    write(p / "manifest/locks.json", {"fingerprint": production_fingerprint(p), "reviewer": reviewer,
        "locked_at": now(), "mock_only": mock_only, "story": True, "style": True, "characters": True, "locations": True,
        "shots": {s["id"]: True for s in shots}})


def require_lock(p, mode):
    locked = read(Path(p) / "manifest/locks.json", {})
    if locked.get("fingerprint") != production_fingerprint(p):
        raise FilmError("Production is unlocked or changed after LOCK; review and LOCK again")
    if mode != "mock" and locked.get("mock_only"):
        raise FilmError("This LOCK authorizes a mock animatic only")
    return locked
