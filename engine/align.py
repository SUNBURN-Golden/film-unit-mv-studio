"""Proposed lyric-line timings for a sung vocal, aligned against the lyric sheet.

claudia-bot's alignment (``slop/audio/align.py`` + ``merge.py``, MIT © 2026
anabology — https://github.com/anabologyco-maker/claudia-bot) is a wav2vec2
CTC *forced* aligner on Latin characters, merged with Whisper where its
confidence is low. Its Latin-only normalisation (``[^a-z]``) erases Hangul, so
the Korean path here is built new, and the real-song run (docs/CLAUDIA_PORT.md
§5) decided its shape:

* **Free transcription is not the anchor.** Local Whisper transcribing sung
  Korean dropped whole verses or looped on hallucinations, so fuzzy-matching
  its words to the sheet anchored repeated choruses to the wrong repeat.
* **Forced alignment of the sheet text.** Whisper's own cross-attention
  alignment (faster-whisper ``find_alignment``: decoder token probabilities +
  DTW over the alignment heads; MIT weights, no torch) is given the sheet line
  and the vocal stem. A line is placed where it is *the first thing sung* in a
  30 s window: the window starts just before a vocal onset and the line's mean
  token probability must reach ``FORCE_MIN``. Candidates are vocal onsets plus
  a dense 0.25 s grid right after the previous line (lines often follow
  without a pause).
* **Ported claudia correction rules.** Energy-rise start (first 10 ms frame
  above 0.3 x the first token's peak; tokens < 0.45 s keep their start —
  searched only in the token's last 2.5 s, because a forced first token
  absorbs any lead-in), interpolation of unplaced lines between placed
  neighbours, an ear-check list.
* **New consistency rules.** A line spanning more than 1.6 x its expected
  length has usually swallowed the previous line, which was then placed too
  early on unrelated singing: the pair is re-forced from the long line's start
  and the previous line moves there when both pass. Repeated sections whose
  line offsets disagree with the first occurrence by > 0.4 s go to the ear
  check.

Compute note: CTranslate2's int8 kernels returned different encoder outputs
for identical input on the run machine's CPU; float32 was bit-stable, so it is
the default ``compute_type``.

Nothing here is auto-approved. ``lyrics/align_proposal.json`` is shaped for
``lyrics-import`` (review empty); ``lyrics/align_report.json`` carries the
evidence and the ear-check list. ``lyrics/lyrics_timed.json`` is only written
by ``lyrics-import`` and the review fingerprint stays the gate.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path

import numpy as np

from .core import FilmError, now, project_mutex, read, require_legacy_profile, safe_path, write

SCHEMA_VERSION = 1
SR = 16000                       # recognizer sample rate
WIN_S = 30.0                     # Whisper's window
FORCE_MIN = 0.4                  # mean token probability for a forced line to count as placed
LEAD_S = 0.2                     # a window starts this much before its candidate onset
NEAR_STEP_S, NEAR_N = 0.25, 13   # dense candidates 0–3 s after the previous line
HORIZON_S = 90.0                 # search reach past the previous line
OUTLIER = 1.6                    # span > OUTLIER x expected -> repair, else ear check
REPEAT_TOL_S = 0.4               # repeat-section offset disagreement listed for the ear
EAR_CONF = 0.55                  # placed lines below this confidence go to the ear check
LINE_MIN_S = 0.6                 # interpolation floor per line

HANGUL = re.compile(r"[가-힣ᄀ-ᇿ]")


def norm_text(text):
    """Hangul-safe normalisation (claudia's is ``[^a-z]``): NFC, keep Hangul
    syllables/jamo, letters and digits, case-folded."""
    text = unicodedata.normalize("NFC", str(text))
    return "".join(c for c in text if HANGUL.match(c) or c.isalnum()).lower()


def read_rows(project):
    """Effective lyric rows. Repeat markers left unexpanded are expanded to their
    suggested source section — part of the proposal the reviewer confirms."""
    from .lyrics import _effective_rows, prepare_lyrics
    document = prepare_lyrics(project)
    expansions = dict(document.get("repeat_expansions") or {})
    for row in document["rows"]:
        if row["kind"] == "repeat" and row["id"] not in expansions:
            expansions[row["id"]] = row["suggested_source_row_ids"]
    effective, unresolved = _effective_rows({**document, "repeat_expansions": expansions})
    if unresolved:
        raise FilmError("가사 반복 표시를 연결할 수 없습니다: " + ", ".join(unresolved[:8]))
    rows = [r for r in effective if r["kind"] == "lyric" and norm_text(r["text"])]
    if not rows:
        raise FilmError("input/lyrics.txt has no lyric lines to align")
    return document, expansions, rows


# ---- audio ---------------------------------------------------------------------------

def load_audio(path):
    """Any audio file -> mono float32 at 16 kHz through FFmpeg."""
    exe = shutil.which("ffmpeg")
    if not exe:
        raise FilmError("ffmpeg is not installed or on PATH")
    r = subprocess.run([exe, "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise FilmError(f"ffmpeg could not decode {Path(path).name}: {r.stderr.decode(errors='replace')[-300:]}")
    return np.frombuffer(r.stdout, np.float32).copy()


def energy(audio):
    """10 ms RMS frames."""
    hop = SR // 100
    n = len(audio) // hop
    if not n:
        return np.zeros(0, np.float32)
    return np.sqrt(np.add.reduceat(audio[:n * hop] ** 2, np.arange(0, n * hop, hop)) / hop)


def voiced(R, rel=0.12):
    return R > rel * np.percentile(R, 95) if len(R) else np.zeros(0, bool)


def onsets(V, min_gap_s=0.1, min_run_s=0.1):
    """Starts (s) of voiced runs that follow at least ``min_gap_s`` of quiet."""
    out, quiet, i, n = [], 10 ** 9, 0, len(V)
    while i < n:
        if V[i]:
            j = i
            while j < n and V[j]:
                j += 1
            if quiet >= min_gap_s * 100 and (j - i) >= min_run_s * 100:
                out.append(round(i / 100, 2))
            quiet, i = 0, j
        else:
            quiet, i = quiet + 1, i + 1
    return out


def rise(R, word):
    """claudia energy-rise start of a line's first token (lead-in capped to 2.5 s)."""
    t0, t1 = word["t0"], word["t1"]
    if t1 - t0 < 0.45 or not len(R):
        return t0
    lo = max(t0, t1 - 2.5)
    a, b = int(lo * 100), int((t1 - 0.12) * 100)
    seg = R[a:b]
    if not len(seg):
        return t0
    hit = np.flatnonzero(seg > 0.3 * R[a:int(t1 * 100) + 1].max())
    return lo + hit[0] / 100 if len(hit) else t0


def line_end(V, words):
    """Last voiced frame inside the line's last token (+0.1 s), never past its end."""
    w = words[-1]
    a, b = int(w["t0"] * 100), int(w["t1"] * 100)
    hit = np.flatnonzero(V[a:b])
    return min(w["t1"], (a + hit[-1]) / 100 + 0.1) if len(hit) else w["t1"]


def mean_conf(words):
    return float(np.mean([w["conf"] for w in words])) if words else 0.0


def _words(ws):
    return [[w["w"].strip(), round(w["t0"], 2), round(w["t1"], 2), round(w["conf"], 2)] for w in ws]


# ---- the forced aligner ---------------------------------------------------------------

class WhisperForcedAligner:
    """Local faster-whisper (MIT) forced alignment of given text. No network API;
    the model downloads once from huggingface.co on first use."""

    def __init__(self, model_size="small", compute_type="float32", language="ko", threads=0):
        try:
            from faster_whisper import WhisperModel
            from faster_whisper.tokenizer import Tokenizer
        except ImportError:
            raise FilmError("로컬 정렬기를 찾을 수 없습니다: python -m pip install faster-whisper 뒤 다시 "
                            "실행하세요 (모델은 처음 한 번 huggingface.co에서 내려받습니다). "
                            "설치하지 않고 타이밍을 추측하지 않습니다.") from None
        self.model = WhisperModel(model_size, device="cpu", compute_type=compute_type, cpu_threads=threads)
        self.tokenizer = Tokenizer(self.model.hf_tokenizer, self.model.model.is_multilingual,
                                   task="transcribe", language=language)
        self.id = f"faster-whisper:{model_size}:{compute_type}:forced"
        self._cache = {}

    def _encode(self, audio, t0):
        key = (round(t0, 2), len(audio))
        if key not in self._cache:
            if len(self._cache) > 64:
                self._cache.clear()
            feats = self.model.feature_extractor(audio)
            frames = feats.shape[-1]
            feats = np.ascontiguousarray(np.pad(feats, ((0, 0), (0, max(0, 3000 - frames))))[:, :3000],
                                         dtype=np.float32)
            # keep `feats` referenced: the StorageView borrows its memory
            self._cache[key] = (self.model.encode(feats), min(frames, 3000), feats)
        return self._cache[key][:2]

    def force(self, audio, t0, lines):
        """Per line, per word: {w, t0, t1, conf} on the song clock, or None."""
        enc, frames = self._encode(audio, t0)
        tokens = self.tokenizer.encode(" " + " ".join(lines))
        try:
            words = self.model.find_alignment(self.tokenizer, [tokens], enc, frames)[0]
        except IndexError:
            return None
        out, k = [], 0
        for line in lines:
            count = len(line.split())
            chunk = words[k:k + count]
            k += count
            if len(chunk) < count:
                return None
            out.append([{"w": w["word"], "t0": t0 + float(w["start"]), "t1": t0 + float(w["end"]),
                         "conf": float(w["probability"])} for w in chunk])
        return out


def _window(audio, start):
    return audio[int(start * SR):int((start + WIN_S) * SR)]


def place_lines(backend, audio, texts, tau=FORCE_MIN):
    """Each line where it is the first thing sung after the previous line."""
    R = energy(audio)
    V = voiced(R)
    starts = onsets(V)
    duration = len(audio) / SR
    placed, cursor, tried = [], 0.0, 0
    for i in range(len(texts)):
        near = [round(cursor + NEAR_STEP_S * k, 2) for k in range(NEAR_N)] if i and placed[-1] else []
        candidates = sorted(set(near) | {o for o in starts if cursor <= o <= cursor + HORIZON_S})
        hit = None
        for o in candidates:
            if o > duration - 1:
                break
            w0 = max(0.0, o - LEAD_S)
            result = backend.force(_window(audio, w0), w0, texts[i:i + 2])
            tried += 1
            if not result:
                continue
            words = result[0]
            conf = mean_conf(words)
            if conf >= tau and words[-1]["t1"] < w0 + WIN_S - 1:
                hit = {"t0": round(max(o, rise(R, words[0])), 2), "t1": round(line_end(V, words), 2),
                       "conf": round(conf, 3), "src": "forced", "words": _words(words)}
                break
        placed.append(hit)
        if hit:
            cursor = max(cursor, hit["t1"] - 0.05)
    return placed, {"onsets": len(starts), "alignments": tried}


def _expected_span(texts, placed, i):
    per_char = [(p["t1"] - p["t0"]) / max(1, len(norm_text(texts[j]))) for j, p in enumerate(placed) if p]
    by_char = float(np.median(per_char)) * len(norm_text(texts[i])) if per_char else 0.0
    same = [p["t1"] - p["t0"] for j, p in enumerate(placed)
            if p and j != i and norm_text(texts[j]) == norm_text(texts[i])]
    return min(float(np.median(same)), by_char) if same else by_char


def repair(backend, audio, texts, placed, tau=FORCE_MIN):
    """Duration-outlier repair (module docstring). Mutates ``placed``; returns notes."""
    R = energy(audio)
    V = voiced(R)
    notes = []
    for i in range(1, len(texts)):
        cur, prev = placed[i], placed[i - 1]
        if not cur or not prev:
            continue
        expected = _expected_span(texts, placed, i)
        span = cur["t1"] - cur["t0"]
        if not expected or span <= OUTLIER * expected:
            continue
        w0 = max(0.0, cur["t0"] - LEAD_S)
        result = backend.force(_window(audio, w0), w0, [texts[i - 1], texts[i]])
        if result and mean_conf(result[0]) >= tau and mean_conf(result[1]) >= tau:
            note = (f"moved from {prev['t0']:.2f}-{prev['t1']:.2f} s: the next line spanned {span:.1f} s "
                    f"(> {OUTLIER} x {expected:.1f} s)")
            first = {"t0": round(max(cur["t0"], rise(R, result[0][0])), 2), "t1": round(line_end(V, result[0]), 2),
                     "conf": round(mean_conf(result[0]), 3), "src": "forced+repair", "note": note,
                     "words": _words(result[0])}
            second = {"t0": round(max(first["t1"], rise(R, result[1][0])), 2),
                      "t1": round(line_end(V, result[1]), 2), "conf": round(mean_conf(result[1]), 3),
                      "src": "forced+repair", "words": _words(result[1])}
            if first["t0"] < first["t1"] <= second["t0"] < second["t1"]:
                placed[i - 1], placed[i] = first, second
                notes.append({"line": i - 1, "note": note})
                continue
        cur["note"] = f"spans {span:.1f} s (> {OUTLIER} x {expected:.1f} s): may hold an earlier or unsheeted line"
        notes.append({"line": i, "note": cur["note"]})
    return notes


def interpolate(placed, duration_s):
    """Unplaced lines between placed neighbours spread evenly over the gap
    (claudia rule). Lines before the first or after the last placed line stay
    unplaced — never guessed."""
    good = [i for i, p in enumerate(placed) if p]
    for i, p in enumerate(placed):
        if p:
            continue
        prv = max((g for g in good if g < i), default=None)
        nxt = min((g for g in good if g > i), default=None)
        if prv is None or nxt is None:
            continue
        run = list(range(prv + 1, nxt))
        t0, t1 = placed[prv]["t1"], placed[nxt]["t0"]
        if t1 - t0 < LINE_MIN_S * len(run):
            continue
        span = (t1 - t0) / len(run)
        k = run.index(i)
        placed[i] = {"t0": round(t0 + span * k, 2), "t1": round(min(duration_s, t0 + span * (k + 1)), 2),
                     "conf": 0.0, "src": "interp"}
    return placed


def repeat_offsets(rows, placed):
    """Lines of a repeated section whose offset from the section start differs
    from the first occurrence's by more than REPEAT_TOL_S."""
    first_of = {r["id"]: i for i, r in enumerate(rows) if not r.get("repeat_marker_id")}
    groups = {}
    for i, row in enumerate(rows):
        if row.get("repeat_marker_id"):
            groups.setdefault(row["repeat_marker_id"], []).append(i)
    out = []
    for idx in groups.values():
        head = idx[0]
        origin = first_of.get(rows[head].get("original_source_row_id"))
        if origin is None or not placed[head] or not placed[origin]:
            continue
        for i in idx[1:]:
            j = first_of.get(rows[i].get("original_source_row_id"))
            if j is None or not placed[i] or not placed[j]:
                continue
            delta = (placed[i]["t0"] - placed[head]["t0"]) - (placed[j]["t0"] - placed[origin]["t0"])
            if abs(delta) > REPEAT_TOL_S:
                out.append({"line": i, "repeat_of": j, "delta_s": round(delta, 2)})
    return out


def propose(project, backend=None, model_size="small", compute_type="float32"):
    """Write ``lyrics/align_proposal.json`` + ``lyrics/align_report.json``.

    ``backend`` (``id`` + ``force(audio, t0, lines)``) is injectable so tests
    run without a model.
    """
    started = time.monotonic()
    p = Path(project)
    with project_mutex(p):
        require_legacy_profile(p)
        from .audio import ensure_vocals
        vocals = ensure_vocals(p)
        document, expansions, rows = read_rows(p)
        audio = load_audio(safe_path(p, vocals["path"]))
        if backend is None:
            backend = WhisperForcedAligner(model_size=model_size, compute_type=compute_type)
        texts = [r["text"].strip() for r in rows]
        placed, stats = place_lines(backend, audio, texts)
        repairs = repair(backend, audio, texts, placed)
        placed = interpolate(placed, len(audio) / SR)
        repeats = repeat_offsets(rows, placed)
        duration_ms = read(p / "analysis/audio.json", {}).get("duration_ms") or int(len(audio) * 1000 / SR)
        cues, detail, ear, prev_end = [], [], [], 0
        for row, hit in zip(rows, placed):
            entry = {"row_id": row["id"], "text": row["text"]}
            start = max(int(round(hit["t0"] * 1000)), prev_end) if hit else None
            end = min(max(int(round(hit["t1"] * 1000)), start + 120), duration_ms) if hit else None
            if not hit or end <= start:
                detail.append({**entry, "src": "unplaced"})
                ear.append({**entry, "reason": "unplaced",
                            "detail": "not found as the first sung line after the previous one — time it by ear"})
                continue
            prev_end = end
            cues.append({"id": row["id"].replace(":", "_"), "source_row_id": row["id"], "text": row["text"],
                         "char_start": 0, "char_end": len(row["text"]), "start_ms": start, "end_ms": end})
            detail.append({**entry, **hit, "start_ms": start, "end_ms": end})
            if hit["src"] == "interp":
                ear.append({**entry, "reason": "interpolated", "start_ms": start,
                            "detail": "placed evenly between its neighbours, not heard — ear-check"})
            elif hit["conf"] < EAR_CONF:
                ear.append({**entry, "reason": "low confidence", "start_ms": start,
                            "detail": f"forced-alignment confidence {hit['conf']:.2f} < {EAR_CONF}"})
            if hit.get("note"):
                ear.append({**entry, "reason": "repaired" if hit["src"] == "forced+repair" else "long span",
                            "start_ms": start, "detail": hit["note"]})
        for r in repeats:
            row = rows[r["line"]]
            ear.append({"row_id": row["id"], "text": row["text"], "reason": "repeat offset",
                        "start_ms": next((c["start_ms"] for c in cues if c["source_row_id"] == row["id"]), None),
                        "detail": f"{r['delta_s']:+.2f} s against the same line in the first occurrence"})
        if not cues:
            raise FilmError("한 줄도 맞추지 못했습니다: 가사와 음원이 같은 곡인지 확인하세요")
        gaps = [{"after_row": a["source_row_id"], "from_ms": a["end_ms"], "to_ms": b["start_ms"]}
                for a, b in zip(cues, cues[1:]) if b["start_ms"] - a["end_ms"] > 4000]
        counts = {k: sum(d["src"] == k for d in detail) for k in ("forced", "forced+repair", "interp", "unplaced")}
        proposal = {"schema_version": 1, "source_path": "input/lyrics.txt",
                    "source_sha256": document["source_sha256"], "repeat_expansions": expansions,
                    "cues": cues, "review": None}
        report = {"schema_version": SCHEMA_VERSION, "kind": "align_report", "generated_at": now(),
                  "aligner": {"id": backend.id, "force_min": FORCE_MIN, **stats},
                  "vocal": vocals, "lines": len(rows), "counts": counts,
                  "elapsed_s": round(time.monotonic() - started, 1),
                  "rows": detail, "ear_check": ear, "repeat_offsets": repeats, "repairs": repairs,
                  "long_gaps": gaps,
                  "note": "제안 타이밍입니다(검토 전). lyrics-import --file lyrics/align_proposal.json 으로 "
                          "가져오고, 귀로 확인한 뒤에만 --reviewer 를 붙이세요. 긴 공백(long_gaps)은 "
                          "가사지에 없는 노래 구간일 수 있습니다."}
        write(p / "lyrics/align_proposal.json", proposal)
        write(p / "lyrics/align_report.json", report)
    return {"proposal": "lyrics/align_proposal.json", "report": "lyrics/align_report.json",
            "aligner": backend.id, "lines": len(rows), "cues": len(cues), "counts": counts,
            "ear_check": len(ear), "elapsed_s": report["elapsed_s"]}
