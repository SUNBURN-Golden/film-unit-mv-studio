"""film-quality-diagnostics — automatic technical media diagnostics.

Finds technical faults in finished delivery files — black-frame runs, audio
clipping/silence, timebase discontinuities, decode damage and subtitle glyph
coverage — without ever becoming an aesthetic review or a Final approval.
Every report carries ``not_an_approval: true`` and nothing here writes to the
review/LOCK binding; a diagnostic result is measurement evidence for a human
reviewer, never an automatic judgement of the work.

Checks (ffprobe / ffmpeg decodes, numpy statistics):

- decode integrity: the file must decode end to end; a truncated or corrupt
  file is reported with the last cleanly decoded frame;
- PTS: every video packet's sorted display time must equal ``index / fps``
  exactly (the integer frame clock) — gaps, duplicates, non-zero starts and
  non-CFR packet durations are defects;
- duration/frame count: decoded frames and container duration are compared
  against the expected contract (e.g. 240 s at 24 fps = 5,760 frames);
- audio: stream count and channel count against the expected contract,
  clipped-sample runs and silence runs measured on the decoded PCM;
- content: black-frame runs and still-frame runs measured on decoded pixels;
- font coverage: the build's sealed cue characters against its sealed
  subtitle font cmap (reuses ``engine.lyrics._font``).

Intended stills, blackouts and silence declared in the plan/timeline (a
legacy ``STATIC`` shot, ``motion_intent: STATIC``, a declared exposure
``hold``, the master's own measured silence, or an explicit ``intended``
declaration) are labelled ``INTENDED``. Undeclared matches stay ``CANDIDATE``
findings a user classifies as ``ACCEPTED_INTENDED`` / ``DEFECT`` /
``NOT_A_DEFECT`` with a recorded reason; the classification is stored in the
report and never changes the artifact.
"""
from pathlib import Path
import platform
import re
import shlex
from fractions import Fraction

import numpy as np

from .core import FilmError, digest, now, object_hash, probe, read, run, \
    safe_path, write
from .frame_clock import check_fps
from .media_verify import _reap, _stream_decode

REPORT_TYPE = "quality_diagnostics"
REPORT_VERSION = 1
DIAGNOSTICS_DIR = "qc/diagnostics"

CLASSIFICATIONS = ("ACCEPTED_INTENDED", "DEFECT", "NOT_A_DEFECT")
SEVERITIES = ("DEFECT", "CANDIDATE", "INTENDED", "INFO")

# A spec violation (corruption, frame loss, off-contract timebase or stream
# shape) can never be excused as an accepted limitation — design doc §10.1.
DEFECT_KINDS = frozenset({
    "PROBE_ERROR", "DECODE_ERROR", "TRUNCATED_DECODE",
    "FRAME_COUNT_MISMATCH", "DURATION_MISMATCH",
    "PTS_NONZERO_START", "PTS_DISCONTINUITY", "PTS_DURATION",
    "STREAM_COUNT", "AUDIO_CHANNELS_MISMATCH",
    "ARTIFACT_HASH_MISMATCH", "FONT_MISSING_GLYPHS"})
CANDIDATE_KINDS = frozenset({
    "BLACK_RUN", "STILL_RUN", "SILENCE_RUN", "CLIPPING_RUN",
    "FONT_UNVERIFIED", "FONT_CHECK_SKIPPED"})
# Candidate kinds that a declared intended region can cover.
INTENDED_MAP = {"BLACK_RUN": "BLACK", "STILL_RUN": "STILL",
                "SILENCE_RUN": "SILENCE"}

# Measurement thresholds — tunable per call, documented defaults.
BLACK_MEAN = 12          # mean channel value (0-255) at or below = black frame
STILL_DIFF = 2           # max per-pixel channel change vs previous frame
CLIP_LEVEL = 32767       # |s16| full-scale sample magnitude
CLIP_MIN_SAMPLES = 4     # clipped samples in one audio window to flag it
SILENCE_PEAK = 92        # |s16| peak at or below ~-51 dBFS = silence
AUDIO_WINDOW_MS = 50     # audio measurement window
MIN_RUN_MS = 250         # runs shorter than this are not reported

_ARTIFACT_ROLES = {"MASTER_SUBBED.mp4": "subbed", "MASTER_CLEAN.mp4": "clean",
                   "DRAFT_PREVIEW.mp4": "draft"}
_INTENDED_KINDS = ("BLACK", "STILL", "SILENCE")
_BUILD_ID = re.compile(r"B[0-9]{4,}")


def _check_build_id(build_id):
    if not _BUILD_ID.fullmatch(str(build_id)):
        raise FilmError("build_id must be a B0001-style build directory name")
    return str(build_id)


def _tool_versions():
    try:
        out = run(["ffmpeg", "-version"], timeout=30).decode(
            errors="replace")
        ffmpeg_version = out.splitlines()[0] if out else "UNKNOWN"
    except Exception:
        ffmpeg_version = "UNKNOWN"
    return {"ffmpeg": ffmpeg_version, "python": platform.python_version()}


def _runs(flags):
    """list[bool] -> list of half-open [start, end) runs of True."""
    result, start = [], None
    for index, flag in enumerate(list(flags) + [False]):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            result.append((start, index))
            start = None
    return result


def _frames_location(start_frame, end_frame, fps):
    return {"stream": "video", "start_frame": start_frame,
            "end_frame": end_frame,
            "start_ms": int(Fraction(start_frame, fps) * 1000),
            "end_ms": int(Fraction(end_frame, fps) * 1000)}


def _ms_location(start_ms, end_ms, stream):
    return {"stream": stream, "start_frame": None, "end_frame": None,
            "start_ms": int(start_ms), "end_ms": int(end_ms)}


def _stream_location(stream):
    return {"stream": stream, "start_frame": None, "end_frame": None,
            "start_ms": None, "end_ms": None}


def _finding(kind, severity, artifact, location, detail, repro,
             provenance=None, measurement=None):
    key_src = {"kind": kind, "artifact": artifact, "location": location,
               "detail": detail}
    return {"key": object_hash(key_src)[:16], "kind": kind,
            "severity": severity, "artifact": artifact, "location": location,
            "detail": detail, "repro": repro, "provenance": provenance,
            "measurement": measurement, "classification": None}


def _covered(location, kind, declared):
    """An intended region covers a finding only over the same ms span and
    only for the matching declaration kind."""
    want = INTENDED_MAP.get(kind)
    if want is None or location.get("start_ms") is None:
        return False
    return any(d["kind"] == want
               and d["start_ms"] <= location["start_ms"]
               and location["end_ms"] <= d["end_ms"]
               for d in declared)


def _repro_decode(path):
    return ("ffmpeg -v error -xerror -nostdin -i " + shlex.quote(str(path))
            + " -f null -")


def _repro_packets(path):
    return ("ffprobe -v error -select_streams v:0 -show_entries "
            "packet=pts,duration -of compact " + shlex.quote(str(path)))


def _repro_video(path, location):
    start = (location["start_ms"] or 0) / 1000
    count = max(1, (location["end_frame"] or 0)
                - (location["start_frame"] or 0))
    return ("ffmpeg -v error -ss " + f"{start:.3f}" + " -i "
            + shlex.quote(str(path)) + " -map 0:v:0 -frames:v "
            + str(count) + " -f null -")


def _repro_audio(path, location):
    start = (location["start_ms"] or 0) / 1000
    end = (location["end_ms"] or 0) / 1000
    return ("ffmpeg -v error -ss " + f"{start:.3f}" + " -to "
            + f"{end:.3f}" + " -i " + shlex.quote(str(path))
            + " -map 0:a:0 -f s16le -")


def parse_intended(text):
    """Parse `KIND:STARTMS-ENDMS,...` declarations (CLI/UI input) into the
    `intended` list shape."""
    regions = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(r"(BLACK|STILL|SILENCE):(\d+)-(\d+)",
                             part.upper())
        if not match:
            raise FilmError("intended regions must be "
                            "KIND:STARTMS-ENDMS with KIND in "
                            + ", ".join(_INTENDED_KINDS))
        regions.append({"kind": match.group(1),
                        "start_ms": int(match.group(2)),
                        "end_ms": int(match.group(3)),
                        "source": "declared"})
    return normalize_intended(regions)


def normalize_intended(intended):
    """Validate caller-declared intended regions: a list of objects with a
    kind and a half-open millisecond span."""
    normalized = []
    for item in intended or []:
        if type(item) is not dict:
            raise FilmError("each intended region must be an object")
        kind = item.get("kind")
        if kind not in _INTENDED_KINDS:
            raise FilmError("intended kind must be one of "
                            + ", ".join(_INTENDED_KINDS))
        start, end = item.get("start_ms"), item.get("end_ms")
        if type(start) is not int or type(end) is not int or end <= start:
            raise FilmError("intended regions need integer start_ms/end_ms")
        normalized.append({"kind": kind, "start_ms": start, "end_ms": end,
                           "source": item.get("source") or "declared"})
    return normalized


def expect_contract(*, fps=24, seconds=None, frames=None,
                    audio_streams=None, audio_channels=None):
    """The expected delivery contract. `seconds` may be any rational —
    e.g. 240 s at 24 fps expects exactly 5,760 frames."""
    fps = check_fps(fps)
    if frames is None and seconds is not None:
        value = Fraction(seconds) * fps
        if value.denominator != 1:
            raise FilmError("Expected duration does not land on a frame "
                            "boundary")
        frames = int(value)
    if frames is not None and (type(frames) is not int or frames < 1):
        raise FilmError("expected frames must be a positive integer")
    return {"fps": fps, "frames": frames,
            "seconds": float(seconds) if seconds is not None else None,
            "audio_streams": audio_streams,
            "audio_channels": audio_channels}


def _check_streams(path, artifact, expect, provenance, findings):
    info = probe(path)
    videos = [s for s in info["streams"] if s["codec_type"] == "video"]
    sounds = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if len(videos) != 1:
        findings.append(_finding(
            "STREAM_COUNT", "DEFECT", artifact, _stream_location("video"),
            f"expected one video track, found {len(videos)}",
            _repro_decode(path), provenance))
    want_audio = expect.get("audio_streams")
    if want_audio is not None and len(sounds) != want_audio:
        findings.append(_finding(
            "STREAM_COUNT", "DEFECT", artifact, _stream_location("audio"),
            f"expected {want_audio} audio track(s), found {len(sounds)}",
            _repro_decode(path), provenance))
    if want_audio is None and len(sounds) != 1:
        findings.append(_finding(
            "STREAM_COUNT", "INFO", artifact, _stream_location("audio"),
            f"{len(sounds)} audio track(s); no channel contract declared",
            _repro_decode(path), provenance))
    if sounds:
        channels = int(sounds[0].get("channels") or 0)
        expected_channels = expect.get("audio_channels")
        if expected_channels is not None and channels != expected_channels:
            findings.append(_finding(
                "AUDIO_CHANNELS_MISMATCH", "DEFECT", artifact,
                _stream_location("audio"),
                f"audio track has {channels} channel(s), the contract "
                f"expects {expected_channels}",
                _repro_audio(path, _ms_location(0, 0, "audio")), provenance))
        elif expected_channels is None:
            findings.append(_finding(
                "AUDIO_CHANNELS_MISMATCH", "INFO", artifact,
                _stream_location("audio"),
                f"audio track has {channels} channel(s); no channel "
                "contract declared",
                _repro_audio(path, _ms_location(0, 0, "audio")), provenance))
    return info


def _check_pts(path, artifact, fps, expected_frames, provenance, findings):
    """Rational packet timing on the sorted display order — the frame clock
    contract (first PTS 0, step 1/fps, last exposure ends at count/fps)."""
    info = probe(path)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"),
                 None)
    if video is None:
        return {}
    time_base = Fraction(video["time_base"])
    step = Fraction(1, fps)
    proc, tail, reader = _stream_decode(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts,duration",
         "-of", "compact=p=0:nk=1", str(path)])
    pts_list, durations = [], []
    bad_rows = 0
    try:
        for line in proc.stdout:
            fields = line.decode(errors="replace").strip().split("|")
            try:
                pts, duration = int(fields[0]), int(fields[1])
            except (IndexError, ValueError):
                bad_rows += 1
                continue
            pts_list.append(pts)
            durations.append(duration)
        code, stderr = _reap(proc, tail, reader)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
            reader.join(timeout=30)
    if code != 0:
        findings.append(_finding(
            "PROBE_ERROR", "DEFECT", artifact, _stream_location("video"),
            "ffprobe packet scan failed: "
            + stderr.decode(errors="replace")[-300:],
            _repro_packets(path), provenance))
        return {}
    packet_count = len(pts_list)
    pts_sorted = sorted(pts_list)
    if pts_sorted and pts_sorted[0] != 0:
        findings.append(_finding(
            "PTS_NONZERO_START", "DEFECT", artifact,
            _frames_location(0, 1, fps),
            f"first display PTS is {pts_sorted[0]} ("
            f"{pts_sorted[0] * time_base}s), the clock requires 0",
            _repro_packets(path), provenance))
    anomalies, duplicates = [], 0
    seen = set()
    for pts in pts_list:
        if pts in seen:
            duplicates += 1
        seen.add(pts)
    for index, pts in enumerate(pts_sorted):
        if pts * time_base != step * index:
            anomalies.append({"index": index,
                              "actual": str(pts * time_base),
                              "expected": str(step * index)})
            if len(anomalies) >= 25:
                break
    if anomalies:
        first = anomalies[0]
        findings.append(_finding(
            "PTS_DISCONTINUITY", "DEFECT", artifact,
            _frames_location(first["index"], first["index"] + 1, fps),
            f"{len(anomalies)}{'+' if len(anomalies) == 25 else ''} "
            "display-time discontinuities; first at index "
            f"{first['index']} shows {first['actual']}s, expected "
            f"{first['expected']}s" +
            (f"; {duplicates} duplicated packet PTS"
             if duplicates else ""),
            _repro_packets(path), provenance))
    bad_durations = sum(1 for d in durations if d * time_base != step)
    if bad_durations:
        findings.append(_finding(
            "PTS_DURATION", "DEFECT", artifact, _stream_location("video"),
            f"{bad_durations} of {len(durations)} packets are not exactly "
            f"{step}s — the stream is not CFR",
            _repro_packets(path), provenance))
    if expected_frames is not None and packet_count != expected_frames:
        findings.append(_finding(
            "FRAME_COUNT_MISMATCH", "DEFECT", artifact,
            _stream_location("video"),
            f"{packet_count} video packets decoded, the contract expects "
            f"{expected_frames} frames",
            _repro_packets(path), provenance))
    if bad_rows:
        findings.append(_finding(
            "PROBE_ERROR", "DEFECT", artifact, _stream_location("video"),
            f"{bad_rows} unparseable packet rows during the PTS scan",
            _repro_packets(path), provenance))
    return {"packet_count": packet_count,
            "pts_anomalies": len(anomalies),
            "non_cfr_packets": bad_durations}


def _scan_video(path, artifact, fps, expected_frames, intended, provenance,
                findings, *, min_run_ms=MIN_RUN_MS):
    """Streamed rgb24 decode: per-frame brightness and frame-to-frame change.

    Runs of black frames and of unchanged frames are measured content
    observations — declared regions are INTENDED, undeclared are CANDIDATE.
    A short or erroring decode is a decode defect with the last good index.
    """
    info = probe(path)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"),
                 None)
    if video is None:
        return {}
    width, height = int(video["width"]), int(video["height"])
    stride = width * height * 3
    proc, tail, reader = _stream_decode(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
         "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    black, still, decoded = [], [], 0
    prev = None
    truncated = False
    try:
        while True:
            buf = proc.stdout.read(stride)
            if not buf:
                break
            if len(buf) != stride:
                truncated = True
                break
            pixels = np.frombuffer(buf, dtype=np.uint8)
            black.append(bool(pixels.reshape(-1, 3).mean() <= BLACK_MEAN))
            if prev is None:
                still.append(False)
            else:
                diff = np.abs(pixels.astype(np.int16)
                              - prev.astype(np.int16))
                still.append(int(diff.max()) <= STILL_DIFF)
            prev = pixels
            decoded += 1
        code, stderr = _reap(proc, tail, reader)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
            reader.join(timeout=30)
    error_text = stderr.decode(errors="replace").strip()
    if code != 0 or error_text:
        detail = "; ".join(error_text.splitlines()[-3:])[:400] \
            if error_text else f"decoder exited {code}"
        findings.append(_finding(
            "DECODE_ERROR", "DEFECT", artifact,
            _frames_location(decoded, decoded + 1, fps),
            f"video decode failed after {decoded} clean frames: {detail}",
            _repro_decode(path), provenance))
    if truncated:
        findings.append(_finding(
            "TRUNCATED_DECODE", "DEFECT", artifact,
            _frames_location(decoded, decoded + 1, fps),
            f"truncated frame after {decoded} clean frames — a partial "
            "frame means the file is damaged",
            _repro_decode(path), provenance))
    if expected_frames is not None and decoded != expected_frames:
        findings.append(_finding(
            "FRAME_COUNT_MISMATCH", "DEFECT", artifact,
            _frames_location(decoded, decoded + 1, fps),
            f"{decoded} frames decoded, the contract expects "
            f"{expected_frames}",
            _repro_decode(path), provenance))
    min_run = max(2, int(round(Fraction(min_run_ms, 1000) * fps)))
    black_runs = _runs(black)
    for start, end in black_runs:
        if end - start < min_run:
            continue
        location = _frames_location(start, end, fps)
        severity = "INTENDED" if _covered(location, "BLACK_RUN",
                                          intended) else "CANDIDATE"
        findings.append(_finding(
            "BLACK_RUN", severity, artifact, location,
            f"{end - start} consecutive black frames",
            _repro_video(path, location), provenance,
            measurement={"frames": end - start}))
    for start, end in _runs(still):
        if end - start < min_run:
            continue
        # A run that is entirely inside a reported black run is the black
        # run, not a separate still.
        if any(b_start <= start and end <= b_end
               for b_start, b_end in black_runs):
            continue
        location = _frames_location(start, end, fps)
        severity = "INTENDED" if _covered(location, "STILL_RUN",
                                          intended) else "CANDIDATE"
        findings.append(_finding(
            "STILL_RUN", severity, artifact, location,
            f"{end - start} consecutive unchanged frames",
            _repro_video(path, location), provenance,
            measurement={"frames": end - start}))
    return {"decoded_frames": decoded, "black_frames": sum(black),
            "still_frames": sum(still)}


def _scan_audio(path, artifact, fps, intended, provenance, findings, *,
                min_run_ms=MIN_RUN_MS):
    """Streamed s16le decode: clipped-sample runs and silence runs."""
    info = probe(path)
    sound = next((s for s in info["streams"]
                  if s["codec_type"] == "audio"), None)
    if sound is None:
        return {}
    rate = int(sound["sample_rate"])
    channels = int(sound.get("channels") or 1)
    window = max(1, rate * AUDIO_WINDOW_MS // 1000) * channels
    proc, tail, reader = _stream_decode(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
         "-map", "0:a:0", "-f", "s16le", "-"])
    silent, clipped, samples, max_abs = [], [], 0, 0
    pending = b""

    def _measure(block_bytes):
        nonlocal samples, max_abs
        block = np.frombuffer(block_bytes, dtype=np.int16)
        if block.size == 0:
            return
        peak = int(np.abs(block.astype(np.int32)).max())
        max_abs = max(max_abs, peak)
        silent.append(peak <= SILENCE_PEAK)
        clipped.append(int((block == CLIP_LEVEL).sum()
                           + (block == -CLIP_LEVEL - 1).sum())
                       >= CLIP_MIN_SAMPLES)
        samples += block.size // channels

    try:
        while True:
            chunk = proc.stdout.read(1 << 20)
            if not chunk:
                break
            pending += chunk
            usable = len(pending) // (window * 2) * (window * 2)
            for offset in range(0, usable, window * 2):
                _measure(pending[offset:offset + window * 2])
            pending = pending[usable:]
        if len(pending) >= 2:
            _measure(pending[:len(pending) // 2 * 2])
        code, stderr = _reap(proc, tail, reader)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
            reader.join(timeout=30)
    error_text = stderr.decode(errors="replace").strip()
    if code != 0 or error_text:
        detail = "; ".join(error_text.splitlines()[-3:])[:400] \
            if error_text else f"decoder exited {code}"
        findings.append(_finding(
            "DECODE_ERROR", "DEFECT", artifact,
            _ms_location(int(Fraction(samples, rate) * 1000),
                         int(Fraction(samples, rate) * 1000), "audio"),
            f"audio decode failed after {samples} samples: {detail}",
            _repro_decode(path), provenance))
    window_ms = AUDIO_WINDOW_MS
    for start, end in _runs(silent):
        if (end - start) * window_ms < min_run_ms:
            continue
        location = _ms_location(start * window_ms, end * window_ms, "audio")
        severity = "INTENDED" if _covered(location, "SILENCE_RUN",
                                          intended) else "CANDIDATE"
        findings.append(_finding(
            "SILENCE_RUN", severity, artifact, location,
            f"{(end - start) * window_ms} ms of audio silence",
            _repro_audio(path, location), provenance,
            measurement={"ms": (end - start) * window_ms}))
    for start, end in _runs(clipped):
        location = _ms_location(start * window_ms, end * window_ms, "audio")
        findings.append(_finding(
            "CLIPPING_RUN", "CANDIDATE", artifact, location,
            f"clipped samples across {(end - start) * window_ms} ms "
            f"(window peak reaches full scale)",
            _repro_audio(path, location), provenance,
            measurement={"windows": end - start}))
    peak_db = None
    if max_abs:
        peak_db = round(20 * np.log10(max_abs / 32768), 1)
    return {"decoded_samples": samples, "sample_rate": rate,
            "channels": channels, "peak_abs": max_abs,
            "peak_dbfs": peak_db,
            "silent_windows": sum(silent), "clipped_windows": sum(clipped)}


def _check_font(project, build_dir, artifact, provenance, findings,
                build_id):
    """Subtitle glyph coverage of the cues this build burned against the
    font it sealed (`lyrics_timed.json`, `subtitle_fonts/`) — the same cmap
    check Final gates on, reported here as measurement. Only sealed build
    bytes are read: the live project's font and lyric timing are neither
    consulted nor rewritten."""
    repro = ("python -m engine.cli diagnose " + shlex.quote(str(project))
             + " --build " + shlex.quote(str(build_id)))
    try:
        from .lyrics import _font
        timed = safe_path(build_dir, "lyrics_timed.json")
        if not timed.is_file():
            raise FilmError(f"{build_id} sealed no lyrics_timed.json")
        document = read(timed)
        text = "".join(cue["text"] for cue in document.get("cues", []))
        sealed = read(safe_path(build_dir, "subtitle_report.json"), {})
        family = (sealed.get("font") or {}).get("requested_family") \
            or "DejaVu Sans"
        fonts = sorted(safe_path(build_dir, "subtitle_fonts")
                       .glob("subtitle.*"))
        if fonts:
            _, report = _font(build_dir, {"subtitles": {
                "font_name": family,
                "font_file": str(fonts[0].relative_to(build_dir))}}, text)
        else:
            report = {"status": "unverified", "missing_codepoints": [],
                      "family": family,
                      "note": f"{build_id} sealed no subtitle font file"}
    except (FilmError, KeyError, TypeError, ValueError, AttributeError) as e:
        findings.append(_finding(
            "FONT_CHECK_SKIPPED", "CANDIDATE", artifact,
            _stream_location("subtitle"),
            f"font coverage check could not run: {str(e)[:200]}",
            repro, provenance))
        return {}
    status = report.get("status")
    missing = report.get("missing_codepoints") or []
    if status == "missing_glyphs":
        findings.append(_finding(
            "FONT_MISSING_GLYPHS", "DEFECT", artifact,
            _stream_location("subtitle"),
            f"{len(missing)} cue characters have no glyph: "
            + ", ".join(missing[:20]),
            repro, provenance,
            measurement={"missing": missing}))
    elif status != "verified":
        findings.append(_finding(
            "FONT_UNVERIFIED", "CANDIDATE", artifact,
            _stream_location("subtitle"),
            f"font glyph coverage is {status}; Final requires a verified "
            "full-coverage font"
            + (f" ({report['note']})" if report.get("note") else ""),
            repro, provenance))
    return {"font_status": status, "font_family": report.get("family"),
            "font_sha256": report.get("sha256"),
            "missing_codepoints": missing}


def _summarize(findings):
    counts = {s: sum(1 for f in findings if f["severity"] == s)
              for s in SEVERITIES}
    counts["unclassified_candidates"] = sum(
        1 for f in findings
        if f["severity"] in ("CANDIDATE", "INTENDED")
        and not f.get("classification"))
    return counts


def _number_findings(report):
    for index, finding in enumerate(report["findings"], 1):
        finding["id"] = f"F{index:03d}"
    return report


def diagnose_media(path, *, expect=None, intended=None, provenance=None):
    """Diagnose one media file. Returns the report document; nothing is
    written and nothing about the file is changed.

    `expect` is the delivery contract (`expect_contract`); `intended` is a
    list of declared regions (`normalize_intended`); `provenance` is an
    optional free-form link (build record, render manifest digest, encode
    digest) attached to every finding.
    """
    path = Path(path)
    if not path.is_file():
        raise FilmError(f"Missing file: {path}")
    expect = expect or {"fps": 24}
    fps = check_fps(expect.get("fps", 24))
    declared = normalize_intended(intended)
    artifact = path.name
    findings = []
    try:
        info = _check_streams(path, artifact, expect, provenance, findings)
    except FilmError as e:
        findings.append(_finding(
            "PROBE_ERROR", "DEFECT", artifact,
            _stream_location("container"),
            f"ffprobe could not parse the file: {str(e)[:300]}",
            _repro_decode(path), provenance))
        info = {"streams": [], "format": {}}
    videos = [s for s in info.get("streams", [])
              if s["codec_type"] == "video"]
    video_measurement, audio_measurement = {}, {}
    if videos:
        video_measurement = _check_pts(
            path, artifact, fps, expect.get("frames"), provenance, findings)
        video_measurement.update(_scan_video(
            path, artifact, fps, expect.get("frames"), declared,
            provenance, findings))
        audio_measurement = _scan_audio(
            path, artifact, fps, declared, provenance, findings)
    duration = None
    try:
        duration = float(info.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = None
    if expect.get("seconds") is not None and duration is not None:
        tolerance = 1 / fps + .03
        if abs(duration - float(expect["seconds"])) > tolerance:
            findings.append(_finding(
                "DURATION_MISMATCH", "DEFECT", artifact,
                _stream_location("container"),
                f"container duration {duration:.3f}s differs from the "
                f"contract {float(expect['seconds']):.3f}s by more than "
                "one frame",
                "ffprobe -v error -show_format "
                + shlex.quote(str(path)), provenance))
    stream_count = len(info.get("streams", []))
    report = {
        "report_type": REPORT_TYPE,
        "report_version": REPORT_VERSION,
        "not_an_approval": True,
        "note": "Technical measurements only — this report is not a Final "
                "approval, an aesthetic score or a review binding. Classify "
                "candidate findings by hand; DEFECT findings mark spec "
                "violations that acceptance cannot excuse.",
        "created_at": now(),
        "tool": _tool_versions(),
        "subject": {"kind": "file", "path": str(path),
                    "sha256": digest(path)},
        "artifacts": {artifact: {
            "path": str(path), "sha256": digest(path),
            "provenance": provenance,
            "container": info.get("format", {}).get("format_name"),
            "duration": duration, "streams": stream_count,
            "expect": expect,
            "measurements": {"video": video_measurement,
                             "audio": audio_measurement}}},
        "intended": declared,
        "checks": ["streams", "pts", "duration", "decode", "black_frames",
                   "still_frames", "audio_channels", "clipping", "silence"],
        "findings": findings,
    }
    report["summary"] = _summarize(findings)
    return _number_findings(report)


def _build_expect(record, build_dir):
    """The delivery contract a sealed build promises: integer frames on the
    24 fps clock and the captured master's own audio shape."""
    fmt = record.get("format") or {}
    fps = int(fmt.get("fps") or 24)
    frames = record.get("output_frames")
    seconds = None
    if frames is None and record.get("duration_ms") is not None:
        duration_ms = int(record["duration_ms"])
        from .core import frame_at
        frames = frame_at(duration_ms, fps)
        seconds = Fraction(duration_ms, 1000)
    elif frames is not None:
        seconds = Fraction(frames, fps)
    channels, audio_streams = None, None
    audio_rel = (record.get("audio") or {}).get("path")
    if audio_rel:
        master = safe_path(build_dir, audio_rel)
        if master.is_file():
            try:
                sound = next(s for s in probe(master)["streams"]
                             if s["codec_type"] == "audio")
                channels = int(sound.get("channels") or 0) or None
                audio_streams = 1
            except (FilmError, StopIteration, KeyError, ValueError):
                channels = None
    return expect_contract(fps=fps, frames=frames, seconds=seconds,
                           audio_streams=audio_streams,
                           audio_channels=channels)


def _bound_plan_digests(project, record):
    """shot_id -> motion plan digests the build's own CUT reviews bound.

    A Build 2 record names the review ids that authorized it; each CUT
    review binds `motion_plan_sha256`, so a live plan with those bytes is
    the plan the build was compiled under. The review log is append-only.
    """
    reviews = record.get("reviews")
    cuts = reviews.get("cuts") if type(reviews) is dict else None
    if type(cuts) is not dict or not cuts:
        return {}
    review_ids = {v for v in cuts.values() if type(v) is str}
    try:
        from .animation_review import load_reviews
        rows = load_reviews(project)
    except FilmError:
        return {}
    bound = {}
    for row in rows:
        if row.get("scope") == "CUT" and row.get("review_id") in review_ids \
                and row.get("motion_plan_sha256"):
            bound.setdefault(row.get("shot_id"), set()).add(
                row["motion_plan_sha256"])
    return bound


def intended_regions(project, record=None, build_dir=None):
    """Stills, blackouts and silence the plan/timeline itself declares.

    - a legacy `STATIC` shot is the explicit editorial choice for an
      intended still over its frame range — `render_mode` is a LEGACY_MV
      field, so it is read only on the legacy profile;
    - an `animation_shot_plan` with `motion_intent: STATIC` declares the
      same intent for that cut's output range, and an exposure slot's
      `hold` flag declares the still over the frames every static layer
      holds — a null transform is only static identity, never a
      declaration of intent;
    - the master's own measured `silence_regions` make matching output
      silence inherent to the song, not a defect.

    With `build_dir` the declarations are the ones that build sealed: its
    `snapshot/` copy of the project, shot list, timeline and audio
    analysis. Motion plans are not snapshotted, so a live plan counts only
    when its bytes are the ones the build's CUT reviews bound — a later
    edit to the live project never relabels an already sealed MP4.
    """
    return _declarations(project, record, build_dir)[0]


def _declarations(project, record=None, build_dir=None):
    """(declared regions, declarations ignored because the build did not
    seal them)."""
    p = Path(project)
    declared, unbound = [], []
    record = record or {}
    src = p if build_dir is None else safe_path(build_dir, "snapshot")
    bound = {} if build_dir is None else _bound_plan_digests(p, record)
    config = read(safe_path(src, "project.yaml"), {}) or {}
    fmt = record.get("format") or config.get("format") or {}
    fps = int(fmt.get("fps") or 24)
    profile = record.get("profile") or config.get("production_profile") \
        or "LEGACY_MV"
    shots_path = safe_path(src, "manifest/shots.json")
    if profile == "LEGACY_MV" and shots_path.is_file():
        try:
            shots = read(shots_path) or []
        except FilmError:
            shots = []
        from .core import frame_at
        for shot in shots:
            if shot.get("render_mode") != "STATIC":
                continue
            start = frame_at(shot["in_ms"], fps)
            end = frame_at(shot["out_ms"], fps)
            declared.append({
                "kind": "STILL", "start_ms": int(Fraction(start, fps) * 1000),
                "end_ms": int(Fraction(end, fps) * 1000),
                "source": f"{shot['id']} render_mode STATIC"})
    if profile == "FRAME_ANIMATION_V1":
        try:
            from .animation_schema import (load_animation_timeline,
                                           read_canon,
                                           validate_animation_timeline)
            from .motion_plan import plan_path
            document = load_animation_timeline(src)
            layout = validate_animation_timeline(document)
            for entry in layout["entries"]:
                plan_file = safe_path(src, plan_path(entry["shot_id"]))
                if build_dir is not None and not plan_file.is_file():
                    plan_file = safe_path(p, plan_path(entry["shot_id"]))
                    if plan_file.is_file() and digest(plan_file) \
                            not in bound.get(entry["shot_id"], ()):
                        unbound.append(f"{entry['shot_id']} plan.json is "
                                       "not the plan this build's CUT "
                                       "reviews bound")
                        continue
                if not plan_file.is_file():
                    continue
                try:
                    plan = read_canon(plan_file)
                except FilmError:
                    continue
                if type(plan) is not dict:
                    continue
                out_start, out_end = entry["output_range"]
                if plan.get("motion_intent") == "STATIC":
                    # STATIC is the explicit human intent declaration
                    # (schema §5); it is never inferred from transforms.
                    declared.append({
                        "kind": "STILL",
                        "start_ms": int(Fraction(out_start, fps) * 1000),
                        "end_ms": int(Fraction(out_end, fps) * 1000),
                        "source": f"{entry['shot_id']} "
                                  "motion_intent STATIC"})
                    continue
                tracks = plan.get("tracks") or {}
                camera = tracks.get("camera") if type(tracks) is dict \
                    else None
                layers = tracks.get("layers") if type(tracks) is dict \
                    else None
                if type(camera) is not dict \
                        or camera.get("transform") is not None \
                        or type(layers) is not dict or not layers:
                    continue
                # Inside an ANIMATED cut an intended still exists only
                # where every layer's exposure carries the contract's
                # declared `hold` over the same frames.
                common = [(0, out_end - out_start)]
                for layer in layers.values():
                    if type(layer) is not dict \
                            or layer.get("transform") is not None:
                        common = []
                        break
                    holds = [(s["start"], s["end"])
                             for schedule in layer.get("exposure") or []
                             if type(schedule) is dict
                             for s in schedule.get("slots") or []
                             if type(s) is dict and s.get("hold") is True
                             and type(s.get("start")) is int
                             and type(s.get("end")) is int]
                    common = [(max(a, c), min(b, d))
                              for a, b in common for c, d in holds
                              if max(a, c) < min(b, d)]
                for start, end in common:
                    declared.append({
                        "kind": "STILL",
                        "start_ms": int(Fraction(out_start + start, fps)
                                        * 1000),
                        "end_ms": int(Fraction(out_start + end, fps)
                                      * 1000),
                        "source": f"{entry['shot_id']} declared "
                                  "exposure hold"})
        except FilmError:
            pass
    analysis_path = safe_path(src, "analysis/audio.json")
    if build_dir is not None and not analysis_path.is_file() \
            and (p / "analysis/audio.json").is_file():
        unbound.append("analysis/audio.json silence_regions are not "
                       "sealed in this build")
    if analysis_path.is_file():
        analysis = read(analysis_path, {}) or {}
        for region in analysis.get("silence_regions") or []:
            if type(region.get("in_ms")) is int \
                    and type(region.get("out_ms")) is int \
                    and region["out_ms"] > region["in_ms"]:
                # The analyzer's ~-45 dBFS hop-grid regions and this
                # decoder's 50 ms silence windows measure the same span
                # at different resolutions; one window of edge tolerance
                # keeps the master's own declared silence INTENDED.
                declared.append({
                    "kind": "SILENCE",
                    "start_ms": max(0, region["in_ms"] - AUDIO_WINDOW_MS),
                    "end_ms": region["out_ms"] + AUDIO_WINDOW_MS,
                    "source": "measured master silence_regions"})
    return declared, unbound


def _artifact_provenance(record, build_dir, name, role):
    provenance = {"build_id": record.get("build_id"),
                  "build_json_sha256": digest(build_dir / "build.json"),
                  "mode": record.get("mode")
                  or record.get("document_type"),
                  "artifact": name}
    if record.get("edit_digest"):
        provenance["edit_digest"] = record["edit_digest"]
    outputs = record.get("outputs") or {}
    encoding = record.get("encoding") or {}
    sequences = record.get("sequences") or {}
    recorded_sha = None
    if role in outputs:
        recorded_sha = outputs[role].get("sha256")
        provenance["output_role"] = role
    files = record.get("files") or {}
    if recorded_sha is None and name in files:
        recorded_sha = files[name]
    if role in (encoding or {}):
        provenance["encode_digest"] = encoding[role].get("encode_digest")
    root_key = f"{role}_sequence_root"
    if sequences.get(root_key):
        provenance["sequence_root"] = sequences[root_key]
    provenance["artifact_sha256_recorded"] = recorded_sha
    return provenance, recorded_sha


def diagnose_build(project, build_id, *, intended=None, artifacts=None,
                   store=True):
    """Diagnose a build's delivery MP4s and store the report.

    Each finding links to the sealed build's provenance: the build id, the
    build.json hash, the recorded artifact hash and — for Build 2 animation
    builds — the encode digest and sequence root. The report lands in
    `qc/diagnostics/<build_id>.json`; artifact bytes are never touched.
    """
    p = Path(project)
    _check_build_id(build_id)
    build_dir = safe_path(p, f"builds/{build_id}")
    record_path = safe_path(build_dir, "build.json")
    if not record_path.is_file():
        raise FilmError(f"No build record for {build_id}")
    record = read(record_path)
    expect = _build_expect(record, build_dir)
    sealed, unbound = _declarations(p, record, build_dir)
    declared = sealed + normalize_intended(intended)
    # safe_path keeps every delivery read inside the build directory — a
    # symlink pointing elsewhere is refused, never decoded.
    names = [name for name in _ARTIFACT_ROLES
             if safe_path(build_dir, name).is_file()]
    if artifacts:
        names = [n for n in names if n in artifacts]
    if not names:
        raise FilmError(f"{build_id} has no diagnosable delivery MP4 "
                        "(MASTER_*.mp4 or DRAFT_PREVIEW.mp4)")
    base_provenance = {"build_id": build_id,
                       "build_json_sha256": digest(record_path),
                       "mode": record.get("mode")
                       or record.get("document_type")}
    if record.get("edit_digest"):
        base_provenance["edit_digest"] = record["edit_digest"]
    report = {
        "report_type": REPORT_TYPE,
        "report_version": REPORT_VERSION,
        "not_an_approval": True,
        "note": "Technical measurements only — this report is not a Final "
                "approval, an aesthetic score or a review binding. Classify "
                "candidate findings by hand; DEFECT findings mark spec "
                "violations that acceptance cannot excuse.",
        "created_at": now(),
        "tool": _tool_versions(),
        "subject": {"kind": "build", "project": str(p),
                    "build_id": build_id,
                    "build_json_sha256": base_provenance
                    ["build_json_sha256"]},
        "artifacts": {},
        "intended": declared,
        "intended_unbound": unbound,
        "checks": ["streams", "pts", "duration", "decode", "black_frames",
                   "still_frames", "audio_channels", "clipping", "silence",
                   "artifact_hash", "font_coverage"],
        "build_status": record.get("status"),
        "findings": []}
    for name in names:
        role = _ARTIFACT_ROLES[name]
        target = build_dir / name
        provenance, recorded_sha = _artifact_provenance(
            record, build_dir, name, role)
        sub = diagnose_media(target, expect=dict(expect),
                             intended=declared,
                             provenance=provenance)
        actual_sha = digest(target)
        artifact_findings = sub["findings"]
        if recorded_sha and actual_sha != recorded_sha:
            artifact_findings.append(_finding(
                "ARTIFACT_HASH_MISMATCH", "DEFECT", name,
                _stream_location("container"),
                f"file hash {actual_sha[:16]}… differs from the recorded "
                f"{recorded_sha[:16]}… — the artifact changed after "
                "sealing",
                "python -m engine.cli diagnose "
                + shlex.quote(str(p)) + " --build " + build_id,
                provenance))
        provenance["artifact_sha256"] = actual_sha
        report["findings"].extend(artifact_findings)
        report["artifacts"][name] = sub["artifacts"][name]
        report["artifacts"][name]["provenance"] = provenance
    font_findings = []
    report["font"] = _check_font(p, build_dir, names[0], base_provenance,
                                 font_findings, build_id)
    report["findings"].extend(font_findings)
    report["summary"] = _summarize(report["findings"])
    _number_findings(report)
    if store:
        target = report_path(p, build_id)
        # A re-run of the same build keeps the user's classifications on
        # identical findings — the judgement binds the measured finding,
        # not the run.
        previous = read(target, {}) if target.is_file() else {}
        remembered = {f["key"]: f["classification"]
                      for f in previous.get("findings", [])
                      if f.get("classification")}
        for finding in report["findings"]:
            if finding["key"] in remembered:
                finding["classification"] = remembered[finding["key"]]
        report["summary"] = _summarize(report["findings"])
        write(target, report)
        report["report_path"] = str(target)
    return report


def report_path(project, build_id):
    return safe_path(project, f"{DIAGNOSTICS_DIR}/"
                              f"{_check_build_id(build_id)}.json")


def load_report(project, build_id):
    path = report_path(project, build_id)
    if not path.is_file():
        return None
    return read(path)


def classify_finding(project, build_id, finding, decision, reason, reviewer):
    """Classify one finding with a recorded reason.

    Candidates become ACCEPTED_INTENDED / DEFECT / NOT_A_DEFECT — the user's
    judgement on measured evidence, stored in the report. A DEFECT finding
    is a spec violation and can never be classified ACCEPTED_INTENDED; the
    artifact itself is never modified.
    """
    if decision not in CLASSIFICATIONS:
        raise FilmError("decision must be one of "
                        + ", ".join(CLASSIFICATIONS))
    if not str(reason).strip() or not str(reviewer).strip():
        raise FilmError("a classification needs a reason and a reviewer")
    path = report_path(project, build_id)
    if not path.is_file():
        raise FilmError(f"No diagnostics report for {build_id}; run the "
                        "diagnosis first")
    report = read(path)
    match = next((f for f in report["findings"]
                  if f.get("id") == finding or f.get("key") == finding),
                 None)
    if match is None:
        raise FilmError(f"No finding {finding} in the {build_id} report")
    if match["severity"] == "DEFECT" and decision == "ACCEPTED_INTENDED":
        raise FilmError("a spec violation cannot be excused as "
                        "ACCEPTED_INTENDED — fix the artifact or classify "
                        "it DEFECT")
    match["classification"] = {"decision": decision,
                               "reason": str(reason).strip(),
                               "reviewer": str(reviewer).strip(),
                               "classified_at": now()}
    report["summary"] = _summarize(report["findings"])
    write(path, report)
    return report
