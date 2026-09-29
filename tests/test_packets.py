import json
from math import ceil
from pathlib import Path
import pytest
from PIL import Image
from engine.audio import analyze, synth_test_audio
from engine.cli import main
from engine.core import FilmError, digest, init_project, production_fingerprint, read, visual_context_fingerprint, write
from engine.packets import export_packets
from engine.production import make_package


@pytest.fixture
def project(tmp_path):
    audio = synth_test_audio(tmp_path / "test.wav", seconds=12)
    p = init_project(tmp_path, "packets", audio, "Packet fixture", synthetic=True)
    analyze(p)
    make_package(p)
    return p


def test_packets_cover_every_shot_and_follow_render_mode(project):
    shots = read(project / "manifest/shots.json")
    shots[0]["render_mode"] = "STATIC"
    write(project / "manifest/shots.json", shots)
    result = export_packets(project)
    data = read(project / "render/packets/packets.json")
    assert result["shots"] == len(shots) == len(data["shots"])
    assert data["shots"][0]["motion"] is None
    assert result["motions"] == len(shots) - 1
    for shot, packet in zip(shots[1:], data["shots"][1:]):
        assert packet["motion"]["min_seconds"] == ceil(shot["duration_ms"] / 1000)
        assert packet["motion"]["start_frame"] == shot["references"][0]
        assert "import-asset" in packet["motion"]["import"] and "--kind draft" in packet["motion"]["import"]
    assert data["aspect_ratio"] == "4:3"
    frame = data["shots"][0]["frame"]
    assert shots[0]["description"] in frame["prompt"] and "No text" in frame["prompt"]
    assert "import-frame" in frame["import"]
    assert any("still marked draft" in w for w in result["warnings"])


def test_packets_leave_production_state_unchanged(project):
    shots_before = digest(project / "manifest/shots.json")
    production, context = production_fingerprint(project), visual_context_fingerprint(project)
    export_packets(project)
    assert digest(project / "manifest/shots.json") == shots_before
    assert production_fingerprint(project) == production
    assert visual_context_fingerprint(project) == context


def test_packets_list_existing_references_and_report_missing(project):
    Image.new("RGB", (8, 8), "white").save(project / "characters/char01.png")
    chars = read(project / "bible/characters.yaml")
    chars["characters"][0]["reference_images"] = ["characters/char01.png", "characters/missing.png"]
    write(project / "bible/characters.yaml", chars)
    result = export_packets(project)
    first = read(project / "render/packets/packets.json")["shots"][0]
    assert first["frame"]["references"] == ["characters/char01.png"]
    assert any("characters/missing.png" in w for w in result["warnings"])


def test_packets_reject_references_outside_project(project):
    chars = read(project / "bible/characters.yaml")
    chars["characters"][0]["reference_images"] = ["../outside.png"]
    write(project / "bible/characters.yaml", chars)
    with pytest.raises(FilmError, match="inside this project"):
        export_packets(project)


def test_packet_page_escapes_shot_text(project):
    shots = read(project / "manifest/shots.json")
    shots[0]["description"] = "<script>alert(1)</script>"
    write(project / "manifest/shots.json", shots)
    export_packets(project)
    page = (project / "render/packets/index.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_packets_shot_filter_and_cli(project, capsys):
    shots = read(project / "manifest/shots.json")
    with pytest.raises(FilmError, match="Unknown shot ID"):
        export_packets(project, ["S999"])
    assert main(["packets", str(project), "--shots", shots[0]["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["shots"] == 1
    assert [x["id"] for x in read(project / "render/packets/packets.json")["shots"]] == [shots[0]["id"]]
