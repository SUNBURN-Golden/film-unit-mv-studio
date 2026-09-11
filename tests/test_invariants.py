import copy
import json
from pathlib import Path
import numpy as np
import pytest
import soundfile as sf
from engine.core import FilmError, frame_at, init_project, lock_production, object_hash, read, require_lock, validate_manifest, write
from engine.audio import analyze, synth_test_audio
from engine.production import make_package
from engine.budget import approve, make_estimate, require_approval, reserve


@pytest.fixture
def project(tmp_path):
    audio = synth_test_audio(tmp_path / "test.wav", seconds=2)
    p = init_project(tmp_path, "test_project", audio, "Test fixture", synthetic=True)
    analyze(p)
    make_package(p)
    lock_production(p, "test", mock_only=True)
    return p


def test_global_rounding_has_no_accumulated_drift():
    boundaries = list(range(0, 600000, 817)) + [600000]
    frames = [frame_at(b) - frame_at(a) for a, b in zip(boundaries, boundaries[1:])]
    assert sum(frames) == 600 * 24
    assert all(abs(frame_at(ms)*1000/24-ms) <= 1000/48 for ms in boundaries)


def test_lock_invalidated_by_actual_reference_edit(project):
    require_lock(project, "mock")
    with pytest.raises(FilmError, match="mock animatic"):
        require_lock(project, "openart")
    with (project / "storyboard/S001.png").open("ab") as f:
        f.write(b"changed")
    with pytest.raises(FilmError, match="changed after LOCK"):
        require_lock(project, "mock")


def test_manifest_rejects_gaps_overlaps_and_short_shots(project):
    shots = read(project / "manifest/shots.json")
    shots[0]["in_ms"] = 1
    with pytest.raises(FilmError, match="Gap"):
        validate_manifest(shots, 2000)
    shots[0]["in_ms"] = 0
    shots[0]["duration_ms"] -= 1
    with pytest.raises(FilmError, match="Duration mismatch"):
        validate_manifest(shots, 2000)


def test_entire_silent_audio_is_explicit(tmp_path):
    audio = tmp_path / "silence.wav"
    sf.write(audio, np.zeros(44100), 44100)
    p = init_project(tmp_path, "silence", audio, "silence")
    result = analyze(p)
    assert result["all_silent"]
    assert result["tempo_bpm"] is None
    assert result["beat_times_ms"] == []
    assert result["silence_regions"] == [{"in_ms": 0, "out_ms": 1000}]


def test_budget_worst_case_approval_and_idempotent_reservations(project):
    class QuotedRenderer:
        name = "mock"
        def quote(self, shot, quality): return 7
        def config_hash(self): return "exact-config"
    shots = read(project / "manifest/shots.json")
    shots[0]["render_mode"] = "FULL_GENERATIVE"
    estimate = make_estimate(project, shots, QuotedRenderer(), "final", "production")
    assert estimate["worst_case_credits"] == 21
    with pytest.raises(FilmError, match="approve"):
        require_approval(project, estimate)
    approve(project, estimate)
    require_approval(project, estimate)
    changed = dict(estimate, estimate_id="changed")
    with pytest.raises(FilmError, match="approve"):
        require_approval(project, changed)
    for _ in range(3):
        reserve(project, "same-job", 7, estimate)
    reserve(project, "retry-1", 7, estimate)
    reserve(project, "retry-2", 7, estimate)
    assert len(read(project / "render/ledger.json")["jobs"]) == 3
    with pytest.raises(FilmError, match="cap reached"):
        reserve(project, "extra-job", 7, estimate)


def test_project_cannot_escape_root(tmp_path):
    with pytest.raises(FilmError):
        init_project(tmp_path, "../escape", "irrelevant.wav", "test")


def test_nonfinite_provider_quote_cannot_bypass_budget(project):
    class BadQuote:
        name = "mock"
        def quote(self, shot, quality): return float("nan")
        def config_hash(self): return "invalid"
    shots = read(project / "manifest/shots.json")
    shots[0]["render_mode"] = "FULL_GENERATIVE"
    with pytest.raises(FilmError, match="unknown quote"):
        make_estimate(project, shots, BadQuote(), "draft", "test")
