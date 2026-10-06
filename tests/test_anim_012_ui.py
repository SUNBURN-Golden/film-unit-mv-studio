"""ANIM-012: the live path-B section of the FRAME_ANIMATION_V1 panel.

The ANIM-011 placeholder is replaced by a real flow wired to the explicit
dev/test adapter `fake_segment` (FAKE/UNQUALIFIED) only. Driven headlessly
with streamlit.testing.v1.AppTest against app/control_panel.py:

- UI-B-READY: control inputs -> capability preflight -> quote -> separate
  cost approval -> submit -> same-identity reconcile -> returned-clip
  verification -> draft import -> DRAFT commit, all journaled.
- UI-B-INPUT-QUOTE: missing required inputs or a stale quote (price or
  input changes) are blocked before any submit; a quote is not approval.
- UI-B-UNKNOWN: a lost submit acknowledgement fences the job — no new
  job/attempt, no substitute submission, no reservation release, no
  automatic polling; only an explicit same-identity reconcile resolves it.
- UI-B-CANCEL-RACE: request vs confirmation, completion-first preservation
  and fenced unknown termination.

No real provider call, paid generation, artwork approval or release is
made or implied; the fake provider is a local file-backed double.
"""
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from engine.animation_assets import load_registry
from engine.animation_migrate import animation_init
from engine.core import read
from engine.segment_fake import configure_fake, fake_state
from engine.segment_gen import load_job_journal, segment_jobs
from test_anim_011_ui import (APPROVER, NAME, button, errors, png, sel,
                              successes, ti, uploader)
from test_compiler_v03 import fixture_project

ROOT = Path(__file__).resolve().parents[1]
CANVAS = (320, 240)
FRAMES = 24  # one second per shot at 24fps


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FILM_UNIT_PROJECTS", str(tmp_path / "projects"))
    return tmp_path


@pytest.fixture
def screen(box):
    projects = box / "projects"
    projects.mkdir()
    project = fixture_project(projects, seconds=3, shot_count=3)
    animation_init(project)
    at = AppTest.from_file(str(ROOT / "app/control_panel.py"),
                           default_timeout=180).run()
    at.sidebar.selectbox[0].select(NAME).run()
    assert not at.exception
    return at, project


def _master_pin(project):
    """The S001 CONTROL_IMAGE/LAYOUT pin imported as the master reference."""
    registry = load_registry(project)
    for asset_id, entry in registry["assets"].items():
        record = entry["revisions"][str(entry["current_revision"])]
        if record["kind"] == "CONTROL_IMAGE" \
                and record["shot_id"] == "S001" \
                and record["control_role"] == "LAYOUT" \
                and record["frame"] == 0:
            return {"asset_id": asset_id,
                    "revision": entry["current_revision"],
                    "content_sha256": record["content_sha256"]}
    raise AssertionError("no S001 layout control imported")


def _b_plan(master_pin, capabilities=None):
    return {"document_type": "animation_shot_plan", "schema_version": 1,
            "shot_id": "S001", "revision": 1, "motion_intent": "ANIMATED",
            "story_role": "fixture", "emotion_role": "fixture",
            "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
            "background": [10, 20, 30], "rig": None,
            "assets": [{**master_pin, "kind": "CONTROL_IMAGE",
                        "role": "master", "layout": None}],
            "events": [],
            "keyposes": [{"kind": "KEYPOSE", "frame": 0, "ref": None}],
            "segments": [{"start": 0, "end": FRAMES, "path": "B",
                          "capabilities": capabilities
                          or ["REFERENCE_IMAGES"]}],
            "tracks": {"camera": {"pivot": [CANVAS[0] // 2, CANVAS[1] // 2],
                                  "transform": None},
                       "layers": {}},
            "preparation": {"work": [], "creator": "fixture",
                            "reviewer": None, "revision_scope": "fixture"}}


def _control(at, name, seed, role, frame):
    """One segment-control upload through the B section."""
    uploader(at, "an_b_ctrl_file").set_value(
        (name, png(seed, CANVAS), "image/png"))
    sel(at, "an_b_ctrl_role").select(role)
    next(i for i in at.number_input if i.key == "an_b_ctrl_frame") \
        .set_value(frame)
    at.run()
    button(at, "an_b_ctrl_import").click().run()


def b_setup(at, project, capabilities=None):
    """S001 ready for path B: master pin, saved B plan, start anchor."""
    sel(at, "an_shot").select("I001")
    # The master reference is a LAYOUT control input (A-path uploader).
    uploader(at, "an_ctrl_file").set_value(
        [("master.png", png(10, CANVAS), "image/png")])
    sel(at, "an_ctrl_role").select("layout")
    at.run()
    button(at, "an_ctrl_import").click().run()
    assert any("DRAFT로 기록" in s for s in successes(at))
    uploader(at, "an_plan_file").set_value(
        [("plan.json", json.dumps(_b_plan(_master_pin(project), capabilities)
                                  ).encode(), "application/json")])
    at.run()
    button(at, "an_plan_save").click().run()
    assert any("계획을 저장" in s for s in successes(at))
    # The segment start anchor is a conditioning input (segment-control).
    _control(at, "anchor.png", 1, "keypose", 0)
    assert any("제어 입력" in s for s in successes(at))


def quote_b(at):
    button(at, "an_b_quote").click().run()


def approve_and_submit(at, approver=APPROVER, cap=10000):
    ti(at, "an_b_approver").set_value(approver)
    next(i for i in at.number_input if i.key == "an_b_cap").set_value(cap)
    next(c for c in at.checkbox if c.key == "an_b_approve").set_value(True)
    at.run()
    assert not button(at, "an_b_submit").disabled
    button(at, "an_b_submit").click().run()


def warnings(at):
    return [w.value for w in at.warning]


def only_job(project):
    jobs = segment_jobs(project)["jobs"]
    assert len(jobs) == 1
    return jobs[0]


# --- UI-B-READY --------------------------------------------------------------

def test_ui_b_ready_full_flow(screen):
    at, project = screen
    b_setup(at, project)
    # capability preflight + quote; nothing reaches the provider yet.
    quote_b(at)
    assert not at.exception
    assert any("견적" in s and "승인" in s for s in successes(at))
    assert fake_state(project)["requests"] == {}
    job_id = only_job(project)["job_id"]
    # The quote is not approval: a separate human approval gates submit.
    approve_and_submit(at)
    assert any("RUNNING" in s for s in successes(at))
    requests = fake_state(project)["requests"]
    assert len(requests) == 1                     # one request, one identity
    # Explicit same-identity reconcile -> returned clip pending verify.
    sel(at, "an_b_job").select(job_id)
    button(at, "an_b_reconcile").click().run()
    assert any("OUTPUT_PENDING_VERIFY" in s for s in successes(at))
    # Returned bytes/time verified, then staged as cut-local members.
    button(at, "an_b_import").click().run()
    assert any("VERIFIED" in s for s in successes(at))
    job = only_job(project)
    assert job["status"] == "VERIFIED"
    assert job["adapter"] == "fake_segment"
    assert job["qualification_state"] == "UNQUALIFIED"
    # The verified import commits as a DRAFT sequence pinned to the shot.
    button(at, "an_b_commit").click().run()
    assert any("DRAFT" in s for s in successes(at))
    registry = load_registry(project)
    pin = registry["assignments"]["S001"]
    record = registry["assets"][pin["asset_id"]]["revisions"][
        str(pin["revision"])]
    assert record["kind"] == "FRAME_SEQUENCE"
    assert record["acceptance"]["state"] == "DRAFT"
    assert record["provenance"]["provider_class"] == "FAKE"
    # The journal binds input/source digests, toolchain, actions, results.
    journal = load_job_journal(project, "S001", job_id)
    assert [r["event"] for r in journal] == [
        "SUBMIT_INTENT", "SUBMIT_ACCEPTED", "STATUS_QUERY",
        "STATUS_OBSERVED", "IMPORT_INTENT", "IMPORT_OBSERVED"]
    intent = journal[0]["data"]
    assert intent["snapshot"]["plan_sha256"]
    assert intent["snapshot"]["spec_sha256"]
    assert intent["toolchain"]["id"] == "fake_segment"
    assert intent["quote"]["amount"] > 0
    # The journal expander is rendered with the adapter version caption.
    assert any(e.label.startswith("job journal") for e in at.expander)
    assert any("fake_segment_v1" in c.value for c in at.caption)


# --- UI-B-INPUT-QUOTE ---------------------------------------------------------

def test_ui_b_missing_input_blocks_before_submit(screen):
    at, project = screen
    # POSE_GUIDE declared but no pose control exists in the segment.
    b_setup(at, project, capabilities=["REFERENCE_IMAGES", "POSE_GUIDE"])
    quote_b(at)
    assert not at.exception
    assert any("NEEDS_MANUAL_WORK" in e and "pose" in e for e in errors(at))
    # No job, no provider request — blocked at preflight, before submit.
    assert segment_jobs(project)["jobs"] == []
    assert fake_state(project)["requests"] == {}


def test_ui_b_stale_quote_and_changed_inputs_block_submit(screen):
    at, project = screen
    b_setup(at, project)
    quote_b(at)
    assert any("견적" in s for s in successes(at))
    job_id = only_job(project)["job_id"]
    # The provider's price changed after the quote: the approved id is stale.
    configure_fake(project, price_credits_per_frame=9)
    approve_and_submit(at)
    assert not at.exception
    assert any("QUOTE_CHANGED" in e for e in errors(at))
    assert fake_state(project)["requests"] == {}
    job = only_job(project)
    assert job["job_id"] == job_id and job["status"] == "PLANNED"
    # Inputs changed after quoting (a pose control appears mid-segment):
    # the stored quote binds the old input identity and is refused on
    # submit — the job key changes with the inputs.
    configure_fake(project, price_credits_per_frame=2)
    _control(at, "pose.png", 6, "pose", 10)
    assert any("제어 입력" in s for s in successes(at))
    button(at, "an_b_submit").click().run()
    assert any("QUOTE_CHANGED" in e for e in errors(at))
    assert fake_state(project)["requests"] == {}
    # Refused submits mint no provider request; the refused submit left its
    # PLANNED job record for audit but nothing reached the fake provider.
    assert len(segment_jobs(project)["jobs"]) == 2
    assert all(j["status"] == "PLANNED"
               for j in segment_jobs(project)["jobs"])


def test_ui_b_quote_is_not_approval(screen):
    at, project = screen
    b_setup(at, project)
    quote_b(at)
    # Without the separate approval checkbox + approver the submit stays
    # disabled — a quote never implies consent.
    assert button(at, "an_b_submit").disabled
    ti(at, "an_b_approver").set_value(APPROVER)
    at.run()
    assert button(at, "an_b_submit").disabled   # checkbox still unchecked
    assert fake_state(project)["requests"] == {}


# --- UI-B-UNKNOWN -------------------------------------------------------------

def test_ui_b_unknown_fences_until_explicit_reconcile(screen):
    at, project = screen
    b_setup(at, project)
    configure_fake(project, behaviors={"lost_ack": True,
                                       "lost_ack_delivered": True})
    quote_b(at)
    approve_and_submit(at)
    # The acknowledgement was lost: UNKNOWN is shown, not silently retried.
    assert any("UNKNOWN" in w for w in warnings(at))
    job = only_job(project)
    assert job["status"] == "UNKNOWN"
    request_id = job["request_id"]
    record = fake_state(project)["requests"][request_id]
    assert record["queries"] == 0   # no automatic polling happened
    # Resubmission is fenced at the engine and surfaced as an error.
    button(at, "an_b_submit").click().run()
    assert any("JOB_FENCED" in e for e in errors(at))
    assert len(fake_state(project)["requests"]) == 1
    assert len(segment_jobs(project)["jobs"]) == 1
    job = only_job(project)
    assert job["request_id"] == request_id and job["attempt"] == 1
    # The reservation is held — never auto-released while UNKNOWN.
    ledger = read(project / "render/ledger.json")
    assert any(k.startswith(job["job_id"]) for k in ledger["jobs"])
    # The explicit same-identity reconcile is the only way forward.
    sel(at, "an_b_job").select(job["job_id"])
    button(at, "an_b_reconcile").click().run()
    assert any("OUTPUT_PENDING_VERIFY" in s for s in successes(at))
    record = fake_state(project)["requests"][request_id]
    assert record["queries"] == 1   # exactly the user's explicit query
    journal = load_job_journal(project, "S001", job["job_id"])
    assert [r["event"] for r in journal][:3] == [
        "SUBMIT_INTENT", "SUBMIT_LOST", "STATUS_QUERY"]


# --- UI-B-CANCEL-RACE ---------------------------------------------------------

def test_ui_b_cancel_race_completion_confirmed_first(screen):
    at, project = screen
    b_setup(at, project)
    configure_fake(project, behaviors={"complete_before_cancel": True})
    quote_b(at)
    approve_and_submit(at)
    job_id = only_job(project)["job_id"]
    sel(at, "an_b_job").select(job_id)
    button(at, "an_b_cancel").click().run()
    # 완료가 먼저 확정: the returned clip is kept for verification.
    assert any("OUTPUT_PENDING_VERIFY" in s for s in successes(at))
    button(at, "an_b_import").click().run()
    assert any("VERIFIED" in s for s in successes(at))
    assert only_job(project)["status"] == "VERIFIED"
    journal = load_job_journal(project, "S001", job_id)
    observed = [r["data"]["outcome"] for r in journal
                if r["event"] == "CANCEL_OBSERVED"]
    assert observed == ["COMPLETED_FIRST"]


def test_ui_b_cancel_confirmed_then_fenced_terminal(screen):
    at, project = screen
    b_setup(at, project)
    quote_b(at)
    approve_and_submit(at)
    job_id = only_job(project)["job_id"]
    sel(at, "an_b_job").select(job_id)
    button(at, "an_b_cancel").click().run()
    assert any("CANCEL_CONFIRMED" in s for s in successes(at))
    # Confirmed terminal: no resubmission under the same input identity.
    button(at, "an_b_submit").click().run()
    assert any("JOB_TERMINATED" in e for e in errors(at))
    assert len(fake_state(project)["requests"]) == 1
    # The cancel was never reported as a refund: the reservation stays in
    # the ledger and the charge reflects the provider's report.
    ledger = read(project / "render/ledger.json")
    assert any(k.startswith(job_id) for k in ledger["jobs"])
    assert only_job(project)["charge_state"] == "CONFIRMED_BILLED"


def test_ui_b_lost_cancel_answer_fences_unknown(screen):
    at, project = screen
    b_setup(at, project)
    configure_fake(project, behaviors={"lost_cancel_ack": True})
    quote_b(at)
    approve_and_submit(at)
    job_id = only_job(project)["job_id"]
    sel(at, "an_b_job").select(job_id)
    button(at, "an_b_cancel").click().run()
    # The cancel's acknowledgement never arrived: termination is UNKNOWN.
    assert any("UNKNOWN" in w for w in warnings(at))
    assert only_job(project)["status"] == "UNKNOWN"
    # No substitute submission, no second cancel while ambiguous.
    button(at, "an_b_submit").click().run()
    assert any("JOB_FENCED" in e for e in errors(at))
    button(at, "an_b_cancel").click().run()
    assert any("JOB_FENCED" in e for e in errors(at))
    button(at, "an_b_reconcile").click().run()
    assert any("CANCEL_CONFIRMED" in s for s in successes(at))
    assert len(fake_state(project)["requests"]) == 1
    assert len(segment_jobs(project)["jobs"]) == 1
