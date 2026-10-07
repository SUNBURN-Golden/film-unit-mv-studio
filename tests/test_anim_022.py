"""ANIM-022: W00 pilot gate, route re-checks and sealed delivery approvals.

Every fixture here is synthetic (Pillow PNGs, a generated sine master, the
fake segment adapter). `w00_pilot`, `approver_delegation`,
`w00_spend_approval` and `delivery_approval` records written by these tests
are protocol fixtures exercising the engine boundary — they are not evidence
of real artwork production, human acceptance or release approval. The
reported evidence facets stay qualification UNQUALIFIED, acceptance PENDING,
release NOT_AUTHORIZED.
"""
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

from engine import cli
from engine.animation_compiler import compile_final_candidate
from engine.animation_locks import (declare_waves, record_final_lock,
                                    record_plan_lock, record_route_decision,
                                    record_wave_lock)
from engine.animation_review import (_film_binding_current, append_review,
                                     binding_digest, load_reviews,
                                     record_cut_review,
                                     record_transition_review)
from engine.animation_schema import canon_bytes, read_canon, write_canon
from engine.autopilot import autopilot
from engine.builds import verify_build
from engine.core import FilmError, read, write
from engine.w00_gate import (PILOTS_PATH, _clean_film_binding_current,
                             approve_delivery, deliverable_status,
                             gate_status, load_pilots, pilot_targets,
                             record_delegation, record_pilot,
                             record_spend_approval, validate_pilot)
from engine.segment_fake import configure_fake
from engine.segment_gen import (import_segment_control, segment_import,
                                segment_jobs, segment_quote,
                                segment_reconcile, segment_submit)
from engine.animation_assets import import_draft_image
from engine.motion_plan import save_shot_plan
from test_anim_007 import (APPROVER, CUT_METHODS, HARD_TYPES, LEAD, REVIEWER,
                           TRANSITION_METHODS, adopt_w00, import_shot,
                           produced_project, review_transitions, w00_project)
from test_anim_003 import animation_project
from test_compiler_v03 import fixture_project

W00 = "W00"
PRIMARY = "박준태"
BW, BH, BFRAMES = 320, 240, 48  # fixture canvas; 2s per shot at 24fps


def SHA256_OK(value):
    return type(value) is str and len(value) == 64 \
        and all(c in "0123456789abcdef" for c in value)


def _png(path, seed):
    """A real PNG; every seed produces different pixels and bytes."""
    im = Image.new("RGB", (BW, BH))
    px = im.load()
    for y in range(BH):
        for x in range(BW):
            px[x, y] = ((x * 5 + seed * 29) % 256,
                        (y * 7 + seed * 11) % 256,
                        (x + y + seed * 3) % 256)
    im.save(path)
    return path


def _pin(result):
    if "asset_pin" in result:
        return dict(result["asset_pin"])
    return {"asset_id": result["asset_id"], "revision": result["revision"],
            "content_sha256": result["content_sha256"]}


def _b_plan(p, tmp_path, shot_id="S001", split=False):
    """A saved pure-B shot plan plus start-anchor control(s) for one shot.

    `split` declares two B segments [0, half) and [half, BFRAMES) so a test
    can run two distinct durable jobs on the same shot.
    """
    half = BFRAMES // 2
    segments = [{"start": 0, "end": BFRAMES, "path": "B",
                 "capabilities": ["REFERENCE_IMAGES"]}]
    if split:
        segments = [{"start": 0, "end": half, "path": "B",
                     "capabilities": ["REFERENCE_IMAGES"]},
                    {"start": half, "end": BFRAMES, "path": "B",
                     "capabilities": ["REFERENCE_IMAGES"]}]
    pin = _pin(import_draft_image(
        p, shot_id, _png(tmp_path / f"{shot_id}_master.png", 10),
        role="layout", frame=0))
    save_shot_plan(p, {
        "document_type": "animation_shot_plan", "schema_version": 1,
        "shot_id": shot_id, "revision": 1, "motion_intent": "ANIMATED",
        "story_role": "synthetic fixture", "emotion_role": "synthetic",
        "canvas": {"width": BW, "height": BH}, "background": [10, 20, 30],
        "rig": None,
        "assets": [{**pin, "kind": "CONTROL_IMAGE", "role": "master",
                    "layout": None}],
        "events": [], "keyposes": [],
        "segments": segments,
        "tracks": {"camera": {"pivot": [BW // 2, BH // 2],
                              "transform": None},
                   "layers": {}},
        "preparation": {"work": [], "creator": "synthetic fixture",
                        "reviewer": None, "revision_scope": "fixture"}})
    anchors = [0, half] if split else [0]
    for frame in anchors:
        import_segment_control(
            p, shot_id, _png(tmp_path / f"{shot_id}_a{frame}.png",
                             1 + frame),
            role="keypose", frame=frame)


def _verified_job(p, shot_id="S001", start=0, end=BFRAMES):
    """One fake-provider segment job run to VERIFIED on `shot_id`."""
    quote = segment_quote(p, shot_id, start, end)
    sub = segment_submit(p, shot_id, start, end,
                         approver="synthetic fixture approver",
                         quote_id=quote["quote_id"], cap=10000)
    segment_reconcile(p, sub["job_id"])
    segment_import(p, sub["job_id"])
    return sub["job_id"]


def _prepared_w00(tmp_path, with_plan=False, split_plan=False):
    """W00 locked and adopted; T001 reviewed; pilot targets ready."""
    p = w00_project(tmp_path)
    if with_plan:
        _b_plan(p, tmp_path, split=split_plan)
    record_plan_lock(p, APPROVER)
    record_wave_lock(p, W00, APPROVER)
    adopt_w00(p)
    record_transition_review(p, "T001", reviewer=REVIEWER,
                             methods=TRANSITION_METHODS)
    return p


def _keep_pilots(p, decision="KEEP"):
    """Synthetic pilot records for every W00 target."""
    records = []
    for target in pilot_targets(p)["cuts"] + pilot_targets(p)["transitions"]:
        records.append(record_pilot(
            p, target, decision=decision,
            reviewer="synthetic pilot reviewer",
            reviewer_kind="SYNTHETIC_FIXTURE",
            reason="fixture: hard main-film target",
            capability_ref="CE-fixture"))
    return records


def _decide(p, decision="KEEP", apply_scope=None, changes=None):
    return record_route_decision(
        p, W00, decision=decision, decider=LEAD, approver=APPROVER,
        checked_types=[d["type"] for d in HARD_TYPES], unchecked_types=[],
        apply_scope=["W01"] if apply_scope is None else apply_scope,
        changes=changes,
        reviewed_conditions=["fixture: W00 targets played at speed"],
        observations=["fixture: route observation"],
        cost_time_impact="fixture: none")


def _final_build(p, tmp_path):
    produced_project(p, tmp_path)
    review_transitions(p)
    record_final_lock(p, APPROVER)
    result = compile_final_candidate(p)
    return result["build_id"]


def _tree_hash(folder):
    h = hashlib.sha256()
    for f in sorted(Path(folder).rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(folder)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def _build_folder(p, build_id):
    return p / "builds" / build_id


# --- pilot targets, records and the next-wave gate (W00-ROUTE) --------------

def test_pilot_targets_and_initial_gate(tmp_path):
    p = _prepared_w00(tmp_path)
    targets = pilot_targets(p)
    assert targets["declared"] is True and targets["wave"] == "W00"
    assert targets["cuts"] == ["I001", "I002"]
    assert targets["transitions"] == ["T001"]  # joins two W00 shots
    gate = gate_status(p)
    assert gate["gate"]["state"] == "BLOCKED"
    reasons = " ".join(gate["gate"]["reasons"])
    assert "no pilot record" in reasons and "route decision is pending" \
        in reasons
    rows = {r["target_id"]: r for r in gate["pilots"]}
    assert all(r["state"] == "NO_PILOT_RECORD" for r in rows.values())
    # Honest facets: protocol records never claim real approval.
    assert gate["facets"] == {"qualification_state": "UNQUALIFIED",
                              "acceptance_state": "PENDING",
                              "release_state": "NOT_AUTHORIZED"}


def test_pilot_record_binds_current_review_and_gates(tmp_path):
    p = _prepared_w00(tmp_path)
    records = _keep_pilots(p)
    assert [r["pilot_id"] for r in records] == ["WP0001", "WP0002", "WP0003"]
    record = records[0]
    # The bound digests: source, sequence and reviewed-artifact binding.
    assert SHA256_OK(record["source_sha256"])
    assert SHA256_OK(record["sequence_sha256"])
    assert SHA256_OK(record["artifact_sha256"])
    assert record["review_id"] and record["reviewer_kind"] \
        == "SYNTHETIC_FIXTURE"
    assert record["manifest_ref"] is None  # W01 unproduced: not invented
    # The transition pilot binds both endpoint sources/sequences.
    transition = records[2]
    assert transition["target_kind"] == "TRANSITION"
    assert SHA256_OK(transition["source_sha256"])
    # Gate still waits for the route decision.
    gate = gate_status(p)
    assert gate["gate"]["state"] == "BLOCKED"
    assert gate["gate"]["reasons"] == ["the W00 route decision is pending"]
    _decide(p)
    gate = gate_status(p)
    assert gate["gate"]["state"] == "OPEN" and not gate["gate"]["reasons"]
    assert gate["open_waves"] == ["W01"]
    assert all(r["state"] == "CURRENT" for r in gate["pilots"])
    # Playback review state is reported per scope.
    assert gate["playback"]["cuts"]["I001"]["state"] == "CURRENT"
    assert gate["playback"]["cuts"]["I001"]["methods"] == CUT_METHODS
    assert gate["playback"]["transitions"]["T001"]["methods"] \
        == TRANSITION_METHODS


def test_pilot_needs_a_current_review_and_w00_target(tmp_path):
    p = w00_project(tmp_path)
    record_plan_lock(p, APPROVER)
    record_wave_lock(p, W00, APPROVER)
    # Unadopted cuts have nothing a pilot record could bind.
    with pytest.raises(FilmError, match="current adopted review"):
        record_pilot(p, "I001", decision="KEEP", reviewer="r",
                     reviewer_kind="SYNTHETIC_FIXTURE", reason="fixture")
    adopt_w00(p)
    with pytest.raises(FilmError, match="pilot target"):
        record_pilot(p, "I003", decision="KEEP", reviewer="r",
                     reviewer_kind="SYNTHETIC_FIXTURE", reason="fixture")
    # T001's transition review is missing: refused, never invented.
    with pytest.raises(FilmError, match="current adopted review"):
        record_pilot(p, "T001", decision="KEEP", reviewer="r",
                     reviewer_kind="SYNTHETIC_FIXTURE", reason="fixture")


def test_pilot_log_is_canonical_and_tamper_evident(tmp_path):
    p = _prepared_w00(tmp_path)
    _keep_pilots(p)
    path = p / PILOTS_PATH
    lines = path.read_bytes().split(b"\n")
    assert lines[-1] == b"" and len(lines) == 4
    stored = load_pilots(p)
    for line, record in zip(lines, stored):
        assert line + b"\n" == canon_bytes(record)
        validate_pilot(record)  # binding_sha256 recomputes
    # Mutating a bound field breaks the stored binding digest on load.
    bad = json.loads(lines[0])
    bad["source_sha256"] = "0" * 64
    path.write_bytes(canon_bytes(bad) + b"\n".join(lines[1:]))
    with pytest.raises(FilmError, match="binding_sha256"):
        load_pilots(p)


def test_keep_gate_blocks_on_a_dissenting_pilot(tmp_path):
    p = _prepared_w00(tmp_path)
    _keep_pilots(p, decision="CHANGE")  # pilot-level judgement: change
    _decide(p, decision="KEEP", changes=None)
    gate = gate_status(p)
    assert gate["gate"]["state"] == "BLOCKED"
    assert any("KEEP must not contradict" in r
               for r in gate["gate"]["reasons"])


# --- approver rule and delegation -------------------------------------------

def test_approver_rule_primary_delegate_and_refusal(tmp_path):
    p = _prepared_w00(tmp_path)
    with pytest.raises(FilmError, match="박준태"):
        record_pilot(p, "I001", decision="KEEP", reviewer="someone else",
                     reviewer_kind="HUMAN", reason="fixture")
    # A delegate without a delegation record is refused as well.
    with pytest.raises(FilmError, match="no unexpired delegation"):
        record_pilot(p, "I001", decision="KEEP", reviewer="delegate x",
                     reviewer_kind="HUMAN", reason="fixture")
    delegation = record_delegation(
        p, delegate="delegate x", scope=["W00_PILOT"],
        expires_at_ms=int(time.time() * 1000) + 3600_000)
    record = record_pilot(p, "I001", decision="KEEP", reviewer="delegate x",
                          reviewer_kind="HUMAN", reason="fixture")
    assert record["delegation_id"] == delegation["delegation_id"]
    # An expired delegation no longer authorizes.
    record_delegation(
        p, delegate="delegate y", scope=["ALL"],
        expires_at_ms=int(time.time() * 1000) + 1000,
        now_ms=int(time.time() * 1000) - 5000)
    with pytest.raises(FilmError, match="no unexpired delegation"):
        record_pilot(p, "I002", decision="KEEP", reviewer="delegate y",
                     reviewer_kind="HUMAN", reason="fixture",
                     now_ms=int(time.time() * 1000) + 5000)
    # A delegation for a different scope does not authorize W00_PILOT.
    record_delegation(p, delegate="delegate z", scope=["DELIVERY_APPROVAL"],
                      expires_at_ms=int(time.time() * 1000) + 3600_000)
    with pytest.raises(FilmError, match="no unexpired delegation"):
        record_pilot(p, "I002", decision="KEEP", reviewer="delegate z",
                     reviewer_kind="HUMAN", reason="fixture")
    # Only the primary approver may delegate.
    with pytest.raises(FilmError, match="primary approver"):
        record_delegation(p, delegate="nope", scope=["ALL"], delegator="not JT",
                          expires_at_ms=int(time.time() * 1000) + 1000)


def test_synthetic_pilots_never_create_real_approval(tmp_path):
    p = _prepared_w00(tmp_path)
    _keep_pilots(p)
    _decide(p)
    gate = gate_status(p)
    assert gate["gate"]["state"] == "OPEN"
    assert gate["evidence"]["synthetic_pilots"] == 3
    # The software gate opens on protocol records; real qualification,
    # acceptance and release stay honestly pending.
    assert gate["facets"] == {"qualification_state": "UNQUALIFIED",
                              "acceptance_state": "PENDING",
                              "release_state": "NOT_AUTHORIZED"}


# --- W00-ROUTE: CHANGE/MIX re-checks -----------------------------------------

def test_change_rechecks_spend_reservation_and_unknown(tmp_path):
    p = _prepared_w00(tmp_path, with_plan=True)
    quote = segment_quote(p, "S001", 0, BFRAMES)  # PLANNED, quoted, unspent
    _keep_pilots(p)
    _decide(p, decision="CHANGE", changes=["fixture: S002 route changes"])
    gate = gate_status(p)
    assert gate["gate"]["state"] == "BLOCKED"
    assert any("additional-spend approval" in r
               for r in gate["gate"]["reasons"])
    assert gate["quote"]["totals"] == {"credits": quote["amount"]}
    # A spend approval bound to this exact decision clears that reason.
    with pytest.raises(FilmError, match="박준태"):
        record_spend_approval(p, approver="intruder",
                              reviewer_kind="HUMAN", reason="x",
                              jobs=[quote["job_id"]])
    spend = record_spend_approval(p, approver=PRIMARY,
                                  reviewer_kind="HUMAN",
                                  reason="fixture: approved re-spend",
                                  jobs=[quote["job_id"]])
    assert spend["decision_id"]
    gate = gate_status(p)
    assert gate["gate"]["state"] == "OPEN"
    # A superseding route decision re-opens the obligation: the approval
    # binds the exact decision revision, never the wave in general.
    _decide(p, decision="MIX", changes=["fixture: second revision"])
    gate = gate_status(p)
    assert gate["gate"]["state"] == "BLOCKED"
    assert any("additional-spend approval" in r
               for r in gate["gate"]["reasons"])
    record_spend_approval(p, approver=PRIMARY, reviewer_kind="HUMAN",
                          reason="fixture: second approval",
                          jobs=[quote["job_id"]])
    assert gate_status(p)["gate"]["state"] == "OPEN"


def test_unknown_job_fences_any_route(tmp_path):
    p = _prepared_w00(tmp_path, with_plan=True)
    configure_fake(p, behaviors={"lost_ack": True})
    quote = segment_quote(p, "S001", 0, BFRAMES)
    sub = segment_submit(p, "S001", 0, BFRAMES,
                         approver="synthetic fixture approver",
                         quote_id=quote["quote_id"], cap=10000)
    assert sub["status"] == "UNKNOWN"
    _keep_pilots(p)
    _decide(p)  # KEEP — the unknown job fences it anyway
    gate = gate_status(p)
    assert gate["gate"]["state"] == "BLOCKED"
    assert any(sub["job_id"] in r and "UNKNOWN" in r
               for r in gate["gate"]["reasons"])
    job = [j for j in segment_jobs(p)["jobs"]
           if j["job_id"] == sub["job_id"]][0]
    assert gate["unknown"] == [{"job_id": sub["job_id"],
                                "status": "UNKNOWN",
                                "charge_state": job["charge_state"]}]
    # Nothing was auto-retried or resubmitted: the one job identity stays.
    assert [j["job_id"] for j in segment_jobs(p)["jobs"]] == [sub["job_id"]]
    # Reconciling the same identity clears the fence.
    configure_fake(p, behaviors={"lost_ack": True,
                                 "lost_ack_delivered": True})
    segment_reconcile(p, sub["job_id"])
    assert gate_status(p)["gate"]["state"] == "OPEN"


def test_live_and_unreconciled_reservation_block_change(tmp_path):
    p = _prepared_w00(tmp_path, with_plan=True, split_plan=True)
    job_id = _verified_job(p, end=BFRAMES // 2)
    _keep_pilots(p)
    _decide(p, decision="CHANGE", changes=["fixture: route revision"])
    assert gate_status(p)["gate"]["state"] == "OPEN"  # settled: no obligation
    # A corrupted ledger entry must re-block the changed route.
    job = read(p / "animation/segment_jobs/S001" / f"{job_id}.json")
    ledger_path = p / "render/ledger.json"
    ledger = read(ledger_path)
    key = job["reservation"]["ledger_key"]
    ledger["jobs"][key]["reserved_amount"] += 1
    write(ledger_path, ledger)
    gate = gate_status(p)
    assert gate["gate"]["state"] == "BLOCKED"
    assert any("reservation" in r for r in gate["gate"]["reasons"])
    ledger["jobs"][key]["reserved_amount"] -= 1
    write(ledger_path, ledger)
    # A still-live job also fences a changed route until it settles.
    quote = segment_quote(p, "S001", BFRAMES // 2, BFRAMES)
    running = segment_submit(p, "S001", BFRAMES // 2, BFRAMES,
                             approver="synthetic fixture approver",
                             quote_id=quote["quote_id"], cap=10000)
    assert running["status"] == "RUNNING"
    gate = gate_status(p)
    assert any("settle the in-flight" in r
               for r in gate["gate"]["reasons"])
    segment_reconcile(p, running["job_id"])
    segment_import(p, running["job_id"])


def test_pilot_job_binding_must_be_verified_and_on_target(tmp_path):
    p = _prepared_w00(tmp_path, with_plan=True)
    quote = segment_quote(p, "S001", 0, BFRAMES)
    running = segment_submit(p, "S001", 0, BFRAMES,
                             approver="synthetic fixture approver",
                             quote_id=quote["quote_id"], cap=10000)
    with pytest.raises(FilmError, match="RUNNING"):
        record_pilot(p, "I001", decision="KEEP", reviewer="r",
                     reviewer_kind="SYNTHETIC_FIXTURE", reason="fixture",
                     job_result_id=running["job_id"])
    segment_reconcile(p, running["job_id"])
    segment_import(p, running["job_id"])
    record = record_pilot(p, "I001", decision="KEEP", reviewer="r",
                          reviewer_kind="SYNTHETIC_FIXTURE",
                          reason="fixture",
                          job_result_id=running["job_id"])
    assert record["job_result_id"] == running["job_id"]
    # Another shot's job does not bind to this target.
    _b_plan(p, tmp_path, shot_id="S002")
    other = _verified_job(p, "S002")
    with pytest.raises(FilmError, match="not to"):
        record_pilot(p, "I001", decision="KEEP", reviewer="r",
                     reviewer_kind="SYNTHETIC_FIXTURE", reason="fixture",
                     job_result_id=other)


def test_apply_scope_bounds_the_gate(tmp_path):
    p = animation_project(tmp_path, shot_count=4, seconds=8)
    declare_waves(p, [
        {"wave": "W00", "shots": ["S001", "S002"],
         "difficulty": list(HARD_TYPES)},
        {"wave": "W01", "shots": ["S003"], "difficulty": []},
        {"wave": "W02", "shots": ["S004"], "difficulty": []}])
    import_shot(p, tmp_path, "S001", 10)
    import_shot(p, tmp_path, "S002", 20)
    record_plan_lock(p, APPROVER)
    record_wave_lock(p, W00, APPROVER)
    adopt_w00(p)
    record_transition_review(p, "T001", reviewer=REVIEWER,
                             methods=TRANSITION_METHODS)
    _keep_pilots(p)
    _decide(p, decision="MIX", apply_scope=["W01"],
            changes=["fixture: mixed route"])
    gate = gate_status(p)
    assert gate["open_waves"] == ["W01"]
    with pytest.raises(FilmError, match="apply scope"):
        record_wave_lock(p, "W02", APPROVER)


# --- ART-STALE: changed inputs stale the affected approvals ------------------

def test_changed_input_stales_the_pilot_and_reblocks(tmp_path):
    p = _prepared_w00(tmp_path)
    _keep_pilots(p)
    _decide(p)
    assert gate_status(p)["gate"]["state"] == "OPEN"
    # A new S001 revision moves the adopted pin: the cut and the transition
    # it feeds go stale, and so does the pilot record bound to them.
    import_shot(p, tmp_path, "S001", 11)
    gate = gate_status(p)
    rows = {r["target_id"]: r for r in gate["pilots"]}
    assert rows["I001"]["state"] == "STALE"
    assert rows["T001"]["state"] == "STALE"
    assert rows["I002"]["state"] == "CURRENT"
    assert gate["gate"]["state"] == "BLOCKED"
    assert any("pilot record is stale" in r
               for r in gate["gate"]["reasons"])
    assert "CUT:I001" in gate["closure"]["stale"]
    assert "TRANSITION:T001" in gate["closure"]["stale"]
    assert "WAVE_LOCK:W00" in gate["closure"]["stale"]
    # Recovery re-runs the same human gates; the stale record stays
    # append-only history, never revived.
    record_wave_lock(p, W00, APPROVER)
    record_cut_review(p, "I001", reviewer=REVIEWER, methods=CUT_METHODS)
    record_transition_review(p, "T001", reviewer=REVIEWER,
                             methods=TRANSITION_METHODS)
    record = record_pilot(p, "I001", decision="KEEP", reviewer="r",
                          reviewer_kind="SYNTHETIC_FIXTURE",
                          reason="fixture: re-reviewed")
    assert record["pilot_id"] == "WP0004"
    record_pilot(p, "T001", decision="KEEP", reviewer="r",
                 reviewer_kind="SYNTHETIC_FIXTURE", reason="fixture")
    assert gate_status(p)["gate"]["state"] == "OPEN"


def test_orphaned_pilot_reports_out_of_scope(tmp_path):
    p = _prepared_w00(tmp_path)
    _keep_pilots(p)
    # Rename the transition: the T001 record is history for a target that
    # no longer exists in the current W00 scope.
    timeline = read_canon(p / "timeline/edit.json")
    timeline["entries"][0]["transition_out"]["id"] = "T005"
    write_canon(p / "timeline/edit.json", timeline)
    gate = gate_status(p)
    rows = {r["target_id"]: r for r in gate["pilots"]}
    assert rows["T001"]["state"] == "OUT_OF_SCOPE"
    assert "T001" not in gate["pilot_targets"]["transitions"]
    assert "T005" in gate["pilot_targets"]["transitions"]
    # The rename stales the plan binding; re-lock and review the renamed
    # transition so only its missing pilot blocks the gate.
    record_plan_lock(p, APPROVER)
    record_transition_review(p, "T005", reviewer=REVIEWER,
                             methods=TRANSITION_METHODS)
    _decide(p)
    gate = gate_status(p)
    # The orphaned T001 record is history: it must not keep the gate
    # blocked — only the live T005 target's missing pilot does.
    assert gate["gate"]["state"] == "BLOCKED"
    assert gate["gate"]["reasons"] == ["T005: no pilot record"]
    record_pilot(p, "T005", decision="KEEP", reviewer="r",
                 reviewer_kind="SYNTHETIC_FIXTURE", reason="fixture")
    gate = gate_status(p)
    assert gate["gate"]["state"] == "OPEN"
    rows = {r["target_id"]: r for r in gate["pilots"]}
    assert rows["T005"]["state"] == "CURRENT"
    assert rows["T001"]["state"] == "OUT_OF_SCOPE"


def test_stale_film_approval_never_carries_to_new_hash(tmp_path):
    p = w00_project(tmp_path)
    build_id = _final_build(p, tmp_path)
    approve_delivery(p, build_id, "MASTER_CLEAN.mp4",
                     approver=PRIMARY, reviewer_kind="HUMAN")
    status = deliverable_status(p, build_id)
    assert status["deliverables"]["MASTER_CLEAN.mp4"]["state"] == "CURRENT"
    # Replace the sealed deliverable bytes (a "new artifact hash"): the old
    # approval binds the old digest and stops verifying.
    target = _build_folder(p, build_id) / "MASTER_CLEAN.mp4"
    target.write_bytes(target.read_bytes() + b"tampered")
    status = deliverable_status(p, build_id)
    assert status["deliverables"]["MASTER_CLEAN.mp4"]["state"] == "STALE"
    assert status["deliverables"]["MASTER_CLEAN.mp4"]["approver"] is None


# --- sealed clean/subbed delivery approvals -----------------------------------

def test_clean_and_subbed_approve_separately(tmp_path):
    p = w00_project(tmp_path)
    build_id = _final_build(p, tmp_path)
    folder = p / "builds" / build_id
    status = deliverable_status(p, build_id)
    assert all(row["state"] == "UNREVIEWED"
               for row in status["deliverables"].values())
    before = _tree_hash(folder)
    out = approve_delivery(p, build_id, "MASTER_CLEAN.mp4",
                           approver=PRIMARY, reviewer_kind="HUMAN")
    status = deliverable_status(p, build_id)
    clean = status["deliverables"]["MASTER_CLEAN.mp4"]
    subbed = status["deliverables"]["MASTER_SUBBED.mp4"]
    assert clean["state"] == "CURRENT" and clean["governed"] is True
    assert clean["approval_id"] == out["approval"]["approval_id"]
    assert subbed["state"] == "UNREVIEWED"  # one approval covers one file
    # The approval binds this file's own hash, distinct from the other.
    assert clean["deliverable_sha256"] != subbed["deliverable_sha256"]
    assert out["approval"]["deliverable_sha256"] \
        == clean["deliverable_sha256"]
    # Nothing inside the sealed build changed.
    assert _tree_hash(folder) == before
    assert verify_build(folder)["valid"] is True
    # The subbed file then gets its own separate approval.
    approve_delivery(p, build_id, "MASTER_SUBBED.mp4",
                     approver="synthetic fixture approver",
                     reviewer_kind="SYNTHETIC_FIXTURE")
    status = deliverable_status(p, build_id)
    assert status["deliverables"]["MASTER_SUBBED.mp4"]["state"] == "CURRENT"
    assert status["deliverables"]["MASTER_SUBBED.mp4"]["reviewer_kind"] \
        == "SYNTHETIC_FIXTURE"
    gate = gate_status(p)
    assert gate["evidence"]["synthetic_delivery_approvals"] == 1
    assert gate["facets"]["release_state"] == "NOT_AUTHORIZED"
    assert _tree_hash(folder) == before
    assert verify_build(folder)["valid"] is True


def test_clean_approval_ignores_font_and_cue_bindings(tmp_path):
    p = w00_project(tmp_path)
    build_id = _final_build(p, tmp_path)
    approve_delivery(p, build_id, "MASTER_CLEAN.mp4",
                     approver=PRIMARY, reviewer_kind="HUMAN")
    approve_delivery(p, build_id, "MASTER_SUBBED.mp4",
                     approver=PRIMARY, reviewer_kind="HUMAN")
    status = deliverable_status(p, build_id)
    assert all(row["state"] == "CURRENT"
               for row in status["deliverables"].values())
    folder = _build_folder(p, build_id)
    # A font/cue change stales the subbed master and the subbed delivery
    # only (ADR 0001 §8). Every file in the sealed build is in its
    # inventory, so the change is simulated on the record side: the same
    # approval bound to different font/lyrics-review digests.
    by_deliverable = {r["deliverable"]: r for r in load_reviews(p)
                      if r["scope"] == "FINAL_FILM"}

    def rebound(deliverable, review_id):
        record = dict(by_deliverable[deliverable])
        record["review_id"] = review_id
        record["font_sha256"] = "0" * 64
        record["lyrics_review_sha256"] = "1" * 64
        record["binding_sha256"] = binding_digest(record)
        return append_review(p, record)

    changed_clean = rebound("MASTER_CLEAN.mp4", "RV0090")
    changed_subbed = rebound("MASTER_SUBBED.mp4", "RV0091")
    # Predicate level: the changed font/cue binding fails the subbed
    # check but is irrelevant to the clean-only bindings.
    assert _film_binding_current(folder, changed_subbed) is False
    assert _clean_film_binding_current(folder, changed_clean) is True
    status = deliverable_status(p, build_id)
    clean = status["deliverables"]["MASTER_CLEAN.mp4"]
    subbed = status["deliverables"]["MASTER_SUBBED.mp4"]
    # The clean approval treats the record as current; the subbed side
    # rejects it as stale and keeps the last still-bound approval.
    assert clean["state"] == "CURRENT"
    assert clean["review_id"] == "RV0090"
    assert subbed["state"] == "CURRENT"
    assert subbed["review_id"] \
        == by_deliverable["MASTER_SUBBED.mp4"]["review_id"]


def test_delivery_approver_rule_and_unknown_deliverable(tmp_path):
    p = w00_project(tmp_path)
    build_id = _final_build(p, tmp_path)
    with pytest.raises(FilmError, match="박준태"):
        approve_delivery(p, build_id, "MASTER_CLEAN.mp4",
                         approver="random", reviewer_kind="HUMAN")
    with pytest.raises(FilmError, match="separately"):
        approve_delivery(p, build_id, "FILM.mp4",
                         approver=PRIMARY, reviewer_kind="HUMAN")
    # A delegated approver for the delivery scope passes the rule.
    record_delegation(p, delegate="delivery delegate",
                      scope=["DELIVERY_APPROVAL"],
                      expires_at_ms=int(time.time() * 1000) + 3600_000)
    out = approve_delivery(p, build_id, "MASTER_CLEAN.mp4",
                           approver="delivery delegate",
                           reviewer_kind="HUMAN")
    assert out["approval"]["delegation_id"] == "DG0001"


def test_nothing_auto_executes(tmp_path):
    p = _prepared_w00(tmp_path)
    _keep_pilots(p)
    job_dirs = sorted(p.glob("animation/segment_jobs/*/*.json"))
    _decide(p, decision="CHANGE", changes=["fixture"])
    gate_status(p)
    assert sorted(p.glob("animation/segment_jobs/*/*.json")) == job_dirs
    assert not list(p.glob("builds/B*"))
    stage = autopilot(p)
    assert stage["stage"] in ("NEEDS_PRODUCTION_INPUTS",
                              "NEEDS_WAVE_LOCK", "WAVE_SCOPE_CLOSED",
                              "NEEDS_CUT_REVIEW")


# --- LEGACY_MV boundary and CLI ------------------------------------------------

def test_legacy_mv_is_refused_and_unchanged(tmp_path):
    p = fixture_project(tmp_path, seconds=6, shot_count=3)
    for call in (lambda: gate_status(p),
                 lambda: pilot_targets(p),
                 lambda: record_pilot(p, "I001", decision="KEEP",
                                      reviewer="r",
                                      reviewer_kind="SYNTHETIC_FIXTURE",
                                      reason="x"),
                 lambda: record_delegation(p, delegate="d", scope=["ALL"],
                                           expires_at_ms=1 << 62),
                 lambda: approve_delivery(p, "B0001", "MASTER_CLEAN.mp4",
                                          approver=PRIMARY,
                                          reviewer_kind="HUMAN")):
        with pytest.raises(FilmError, match="LEGACY_MV"):
            call()
    assert autopilot(p)["stage"] == "NEEDS_MODEL_CHOICE"


def test_w00_status_cli(tmp_path, capsys):
    p = _prepared_w00(tmp_path)
    _keep_pilots(p)
    assert cli.main(["w00-status", str(p)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "w00_gate"
    assert out["gate"]["state"] == "BLOCKED"
    assert out["facets"]["release_state"] == "NOT_AUTHORIZED"
    _decide(p)
    assert cli.main(["w00-status", str(p)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["gate"]["state"] == "OPEN"
    # Errors surface with a nonzero code on a legacy project.
    (tmp_path / "legacy").mkdir()
    legacy = fixture_project(tmp_path / "legacy", seconds=2)
    assert cli.main(["w00-status", str(legacy)]) == 1
    assert "LEGACY_MV" in capsys.readouterr().err


def test_panel_import_needs_no_unix_only_module():
    """The frozen desktop self-test imports app.animation_ui on Windows,
    where the Unix-only `resource` module (used by perf_scheduler) does not
    exist. Importing the W00 gate must not pull perf_scheduler in."""
    root = Path(__file__).resolve().parents[1]
    code = ("import sys; sys.modules['resource'] = None\n"
            "import importlib\n"
            "for name in ('engine.w00_gate', 'app.animation_ui'):\n"
            "    importlib.import_module(name)\n"
            "assert 'engine.perf_scheduler' not in sys.modules\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=root,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
