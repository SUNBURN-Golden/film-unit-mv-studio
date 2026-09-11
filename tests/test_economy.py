"""Real local media + injected provider failures. No network, no paid generation."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import copy
import shutil
import pytest
from test_media import small_project
from engine import budget
from engine.core import FilmError, digest, ffmpeg, read, write
from engine.economy import initialize
from engine.fal_renderer import ENDPOINT, FalHTTP
from engine.openart_bridge import register_quote, claim_submission, record_submission, record_failure, import_completed
from engine.pipeline import compile_project, prepare
from engine.qc import SEMANTIC_ITEMS, save_review
from engine.renderers import RenderBlocked
from engine.takes import select_window
from engine.work_queue import status


@pytest.fixture
def priced(small_project):
    p = small_project
    config = read(p / "project.yaml")
    config["budget"].update(max_usd=1, max_credits=100, max_retry_per_shot=2)
    write(p / "project.yaml", config)
    form = read(Path(__file__).resolve().parents[1] / "templates/pixverse_v6_image2video.json")
    reference = {"id": "TEST_ONLY", "label": "fixture", "type": "image", "url": "https://example.invalid/fixture.png"}
    cost = {"items": [{"model": form["model"], "mode": form["mode"], "totalCredits": 7,
        "config": {"duration": 5, "resolution": "720p", "videoCount": 1, "generateAudio": False}}]}
    for quality in ("draft", "final"):
        register_quote(p, "S001", form, reference, cost, quality, {"resolution": "720p"})
    write(p / "render/fal_config.json", {"endpoint": ENDPOINT, "resolution": "720p", "usd_per_video": .1,
        "price_valid_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
    initialize(p)
    return p


def approved(p, mode="economy", quality="draft"):
    estimate = prepare(p, mode=mode, quality=quality)[4]
    budget.approve(p, estimate)
    return estimate


def record(p):
    return read(p / "qc/report.json")["shots"][0]


def test_cost_order_separate_pools_and_hard_provider_constraint(priced):
    p = priced
    expensive = read(p / "render/openart_config.json")
    for quality in ("draft", "final"):
        expensive["shots"]["S001"][quality]["credits"] = 30
    write(p / "render/profiles/expensive.json", expensive)
    policy = read(p / "render/economy.json")
    policy["profiles"].insert(0, {"id": "expensive", "provider": "openart", "config_path": "render/profiles/expensive.json"})
    write(p / "render/economy.json", policy)
    prepared = prepare(p, mode="economy", quality="draft")
    assert [a["profile"] for a in prepared[4]["rows"][0]["attempts"]] == ["openart", "openart", "expensive"]
    # Different currencies are never compared by their numerical magnitude.
    assert prepared[4]["pools"]["USD"]["worst_case_amount"] == 0
    shot = copy.deepcopy(prepared[0][0])
    shot["renderer"] = "fal"
    assert all(x[0] == "fal" for x in prepared[3].schedule(shot, 3))
    shot["renderer"] = "an-unavailable-model"
    with pytest.raises(FilmError, match="no compatible"):
        prepared[3].schedule(shot, 3)
    # Explicit per-shot routing controls the approved fallback sequence.
    policy["shots"] = {"S001": ["openart", "fal"]}
    write(p / "render/economy.json", policy)
    estimate = approved(p)
    assert estimate["pools"]["credits"]["worst_case_amount"] == 14
    assert estimate["pools"]["USD"]["worst_case_amount"] == .1
    for unit, value in [("credits", 14), ("USD", .1)]:
        budget.reserve(p, unit, value, estimate, unit)
        with pytest.raises(FilmError, match="cap reached"):
            budget.reserve(p, unit + "extra", .01, estimate, unit)
    config = read(p / "project.yaml")
    config["budget"]["max_usd"] = 0
    write(p / "project.yaml", config)
    with pytest.raises(FilmError, match="USD exceeds budget"):
        prepare(p, mode="economy", quality="draft")


def test_quote_refresh_does_not_reset_run_but_price_or_qc_change_requires_approval(priced):
    p = priced
    original = approved(p)
    config = read(p / "render/openart_config.json")
    for quality in ("draft", "final"):
        config["shots"]["S001"][quality].update(valid_until="2099-01-01T00:00:00+00:00", cost_evidence={"renewed": True})
    write(p / "render/openart_config.json", config)
    assert prepare(p, mode="economy", quality="draft")[4]["estimate_id"] == original["estimate_id"]
    config["shots"]["S001"]["draft"]["credits"] = 8
    write(p / "render/openart_config.json", config)
    with pytest.raises(FilmError, match="approve"):
        compile_project(p, mode="economy", quality="draft")
    approved(p)
    cfg = read(p / "project.yaml")
    cfg["qc"]["threshold"] = 90
    write(p / "project.yaml", cfg)
    with pytest.raises(FilmError, match="approve"):
        compile_project(p, mode="economy", quality="draft")
    assert not (p / "render/ledger.json").exists()
    cfg["qc"]["threshold"] = float("nan")
    write(p / "project.yaml", cfg)
    with pytest.raises(FilmError, match="QC threshold"):
        prepare(p, mode="economy", quality="draft")


def test_provider_fallback_reuses_take_for_final_and_local_edit(priced, monkeypatch):
    p = priced
    source = p / "fixture_motion.mp4"
    ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=3", "-c:v", "libx264", "-threads", "1", source])
    bad_source = p / "fixture_rejected.mp4"
    ffmpeg(["-f", "lavfi", "-i", "color=c=gray:size=320x240:rate=24:duration=3", "-c:v", "libx264", "-threads", "1", bad_source])
    posts = []
    def request(self, method, url, payload=None):
        if method == "POST":
            posts.append(payload)
            return {"request_id": "fixture", "status_url": "https://queue.fal.run/fal-ai/wan/requests/fixture/status",
                "response_url": "https://queue.fal.run/fal-ai/wan/requests/fixture"}
        return {"status": "COMPLETED"} if url.endswith("status") else {"video": {"url": "https://fal.media/fixture.mp4"}}
    monkeypatch.setenv("FAL_KEY", "fixture-not-transmitted")
    monkeypatch.setattr(FalHTTP, "request", request)
    monkeypatch.setattr(FalHTTP, "download", lambda self, url, target: shutil.copyfile(source, target))
    approved(p)
    for attempt in (0, 1):
        compile_project(p, mode="economy", quality="draft")
        current = record(p)
        assert current["attempt"] == attempt and current["provider"] == "openart"
        assert status(p)["tasks"][0]["action"] == "CLAIM_THEN_SUBMIT_OPENART_ONCE"
        # Merely pending does not spend another attempt or touch fal.
        compile_project(p, mode="economy", quality="draft")
        assert not posts and record(p)["job_id"] == current["job_id"]
        job = claim_submission(p, current["job_id"])
        if attempt:
            assert "fixture identity failure" in job["arguments"]["params"]["prompt"]
        with pytest.raises(FilmError, match="already claimed"):
            claim_submission(p, current["job_id"])
        assert status(p)["tasks"][0]["action"] == "RECONCILE_DO_NOT_RESUBMIT"
        record_submission(p, current["job_id"], f"fixture-history-{attempt}")
        with pytest.raises(FilmError, match="history ID"):
            import_completed(p, current["job_id"], "wrong-history", source)
        if attempt == 0:
            import_completed(p, current["job_id"], "fixture-history-0", bad_source)
            compile_project(p, mode="economy", quality="draft")
            review = record(p)
            assert review["status"] == "NEEDS_REVIEW"
            scores = {k: 100 for k in SEMANTIC_ITEMS}
            scores["character_identity"] = 0
            save_review(p, review, scores, "fixture", "TEST: fixture identity failure")
        else:
            record_failure(p, current["job_id"], f"fixture-history-{attempt}", "fixture identity failure")
    compile_project(p, mode="economy", quality="draft")
    current = record(p)
    assert current["attempt"] == 2 and current["provider"] == "fal" and current["status"] == "NEEDS_REVIEW"
    assert len(posts) == 1 and "fixture identity failure" in posts[0]["prompt"]
    assert status(p)["reserved_in_this_project"] == {"USD": .1, "credits": 14}
    assert status(p)["tasks"][0]["action"] == "REVIEW_ANIMATION"
    save_review(p, current, {k: 100 for k in SEMANTIC_ITEMS}, "fixture", "TEST ONLY: local media fixture meets test expectations")
    assert compile_project(p, mode="economy", quality="draft")["status"] == "COMPLETE"
    # Same generation settings with a different local output size: no new request.
    approved(p, quality="final")
    monkeypatch.delenv("FAL_KEY")
    compile_project(p, mode="economy", quality="final")
    current = record(p)
    assert current["status"] == "NEEDS_REVIEW" and current["reused_take"] and len(posts) == 1
    assert current["technical"]["no_audio"]
    save_review(p, current, {k: 100 for k in SEMANTIC_ITEMS}, "fixture", "TEST ONLY: pre-trim fixture pass")
    before = current["clip_sha256"]
    with pytest.raises(FilmError, match="too short"):
        select_window(p, "S001", 2000, "invalid trim")
    select_window(p, "S001", 500, "TEST: use later moving pattern")
    compile_project(p, mode="economy", quality="final")
    edited = record(p)
    assert edited["clip_sha256"] != before and edited["status"] == "NEEDS_REVIEW"
    assert edited["source_in_ms"] == 500 and len(posts) == 1
    save_review(p, edited, {k: 100 for k in SEMANTIC_ITEMS}, "fixture", "TEST ONLY: edited fixture pass")
    assert compile_project(p, mode="economy", quality="final")["status"] == "COMPLETE"
    assert digest(p / "input/master.wav") == read(p / "project.yaml")["audio"]["sha256"]
    assert status(p)["ledger_jobs"] == 3


def test_ambiguous_submission_survives_quality_and_budget_change(priced, monkeypatch):
    p = priced
    calls = []
    def ambiguous(self, method, url, payload=None):
        calls.append(method)
        raise TimeoutError("unknown provider submission outcome")
    monkeypatch.setattr(FalHTTP, "request", ambiguous)
    monkeypatch.setenv("FAL_KEY", "fixture-not-transmitted")
    approved(p, mode="fal")
    with pytest.raises(RenderBlocked, match="reconciliation"):
        compile_project(p, mode="fal", quality="draft")
    old_job = record(p)["job_id"]
    config = read(p / "project.yaml")
    config["budget"]["max_usd"] = 2
    write(p / "project.yaml", config)
    approved(p, mode="fal", quality="final")
    with pytest.raises(RenderBlocked, match="unknown"):
        compile_project(p, mode="fal", quality="final")
    assert calls == ["POST"] and record(p)["job_id"] == old_job
    assert status(p)["ledger_jobs"] == 1


def test_exhausted_attempt_never_becomes_another_paid_generation_on_resume(priced, monkeypatch):
    p = priced
    posts = []
    def failure(self, method, url, payload=None):
        if method == "POST":
            posts.append(payload)
            return {"request_id": str(len(posts)), "status_url": "https://queue.fal.run/fal-ai/wan/requests/fixture/status",
                "response_url": "https://queue.fal.run/fal-ai/wan/requests/fixture"}
        return {"status": "COMPLETED", "error": "fixture confirmed failure"}
    monkeypatch.setattr(FalHTTP, "request", failure)
    monkeypatch.setenv("FAL_KEY", "fixture-not-transmitted")
    approved(p, mode="fal")
    for _ in range(3):
        result = compile_project(p, mode="fal", quality="draft")
        assert result["status"] == "AWAITING_REVIEW_OR_RENDER"
    assert len(posts) == 3 and status(p)["ledger_jobs"] == 3
