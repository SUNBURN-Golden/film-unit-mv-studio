from pathlib import Path

from PIL import Image
import pytest

from engine.core import FilmError, atomic_text, digest, ffmpeg, init_project, probe, read, write
from engine.flow_music import export_flow_music, import_flow_result


def flow_fixture(tmp_path, seconds=4):
    master = tmp_path / "flow_master.wav"
    ffmpeg([
        "-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=16000:duration={seconds}",
        "-c:a", "pcm_s16le", master,
    ])
    lyrics = "첫 번째 정확한 가사\n두 번째 정확한 가사\n"
    p = init_project(
        tmp_path, "flow_fixture", master,
        "Deadpan cinematic test brief. Preserve the supplied song and exact timing.",
        lyrics=lyrics, synthetic=True,
    )
    config = read(p / "project.yaml")
    config["format"].update(width=320, height=240, fps=24, aspect_ratio="4:3")
    write(p / "project.yaml", config)
    duration_ms = round(float(probe(master)["format"]["duration"]) * 1000)
    write(p / "analysis/audio.json", {
        "schema_version": "0.1",
        "master_sha256": digest(master),
        "duration_ms": duration_ms,
        "beat_times_ms": [],
        "onsets_ms": [],
        "section_boundaries_ms": [0, duration_ms],
    })
    write(p / "manifest/sequence.json", [{
        "id": "SEQ01", "in_ms": 0, "out_ms": duration_ms, "title": "Flow trial section",
    }])
    atomic_text(p / "bible/story.md", "A father tries to finish one task before playtime interrupts him.\n")
    write(p / "bible/style_bible.yaml", {"medium": "cinematic live action", "draft": False})
    write(p / "bible/characters.yaml", {"characters": [{"id": "FATHER", "invariants": {"hair": "short"}}], "draft": False})
    write(p / "bible/locations.yaml", {"locations": [{"id": "HOME", "description": "small study"}], "draft": False})
    write(p / "bible/directing.yaml", {"camera": "controlled", "draft": False})

    midpoint = duration_ms // 2
    shots = [
        {
            "id": "S001", "sequence": "SEQ01", "in_ms": 0, "out_ms": midpoint,
            "duration_ms": midpoint, "description": "Father works at the desk.",
            "characters": ["FATHER"], "locations": ["HOME"], "composition": "medium wide",
            "camera": {"type": "locked", "movement": "none"},
            "motion": {"complexity": "low", "instruction": "Hands type once", "local_effect": "hold"},
            "references": ["storyboard/S001.png"], "render_mode": "FULL_GENERATIVE",
            "renderer": "manual", "status": "storyboard", "storyboard_kind": "imported",
        },
        {
            "id": "S002", "sequence": "SEQ01", "in_ms": midpoint, "out_ms": duration_ms,
            "duration_ms": duration_ms - midpoint, "description": "He looks behind him after an interruption.",
            "characters": ["FATHER"], "locations": ["HOME"], "composition": "medium close-up",
            "camera": {"type": "locked", "movement": "none"},
            "motion": {"complexity": "low", "instruction": "Eyes turn, then head", "local_effect": "hold"},
            "references": ["storyboard/S002.png"], "render_mode": "FULL_GENERATIVE",
            "renderer": "manual", "status": "storyboard", "storyboard_kind": "imported",
        },
    ]
    write(p / "manifest/shots.json", shots)
    Image.new("RGB", (320, 240), "navy").save(p / "storyboard/S001.png")
    Image.new("RGB", (320, 240), "gray").save(p / "storyboard/S002.png")
    return p, master, duration_ms, lyrics


def make_flow_result(path, seconds=4):
    ffmpeg([
        "-f", "lavfi", "-i", f"color=c=purple:s=320x240:r=24:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=880:sample_rate=16000:duration={seconds}",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-threads", "1", "-shortest", path,
    ])
    return path


def test_flow_export_is_append_only_source_faithful_and_makes_no_provider_call(tmp_path):
    p, master, duration_ms, lyrics = flow_fixture(tmp_path)
    first = export_flow_music(p)
    first_dir = Path(first["bundle"])
    first_manifest_hash = digest(first_dir / "manifest.json")

    assert first["status"] == "READY_FOR_MANUAL_FLOW_MUSIC"
    assert first["paid_provider_calls"] == 0
    assert first["shots"] == 2
    assert first_dir.name == "F0001"
    assert digest(first_dir / "audio" / "master.wav") == digest(master)
    assert (first_dir / "lyrics.txt").read_text(encoding="utf-8") == lyrics
    prompt = (first_dir / "MASTER_PROMPT.md").read_text(encoding="utf-8")
    assert "attached ORIGINAL MUSIC FILE" in prompt
    assert "첫 번째 정확한 가사" in prompt
    assert "S001" in prompt and "S002" in prompt
    manifest = read(first_dir / "manifest.json")
    assert manifest["paid_provider_calls_made"] == 0
    assert manifest["provider_submission"] == "MANUAL_ONLY"
    assert manifest["duration_ms"] == duration_ms
    assert not manifest["missing_assets"]

    second = export_flow_music(p)
    assert Path(second["bundle"]).name == "F0002"
    assert digest(first_dir / "manifest.json") == first_manifest_hash


def test_whole_flow_result_registers_exact_shot_windows_as_draft(tmp_path):
    p, _, duration_ms, _ = flow_fixture(tmp_path)
    source = make_flow_result(tmp_path / "flow_result.mp4", duration_ms / 1000)
    result = import_flow_result(p, source)

    assert result["status"] == "IMPORTED"
    assert result["kind"] == "draft"
    assert result["shots_registered"] == 2
    registry = read(p / "manifest/assets.json")
    first = registry["shots"]["S001"]["draft"]
    second = registry["shots"]["S002"]["draft"]
    assert first["sha256"] == second["sha256"] == digest(source)
    assert first["source_in_ms"] == 0
    assert second["source_in_ms"] == duration_ms // 2
    assert first["path"] == second["path"]
    assert result["audio_policy"].startswith("Downloaded Flow audio is ignored")


def test_flow_result_is_not_silently_retimed_and_final_requires_attestation(tmp_path):
    p, _, duration_ms, _ = flow_fixture(tmp_path)
    too_short = make_flow_result(tmp_path / "short.mp4", (duration_ms - 1000) / 1000)
    with pytest.raises(FilmError, match="will not silently retime"):
        import_flow_result(p, too_short)

    exact = make_flow_result(tmp_path / "exact.mp4", duration_ms / 1000)
    with pytest.raises(FilmError, match="reviewer and evidence"):
        import_flow_result(p, exact, kind="final")

    result = import_flow_result(
        p, exact, kind="final", reviewer="Human trial reviewer",
        evidence="Reviewed the downloaded whole-song Flow result against both exact source windows",
    )
    assert result["kind"] == "final"
    registry = read(p / "manifest/assets.json")
    assert registry["shots"]["S001"]["final"]["review"]["binding"]
    assert registry["shots"]["S002"]["final"]["review"]["binding"]
