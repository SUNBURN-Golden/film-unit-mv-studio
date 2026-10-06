"""제작 준비 (production brief board): staged materials vs adopted inputs.

Uploaded references and candidate audio first land in ``brief/staging/`` as
TEMPORARY material. Nothing under ``brief/`` participates in compile or in any
review/LOCK fingerprint, so staged bytes can never feed production silently.
Adoption is an explicit action that copies the staged bytes into the existing
project inputs (``input/master.*``, ``input/brief.md``, ``input/lyrics.txt``,
``input/references/`` and ``project.yaml`` direction notes) and records the
adopted digest in ``brief/board.json``.

Changing an adopted original reports which reviews, cues and the production
LOCK become stale and need re-review; existing review records, LOCK bytes and
superseded lyric timing are preserved — never rewritten or inherited. The
measured timeline is bound to the adopted master, so a different song is
refused once analysis/shots exist and belongs in a new project. Lyric text is
stored verbatim; vocal timing is never estimated from lyric length.
"""
from pathlib import Path
import hashlib
import os
import shutil
import tempfile

from .core import (FilmError, atomic_text, digest, init_project, now, probe,
                   production_fingerprint, production_profile, project_mutex,
                   read, safe_path, visual_context_fingerprint, write)
from .lyrics import lyrics_review_fingerprint, prepare_lyrics, timing_fingerprint
from .resolver import reference_hash, review_binding, shot_hash

BOARD_SCHEMA = 1
SUPPORTED_AUDIO_SUFFIXES = {".mp3", ".wav"}
MIN_AUDIO_SECONDS = 1.0
# Engine input limit; the verified baseline remains 240 s at 24 fps.
MAX_AUDIO_SECONDS = 600.1
BASELINE_SECONDS = 240
BASELINE_FPS = 24
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
TEXT_SUFFIXES = {".md", ".txt"}
TEXT_FIELDS = {"brief": "input/brief.md", "lyrics": "input/lyrics.txt"}


def _board_path(p):
    return Path(p) / "brief" / "board.json"


def load_board(p):
    board = read(_board_path(p), {})
    if not board:
        return {"schema_version": BOARD_SCHEMA, "document": "brief_board",
                "seq": 1, "staged": [], "adopted": {"references": []},
                "adoptions": []}
    if board.get("schema_version") != BOARD_SCHEMA or board.get("document") != "brief_board":
        raise FilmError("Unsupported brief board document")
    board.setdefault("staged", [])
    board.setdefault("adopted", {}).setdefault("references", [])
    board.setdefault("adoptions", [])
    board.setdefault("seq", len(board["staged"]) + 1)
    return board


def _save_board(p, board):
    write(_board_path(p), board)


def _safe_name(name):
    """Browser-supplied names are untrusted; keep only the basename."""
    name = Path(str(name).replace("\\", "/")).name
    if not name or name.startswith(".") or name in {"..", "."}:
        name = "material.bin"
    return name


def _sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _kind(name):
    suffix = Path(name).suffix.lower()
    if suffix in SUPPORTED_AUDIO_SUFFIXES:
        return "audio"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in TEXT_SUFFIXES:
        return "note"
    return "file"


def _atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-brief-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _staged(board, staged_id):
    record = next((r for r in board["staged"] if r["id"] == staged_id), None)
    if record is None:
        raise FilmError(f"Unknown staged material: {staged_id}")
    return record


def stage_material(p, name, data, note=""):
    """Store uploaded bytes under brief/staging/ as TEMPORARY material.

    Staged files are inert: they are outside every fingerprint and are never
    copied into a build. Importing identical bytes twice — or bytes identical
    to an adopted input — is a duplicate and refused with a message.
    """
    p = Path(p)
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, (bytes, bytearray)) or not bytes(data):
        raise FilmError("Staged material is empty")
    data = bytes(data)
    sha = _sha_bytes(data)
    with project_mutex(p):
        board = load_board(p)
        for record in board["staged"]:
            if record["sha256"] == sha:
                raise FilmError(f"Duplicate import: identical bytes are already staged as {record['id']}")
        config = read(p / "project.yaml", {})
        audio_cfg = config.get("audio", {})
        master = safe_path(p, audio_cfg["path"]) if audio_cfg.get("path") else None
        if master is not None and master.is_file() and digest(master) == sha:
            raise FilmError("Duplicate import: identical bytes are already the adopted master audio")
        if any(r.get("sha256") == sha for r in board["adopted"].get("references", [])):
            raise FilmError("Duplicate import: identical bytes are already an adopted reference")
        record_id = f"M{board.get('seq', 1):04d}"
        board["seq"] = board.get("seq", 1) + 1
        safe = _safe_name(name)
        relative = f"brief/staging/{record_id}_{safe}"
        _atomic_bytes(safe_path(p, relative), data)
        record = {"id": record_id, "name": safe, "path": relative, "sha256": sha,
                  "bytes": len(data), "kind": _kind(safe), "note": str(note).strip(),
                  "state": "temporary", "staged_at": now()}
        board["staged"].append(record)
        _save_board(p, board)
        return record


def stage_note(p, text, name="note.md"):
    """A pasted note or link list is staged like any other temporary material."""
    if not str(text).strip():
        raise FilmError("Note is empty")
    safe = _safe_name(name)
    if not Path(safe).suffix:
        safe += ".md"
    return stage_material(p, safe, str(text))


def discard_material(p, staged_id):
    """Drop a temporary material; adopted inputs are never discarded here."""
    p = Path(p)
    with project_mutex(p):
        board = load_board(p)
        record = _staged(board, staged_id)
        if record["state"] != "temporary":
            raise FilmError("Only temporary materials can be discarded")
        safe_path(p, record["path"]).unlink(missing_ok=True)
        board["staged"].remove(record)
        _save_board(p, board)
        return record["id"]


def validate_audio(source):
    """The same contract init_project enforces: MP3/WAV with an audio stream, 1 s–10 min."""
    source = Path(source)
    if not source.is_file():
        raise FilmError(f"Missing audio file: {source}")
    if source.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
        raise FilmError("Please upload MP3 or WAV")
    try:
        metadata = probe(source)
    except FilmError as exc:
        raise FilmError("Cannot read this file as audio (corrupt or unsupported format)") from exc
    if not any(s["codec_type"] == "audio" for s in metadata["streams"]):
        raise FilmError("No audio stream")
    seconds = float(metadata["format"].get("duration", 0))
    if not MIN_AUDIO_SECONDS <= seconds <= MAX_AUDIO_SECONDS:
        raise FilmError("Audio must be between 1 second and 10 minutes")
    return {"seconds": seconds}


def adopt_audio(p, staged_id):
    """Adopt a staged file as the project master, recording its digest.

    Once a measured timeline exists (analysis, shot manifest or a converted
    animation timeline) the master is bound to it: a different song is refused
    and belongs in a new project, while restaging the identical bytes restores
    a missing master. Before that point a superseded master is preserved under
    ``input/master-superseded-*``.
    """
    p = Path(p)
    with project_mutex(p):
        board = load_board(p)
        record = _staged(board, staged_id)
        if record["state"] != "temporary":
            raise FilmError("This material was already adopted")
        source = safe_path(p, record["path"])
        info = validate_audio(source)
        new_sha = digest(source)
        config = read(p / "project.yaml")
        current = config.get("audio", {})
        analysis_path = p / "analysis" / "audio.json"
        bound = (analysis_path.exists() or (p / "manifest/shots.json").exists()
                 or (p / "timeline/edit.json").exists())
        if bound:
            expected = None
            if analysis_path.exists():
                expected = read(analysis_path).get("master_sha256")
            if not expected:
                expected = current.get("sha256")
            if new_sha != expected:
                raise FilmError(
                    "A different song cannot replace the adopted master: the measured "
                    "timeline, shots, reviews and LOCK belong to the current audio. "
                    "Import the new song as a new project.")
        master = safe_path(p, current["path"]) if current.get("path") else None
        if master is not None and master.is_file() and digest(master) == new_sha:
            raise FilmError("Duplicate import: the same audio is already adopted as the master")
        dest = p / "input" / ("master" + source.suffix.lower())
        if dest.exists() and master is not None and dest.resolve() != master.resolve():
            dest = p / "input" / (f"master-{new_sha[:12]}" + source.suffix.lower())
        preserved = None
        if dest.exists() and digest(dest) != new_sha:
            old_sha = current.get("sha256") or digest(dest)
            preserved = p / "input" / (f"master-superseded-{old_sha[:12]}" + dest.suffix.lower())
            dest.rename(preserved)
        shutil.copyfile(source, dest)
        if digest(dest) != new_sha:
            raise FilmError("Copied audio does not match the staged bytes")
        old_sha = current.get("sha256")
        config["audio"] = {**current, "path": str(dest.relative_to(p)), "sha256": new_sha,
                           "synthetic_test_audio": False}
        write(p / "project.yaml", config)
        record["state"] = "adopted"
        record["adopted_as"] = "audio"
        adoption = {"at": now(), "field": "audio", "sha256": new_sha,
                    "seconds": info["seconds"], "from_staged": record["id"],
                    "replaced_sha256": old_sha}
        if preserved is not None:
            adoption["preserved_previous"] = str(preserved.relative_to(p))
        board["adoptions"].append(adoption)
        board["adopted"]["audio"] = {"path": str(dest.relative_to(p)), "sha256": new_sha,
                                     "seconds": info["seconds"], "adopted_at": adoption["at"]}
        _save_board(p, board)
        return {"sha256": new_sha, "replaced": old_sha, "seconds": info["seconds"],
                "impact": impact_report(p)}


def adopt_text(p, field, text):
    """Adopt brief / original lyrics / desired emotion into existing inputs.

    Adopting lyrics writes ``input/lyrics.txt`` verbatim and runs
    prepare_lyrics, which archives superseded timing; no cue is ever derived
    from the text. Brief and lyric changes alter the shared fingerprints, so
    callers should surface the returned impact report for re-review.
    """
    if field not in {*TEXT_FIELDS, "emotion"}:
        raise FilmError("Unknown brief field")
    p = Path(p)
    text = "" if text is None else str(text)
    with project_mutex(p):
        board = load_board(p)
        if field == "emotion":
            value = text.strip()
            config = read(p / "project.yaml")
            config.setdefault("direction", {})["emotion"] = value
            write(p / "project.yaml", config)
            sha = _sha_bytes(value.encode("utf-8"))
            entry = {"text": value, "sha256": sha, "adopted_at": now()}
            board["adopted"]["emotion"] = entry
            board["adoptions"].append({"at": entry["adopted_at"], "field": "emotion",
                                       "sha256": sha})
            _save_board(p, board)
            return {"sha256": sha, "impact": impact_report(p)}
        if field == "brief" and not text.strip():
            raise FilmError("Brief text is required")
        target = p / TEXT_FIELDS[field]
        sha = _sha_bytes(text.encode("utf-8"))
        if target.exists() and _sha_bytes(target.read_bytes()) == sha:
            raise FilmError("Duplicate import: identical text is already adopted")
        atomic_text(target, text)
        document = None
        if field == "lyrics":
            document = prepare_lyrics(p)
        stamp = now()
        board["adopted"][field] = {"path": TEXT_FIELDS[field], "sha256": sha,
                                   "adopted_at": stamp}
        board["adoptions"].append({"at": stamp, "field": field, "sha256": sha})
        _save_board(p, board)
        return {"sha256": sha, "lyrics_document": document,
                "impact": impact_report(p)}


def adopt_reference(p, staged_id):
    """Adopt a staged file as a mood/reference input under input/references/.

    These are intent materials for the director, not per-shot references; they
    do not enter the review fingerprints. The adopted digest is recorded.
    """
    p = Path(p)
    with project_mutex(p):
        board = load_board(p)
        record = _staged(board, staged_id)
        if record["state"] != "temporary":
            raise FilmError("This material was already adopted")
        source = safe_path(p, record["path"])
        if not source.is_file():
            raise FilmError("Staged file is missing")
        sha = digest(source)
        adopted = board["adopted"].setdefault("references", [])
        if any(r["sha256"] == sha for r in adopted):
            raise FilmError("Duplicate import: identical bytes are already an adopted reference")
        safe = _safe_name(record["name"])
        relative = f"input/references/{safe}"
        dest = safe_path(p, relative)
        if dest.exists():
            if digest(dest) == sha:
                raise FilmError("Duplicate import: identical bytes are already an adopted reference")
            stem, suffix = Path(safe).stem, Path(safe).suffix
            relative = f"input/references/{stem}-{sha[:8]}{suffix}"
            dest = safe_path(p, relative)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        record["state"] = "adopted"
        record["adopted_as"] = "reference"
        entry = {"name": safe, "path": relative, "sha256": sha,
                 "note": record.get("note", ""), "adopted_at": now()}
        adopted.append(entry)
        board["adoptions"].append({"at": entry["adopted_at"], "field": "reference",
                                   "sha256": sha, "from_staged": record["id"],
                                   "path": relative})
        _save_board(p, board)
        return entry


def create_project(root, name, audio, brief_text, lyrics="", emotion="",
                   aspect="4:3", synthetic=False):
    """Create a project via init_project; never overwrites an existing package.

    init_project refuses an existing directory, so a name collision keeps the
    previous package, LOCK and builds untouched. The adopted inputs are then
    registered on the brief board with their digests.
    """
    p = init_project(root, name, audio, brief_text, lyrics,
                     synthetic=synthetic, aspect=aspect)
    board = load_board(p)
    config = read(p / "project.yaml")
    stamp = now()
    board["adopted"].update({
        "audio": {"path": config["audio"]["path"], "sha256": config["audio"]["sha256"],
                  "adopted_at": stamp},
        "brief": {"path": "input/brief.md", "sha256": digest(p / "input/brief.md"),
                  "adopted_at": stamp},
        "lyrics": {"path": "input/lyrics.txt", "sha256": digest(p / "input/lyrics.txt"),
                   "adopted_at": stamp},
    })
    board["adoptions"].append({"at": stamp, "field": "project",
                               "detail": "created via init"})
    if emotion.strip():
        config.setdefault("direction", {})["emotion"] = emotion.strip()
        write(p / "project.yaml", config)
        board["adopted"]["emotion"] = {"text": emotion.strip(),
                                       "sha256": _sha_bytes(emotion.strip().encode("utf-8")),
                                       "adopted_at": stamp}
    _save_board(p, board)
    return p


def impact_report(p):
    """Current staleness of LOCK, lyric review and per-take visual reviews.

    Read-only: it recomputes the existing fingerprints/review bindings and
    reports which items need re-review. It modifies no record.
    """
    p = Path(p)
    config = read(p / "project.yaml")
    audio_cfg = config.get("audio", {})
    needs_review = []
    master = safe_path(p, audio_cfg["path"]) if audio_cfg.get("path") else None
    master_sha = digest(master) if master is not None and master.is_file() else None
    analysis = read(p / "analysis/audio.json", None) if (p / "analysis/audio.json").exists() else None
    audio_info = {
        "present": master_sha is not None,
        "path": audio_cfg.get("path"),
        "sha256": master_sha,
        "matches_config": bool(master_sha) and master_sha == audio_cfg.get("sha256"),
        "synthetic": bool(audio_cfg.get("synthetic_test_audio")),
        "analysis_exists": analysis is not None,
        "analysis_matches_master": bool(analysis) and master_sha is not None
        and analysis.get("master_sha256", audio_cfg.get("sha256")) == master_sha,
        "duration_ms": analysis.get("duration_ms") if analysis else None,
    }
    if not audio_info["present"]:
        needs_review.append({"target": "master audio", "state": "MISSING",
                             "reason": "the adopted master file is missing"})
    elif not audio_info["matches_config"]:
        needs_review.append({"target": "master audio", "state": "STALE",
                             "reason": "the master bytes differ from the adopted digest"})
    if analysis is not None and audio_info["present"] and not audio_info["analysis_matches_master"]:
        needs_review.append({"target": "audio analysis", "state": "STALE",
                             "reason": "measured features belong to a different master"})
    locked = read(p / "manifest/locks.json", {})
    try:
        fingerprint = production_fingerprint(p)
    except (FilmError, OSError, KeyError, TypeError, ValueError):
        fingerprint = None
    if not locked:
        lock_state = "MISSING"
    elif fingerprint is None or locked.get("fingerprint") != fingerprint:
        lock_state = "STALE"
        needs_review.append({"target": "production LOCK", "state": "STALE",
                             "reason": "adopted inputs changed after the LOCK; review and LOCK again"})
    else:
        lock_state = "CURRENT"
    lock_info = {"state": lock_state, "reviewer": locked.get("reviewer"),
                 "locked_at": locked.get("locked_at"),
                 "mock_only": bool(locked.get("mock_only"))}
    lyrics_info = {"document": False, "cues": 0, "unresolved": 0,
                   "review_state": "MISSING", "source_changed": False,
                   "reviewer": None}
    lyrics_path = p / "input/lyrics.txt"
    source_sha = digest(lyrics_path) if lyrics_path.is_file() else None
    timed_path = p / "lyrics" / "lyrics_timed.json"
    if timed_path.exists():
        document = read(timed_path)
        lyrics_info.update(document=True, cues=len(document.get("cues", [])),
                           unresolved=len(document.get("unresolved_row_ids", [])))
        if source_sha is not None and document.get("source_sha256") != source_sha:
            lyrics_info["source_changed"] = True
            needs_review.append({"target": "lyric timing", "state": "STALE",
                                 "reason": "the lyric source changed; superseded timing is kept in lyrics/history"})
        review = document.get("review")
        if not isinstance(review, dict) or not str(review.get("reviewer", "")).strip():
            lyrics_info["review_state"] = "UNREVIEWED"
            needs_review.append({"target": "lyric timing review", "state": "UNREVIEWED",
                                 "reason": "cue timing needs a reviewer bound to the current source"})
        else:
            lyrics_info["reviewer"] = review.get("reviewer")
            valid = (review.get("timing_sha256") == timing_fingerprint(document))
            try:
                valid = valid and review.get("lyrics_review_sha256") == lyrics_review_fingerprint(p, document)
            except (FilmError, OSError, KeyError, TypeError, ValueError):
                valid = False
            lyrics_info["review_state"] = "CURRENT" if valid else "STALE"
            if not valid:
                needs_review.append({"target": "lyric timing review", "state": "STALE",
                                     "reason": "source, cues, subtitle settings or font changed; re-review"})
        if lyrics_info["unresolved"]:
            needs_review.append({"target": "lyric cues", "state": "UNREVIEWED",
                                 "reason": f"{lyrics_info['unresolved']} lyric rows remain untimed"})
    reviews = []
    shots_path = p / "manifest/shots.json"
    if shots_path.exists():
        shots = read(shots_path)
        registry = read(p / "manifest/assets.json", {"shots": {}}).get("shots", {})
        try:
            context = visual_context_fingerprint(p)
        except (FilmError, OSError, KeyError, TypeError, ValueError):
            context = None
        for shot_id, kinds in sorted(registry.items()):
            shot = next((s for s in shots if s["id"] == shot_id), None)
            for kind in sorted(kinds):
                entry = kinds[kind]
                review = entry.get("review") or {}
                state = "DRAFT" if kind == "draft" else "UNREVIEWED"
                if shot is not None and kind == "final" and review.get("binding"):
                    try:
                        approved = bool(
                            context and entry.get("shot_hash") == shot_hash(shot)
                            and entry.get("reference_hash") == reference_hash(p, shot)
                            and review.get("visual_context_id") == context
                            and review.get("binding") == review_binding(entry, context)
                            and str(review.get("reviewer", "")).strip()
                            and str(review.get("evidence", "")).strip())
                    except (FilmError, OSError, KeyError, TypeError, ValueError):
                        approved = False
                    state = "CURRENT" if approved else "STALE"
                    if not approved:
                        needs_review.append({"target": f"{shot_id} final take review",
                                             "state": "STALE",
                                             "reason": "visual context or shot inputs changed; re-review the take"})
                reviews.append({"shot": shot_id, "kind": kind, "state": state,
                                "reviewer": review.get("reviewer")})
    animation_locks = None
    profile = production_profile(config)
    if profile != "LEGACY_MV":
        try:
            from .animation_locks import lock_status
            animation_locks = lock_status(p)
            for name, wave in (animation_locks.get("waves") or {}).items():
                if wave.get("state") == "STALE":
                    needs_review.append({"target": f"WAVE_LOCK {name}", "state": "STALE",
                                         "reason": "wave contents changed; re-lock and re-review"})
            final = animation_locks.get("final") or {}
            if final.get("state") == "STALE":
                needs_review.append({"target": "FINAL_LOCK", "state": "STALE",
                                     "reason": "final scope changed; re-lock"})
        except (FilmError, ImportError, KeyError, TypeError, ValueError):
            animation_locks = {"state": "unavailable"}
    return {"profile": profile, "audio": audio_info, "lock": lock_info,
            "lyrics": lyrics_info, "visual_reviews": reviews,
            "animation_locks": animation_locks, "needs_review": needs_review}


def board_status(p):
    """Everything the preparation screen shows: adopted inputs, staging and limits."""
    p = Path(p)
    config = read(p / "project.yaml")
    board = load_board(p)
    impact = impact_report(p)
    brief_path, lyrics_path = p / "input/brief.md", p / "input/lyrics.txt"
    brief_text = brief_path.read_text(encoding="utf-8") if brief_path.is_file() else ""
    lyrics_text = lyrics_path.read_text(encoding="utf-8") if lyrics_path.is_file() else ""
    references = []
    for ref in board["adopted"].get("references", []):
        path = safe_path(p, ref["path"])
        references.append({**ref, "present": path.is_file(),
                           "matches": path.is_file() and digest(path) == ref["sha256"]})
    shots = read(p / "manifest/shots.json", [])
    placeholders = [s["id"] for s in shots if s.get("storyboard_kind") == "placeholder"]
    pending = []
    if not impact["audio"]["present"]:
        pending.append("no master audio")
    if not brief_text.strip():
        pending.append("brief is empty")
    if not lyrics_text.strip():
        pending.append("lyric source is empty")
    emotion = config.get("direction", {}).get("emotion", "")
    if not str(emotion).strip():
        pending.append("desired emotion undecided")
    if not references:
        pending.append("no adopted references")
    if not impact["audio"]["analysis_exists"]:
        pending.append("audio not analyzed")
    if placeholders:
        pending.append(f"{len(placeholders)} storyboard shots are temporary slates")
    return {
        "profile": impact["profile"],
        "audio": impact["audio"],
        "brief": {"present": bool(brief_text), "text": brief_text,
                  "sha256": _sha_bytes(brief_text.encode("utf-8")) if brief_text else None,
                  "chars": len(brief_text)},
        "lyrics": {**impact["lyrics"], "text": lyrics_text, "chars": len(lyrics_text)},
        "emotion": emotion,
        "references": references,
        "staged": board["staged"],
        "adoptions": board["adoptions"],
        "limits": {"audio_suffixes": sorted(SUPPORTED_AUDIO_SUFFIXES),
                   "min_seconds": MIN_AUDIO_SECONDS, "max_seconds": 600,
                   "baseline_seconds": BASELINE_SECONDS,
                   "fps": config.get("format", {}).get("fps", BASELINE_FPS)},
        "placeholders": placeholders,
        "pending": pending,
        "impact": impact,
    }
