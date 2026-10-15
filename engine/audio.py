"""Measured audio timing. Structural boundaries are candidates, never verse labels."""
from pathlib import Path
import importlib.util
import subprocess
import sys
import tempfile

import numpy as np
from scipy.signal import butter, filtfilt, find_peaks
import librosa
import soundfile as sf
from .core import FilmError, digest, ffmpeg, now, read, require_legacy_profile, safe_path, write


def analyze(project):
    p = Path(project)
    require_legacy_profile(p)
    config = read(p / "project.yaml")
    master = p / config["audio"]["path"]
    if digest(master) != config["audio"]["sha256"]:
        raise FilmError("Master changed: import as a new project")
    # Decode a separate analysis derivative; master bytes are never overwritten.
    wav = p / "analysis/mono_22050.wav"
    ffmpeg(["-i", master, "-vn", "-ac", "1", "-ar", "22050", "-c:a", "pcm_f32le", wav])
    y, sr = sf.read(wav, dtype="float32")
    duration = round(len(y) * 1000 / sr)
    if not 1000 <= duration <= 600_100:
        raise FilmError("Decoded audio must be between 1 second and 10 minutes")
    hop = 512
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
    times = np.minimum(duration, np.rint(librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop) * 1000).astype(int))
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    silent = float(np.max(np.abs(y))) < 1e-6
    if silent:
        tempo, beats, onsets = None, [], []
    else:
        tempo_raw, beat_frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, hop_length=hop)
        tempo = float(np.asarray(tempo_raw).reshape(-1)[0])
        if not np.isfinite(tempo) or tempo <= 0:
            tempo = None
        beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop).tolist()
        onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, hop_length=hop, units="time").tolist()
    # Feature-change heuristic: spectral centroid + energy, smoothed over ~1 s.
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop)[0]
    kernel = np.ones(43) / 43
    features = np.stack([np.log1p(rms * 100), np.log1p(centroid)])
    features = (features - features.mean(axis=1, keepdims=True)) / (features.std(axis=1, keepdims=True) + 1e-6)
    smooth = np.array([np.convolve(f, kernel, mode="same") for f in features])
    novelty = np.linalg.norm(smooth[:, 43:] - smooth[:, :-43], axis=0)
    peaks, _ = find_peaks(novelty, distance=round(10 * sr / hop), prominence=max(0.2, float(novelty.std())))
    candidates = sorted(set(int((i + 21) * hop * 1000 / sr) for i in peaks if 6000 < (i + 21) * hop * 1000 / sr < duration - 6000))
    energy_peaks, _ = find_peaks(rms, distance=round(sr / hop), prominence=max(float(rms.max()) * 0.12, 1e-6))
    quiet = rms < 10 ** (-45 / 20)
    transitions = np.diff(np.r_[False, quiet, False].astype(int))
    regions = []
    for start, end in zip(np.flatnonzero(transitions == 1), np.flatnonzero(transitions == -1)):
        a, b = int(times[start]), min(duration, round(end * hop * 1000 / sr))
        if b - a >= 250:
            regions.append({"in_ms": a, "out_ms": b})
    ms = lambda values: sorted(set(round(v * 1000) for v in values if 0 <= v * 1000 < duration))
    data = {
        "schema_version": "0.1", "master_sha256": digest(master), "duration_ms": duration,
        "analysis_sample_rate": sr, "hop_length": hop, "timing_resolution_ms": round(hop * 1000 / sr, 3),
        "tempo_bpm": tempo, "tempo_is_estimate": True, "beat_times_ms": ms(beats), "onsets_ms": ms(onsets),
        "energy_curve": [{"time_ms": int(t), "rms": round(float(v), 7)} for t, v in zip(times[::4], rms[::4])],
        "section_boundaries_ms": [0, *candidates, duration], "section_method": "spectral-energy change candidates; human musical labels required",
        "silence_regions": regions, "major_peaks_ms": [int(times[i]) for i in energy_peaks],
        "all_silent": silent, "librosa_version": librosa.__version__,
    }
    write(p / "analysis/audio.json", data)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(14, 3), facecolor="#ECEAE4")
    ax.set_facecolor("#ECEAE4")
    stride = max(1, len(y) // 18000)
    ax.plot(np.arange(0, len(y), stride) / sr, y[::stride], color="#48A6A0", linewidth=0.6)
    for t in candidates:
        ax.axvline(t / 1000, color="#181818", alpha=0.35, linewidth=0.8)
    ax.set(xlabel="Time (seconds)", ylabel="Amplitude", title="Measured master audio / candidate section boundaries")
    fig.tight_layout()
    fig.savefig(p / "analysis/waveform.png", dpi=130)
    plt.close(fig)
    return data


# ---------------------------------------------------------------------------
# vocal stem (Claudia port: lyric alignment and take sync checks need a
# vocal-only reference; Film Unit never ships a paid separator)


VOCALS_JSON = "analysis/vocals.json"
VOCALS_PATH = "analysis/vocals.wav"


def _supplied_vocals(p):
    """A vocal stem the user dropped in input/ (vocals.<ext>), or None."""
    for ext in (".wav", ".mp3"):
        candidate = p / "input" / f"vocals{ext}"
        if candidate.is_file():
            return candidate
    return None


def _demucs_available():
    return importlib.util.find_spec("demucs") is not None


def _demucs_separate(master, target):
    """Run the installed demucs CLI once; returns the separated vocals.wav."""
    with tempfile.TemporaryDirectory(prefix="filmunit-demucs-") as tmp:
        cmd = [sys.executable, "-m", "demucs", "--two-stems", "vocals",
               "-o", tmp, str(master)]
        run = subprocess.run(cmd, capture_output=True, text=True)
        if run.returncode != 0:
            raise FilmError("demucs separation failed: "
                            + (run.stderr or run.stdout)[-300:])
        found = sorted(Path(tmp).rglob("vocals.wav"))
        if not found:
            raise FilmError("demucs finished but produced no vocals.wav")
        return found[0].read_bytes()


def _proxy_vocals(p, target):
    """Honest fallback stem: harmonic component band-limited to the voice
    range. Labelled 'proxy' everywhere — it is not a demucs separation."""
    wav = p / "analysis/mono_22050.wav"
    if not wav.is_file():
        raise FilmError("run analyze first — analysis/mono_22050.wav is missing")
    y, sr = sf.read(wav, dtype="float32")
    harmonic, _ = librosa.effects.hpss(y)
    b, a = butter(4, [200 / (sr / 2), 8000 / (sr / 2)], btype="band")
    vocal = filtfilt(b, a, harmonic).astype("float32")
    peak = float(np.max(np.abs(vocal))) if len(vocal) else 0.0
    if peak > 1e-6:
        vocal *= min(0.89, 0.89 / peak)
    sf.write(target, vocal, sr, subtype="PCM_16")


def ensure_vocals(project, shot=None):
    """A vocal-only reference WAV for lyric alignment and take sync checks.

    Sources, in order: `input/vocals.<wav|mp3>` supplied by the user
    (labelled 'supplied'), a local demucs separation when demucs is
    installed ('demucs'), else a harmonic+vocal-band proxy from the
    analysis WAV ('proxy' — honest, never presented as a separation).
    Cached under analysis/; rebuilt when the master sha changes.
    `shot` is accepted for callers that intend to window the stem
    (see sync_check.vocal_reference); the stem itself is always full-song.
    """
    p = Path(project)
    require_legacy_profile(p)
    config = read(p / "project.yaml")
    master = safe_path(p, config["audio"]["path"])
    master_sha = digest(master)
    supplied = _supplied_vocals(p)
    supplied_sha = digest(supplied) if supplied is not None else None
    cached = read(p / VOCALS_JSON, {})
    if cached.get("master_sha256") == master_sha \
            and cached.get("supplied_sha256") == supplied_sha \
            and safe_path(p, cached.get("path", "")).is_file():
        return {"path": cached["path"], "source": cached["source"],
                "sha256": digest(safe_path(p, cached["path"]))}
    target = p / VOCALS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    if supplied is not None:
        ffmpeg(["-i", supplied, "-vn", "-ac", "1", "-ar", "22050",
                "-c:a", "pcm_s16le", target])
        source = "supplied"
    elif _demucs_available():
        target.write_bytes(_demucs_separate(master, target))
        source = "demucs"
    else:
        _proxy_vocals(p, target)
        source = "proxy"
    write(p / VOCALS_JSON, {"master_sha256": master_sha, "path": VOCALS_PATH,
                            "source": source, "supplied_sha256": supplied_sha,
                            "created_at": now()})
    return {"path": VOCALS_PATH, "source": source, "sha256": digest(target)}


def synth_test_audio(path, seconds=65, sr=44100):
    """Original deterministic test signal, not a Suno song or supplied music."""
    t = np.arange(round(seconds * sr)) / sr
    y = np.zeros_like(t)
    for start in np.arange(0, seconds, 0.5):
        u = t - start
        mask = (u >= 0) & (u < 0.15)
        y[mask] += 0.2 * np.sin(2 * np.pi * (90 * u[mask] - 150 * u[mask] ** 2)) * np.exp(-35 * u[mask])
    notes = [220, 261.6256, 329.6276, 293.6648, 196, 246.9417, 293.6648, 261.6256]
    for i, start in enumerate(np.arange(0, seconds, 2)):
        u = t - start
        mask = (u >= 0) & (u < 2)
        freq = notes[i % len(notes)]
        envelope = (1 - np.exp(-u[mask] * 10)) * np.exp(-u[mask] * 1.8)
        y[mask] += 0.10 * (np.sin(2 * np.pi * freq * u[mask]) + 0.35 * np.sin(4 * np.pi * freq * u[mask])) * envelope
    y *= np.minimum(t / 0.1, 1) * np.minimum((seconds - t) / 0.15, 1)
    stereo = np.column_stack([y, y * 0.96])
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, stereo, sr, subtype="PCM_16")
    return Path(path)
