"""film-lyrics-review: engine coverage of the lyric/cue review workbench.

Repeat-section and expansion state, simultaneous/back-to-back seams,
coverage gaps, long-line and fast-read flags, per-cue missing-glyph
mapping, listening-position -> cue selection, source diffs, imported
candidate evaluation (never pre-reviewed), the legacy fingerprint and
ANIM-020 subbed-only invalidation scope, and clean-vs-subbed build
hash separation. All fixtures are small synthetic projects; review/lock
records here are protocol fixtures, never artwork approval —
qualification UNQUALIFIED, acceptance PENDING, release NOT_AUTHORIZED.
"""
import json
from pathlib import Path

import pytest

from engine.core import (FilmError, atomic_text, digest, frame_at,
                         production_fingerprint, read,
                         visual_context_fingerprint, write)
from engine.lyrics import (export_subtitles, prepare_lyrics, save_timing,
                           validate_lyrics)
from engine.lyrics_review import (cue_at, evaluate_candidate,
                                  invalidation_report, source_diff,
                                  workbench)

from test_anim_003 import animation_project, make_sequence
from test_compiler_v03 import fixture_project

REVIEWER = "Synthetic fixture reviewer"


def _time_all(p, reviewer=REVIEWER):
    """Time every lyric row across the measured duration, then review."""
    doc = prepare_lyrics(p)
    duration = read(p / "analysis/audio.json")["duration_ms"]
    rows = [r for r in doc["rows"] if r["kind"] == "lyric"]
    span = duration // max(1, len(rows))
    doc["cues"] = [
        {"id": f"C{i:03d}", "source_row_id": row["id"], "text": row["text"],
         "start_ms": i * span, "end_ms": (i + 1) * span - 50}
        for i, row in enumerate(rows)]
    return save_timing(p, doc, reviewer=reviewer)


def _write_lyrics(p, text):
    atomic_text(p / "input/lyrics.txt", text)


# --- workbench read model -------------------------------------------------

def test_workbench_reads_reviewed_document_and_frames(tmp_path):
    p = fixture_project(tmp_path, seconds=3, shot_count=1, lyric_count=3)
    board = workbench(p)
    assert board["profile"] == "LEGACY_MV"
    assert board["duration_ms"] == 3000
    assert board["review"]["state"] == "CURRENT"
    assert board["counts"]["cues"] == 3
    first = board["cues"][0]
    assert first["frames"] == [frame_at(first["start_ms"], 24),
                               frame_at(first["end_ms"], 24)]
    assert first["width_px"] >= 0 and first["usable_width_px"] > 0
    assert board["font"]["final_blocked"] is False
    assert board["source_diff"]["pending"] is None
    assert board["rows"]["unresolved"] == []


def test_cue_at_links_listening_position_to_selection(tmp_path):
    p = fixture_project(tmp_path, seconds=3, shot_count=1, lyric_count=3)
    board = workbench(p)
    cues = board["cues"]
    on = cue_at({"cues": cues}, cues[0]["start_ms"] + 5, fps=24)
    assert on["state"] == "ON_CUE" and on["cue"]["id"] == cues[0]["id"]
    assert on["position_frame"] == frame_at(cues[0]["start_ms"] + 5, 24)
    # The gap between cue 1's end and cue 2's start reports both flanks.
    gap_ms = (cues[0]["end_ms"] + cues[1]["start_ms"]) // 2
    gap = cue_at({"cues": cues}, gap_ms, fps=24)
    assert gap["state"] == "IN_GAP"
    assert gap["previous"]["id"] == cues[0]["id"]
    assert gap["next"]["id"] == cues[1]["id"]
    assert gap["gap_before_ms"] == gap_ms - cues[0]["end_ms"]
    first = cue_at({"cues": cues}, 0, fps=24)
    if cues[0]["start_ms"] > 0:
        assert first["state"] == "BEFORE_FIRST"
    last = cue_at({"cues": cues}, 2999, fps=24)
    assert last["state"] == "AFTER_LAST"
    assert last["previous"]["id"] == cues[-1]["id"]
    with pytest.raises(FilmError):
        cue_at({"cues": cues}, -1)


def test_repeat_marker_requires_explicit_expansion(tmp_path):
    p = fixture_project(tmp_path, seconds=4, shot_count=1, lyric_count=2)
    _write_lyrics(p, "[hook]\nRoots remain\n[verse]\nWe belong\n[hook]\n")
    doc = prepare_lyrics(p)
    marker = next(r for r in doc["rows"] if r["kind"] == "repeat")
    assert marker["suggested_source_row_ids"] == ["L0002"]
    doc["cues"] = [
        {"id": "C1", "source_row_id": "L0002", "text": "Roots remain",
         "start_ms": 0, "end_ms": 1500},
        {"id": "C2", "source_row_id": "L0004", "text": "We belong",
         "start_ms": 1600, "end_ms": 3000}]
    save_timing(p, doc)
    board = workbench(p)
    rep = board["repeats"][0]
    assert rep["marker_row_id"] == "L0005" and not rep["resolved"]
    assert rep["expansion"] is None
    assert board["rows"]["unresolved"] == ["L0005"]
    # An explicit expansion plus a cue on the expanded row resolves it.
    doc["repeat_expansions"] = {"L0005": ["L0002"]}
    doc["cues"].append({"id": "C3", "source_row_id": "L0005:L0002",
                        "text": "Roots remain",
                        "start_ms": 3100, "end_ms": 3900})
    save_timing(p, doc)
    board = workbench(p)
    rep = board["repeats"][0]
    assert rep["resolved"] and rep["expansion"] == ["L0002"]
    timed = next(e for e in rep["expanded_rows"] if e["timed"])
    assert timed["effective_id"] == "L0005:L0002"


def test_back_to_back_and_tight_gap_seams_are_explained(tmp_path):
    p = fixture_project(tmp_path, seconds=3, shot_count=1, lyric_count=3)
    doc = prepare_lyrics(p)
    rows = [r for r in doc["rows"] if r["kind"] == "lyric"]
    doc["cues"] = [
        {"id": "C1", "source_row_id": rows[0]["id"], "text": rows[0]["text"],
         "start_ms": 0, "end_ms": 1000},
        {"id": "C2", "source_row_id": rows[1]["id"], "text": rows[1]["text"],
         "start_ms": 1000, "end_ms": 2000},          # touches C1
        {"id": "C3", "source_row_id": rows[2]["id"], "text": rows[2]["text"],
         "start_ms": 2020, "end_ms": 2900}]          # 20 ms < one frame
    save_timing(p, doc)
    board = workbench(p)
    relations = {s["right_id"]: s["relation"] for s in board["seams"]}
    assert relations == {"C2": "BACK_TO_BACK", "C3": "TIGHT_GAP"}
    assert board["counts"]["back_to_back"] == 1


def test_long_line_fast_read_and_coverage_gap_flags(tmp_path):
    p = fixture_project(tmp_path, seconds=3, shot_count=1, lyric_count=1)
    _write_lyrics(p, "짧은 첫 구절\n"
                     + "아주 길게 이어지는 두 번째 구절로 화면 가운데 "
                       "세 줄 이상이 필요할 만큼 아주 길고 길게 쓴 가사를 "
                       "계속 이어서 노래하는 경우입니다\n"
                     + "빠른 세 번째\n")
    doc = prepare_lyrics(p)
    rows = [r for r in doc["rows"] if r["kind"] == "lyric"]
    doc["cues"] = [
        {"id": "C1", "source_row_id": rows[0]["id"], "text": rows[0]["text"],
         "start_ms": 0, "end_ms": 900},
        {"id": "C2", "source_row_id": rows[1]["id"], "text": rows[1]["text"],
         "start_ms": 1000, "end_ms": 2400},
        {"id": "C3", "source_row_id": rows[2]["id"], "text": rows[2]["text"],
         "start_ms": 2400, "end_ms": 2600}]          # 200 ms < MIN_CUE_MS
    save_timing(p, doc)
    board = workbench(p)
    flags = {c["id"]: c["flags"] for c in board["cues"]}
    assert "LONG_LINE" in flags["C2"]
    assert "SHORT" in flags["C3"]
    long_cue = next(c for c in board["cues"] if c["id"] == "C2")
    assert long_cue["est_lines"] > 2
    # Drop the last cue: its row's uncovered span becomes a coverage gap.
    doc["cues"].pop()
    save_timing(p, doc)
    board = workbench(p)
    assert [g["row_id"] for g in board["coverage_gaps"]] == [rows[2]["id"]]
    assert board["coverage_gaps"][0]["text"] == rows[2]["text"]


def test_korean_missing_glyphs_named_per_cue_and_final_blocked(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1, lyric_count=1)
    _write_lyrics(p, "존재를 긍정해\n")
    _time_all(p)
    board = workbench(p)
    report = board["font"]["report"]
    # DejaVu Sans covers no Hangul syllables: the check must name them.
    assert report["status"] == "missing_glyphs"
    assert report["missing_codepoints"]
    assert board["font"]["final_blocked"] is True
    cue_ids = list(board["font"]["missing_by_cue"])
    assert cue_ids == [board["cues"][0]["id"]]
    assert "MISSING_GLYPHS" in board["cues"][0]["flags"]
    # The same verdict the Final gate enforces — never softened.
    with pytest.raises(FilmError, match="glyph coverage"):
        export_subtitles(p, p / "strict", 2000, strict=True)


# --- source diff ------------------------------------------------------------

def test_source_diff_shows_pending_edit_and_invalidated_base(tmp_path):
    p = fixture_project(tmp_path, seconds=3, shot_count=1, lyric_count=2)
    _time_all(p)
    _write_lyrics(p, "Synthetic fixture line 01\nA changed second line\n")
    diff = source_diff(p)
    # The mirror still holds the prepared bytes: the edit is pending.
    assert diff["pending"] is not None and diff["pending"]["changed"]
    change = diff["pending"]["changes"][0]
    assert change["op"] == "replace"
    assert "A changed second line" in change["after"]
    prepare_lyrics(p)                       # archives the old timing
    diff = source_diff(p)
    assert diff["pending"] is None          # mirror is synced now
    invalidated = diff["invalidated"]
    assert invalidated is not None and invalidated["changed"]
    assert "A changed second line" in invalidated["changes"][0]["after"]


# --- imported candidate evaluation ------------------------------------------

def _candidate(p, **overrides):
    doc = prepare_lyrics(p)
    rows = [r for r in doc["rows"] if r["kind"] == "lyric"]
    duration = read(p / "analysis/audio.json")["duration_ms"]
    span = duration // len(rows)
    candidate = {
        "schema_version": 1, "source_path": "input/lyrics.txt",
        "source_sha256": doc["source_sha256"], "source_missing": False,
        "rows": doc["rows"], "repeat_expansions": {},
        "cues": [{"id": f"ASR{i}", "source_row_id": row["id"],
                  "text": row["text"],
                  "start_ms": i * span, "end_ms": (i + 1) * span - 50}
                 for i, row in enumerate(rows)],
        "unresolved_row_ids": [], "review": None}
    candidate.update(overrides)
    return candidate


def test_asr_candidate_is_never_treated_as_reviewed(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1, lyric_count=2)
    candidate = _candidate(p)
    # Even a candidate smuggled in with a review record stays UNREVIEWED.
    candidate["review"] = {"schema_version": 2, "reviewer": "ASR bot",
                           "reviewed_at": "2026-01-01",
                           "timing_sha256": "0" * 64,
                           "lyrics_review_sha256": "0" * 64}
    timed_path = p / "lyrics/lyrics_timed.json"
    before = timed_path.read_bytes()
    result = evaluate_candidate(p, candidate)
    assert result["valid"] and result["review_state"] == "UNREVIEWED"
    assert result["candidate_review_ignored"] is True
    assert result["human_review_required"] is True
    # Evaluating never writes a timing document.
    assert timed_path.read_bytes() == before
    # Saving without a reviewer keeps it unreviewed; only a named human
    # reviewer binds the approval.
    saved = save_timing(p, candidate)
    assert saved["review"] is None
    saved = save_timing(p, candidate, reviewer=REVIEWER)
    assert saved["review"]["reviewer"] == REVIEWER
    board = workbench(p)
    assert board["review"]["state"] == "CURRENT"


def test_candidate_overlap_and_source_mismatch_are_named(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1, lyric_count=2)
    candidate = _candidate(p)
    candidate["cues"][1]["start_ms"] = candidate["cues"][0]["end_ms"] - 100
    result = evaluate_candidate(p, candidate)
    assert not result["valid"]
    assert any("verlapping" in e or "order" in e for e in result["errors"])
    # The tolerant seam scan still shows the overlap pair.
    assert any(s["relation"] == "OVERLAP" for s in result["seams"])
    foreign = _candidate(p, source_sha256="0" * 64)
    result = evaluate_candidate(p, foreign)
    assert not result["valid"]
    assert any("different lyrics source" in e for e in result["errors"])
    bogus = evaluate_candidate(p, {"schema_version": 99})
    assert not bogus["valid"] and bogus["errors"]


# --- invalidation scope -------------------------------------------------------

def test_legacy_timing_edit_stales_lock_but_keeps_visual_context(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1, lyric_count=2)
    from engine.core import lock_production
    lock_production(p, "fixture approver")
    report = invalidation_report(p, "TIMING")
    assert report["profile"] == "LEGACY_MV"
    assert report["locks_now"]["production"] == "CURRENT"
    assert "production LOCK" in report["projection"]["stale"]
    assert "가사 검수" in report["projection"]["stale"]
    assert any("샷" in k for k in report["projection"]["kept"])
    # Timing is not part of the visual fingerprint: shot approvals survive.
    before = visual_context_fingerprint(p)
    doc = prepare_lyrics(p)
    doc["cues"][0]["start_ms"] += 100
    save_timing(p, doc)
    assert visual_context_fingerprint(p) == before
    assert production_fingerprint(p) != read(
        p / "manifest/locks.json")["fingerprint"]
    # A source-text change does rewrite the visual intent.
    source_report = invalidation_report(p, "SOURCE_TEXT")
    assert any("영상" in s for s in source_report["projection"]["stale"])


def test_animation_cue_font_changes_invalidate_subbed_only(tmp_path):
    p = animation_project(tmp_path, shot_count=2, seconds=2)
    from engine.animation_assets import import_frame_sequence
    for index in range(2):
        import_frame_sequence(
            p, f"S{index + 1:03d}",
            folder=make_sequence(tmp_path / f"seq{index}", 24,
                                 seed=index * 50))
    report = invalidation_report(p, "TIMING")
    assert report["profile"] == "FRAME_ANIMATION_V1"
    proj = report["projection"]
    assert proj["method"] == "ANIM-020 plan_rebuild"
    # Cut and transition reviews stay bound; only the delivery re-encodes.
    assert all(v["state"] == "KEPT" for v in proj["cuts"].values())
    assert proj["locks"]["FINAL_LOCK"]["state"] == "STALE"
    assert proj["locks"]["PLAN_LOCK"]["state"] == "KEPT"
    assert proj["final_film"]["state"] == "STALE"
    closure = proj["closure"]
    assert closure["dirty_frames"] == []            # clean untouched
    assert closure["subbed_dirty_frames"] == list(range(48))
    assert closure["encode_roles"] == ["subbed"]
    assert "CUT 검수" in report["survives"]
    font = invalidation_report(p, "FONT")
    assert font["projection"]["locks"]["FINAL_LOCK"]["state"] == "STALE"
    # Source text reaches the shared intent: everything goes stale.
    source = invalidation_report(p, "SOURCE_TEXT")
    assert source["projection"]["method"] == "binding-fields"
    assert not source["survives"]


def test_animation_invalidation_without_sequence_pins_still_reports(tmp_path):
    p = animation_project(tmp_path, shot_count=2, seconds=2)
    report = invalidation_report(p, "TIMING")
    proj = report["projection"]
    # With no adopted sequences the ANIM-020 graph cannot be built; the
    # binding-level answer is still honest about what a cue edit touches.
    assert proj.get("unavailable") or proj["method"] == "ANIM-020 plan_rebuild"
    assert "FINAL_LOCK" in proj["stale"]


# --- sealed build clean/subbed separation -------------------------------------

def test_build_summary_separates_clean_and_subbed_lyric_binding(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1, lyric_count=2)
    from engine.compiler import compile_preview
    compile_preview(p)
    board = workbench(p)
    build = board["builds"][0]
    assert build["build_id"] == "B0001"
    assert build["clean_sha256"] and build["subbed_sha256"]
    assert build["distinct_outputs"] is True
    assert build["clean_sha256"] != build["subbed_sha256"]
    assert build["review_matches_current"] is True
    assert build["lyrics"]["timing_sha256"]
    # A timing edit does not touch sealed bytes but the current review no
    # longer matches what the build sealed.
    doc = prepare_lyrics(p)
    doc["cues"][0]["start_ms"] += 50
    save_timing(p, doc, reviewer=REVIEWER)
    board = workbench(p)
    build = board["builds"][0]
    assert build["review_matches_current"] is False
    assert build["clean_sha256"] == digest(
        p / "builds/B0001/MASTER_CLEAN.mp4")
    assert build["subbed_sha256"] == digest(
        p / "builds/B0001/MASTER_SUBBED.mp4")


def test_workbench_handles_missing_analysis_and_empty_document(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1, lyric_count=2)
    (p / "analysis/audio.json").unlink()
    board = workbench(p)
    assert board["duration_ms"] is None
    assert any("분석" in w for w in board["warnings"])
    # The stored timing is still shown — only range re-validation needs
    # the measured duration. The recorded review still binds the exact
    # document, so it stays CURRENT even without analysis.
    assert board["counts"]["cues"] == 2
    assert board["review"]["state"] == "CURRENT"
    # A stored document edited after review makes the bound fingerprint
    # stale — never silently revived.
    doc = prepare_lyrics(p)
    doc["cues"][0]["start_ms"] += 40
    write(p / "lyrics/lyrics_timed.json", doc)
    board = workbench(p)
    assert board["review"]["state"] == "STALE"
