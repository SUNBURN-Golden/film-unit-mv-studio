from pathlib import Path
import shutil
import pytest
from engine.core import FilmError, digest, ffmpeg, init_project, lock_production, production_fingerprint, read, write
from engine.audio import analyze, synth_test_audio
from engine.production import import_frame, make_package
from engine.pipeline import compile_project
from engine.qc import SEMANTIC_ITEMS, save_review


@pytest.fixture
def small_project(tmp_path):
    master = synth_test_audio(tmp_path / "source.wav", 2)
    p = init_project(tmp_path, "media_fixture", master, "TEST ONLY: imports below are local fixture videos, not AI generations", synthetic=True)
    analyze(p)
    make_package(p)
    config = read(p / "project.yaml")
    config["format"].update(width=320, height=240)
    write(p / "project.yaml", config)
    shots = read(p / "manifest/shots.json")
    shots[0]["render_mode"] = "FULL_GENERATIVE"
    write(p / "manifest/shots.json", shots)
    # Explicit test fixture keyframe, used solely to exercise imported-media review.
    import_frame(p, "S001", p / "storyboard/S001.png")
    lock_production(p, "test fixture", mock_only=False)
    return p


def clip(path, color, seconds=2):
    ffmpeg(["-f", "lavfi", "-i", f"color=c={color}:s=320x240:r=24:d={seconds}", "-f", "lavfi", "-i", "sine=frequency=1500:duration=2", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-threads", "1", path])


def test_unknown_semantic_scores_block_then_retry_uses_evidence(small_project):
    p = small_project
    clip(p / "render/manual/S001_a0.mp4", "gray")
    first = compile_project(p, mode="manual")
    assert first["status"] == "AWAITING_REVIEW_OR_RENDER"
    record = read(p / "qc/report.json")["shots"][0]
    assert record["status"] == "NEEDS_REVIEW"
    assert record["technical"]["no_audio"]
    assert all(v is None for v in record["semantic"].values())
    scores = {k: 100 for k in SEMANTIC_ITEMS}
    scores["character_identity"] = 0  # failure cannot be hidden by the average
    save_review(p, record, scores, "automated fixture reviewer", "TEST: triangle tie missing")
    second = compile_project(p, mode="manual")
    assert second["status"] == "AWAITING_REVIEW_OR_RENDER"
    waiting = read(p / "qc/report.json")["shots"][0]
    assert waiting["attempt"] == 1
    assert "triangle tie missing" in waiting["message"]
    clip(p / "render/manual/S001_a1.mp4", "teal")
    compile_project(p, mode="manual")
    record2 = read(p / "qc/report.json")["shots"][0]
    assert record2["status"] == "NEEDS_REVIEW"
    assert record2["clip_sha256"] != record["clip_sha256"]
    save_review(p, record2, {k: 100 for k in SEMANTIC_ITEMS}, "automated fixture reviewer", "TEST ONLY: all fixture expectations satisfied")
    final = compile_project(p, mode="manual")
    assert final["status"] == "COMPLETE"
    assert final["retries"] == 1
    assert read(p / "project.yaml")["audio"]["sha256"] == digest(p / "input/master.wav")


def test_corrupt_clips_stop_at_retry_cap(small_project):
    p = small_project
    for attempt in range(3):
        (p / f"render/manual/S001_a{attempt}.mp4").write_bytes(b"not a video")
    result = compile_project(p, mode="manual")
    assert result["status"] == "AWAITING_REVIEW_OR_RENDER"
    report = read(p / "qc/report.json")
    assert report["shots"][0]["status"] == "FAIL"
    assert report["retries"] == 2
    assert not (p / "output/MASTER.mp4").exists()


def test_master_not_modified_by_mock_render(small_project):
    p = small_project
    before = digest(p / "input/master.wav")
    result = compile_project(p, mode="mock")
    assert result["status"] == "COMPLETE"
    assert result["generated_shots"] == 0
    assert digest(p / "input/master.wav") == before


def test_replacing_imported_clip_invalidates_cache(small_project):
    p = small_project
    source = p / "render/manual/S001_a0.mp4"
    clip(source, "gray")
    compile_project(p, mode="manual")
    old_hash = read(p / "qc/report.json")["shots"][0]["clip_sha256"]
    clip(source, "red")
    compile_project(p, mode="manual")
    new = read(p / "qc/report.json")["shots"][0]
    assert old_hash != new["clip_sha256"]
    assert new["status"] == "NEEDS_REVIEW"
