"""film-shot-board: engine coverage of the cut workbench.

Read model parity with the integer frame clock, pairwise boundary/transition
ownership, draft-vs-adopted comparison with stale approval projection,
visual-only apply (lyrics/cues untouched), and selection keyed by stable
instance id across reorder + reload. All fixtures are synthetic Pillow PNGs
on a converted fixture project — every review/lock record written here is a
protocol fixture, never artwork approval; qualification UNQUALIFIED,
acceptance PENDING, release NOT_AUTHORIZED.
"""
import hashlib
import json
from pathlib import Path

import pytest

from engine.animation_assets import import_frame_sequence
from engine.animation_locks import lock_status, record_plan_lock
from engine.animation_review import (record_cut_review,
                                     record_transition_review, review_status)
from engine.animation_schema import read_canon, write_canon
from engine.core import FilmError, digest
from engine.shot_board import (apply_edit, board_entry, board_view,
                               boundary_window, propose_edit)
from test_anim_003 import animation_project, make_sequence

REVIEWER = "Shot board fixture reviewer"
APPROVER = "Shot board fixture approver"


def _project(tmp_path, shot_count=3, seconds=3, spare=6):
    """3 cuts of 24 frames; S001's adopted source has `spare` tail frames."""
    p = animation_project(tmp_path, shot_count=shot_count, seconds=seconds)
    for index in range(shot_count):
        shot = f"S{index + 1:03d}"
        count = 24 + spare if index == 0 else 24
        import_frame_sequence(
            p, shot,
            folder=make_sequence(tmp_path / f"seq{index}", count,
                                 seed=index * 100))
    return p


def _reorder_first_two(p):
    """Swap the first two timeline entries in place, keeping the doc valid."""
    path = p / "timeline/edit.json"
    document = read_canon(path)
    document["entries"][0], document["entries"][1] = \
        document["entries"][1], document["entries"][0]
    for left, right in zip(document["entries"], document["entries"][1:]):
        left["transition_out"]["to_instance"] = right["instance_id"]
    write_canon(path, document)
    return document


# --- read model + integer clock parity ----------------------------------------

def test_board_view_matches_integer_frame_clock(tmp_path):
    p = _project(tmp_path)
    view = board_view(p)
    assert view["fps"] == 24 and view["output_frames"] == 72
    assert view["coverage"] == {"uncovered_frames": 0,
                              "single_source_frames": 72,
                              "shared_frames": 0,
                              "frames_with_three_or_more": 0}
    first = board_entry(view, "I001")
    assert first["shot_id"] == "S001"
    assert first["output_range"] == [0, 24]
    assert first["display_range"] == [1, 24]          # 1-based, no drift
    assert first["exposure_window"] == {"start": [0, 1], "end": [1, 1]}
    assert first["pin"]["resolved"]
    assert first["pin"]["member_count"] == 30         # real spare extent
    assert first["unused_handles"] == {"before": 0, "after": 0}
    last = board_entry(view, "I003")
    assert last["output_range"] == [48, 72]
    assert last["transition_out"] is None
    assert view["transitions"][0]["display_range"] is None  # empty [24,24)
    boundary = view["transitions"][0]["boundary_frames"]
    assert boundary["before"]["frame_index"] == 23
    assert boundary["before"]["display"] == 24
    assert boundary["before"]["file"] == "F_000024.png"
    assert boundary["after"]["frame_index"] == 24
    assert boundary["after"]["file"] == "F_000025.png"


def test_boundary_window_owns_transition_frames(tmp_path):
    p = _project(tmp_path)
    draft = propose_edit(p, "I001", transition={
        "type": "CROSSFADE", "overlap_frames": 4, "fund": "OUTGOING"})
    assert draft["valid"], draft["errors"]
    apply_edit(p, draft)
    view = board_view(p)
    boundary = view["transitions"][0]
    assert boundary["type"] == "CROSSFADE"
    assert boundary["overlap_frames"] == 4
    assert boundary["output_range"] == [24, 28]
    # The record lives on the outgoing entry; the incoming op composes it.
    assert boundary["owner"] == "I001"
    assert boundary["boundary_frames"]["before"]["frame_index"] == 23
    assert boundary["boundary_frames"]["after"]["frame_index"] == 28
    window = boundary_window(p, 0)
    assert window["record_owner"] == "I001"
    assert window["overlap_op_owner"] == "I002"
    shared = [f for f in window["frames"] if 24 <= f["frame_index"] < 28]
    assert len(shared) == 4
    for k, frame in enumerate(shared):
        assert frame["op_owner"] == "I002"
        assert frame["transition"] == "T001"
        sides = {c["side"] for c in frame["contributors"]}
        assert sides == {"outgoing", "incoming"}
        weights = {c["instance_id"]: c["weight"]
                   for c in frame["contributors"]}
        # Linear interior over 4 frames: exact rationals, never floats.
        out_num, out_den = weights["I001"]
        in_num, in_den = weights["I002"]
        assert in_num * 5 == (k + 1) * in_den          # (k+1)/5 incoming
        assert out_num * 5 == (4 - k) * out_den        # (4-k)/5 outgoing
        assert out_num * in_den + in_num * out_den == out_den * in_den
    sole = window["frames"][0]
    assert sole["contributors"][0]["side"] == "sole"
    assert sole["frame_index"] == 22 and sole["file"] == "F_000023.png"


def test_boundary_window_margin_reads_neighbor_overlap(tmp_path):
    """`O_in + O_out == L` is legal: a cut can own zero exclusive frames.

    With two 12-frame crossfades around a 24-frame middle cut, T002's
    margin reaches inside T001's overlap, so the window's contributors name
    a third instance (I001) — the window must resolve it, not KeyError.
    """
    p = _project(tmp_path, spare=24)          # S001 carries 48 members
    path = p / "timeline/edit.json"
    document = read_canon(path)
    document["entries"][0]["used_source_range"] = [0, 48]
    for index in (0, 1):
        transition = document["entries"][index]["transition_out"]
        transition.update({"type": "CROSSFADE", "overlap_frames": 12,
                           "curve": "LINEAR_INTERIOR_V1"})
    # produced = 48 + 24 + 24 − 12 − 12 = 72, target_frames unchanged.
    write_canon(path, document)
    window = boundary_window(p, 1)
    assert window["transition"]["id"] == "T002"
    assert window["transition"]["output_range"] == [48, 60]
    assert window["boundary_frames"] == {"before": 47, "after": 60}
    by_frame = {f["frame_index"]: f for f in window["frames"]}
    for frame_index in (46, 47):              # inside T001's overlap [36,48)
        frame = by_frame[frame_index]
        assert frame["transition"] == "T001"
        contributors = {c["instance_id"]: c for c in frame["contributors"]}
        assert set(contributors) == {"I001", "I002"}
        # k = frame − 36 over O = 12: incoming weight (k+1)/13.
        k = frame_index - 36
        assert contributors["I002"]["weight"] == [k + 1, 13]
        assert contributors["I001"]["weight"] == [12 - k, 13]
        assert contributors["I001"]["member_path"]                      # resolved
        assert contributors["I002"]["side"] == "incoming"
    # I002 owns no exclusive frame — every frame it touches is overlapped —
    # and the trailing margin lands on I003's sole frames, also resolved.
    for frame_index in (60, 61):
        contributors = by_frame[frame_index]["contributors"]
        assert [c["instance_id"] for c in contributors] == ["I003"]
        assert contributors[0]["side"] == "sole"
        assert contributors[0]["member_path"]


# --- draft vs adopted + stale projection ----------------------------------------

def test_boundary_shift_diff_compares_adopted_and_draft(tmp_path):
    p = _project(tmp_path)
    before = (p / "timeline/edit.json").read_bytes()
    draft = propose_edit(p, "I001", boundary_shift=4)
    assert draft["valid"], draft["errors"]
    # Proposing alone never writes the canonical document.
    assert (p / "timeline/edit.json").read_bytes() == before
    changed = {row["instance_id"]: row for row in draft["diff"]}
    assert set(changed) == {"I001", "I002"}
    assert changed["I001"]["adopted"]["used_source_range"] == [0, 24]
    assert changed["I001"]["draft"]["used_source_range"] == [0, 28]
    assert changed["I001"]["adopted"]["output_range"] == [0, 24]
    assert changed["I001"]["draft"]["output_range"] == [0, 28]
    assert changed["I001"]["draft"]["unused_handles"]["after"] == 2
    assert changed["I002"]["draft"]["used_source_range"] == [4, 24]
    assert changed["I002"]["draft"]["unused_handles"]["before"] == 4
    assert draft["produced_frames"] == 72
    # ANIM-020 closure: exactly the moved boundary frames need recomposing.
    assert draft["closure"]["dirty_frames"] == [24, 25, 26, 27]
    assert draft["closure"]["output_frames"] == {"before": 72, "after": 72}


def test_apply_keeps_output_frames_and_stales_bound_reviews(tmp_path):
    p = _project(tmp_path)
    record_cut_review(p, "I001", reviewer=REVIEWER,
                      methods=["CUT_FULL_SPEED_PLAYBACK"])
    record_transition_review(p, "T001", reviewer=REVIEWER,
                             methods=["TRANSITION_FULL_SPEED_PLAYBACK"])
    record_plan_lock(p, APPROVER)
    assert review_status(p)["targets"]["I001"]["state"] == "CURRENT"
    assert lock_status(p)["plan"]["state"] == "CURRENT"
    draft = propose_edit(p, "I001", boundary_shift=4)
    # The projection names the exact scopes that will go stale.
    assert draft["projection"]["reviews"]["I001"]["after"] == "STALE"
    assert draft["projection"]["reviews"]["T001"]["after"] == "STALE"
    assert draft["projection"]["reviews"]["I003"]["after"] == "UNREVIEWED"
    assert draft["projection"]["locks"]["PLAN_LOCK"]["after"] == "STALE"
    result = apply_edit(p, draft)
    assert result["output_frames"] == 72
    assert "CUT:I001" in result["stale"]
    assert "TRANSITION:T001" in result["stale"]
    assert "PLAN_LOCK" in result["stale"]
    # The old records are evidence only — stale, never inherited approval.
    assert review_status(p)["targets"]["I001"]["state"] == "STALE"
    assert lock_status(p)["plan"]["state"] == "STALE"
    # The derived view was recomputed to the new edit.
    derived = read_canon(p / "timeline/derived.json")
    assert derived["entries"][0]["output_range"] == [0, 28]
    assert derived["entries"][1]["output_range"] == [28, 48]


def test_apply_refuses_a_stale_draft(tmp_path):
    p = _project(tmp_path)
    first = propose_edit(p, "I001", boundary_shift=4)
    assert first["valid"]
    second = propose_edit(p, "I001", boundary_shift=2)
    assert second["valid"], second["errors"]
    apply_edit(p, second)
    with pytest.raises(FilmError, match="타임라인이 바뀌었습니다"):
        apply_edit(p, first)


def test_apply_refuses_when_pins_move(tmp_path):
    p = _project(tmp_path)
    draft = propose_edit(p, "I001", boundary_shift=2)
    assert draft["valid"], draft["errors"]
    import_frame_sequence(  # a new adopted revision moves the pin
        p, "S003", folder=make_sequence(tmp_path / "seq3b", 24, seed=999))
    # Adoption rewrites sequence_revision inside edit.json, so the draft's
    # base-document guard refuses before anything lands.
    with pytest.raises(FilmError, match="초안을 만든 뒤"):
        apply_edit(p, draft)


def test_invalid_drafts_are_named_not_applied(tmp_path):
    p = _project(tmp_path)
    # Shift past S002's used range entirely.
    draft = propose_edit(p, "I001", boundary_shift=24)
    assert not draft["valid"]
    assert any("Transition overlap" in e or "at least one source frame" in e
               or "소스가 짧습니다" in e or "used range" in e
               for e in draft["errors"])
    with pytest.raises(FilmError):
        apply_edit(p, draft)
    # Overlap may not reach either neighbor's full used length.
    draft = propose_edit(p, "I001", transition={
        "type": "CROSSFADE", "overlap_frames": 24, "fund": "OUTGOING"})
    assert not draft["valid"]
    # Funding from a side that has no spare frames names the short source.
    draft = propose_edit(p, "I002", transition={
        "type": "CROSSFADE", "overlap_frames": 2, "fund": "OUTGOING"})
    assert not draft["valid"]
    assert any("소스가 짧습니다" in e for e in draft["errors"])
    # An unresolved cut cannot be edited blindly either.
    (tmp_path / "unresolved").mkdir()
    q = animation_project(tmp_path / "unresolved", shot_count=2, seconds=2)
    draft = propose_edit(q, "I001", boundary_shift=1)
    assert not draft["valid"]
    assert any("소스 여유분을 계산할 수 없습니다" in e for e in draft["errors"])


# --- lyric / manifest isolation -------------------------------------------------

def test_apply_never_moves_lyrics_or_manifest(tmp_path):
    p = _project(tmp_path)
    watched = ["input/lyrics.txt", "lyrics/lyrics_timed.json",
               "manifest/shots.json", "input/master.wav"]
    before = {rel: digest(p / rel) if (p / rel).is_file() else None
              for rel in watched}
    cues = json.loads((p / "lyrics/lyrics_timed.json").read_text())["cues"]
    cue_hash = hashlib.sha256(
        json.dumps(cues, sort_keys=True).encode()).hexdigest()
    draft = propose_edit(p, "I001", boundary_shift=4)
    apply_edit(p, draft)
    for rel, value in before.items():
        assert (digest(p / rel) if (p / rel).is_file() else None) == value
    cues_after = json.loads((p / "lyrics/lyrics_timed.json").read_text())["cues"]
    assert hashlib.sha256(json.dumps(
        cues_after, sort_keys=True).encode()).hexdigest() == cue_hash
    # The read model reports the same untouched fingerprints.
    view = board_view(p)
    assert view["lyrics"]["timing_sha256"] == before["lyrics/lyrics_timed.json"]
    assert view["lyrics"]["cues"] == len(cues)


# --- stable selection across reorder + reload -----------------------------------

def test_selection_identity_survives_reorder(tmp_path):
    p = _project(tmp_path)
    view = board_view(p)
    assert view["instances"] == ["I001", "I002", "I003"]
    assert board_entry(view, "I002")["shot_id"] == "S002"
    _reorder_first_two(p)
    view = board_view(p)          # a fresh read — the "reload"
    assert view["instances"] == ["I002", "I001", "I003"]
    picked = board_entry(view, "I002")
    assert picked["shot_id"] == "S002"          # identity, not position
    assert picked["position"] == 0
    assert picked["output_range"] == [0, 24]
    # The boundary now declared on I002 connects to its new follower.
    assert view["transitions"][0]["from_instance"] == "I002"
    assert view["transitions"][0]["to_instance"] == "I001"


def test_board_view_on_legacy_or_missing_timeline_raises(tmp_path):
    from test_compiler_v03 import fixture_project
    (tmp_path / "legacy").mkdir()
    legacy = fixture_project(tmp_path / "legacy", seconds=2, shot_count=2)
    with pytest.raises(FilmError):
        board_view(legacy)
    (tmp_path / "anim").mkdir()
    q = animation_project(tmp_path / "anim", shot_count=2, seconds=2)
    (q / "timeline/edit.json").write_text("{not canon")
    with pytest.raises(FilmError):
        board_view(q)
