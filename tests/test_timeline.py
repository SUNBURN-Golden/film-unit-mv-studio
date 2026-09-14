from copy import deepcopy

import pytest
from PIL import Image

from engine.core import FilmError, digest, read, validate_manifest, write
from engine.production import import_frame
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


def _add_storyboards(p):
    shots = read(p / "manifest/shots.json")
    (p / "storyboard").mkdir(exist_ok=True)
    for index, shot in enumerate(shots):
        reference = f"storyboard/{shot['id']}.png"
        Image.new("RGB", (96, 72), (index * 70, 60, 140)).save(p / reference)
        shot.update(references=[reference, "characters/shared.png", "locations/shared.png"],
                    storyboard_kind="imported")
    write(p / "manifest/shots.json", shots)
    return shots


def test_split_copies_storyboard_bytes_and_import_keeps_left_unchanged(timeline_project, tmp_path):
    p = timeline_project
    _add_storyboards(p)
    original = p / "storyboard/S001.png"
    original_sha = digest(original)
    result = split_shot(p, "S001", 1500)
    left, right = result["shots"][:2]
    assert left["references"][0] == "storyboard/S001.png"
    assert right["references"][0] == "storyboard/S004.png"
    assert right["references"][1:] == left["references"][1:]
    copied = p / right["references"][0]
    assert digest(copied) == original_sha
    assert not original.samefile(copied)
    replacement = tmp_path / "replacement.png"
    Image.new("RGB", (96, 72), "yellow").save(replacement)
    import_frame(p, right["id"], replacement)
    assert digest(original) == original_sha
    assert digest(copied) != original_sha
    assert read(p / "manifest/shots.json")[1]["references"][1:] == left["references"][1:]


@pytest.mark.parametrize("edited_id", ["S001", "S002"])
def test_import_detaches_legacy_shared_reference_in_either_direction(timeline_project, tmp_path, edited_id):
    p = timeline_project
    shots = _add_storyboards(p)
    (p / "storyboard/S002.png").unlink()
    shots[1]["references"][0] = shots[0]["references"][0]
    write(p / "manifest/shots.json", shots)
    original_sha = digest(p / "storyboard/S001.png")
    replacement = tmp_path / "replacement.png"
    Image.new("RGB", (96, 72), "yellow").save(replacement)
    import_frame(p, edited_id, replacement)
    after = {shot["id"]: shot for shot in read(p / "manifest/shots.json")}
    other_id = "S002" if edited_id == "S001" else "S001"
    assert after[edited_id]["references"][0] == f"storyboard/{edited_id}.png"
    assert after[other_id]["references"][0] == f"storyboard/{other_id}.png"
    assert digest(p / after[other_id]["references"][0]) == original_sha
    assert digest(p / after[edited_id]["references"][0]) != original_sha
    assert all(shot["references"][1:] == ["characters/shared.png", "locations/shared.png"]
               for shot in after.values())


def test_split_rejects_invalid_frame_or_unsafe_reference_before_copy(timeline_project):
    p = timeline_project
    shots = _add_storyboards(p)
    with pytest.raises(FilmError, match="shorter than one frame"):
        split_shot(p, "S001", 1)
    assert not (p / "storyboard/S004.png").exists()
    shots[0]["references"][0] = "../outside.png"
    write(p / "manifest/shots.json", shots)
    manifest_sha = digest(p / "manifest/shots.json")
    with pytest.raises(FilmError, match="inside this project"):
        split_shot(p, "S001", 1500)
    assert digest(p / "manifest/shots.json") == manifest_sha
    assert not (p / "storyboard/S004.png").exists()


@pytest.mark.parametrize("references", [[], ["storyboard/missing.png", "characters/shared.png"]])
def test_split_missing_artwork_has_unique_reference_without_inventing_image(timeline_project, references):
    p = timeline_project
    shots = read(p / "manifest/shots.json")
    shots[0].update(references=references, storyboard_kind="imported")
    write(p / "manifest/shots.json", shots)
    right = split_shot(p, "S001", 1500)["shots"][1]
    assert right["references"] == ["storyboard/S004.png", *references[1:]]
    assert right["storyboard_kind"] == "placeholder"
    assert not (p / "storyboard/S004.png").exists()


def test_split_skips_orphan_storyboard_instead_of_overwriting(timeline_project):
    p = timeline_project
    _add_storyboards(p)
    orphan = p / "storyboard/S004.png"
    Image.new("RGB", (96, 72), "yellow").save(orphan)
    before = digest(orphan)
    right = split_shot(p, "S001", 1500)["shots"][1]
    assert right["id"] == "S005"
    assert digest(orphan) == before


def test_legacy_detach_collision_rejects_before_any_artwork_changes(timeline_project, tmp_path):
    p = timeline_project
    shots = _add_storyboards(p)
    shots[1]["references"][0] = shots[0]["references"][0]
    write(p / "manifest/shots.json", shots)
    before = {f: digest(f) for f in p.rglob("*") if f.is_file()}
    replacement = tmp_path / "replacement.png"
    Image.new("RGB", (96, 72), "yellow").save(replacement)
    with pytest.raises(FilmError, match="already exists"):
        import_frame(p, "S001", replacement)
    assert all(digest(path) == sha for path, sha in before.items())


def test_import_replaces_hardlink_without_changing_shared_inode(timeline_project, tmp_path):
    p = timeline_project
    _add_storyboards(p)
    left, right = p / "storyboard/S001.png", p / "storyboard/S002.png"
    right.unlink()
    right.hardlink_to(left)
    before = digest(left)
    replacement = tmp_path / "replacement.png"
    Image.new("RGB", (96, 72), "yellow").save(replacement)
    import_frame(p, "S002", replacement)
    assert digest(left) == before
    assert digest(right) != before
    assert not left.samefile(right)


def test_import_initializes_an_empty_first_reference(timeline_project, tmp_path):
    p = timeline_project
    shots = _add_storyboards(p)
    shots[1]["references"] = []
    (p / "storyboard/S002.png").unlink()
    write(p / "manifest/shots.json", shots)
    replacement = tmp_path / "replacement.png"
    Image.new("RGB", (96, 72), "yellow").save(replacement)
    import_frame(p, "S002", replacement)
    updated = read(p / "manifest/shots.json")[1]
    assert updated["references"] == ["storyboard/S002.png"]
    assert updated["storyboard_kind"] == "imported"
    assert digest(p / updated["references"][0]) == digest(replacement)


def test_import_does_not_overwrite_orphan_canonical_target(timeline_project, tmp_path):
    p = timeline_project
    shots = _add_storyboards(p)
    shots[1]["references"][0] = shots[0]["references"][0]
    write(p / "manifest/shots.json", shots)
    before = {f: digest(f) for f in p.rglob("*") if f.is_file()}
    replacement = tmp_path / "replacement.png"
    Image.new("RGB", (96, 72), "yellow").save(replacement)
    with pytest.raises(FilmError, match="unrelated storyboard"):
        import_frame(p, "S002", replacement)
    assert all(digest(path) == sha for path, sha in before.items())
