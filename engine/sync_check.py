"""Lip-sync as an audio check: does a take still carry the vocal it was given?

Port of claudia-bot's ``slop/sync/avsync.py`` (MIT, Copyright (c) 2026
anabology — https://github.com/anabologyco-maker/claudia-bot), method and
constants unchanged: 16 kHz mono, 10 ms frames, 64 ms windows, 80 mels
50–7600 Hz, 70 dB floor, global lag search ±1.0 s, 100 ms wave NCC, mel
cosine of mean-removed log-mel frames with a null-calibrated threshold, and
the divergence rule (first 300 ms mostly-unmatched stretch that never
recovers with a 400 ms matched run).

Not ported: the Pillow diagnostic figure (presentation only — records carry
the numbers) and the hard-wired project/song loading (Film Unit reads its
own ``analysis/`` and ``qc/conti/`` files). ``review`` (the lag-corrected
overlay render) is ported because a person still needs to eyeball the
divergence point.

Honesty gate: this measures takes whose generator *copies* a supplied vocal
(Seedance-style reference mode). A Flow/Veo clip made from a silent motion
prompt has no audio stream — the measurement then reports ``unmeasurable``,
never a faked number.
"""
from __future__ import annotations

import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np

from .core import FilmError, ffmpeg, now, read, safe_path, write

SR, HOP, NFFT = 16000, 160, 1024          # 10 ms frames, 64 ms windows
FR = SR // HOP                             # frames per second
MAX_LAG_S = 1.0                            # global lag search window
REF_MARGIN_S = 1.5                         # vocal reference is cut with this much extra on each side


# ---- decoding ----------------------------------------------------------------------

def load_audio(path, sr=SR):
    """Any audio/video file -> mono float32 at ``sr`` through FFmpeg."""
    path = Path(path)
    if not path.is_file():
        raise FilmError(f"audio/video file not found: {path}")
    import shutil
    exe = shutil.which("ffmpeg")
    if not exe:
        raise FilmError("ffmpeg is not installed or on PATH")
    r = subprocess.run([exe, "-v", "error", "-i", str(path), "-map", "a:0", "-ac", "1",
                        "-ar", str(sr), "-f", "f32le", "-"], capture_output=True)
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace")[-300:]
        if "matches no streams" in err or "Stream map" in err:
            raise FilmError(f"{path.name} has no audio stream: nothing to measure — "
                            "motion takes made with no audio play at lag 0")
        raise FilmError(f"ffmpeg could not decode the audio of {path.name}: {err.strip()}")
    x = np.frombuffer(r.stdout, np.float32).copy()
    if not len(x):
        raise FilmError(f"{path.name} decoded to an empty audio track")
    return x


# ---- the measurement (ported; maths unchanged) --------------------------------------

def mel_fb(n_mels=80, fmin=50, fmax=7600):
    hz2m = lambda f: 2595 * np.log10(1 + f / 700)  # noqa: E731
    m2hz = lambda m: 700 * (10 ** (m / 2595) - 1)  # noqa: E731
    pts = m2hz(np.linspace(hz2m(fmin), hz2m(fmax), n_mels + 2))
    bins = np.fft.rfftfreq(NFFT, 1 / SR)
    fb = np.zeros((n_mels, len(bins)))
    for i in range(n_mels):
        l, c, r = pts[i:i + 3]
        fb[i] = np.clip(np.minimum((bins - l) / (c - l), (r - bins) / (r - c)), 0, None)
    return fb


def logmel(x, n):
    x = np.pad(x, (NFFT // 2, NFFT // 2 + n * HOP))
    idx = np.arange(NFFT)[None, :] + HOP * np.arange(n)[:, None]
    S = np.abs(np.fft.rfft(x[idx] * np.hanning(NFFT), axis=1)) ** 2
    return 10 * np.log10(S @ mel_fb().T + 1e-10)             # frames x mels, frame i centred at i*10 ms


def win_sum(x, w):
    """Centred moving sum, same length."""
    c = np.concatenate([[0], np.cumsum(x)]); h = w // 2; i = np.arange(len(x))
    return c[np.clip(i + h, 0, len(x))] - c[np.clip(i - h, 0, len(x))]


def global_lag(clip, ref, max_s=MAX_LAG_S):
    """Samples by which the clip is later than the ref (clip[t] ~ ref[t - lag]) and the peak NCC."""
    n = 1 << int(np.ceil(np.log2(len(ref) + len(clip))))
    xc = np.fft.irfft(np.fft.rfft(clip, n) * np.conj(np.fft.rfft(ref, n)), n)
    lags = np.r_[np.arange(0, n // 2), np.arange(-n // 2, 0)]
    xc = np.where(np.abs(lags) <= max_s * SR, xc, -np.inf); k = int(np.argmax(xc))
    return int(lags[k]), float(xc[k] / np.sqrt((ref ** 2).sum() * (clip ** 2).sum() + 1e-12))


def on_clip(ref, lag, n):
    """The ref placed on the clip's timeline."""
    r = np.zeros(n, np.float32)
    if lag >= 0:
        m = max(0, min(len(ref), n - lag)); r[lag:lag + m] = ref[:m]
    else:
        m = max(0, min(len(ref) + lag, n)); r[:m] = ref[-lag:-lag + m]
    return r


def unit_rows(M, floor_db=70):
    M = np.maximum(M, M.max() - floor_db); M = M - M.mean(1, keepdims=True)
    return M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)


def check(clip_p, ref_p, song_t0_ms=None, beats_ms=None):
    """The sync measurement. ``song_t0_ms`` is the song time of the clip's first
    frame; ``beats_ms`` are the song's measured beats (cut suggestions land on
    them and their midpoints)."""
    clip, ref = load_audio(clip_p), load_audio(ref_p)
    lag, gncc = global_lag(clip, ref)
    r = on_clip(ref, lag, len(clip)); n = len(clip) // HOP + 1
    t = np.arange(n) / FR
    w = SR // 10; pad = lambda x: np.pad(x, (0, n * HOP - len(x)))[:n * HOP]  # noqa: E731
    cc, rr, pp = pad(clip), pad(r), pad(clip * r)
    num, ec, er = win_sum(pp, w)[::HOP][:n], win_sum(cc * cc, w)[::HOP][:n], win_sum(rr * rr, w)[::HOP][:n]
    wave = num / np.sqrt(ec * er + 1e-12)
    rms_c, rms_r = np.sqrt(ec / w), np.sqrt(er / w)
    active = (rms_c > .1 * np.percentile(rms_c, 95)) | (rms_r > .1 * np.percentile(rms_r, 95))
    Mc, Mr = logmel(clip, n), logmel(r, n)
    A, B = unit_rows(Mc), unit_rows(Mr); C = A @ B.T                 # C[i, j] = sim(clip frame i, ref-on-clip frame j)
    sm = lambda x, k=11: np.convolve(np.nan_to_num(x), np.ones(k) / k, "same")  # noqa: E731
    mel = sm(np.diagonal(C).copy())
    null = []
    for k in (37, 61, 89, 131, -37, -61, -89, -131):              # offsets that avoid 8th-note multiples (~22.8 frames)
        d = np.full(n, np.nan); idx = np.arange(n); j = idx + k; ok = (j >= 0) & (j < n); d[ok] = C[idx[ok], j[ok]]
        null.append(sm(d)[active & ok])
    null = np.concatenate(null); null90 = float(np.percentile(null, 90)) if len(null) else .3
    sure = active & (wave > .6)
    if sure.sum() > 10:
        matched_med = float(np.median(mel[sure]))
    else:
        matched_med = float(np.percentile(mel[active], 75)) if active.any() else null90
    thr = null90 + .4 * (matched_med - null90)
    match = active & ((wave >= .4) | (mel >= thr))
    # divergence: first 300 ms stretch of mostly-unmatched voice that never recovers (a 400 ms matched run)
    L, R = int(.3 * FR), int(.4 * FR); dips = []; div = None; i = 0
    first = int(np.argmax(match)) if match.any() else 0
    while i < n - L:
        seg = active[i:i + L]
        if seg.sum() >= .6 * L and match[i:i + L][seg].mean() < .3 and match[i]:
            i += 1; continue
        if seg.sum() >= .6 * L and match[i:i + L][seg].mean() < .3:
            run, rec = 0, None
            for j in range(i + L, n):
                run = run + 1 if match[j] else (run if not active[j] else 0)
                if run >= R:
                    rec = j - R + 1; break
            if rec is None:
                div = i; break
            dips.append((round(i / FR, 2), round(rec / FR, 2))); i = rec; continue
        i += 1
    if div is None:                                               # a tail too short for the window: voice after the last match
        good = np.where(match)[0]
        if len(good) and active[good[-1] + 1:].sum() >= int(.08 * FR):
            div = good[-1] + 1
    if div is not None:
        good = np.where(match[:div])[0]; until = (good[-1] + 1) / FR if len(good) else 0.0
    else:
        until = n / FR
    until_safe = max(0.0, until - .042) if div is not None else len(clip) / SR
    # in-sync spans: matched voice, bridged across silence and gaps <= 0.25 s; spans < 0.5 s are noise
    spans, i0, gap, last = [], None, 0, 0
    for i in range(n):
        if match[i]:
            i0 = i if i0 is None else i0; gap = 0; last = i
        elif active[i] and i0 is not None:
            gap += 1
            if gap > int(.25 * FR):
                spans.append((i0, last + 1)); i0, gap = None, 0
    if i0 is not None:
        spans.append((i0, last + 1))
    spans = [(a / FR, b / FR) for a, b in spans if (b - a) / FR >= .5]
    song_t0 = (song_t0_ms / 1000.0) if song_t0_ms is not None else None
    to_song = (lambda x: song_t0 + x - lag / SR) if song_t0 is not None else (lambda x: None)  # noqa: E731
    out = {"clip": str(clip_p), "ref": str(ref_p), "song_t0_ms": song_t0_ms, "lag_s": round(lag / SR, 4),
           "lag_ncc": round(gncc, 3), "mode": "copy" if gncc > .2 else "resynth",
           "thr_mel": round(thr, 3), "null90": round(null90, 3), "matched_med": round(matched_med, 3),
           "synced_from": round(first / FR, 2), "diverges": div is not None,
           "diverge_at": round(div / FR, 2) if div is not None else None,
           "synced_until": round(until_safe, 3), "dips": dips, "duration": round(len(clip) / SR, 3),
           "spans": [[round(a, 3), round(b, 3)] for a, b in spans]}
    if song_t0 is not None:
        out["spans_song"] = [[round(to_song(a), 3), round(to_song(b) - .042, 3)] for a, b in spans]
        bt = np.asarray([b / 1000.0 for b in (beats_ms or [])], dtype=float)
        e8 = np.sort(np.r_[bt, (bt[:-1] + bt[1:]) / 2]) if len(bt) else bt
        us = to_song(until_safe)
        out.update({"synced_until_song": round(us, 3),
                    "diverge_at_song": round(to_song(div / FR), 3) if div is not None else None,
                    "cut_beat_song": float(bt[bt <= us].max()) if (bt <= us).any() else None,
                    "cut_eighth_song": float(e8[e8 <= us].max()) if (e8 <= us).any() else None})
        out["cut_eighth_clip"] = round(out["cut_eighth_song"] - song_t0 + lag / SR, 3) if out.get("cut_eighth_song") else None
    return _plain(out)


def _plain(o):
    """numpy scalars -> plain Python numbers so the result is JSON-clean."""
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    if isinstance(o, np.generic):
        return o.item()
    return o


# ---- the review render (ported; marker from the divergence on) -----------------------

def review(clip_p, ref_p, out_p, song_t0_ms=None, beats_ms=None):
    """The clip with the reference audio laid on (lag-corrected) and a burned-in
    marker from the divergence on — so a person can eyeball the cut point."""
    o = check(clip_p, ref_p, song_t0_ms=song_t0_ms, beats_ms=beats_ms)
    lag, us = o["lag_s"], o["synced_until"]
    src = Path(ref_p)
    if lag > 0:
        af = f"[1:a]adelay={int(lag * 1000)}:all=1,aresample=48000,apad[a]"
    else:
        af = f"[1:a]atrim=start={-lag:.4f},asetpts=PTS-STARTPTS,aresample=48000,apad[a]"
    fs = 34
    vf = (f"drawtext=text='%{{pts\\:hms}}':fontsize={fs}:fontcolor=white:box=1:boxcolor=black@0.6:"
          f"boxborderw=10:x=w-tw-40:y=40,"
          f"drawtext=text='OUR AUDIO · IN SYNC':fontsize={fs}:fontcolor=black:box=1:boxcolor=white@0.9:"
          f"boxborderw=12:x=40:y=40:enable='lt(t,{us:.3f})'"
          + (f",drawtext=text='DIVERGED at {us:.2f}s · CUT HERE':fontsize={fs}:fontcolor=white:box=1:"
             f"boxcolor=0xD97757@0.95:boxborderw=12:x=40:y=40:enable='gte(t,{us:.3f})'" if o["diverges"] else ""))
    out = Path(out_p)
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg(["-i", str(clip_p), "-i", str(src), "-filter_complex", f"[0:v]{vf}[v];{af}",
            "-map", "[v]", "-map", "[a]", "-t", f"{o['duration']:.3f}", "-c:v", "libx264", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", out])
    o["review"] = str(out)
    return o


# ---- per-shot measurement against the shot's vocal reference -------------------------

def _shot(p, shot_id):
    shots = read(p / "manifest/shots.json", [])
    shot = next((s for s in shots if s["id"] == shot_id), None)
    if shot is None:
        raise FilmError(f"Unknown shot ID: {shot_id}")
    return shot


def vocal_reference(project, shot_id, force=False):
    """Cut the shot's vocal reference (with margin) from the analysis vocal
    track. This is the audio a person attaches to a voice-conditioned clip, and
    what a copied-vocal take is measured against."""
    from .audio import ensure_vocals
    p = Path(project)
    out = p / "render/vocal_ref" / f"{shot_id}.wav"
    if out.is_file() and not force:
        return {"path": str(out.relative_to(p)), "source": "cached"}
    shot = _shot(p, shot_id)
    vocals = ensure_vocals(p)
    src = safe_path(p, vocals["path"])
    start = max(0.0, shot["in_ms"] / 1000 - REF_MARGIN_S)
    end = shot["out_ms"] / 1000 + REF_MARGIN_S
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg(["-i", str(src), "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
            "-ac", "1", "-c:a", "pcm_f32le", out])
    return {"path": str(out.relative_to(p)), "source": vocals["source"], "cut_from": vocals["path"],
            "song_t0_ms": shot["in_ms"], "note": "reference starts "
            f"{shot['in_ms'] / 1000 - start:.2f} s before the shot; the ±{MAX_LAG_S} s lag window covers it"}


def measure_take(project, shot_id, sha256=None, render_review=False):
    """Measure a checked take's own soundtrack against the shot's vocal reference
    and store the result in its ``qc/conti`` check record.

    Capability-gated and honest: a take without an audio stream (the normal
    Flow/Veo motion clip) is recorded ``unmeasurable``, not as lag 0.
    """
    from .conti_brief import _record_path, load_check
    from .core import probe
    p = Path(project)
    record = load_check(p, shot_id, sha256)
    take = safe_path(p, record["take_path"])
    shot = _shot(p, shot_id)
    beats = read(p / "analysis/audio.json", {}).get("beat_times_ms", [])
    try:
        info = probe(take)
    except FilmError:
        info = {}
    if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
        record["sync"] = {"status": "unmeasurable",
                          "reason": "the take has no audio stream (motion clips made with no audio play at lag 0)",
                          "measured_at": now()}
        write(_record_path(p, shot_id, record["take_sha256"]), record)
        return record["sync"]
    ref = vocal_reference(p, shot_id)
    result = check(take, safe_path(p, ref["path"]), song_t0_ms=shot["in_ms"], beats_ms=beats)
    if render_review:
        review_path = p / "qc/conti" / shot_id / f"{record['take_sha256'][:16]}" / "sync_review.mp4"
        result = review(take, safe_path(p, ref["path"]), review_path, song_t0_ms=shot["in_ms"], beats_ms=beats)
    record["sync"] = {"status": "measured", "measured_at": now(),
                      "reference": ref["path"], "reference_source": ref["source"],
                      "honesty_note": "meaningful only for generators that copy the supplied vocal "
                                      "(reference/voice-conditioned clips); a resynthesized soundtrack "
                                      "reports mode=resynth",
                      **result}
    write(_record_path(p, shot_id, record["take_sha256"]), record)
    return record["sync"]
