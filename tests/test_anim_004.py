"""ANIM-004: 24fps frame clock, per-layer ones/twos exposure, sequence normalization.

All fixtures are synthetic (Pillow PNGs, small integer frame counts); nothing
here is an artistic approval, a production qualification or a Final.
"""
import json
from fractions import Fraction
from pathlib import Path

import pytest

from engine import cli
from engine.animation_schema import read_canon
from engine.core import FilmError, digest, read
from engine.exposure import (adopt_segment_output, expand, expand_track, hold,
                             ones, schedule_digest, twos, validate_schedule,
                             validate_track)
from engine.frame_clock import (check_fps, display_number, exposure_window,
                                frame_duration, frame_filename, frame_index_of,
                                frames_for_duration, fps_rational,
                                last_exposure_end, pts_end, pts_rational,
                                pts_start)
from engine.animation_assets import import_frame_sequence
from engine.animation_migrate import animation_init
from engine.compiler import compile_preview
from engine.frame_sequence import (animation_validate, map_source_frames,
                                   member_map_for_entry,
                                   normalize_shot_sequence)
from test_anim_003 import animation_project, make_sequence, rgba_frame
from test_compiler_v03 import fixture_project, newest_build


# --- frame clock (design 6.1): integer frames, rational PTS, 24fps ---------

def test_clock_24fps_rational_pts():
    assert fps_rational(24) == {"num": 24, "den": 1}
    assert pts_start(0, 24) == 0
    assert pts_start(1, 24) == Fraction(1, 24)
    assert pts_start(24, 24) == 1
    assert pts_end(23, 24) == 1
    assert pts_rational(48, 24) == {"num": 2, "den": 1}
    assert pts_rational(7, 24) == {"num": 7, "den": 24}
    assert frame_duration(24) == Fraction(1, 24)
    # Exact rational window: 96 frames at 24fps is exactly 4 seconds.
    assert frames_for_duration(4, 24) == 96
    assert frames_for_duration(Fraction(4, 1), 24) == 96
    assert last_exposure_end(96, 24) == 4
    window = exposure_window(94, 24)
    assert window == {"start": {"num": 47, "den": 12},
                      "end": {"num": 95, "den": 24}}
    # Sub-frame durations are rejected, never rounded.
    with pytest.raises(FilmError, match="frame boundary"):
        frames_for_duration(Fraction(1, 48), 24)
    for bad in (0, -1, Fraction(48, 2), "24", 24.0):
        with pytest.raises(FilmError, match="fps"):
            check_fps(bad)


def test_clock_display_numbering_and_filenames():
    assert display_number(0) == 1 and display_number(95) == 96
    assert frame_filename(0) == "F_000001.png"
    assert frame_filename(4095) == "F_004096.png"
    assert frame_index_of("F_000001.png") == 0
    assert frame_index_of("F_000180.png") == 179
    for bad in ("f_000001.png", "F_1.png", "F_000001.jpg", "000001.png", ""):
        with pytest.raises(FilmError, match="F_000001"):
            frame_index_of(bad)
    with pytest.raises(FilmError):
        display_number(-1)


# --- exposure (design 7): ones/twos, coverage, anchors, odd ends ------------

def test_ones_exposes_every_frame():
    schedule = ones("character", 0, 4, [0, 1, 2, 3])
    info = validate_schedule(schedule)
    assert info["frames"] == 4 and info["stride"] == 1
    rows = expand(schedule)
    assert [r["frame"] for r in rows] == [0, 1, 2, 3]
    assert [r["drawing"] for r in rows] == [0, 1, 2, 3]
    assert not any(r["hold"] for r in rows)


def test_twos_96_frames_full_coverage():
    """The design's example: a 96-frame cut on twos."""
    schedule = twos("character", 0, 96)
    info = validate_schedule(schedule)
    assert info["frames"] == 96 and info["slots"] == 48
    rows = expand(schedule)
    assert len(rows) == 96
    assert [r["frame"] for r in rows] == list(range(96))
    # Drawing 94 is exposed on output frames 94 and 95 — no overrun, no gap.
    assert rows[94]["drawing"] == 94 and rows[94]["hold"] is False
    assert rows[95]["drawing"] == 94 and rows[95]["hold"] is True
    assert rows[95]["frame"] == 95 and rows[-1]["frame"] == 95
    assert {r["slot"] for r in rows} == set(range(48))
    # Each drawing is exposed exactly twice, in pair order.
    assert [r["drawing"] for r in rows] == [i // 2 * 2 for i in range(96)]


def test_camera_track_ones_with_state():
    """Camera layer runs on ones with a state per frame, never a drawing."""
    states = [{"pan_x": i, "pan_y": 0} for i in range(96)]
    schedule = ones("camera", 0, 96, states)
    info = validate_schedule(schedule)
    assert info["frames"] == 96 and info["distinct_drawings"] == 0
    rows = expand(schedule)
    assert [r["frame"] for r in rows] == list(range(96))
    assert rows[94]["state"] == {"pan_x": 94, "pan_y": 0}
    assert all(r["drawing"] is None for r in rows)


def test_odd_end_tail_is_one_frame_not_overrun():
    schedule = twos("character", 0, 5, odd_end=True)
    rows = expand(schedule)
    assert [r["frame"] for r in rows] == [0, 1, 2, 3, 4]
    last = schedule["slots"][-1]
    assert last == {"start": 4, "end": 5, "drawing": 4, "state": None,
                    "partial": "ODD_END"}
    # Without the explicit flag the odd tail is refused, not silently made.
    with pytest.raises(FilmError, match="odd-length"):
        twos("character", 0, 5)


def test_phase_one_twos_leads_with_single_frame():
    schedule = twos("character", 0, 5, phase=1)
    rows = expand(schedule)
    assert [r["frame"] for r in rows] == list(range(5))
    assert schedule["slots"][0] == {"start": 0, "end": 1, "drawing": 0,
                                    "state": None, "partial": "PHASE"}
    with pytest.raises(FilmError, match="phase"):
        twos("character", 0, 6, phase=2)


def test_shared_anchor_duplicate_is_blocked():
    """Adjacent slots repeating a drawing must declare the shared anchor."""
    duplicate = twos("character", 0, 4)
    duplicate["slots"][1]["drawing"] = 0  # same as slot 0's drawing
    with pytest.raises(FilmError, match="shared anchor"):
        validate_schedule(duplicate)
    # Marked continued, the repeated drawing is one anchor owned once.
    duplicate["slots"][1]["continued"] = True
    rows = expand(duplicate)
    assert [r["drawing"] for r in rows] == [0, 0, 0, 0]
    assert [r["continued"] for r in rows] == [False, False, True, True]


def test_shared_anchor_across_schedule_boundary():
    first = twos("character", 0, 4)
    second = twos("character", 4, 8)
    second["slots"][0]["drawing"] = 2  # same drawing as the previous tail
    with pytest.raises(FilmError, match="duplicated across"):
        validate_track([first, second], 8)
    second["slots"][0]["continued"] = True
    assert validate_track([first, second], 8)["slots"] == 4
    # 'continued' cannot invent a different drawing at the boundary.
    second["slots"][0]["drawing"] = 99
    with pytest.raises(FilmError, match="keep the previous"):
        validate_track([first, second], 8)


def test_empty_slot_is_rejected():
    schedule = ones("character", 0, 3, [0, None, 2])
    with pytest.raises(FilmError, match="Empty exposure slots"):
        validate_schedule(schedule)
    # Even expanded-shape forged slots with neither drawing nor state fail.
    forged = ones("character", 0, 2, [0, 1])
    forged["slots"][1] = {"start": 1, "end": 2}
    with pytest.raises(FilmError, match="Empty exposure"):
        validate_schedule(forged)


def test_coverage_gaps_and_overruns_are_rejected():
    schedule = ones("character", 0, 3, [0, 1, 2])
    gapped = dict(schedule, slots=[schedule["slots"][0], schedule["slots"][2]])
    with pytest.raises(FilmError, match="without gaps"):
        validate_schedule(gapped)
    over = ones("character", 0, 2, [0, 1])
    over["slots"][1]["end"] = 5
    with pytest.raises(FilmError, match="exceeds|end exactly"):
        validate_schedule(over)
    # A track must tile [0, length) exactly.
    a, b = twos("character", 0, 4), twos("character", 6, 8)
    with pytest.raises(FilmError, match="without gaps"):
        validate_track([a, b], 8)
    with pytest.raises(FilmError, match="end exactly"):
        validate_track([twos("character", 0, 4)], 6)


def test_undeclared_hold_and_transform_rejection():
    schedule = twos("character", 0, 4)
    schedule["slots"][0]["end"] = 3  # 3-frame slot on a twos track
    schedule["slots"][1]["start"] = 3
    with pytest.raises(FilmError, match="declared hold"):
        validate_schedule(schedule)
    fixed = hold("character", 0, 3, drawing=7)
    assert expand(fixed)[2]["hold"] is True
    # A slot cannot be both a drawing and a state.
    both = ones("character", 0, 2, [0, 1])
    both["slots"][0]["state"] = {"pan": 0}
    with pytest.raises(FilmError, match="both a drawing and a state"):
        validate_schedule(both)


def test_adopt_segment_output_drops_duplicate_endpoint():
    result = adopt_segment_output([10, 11, 12, 13, 14], 0, 4)
    assert result["drawings"] == [10, 11, 12, 13]
    assert result["dropped_anchor"] == 14
    exact = adopt_segment_output([10, 11, 12, 13], 0, 4)
    assert exact["dropped_anchor"] is None
    with pytest.raises(FilmError, match="short output"):
        adopt_segment_output([10, 11], 0, 4)
    with pytest.raises(FilmError, match="only one shared"):
        adopt_segment_output([1, 2, 3, 4, 5, 6], 0, 4)


def test_schedule_digest_is_canonical_and_stable():
    a = twos("character", 0, 4)
    b = twos("character", 0, 4)
    assert schedule_digest(a) == schedule_digest(b)
    b["slots"][0]["drawing"] = 9
    assert schedule_digest(a) != schedule_digest(b)


# --- sequence normalization onto the frame clock ---------------------------

def animated_project_with_96(tmp_path):
    """Two 96-frame timeline entries; a 96-member sequence pinned to S001."""
    p = animation_project(tmp_path, shot_count=2, seconds=8)
    folder = tmp_path / "seq96"
    folder.mkdir()
    for i in range(96):
        rgba_frame(folder / f"f{i:04d}.png", size=(64, 48), seed=i)
    result = import_frame_sequence(p, "S001", folder=folder)
    return p, result


def test_identity_member_map_is_ones():
    entry = {"used_source_range": [0, 96], "unused_handles": {"before": 0,
                                                              "after": 0}}
    mapping = member_map_for_entry(entry)
    assert len(mapping) == 96
    assert [m["member"] for m in mapping] == list(range(96))
    assert mapping[4]["local"] == 4 and mapping[4]["slot"] == 4


def test_twos_member_map_on_96_frame_cut(tmp_path):
    p, _ = animated_project_with_96(tmp_path)
    schedule = twos("character", 0, 96)
    result = normalize_shot_sequence(p, "S001", schedules=[schedule])
    assert result["state"] == "DRAFT" and result["fps"] == 24
    entry = result["entries"][0]
    assert entry["frames"] == 96 and entry["used_source_range"] == [0, 96]
    members = [m["member"] for m in entry["member_map"]]
    assert members == [i // 2 * 2 for i in range(96)]
    assert entry["member_map"][94]["member"] == 94
    assert entry["member_map"][95]["member"] == 94
    assert entry["member_map"][95]["hold"] is True
    # Each normalized frame sits on the exact 24fps clock.
    assert entry["exposure_window"][95] == {
        "start": {"num": 95, "den": 24}, "end": {"num": 4, "den": 1}}
    assert len(entry["normalized_sha256"]) == 64
    # The pin reports the hash-bound revision the map was normalized from.
    timeline = read_canon(p / "timeline/edit.json")
    adopted = timeline["entries"][0]["sequence_revision"]
    assert result["pin"]["revision"] == adopted


def test_mixed_exposure_track_with_camera(tmp_path):
    p, _ = animated_project_with_96(tmp_path)
    drawing_track = [twos("character", 0, 96)]
    camera = [ones("camera", 0, 96, [{"zoom": i} for i in range(96)])]
    result = normalize_shot_sequence(p, "S001", schedules=drawing_track)
    assert [m["member"] for m in result["entries"][0]["member_map"]][:4] == \
        [0, 0, 2, 2]
    camera_rows = expand_track(camera, 96)
    assert all(r["state"] is not None and r["drawing"] is None
               for r in camera_rows)


def test_normalize_rejects_member_outside_used_range(tmp_path):
    p, _ = animated_project_with_96(tmp_path)
    schedule = twos("character", 0, 96)
    schedule["slots"][-1]["drawing"] = 96  # outside [0, 96)
    with pytest.raises(FilmError, match="outside the declared used range"):
        normalize_shot_sequence(p, "S001", schedules=[schedule])
    # Handles stay unused: a drawing below `start` is refused too.
    entry = {"used_source_range": [4, 96],
             "unused_handles": {"before": 4, "after": 0}}
    short = twos("character", 0, 92)
    short["slots"][0]["drawing"] = 3
    with pytest.raises(FilmError, match="outside the declared used range"):
        member_map_for_entry(entry, [short])


def test_normalize_unassigned_and_unresolved_shot(tmp_path):
    p = animation_project(tmp_path, shot_count=1)
    with pytest.raises(FilmError, match="no assigned|Unassigned|resolve"):
        normalize_shot_sequence(p, "S001")
    with pytest.raises(FilmError, match="no timeline entry"):
        normalize_shot_sequence(p, "S999")


def test_animation_validate_reports_resolution(tmp_path):
    p, _ = animated_project_with_96(tmp_path)
    report = animation_validate(p)
    assert report["target_frames"] == 192
    by_shot = {e["shot_id"]: e for e in report["entries"]}
    assert by_shot["S001"]["resolved"] is True
    assert by_shot["S001"]["members"] == 96
    assert by_shot["S002"]["resolved"] is False
    assert report["ok"] is False and report["unresolved"]
    assert "no review" in report["note"]


def test_map_source_frames_fps_normalization():
    # 30fps provider output mapped onto the 24fps output clock.
    mapped = map_source_frames(30, 24, 48, source_count=120)
    assert mapped["rule"] == "FLOOR_CONTAINMENT_V1"
    assert mapped["frames"][:5] == [0, 1, 2, 3, 5]  # floor(i * 30/24)
    assert mapped["frames"][-1] == int(Fraction(47) * Fraction(30, 24))
    # A source shorter than the owned range is rejected, never padded.
    with pytest.raises(FilmError, match="shorter than the owned range"):
        map_source_frames(30, 24, 48, source_count=10)
    with pytest.raises(FilmError, match="fps"):
        map_source_frames(30, 0, 4)


# --- profile gating and regression boundaries ------------------------------

def test_legacy_project_rejects_animation_normalization(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1)
    for call in (lambda: normalize_shot_sequence(p, "S001"),
                 lambda: animation_validate(p)):
        with pytest.raises(FilmError, match="LEGACY_MV"):
            call()


def test_cli_animation_validate(tmp_path):
    p, _ = animated_project_with_96(tmp_path)
    assert cli.main(["animation-validate", str(p)]) == 0
    (tmp_path / "legacy").mkdir()
    legacy = fixture_project(tmp_path / "legacy", seconds=1, shot_count=1)
    assert cli.main(["animation-validate", str(legacy)]) == 1


def test_preview_still_consumes_member_map(tmp_path):
    """Draft preview wiring through member_map_for_entry stays identical."""
    p = animation_project(tmp_path, shot_count=1, seconds=1)
    make_sequence(tmp_path / "seq", count=24, size=(64, 48))
    import_frame_sequence(p, "S001", folder=tmp_path / "seq")
    result = compile_preview(p)
    folder, record = newest_build(p)
    assert result["status"] == "COMPLETE" and record["mode"] == "PREVIEW"
    assert record["frames"]["total"] == 24
    assert record["frames"]["placeholder_frames"] == 0
    rows = [json.loads(line) for line in
            (folder / "draft_frame_map.jsonl").read_text().splitlines()]
    assert len(rows) == 24
    assert rows[0]["file"] == "draft_frames/F_000001.png"
    assert rows[0]["sources"][0]["local_frame_index"] == 0
    assert all(r["sources"][0]["resolved"] for r in rows)


def test_original_audio_lyrics_and_cues_untouched(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=2)
    names = ["analysis/audio.json", "lyrics/lyrics_timed.json",
             "lyrics/lyrics_source.txt", "manifest/shots.json"]
    before = {n: digest(p / n) for n in names if (p / n).is_file()}
    config = read(p / "project.yaml")
    before[config["audio"]["path"]] = digest(p / config["audio"]["path"])
    animation_init(p)
    folder = tmp_path / "seq"
    folder.mkdir()
    for i in range(48):
        rgba_frame(folder / f"f{i:04d}.png", size=(64, 48), seed=i)
    import_frame_sequence(p, "S001", folder=folder)
    normalize_shot_sequence(p, "S001",
                            schedules=[twos("character", 0, 24)])
    animation_validate(p)
    for n, sha in before.items():
        assert digest(p / n) == sha
