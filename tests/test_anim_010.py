"""ANIM-010: path-B conditional segment generation — capability preflight,
segment/endpoint rules, quote/reservation/ledger ordering, UNKNOWN fencing
and source-PTS mapping, driven end to end by the deterministic FAKE provider.

Everything here is local protocol evidence only: the provider double in
engine/segment_fake.py renders real changing PNGs but is labelled
FAKE/UNQUALIFIED — no real provider call, no paid generation, no artwork
approval and no production authorization is created or implied.
"""
import json
import os
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image

from engine import cli
from engine.animation_assets import (import_draft_image, import_layer_rgba,
                                     import_mask, load_registry,
                                     resolve_shot_sequence)
from engine.animation_schema import read_canon, write_canon
from engine.compositor import composite_shot
from engine.core import FilmError, read, write
from engine.frame_sequence import (animation_validate,
                                   normalize_shot_sequence)
from engine.motion_plan import load_shot_plan, save_shot_plan
from engine.segment_fake import configure_fake, fake_state, make_adapter
from engine.segment_gen import (JOURNAL_TYPE, TRANSITIONS,
                                commit_segment_sequence,
                                import_segment_control, load_job_journal,
                                segment_import, segment_jobs, segment_quote,
                                segment_reconcile, segment_retry,
                                segment_submit, validate_job_journal)
from test_anim_008 import (_mask_half, _sprite, c_project)
from test_compiler_v03 import fixture_project

W, H, FRAMES = 64, 48, 24
OUT_FPS, SRC_FPS = 24, 30
NEEDED = int(Fraction(FRAMES - 1) * Fraction(SRC_FPS, OUT_FPS)) + 1  # 29


def _png(path, seed):
    """A real 64x48 PNG; every seed produces different pixels and bytes."""
    im = Image.new("RGB", (W, H))
    px = im.load()
    for y in range(H):
        for x in range(W):
            px[x, y] = ((x * 5 + seed * 29) % 256,
                        (y * 7 + seed * 11) % 256,
                        (x + y + seed * 3) % 256)
    im.save(path)
    return path


def _sub(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path


def _pin(result):
    if "asset_pin" in result:
        return dict(result["asset_pin"])
    return {"asset_id": result["asset_id"], "revision": result["revision"],
            "content_sha256": result["content_sha256"]}


def _asset(pin, kind, role):
    return {**pin, "kind": kind, "role": role, "layout": None}


def b_plan(master_pins, capabilities=None, segments=None, assets_extra=None):
    """A pure-B shot plan over [0, 24): rig-free, track-free by contract."""
    capabilities = ["REFERENCE_IMAGES"] if capabilities is None \
        else capabilities
    segments = segments or [{"start": 0, "end": FRAMES, "path": "B",
                             "capabilities": capabilities}]
    return {"document_type": "animation_shot_plan", "schema_version": 1,
            "shot_id": "S001", "revision": 1, "motion_intent": "ANIMATED",
            "story_role": "synthetic fixture", "emotion_role": "synthetic",
            "canvas": {"width": W, "height": H}, "background": [10, 20, 30],
            "rig": None,
            "assets": [_asset(pin, "CONTROL_IMAGE", "master")
                       for pin in master_pins] + list(assets_extra or []),
            "events": [], "keyposes": [],
            "segments": segments,
            "tracks": {"camera": {"pivot": [W // 2, H // 2],
                                  "transform": None},
                       "layers": {}},
            "preparation": {"work": [], "creator": "synthetic fixture",
                            "reviewer": None, "revision_scope": "fixture"}}


def b_scene(tmp_path, *, capabilities=None, masters=1, end_anchor=False):
    """Project + master reference(s) + saved B plan + start anchor control.

    With `end_anchor` the plan forces START_END_IMAGES and the landing
    keypose is imported at frame == length (the only out-of-range control a
    B segment may own).
    """
    p = c_project(tmp_path)
    pins = []
    for index in range(masters):
        pin = _pin(import_draft_image(
            p, "S001", _png(tmp_path / f"master{index}.png", 10 + index),
            role="layout", frame=0))
        pins.append(pin)
    caps = list(capabilities) if capabilities is not None \
        else ["REFERENCE_IMAGES"]
    if end_anchor and "START_END_IMAGES" not in caps:
        caps.append("START_END_IMAGES")
    save_shot_plan(p, b_plan(pins, capabilities=caps))
    anchor = import_segment_control(p, "S001", _png(tmp_path / "start.png", 1),
                                    role="keypose", frame=0)
    end_pin = None
    if end_anchor:
        end_pin = _pin(import_segment_control(
            p, "S001", _png(tmp_path / "end.png", 2), role="keypose",
            frame=FRAMES))
    return p, pins, _pin(anchor), end_pin


def _submit(p, start=0, end=FRAMES, quote=None, **kwargs):
    quote = quote or segment_quote(p, "S001", start, end)
    return segment_submit(p, "S001", start, end,
                          approver="synthetic tester",
                          quote_id=quote["quote_id"],
                          cap=kwargs.pop("cap", 10000), **kwargs)


def _run_to_verified(p, start=0, end=FRAMES, queries=1, **kwargs):
    sub = _submit(p, start, end, **kwargs)
    rec = None
    for _ in range(queries):
        rec = segment_reconcile(p, sub["job_id"])
    assert rec["status"] == "OUTPUT_PENDING_VERIFY"
    return sub["job_id"]


def _job_record(p, job_id):
    return read(p / "animation/segment_jobs/S001" / f"{job_id}.json")


# --- capability preflight ----------------------------------------------------

def test_preflight_success_declares_adapter_and_spec(tmp_path):
    p, masters, anchor, _ = b_scene(tmp_path)
    result = segment_quote(p, "S001", 0, FRAMES)
    assert result["status"] == "PLANNED"
    assert result["unit"] == "credits" and result["amount"] == NEEDED * 2
    assert result["provider_class"] == "FAKE"
    assert result["qualification_state"] == "UNQUALIFIED"
    # The durable job record exists before any submission.
    jobs = segment_jobs(p)["jobs"]
    assert [j["job_id"] for j in jobs] == [result["job_id"]]
    assert jobs[0]["status"] == "PLANNED" and jobs[0]["request_id"] is None
    # The adapter declaration is the honest capability envelope.
    caps = make_adapter(p, "fake_segment").capabilities()
    for key in ("reference_images_max", "pose_guide", "layout_guide",
                "mask_region", "start_image", "end_image",
                "returned_endpoint_rule", "alpha_output", "native_width",
                "native_height", "native_fps", "min_returned_frames",
                "max_returned_frames", "operation_identity", "cost_unit"):
        assert key in caps
    assert caps["provider_class"] == "FAKE"
    assert caps["qualification_state"] == "UNQUALIFIED"


def test_missing_required_inputs_refused(tmp_path):
    # No master pin while REFERENCE_IMAGES is declared.
    p = c_project(_sub(tmp_path, "m1"))
    save_shot_plan(p, b_plan([], capabilities=["REFERENCE_IMAGES"]))
    import_segment_control(p, "S001", _png(tmp_path / "m1/s.png", 1),
                           role="keypose", frame=0)
    with pytest.raises(FilmError, match="NEEDS_MANUAL_WORK"):
        segment_quote(p, "S001", 0, FRAMES)
    # No start anchor control at the segment start.
    p2 = c_project(_sub(tmp_path, "m2"))
    master = _pin(import_draft_image(
        p2, "S001", _png(tmp_path / "m2/m.png", 3), role="layout", frame=0))
    save_shot_plan(p2, b_plan([master]))
    with pytest.raises(FilmError, match="NEEDS_MANUAL_WORK"):
        segment_quote(p2, "S001", 0, FRAMES)
    # POSE_GUIDE declared but no pose control exists in the segment.
    p3, _, _, _ = b_scene(_sub(tmp_path, "m3"),
                          capabilities=["REFERENCE_IMAGES", "POSE_GUIDE"])
    with pytest.raises(FilmError, match="NEEDS_MANUAL_WORK"):
        segment_quote(p3, "S001", 0, FRAMES)
    # START_END_IMAGES declared but no end anchor pinned.
    p4 = c_project(_sub(tmp_path, "m4"))
    master4 = _pin(import_draft_image(
        p4, "S001", _png(tmp_path / "m4/m.png", 4), role="layout", frame=0))
    save_shot_plan(p4, b_plan([master4],
                            capabilities=["REFERENCE_IMAGES",
                                          "START_END_IMAGES"]))
    import_segment_control(p4, "S001", _png(tmp_path / "m4/s.png", 5),
                           role="keypose", frame=0)
    with pytest.raises(FilmError, match="NEEDS_MANUAL_WORK"):
        segment_quote(p4, "S001", 0, FRAMES)


def test_unsupported_inputs_fail_capability_unavailable(tmp_path):
    # Provider accepts no pose guide but the segment declares one.
    p, _, _, _ = b_scene(_sub(tmp_path, "a"),
                         capabilities=["REFERENCE_IMAGES", "POSE_GUIDE"])
    import_segment_control(p, "S001", _png(tmp_path / "a/pose.png", 6),
                           role="pose", frame=6)
    configure_fake(p, guides={"pose": "NONE"})
    with pytest.raises(FilmError, match="CAPABILITY_UNAVAILABLE"):
        segment_quote(p, "S001", 0, FRAMES)
    # Provider accepts no mask/region input but the plan carries a mask pin.
    p2, masters2, _, _ = b_scene(_sub(tmp_path, "b"),
                                 capabilities=["REFERENCE_IMAGES",
                                               "MASK_REGION"])
    layer = import_layer_rgba(p2, _sprite(tmp_path / "b/layer.png"),
                              pivot=[8, 8], z_order=0)
    mask = import_mask(p2, _mask_half(tmp_path / "b/mask.png"),
                       target=_pin(layer))
    plan = load_shot_plan(p2, "S001")
    plan["assets"].append(_asset(_pin(mask), "MASK", "mask"))
    save_shot_plan(p2, plan)
    configure_fake(p2, guides={"mask_region": False})
    with pytest.raises(FilmError, match="CAPABILITY_UNAVAILABLE"):
        segment_quote(p2, "S001", 0, FRAMES)
    # A declared alpha requirement fails on a DROPPED-alpha adapter.
    p3, _, _, _ = b_scene(_sub(tmp_path, "c"),
                          capabilities=["REFERENCE_IMAGES", "ALPHA_OUTPUT"])
    with pytest.raises(FilmError, match="CAPABILITY_UNAVAILABLE"):
        segment_quote(p3, "S001", 0, FRAMES)


def test_required_references_are_never_trimmed(tmp_path):
    p, _, _, _ = b_scene(tmp_path, masters=3)
    configure_fake(p, limits={"max_reference_images": 2})
    with pytest.raises(FilmError, match="never trimmed"):
        segment_quote(p, "S001", 0, FRAMES)
    # Nothing reached the fake provider: no request was recorded.
    assert fake_state(p)["requests"] == {}


def test_start_only_adapter_cannot_serve_end_anchor(tmp_path):
    # The fake provider configured start-only.
    p, _, _, _ = b_scene(_sub(tmp_path, "fake"), end_anchor=True)
    configure_fake(p, endpoints={"end_image": False})
    with pytest.raises(FilmError, match="start-only"):
        segment_quote(p, "S001", 0, FRAMES)
    # The declared Gemini video adapter is start-image-only: the end
    # condition is never silently dropped, the plan cannot be served.
    p2, _, _, _ = b_scene(_sub(tmp_path, "gem"), end_anchor=True)
    with pytest.raises(FilmError, match="start-only"):
        segment_quote(p2, "S001", 0, FRAMES, adapter_id="gemini_video")
    gem = make_adapter(p2, "gemini_video")
    assert gem.capabilities()["end_image"] is False
    assert gem.capabilities()["returned_endpoint_rule"] == "START_ONLY"
    with pytest.raises(FilmError, match="UNQUALIFIED"):
        gem.quote({"segment": {"start": 0, "end": 1}})


def test_segment_range_must_match_the_stored_plan(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    with pytest.raises(FilmError, match="SEGMENT_NOT_IN_PLAN"):
        segment_quote(p, "S001", 0, FRAMES - 1)
    with pytest.raises(FilmError, match="SEGMENT_NOT_IN_PLAN"):
        segment_quote(p, "S001", 1, FRAMES)


# --- quote, reservation, journal ordering -------------------------------------

def test_quote_before_submit_and_changed_quote_reapproval(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    # A guessed/stale quote id is not an approval of the live quote.
    with pytest.raises(FilmError, match="QUOTE_CHANGED"):
        segment_submit(p, "S001", 0, FRAMES, approver="t",
                       quote_id="0" * 64, cap=10000)
    quote = segment_quote(p, "S001", 0, FRAMES)
    # A provider-side price change voids the approved quote id.
    configure_fake(p, price_credits_per_frame=5)
    with pytest.raises(FilmError, match="QUOTE_CHANGED"):
        _submit(p, quote=quote)
    refreshed = segment_quote(p, "S001", 0, FRAMES)
    assert refreshed["amount"] == NEEDED * 5
    result = _submit(p, quote=refreshed)
    assert result["status"] == "RUNNING"
    assert result["quote_id"] == refreshed["quote_id"]


def test_approval_cap_is_checked(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    quote = segment_quote(p, "S001", 0, FRAMES)
    with pytest.raises(FilmError, match="APPROVAL_CAP"):
        segment_submit(p, "S001", 0, FRAMES, approver="t",
                       quote_id=quote["quote_id"], cap=1)
    job = _job_record(p, segment_jobs(p)["jobs"][0]["job_id"])
    assert job["status"] == "PLANNED"  # cap refusal left no submission


def test_reservation_and_journal_precede_submit(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    quote = segment_quote(p, "S001", 0, FRAMES)
    ledger = read(p / "render/ledger.json", {"jobs": {}})
    assert ledger["jobs"] == {}  # nothing reserved before an approved submit
    result = _submit(p, quote=quote)
    ledger = read(p / "render/ledger.json")
    key = result["reservation"]["ledger_key"]
    assert key in ledger["jobs"]
    assert ledger["jobs"][key]["reserved_amount"] == quote["amount"]
    # The job journal's first record is the submit intent — before the
    # provider observed anything — and every record validates.
    journal = load_job_journal(p, "S001", result["job_id"])
    assert [r["document_type"] for r in journal] == [JOURNAL_TYPE] * len(journal)
    assert journal[0]["event"] == "SUBMIT_INTENT"
    assert journal[-1]["event"] == "SUBMIT_ACCEPTED"
    job = _job_record(p, result["job_id"])
    assert job["attempt_id"] and job["request_id"]


def test_same_input_resume_reuses_job_and_request(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    first = _submit(p)
    again = _submit(p)
    assert again["resumed"] is True
    assert again["job_id"] == first["job_id"]
    assert again["request_id"] == first["request_id"]
    # One submission identity server-side; no duplicate request.
    assert len(fake_state(p)["requests"]) == 1
    journal = load_job_journal(p, "S001", first["job_id"])
    assert [r["event"] for r in journal].count("SUBMIT_INTENT") == 1


def test_retry_is_a_cost_cap_not_permission(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    quote = segment_quote(p, "S001", 0, FRAMES)
    configure_fake(p, behaviors={"reject": True})
    result = _submit(p, quote=quote)  # definitive refusal -> FAILED_CONFIRMED
    assert result["status"] == "FAILED_CONFIRMED"
    # max_retries=0 bounds the job to its first attempt.
    with pytest.raises(FilmError, match="RETRY_CAP"):
        segment_retry(p, result["job_id"], approver="t",
                      quote_id=segment_quote(p, "S001", 0, FRAMES)
                      ["quote_id"])


# --- UNKNOWN fencing -----------------------------------------------------------

def test_lost_ack_fences_resubmission_then_reconciles(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    configure_fake(p, behaviors={"lost_ack": True,
                                 "lost_ack_delivered": True})
    result = _submit(p)
    assert result["status"] == "UNKNOWN"
    assert result["request_id"]
    # Same input, same job: resubmission is fenced, never a new request id.
    with pytest.raises(FilmError, match="JOB_FENCED"):
        _submit(p)
    assert len(fake_state(p)["requests"]) == 1
    # Explicit reconciliation on the same identity resolves it.
    rec = segment_reconcile(p, result["job_id"])
    assert rec["status"] == "OUTPUT_PENDING_VERIFY"
    assert rec["operation_id"]
    job = _job_record(p, result["job_id"])
    assert job["request_id"] == result["request_id"]
    events = [r["event"] for r in
              load_job_journal(p, "S001", result["job_id"])]
    assert "SUBMIT_LOST" in events and "STATUS_OBSERVED" in events


def test_lost_ack_never_received_confirms_failure_conservatively(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    configure_fake(p, behaviors={"lost_ack": True,
                                 "lost_ack_delivered": False})
    result = _submit(p, max_retries=1)
    assert result["status"] == "UNKNOWN"
    rec = segment_reconcile(p, result["job_id"])
    assert rec["status"] == "FAILED_CONFIRMED"
    assert rec["charge_state"] == "CONFIRMED_UNCHARGED"
    # The reservation is still held: never silently refunded.
    ledger = read(p / "render/ledger.json")
    assert result["reservation"]["ledger_key"] in ledger["jobs"]
    # An explicit reviewed retry mints a new attempt after the confirmed
    # failure — bounded by the approved retry cap.
    configure_fake(p, behaviors={"lost_ack": False})
    quote = segment_quote(p, "S001", 0, FRAMES)
    retry = segment_retry(p, result["job_id"], approver="reviewer",
                          quote_id=quote["quote_id"])
    assert retry["attempt"] == 2
    assert retry["request_id"] != result["request_id"]
    assert retry["status"] == "RUNNING"
    assert len(read(p / "render/ledger.json")["jobs"]) == 2


def test_late_completion_needs_explicit_queries(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    configure_fake(p, behaviors={"complete_after": 3})
    result = _submit(p)
    assert segment_reconcile(p, result["job_id"])["status"] == "RUNNING"
    assert segment_reconcile(p, result["job_id"])["status"] == "RUNNING"
    assert segment_reconcile(p, result["job_id"])["status"] == \
        "OUTPUT_PENDING_VERIFY"
    assert len(fake_state(p)["requests"]) == 1


# --- returned-clip verification, PTS mapping, commit ---------------------------

def test_import_maps_30fps_source_pts_exactly(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    job_id = _run_to_verified(p)
    result = segment_import(p, job_id)
    norm = result["normalization"]
    assert norm["rule"] == "FLOOR_CONTAINMENT_V1"
    assert norm["source_fps"] == SRC_FPS and norm["output_fps"] == OUT_FPS
    expected = [int(Fraction(i) * Fraction(SRC_FPS, OUT_FPS))
                for i in range(FRAMES)]
    assert norm["mapping"] == expected
    assert norm["endpoint_rule"] == "START_ONLY"
    assert norm["dropped_end_frame"] is None
    assert result["member_range"] == [0, FRAMES]
    # Same-fps sources map identically.
    p2, _, _, _ = b_scene(_sub(tmp_path, "same"))
    configure_fake(p2, native={"fps": OUT_FPS})
    job2 = _run_to_verified(p2)
    norm2 = segment_import(p2, job2)["normalization"]
    assert norm2["mapping"] == list(range(FRAMES))


def test_end_anchor_endpoint_rule_is_recorded(tmp_path):
    p, _, _, _ = b_scene(tmp_path, end_anchor=True)
    job_id = _run_to_verified(p)
    # 31 returned frames (30 covering the open interval + the end anchor);
    # the clip is longer than needed, so the used range is explicit.
    with pytest.raises(FilmError, match="CLIP_LONGER_THAN_NEEDED"):
        segment_import(p, job_id)
    job = _job_record(p, job_id)
    assert job["status"] == "OUTPUT_PENDING_VERIFY"  # not failed by refusal
    result = segment_import(p, job_id, source_start=0,
                            source_count=NEEDED + 1)
    norm = result["normalization"]
    assert norm["endpoint_rule"] == "START_END_INCLUDED"
    assert norm["dropped_end_frame"] == norm["returned_frames"] - 1
    assert len(norm["mapping"]) == FRAMES


def test_short_returned_clip_fails_the_job(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    configure_fake(p, behaviors={"short_clip": True})
    job_id = _run_to_verified(p)
    with pytest.raises(FilmError, match="SHORT_RETURNED_CLIP"):
        segment_import(p, job_id)
    job = _job_record(p, job_id)
    assert job["status"] == "FAILED_CONFIRMED"
    assert job["failure"]["reason"] == "SHORT_RETURNED_CLIP"


def test_long_clip_requires_explicit_used_range(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    configure_fake(p, behaviors={"extra_frames": 2})
    job_id = _run_to_verified(p)
    with pytest.raises(FilmError, match="CLIP_LONGER_THAN_NEEDED"):
        segment_import(p, job_id)
    with pytest.raises(FilmError, match="USED_RANGE_INVALID"):
        segment_import(p, job_id, source_start=0, source_count=NEEDED - 1)
    result = segment_import(p, job_id, source_start=1,
                            source_count=NEEDED)
    assert result["status"] == "VERIFIED"
    norm = result["normalization"]
    assert norm["source_start"] == 1 and norm["source_count"] == NEEDED
    assert norm["mapping"][0] == 1


def test_import_gates_and_distinct_frames(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    sub = _submit(p)
    with pytest.raises(FilmError, match="NOT_READY_TO_IMPORT"):
        segment_import(p, sub["job_id"])
    segment_reconcile(p, sub["job_id"])
    result = segment_import(p, sub["job_id"])
    assert result["state"] == "DRAFT"
    assert result["qualification_state"] == "UNQUALIFIED"
    commit_segment_sequence(p, "S001")
    # The fake emits real PNGs whose bytes change per frame.
    resolved = resolve_shot_sequence(p, "S001", [0, FRAMES],
                                     {"before": 0, "after": 0})
    hashes = {f["sha256"] for f in resolved["record"]["files"]}
    assert len(resolved["members"]) == FRAMES
    assert len(hashes) == FRAMES  # every member's pixels differ
    for f in resolved["record"]["files"]:
        Image.open(p / f["relative_name"]).load()  # real decodable PNGs


def test_commit_assembles_draft_sequence_and_normalizes(tmp_path):
    p, masters, anchor, _ = b_scene(tmp_path)
    with pytest.raises(FilmError, match="SEGMENT_NOT_PRODUCED"):
        commit_segment_sequence(p, "S001")
    job_id = _run_to_verified(p)
    segment_import(p, job_id)
    result = commit_segment_sequence(p, "S001")
    assert result["state"] == "DRAFT"
    assert result["frames"] == FRAMES
    assert result["qualification_state"] == "UNQUALIFIED"
    registry = load_registry(p)
    record = registry["assets"][result["asset_id"]]["revisions"]["1"]
    assert record["kind"] == "FRAME_SEQUENCE"
    assert record["acceptance"]["state"] == "DRAFT"
    assert record["provenance"]["provider_class"] == "FAKE"
    assert record["provenance"]["jobs"] == [job_id]
    assert record["provenance"]["source_maps"][0]["normalization"]["rule"] \
        == "FLOOR_CONTAINMENT_V1"
    # The conditioning pins are recorded dependencies.
    dep_ids = {d["asset_id"] for d in record["dependencies"]}
    assert masters[0]["asset_id"] in dep_ids
    assert anchor["asset_id"] in dep_ids
    # The timeline adopted the revision and the existing normalize/preview
    # machinery consumes it like any FRAME_SEQUENCE.
    timeline = read_canon(p / "timeline/edit.json")
    assert timeline["entries"][0]["sequence_revision"] == result["revision"]
    norm = normalize_shot_sequence(p, "S001")
    assert norm["state"] == "DRAFT"
    assert [row["member"] for row in norm["entries"][0]["member_map"]] \
        == list(range(FRAMES))
    assert animation_validate(p)["ok"] is True


def test_two_segment_commit_covers_each_once(tmp_path):
    p = c_project(tmp_path)
    master = _pin(import_draft_image(p, "S001", _png(tmp_path / "m.png", 9),
                                     role="layout", frame=0))
    segments = [{"start": 0, "end": 12, "path": "B",
                 "capabilities": ["REFERENCE_IMAGES"]},
                {"start": 12, "end": FRAMES, "path": "B",
                 "capabilities": ["REFERENCE_IMAGES"]}]
    save_shot_plan(p, b_plan([master], segments=segments))
    import_segment_control(p, "S001", _png(tmp_path / "a0.png", 1),
                           role="keypose", frame=0)
    import_segment_control(p, "S001", _png(tmp_path / "a1.png", 2),
                           role="keypose", frame=12)
    for start, end in ((0, 12), (12, FRAMES)):
        job = _run_to_verified(p, start, end)
        segment_import(p, job)
    result = commit_segment_sequence(p, "S001")
    assert result["frames"] == FRAMES
    # The shared anchor at frame 12 is produced once, by the next segment.
    registry = load_registry(p)
    record = registry["assets"][result["asset_id"]]["revisions"]["1"]
    assert [f["frame_index"] for f in record["files"]] == list(range(FRAMES))
    jobs = segment_jobs(p, "S001")["jobs"]
    assert {(j["segment"]["start"], j["segment"]["end"]) for j in jobs} == \
        {(0, 12), (12, FRAMES)}


def test_stale_plan_jobs_do_not_count(tmp_path):
    p, masters, anchor, _ = b_scene(tmp_path)
    job_id = _run_to_verified(p)
    segment_import(p, job_id)
    # Re-saving a changed plan invalidates jobs bound to the old digest.
    plan = load_shot_plan(p, "S001")
    plan["revision"] = 2
    plan["story_role"] = "changed synthetic fixture"
    save_shot_plan(p, plan)
    with pytest.raises(FilmError, match="SEGMENT_NOT_PRODUCED"):
        commit_segment_sequence(p, "S001")


def test_commit_refuses_stale_inputs_and_commits_new_job(tmp_path):
    p, _, anchor, _ = b_scene(tmp_path)
    old_job = _run_to_verified(p)
    segment_import(p, old_job)
    # Revising the start anchor on the same asset id re-pins the segment
    # input; the older VERIFIED job's key no longer matches the current
    # spec, so it must not be assembled even when it is the only job.
    revised = import_segment_control(
        p, "S001", _png(tmp_path / "start2.png", 41), role="keypose",
        frame=0, asset_id=anchor["asset_id"])
    assert revised["asset_pin"]["asset_id"] == anchor["asset_id"]
    assert revised["asset_pin"]["revision"] == anchor["revision"] + 1
    with pytest.raises(FilmError, match="SEGMENT_STALE_INPUTS") as exc:
        commit_segment_sequence(p, "S001")
    assert anchor["asset_id"] in str(exc.value)
    # A job produced for the new pins supplies the committed frames.
    new_job = _run_to_verified(p)
    assert new_job != old_job
    segment_import(p, new_job)
    result = commit_segment_sequence(p, "S001")
    registry = load_registry(p)
    record = registry["assets"][result["asset_id"]]["revisions"][
        str(result["revision"])]
    staged = {m["member_index"]: m["sha256"]
              for m in _job_record(p, new_job)["import"]["members"]}
    assert [f["sha256"] for f in record["files"]] == \
        [staged[i] for i in range(FRAMES)]
    # Provenance lists only the job that actually supplied frames.
    assert record["provenance"]["jobs"] == [new_job]
    assert {m["job_id"] for m in record["provenance"]["source_maps"]} == \
        {new_job}


def test_commit_with_used_range_offset_and_handles(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    timeline = read_canon(p / "timeline/edit.json")
    timeline["entries"][0]["used_source_range"] = [4, 4 + FRAMES]
    timeline["entries"][0]["unused_handles"] = {"before": 2, "after": 3}
    write_canon(p / "timeline/edit.json", timeline)
    job_id = _run_to_verified(p)
    segment_import(p, job_id)
    # Members stage at absolute indices [4, 28); the lead-in and declared
    # handle positions reuse the nearest staged member, so commit does not
    # fail with MISSING_FRAMES.
    result = commit_segment_sequence(p, "S001")
    assert result["frames"] == 4 + FRAMES + 3
    record = load_registry(p)["assets"][result["asset_id"]]["revisions"][
        str(result["revision"])]
    staged = {m["member_index"]: m["sha256"]
              for m in _job_record(p, job_id)["import"]["members"]}
    expected = ([staged[4]] * 4
                + [staged[i] for i in range(4, 4 + FRAMES)]
                + [staged[4 + FRAMES - 1]] * 3)
    assert [f["sha256"] for f in record["files"]] == expected
    norm = normalize_shot_sequence(p, "S001")
    assert [row["member"] for row in norm["entries"][0]["member_map"]] == \
        list(range(4, 4 + FRAMES))
    assert animation_validate(p)["ok"] is True


def test_submit_reject_and_retry_use_allowed_edges(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    quote = segment_quote(p, "S001", 0, FRAMES)
    configure_fake(p, behaviors={"reject": True})
    result = _submit(p, quote=quote, max_retries=1)
    assert result["status"] == "FAILED_CONFIRMED"
    job = _job_record(p, result["job_id"])
    edges = [(h["from"], h["to"]) for h in job["history"]]
    # A definite reject has no SUBMITTING -> FAILED_CONFIRMED edge in the
    # schema-13 table; it goes through UNKNOWN -> FAILED_CONFIRMED.
    assert edges[-2:] == [("SUBMITTING", "UNKNOWN"),
                          ("UNKNOWN", "FAILED_CONFIRMED")]
    # An explicit continuation takes the journaled edge back to RESERVED.
    configure_fake(p, behaviors={"reject": False})
    quote = segment_quote(p, "S001", 0, FRAMES)
    retry = segment_retry(p, result["job_id"], approver="reviewer",
                          quote_id=quote["quote_id"])
    assert retry["status"] == "RUNNING" and retry["attempt"] == 2
    job = _job_record(p, result["job_id"])
    assert ("FAILED_CONFIRMED", "RESERVED") in \
        [(h["from"], h["to"]) for h in job["history"]]
    for h in job["history"]:
        assert h["to"] in TRANSITIONS[h["from"]]


def test_segment_cap_and_project_budget_are_separate(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    # --cap approves the segment quote; the ledger-wide limit is the
    # project budget, not the per-segment cap.
    config = read(p / "project.yaml")
    config["budget"]["max_credits"] = 10
    write(p / "project.yaml", config)
    quote = segment_quote(p, "S001", 0, FRAMES)
    with pytest.raises(FilmError, match="cap reached"):
        _submit(p, quote=quote)  # per-segment cap 10000 approves; budget 10
    ledger = read(p / "render/ledger.json", {"jobs": {}})
    assert ledger["jobs"] == {}


def test_segment_jobs_refuse_symlinked_dirs(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    _run_to_verified(p)
    moved = tmp_path / "moved_jobs"
    (p / "animation/segment_jobs/S001").rename(moved)
    os.symlink(moved, p / "animation/segment_jobs/S001",
               target_is_directory=True)
    with pytest.raises(FilmError, match="SEGMENT_JOBS_SYMLINK"):
        segment_jobs(p)
    with pytest.raises(FilmError, match="SEGMENT_JOBS_SYMLINK"):
        segment_jobs(p, "S001")
    root_moved = tmp_path / "moved_root"
    (p / "animation/segment_jobs").rename(root_moved)
    os.symlink(root_moved, p / "animation/segment_jobs",
               target_is_directory=True)
    with pytest.raises(FilmError, match="SEGMENT_JOBS_SYMLINK"):
        segment_jobs(p)


def test_segment_control_rules_and_no_member_fill(tmp_path):
    p, masters, _, _ = b_scene(tmp_path)
    # Interior frames only, unless a B segment forces the end anchor.
    with pytest.raises(FilmError, match="FRAME_OUT_OF_RANGE"):
        import_segment_control(p, "S001", _png(tmp_path / "x.png", 8),
                               role="keypose", frame=FRAMES)
    with pytest.raises(FilmError, match="FRAME_OUT_OF_RANGE"):
        import_segment_control(p, "S001", _png(tmp_path / "x.png", 8),
                               role="keypose", frame=FRAMES + 1)
    with pytest.raises(FilmError, match="role must be one of"):
        import_segment_control(p, "S001", _png(tmp_path / "x.png", 8),
                               role="inbetween", frame=3)
    result = import_segment_control(p, "S001", _png(tmp_path / "p.png", 7),
                                    role="pose", frame=6)
    assert result["state"] == "DRAFT"
    record = load_registry(p)["assets"][result["asset_pin"]["asset_id"]]
    rec = record["revisions"][str(record["current_revision"])]
    assert rec["kind"] == "CONTROL_IMAGE" and rec["control_role"] == "POSE"
    assert rec["frame"] == 6
    # Conditioning inputs never fill produced draft members.
    assert not (p / "animation/shots/S001/draft_frames.json").exists()
    small = tmp_path / "small.png"
    Image.new("RGB", (16, 16)).save(small)
    with pytest.raises(FilmError, match="CANVAS_MISMATCH"):
        import_segment_control(p, "S001", small, role="pose", frame=8)


def test_pose_guide_interior_control_flows_into_spec(tmp_path):
    p, _, _, _ = b_scene(tmp_path,
                         capabilities=["REFERENCE_IMAGES", "POSE_GUIDE"])
    import_segment_control(p, "S001", _png(tmp_path / "pose.png", 4),
                           role="pose", frame=6)
    result = segment_quote(p, "S001", 0, FRAMES)
    assert result["status"] == "PLANNED"


# --- state gating ---------------------------------------------------------------

def test_compositor_still_refuses_b_plans(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    with pytest.raises(FilmError, match="path-C segments only"):
        composite_shot(p, "S001")


def test_legacy_project_refuses_segment_operations(tmp_path):
    legacy = fixture_project(_sub(tmp_path, "legacy"), shot_count=1,
                             seconds=1)
    with pytest.raises(FilmError, match="LEGACY_MV"):
        segment_quote(legacy, "S001", 0, FRAMES)
    with pytest.raises(FilmError, match="LEGACY_MV"):
        segment_jobs(legacy)


def test_unknown_job_and_terminal_states(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    with pytest.raises(FilmError, match="Unknown segment job"):
        segment_reconcile(p, "seg-0000000000000000000a")
    job_id = _run_to_verified(p)
    segment_import(p, job_id)
    with pytest.raises(FilmError, match="NOTHING_TO_RECONCILE"):
        segment_reconcile(p, job_id)
    result = segment_import(p, job_id)  # verified imports are resumable
    assert result["resumed"] is True


def test_job_journal_schema(tmp_path):
    p, _, _, _ = b_scene(tmp_path)
    result = _submit(p)
    journal = load_job_journal(p, "S001", result["job_id"])
    assert journal, "submit must journal"
    for record in journal:
        assert set(record) == {"document_type", "schema_version", "job_id",
                               "attempt_id", "request_id", "event", "at",
                               "data"}
        assert record["job_id"] == result["job_id"]
        assert record["request_id"] == result["request_id"]
    bad = dict(journal[0]); bad["event"] = "TELEPORT"
    with pytest.raises(FilmError, match="Unknown job_journal event"):
        validate_job_journal(bad)
    bad = dict(journal[0]); bad["document_type"] = "not_a_journal"
    with pytest.raises(FilmError, match="Unknown document_type"):
        validate_job_journal(bad)


# --- CLI ------------------------------------------------------------------------

def test_cli_segment_flow(tmp_path, capsys):
    p, _, _, _ = b_scene(tmp_path)
    assert cli.main(["segment-quote", str(p), "S001",
                     "--start", "0", "--end", str(FRAMES)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["provider_class"] == "FAKE"
    assert out["qualification_state"] == "UNQUALIFIED"
    assert cli.main(["segment-submit", str(p), "S001",
                     "--start", "0", "--end", str(FRAMES),
                     "--approver", "tester", "--quote-id", out["quote_id"],
                     "--cap", "10000"]) == 0
    sub = json.loads(capsys.readouterr().out)
    job = sub["job_id"]
    assert cli.main(["segment-reconcile", str(p), job]) == 0
    capsys.readouterr()
    assert cli.main(["segment-import", str(p), job]) == 0
    capsys.readouterr()
    assert cli.main(["segment-commit", str(p), "S001"]) == 0
    commit = json.loads(capsys.readouterr().out)
    assert commit["state"] == "DRAFT" and commit["frames"] == FRAMES
    assert cli.main(["segment-jobs", str(p)]) == 0
    jobs = json.loads(capsys.readouterr().out)
    assert jobs["jobs"][0]["status"] == "VERIFIED"
    assert cli.main(["segment-fake", str(p)]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["provider_class"] == "FAKE"
    # A fenced UNKNOWN job refuses a resubmission through the CLI too.
    p2, _, _, _ = b_scene(_sub(tmp_path, "cli2"))
    assert cli.main(["segment-fake", str(p2),
                     "--set", '{"behaviors": {"lost_ack": true}}']) == 0
    capsys.readouterr()
    assert cli.main(["segment-quote", str(p2), "S001",
                     "--start", "0", "--end", str(FRAMES)]) == 0
    q2 = json.loads(capsys.readouterr().out)
    assert cli.main(["segment-submit", str(p2), "S001",
                     "--start", "0", "--end", str(FRAMES),
                     "--approver", "t", "--quote-id", q2["quote_id"],
                     "--cap", "10000"]) == 0
    capsys.readouterr()
    assert cli.main(["segment-submit", str(p2), "S001",
                     "--start", "0", "--end", str(FRAMES),
                     "--approver", "t", "--quote-id", q2["quote_id"],
                     "--cap", "10000"]) == 1
    assert "JOB_FENCED" in capsys.readouterr().err


def test_cli_rejects_legacy(tmp_path, capsys):
    legacy = fixture_project(_sub(tmp_path, "legacy"), shot_count=1,
                             seconds=1)
    assert cli.main(["segment-quote", str(legacy), "S001",
                     "--start", "0", "--end", "24"]) == 1
    assert "LEGACY_MV" in capsys.readouterr().err
