"""Source-faithful, independently reviewed lyric timing and subtitle exports.

No alignment is guessed. An untimed source produces an incomplete Preview, and
Final requires a reviewer bound to the exact source and timing content. Section
labels are metadata; an empty repeated section needs an explicit expansion.
"""
from __future__ import annotations

import copy
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .core import FilmError, atomic_text, digest, now, object_hash, probe, read, safe_path, write

SCHEMA_VERSION = 1
_MARKER = re.compile(r"^\[([^\[\]]+)\]$")


def _atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-lyrics-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _source(project):
    path = Path(project) / "input/lyrics.txt"
    data = path.read_bytes() if path.exists() else b""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FilmError("input/lyrics.txt must be UTF-8") from exc
    rows = []
    for line, value in enumerate(text.splitlines(), 1):
        if not value.strip():
            continue
        marker = _MARKER.fullmatch(value.strip())
        rows.append({"id": f"L{line:04d}", "line": line, "text": value,
                     "kind": "section" if marker else "lyric",
                     **({"section": marker.group(1).strip().casefold()} if marker else {})})
    known = {}
    for index, row in enumerate(rows):
        if row["kind"] != "section":
            continue
        block = []
        for following in rows[index + 1:]:
            if following["kind"] != "lyric":
                break
            block.append(following["id"])
        if block:
            known[row["section"]] = block
        elif row["section"] in known:
            row["kind"] = "repeat"
            row["suggested_source_row_ids"] = known[row["section"]][:]
    return data, hashlib.sha256(data).hexdigest(), rows, not path.exists()


def _new_document(source_hash, rows, missing=False):
    return {"schema_version": SCHEMA_VERSION, "source_path": "input/lyrics.txt",
            "source_sha256": source_hash, "source_missing": missing,
            "rows": rows, "repeat_expansions": {}, "cues": [],
            "unresolved_row_ids": [row["id"] for row in rows if row["kind"] != "section"],
            "review": None}


def prepare_lyrics(project):
    """Mirror source bytes and initialize timings; retain stale timing in history."""
    project = Path(project)
    data, source_hash, rows, missing = _source(project)
    folder = project / "lyrics"
    folder.mkdir(parents=True, exist_ok=True)
    mirror = folder / "lyrics_source.txt"
    if not mirror.exists() or mirror.read_bytes() != data:
        _atomic_bytes(mirror, data)
    target = folder / "lyrics_timed.json"
    old = None
    if target.exists():
        try:
            old = read(target)
        except (ValueError, TypeError):
            old = None
    if isinstance(old, dict) and old.get("schema_version") == SCHEMA_VERSION and old.get("source_sha256") == source_hash:
        # The source rows are never authoritative in an editable JSON document.
        return {**old, "rows": rows, "source_missing": missing}
    document = _new_document(source_hash, rows, missing)
    if old is not None:
        history = folder / "history" / f"timing_{object_hash(old)}.json"
        if not history.exists():
            write(history, old)
        document["invalidated_reason"] = "Lyrics source or timing schema changed; timing and review were invalidated"
    write(target, document)
    return document


def _effective_rows(document):
    rows = document["rows"]
    row_map = {row["id"]: row for row in rows}
    expansions = document.get("repeat_expansions", {})
    if not isinstance(expansions, dict):
        raise FilmError("repeat_expansions must map repeat marker IDs to source row IDs")
    repeats = {row["id"] for row in rows if row["kind"] == "repeat"}
    if set(expansions) - repeats:
        raise FilmError("Unknown repeat marker in repeat_expansions")
    effective, unresolved = [], []
    for row in rows:
        if row["kind"] == "lyric":
            effective.append(row)
        elif row["kind"] == "repeat":
            selection = expansions.get(row["id"])
            if selection is None:
                unresolved.append(row["id"])
                continue
            if selection != row["suggested_source_row_ids"]:
                raise FilmError("A repeat expansion must explicitly reference the complete matching source section in order")
            for source_id in selection:
                effective.append({**row_map[source_id], "id": f"{row['id']}:{source_id}",
                                  "repeat_marker_id": row["id"], "original_source_row_id": source_id})
    return effective, unresolved


def timing_fingerprint(document):
    """No shot data participates: changing a cut cannot move lyric timing."""
    return object_hash({"source_sha256": document["source_sha256"],
                        "repeat_expansions": document.get("repeat_expansions", {}),
                        "cues": document.get("cues", [])})


def _check(document, duration_ms):
    if type(duration_ms) is not int or duration_ms <= 0:
        raise FilmError("Lyrics duration must be positive integer milliseconds")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise FilmError("Unsupported lyric timing schema")
    effective, unresolved = _effective_rows(document)
    row_map = {row["id"]: row for row in effective}
    order = {row["id"]: index for index, row in enumerate(effective)}
    cues = document.get("cues", [])
    if not isinstance(cues, list):
        raise FilmError("cues must be a list")
    ids, coverage = set(), {row["id"]: [] for row in effective}
    previous_end, previous_order, previous_char_end = 0, -1, 0
    for cue in cues:
        if not isinstance(cue, dict):
            raise FilmError("Each cue must be an object")
        cue_id = cue.get("id")
        if not isinstance(cue_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", cue_id) or cue_id in ids:
            raise FilmError("Cue IDs must be unique letters, digits, underscores or hyphens")
        ids.add(cue_id)
        source_id = cue.get("source_row_id")
        if source_id not in row_map:
            raise FilmError(f"Unknown source row for {cue_id}")
        start, end = cue.get("start_ms"), cue.get("end_ms")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= duration_ms:
            raise FilmError(f"Invalid integer-millisecond range for {cue_id}")
        if start < previous_end:
            raise FilmError(f"Overlapping or out-of-order lyric timing at {cue_id}")
        text = row_map[source_id]["text"]
        first, last = cue.get("char_start", 0), cue.get("char_end", len(text))
        if type(first) is not int or type(last) is not int or not 0 <= first < last <= len(text):
            raise FilmError(f"Invalid source character range for {cue_id}")
        if cue.get("text") != text[first:last]:
            raise FilmError(f"Cue {cue_id} changes the authoritative lyric text")
        index = order[source_id]
        if index < previous_order or (index == previous_order and first < previous_char_end):
            raise FilmError(f"Duplicate or reordered source text at {cue_id}")
        coverage[source_id].append((first, last))
        previous_end, previous_order, previous_char_end = end, index, last
    for row in effective:
        cursor = 0
        complete = True
        for first, last in coverage[row["id"]]:
            if row["text"][cursor:first].strip():
                complete = False
            cursor = last
        if row["text"][cursor:].strip():
            complete = False
        if not complete:
            unresolved.append(row["id"])
    checked = copy.deepcopy(document)
    checked["effective_rows"] = effective
    checked["unresolved_row_ids"] = unresolved
    warnings = []
    if not effective:
        warnings.append("No lyric text is available")
    if unresolved:
        warnings.append(f"{len(unresolved)} lyric rows or repeat instructions remain untimed")
    review = checked.get("review")
    if not isinstance(review, dict) or not str(review.get("reviewer", "")).strip() or review.get("timing_sha256") != timing_fingerprint(checked):
        checked["review"] = None
        warnings.append("Lyric timing has not been reviewed for the current source and cues")
    return checked, warnings


def validate_lyrics(project, duration_ms, strict=False):
    """Return (current document, warnings). Preview drops malformed stored cues."""
    document = prepare_lyrics(project)
    try:
        document, warnings = _check(document, duration_ms)
    except (FilmError, KeyError, TypeError, ValueError) as exc:
        if strict:
            raise FilmError(f"Invalid lyric timeline: {exc}") from exc
        document = _new_document(document["source_sha256"], document["rows"], document.get("source_missing", False))
        document, warnings = _check(document, duration_ms)
        warnings.insert(0, f"Stored lyric timing was excluded from Preview: {exc}")
    if strict and warnings:
        raise FilmError("Final lyrics are incomplete: " + "; ".join(warnings))
    return document, warnings


def _duration(project):
    analysis = read(Path(project) / "analysis/audio.json", {})
    duration = analysis.get("duration_ms")
    if type(duration) is not int or duration <= 0:
        raise FilmError("Analyze the master audio before saving lyric timing")
    return duration


def save_timing(project, document, reviewer=""):
    """Validate exact text/timing; reviewer affirms this complete timing revision."""
    fresh = prepare_lyrics(project)
    if not isinstance(document, dict) or document.get("source_sha256") != fresh["source_sha256"]:
        raise FilmError("Lyrics source changed; reload before saving timing")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise FilmError("Unsupported lyric timing schema")
    candidate = copy.deepcopy(document)
    candidate["rows"] = fresh["rows"]
    candidate["source_path"] = "input/lyrics.txt"
    candidate["source_missing"] = fresh["source_missing"]
    candidate["review"] = None
    candidate, _ = _check(candidate, _duration(project))
    if reviewer.strip():
        if candidate["unresolved_row_ids"] or not candidate["effective_rows"]:
            raise FilmError("Resolve every lyric row and repeat before approving timing")
        candidate["review"] = {"reviewer": reviewer.strip(), "reviewed_at": now(),
                               "timing_sha256": timing_fingerprint(candidate)}
    candidate["updated_at"] = now()
    write(Path(project) / "lyrics/lyrics_timed.json", candidate)
    return candidate


def _srt_time(milliseconds):
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def _ass_time(centiseconds):
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{fraction:02}"


def _ass_text(text):
    # ASS does not treat a doubled backslash as an escaped literal. A zero-width
    # word joiner keeps literal backslashes from forming \N/\n/\h controls;
    # source JSON and SRT retain the original bytes/text.
    return text.replace("\\", "\\\u2060").replace("{", "\\{").replace("}", "\\}").replace("\r", "").replace("\n", "\\N")


def _font(project, config, text):
    settings = config.get("subtitles", {})
    family = str(settings.get("font_name", "DejaVu Sans"))
    if any(character in family for character in ",\r\n"):
        raise FilmError("Subtitle font_name cannot contain comma or newline")
    report = {"requested_family": family, "status": "unverified", "missing_codepoints": []}
    path = None
    if settings.get("font_file"):
        path = safe_path(project, settings["font_file"])
        if not path.is_file():
            raise FilmError("Configured subtitle font_file does not exist")
    else:
        try:
            match = subprocess.run(["fc-match", "-f", "%{file}\n%{family}\n", family], capture_output=True, text=True, check=True, timeout=10)
            parts = match.stdout.splitlines()
            if parts and Path(parts[0]).is_file():
                path = Path(parts[0])
                report["matched_family"] = parts[1] if len(parts) > 1 else "unknown"
        except (OSError, subprocess.SubprocessError):
            pass
    if path:
        report["path"] = str(path)
        report["sha256"] = digest(path)
        try:
            from fontTools.ttLib import TTFont, TTLibError
            with TTFont(path, fontNumber=0) as font:
                cmap = font.getBestCmap() or {}
                missing = sorted({ord(char) for char in text if not char.isspace() and ord(char) not in cmap})
                report["missing_codepoints"] = [f"U+{point:04X}" for point in missing]
                report["status"] = "missing_glyphs" if missing else "verified"
                names = font["name"].names
                family_names = [name.toUnicode() for name in names if name.nameID == 1]
                if family_names:
                    family = family_names[0]
        except ImportError:
            report["note"] = "Font cmap verification unavailable; install fonttools"
        except (OSError, KeyError, ValueError, TTLibError):
            report["note"] = "Font cmap verification unavailable; install fonttools or choose a readable font"
    report["family"] = family
    return path, report


def export_subtitles(project, output_dir, duration_ms, strict=False):
    """Export exact SRT, safe-area ASS, source, timing and a font coverage report."""
    project, folder = Path(project), Path(output_dir)
    document, warnings = validate_lyrics(project, duration_ms, strict=strict)
    config = read(project / "project.yaml")
    fmt, settings = config.get("format", {}), config.get("subtitles", {})
    width, height = int(fmt.get("width", 1440)), int(fmt.get("height", 1080))
    if width <= 0 or height <= 0:
        raise FilmError("Invalid subtitle canvas")
    font_file, report = _font(project, config, "".join(cue["text"] for cue in document["cues"]))
    if report["status"] != "verified":
        warning = "Subtitle glyph coverage " + report["status"]
        if report["missing_codepoints"]:
            warning += ": " + ", ".join(report["missing_codepoints"][:20])
        warnings.append(warning)
        if strict:
            raise FilmError(warning + "; configure subtitles.font_file with full source coverage")
    folder.mkdir(parents=True, exist_ok=True)
    if font_file:
        fonts = folder / "subtitle_fonts"
        fonts.mkdir(exist_ok=True)
        target_font = fonts / ("subtitle" + font_file.suffix.lower())
        if font_file.resolve() != target_font.resolve():
            shutil.copyfile(font_file, target_font)
        report["exported_path"] = str(target_font)
    source, timed, ass, srt = [folder / name for name in ("lyrics_source.txt", "lyrics_timed.json", "lyrics.ass", "lyrics.srt")]
    _atomic_bytes(source, (project / "lyrics/lyrics_source.txt").read_bytes())
    write(timed, document)
    font_size = float(settings.get("font_size", max(8, round(height * 0.044))))
    if not math.isfinite(font_size) or not 8 <= font_size <= height / 3:
        raise FilmError("Subtitle font_size must be finite and between 8 pixels and one third of the canvas")
    horizontal = max(round(width * 0.05), int(settings.get("margin_x", 0)))
    bottom = max(round(height * 0.07), int(settings.get("margin_bottom", 0)))
    if horizontal >= width / 2 or bottom >= height / 2:
        raise FilmError("Subtitle safe-area margins leave no usable text area")
    header = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\n"
              "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
              f"Style: Lyrics,{report['family']},{font_size:g},&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,{horizontal},{horizontal},{bottom},1\n"
              "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    srt_blocks, ass_events = [], []
    for index, cue in enumerate(document["cues"], 1):
        srt_blocks.append(f"{index}\n{_srt_time(cue['start_ms'])} --> {_srt_time(cue['end_ms'])}\n{cue['text']}\n")
        start = (cue["start_ms"] + 5) // 10
        end = max(start + 1, (cue["end_ms"] + 5) // 10)
        ass_events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Lyrics,,0,0,0,,{_ass_text(cue['text'])}")
    if any(cue["start_ms"] % 10 or cue["end_ms"] % 10 for cue in document["cues"]):
        warnings.append("ASS rounds boundaries to centiseconds; JSON and SRT preserve exact integer milliseconds")
    atomic_text(srt, "\n".join(srt_blocks))
    atomic_text(ass, header + "\n".join(ass_events) + "\n")
    write(folder / "subtitle_report.json", {"warnings": warnings, "font": report, "timing_sha256": timing_fingerprint(document)})
    return {"document": document, "warnings": warnings, "ass": ass, "srt": srt,
            "timed": timed, "source": source, "font_report": report}


def burn_subtitles(clean_path, ass_path, output_path, fmt):
    """Burn ASS without shell/filter path interpolation; copy the encoded audio."""
    clean, ass, output = Path(clean_path).resolve(), Path(ass_path).resolve(), Path(output_path).resolve()
    if clean == output:
        raise FilmError("Subtitle output must not replace the clean master")
    original = probe(clean)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Controlled relative filter filenames avoid FFmpeg's second quoting layer.
    with tempfile.TemporaryDirectory(prefix=".subtitle-", dir=output.parent) as work:
        work = Path(work)
        shutil.copyfile(ass, work / "lyrics.ass")
        fonts = ass.parent / "subtitle_fonts"
        if fonts.is_dir():
            shutil.copytree(fonts, work / "fonts")
        (work / "fonts").mkdir(exist_ok=True)
        pending = work / "subbed.mp4"
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-filter_threads", "1",
                   "-i", str(clean), "-map", "0:v:0", "-map", "0:a:0", "-vf", "ass=filename=lyrics.ass:fontsdir=fonts",
                   "-c:v", "libx264", "-crf", str(fmt.get("crf", 18)), "-preset", "veryfast", "-pix_fmt", "yuv420p",
                   "-c:a", "copy", "-map_metadata", "-1", "-movflags", "+faststart", str(pending)]
        try:
            result = subprocess.run(command, cwd=work, capture_output=True, timeout=1800)
        except (OSError, subprocess.SubprocessError) as exc:
            raise FilmError(f"Subtitle rendering failed: {exc}") from exc
        if result.returncode:
            raise FilmError(result.stderr.decode(errors="replace")[-4000:])
        actual = probe(pending)
        streams = actual["streams"]
        if len([stream for stream in streams if stream["codec_type"] == "video"]) != 1 or len([stream for stream in streams if stream["codec_type"] == "audio"]) != 1:
            raise FilmError("Subtitled master must contain exactly one video and one original-audio stream")
        original_video = next(stream for stream in original["streams"] if stream["codec_type"] == "video")
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        if any(video.get(key) != original_video.get(key) for key in ("width", "height", "r_frame_rate")):
            raise FilmError("Subtitle burn changed the video canvas or frame rate")
        if "nb_frames" in video and "nb_frames" in original_video and video["nb_frames"] != original_video["nb_frames"]:
            raise FilmError("Subtitle burn changed the video frame count")
        if abs(float(actual["format"]["duration"]) - float(original["format"]["duration"])) > 1 / float(fmt.get("fps", 24)) + 0.03:
            raise FilmError("Subtitle burn changed the master duration")
        pending.replace(output)
    return output
