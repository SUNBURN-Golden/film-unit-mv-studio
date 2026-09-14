"""Local selection failures must never turn a valid take into another generation."""
import pytest

from engine import pipeline
from engine.core import FilmError, ffmpeg, init_project, lock_production, read, write
from engine.production import import_frame, make_package
from engine.renderers import ManualRenderer, RenderBlocked


@pytest.fixture
def registration_project(tmp_path):
    master = tmp_path / "source.wav"
    ffmpeg(["-f", "lavfi", "-i", "sine=frequency=220:duration=2", master])
    project = init_project(tmp_path, "registration_fixture", master,
                           "Synthetic registration failure regression; no AI generation.",
                           synthetic=True)
    config = read(project / "project.yaml")
    config["format"].update(width=320, height=240)
    write(project / "project.yaml", config)
    write(project / "analysis/audio.json", {
        "duration_ms": 2000, "beat_times_ms": [], "section_boundaries_ms": [0, 2000],
        "fixture": "Synthetic two-second PCM; musical feature inference is not tested here",
    })
    make_package(project)
    shots = read(project / "manifest/shots.json")
    shots[0]["render_mode"] = "FULL_GENERATIVE"
    write(project / "manifest/shots.json", shots)
    import_frame(project, "S001", project / "storyboard/S001.png")
    lock_production(project, "Synthetic fixture reviewer", mock_only=False)
    ffmpeg(["-f", "lavfi", "-i", "color=c=gray:s=320x240:r=24:d=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "1",
            project / "render/manual/S001_a0.mp4"])
    return project


@pytest.mark.parametrize("failure", [FilmError, OSError])
def test_registration_failure_blocks_without_retry_or_repeated_submission(
        registration_project, monkeypatch, failure):
    project = registration_project
    submissions, inspected, registrations = [], [], []
    original_render = ManualRenderer.render
    original_inspect = pipeline.inspect_clip

    def render(self, shot, target, attempt=0, correction="", job_id=""):
        submissions.append((attempt, job_id))
        return original_render(self, shot, target, attempt, correction, job_id)

    def inspect(*args, **kwargs):
        result = original_inspect(*args, **kwargs)
        inspected.append(result["status"])
        return result

    def cannot_register(*args, **kwargs):
        registrations.append(args[1]["id"])
        raise failure("Injected local asset storage failure")

    monkeypatch.setattr(ManualRenderer, "render", render)
    monkeypatch.setattr(pipeline, "inspect_clip", inspect)
    monkeypatch.setattr(pipeline, "register_asset", cannot_register)

    # Reopening the same failed batch must reuse the already normalized clip.
    for _ in range(2):
        with pytest.raises(RenderBlocked, match="Local asset registration failed"):
            pipeline.compile_project(project, mode="manual")
        report = read(project / "qc/report.json")
        assert report["status"] == "BLOCKED"
        assert report["shots"][0]["status"] == "BLOCKED"
        states = list((project / "render/final").glob("*/state.json"))
        assert len(states) == 1
        state = read(states[0])["shots"]["S001"]
        assert state["attempt"] == 0
        assert not state.get("failures")
        assert len(submissions) == 1
        assert report["shots"][0]["job_id"] == submissions[0][1]

    assert inspected == ["NEEDS_REVIEW", "NEEDS_REVIEW"]
    assert registrations == ["S001", "S001"]
    assert submissions[0][0] == 0
    assert not list((project / "render/final").glob("*/S001_a1*"))
