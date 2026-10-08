"""film-workspace-accessibility: next action, timeline keys, failure keep.

Synthetic fixtures only. No OAuth, no network, no paid call. A saved
frame is a workspace preference, not a review, a LOCK or a Final.
"""
import json
from pathlib import Path

import pytest

from engine.animation_assets import import_frame_sequence
from engine.animation_migrate import animation_init
from engine.animation_schema import read_canon
from engine.core import FilmError, read, write
from engine.durable_journal import DurableJournal
from engine.motion_plan import save_shot_plan
from engine.shot_board import board_view
from engine.workspace_accessibility import (
    NOTE_LIMIT, commit_workspace, credential_view, decide_stage, journey,
    keyboard_targets, load_prefs, marker, restart_view, timeline_focus,
    window_bounds, zoom_step)
from test_anim_003 import make_sequence
from test_compiler_v03 import fixture_project


def _project(tmp_path, *, shots=2, seconds=2):
    return fixture_project(tmp_path, seconds=seconds, shot_count=shots)


def _animate(tmp_path, *, import_frames=False):
    project = _project(tmp_path)
    config = read(project / "project.yaml")
    # Match the imported frame size so a stored plan's canvas agrees with
    # both the project format and the sequence the board re-reads.
    config["format"].update(width=96, height=72)
    write(project / "project.yaml", config)
    animation_init(project)
    if import_frames:
        for index in range(2):
            import_frame_sequence(
                project, f"S{index + 1:03d}",
                folder=make_sequence(tmp_path / f"seq{index}", 24,
                                     seed=index + 1))
    return project


def _plan(shot_id, length):
    return {
        "document_type": "animation_shot_plan", "schema_version": 1,
        "shot_id": shot_id, "revision": 1, "motion_intent": "ANIMATED",
        "story_role": "fixture", "emotion_role": "calm",
        "canvas": {"width": 96, "height": 72}, "background": [0, 0, 0],
        "rig": None, "assets": [], "events": [],
        "keyposes": [{"kind": "KEYPOSE", "frame": 0, "ref": None}],
        "segments": [{"start": 0, "end": length, "path": "A",
                      "capabilities": []}],
        "tracks": {"camera": {"pivot": [48, 36], "transform": None},
                   "layers": {}},
        "preparation": {"work": [], "creator": "synthetic fixture",
                        "reviewer": None, "revision_scope": "fixture"}}


def _snap(project):
    names = ["input/lyrics.txt", "lyrics/lyrics_timed.json",
             "manifest/locks.json", "manifest/shots.json", "project.yaml",
             "timeline/edit.json", "production/approvals.jsonl"]
    config = read(project / "project.yaml")
    names.append(config["audio"]["path"])
    found = {}
    for rel in names:
        path = project / rel
        found[rel] = path.read_bytes() if path.is_file() else None
    return found


def _row(shot, review, *, pinned=True, planned=True):
    return {"instance_id": "I" + shot[1:], "shot_id": shot,
            "pin": {"resolved": pinned},
            "plan": {"keyposes": 1} if planned else None,
            "review": review}


# --- acceptance: words, not color; no raw JSON; keep frame on failure -------

def test_markers_distinguish_review_failure_and_progress_without_color():
    review = marker("CURRENT")
    failed = marker("FAILED")
    progress = marker("IN_PROGRESS")
    texts = {review["text"], failed["text"], progress["text"]}
    assert len(texts) == 3
    for item in (review, failed, progress):
        assert set(item) == {"code", "word", "mark", "pattern", "text"}
        assert item["word"] and item["mark"]
        assert item["text"] == f"{item['mark']} {item['word']}"
    assert review["word"] == "검수 완료"
    assert failed["word"] == "실패"
    assert progress["word"] == "진행 중"


def test_legacy_project_is_refused_and_untouched(tmp_path):
    project = _project(tmp_path)
    before = _snap(project)
    with pytest.raises(FilmError, match="LEGACY_MV"):
        journey(project)
    with pytest.raises(FilmError, match="LEGACY_MV"):
        load_prefs(project)
    with pytest.raises(FilmError, match="LEGACY_MV"):
        commit_workspace(project, frame=0, zoom="second",
                         reduced_motion=False, narrow_layout=False, note="")
    assert _snap(project) == before
    assert not (project / "workspace" / "accessibility.json").exists()


def test_unpinned_project_next_action_is_prepare(tmp_path):
    project = _animate(tmp_path)
    guide = journey(project)
    assert guide["current"] == "prepare"
    assert guide["work_empty"] is True
    assert guide["json_edit_required"] is False
    assert "edit.json" not in guide["next_action"]
    assert "준비" in guide["next_action"]
    assert guide["fps"] == 24
    assert "240초" in guide["baseline_note"]
    assert "5760" in guide["baseline_note"]
    assert guide["facets"]["qualification_state"] == "UNQUALIFIED"
    assert guide["facets"]["release_state"] == "NOT_AUTHORIZED"
    current = next(stage for stage in guide["stages"] if stage["id"] == "prepare")
    assert current["status"]["text"] == "… 진행 중"


def test_pinned_project_without_plan_next_action_is_motion(tmp_path):
    project = _animate(tmp_path, import_frames=True)
    guide = journey(project)
    assert guide["current"] == "motion"
    assert "동작" in guide["next_action"]
    assert guide["json_edit_required"] is False
    assert "S001" in guide["next_action"]


def test_planned_project_next_action_is_review_and_keeps_lyrics(tmp_path):
    project = _animate(tmp_path, import_frames=True)
    view = board_view(project)
    for row in view["entries"]:
        length = row["output_range"][1] - row["output_range"][0]
        save_shot_plan(project, _plan(row["shot_id"], length))
    before = _snap(project)
    guide = journey(project)
    assert guide["current"] == "review"
    assert "검토" in guide["next_action"]
    assert "미검수" in marker("UNREVIEWED")["text"]
    assert _snap(project) == before


def test_decide_stage_revise_then_output_does_not_inherit_approval():
    stale = [_row("S001", "STALE"), _row("S002", "UNREVIEWED")]
    report = decide_stage(stale, {"final": "UNLOCKED"})
    assert report["current"] == "revise"
    assert "S001" in report["next_action"]
    assert "따라가지 않습니다" in report["next_action"]
    changes = [_row("S001", "CHANGES_REQUIRED"), _row("S002", "CURRENT")]
    assert decide_stage(changes)["current"] == "revise"
    done = [_row("S001", "CURRENT"), _row("S002", "CURRENT")]
    output = decide_stage(done, {"final": "CURRENT"})
    assert output["current"] == "output"
    assert "대신하지 않습니다" in output["next_action"]
    assert "작품 승인은 아닙니다" in output["next_action"]
    revise = next(stage for stage in output["stages"] if stage["id"] == "revise")
    assert revise["status"]["word"] == "해당 없음"
    empty = decide_stage([], {})
    assert empty["current"] == "prepare" and empty["work_empty"] is True


def test_failed_commit_preserves_frame_and_note(tmp_path):
    project = _animate(tmp_path)
    saved = commit_workspace(project, frame=3, zoom="close",
                             reduced_motion=True, narrow_layout=False,
                             note="첫 메모")
    assert saved["selected_frame"] == 3 and saved["draft_note"] == "첫 메모"
    blob = (project / "workspace" / "accessibility.json").read_bytes()
    with pytest.raises(FilmError, match="그대로입니다"):
        commit_workspace(project, frame=10_000, zoom="close",
                         reduced_motion=True, narrow_layout=False,
                         note="바뀐 메모")
    with pytest.raises(FilmError, match="그대로입니다"):
        commit_workspace(project, frame=3, zoom="nope",
                         reduced_motion=True, narrow_layout=False,
                         note="바뀐 메모")
    with pytest.raises(FilmError, match="그대로입니다"):
        commit_workspace(project, frame=True, zoom="close",
                         reduced_motion=True, narrow_layout=False,
                         note="바뀐 메모")
    with pytest.raises(FilmError, match="그대로입니다"):
        commit_workspace(project, frame=3, zoom="close",
                         reduced_motion=True, narrow_layout=False,
                         note="가" * (NOTE_LIMIT + 1))
    assert (project / "workspace" / "accessibility.json").read_bytes() == blob
    again = load_prefs(project)
    assert again["selected_frame"] == 3
    assert again["draft_note"] == "첫 메모"
    assert again["reduced_motion"] is True


def test_commit_does_not_touch_lyrics_locks_timeline_or_audio(tmp_path):
    project = _animate(tmp_path)
    before = _snap(project)
    commit_workspace(project, frame=1, zoom="second", reduced_motion=False,
                     narrow_layout=True, note="메모")
    assert _snap(project) == before
    assert before["production/approvals.jsonl"] is None
    assert not (project / "production" / "approvals.jsonl").exists()
    stored = read_canon(project / "workspace" / "accessibility.json")
    assert stored["document_type"] == "workspace_accessibility"
    assert stored["schema_version"] == 1
    assert "edit.json" not in json.dumps(stored, ensure_ascii=False)


def test_corrupt_prefs_stay_until_a_valid_save(tmp_path):
    project = _animate(tmp_path)
    path = project / "workspace" / "accessibility.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{")
    with pytest.raises(FilmError):
        load_prefs(project)
    assert path.read_bytes() == b"{"
    with pytest.raises(FilmError):
        commit_workspace(project, frame=4, zoom="nope",
                         reduced_motion=False, narrow_layout=False, note="x")
    assert path.read_bytes() == b"{"
    saved = commit_workspace(project, frame=4, zoom="frame",
                             reduced_motion=False, narrow_layout=False,
                             note="복구")
    assert saved["selected_frame"] == 4 and saved["draft_note"] == "복구"
    assert saved["stored"] is True


def test_missing_credential_hides_secret_and_stays_unqualified(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "drive_connection.json").write_text(json.dumps({
        "connection_id": "conn-test", "credential_epoch": 2,
        "access_token": "SECRET-TOKEN-VALUE"}), encoding="utf-8")
    view = credential_view(home)
    dumped = json.dumps(view, ensure_ascii=False)
    assert "SECRET-TOKEN-VALUE" not in dumped
    assert view["secrets_displayed"] is False
    assert view["state"] == "MISSING_CREDENTIAL"
    assert view["word"] == "자격 없음"
    assert view["qualification_state"] == "UNQUALIFIED"
    assert view["connection_record"] == "있음"
    assert view["credential_epoch"] == 2
    assert view["auto_retry"] is False
    absent = credential_view(tmp_path / "empty-home")
    assert absent["connection_record"] == "없음"
    assert absent["text"] == "⚿ 자격 없음"


def test_restart_open_journal_does_not_retry_or_drop_the_frame(tmp_path):
    project = _animate(tmp_path)
    commit_workspace(project, frame=6, zoom="second", reduced_motion=False,
                     narrow_layout=False, note="유지")
    prefs = (project / "workspace" / "accessibility.json").read_bytes()
    journal_path = project / "render" / "execution" / "job_journal.jsonl"
    journal = DurableJournal(journal_path)
    journal.append("coordinator", "SUBMIT_INTENT", {"kind": "fixture"})
    view = restart_view(project)
    assert view["state"] == "RESTART"
    assert view["mark"] == "↩"
    assert view["auto_retry"] is False
    assert "하지 않습니다" in view["detail"]
    held = journal_path.read_bytes()
    assert (project / "workspace" / "accessibility.json").read_bytes() == prefs
    again = restart_view(project)
    assert again["state"] == "RESTART"
    assert journal_path.read_bytes() == held
    sealed = held
    journal.append("coordinator", "JOB_SEALED", {})
    closed = restart_view(project)
    assert closed["state"] == "NONE"
    assert (project / "workspace" / "accessibility.json").read_bytes() == prefs
    assert load_prefs(project)["selected_frame"] == 6
    # A torn tail is a restart, and the tear is not rewritten.
    torn = project / "render" / "execution" / "other.jsonl"
    # The reader only looks at the fixed execution path. Replace it.
    journal_path.write_bytes(b'{"truncated":true')
    torn_view = restart_view(project)
    assert torn_view["state"] == "RESTART"
    assert torn_view["tail"] == "TRUNCATED"
    assert journal_path.read_bytes() == b'{"truncated":true'
    assert sealed != journal_path.read_bytes()
    assert not torn.exists()


def test_long_timeline_keyboard_zoom_and_reduced_motion_keep_frames():
    total = 5760  # 240s at 24fps
    bounds = window_bounds(total, 1000, "second")
    assert bounds["span"] == 24
    assert bounds["start"] <= 1000 < bounds["end"]
    assert bounds["end"] - bounds["start"] == 24
    home = keyboard_targets(total, 0, "wide")
    assert home["previous_frame"] == 0 and home["home"] == 0
    assert home["next_frame"] == 1 and home["end"] == 5759
    mid = keyboard_targets(total, 1000, "second")
    assert mid["previous_frame"] == 999
    assert mid["next_frame"] == 1001
    assert mid["previous_page"] == 1000 - 24
    assert mid["next_page"] == 1000 + 24
    assert zoom_step("second", "in") == "close"
    assert zoom_step("frame", "in") == "frame"
    assert zoom_step("wide", "out") == "wide"
    # Reduced motion is not an input to the move: the same keys land the same.
    assert keyboard_targets(total, 1000, "second") == mid
    guide = {
        "output_frames": 10,
        "entries": [
            {"instance_id": "I001", "shot_id": "S001",
             "output_range": [0, 6], "review": "UNREVIEWED"},
            {"instance_id": "I002", "shot_id": "S002",
             "output_range": [4, 10], "review": "CURRENT"}],
        "transitions": [{"output_range": [4, 6], "to_instance": "I002"}]}
    focused = timeline_focus(guide, 5, "frame")
    assert focused["rows"][0]["owner"] == "I002"
    assert focused["rows"][0]["status"] == "✓ 검수 완료"
    assert focused["rows"][0]["selection"] == "▶ 선택"
    assert focused["reduced_motion_changes_frames"] is False
    earlier = timeline_focus(guide, 1, "frame")
    assert earlier["rows"][0]["owner"] == "I001"
    assert earlier["rows"][0]["status"] == "○ 미검수"
    wide = timeline_focus(guide, 5, "wide")
    assert wide["window_rows"] == 10
    assert len(wide["rows"]) == 10


def test_core_actions_do_not_require_a_hand_edited_json_file(tmp_path):
    project = _animate(tmp_path)
    guide = journey(project)
    assert guide["json_edit_required"] is False
    assert "edit.json" not in guide["next_action"]
    for action in guide["core_actions"]:
        assert "json" not in action.lower()
    commit_workspace(project, frame=2, zoom="span", reduced_motion=True,
                     narrow_layout=False, note="첫 장면으로")
    text = (project / "workspace" / "accessibility.json").read_text(
        encoding="utf-8")
    assert "첫 장면으로" in text
    parsed = read_canon(project / "workspace" / "accessibility.json")
    assert parsed["selected_frame"] == 2
    assert "Synthetic fixture line" in (project / "input" / "lyrics.txt").read_text(
        encoding="utf-8")
