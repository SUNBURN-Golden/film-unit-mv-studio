"""Fault-injected Gemini tests: a fake transport stands in for Google; nothing is sent or paid."""
import base64
import io
from pathlib import Path
from urllib.error import HTTPError
from PIL import Image
import pytest
from engine import gemini
from engine.core import FilmError, ffmpeg, read, write
from engine.gemini import (DEFAULT_CONFIG, GeminiHTTP, GeminiImage, GeminiVideoRenderer, api_url, storage_url)
from engine.imagegen import ImageRejected
from engine.renderers import AwaitingRender, RenderBlocked

VIDEO_URI = "https://generativelanguage.googleapis.com/v1beta/files/fixture:download?alt=media"


def png_b64(size=(64, 36)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "teal").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


class FakeGemini:
    def __init__(self):
        self.posts, self.bodies, self.gets, self.downloads = [], [], 0, 0
        self.done = False
        self.error = None          # HTTP status to raise on POST
        self.ambiguous = False     # connection lost after sending
        self.frame_image = True
        self.video_error = None
        self.filtered = False
        self.fail_download = False

    def request(self, method, url, payload=None, timeout=60):
        api_url(url)
        if method == "POST":
            self.posts.append(url)
            self.bodies.append(payload)
            if self.error:
                raise HTTPError(url, self.error, "rejected", {}, io.BytesIO(b'{"error": {"message": "quota"}}'))
            if self.ambiguous:
                raise TimeoutError("sent, no answer")
            if url.endswith("/interactions"):
                content = [{"type": "image", "mime_type": "image/png", "data": png_b64()}] if self.frame_image else []
                return {"id": "interaction-1", "status": "completed",
                        "steps": [{"type": "model_output", "content": content}]}
            return {"name": f"models/{gemini.VIDEO_MODEL}/operations/op{len(self.posts)}"}
        self.gets += 1
        if not self.done:
            return {"done": False}
        if self.video_error:
            return {"done": True, "error": {"message": self.video_error}}
        if self.filtered:
            return {"done": True, "response": {"generateVideoResponse": {"raiMediaFilteredReasons": ["policy"]}}}
        return {"done": True, "response": {"generateVideoResponse": {"generatedSamples": [{"video": {"uri": VIDEO_URI}}]}}}

    def download(self, url, target):
        self.downloads += 1
        if self.fail_download:
            raise OSError("interrupted")
        ffmpeg(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=24", "-f", "lavfi", "-i", "sine=frequency=440",
                "-t", "8", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", target])


@pytest.fixture
def unit(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-only-never-transmitted")
    write(tmp_path / "project.yaml", {"format": {"width": 1920, "height": 1080, "fps": 24, "aspect_ratio": "16:9"},
                                      "budget": {"max_usd": 2, "max_retry_per_shot": 1}})
    write(tmp_path / "bible/style_bible.yaml", {"medium": "2D animation"})
    write(tmp_path / "bible/characters.yaml", {"characters": []})
    write(tmp_path / "render/gemini_config.json", DEFAULT_CONFIG)
    Image.new("RGB", (64, 36), "white").save(tmp_path / "frame.png")
    shot = {"id": "S001", "in_ms": 0, "out_ms": 5000, "duration_ms": 5000, "description": "Walk two steps",
            "render_mode": "LIMITED_MOTION", "renderer": "auto", "references": ["frame.png"],
            "composition": "wide", "camera": {"movement": "none"}, "motion": {"instruction": "walk"}}
    fake = FakeGemini()
    renderer = GeminiVideoRenderer(tmp_path, {"width": 1920, "height": 1080, "fps": 24}, fake)
    renderer.quality = "final"
    return tmp_path, shot, renderer, fake


def test_quote_picks_shortest_covering_clip_and_checks_limits(unit):
    p, shot, r, _ = unit
    assert r.quote({**shot, "duration_ms": 3200}, "final") == 0.2
    assert r.quote(shot, "final") == 0.3
    assert r.quote({**shot, "duration_ms": 8000}, "final") == 0.4
    with pytest.raises(FilmError, match="at most 8s"):
        r.quote({**shot, "duration_ms": 8001}, "final")
    with pytest.raises(FilmError, match="another provider"):
        r.quote({**shot, "renderer": "fal"}, "final")
    write(p / "render/gemini_config.json", {**DEFAULT_CONFIG, "video": {**DEFAULT_CONFIG["video"], "resolution": "1080p", "usd_per_second": 0.08}})
    assert r.quote({**shot, "duration_ms": 3200}, "final") == 0.64  # 1080p is 8s only


def test_quote_rejects_unsupported_aspect_and_stale_price(unit):
    p, shot, r, _ = unit
    config = read(p / "project.yaml")
    config["format"].update(width=1440, aspect_ratio="4:3")
    write(p / "project.yaml", config)
    with pytest.raises(FilmError, match="16:9 or 9:16"):
        r.quote(shot, "final")
    write(p / "render/gemini_config.json", {**DEFAULT_CONFIG, "price_valid_until": "2020-01-01T00:00:00+00:00"})
    with pytest.raises(FilmError, match="expired"):
        r.quote(shot, "final")
    write(p / "render/gemini_config.json", {**DEFAULT_CONFIG, "video": {**DEFAULT_CONFIG["video"], "usd_per_second": 0.01}})
    with pytest.raises(FilmError, match="price snapshot"):
        r.quote(shot, "final")


def test_submit_once_then_resume_polls_and_downloads(unit):
    p, shot, r, fake = unit
    target = p / "out.mp4"
    with pytest.raises(AwaitingRender):
        r.render(shot, target, job_id="job1")
    assert fake.posts[0].endswith(f"models/{gemini.VIDEO_MODEL}:predictLongRunning")
    instance, params = fake.bodies[0]["instances"][0], fake.bodies[0]["parameters"]
    assert instance["image"]["inlineData"]["mimeType"] == "image/png" and instance["image"]["inlineData"]["data"]
    assert "Walk two steps" in instance["prompt"] and "Character invariants" not in instance["prompt"]
    assert params == {"aspectRatio": "16:9", "durationSeconds": 6, "resolution": "720p", "personGeneration": "allow_adult"}
    with pytest.raises(AwaitingRender):
        r.render(shot, target, job_id="job1")
    fake.done = True
    result = r.render(shot, target, job_id="job1")
    assert len(fake.posts) == 1 and fake.downloads == 1 and target.exists()
    receipt = read(p / "render/gemini_jobs/job1.json")
    assert receipt["status"] == "COMPLETED" and receipt["sha256"] and result.provider == gemini.VIDEO_MODEL
    r.render(shot, target, job_id="job1")
    assert len(fake.posts) == 1 and fake.downloads == 1


def test_ambiguous_submission_is_never_resubmitted(unit):
    p, shot, r, fake = unit
    fake.ambiguous = True
    with pytest.raises(RenderBlocked, match="reconciliation"):
        r.render(shot, p / "out.mp4", job_id="job1")
    fake.ambiguous = False
    with pytest.raises(RenderBlocked, match="unknown"):
        r.render(shot, p / "out.mp4", job_id="job1")
    assert len(fake.posts) == 1
    assert read(p / "render/gemini_jobs/job1.json")["status"] == "SUBMITTING"


def test_definitive_rejection_can_be_submitted_again(unit):
    p, shot, r, fake = unit
    fake.error = 429
    with pytest.raises(RenderBlocked, match="rejected"):
        r.render(shot, p / "out.mp4", job_id="job1")
    assert not (p / "render/gemini_jobs/job1.json").exists()
    fake.error = None
    with pytest.raises(AwaitingRender):
        r.render(shot, p / "out.mp4", job_id="job1")
    assert len(fake.posts) == 2


def test_failed_or_filtered_generation_is_recorded(unit):
    p, shot, r, fake = unit
    with pytest.raises(AwaitingRender):
        r.render(shot, p / "a.mp4", job_id="job1")
    fake.done, fake.video_error = True, "bad input"
    with pytest.raises(FilmError, match="bad input"):
        r.render(shot, p / "a.mp4", job_id="job1")
    assert read(p / "render/gemini_jobs/job1.json")["status"] == "FAILED"
    fake.video_error, fake.done = None, False
    with pytest.raises(AwaitingRender):
        r.render(shot, p / "b.mp4", job_id="job2")
    fake.done, fake.filtered = True, True
    with pytest.raises(FilmError, match="policy"):
        r.render(shot, p / "b.mp4", job_id="job2")


def test_download_failure_resumes_without_paying_again(unit):
    p, shot, r, fake = unit
    fake.done, fake.fail_download = True, True
    with pytest.raises(RenderBlocked, match="2 days"):
        r.render(shot, p / "out.mp4", job_id="job1")
    fake.fail_download = False
    r.render(shot, p / "out.mp4", job_id="job1")
    assert len(fake.posts) == 1 and fake.downloads == 2


def test_key_goes_only_to_the_api_host(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")
    with pytest.raises(RenderBlocked):
        GeminiHTTP().request("GET", "https://example.com/v1beta/x")
    with pytest.raises(RenderBlocked):
        api_url("https://generativelanguage.googleapis.com.evil.test/v1beta/x")
    with pytest.raises(RenderBlocked):
        storage_url("https://evil.test/video.mp4")
    assert storage_url("https://video-downloads.googleusercontent.com/abc")
    seen = []

    class Opener:
        def open(self, request, timeout=60):
            url = request if isinstance(request, str) else request.full_url
            headers = {} if isinstance(request, str) else dict(request.header_items())
            seen.append((url, headers))
            if "googleapis" in url:
                raise HTTPError(url, 302, "Found", {"Location": "https://video-downloads.googleusercontent.com/abc"}, None)
            return io.BytesIO(b"clip bytes")

    monkeypatch.setattr(gemini, "build_opener", lambda *handlers: Opener())
    target = Path(__file__).parent / "_tmp_download.mp4"
    try:
        GeminiHTTP().download(VIDEO_URI, target)
        assert target.read_bytes() == b"clip bytes"
    finally:
        target.unlink(missing_ok=True)
    assert "X-goog-api-key" in seen[0][1]
    assert seen[1] == ("https://video-downloads.googleusercontent.com/abc", {})


def test_gemini_image_request_and_response(unit):
    p, _, _, fake = unit
    Image.new("RGB", (64, 36), "white").save(p / "ref.png")
    image = GeminiImage(None, fake)
    image.preflight(p)
    data = image.generate("A quiet street", [p / "ref.png"], "16:9")
    assert Image.open(io.BytesIO(data)).size == (64, 36)
    body = fake.bodies[0]
    assert fake.posts[0].endswith("/interactions") and body["model"] == gemini.FRAME_MODEL
    assert body["input"][0] == {"type": "text", "text": "A quiet street"} and body["input"][1]["type"] == "image"
    assert body["response_format"] == {"type": "image", "aspect_ratio": "16:9", "image_size": "1K"}


def test_gemini_image_rejection_and_empty_answer(unit):
    p, _, _, fake = unit
    image = GeminiImage(None, fake)
    fake.error = 429
    with pytest.raises(ImageRejected, match="429"):
        image.generate("x", [], "16:9")
    fake.error, fake.frame_image = None, False
    with pytest.raises(ImageRejected, match="no image"):
        image.generate("x", [], "16:9")


def test_gemini_image_price_evidence_is_checked(unit):
    p, _, _, _ = unit
    write(p / "render/gemini_config.json", {**DEFAULT_CONFIG, "price_valid_until": "2020-01-01T00:00:00+00:00"})
    with pytest.raises(FilmError, match="expired"):
        GeminiImage().preflight(p)
