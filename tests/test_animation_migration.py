"""ANIM-002: explicit conversion, metadata backup and the separated readers.

The synthetic fixtures only exercise the migration contract; they are not a
real production conversion, an artistic review or a qualification result.
"""
import json
from pathlib import Path

import pytest

from engine import cli
from engine.animation_migrate import animation_init
from engine.animation_schema import (canon_bytes, check_document, load_animation_timeline,
                                     read_canon, validate_animation_timeline, write_canon)
from engine.budget import approve as approve_estimate
from engine.builds import list_builds, replay_build, verify_build
from engine.compiler import compile_final, compile_preview, import_asset
from engine.core import (FilmError, atomic_text, digest, frame_at, lock_production,
                         read, require_lock, write)
from engine.economy import initialize as economy_initialize
from engine.imagegen import estimate as image_estimate
from engine.pipeline import prepare
from engine.production import import_frame, make_package
from engine.qc import save_review
from engine.schema import migrate_project
from engine.takes import select_window
from engine.timeline import split_shot
from engine.lyrics import prepare_lyrics
from test_compiler_v03 import fixture_project, newest_build, assert_master

IGNORED = {".compile.lock", "migrations", "timeline"}


def project_files(p):
    return {str(f.relative_to(p)): digest(f)
            for f in p.rglob("*") if f.is_file() and f.name != ".compile.lock"
            and f.relative_to(p).parts[0] not in {"migrations", "timeline"}}


def timeline(lengths, overlaps=None):
    entries = []
    for i, length in enumerate(lengths):
        entries.append({"instance_id": f"I{i+1:03d}", "shot_id": f"S{i+1:03d}",
                        "sequence_revision": 1, "used_source_range": [0, length],
                        "unused_handles": {"before": 0, "after": 0},
                        "transition_out": None})
    for i, overlap in enumerate(overlaps if overlaps is not None else [0] * (len(lengths) - 1)):
        entries[i]["transition_out"] = {"id": f"T{i+1:03d}",
                                        "type": "CROSSFADE" if overlap else "HARD_CUT",
                                        "to_instance": entries[i + 1]["instance_id"],
                                        "overlap_frames": overlap,
                                        **({"curve": "LINEAR_INTERIOR_V1"} if overlap else {})}
    return {"document_type": "animation_timeline", "schema_version": 1,
            "target_frames": sum(lengths) - sum(overlaps or [0]), "entries": entries}


def test_animation_init_converts_and_preserves_bytes(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=3)
    before = project_files(p)
    original_yaml = (p / "project.yaml").read_bytes()
    report = animation_init(p)
    config = read(p / "project.yaml")
    assert config["schema_version"] == 4
    assert config["production_profile"] == "FRAME_ANIMATION_V1"
    assert config["schema_versions"] == {"project": 4, "shot": 3, "lyrics": 1,
                                       "audio": 1, "build": 2}
    animation = config["animation"]
    assert animation["schema_version"] == 1
    assert animation["output_frames"] == frame_at(2000, 24) == 48
    assert animation["timeline"] == "timeline/edit.json"
    assert animation["assets"] == "manifest/animation_assets.json"
    assert animation["execution_plan"] == "execution/plan.json"
    # The original format declaration is untouched; no 16:9/1080p defaulting.
    assert config["format"]["aspect_ratio"] == "4:3"
    # Master, lyrics, cues, manifests, storyboard, bibles: every preserved byte.
    assert all(digest(p / name) == value for name, value in before.items()
               if name != "project.yaml")
    assert digest(p / "project.yaml") != before["project.yaml"]
    # First timeline: the preserved edit as hard cuts only.
    document = load_animation_timeline(p)
    layout = validate_animation_timeline(document, 48)
    assert document["target_frames"] == 48
    assert [e["shot_id"] for e in document["entries"]] == ["S001", "S002", "S003"]
    for entry, expected in zip(document["entries"], [(0, 16), (16, 32), (32, 48)]):
        row = next(r for r in layout["entries"] if r["instance_id"] == entry["instance_id"])
        assert tuple(row["output_range"]) == expected
        assert entry["sequence_revision"] is None
        assert entry["used_source_range"] == [0, 16]
        assert entry["unused_handles"] == {"before": 0, "after": 0}
    for entry in document["entries"][:-1]:
        assert entry["transition_out"]["type"] == "HARD_CUT"
        assert entry["transition_out"]["overlap_frames"] == 0
        assert "curve" not in entry["transition_out"]
    assert document["entries"][-1]["transition_out"] is None
    derived = read_canon(p / "timeline/derived.json")
    assert derived["document_type"] == "animation_timeline_derived"
    assert derived["entries"] == layout["entries"]
    # 666/1333 ms boundaries are off-grid at 24 fps; the report shows it.
    assert set(report["mapping_differences"]) == {"S001", "S002", "S003"}
    row = next(r for r in report["mapping"] if r["shot_id"] == "S001")
    assert (row["in_ms"], row["in_frame"], row["in_remainder_ms_fps"]) == (0, 0, 0)
    assert (row["out_ms"], row["out_frame"], row["out_remainder_ms_fps"]) == (666, 16, 984)
    # Exclusive metadata backup under migrations/.
    (backup_dir,) = p.glob("migrations/animation-init-*")
    assert report["backup_dir"] == str(backup_dir.relative_to(p))
    assert (backup_dir / "project.yaml").read_bytes() == original_yaml
    for relative in ("manifest/shots.json", "manifest/sequence.json",
                     "analysis/audio.json", "lyrics/lyrics_timed.json",
                     "lyrics/lyrics_source.txt"):
        assert (backup_dir / relative).read_bytes() == (p / relative).read_bytes()
    saved_report = read(backup_dir / "migration.json")
    assert saved_report["approvals"]["inherited"] is False
    assert saved_report["preserved"]["changed_paths"] == ["project.yaml"]
    # No legacy approval became an animation approval or lock.
    assert not (p / "production/approvals.jsonl").exists()
    assert not (p / "manifest/animation_locks.json").exists()
    assert report["approvals"] == {"inherited": False, "legacy_locks_copied": 0,
                                   "legacy_reviews_copied": 0,
                                   "animation_approvals_created": 0}


def test_conversion_is_explicit_only_and_one_way(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1)
    # Reading/opening/migrating legacy metadata never opts into the new mode.
    migrate_project(p)
    config = read(p / "project.yaml")
    assert config.get("production_profile") in (None, "LEGACY_MV")
    assert not (p / "timeline/edit.json").exists()
    result = animation_init(p)
    assert result["output_frames"] == 48
    with pytest.raises(FilmError, match="LEGACY_MV"):
        animation_init(p)
    # The legacy version reader keeps rejecting the converted project.
    with pytest.raises(FilmError, match="Unsupported project schema"):
        migrate_project(p)


def test_converted_project_keeps_build1_verify_and_replay(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1)
    compile_preview(p)
    folder, record = newest_build(p)
    build_json_hash = digest(folder / "build.json")
    animation_init(p)
    assert verify_build(folder)["valid"]
    replay_dir = tmp_path / "replay_after_conversion"
    replay_build(folder, replay_dir)
    assert_master(replay_dir / "MASTER_SUBBED.mp4", 2)
    assert digest(folder / "build.json") == build_json_hash
    assert list_builds(p)[0]["build_id"] == record["build_id"]


def test_legacy_mutating_paths_stay_blocked_after_conversion(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=2)
    lock_production(p, "fixture director", mock_only=True)
    lock_bytes = digest(p / "manifest/locks.json")
    animation_init(p)
    from engine.audio import analyze
    from engine.director import draft as director_draft
    from engine.handoff import submit_shots, submit_world
    from engine.imagegen import import_reference
    from engine.production import generate_storyboard
    for call in (
        lambda: compile_preview(p), lambda: compile_final(p),
        lambda: split_shot(p, "S001", 1000), lambda: lock_production(p, "x"),
        lambda: import_asset(p, "S001", p / "storyboard/S001.png"),
        lambda: import_frame(p, "S001", p / "storyboard/S001.png"),
        lambda: make_package(p), lambda: prepare(p), lambda: analyze(p),
        lambda: generate_storyboard(p), lambda: director_draft(p, None),
        lambda: submit_world(p, "{}"), lambda: submit_shots(p, "{}"),
        lambda: import_reference(p, "characters", "C01", p / "storyboard/S001.png"),
        lambda: image_estimate(p, "frames", None),
        lambda: approve_estimate(p, {}),
        lambda: select_window(p, "S001", 0, "x"), lambda: economy_initialize(p),
        lambda: save_review(p, {"shot_id": "S001", "clip_sha256": "0" * 64,
                                "production_id": "x"}, {}, "r", "n"),
    ):
        with pytest.raises(FilmError, match="FRAME_ANIMATION_V1"):
            call()
    # Preserved legacy approvals stay readable but promote nothing.
    assert digest(p / "manifest/locks.json") == lock_bytes
    assert require_lock(p, "mock")["reviewer"] == "fixture director"
    assert not (p / "production/approvals.jsonl").exists()
    assert not (p / "manifest/animation_locks.json").exists()
    assert not list(p.glob("builds/B*"))
    # Shared lyric documents keep their own lifecycle.
    assert prepare_lyrics(p)["rows"]
    assert read(p / "manifest/shots.json")[0]["in_ms"] == 0


def test_animation_init_aborts_without_rewriting_when_inputs_are_invalid(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=2)
    yaml_bytes = (p / "project.yaml").read_bytes()
    shots = read(p / "manifest/shots.json")
    shots[1]["in_ms"] = 200  # Break the contiguous coverage.
    write(p / "manifest/shots.json", shots)
    with pytest.raises(FilmError):
        animation_init(p)
    assert (p / "project.yaml").read_bytes() == yaml_bytes
    assert not (p / "timeline").exists()


def test_animation_init_aborts_on_conflicting_backup(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1)
    backup = p / "migrations" / f"animation-init-{digest(p / 'project.yaml')[:16]}"
    backup.mkdir(parents=True)
    atomic_text(backup / "project.yaml", "different original bytes")
    with pytest.raises(FilmError, match="backup"):
        animation_init(p)
    assert read(p / "project.yaml").get("production_profile") in (None, "LEGACY_MV")


def test_animation_init_refuses_to_overwrite_an_existing_timeline(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1)
    write_canon(p / "timeline/edit.json", timeline([48]))
    with pytest.raises(FilmError, match="already exists"):
        animation_init(p)
    assert read(p / "project.yaml").get("production_profile") in (None, "LEGACY_MV")


def test_animation_init_needs_a_shot_manifest(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1)
    (p / "manifest/shots.json").unlink()
    with pytest.raises(FilmError, match="Missing file"):
        animation_init(p)


def test_cli_animation_init(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1)
    assert cli.main(["animation-init", str(p), "--profile", "frame-animation-v1"]) == 0
    assert cli.main(["animation-init", str(p)]) == 1  # already converted


def test_canonical_json_round_trip_and_rejections(tmp_path):
    path = tmp_path / "doc.json"
    write_canon(path, {"b": 1, "a": {"korean": "가사", "flag": True, "none": None}})
    raw = path.read_bytes()
    assert raw == b'{"a":{"flag":true,"korean":"\xea\xb0\x80\xec\x82\xac","none":null},"b":1}\n'
    assert read_canon(path) == {"a": {"flag": True, "korean": "가사", "none": None}, "b": 1}
    atomic_text(path, '{ "a": 1 }\n')  # extra whitespace
    with pytest.raises(FilmError, match="canonical"):
        read_canon(path)
    atomic_text(path, '{"a":1,"a":2}\n')  # duplicate keys
    with pytest.raises(FilmError, match="Duplicate"):
        read_canon(path)
    atomic_text(path, '{"a":1}')  # missing trailing LF
    with pytest.raises(FilmError, match="canonical"):
        read_canon(path)
    for bad in ({"x": 1.5}, {"x": float("nan")}, {"x": 2**63}, {"x": b"\x00"}):
        with pytest.raises(FilmError):
            canon_bytes(bad)


def test_document_version_gate_rejects_unknown_and_future(tmp_path):
    doc = timeline([48])
    check_document(doc, "animation_timeline")
    for mutated, match in [
        ({**doc, "document_type": "manifest/shots"}, "Unknown document_type"),
        ({**doc, "schema_version": 2}, "Unsupported"),
        ({**doc, "schema_version": True}, "Unsupported"),
        ({**doc, "schema_version": "1"}, "Unsupported"),
        ({k: v for k, v in doc.items() if k != "document_type"}, "Unknown"),
        ({k: v for k, v in doc.items() if k != "schema_version"}, "Unsupported"),
    ]:
        with pytest.raises(FilmError, match=match):
            check_document(mutated)


def test_timeline_validator_blocks(tmp_path):
    valid = timeline([24, 24])
    assert validate_animation_timeline(valid, 48)["output_frames"] == 48
    # 96 + 96 - 12 = 180 with a linear crossfade.
    crossed = timeline([96, 96], [12])
    layout = validate_animation_timeline(crossed, 180)
    assert layout["transitions"][0]["output_range"] == [84, 96]
    bad_cases = [
        ({**timeline([24, 24]), "target_frames": 47}, "target_frames"),
        ({**timeline([24, 24]), "unexpected": 1}, "Unknown"),
    ]
    last = timeline([24, 24])
    last["entries"][-1]["transition_out"] = {"id": "T9", "type": "HARD_CUT",
                                             "to_instance": "I999", "overlap_frames": 0}
    bad_cases.append((last, "null transition_out"))
    hard = timeline([24, 24])
    hard["entries"][0]["transition_out"]["overlap_frames"] = 1
    bad_cases.append((hard, "HARD_CUT"))
    soft = timeline([24, 24])
    soft["entries"][0]["transition_out"].update(type="CROSSFADE", overlap_frames=2)
    bad_cases.append((soft, "CROSSFADE"))  # missing curve
    soft["entries"][0]["transition_out"]["curve"] = "EASE_IN"
    bad_cases.append((soft, "CROSSFADE"))  # wrong curve
    bad_cases += [
        (timeline([4, 4], [4]), "inside both"),          # O >= min(L_i, L_next)
        (timeline([10, 4, 10], [2, 3]), "Adjacent"),     # O0 + O1 > L_middle
        (timeline([24, 24], [-1]), "overlap"),
    ]
    wrong_target = timeline([24, 24])
    wrong_target["entries"][0]["transition_out"]["to_instance"] = "I999"
    bad_cases.append((wrong_target, "to_instance"))
    for doc, match in bad_cases:
        with pytest.raises(FilmError, match=match):
            validate_animation_timeline(doc)
    with pytest.raises(FilmError, match="output_frames"):
        validate_animation_timeline(valid, 47)


def test_reader_rejects_tampered_timeline(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1)
    animation_init(p)
    document = read(p / "timeline/edit.json")
    document["entries"][0]["unused_handles"]["after"] = 12
    write(p / "timeline/edit.json", document)  # non-canonical rewrite
    with pytest.raises(FilmError):
        load_animation_timeline(p)
    document["target_frames"] = 47
    write_canon(p / "timeline/edit.json", document)
    with pytest.raises(FilmError, match="target_frames"):
        load_animation_timeline(p)
