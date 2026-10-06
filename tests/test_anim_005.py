"""ANIM-005: pairwise transitions, the frame_map rows and length audits.

Covers design 6.3-6.4/11.1/17 and schema sections 3 and 9: the
96 + 96 - 12 = 180 example, hard cuts, the final boundary, total-length and
three-cut overlap rejection, per-frame multi-source weight tracing, and the
5,760-frame four-minute coverage audit. All fixtures are synthetic; these
tests check the arithmetic and the schema shape only — no artwork review,
production qualification or build seal happens here.
"""
import copy
import json
from fractions import Fraction

import pytest
from PIL import Image

from engine import cli
from engine.animation_assets import import_frame_sequence
from engine.compiler import compile_preview
from engine.animation_schema import canon_bytes, write_canon
from engine.core import FilmError, digest, read, write
from engine.exposure import twos
from engine.frame_clock import frame_filename
from engine.frame_sequence import animation_validate
from engine.transitions import (LINEAR_INTERIOR, attach_frame_outputs,
                                audit_timeline, canon_weight, coverage_counts,
                                frame_contributors, frame_map_bytes,
                                pair_weights, plan_frame_map,
                                verify_frame_map, write_frame_map)
from test_anim_003 import animation_project, make_sequence, rgba_frame
from test_compiler_v03 import fixture_project, newest_build

SIZE = (64, 48)


def timeline_doc(lengths, overlaps=None, target=None):
    """A canonical-shape animation_timeline; target defaults to the arithmetic."""
    entries = []
    for i, length in enumerate(lengths):
        entries.append({"instance_id": f"I{i + 1:03d}",
                        "shot_id": f"S{i + 1:03d}",
                        "sequence_revision": 1,
                        "used_source_range": [0, length],
                        "unused_handles": {"before": 0, "after": 0},
                        "transition_out": None})
    overlaps = [0] * (len(lengths) - 1) if overlaps is None else overlaps
    for i, overlap in enumerate(overlaps):
        entries[i]["transition_out"] = {
            "id": f"T{i + 1:03d}",
            "type": "CROSSFADE" if overlap else "HARD_CUT",
            "to_instance": entries[i + 1]["instance_id"],
            "overlap_frames": overlap,
            **({"curve": "LINEAR_INTERIOR_V1"} if overlap else {})}
    return {"document_type": "animation_timeline", "schema_version": 1,
            "target_frames": sum(lengths) - sum(overlaps)
            if target is None else target,
            "entries": entries}


def project_180(tmp_path):
    """Two-shot converted project retargeted to the doc's 180-frame example."""
    p = animation_project(tmp_path, shot_count=2, seconds=8)
    document = timeline_doc([96, 96], [12])
    document["entries"][0]["unused_handles"] = {"before": 0, "after": 12}
    write_canon(p / "timeline/edit.json", document)
    config = read(p / "project.yaml")
    config["animation"]["output_frames"] = 180
    write(p / "project.yaml", config)
    make_sequence(tmp_path / "seq_s001", count=108, size=SIZE, seed=10)
    make_sequence(tmp_path / "seq_s002", count=96, size=SIZE, seed=200)
    import_frame_sequence(p, "S001", folder=tmp_path / "seq_s001")
    import_frame_sequence(p, "S002", folder=tmp_path / "seq_s002")
    return p, document


def project_hard_cut(tmp_path):
    """The default converted project: two 96-frame HARD_CUT entries."""
    p = animation_project(tmp_path, shot_count=2, seconds=8)
    make_sequence(tmp_path / "seq_s001", count=96, size=SIZE, seed=10)
    make_sequence(tmp_path / "seq_s002", count=96, size=SIZE, seed=200)
    import_frame_sequence(p, "S001", folder=tmp_path / "seq_s001")
    import_frame_sequence(p, "S002", folder=tmp_path / "seq_s002")
    return p


def test_linear_interior_weights():
    assert pair_weights(0, 1) == (Fraction(1, 2), Fraction(1, 2))
    assert pair_weights(0, 2) == (Fraction(2, 3), Fraction(1, 3))
    assert pair_weights(1, 2) == (Fraction(1, 3), Fraction(2, 3))
    # The doc example: position 5 of a 12-frame overlap -> out 7/13, in 6/13.
    assert pair_weights(5, 12) == (Fraction(7, 13), Fraction(6, 13))
    for bad in ((-1, 12), (12, 12), (0, 0), (0, -3), (0.5, 12), ("0", 12)):
        with pytest.raises(FilmError):
            pair_weights(*bad)


def test_weight_pairs_reduce_canonically():
    assert canon_weight(Fraction(2, 4)) == [1, 2]
    assert canon_weight(Fraction(1)) == [1, 1]
    doc = timeline_doc([10, 10], [3])
    audit = audit_timeline(doc)
    # Overlap [7, 10): position 1 gives the reduced pair 2/4 -> [1, 2].
    pairs, transition = frame_contributors(audit, 8)
    assert transition["output_range"] == [7, 10]
    assert [w for _, w in pairs] == [Fraction(1, 2), Fraction(1, 2)]
    assert [canon_weight(w) for _, w in pairs] == [[1, 2], [1, 2]]


def test_180_example_frame_map_rows(tmp_path):
    p, document = project_180(tmp_path)
    plan = plan_frame_map(p)
    rows = plan["rows"]
    assert plan["state"] == "DRAFT" and plan["output_frames"] == 180
    assert len(rows) == 180
    assert plan["audit"]["used_source_frames"] == 192
    assert plan["audit"]["overlap_frames"] == 12
    # The doc example frame: 89 -> S001 local 89 @ 7/13 + S002 local 5 @ 6/13.
    row = rows[89]
    assert row["frame_index"] == 89
    assert row["file"] == "final_frames/F_000090.png"
    assert row["output_sha256"] is None
    assert row["sources"] == [
        {"instance_id": "I001", "shot_id": "S001", "local_frame_index": 89,
         "sequence_revision": 1, "weight": [7, 13]},
        {"instance_id": "I002", "shot_id": "S002", "local_frame_index": 5,
         "sequence_revision": 1, "weight": [6, 13]}]
    assert row["operations"] == [{"type": "CROSSFADE", "transition_id": "T001",
                                  "recipe": LINEAR_INTERIOR}]
    assert row["review_refs"] == []
    # First overlap frame: outgoing 12/13, incoming 1/13; frame before is solo.
    assert [s["weight"] for s in rows[84]["sources"]] == [[12, 13], [1, 13]]
    assert rows[83]["sources"] == [
        {"instance_id": "I001", "shot_id": "S001", "local_frame_index": 83,
         "sequence_revision": 1, "weight": [1, 1]}]
    # Last boundary: output 179 is S002 local 95 alone at F_000180.png.
    assert rows[179]["file"] == "final_frames/F_000180.png"
    assert rows[179]["sources"] == [
        {"instance_id": "I002", "shot_id": "S002", "local_frame_index": 95,
         "sequence_revision": 1, "weight": [1, 1]}]
    assert rows[179]["operations"] == []
    # Exactly the shared overlap frames carry two sources summing to 1.
    assert [i for i, r in enumerate(rows) if len(r["sources"]) == 2] \
        == list(range(84, 96))
    for r in rows:
        assert sum(Fraction(*s["weight"]) for s in r["sources"]) == 1
    report = verify_frame_map(rows, document)
    assert report["verified"] and report["frames"] == 180
    assert report["shared_frames"] == 12


def test_frame_map_canonical_lines(tmp_path):
    p, _ = project_180(tmp_path)
    rows = plan_frame_map(p)["rows"]
    raw = frame_map_bytes(rows)
    lines = raw.split(b"\n")
    assert lines[-1] == b"" and len(lines) == 181
    for line, row in zip(lines, rows):
        assert line + b"\n" == canon_bytes(row)
        assert b"\r" not in line
    path = tmp_path / "frame_map.jsonl"
    write_frame_map(path, rows)
    assert path.read_bytes() == raw
    for line, row in zip(path.read_text().splitlines(), rows):
        assert json.loads(line) == row


def test_hard_cut_boundary(tmp_path):
    p = project_hard_cut(tmp_path)
    plan = plan_frame_map(p)
    rows = plan["rows"]
    assert plan["output_frames"] == 192 and len(rows) == 192
    assert all(len(r["sources"]) == 1 for r in rows)
    assert all(r["operations"] == [] for r in rows)
    # The cut is at output frame 96: last S001 member 95, then S002 member 0.
    assert rows[95]["sources"][0]["local_frame_index"] == 95
    assert rows[95]["sources"][0]["shot_id"] == "S001"
    assert rows[96]["sources"][0] == {
        "instance_id": "I002", "shot_id": "S002", "local_frame_index": 0,
        "sequence_revision": 1, "weight": [1, 1]}
    assert rows[191]["sources"][0]["local_frame_index"] == 95
    assert plan["audit"]["coverage"] == {
        "uncovered_frames": 0, "single_source_frames": 192,
        "shared_frames": 0, "frames_with_three_or_more": 0}
    document = read(p / "timeline/edit.json")
    assert verify_frame_map(rows, document)["shared_frames"] == 0


def test_last_entry_must_not_transition():
    doc = timeline_doc([96, 96], [12])
    doc["entries"][-1]["transition_out"] = {
        "id": "T099", "type": "HARD_CUT", "to_instance": "I999",
        "overlap_frames": 0}
    with pytest.raises(FilmError, match="null transition_out"):
        audit_timeline(doc)


def test_total_mismatch_rejected(tmp_path):
    # Claimed target vs. the sum(lengths) - sum(overlaps) arithmetic.
    doc = timeline_doc([96, 96], [12], target=181)
    with pytest.raises(FilmError, match="target_frames"):
        audit_timeline(doc)
    # A project demanding more frames than the edit produces.
    doc = timeline_doc([96, 96], [12])
    with pytest.raises(FilmError, match="output_frames"):
        audit_timeline(doc, output_frames=179)
    # Project-level: the timeline says 180, output_frames still says 192.
    p, _ = project_180(tmp_path)
    config = read(p / "project.yaml")
    config["animation"]["output_frames"] = 192
    write(p / "project.yaml", config)
    with pytest.raises(FilmError, match="output_frames"):
        plan_frame_map(p)


def test_three_cut_overlap_rejected():
    # O0 + O1 = 5 > L_middle 4: frame triple coverage is refused outright.
    doc = timeline_doc([10, 4, 10], [2, 3])
    with pytest.raises(FilmError, match="Adjacent overlaps"):
        audit_timeline(doc)
    doc = timeline_doc([10, 4, 10], [2, 2], target=999)
    with pytest.raises(FilmError):
        audit_timeline(doc)


def test_boundary_equal_overlaps_are_pairwise():
    # O0 + O1 == L_middle keeps every frame at <= two sources.
    doc = timeline_doc([10, 4, 10], [2, 2])
    audit = audit_timeline(doc)
    assert audit["output_frames"] == 20
    counts = [len(frame_contributors(audit, i)[0]) for i in range(20)]
    assert max(counts) == 2 and counts.count(2) == 4
    pairs, _ = frame_contributors(audit, 8)
    assert [r["instance_id"] for r, _ in pairs] == ["I001", "I002"]
    pairs, _ = frame_contributors(audit, 10)
    assert [r["instance_id"] for r, _ in pairs] == ["I002", "I003"]
    assert [w for _, w in pairs] == [Fraction(2, 3), Fraction(1, 3)]


def test_forged_triple_layout_fails_at_frame_level():
    # A hand-forged layout bypassing document validation is still refused.
    layout = {
        "output_frames": 10,
        "entries": [
            {"instance_id": "I001", "shot_id": "S001", "output_range": [0, 6]},
            {"instance_id": "I002", "shot_id": "S002", "output_range": [4, 8]},
            {"instance_id": "I003", "shot_id": "S003", "output_range": [5, 10]}],
        "transitions": [
            {"id": "T1", "type": "CROSSFADE", "from_instance": "I001",
             "to_instance": "I002", "overlap_frames": 2,
             "output_range": [4, 6]},
            {"id": "T2", "type": "CROSSFADE", "from_instance": "I002",
             "to_instance": "I003", "overlap_frames": 3,
             "output_range": [5, 8]}]}
    assert coverage_counts(layout)[5] == 3
    with pytest.raises(FilmError, match="two transition"):
        frame_contributors(layout, 5)


def test_multi_source_weights_track_members(tmp_path):
    p, document = project_180(tmp_path)
    plan = plan_frame_map(p)
    rows = plan["rows"]
    # Every shared frame lists exactly the two expected members.
    for index in range(84, 96):
        sources = rows[index]["sources"]
        assert [s["shot_id"] for s in sources] == ["S001", "S002"]
        assert sources[0]["local_frame_index"] == index
        assert sources[1]["local_frame_index"] == index - 84
        out_w, in_w = pair_weights(index - 84, 12)
        assert sources[0]["weight"] == canon_weight(out_w)
        assert sources[1]["weight"] == canon_weight(in_w)


def test_exposure_schedule_shifts_member_index(tmp_path):
    p, document = project_180(tmp_path)
    plan = plan_frame_map(p, schedules={"I001": [twos("character", 0, 96)]})
    rows = plan["rows"]
    # Twos expose drawing 2k at slots 2k/2k+1: local 83 -> member 82.
    assert rows[83]["sources"][0]["local_frame_index"] == 82
    # Inside the transition the weight pair is unchanged; the member follows
    # the exposure map (89 -> 88) while I002 stays on ones.
    sources = rows[89]["sources"]
    assert sources[0]["local_frame_index"] == 88
    assert sources[0]["weight"] == [7, 13]
    assert sources[1]["local_frame_index"] == 5
    report = verify_frame_map(rows, document)
    assert report["verified"]


def test_5760_frame_coverage_audit():
    """S240-A: 60 entries, 5,904 used minus 144 overlap = 5,760 @24fps."""
    lengths = [96] * 48 + [108] * 12
    overlaps = [0] * 47 + [12] * 12
    doc = timeline_doc(lengths, overlaps)
    assert doc["target_frames"] == 5760
    audit = audit_timeline(doc)
    assert audit["output_frames"] == 5760
    assert audit["used_source_frames"] == 5904
    assert audit["overlap_frames"] == 144
    assert audit["coverage"] == {"uncovered_frames": 0,
                               "single_source_frames": 5616,
                               "shared_frames": 144,
                               "frames_with_three_or_more": 0}
    two_source = 0
    for index in range(5760):
        pairs, _ = frame_contributors(audit, index)
        assert 1 <= len(pairs) <= 2
        assert sum(w for _, w in pairs) == 1
        two_source += len(pairs) == 2
    assert two_source == 144
    # First and last frames: S001 alone, then S060's local 107 alone.
    first, transition = frame_contributors(audit, 0)
    assert transition is None and first[0][0]["shot_id"] == "S001"
    last, transition = frame_contributors(audit, 5759)
    assert transition is None and last[0][0]["shot_id"] == "S060"
    assert audit["entries"][59]["output_range"] == [5652, 5760]
    # T048 covers [4596, 4608); both neighbouring frames are single-source.
    transition = next(t for t in audit["transitions"] if t["id"] == "T048")
    assert transition["output_range"] == [4596, 4608]
    assert frame_contributors(audit, 4595)[1] is None
    assert [w for _, w in frame_contributors(audit, 4596)[0]] \
        == [Fraction(12, 13), Fraction(1, 13)]
    assert [w for _, w in frame_contributors(audit, 4607)[0]] \
        == [Fraction(1, 13), Fraction(12, 13)]
    assert len(frame_contributors(audit, 4608)[0]) == 1


def test_5616_rejected_against_5760_target():
    """S240-B: 60 x 96 used minus 144 overlap produces 5,616, not 5,760."""
    doc = timeline_doc([96] * 60, [0] * 47 + [12] * 12)
    audit = audit_timeline(doc)
    assert audit["output_frames"] == 5616
    doc["target_frames"] = 5760
    with pytest.raises(FilmError, match="target_frames"):
        audit_timeline(doc)
    doc["target_frames"] = 5616
    with pytest.raises(FilmError, match="output_frames"):
        audit_timeline(doc, output_frames=5760)


def test_unresolved_entry_blocks_frame_map(tmp_path):
    p = animation_project(tmp_path, shot_count=2, seconds=8)
    make_sequence(tmp_path / "seq_s001", count=96, size=SIZE)
    import_frame_sequence(p, "S001", folder=tmp_path / "seq_s001")
    with pytest.raises(FilmError, match="I002"):
        plan_frame_map(p)


def test_verify_frame_map_rejections(tmp_path):
    p, document = project_180(tmp_path)
    rows = plan_frame_map(p)["rows"]

    tampered = copy.deepcopy(rows)
    tampered[89]["sources"][0]["weight"] = [6, 13]
    with pytest.raises(FilmError, match="sum to 1"):
        verify_frame_map(tampered, document)

    tampered = copy.deepcopy(rows)
    tampered[89]["sources"][0]["weight"] = [14, 26]  # same value, unreduced
    with pytest.raises(FilmError, match="reduced"):
        verify_frame_map(tampered, document)

    tampered = copy.deepcopy(rows)
    tampered[89]["sources"][0]["weight"] = [0, 13]
    with pytest.raises(FilmError, match="positive"):
        verify_frame_map(tampered, document)

    tampered = copy.deepcopy(rows)
    del tampered[89]["sources"][1]  # drops a real contributor
    with pytest.raises(FilmError):
        verify_frame_map(tampered, document)

    tampered = copy.deepcopy(rows)
    tampered[89]["sources"].reverse()  # wrong contributor order
    with pytest.raises(FilmError, match="contributors"):
        verify_frame_map(tampered, document)

    tampered = copy.deepcopy(rows)
    tampered[89]["sources"][0]["local_frame_index"] = 96  # outside used range
    with pytest.raises(FilmError, match="used range"):
        verify_frame_map(tampered, document)

    tampered = copy.deepcopy(rows)
    tampered[10]["file"] = "final_frames/F_000999.png"
    with pytest.raises(FilmError, match="wrong output file"):
        verify_frame_map(tampered, output_frames=180)

    tampered = copy.deepcopy(rows)
    tampered[5]["extra"] = 1
    with pytest.raises(FilmError, match="schema section 9"):
        verify_frame_map(tampered, output_frames=180)

    tampered = copy.deepcopy(rows)
    tampered[20]["operations"] = [{"type": "CROSSFADE",
                                   "transition_id": "T001",
                                   "recipe": LINEAR_INTERIOR}]
    with pytest.raises(FilmError, match="CROSSFADE"):
        verify_frame_map(tampered, document)

    with pytest.raises(FilmError, match="rows for"):
        verify_frame_map(rows[:-1], document)
    with pytest.raises(FilmError, match="no output hash"):
        verify_frame_map(rows, document, complete=True)
    with pytest.raises(FilmError, match="document or output_frames"):
        verify_frame_map(rows)


def test_attach_frame_outputs_and_complete_verify(tmp_path):
    p, document = project_180(tmp_path)
    rows = plan_frame_map(p)["rows"]
    frames = tmp_path / "final_frames"
    frames.mkdir()
    for i in range(180):
        rgba_frame(frames / frame_filename(i), size=SIZE, seed=i)
    assert attach_frame_outputs(rows, frames) == 180
    assert all(len(r["output_sha256"]) == 64 for r in rows)
    report = verify_frame_map(rows, document, complete=True)
    assert report["verified"] and report["complete"] and report["frames"] == 180
    # The recorded hash binds the bytes: a changed member rehashes differently.
    before = rows[5]["output_sha256"]
    rgba_frame(frames / "F_000006.png", size=SIZE, seed=999)
    attach_frame_outputs(rows, frames)
    assert rows[5]["output_sha256"] != before
    (frames / "F_000007.png").unlink()
    with pytest.raises(FilmError, match="Missing composed frame"):
        attach_frame_outputs(rows, frames)


def test_draft_preview_uses_the_same_transition_math(tmp_path):
    p, _ = project_180(tmp_path)
    result = compile_preview(p)
    assert result["status"] == "COMPLETE"
    folder, record = newest_build(p)
    assert record["frames"]["total"] == 180
    # The Build 2 frame_map.jsonl is an ANIM-006 artifact; the draft map stays.
    assert not (folder / "frame_map.jsonl").exists()
    lines = (folder / "draft_frame_map.jsonl").read_text().splitlines()
    assert len(lines) == 180
    sources = json.loads(lines[89])["sources"]
    assert [s["weight"] for s in sources] == [[7, 13], [6, 13]]
    assert sources[0]["local_frame_index"] == 89
    assert sources[1]["local_frame_index"] == 5
    # The composed pixel is the same rational blend the frame_map claims.
    snap = folder / "snapshot/animation/assets"
    with Image.open(snap / "A0001/r1/f000089.png") as im:
        outgoing = im.convert("RGB").getpixel((8, 6))
    with Image.open(snap / "A0002/r1/f000005.png") as im:
        incoming = im.convert("RGB").getpixel((8, 6))
    with Image.open(folder / "draft_frames/F_000090.png") as im:
        blended = im.convert("RGB").getpixel((128 + 8, 96 + 6))
    for left, right, got in zip(outgoing, incoming, blended):
        assert abs(got - round(left * 7 / 13 + right * 6 / 13)) <= 2


def test_animation_validate_reports_transition_audit(tmp_path):
    p, _ = project_180(tmp_path)
    report = animation_validate(p)
    assert report["ok"] is True
    assert report["timeline"]["output_frames"] == 180
    assert report["timeline"]["used_source_frames"] == 192
    assert report["timeline"]["overlap_frames"] == 12
    assert report["timeline"]["coverage"]["shared_frames"] == 12
    assert report["timeline"]["transitions"][0]["output_range"] == [84, 96]
    assert cli.main(["animation-validate", str(p)]) == 0


def test_legacy_project_rejects_frame_map(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1)
    with pytest.raises(FilmError, match="LEGACY_MV"):
        plan_frame_map(p)


def test_audio_lyrics_and_cues_untouched(tmp_path):
    p, _ = project_180(tmp_path)
    config = read(p / "project.yaml")
    names = ["analysis/audio.json", "lyrics/lyrics_timed.json",
             "lyrics/lyrics_source.txt", "manifest/shots.json",
             config["audio"]["path"]]
    before = {n: digest(p / n) for n in names if (p / n).is_file()}
    plan_frame_map(p)
    compile_preview(p)
    for n, sha in before.items():
        assert digest(p / n) == sha
