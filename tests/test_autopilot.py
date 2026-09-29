"""Autopilot follows the providers chosen in the model picker. Fakes only; nothing is sent or paid."""
import io
from pathlib import Path
from PIL import Image
import pytest
from engine import gemini, providers
from engine.audio import analyze, synth_test_audio
from engine.autopilot import autopilot
from engine.budget import approve
from engine.core import FilmError, ffmpeg, init_project, lock_production, read, write
from engine.imagegen import ImageProvider, ImageRejected, approve as approve_images
from engine.production import make_package


class FakeImages(ImageProvider):
    def __init__(self, unit=0.0, refs=False):
        self.id, self.model, self.unit_usd = "fake_image", "fake-1", unit
        self.supports_references, self.max_references = refs, 3
        self.calls, self.reject, self.timeout = 0, False, False

    def generate(self, prompt, references, aspect):
        self.calls += 1
        if self.reject:
            raise ImageRejected("quota")
        if self.timeout:
            raise TimeoutError()
        buffer = io.BytesIO()
        Image.new("RGB", (160, 90), "teal").save(buffer, format="PNG")
        return buffer.getvalue()


class FakeVeo:
    def __init__(self):
        self.posts, self.downloads, self.done = 0, 0, False

    def request(self, method, url, payload=None, timeout=60):
        if method == "POST":
            self.posts += 1
            return {"name": f"models/{gemini.VIDEO_MODEL}/operations/op{self.posts}"}
        if not self.done:
            return {"done": False}
        return {"done": True, "response": {"generateVideoResponse": {"generatedSamples": [
            {"video": {"uri": "https://generativelanguage.googleapis.com/v1beta/files/x:download?alt=media"}}]}}}

    def download(self, url, target):
        self.downloads += 1
        ffmpeg(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=24", "-t", "8", "-c:v", "libx264", "-pix_fmt", "yuv420p", target])


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-only-never-transmitted")
    audio = synth_test_audio(tmp_path / "t.wav", seconds=12)
    p = init_project(tmp_path, "auto", audio, "Autopilot fixture", synthetic=True, aspect="16:9")
    analyze(p)
    make_package(p)
    config = read(p / "project.yaml")
    config["budget"].update(max_usd=5, max_retry_per_shot=1)
    write(p / "project.yaml", config)
    return p


def test_it_stops_until_image_and_video_models_are_chosen(project):
    step = autopilot(project)
    assert step["stage"] == "NEEDS_MODEL_CHOICE" and step["missing"] == ["참조 이미지 · 첫 프레임", "영상"]
    providers.select(project, "image", "cloudflare_flux")
    assert autopilot(project)["missing"] == ["영상"]


def test_free_images_then_review_then_lock_then_test_preview(project):
    providers.select(project, "image", "cloudflare_flux")
    providers.select(project, "video", "mock_video")
    fake = FakeImages(unit=0.0)
    step = autopilot(project, image=fake)
    shots = read(project / "manifest/shots.json")
    assert step["stage"] == "FRAMES_READY_FOR_REVIEW" and sorted(step["made"]) == sorted(s["id"] for s in shots)
    assert fake.calls == len(shots)
    assert autopilot(project, image=fake)["stage"] == "NEEDS_LOCK" and fake.calls == len(shots)
    lock_production(project, "Director", mock_only=True)
    step = autopilot(project, image=fake)
    assert step["stage"] == "PREVIEW_READY" and Path(step["build"]).is_dir()


def test_paid_images_wait_for_approval_and_report_the_cost(project):
    providers.select(project, "image", "gemini_image")
    providers.select(project, "video", "mock_video")
    fake = FakeImages(unit=0.09)
    step = autopilot(project, image=fake)
    assert step["stage"] == "NEEDS_IMAGE_APPROVAL" and step["kind"] == "frames" and fake.calls == 0
    assert step["initial_usd"] == round(0.09 * 3, 6) and step["worst_case_usd"] == round(0.09 * 6, 6)
    approve_images(project, "frames", step["estimate_id"])
    assert autopilot(project, image=fake)["stage"] == "FRAMES_READY_FOR_REVIEW" and fake.calls == 3


def test_character_and_location_references_come_first_when_the_model_can_use_them(project):
    chars = read(project / "bible/characters.yaml")
    chars["characters"][0].update(look="adult woman, red coat, black bob hair", behavior="calm")
    write(project / "bible/characters.yaml", chars)
    providers.select(project, "image", "gemini_image")
    providers.select(project, "video", "mock_video")
    fake = FakeImages(unit=0.0, refs=True)
    step = autopilot(project, image=fake)
    assert step["stage"] == "FRAMES_READY_FOR_REVIEW" and step["made"][0] == "CHAR_01"
    assert any("LOC_01" in w for w in step["warnings"])           # the empty location was skipped, with a reason
    assert read(project / "bible/characters.yaml")["characters"][0]["reference_images"] == ["characters/CHAR_01.png"]


def test_a_refused_request_stops_with_the_reason(project):
    providers.select(project, "image", "cloudflare_flux")
    providers.select(project, "video", "mock_video")
    fake = FakeImages()
    fake.reject = True
    with pytest.raises(Exception, match="quota"):
        autopilot(project, image=fake)
    fake.reject = False
    assert autopilot(project, image=fake)["stage"] == "FRAMES_READY_FOR_REVIEW"     # nothing was created, so it runs again


def test_free_images_that_keep_failing_are_reported_and_can_be_retried(project):
    providers.select(project, "image", "cloudflare_flux")
    providers.select(project, "video", "mock_video")
    fake = FakeImages()
    fake.timeout = True
    step = autopilot(project, image=fake)
    assert step["stage"] == "IMAGES_FAILED" and step["kind"] == "frames" and "다시 실행" in step["next"]
    assert len(step["failed"]) == 3
    fake.timeout = False
    assert autopilot(project, image=fake)["stage"] == "FRAMES_READY_FOR_REVIEW"


def test_hand_made_stages_stop_with_a_work_order(project):
    providers.select(project, "image", "manual_image")
    providers.select(project, "video", "manual_video")
    step = autopilot(project)
    assert step["stage"] == "NEEDS_MANUAL_FRAMES" and (project / "render/packets/index.html").is_file() and len(step["shots"]) == 3
    lock_production(project, "Director", mock_only=True)
    from engine.production import import_frame
    for shot in read(project / "manifest/shots.json"):
        image = project / f"{shot['id']}.png"
        Image.new("RGB", (160, 90), "white").save(image)
        import_frame(project, shot["id"], image)
    assert autopilot(project)["stage"] == "NEEDS_LOCK"           # the mock-only lock does not authorise real video
    lock_production(project, "Director")
    assert autopilot(project)["stage"] == "NEEDS_MANUAL_VIDEO"


def test_gemini_video_end_to_end_with_the_chosen_image_model(project, monkeypatch):
    providers.select(project, "image", "cloudflare_flux")
    providers.select(project, "video", "gemini_video")
    veo = FakeVeo()
    monkeypatch.setattr(gemini, "GeminiHTTP", lambda: veo)
    images = FakeImages(unit=0.0)
    shots = read(project / "manifest/shots.json")
    assert autopilot(project, image=images)["stage"] == "FRAMES_READY_FOR_REVIEW"
    assert autopilot(project, image=images)["stage"] == "NEEDS_LOCK"
    lock_production(project, "Director")
    step = autopilot(project, image=images)
    assert step["stage"] == "NEEDS_VIDEO_APPROVAL" and veo.posts == 0
    approve(project, read(project / "render/estimate.json"))
    step = autopilot(project, image=images)
    assert step["stage"] == "GENERATING" and veo.posts == len(shots)
    veo.done = True
    step = autopilot(project, image=images)
    assert step["stage"] == "PREVIEW_READY" and veo.posts == len(shots) and veo.downloads == len(shots)
    assert sorted(step["needs_review"]) == sorted(s["id"] for s in shots)
