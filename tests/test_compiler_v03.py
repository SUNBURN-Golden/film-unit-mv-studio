"""Whole-song compiler regression using synthetic audio, pictures and local video.

These fixtures make no claim of AI generation, lyric alignment or artistic QC.
Their purpose is to catch truncated builds, mutable history and subtitle drift.
"""
from collections import Counter
from pathlib import Path
import shutil

from PIL import Image
import pytest

from engine.builds import list_builds, replay_build, verify_build
from engine.compiler import compile_final, compile_preview, import_asset
from engine.core import (
    FilmError, atomic_text, digest, ffmpeg, init_project, lock_production,
    probe, read, write,
)
from engine.lyrics import prepare_lyrics, save_timing


def fixture_project(tmp_path, *, seconds=2, shot_count=1, lyric_count=2):
    """An explicit PCM duration fixture; musical feature analysis is tested elsewhere."""
    source = tmp_path / "synthetic_master.wav"
    ffmpeg(["-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=16000:duration={seconds}",
            "-c:a", "pcm_s16le", source])
    lyrics = "\n".join(f"Synthetic fixture line {i + 1:02d}" for i in range(lyric_count)) + "\n"
    p = init_project(tmp_path, "compiler_fixture", source,
                     "Synthetic regression only; no real AI generation or alignment.",
                     lyrics=lyrics, synthetic=True)
    config = read(p / "project.yaml")
    config["format"].update(width=320, height=240, fps=24)
    write(p / "project.yaml", config)
    measured_ms = round(float(probe(source)["format"]["duration"]) * 1000)
    assert measured_ms == seconds * 1000
    write(p / "analysis/audio.json", {
        "schema_version": "0.1", "master_sha256": digest(source),
        "duration_ms": measured_ms, "beat_times_ms": [], "onsets_ms": [],
        "section_boundaries_ms": [0, measured_ms],
        "fixture": "Synthetic PCM; duration measured by ffprobe; no feature inference",
    })
    write(p / "manifest/sequence.json", [{"id": "SEQ01", "in_ms": 0,
                                          "out_ms": measured_ms, "title": "Synthetic fixture"}])
    for name in ("style_bible", "characters", "locations"):
        write(p / f"bible/{name}.yaml", {"fixture": "Synthetic test data", "draft": False})
    atomic_text(p / "bible/story.md", "Synthetic compiler regression.\n")
    shots = []
    for i in range(shot_count):
        start, end = i * measured_ms // shot_count, (i + 1) * measured_ms // shot_count
        shot_id = f"S{i + 1:03d}"
        shots.append({
            "id": shot_id, "sequence": "SEQ01", "in_ms": start, "out_ms": end,
            "duration_ms": end - start, "description": "Synthetic local fixture",
            "characters": [], "locations": [], "composition": "centered",
            "camera": {"type": "locked", "movement": "none"},
            "motion": {"complexity": "low", "instruction": "Synthetic fixture hold",
                       "local_effect": "hold"},
            "references": [f"storyboard/{shot_id}.png"],
            "render_mode": "FULL_GENERATIVE", "renderer": "manual", "status": "storyboard",
            "storyboard_kind": "imported",
        })
        Image.new("RGB", (320, 240), (30 + i * 3 % 200, 70, 110)).save(p / f"storyboard/{shot_id}.png")
    write(p / "manifest/shots.json", shots)
    document = prepare_lyrics(p)
    rows = [row for row in document["rows"] if row["kind"] == "lyric"]
    document["cues"] = [
        {"id": f"C{i + 1:03d}", "source_row_id": row["id"], "text": row["text"],
         "start_ms": i * measured_ms // lyric_count,
         "end_ms": (i + 1) * measured_ms // lyric_count - 80}
        for i, row in enumerate(rows)
    ]
    save_timing(p, document, reviewer="Synthetic fixture timing; not inferred from vocals")
    return p


def source_clip(path, color="blue", seconds=5):
    # Deliberately include generated audio: compiler must discard it at normalization.
    ffmpeg(["-f", "lavfi", "-i", f"color=c={color}:s=320x240:r=24:d={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=1500:sample_rate=16000:duration={seconds}",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-threads", "1", "-shortest", path])
    return path


def newest_build(project):
    record = list_builds(project)[0]
    assert record["status"] == "COMPLETE"
    return Path(record["build_dir"]), record


def assert_master(path, seconds):
    metadata = probe(path)
    video = [s for s in metadata["streams"] if s["codec_type"] == "video"]
    audio = [s for s in metadata["streams"] if s["codec_type"] == "audio"]
    assert len(video) == len(audio) == 1
    assert (video[0]["width"], video[0]["height"]) == (320, 240)
    assert video[0]["avg_frame_rate"] == "24/1"
    assert int(video[0]["nb_frames"]) == seconds * 24
    assert abs(float(video[0]["duration"]) - seconds) < 0.001
    assert abs(float(audio[0]["duration"]) - seconds) < 0.05
    assert abs(float(metadata["format"]["duration"]) - seconds) < 0.05


def test_four_minute_mixed_preview_is_immutable_and_reuses_unchanged_shots(tmp_path):
    p = fixture_project(tmp_path, seconds=240, shot_count=48, lyric_count=82)
    source = source_clip(tmp_path / "fixture_take.mp4")
    for i in range(20):
        import_asset(p, f"S{i + 1:03d}", source, kind="final" if i < 7 else "draft",
                     reviewer="Synthetic fixture reviewer", evidence="Local color video only")
    for number in range(46, 49):
        (p / f"storyboard/S{number:03d}.png").unlink()
    master_hash = digest(p / "input/master.wav")
    timing_hash = digest(p / "lyrics/lyrics_timed.json")

    compile_preview(p)
    first_path, first = newest_build(p)
    assert first["build_id"] == "B0001"
    assert first["duration_ms"] == 240000
    assert Counter(s["kind"] for s in first["shots"]) == {
        "final": 7, "draft": 13, "storyboard": 25, "placeholder": 3,
    }
    assert len(first["shots"]) == 48
    for name in ("MASTER_CLEAN.mp4", "MASTER_SUBBED.mp4"):
        assert_master(first_path / name, 240)
    assert len((first_path / "lyrics.srt").read_text().strip().split("\n\n")) == 82
    assert (first_path / "lyrics.ass").is_file()
    assert digest(first_path / "MASTER_CLEAN.mp4") != digest(first_path / "MASTER_SUBBED.mp4")
    # Every captured visual clip is silent; only original master is muxed back in.
    for shot in first["shots"]:
        assert not any(s["codec_type"] == "audio" for s in probe(first_path / shot["clip_path"])["streams"])
    assert verify_build(first_path)["valid"]
    original_build_json = digest(first_path / "build.json")

    Image.new("RGB", (320, 240), "orange").save(p / "storyboard/S024.png")
    compile_preview(p)
    second_path, second = newest_build(p)
    assert second["build_id"] == "B0002"
    old = {s["id"]: s for s in first["shots"]}
    new = {s["id"]: s for s in second["shots"]}
    assert [shot_id for shot_id in old if old[shot_id]["source_sha256"] != new[shot_id]["source_sha256"]] == ["S024"]
    assert [shot_id for shot_id in old if old[shot_id]["clip_sha256"] != new[shot_id]["clip_sha256"]] == ["S024"]
    assert first["audio"]["sha256"] == second["audio"]["sha256"] == master_hash
    assert first["lyrics"]["timing_sha256"] == second["lyrics"]["timing_sha256"]
    assert digest(p / "input/master.wav") == master_hash
    assert digest(p / "lyrics/lyrics_timed.json") == timing_hash
    assert digest(first_path / "build.json") == original_build_json
    assert verify_build(first_path)["valid"]
    assert verify_build(second_path)["valid"]
    assert_master(second_path / "MASTER_SUBBED.mp4", 240)


def test_preview_falls_back_after_corrupt_and_missing_assets(tmp_path):
    p = fixture_project(tmp_path)
    draft = import_asset(p, "S001", source_clip(tmp_path / "draft.mp4", "blue", 2), kind="draft")
    final = import_asset(p, "S001", source_clip(tmp_path / "final.mp4", "red", 2), kind="final")
    (p / final["path"]).write_bytes(b"corrupted after import")
    compile_preview(p)
    assert newest_build(p)[1]["shots"][0]["kind"] == "draft"
    (p / draft["path"]).unlink()
    compile_preview(p)
    assert newest_build(p)[1]["shots"][0]["kind"] == "storyboard"
    (p / "storyboard/S001.png").unlink()
    compile_preview(p)
    folder, record = newest_build(p)
    assert record["shots"][0]["kind"] == "placeholder"
    assert_master(folder / "MASTER_SUBBED.mp4", 2)


def test_final_requires_production_lock_and_reviewed_final_assets(tmp_path):
    p = fixture_project(tmp_path)
    take = source_clip(tmp_path / "draft.mp4", seconds=2)
    import_asset(p, "S001", take, kind="draft")
    with pytest.raises(FilmError):
        compile_final(p)
    lock_production(p, "Synthetic fixture reviewer", mock_only=True)
    with pytest.raises(FilmError):
        compile_final(p)
    lock_production(p, "Synthetic fixture reviewer", mock_only=False)
    with pytest.raises(FilmError):
        compile_final(p)
    import_asset(p, "S001", take, kind="final", reviewer="Synthetic fixture reviewer",
                 evidence="Local fixture manually designated for this test")
    compile_final(p)
    folder, record = newest_build(p)
    assert record["mode"] == "FINAL"
    assert_master(folder / "MASTER_SUBBED.mp4", 2)
    assert verify_build(folder)["valid"]


def test_final_review_does_not_authorize_a_different_source_window(tmp_path):
    p = fixture_project(tmp_path)
    take = source_clip(tmp_path / "five_second_take.mp4", seconds=5)
    lock_production(p, "Synthetic fixture reviewer", mock_only=False)
    import_asset(p, "S001", take, kind="final", reviewer="Synthetic fixture reviewer",
                 evidence="Reviewed only the original 0-2 second source window")
    compile_final(p)
    folder, record = newest_build(p)
    assert record["mode"] == "FINAL"
    # A source-window change must require a fresh review even when the same file,
    # project LOCK and shot duration still pass technical media validation.
    registry = read(p / "manifest/assets.json")
    registry["shots"]["S001"]["final"]["source_in_ms"] = 1000
    write(p / "manifest/assets.json", registry)
    with pytest.raises(FilmError, match="eligible|approved|review"):
        compile_final(p)
    assert verify_build(folder)["valid"]


def test_invalid_timeline_is_not_silently_filled_or_retimed(tmp_path):
    p = fixture_project(tmp_path)
    shots = read(p / "manifest/shots.json")
    shots[0]["in_ms"] = 100
    shots[0]["duration_ms"] -= 100
    write(p / "manifest/shots.json", shots)
    with pytest.raises(FilmError, match="Gap|overlap|coverage"):
        compile_preview(p)
    assert not any(row.get("status") == "COMPLETE" for row in list_builds(p))


def test_measured_audio_duration_cannot_be_replaced_with_a_longer_manifest(tmp_path):
    p = fixture_project(tmp_path)
    analysis = read(p / "analysis/audio.json")
    analysis["duration_ms"] = 3000  # Actual PCM source remains exactly 2 seconds.
    write(p / "analysis/audio.json", analysis)
    shots = read(p / "manifest/shots.json")
    shots[0].update(out_ms=3000, duration_ms=3000)
    write(p / "manifest/shots.json", shots)
    with pytest.raises(FilmError, match="[Aa]udio|duration|[Mm]aster"):
        compile_preview(p)
    assert not any(row.get("status") == "COMPLETE" for row in list_builds(p))


def test_replay_needs_only_captured_inputs_and_detects_tampering(tmp_path):
    p = fixture_project(tmp_path)
    compile_preview(p)
    folder, record = newest_build(p)
    original_json_hash = digest(folder / "build.json")
    for name in ("input", "storyboard", "render", "lyrics"):
        shutil.rmtree(p / name)
    assert verify_build(folder)["valid"]
    replay_dir = tmp_path / "replay"
    replay_build(folder, replay_dir)
    assert_master(replay_dir / "MASTER_SUBBED.mp4", 2)
    assert digest(folder / "build.json") == original_json_hash
    clip = folder / record["shots"][0]["clip_path"]
    with clip.open("ab") as stream:
        stream.write(b"tampered")
    assert not verify_build(folder)["valid"]
    with pytest.raises(FilmError, match="integrity"):
        replay_build(folder, tmp_path / "invalid_replay")
