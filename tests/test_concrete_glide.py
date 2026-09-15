"""Original city preset and a local Preview, with provider submission forbidden.

The short media regression checks the same compiler path as the separately
rendered 240-second demo; it makes no claim of completed character animation.
"""
from pathlib import Path
import json
import re
import shutil
import socket
import wave

import pytest

from engine.builds import list_builds, verify_build
from engine.cli import main
from engine.core import atomic_text, digest, init_project, probe, read, validate_manifest, write
from engine.production import load_preset, make_package


def metadata_project(tmp_path, *, name="city", preset=None):
    """Package authoring only: no media decoding or FFmpeg dependency."""
    project = tmp_path / name
    for directory in ("input", "analysis", "bible", "manifest", "storyboard"):
        (project / directory).mkdir(parents=True)
    atomic_text(project / "input/master.wav", "metadata-only master fixture")
    atomic_text(project / "input/brief.md", "Two original couriers find a route through a night city.")
    atomic_text(project / "input/lyrics.txt", "")
    config = {
        "name": name,
        "audio": {"path": "input/master.wav", "sha256": digest(project / "input/master.wav")},
        "format": {"width": 640, "height": 480, "fps": 24, "aspect_ratio": "4:3"},
        "budget": {"max_usd": 0},
    }
    if preset:
        config["production"] = {"preset": preset}
    write(project / "project.yaml", config)
    write(project / "analysis/audio.json", {
        "duration_ms": 12000,
        "beat_times_ms": [4990, 10010],
        "section_boundaries_ms": [0, 12000],
    })
    return project


def test_concrete_glide_preset_has_original_cast_city_and_distinct_camera():
    preset = load_preset("concrete_glide")
    water = load_preset("water_please")
    assert set(preset) == {"style", "characters", "locations", "directing"}
    assert preset["style"]["palette"] != water["style"]["palette"]
    assert len(preset["style"]["palette"]) >= 4
    assert all(re.fullmatch(r"#[0-9A-Fa-f]{6}", color)
               for color in preset["style"]["palette"].values())
    assert "2D" in preset["style"]["visual_style"]["medium"]

    cast = preset["characters"]["characters"]
    locations = preset["locations"]["locations"]
    cast_ids = {character["id"] for character in cast}
    location_ids = {location["id"] for location in locations}
    assert len(cast_ids) == len(cast) >= 2
    assert len(location_ids) == len(locations) >= 6
    assert cast_ids.isdisjoint({"CHAR_A", "CHAR_B"})
    assert cast[0]["invariants"] and cast[1]["invariants"]
    assert cast[0]["invariants"] != cast[1]["invariants"]
    for character in cast:
        assert character["reference_images"] == []
        assert character["variants"]
    for location in locations:
        assert location["reference_images"] == []
        assert {"wide", "medium"} <= set(location["required_views"])
        assert {"prop", "detail"} & set(location["required_views"])

    direction = preset["directing"]
    assert set(direction["characters"]) <= cast_ids
    assert set(direction["locations"]) <= location_ids
    assert {intent["location"] for intent in direction["sequence_intents"]} == location_ids
    assert direction["camera"]["movement"] != "none"
    assert any(word in json.dumps(direction["camera"]).lower() for word in ("glid", "track"))
    assert direction["composition"] != water["directing"]["composition"]
    assert direction["render_mode"] in {"STATIC", "LIMITED_MOTION"}
    assert direction["motion"]["local_effect"] in {"hold", "pan", "zoom"}
    assert "Reference art required" in direction["review_note"]
    assert "not completed animation" in direction["review_note"]


@pytest.mark.parametrize("selection", ["cli", "project_config"])
def test_package_writes_four_bibles_and_preserves_preset_intent(tmp_path, capsys, selection):
    project = metadata_project(tmp_path, preset="concrete_glide" if selection == "project_config" else None)
    args = ["package", str(project)]
    if selection == "cli":
        args.extend(["--preset", "concrete_glide"])
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["shots"] == 3
    expected = load_preset("concrete_glide")
    expected["style"]["format"] = {"aspect_ratio": "4:3", "resolution": "640x480", "fps": 24}
    for key, filename in {
        "style": "style_bible.yaml", "characters": "characters.yaml",
        "locations": "locations.yaml", "directing": "directing.yaml",
    }.items():
        assert read(project / "bible" / filename) == expected[key]

    shots = read(project / "manifest/shots.json")
    validate_manifest(shots, 12000, 24)
    assert [shot["in_ms"] for shot in shots] == [0, 4990, 10010]
    assert len({shot["references"][0] for shot in shots}) == len(shots)
    for shot in shots:
        for field in ("characters", "locations", "camera", "motion", "render_mode", "composition"):
            assert shot[field] == expected["directing"][field]
        assert (project / shot["references"][0]).is_file()
        assert shot["status"] == "storyboard"
    handoff = (project / "bible/director_request.md").read_text()
    assert expected["directing"]["review_note"] in handoff
    assert not (project / "manifest/locks.json").exists()


def test_preset_and_shots_do_not_share_mutable_direction(tmp_path):
    original = load_preset("concrete_glide")
    changed = load_preset("concrete_glide")
    changed["characters"]["characters"][0]["reference_images"].append("temporary.png")
    changed["style"]["palette"]["custom"] = "#000000"
    changed["directing"]["camera"]["movement"] = "none"
    assert load_preset("concrete_glide") == original

    project = metadata_project(tmp_path)
    shots = make_package(project, preset="concrete_glide")
    shots[0]["camera"]["movement"] = "none"
    shots[0]["characters"].clear()
    shots[0]["motion"]["local_effect"] = "zoom"
    assert shots[1]["camera"] == original["directing"]["camera"]
    assert shots[1]["characters"] == original["directing"]["characters"]
    assert shots[1]["motion"] == original["directing"]["motion"]
    assert read(project / "bible/directing.yaml") == original["directing"]


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="Local Preview regression requires FFmpeg and ffprobe")
def test_concrete_glide_preview_is_local_full_duration_and_keeps_master(tmp_path, monkeypatch, capsys):
    from engine.economy import EconomyRenderer
    from engine.fal_renderer import FalRenderer
    from engine.renderers import OpenArtRenderer

    def forbidden(*args, **kwargs):
        pytest.fail("A local preset Preview must never submit to a provider or use the network")

    for renderer in (OpenArtRenderer, FalRenderer, EconomyRenderer):
        monkeypatch.setattr(renderer, "render", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    source = tmp_path / "synthetic.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 32000)  # Exactly two seconds of synthetic PCM silence.
    project = init_project(tmp_path, "local_preview", source,
                           "Original city timing fixture; no approved animation or lyric timing.",
                           synthetic=True)
    config = read(project / "project.yaml")
    config["format"].update(width=320, height=240)
    write(project / "project.yaml", config)
    write(project / "analysis/audio.json", {
        "duration_ms": 2000, "master_sha256": digest(source),
        "beat_times_ms": [], "section_boundaries_ms": [0, 2000],
        "fixture": "Synthetic PCM duration measured with ffprobe; no musical section inference",
    })
    assert round(float(probe(source)["format"]["duration"]) * 1000) == 2000
    assert main(["package", str(project), "--preset", "concrete_glide"]) == 0
    capsys.readouterr()
    master_sha = digest(project / "input/master.wav")
    assert main(["compile-preview", str(project), "--quality", "final"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "COMPLETE"
    assert result["mode"] == "PREVIEW"
    assert not (project / "manifest/locks.json").exists()

    record = list_builds(project)[0]
    assert record["duration_ms"] == 2000
    assert record["audio"]["sha256"] == master_sha == digest(project / "input/master.wav")
    assert len(record["shots"]) == 1
    assert record["shots"][0]["kind"] == "storyboard"
    build_dir = Path(record["build_dir"])
    for filename in ("MASTER_CLEAN.mp4", "MASTER_SUBBED.mp4"):
        metadata = probe(build_dir / filename)
        video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
        assert int(video["nb_frames"]) == 48
        assert (video["width"], video["height"], video["avg_frame_rate"]) == (320, 240, "24/1")
        assert abs(float(metadata["format"]["duration"]) - 2) < 0.05
        assert len([stream for stream in metadata["streams"] if stream["codec_type"] == "audio"]) == 1
    assert not any(stream["codec_type"] == "audio"
                   for stream in probe(build_dir / record["shots"][0]["clip_path"])["streams"])
    assert verify_build(build_dir)["valid"]
    assert not list((project / "render/requests").iterdir())
    assert main(["builds", str(project)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["build_id"] == record["build_id"]
