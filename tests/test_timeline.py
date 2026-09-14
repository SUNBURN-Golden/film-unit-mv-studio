from copy import deepcopy

import pytest

from engine.core import FilmError, digest, read, validate_manifest, write
from engine.timeline import merge_shots, move_cut, snap_cut, split_shot


@pytest.fixture
def timeline_project(tmp_path):
    p = tmp_path / "project"
    p.mkdir()
    write(p / "project.yaml", {"format": {"fps": 24}})
    write(p / "analysis/audio.json", {"duration_ms": 9000,
          "beat_times_ms": [2990, 3450, 6010], "onsets_ms": [3150, 5800]})
    shots = [{"id": f"S{i + 1:03d}", "sequence": "SEQ01" if i < 2 else "SEQ02",
              "in_ms": i * 3000, "out_ms": (i + 1) * 3000, "duration_ms": 3000,
              "description": "fixture", "characters": [], "locations": [],
              "composition": "wide", "camera": {}, "motion": {}, "references": [],
              "render_mode": "STATIC", "renderer": "auto", "status": "final"}
             for i in range(3)]
    write(p / "manifest/shots.json", shots)
    write(p / "manifest/sequence.json", [
        {"id": "SEQ01", "in_ms": 0, "out_ms": 6000},
        {"id": "SEQ02", "in_ms": 6000, "out_ms": 9000}])
    write(p / "manifest/assets.json", {"schema_version": 1, "shots": {
        shot["id"]: {"final": {"path": f"render/{shot['id']}.mp4"}} for shot in shots}})
    write(p / "qc/report.json", {"shots": [{"shot_id": shot["id"], "status": "PASS"} for shot in shots]})
    write(p / "lyrics/lyrics_timed.json", {"cues": [{"start_ms": 2700, "end_ms": 3900, "text": "independent"}]})
    (p / "input").mkdir()
    (p / "input/lyrics.txt").write_text("independent\n")
    return p


def test_cut_edit_invalidates_only_affected_visuals_and_keeps_lyrics(timeline_project):
    p = timeline_project
    lyrics_before = {str(f): digest(f) for f in [p / "lyrics/lyrics_timed.json", p / "input/lyrics.txt"]}
    untouched = deepcopy(read(p / "manifest/shots.json")[2])
    result = move_cut(p, "S001", 3500)
    assert [(s["in_ms"], s["out_ms"]) for s in result["shots"]] == [(0, 3500), (3500, 6000), (6000, 9000)]
    assert result["shots"][2] == untouched
    assert set(read(p / "manifest/assets.json")["shots"]) == {"S003"}
    assert [r["status"] for r in read(p / "qc/report.json")["shots"]] == ["STALE_TIMELINE", "STALE_TIMELINE", "PASS"]
    assert all(digest(f) == sha for f, sha in lyrics_before.items())


def test_split_merge_preserves_coverage_and_stable_unaffected_ids(timeline_project):
    p = timeline_project
    result = split_shot(p, "S001", 1100)
    assert [s["id"] for s in result["shots"]] == ["S001", "S004", "S002", "S003"]
    validate_manifest(result["shots"], 9000)
    merged = merge_shots(p, "S001", "S004")["shots"]
    assert [(s["id"], s["in_ms"], s["out_ms"]) for s in merged] == [
        ("S001", 0, 3000), ("S002", 3000, 6000), ("S003", 6000, 9000)]
    with pytest.raises(FilmError, match="same sequence"):
        merge_shots(p, "S002", "S003")


def test_rejected_edits_do_not_mutate_files(timeline_project):
    p = timeline_project
    before = {str(f): digest(f) for f in p.rglob("*") if f.is_file()}
    for fn in [lambda: split_shot(p, "S001", 1),
               lambda: move_cut(p, "S001", 6000),
               lambda: move_cut(p, "S003", 8000),
               lambda: merge_shots(p, "S001", "S003"),
               lambda: move_cut(p, "S001", 3000.0)]:
        with pytest.raises(FilmError):
            fn()
    assert all(digest(f) == sha for f, sha in before.items())


def test_snap_uses_measured_timestamps_and_sequence_endpoints(timeline_project):
    p = timeline_project
    assert snap_cut(p, "S001", "beat")["shots"][0]["out_ms"] == 2990
    assert snap_cut(p, "S001", "onset")["shots"][0]["out_ms"] == 3150
    move_cut(p, "S002", 6500)
    sequences = read(p / "manifest/sequence.json")
    assert sequences[0]["out_ms"] == sequences[1]["in_ms"] == 6500
    write(p / "analysis/audio.json", {"duration_ms": 9000, "beat_times_ms": [], "onsets_ms": []})
    with pytest.raises(FilmError, match="No measured"):
        snap_cut(p, "S001", "beat")
