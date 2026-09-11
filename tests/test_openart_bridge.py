"""No provider calls or spending: real schema, synthetic quote and fixture URL."""
from pathlib import Path
import pytest
from engine.core import FilmError, read, write
from engine.openart_bridge import register_quote, record_submission
from engine.renderers import AwaitingRender, OpenArtRenderer
from engine.production import make_package
from engine.audio import analyze, synth_test_audio
from engine.core import init_project


@pytest.mark.parametrize("template", ["seedance_fast_image2video.json", "pixverse_v6_image2video.json"])
def test_bridge_request_schema_audio_off_stable_job_and_quote_drift(tmp_path, template):
    master = synth_test_audio(tmp_path / "source.wav", 5)
    p = init_project(tmp_path, "bridge_fixture", master, "TEST ONLY")
    analyze(p)
    shots = make_package(p)
    shots[0]["render_mode"] = "LIMITED_MOTION"
    write(p / "manifest/shots.json", shots)
    form = read(Path(__file__).resolve().parents[1] / "templates" / template)
    reference = {"id": "TEST_ONLY", "label": "fixture", "type": "image", "url": "https://example.invalid/fixture.png"}
    priced_config = {"duration": 5, "resolution": "720p", "videoCount": 1, "generateAudio": False}
    if "aspectRatio" in form["jsonSchema"]["properties"]:
        priced_config["aspectRatio"] = "4:3"
    cost = {"items": [{"model": form["model"], "mode": form["mode"], "totalCredits": 7, "config": priced_config}]}
    register_quote(p, "S001", form, reference, cost, extra_params={"resolution": "720p"})
    renderer = OpenArtRenderer(p, read(p / "project.yaml")["format"])
    renderer.quality = "draft"
    assert renderer.quote(shots[0], "draft") == 7
    for _ in range(2):
        with pytest.raises(AwaitingRender):
            renderer.render(shots[0], tmp_path / "unused.mp4", job_id="fixture_S001_a0")
    requests = list((p / "render/requests").glob("*.json"))
    assert len(requests) == 1
    request = read(requests[0])
    assert request["arguments"]["params"]["generateAudio"] is False
    if "aspectRatio" not in priced_config:
        assert "aspectRatio" not in request["arguments"]["params"]
    record_submission(p, request["job_id"], "test-history")
    with pytest.raises(FilmError, match="twice"):
        record_submission(p, request["job_id"], "different-history")
    config = read(p / "render/openart_config.json")
    config["shots"]["S001"]["draft"]["params"]["resolution"] = "1080p"
    write(p / "render/openart_config.json", config)
    with pytest.raises(FilmError, match="priced"):
        OpenArtRenderer(p, renderer.fmt).quote(shots[0], "draft")
