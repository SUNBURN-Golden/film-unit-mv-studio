"""Fault-injected queue tests: never contact a provider or spend money."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from PIL import Image
import pytest
from engine.core import FilmError, read, write
from engine.budget import make_estimate, reserve
from engine.fal_renderer import ENDPOINT, FalRenderer, queue_url
from engine.renderers import AwaitingRender, RenderBlocked


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fixture-only-never-transmitted")
    write(tmp_path / "project.yaml", {"budget": {"max_credits": 20, "max_usd": 0.30, "max_retry_per_shot": 2}})
    write(tmp_path / "bible/style_bible.yaml", {"medium": "2D animation"})
    write(tmp_path / "bible/characters.yaml", {"id": "fixture"})
    Image.new("RGB", (64, 48), "white").save(tmp_path / "frame.png")
    config = {"endpoint": ENDPOINT, "resolution": "720p", "usd_per_video": 0.1,
              "price_valid_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    write(tmp_path / "render/fal_config.json", config)
    shot = {"id": "S001", "in_ms": 0, "out_ms": 5000, "duration_ms": 5000, "description": "Walk two steps",
            "render_mode": "LIMITED_MOTION", "renderer": "auto", "references": ["frame.png"],
            "composition": "wide", "camera": {"movement": "none"}, "motion": {"instruction": "walk"}}
    return tmp_path, shot


class QueueFixture:
    def __init__(self):
        self.posts = 0
        self.ready = False
        self.fail_submit = False
        self.fail_download = False
        self.fail_generation = False
        self.downloads = 0

    def request(self, method, url, payload=None):
        if method == "POST":
            self.posts += 1
            self.payload = payload
            if self.fail_submit:
                raise TimeoutError("ambiguous post")
            return {"request_id": "fixture-request", "status_url": "https://queue.fal.run/fal-ai/wan/requests/fixture/status",
                    "response_url": "https://queue.fal.run/fal-ai/wan/requests/fixture"}
        if url.endswith("/status"):
            return {"status": "COMPLETED" if self.ready else "IN_QUEUE", "error": "rejected" if self.fail_generation else None}
        return {"video": {"url": "https://fal.media/fixture.mp4"}}

    def download(self, url, target):
        self.downloads += 1
        if self.fail_download:
            raise OSError("interrupted download")
        target.write_bytes(b"fixture transport bytes, not a playable video")


def renderer(setup, transport):
    p, shot = setup
    r = FalRenderer(p, {}, transport)
    r.quality = "draft"
    return p, shot, r


def test_pending_and_download_failure_resume_without_another_payment(setup):
    network = QueueFixture()
    p, shot, r = renderer(setup, network)
    for _ in range(2):
        with pytest.raises(AwaitingRender):
            r.render(shot, p / "result.mp4", job_id="job")
    assert network.posts == 1
    assert network.payload["enable_safety_checker"] is True
    assert network.payload["enable_output_safety_checker"] is True
    assert network.payload["enable_prompt_expansion"] is False
    assert network.payload["image_url"].startswith("data:image/png;base64,")
    assert "duration" not in network.payload and "generateAudio" not in network.payload
    network.ready = network.fail_download = True
    with pytest.raises(RenderBlocked, match="download"):
        r.render(shot, p / "result.mp4", job_id="job")
    network.fail_download = False
    result = r.render(shot, p / "result.mp4", job_id="job")
    assert result.generated and network.posts == 1
    r.render(shot, p / "result.mp4", job_id="job")
    assert network.downloads == 2


def test_ambiguous_submission_stops_instead_of_spending_again(setup):
    network = QueueFixture()
    network.fail_submit = True
    p, shot, r = renderer(setup, network)
    for _ in range(2):
        with pytest.raises(RenderBlocked):
            r.render(shot, p / "result.mp4", job_id="job")
    assert network.posts == 1
    assert read(p / "render/fal_jobs/job.json")["status"] == "SUBMITTING"


def test_usd_cap_is_separate_from_legacy_openart_credits(setup):
    p, shot, r = renderer(setup, QueueFixture())
    write(p / "render/ledger.json", {"jobs": {"legacy": {"reserved_credits": 20, "estimate_id": "old"}}})
    estimate = make_estimate(p, [shot], r, "draft", "fixture-lock")
    assert estimate["billing_unit"] == "USD" and estimate["worst_case_amount"] == 0.3
    assert "worst_case_credits" not in estimate
    for job in ("a0", "a1", "a2"):
        reserve(p, job, 0.1, estimate)
        reserve(p, job, 0.1, estimate)
    with pytest.raises(FilmError, match="cap reached"):
        reserve(p, "a3", 0.1, estimate)
    with pytest.raises(FilmError, match="differs"):
        reserve(p, "a0", 0.2, estimate)
    changed = read(p / "project.yaml")
    changed["budget"]["max_usd"] = 0
    write(p / "project.yaml", changed)
    with pytest.raises(FilmError, match="exceeds budget"):
        make_estimate(p, [shot], r, "draft", "fixture-lock")


def test_price_duration_key_and_queue_url_preflight(setup, monkeypatch):
    network = QueueFixture()
    p, shot, r = renderer(setup, network)
    original_hash = r.config_hash()
    r.config["price_valid_until"] = "2099-01-01T00:00:00+00:00"
    assert r.config_hash() == original_hash
    with pytest.raises(FilmError, match="split"):
        r.quote(dict(shot, duration_ms=6000), "draft")
    r.config["price_valid_until"] = "2000-01-01T00:00:00+00:00"
    with pytest.raises(FilmError, match="expired"):
        r.quote(shot, "draft")
    monkeypatch.delenv("FAL_KEY")
    with pytest.raises(RenderBlocked, match="not connected"):
        r.preflight()
    with pytest.raises(RenderBlocked, match="URL"):
        queue_url("https://example.invalid/fal-ai/wan/requests/id")
    assert network.posts == 0


def test_confirmed_provider_failure_is_retryable_with_correction(setup):
    network = QueueFixture()
    network.ready = network.fail_generation = True
    p, shot, r = renderer(setup, network)
    with pytest.raises(FilmError, match="generation failed"):
        r.render(shot, p / "result.mp4", job_id="job_a0")
    assert read(p / "render/fal_jobs/job_a0.json")["status"] == "FAILED"
    network.fail_generation = False
    r.render(shot, p / "result.mp4", attempt=1, correction="Keep the tie triangular", job_id="job_a1")
    assert "Keep the tie triangular" in network.payload["prompt"]
    assert network.posts == 2
