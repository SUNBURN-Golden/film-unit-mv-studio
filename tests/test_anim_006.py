"""ANIM-006: cut/transition review binding, Final-candidate output, Build 2.

All fixtures are synthetic (Pillow PNG frames, a generated sine master).
The `animation_review` records written here are protocol fixtures exercising
the engine boundary — they are not evidence of real artwork approval, and
nothing here is a production qualification, a paid generation or a release.
"""
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from engine import cli
from engine.animation_assets import import_frame_sequence
from engine.animation_compiler import frame_pixel_sha256
from engine.animation_review import (binding_digest, film_review_status,
                                     load_reviews, record_cut_review,
                                     record_film_review,
                                     record_transition_review, review_status)
from engine.animation_schema import canon_bytes, read_canon, write_canon
from engine.builds import replay_build, verify_build
from engine.compiler import compile_final, compile_preview
from engine.core import FilmError, digest, ffmpeg, read, write
from test_anim_003 import animation_project, make_sequence, rgba_frame
from test_compiler_v03 import (assert_master, fixture_project, newest_build)

SIZE = (64, 48)
REVIEWER = "Synthetic fixture reviewer"
CUT_METHODS = ["CUT_FULL_SPEED_PLAYBACK"]
TRANSITION_METHODS = ["TRANSITION_FULL_SPEED_PLAYBACK"]
FILM_METHODS = ["FULL_SPEED_WHOLE_FILM", "TECHNICAL_VALIDATION"]


def approved_project(tmp_path):
    """A converted project: two 96-frame cuts fully imported, nothing reviewed."""
    p = animation_project(tmp_path, shot_count=2, seconds=8)
    make_sequence(tmp_path / "seq_s001", count=96, size=SIZE, seed=10)
    make_sequence(tmp_path / "seq_s002", count=96, size=SIZE, seed=200)
    import_frame_sequence(p, "S001", folder=tmp_path / "seq_s001")
    import_frame_sequence(p, "S002", folder=tmp_path / "seq_s002")
    return p


def approve_all(p):
    """Record current cut + transition reviews; returns the review ids."""
    timeline = read_canon(p / "timeline/edit.json")
    ids = {"cuts": {}, "transitions": {}}
    for entry in timeline["entries"]:
        ids["cuts"][entry["instance_id"]] = record_cut_review(
            p, entry["instance_id"], reviewer=REVIEWER,
            methods=CUT_METHODS)["review_id"]
        transition = entry.get("transition_out")
        if transition is not None:
            ids["transitions"][transition["id"]] = record_transition_review(
                p, transition["id"], reviewer=REVIEWER,
                methods=TRANSITION_METHODS)["review_id"]
    return ids


def make_build(p):
    approve_all(p)
    result = compile_final(p)
    folder, record = newest_build(p)
    return folder, record, result


def decode_frame(mp4, index, target):
    ffmpeg(["-i", str(mp4), "-vf", f"select=eq(n\\,{index})",
            "-frames:v", "1", str(target)])
    return target


def differing_pixels(a, b, threshold=48):
    """Pixels whose RGB channels differ by more than `threshold`.

    Both deliveries are independent lossy encodes, so identical source
    frames can differ by codec noise; subtitle text lands as a strong,
    localized difference instead.
    """
    with Image.open(a) as ia, Image.open(b) as ib:
        pa = ia.convert("RGB").tobytes()
        pb = ib.convert("RGB").tobytes()
    return sum(1 for x, y in zip(pa, pb) if abs(x - y) > threshold)


# --- review records and the cut/transition gates (design 10) ---------------

def test_reviewed_edit_becomes_final_candidate(tmp_path):
    p = approved_project(tmp_path)
    ids = approve_all(p)
    result = compile_final(p)
    folder, record = newest_build(p)
    assert result["status"] == "COMPLETE"
    assert record["document_type"] == "animation_build"
    assert record["schema_version"] == 2
    assert record["storage_profile"] == "LOCAL_FULL"
    assert record["mode"] == "FINAL_CANDIDATE"
    assert record["candidate_state"] == "FINAL_CANDIDATE_READY"
    assert record["draft"] is False
    assert record["output_frames"] == 192
    # The recorded review references are exactly the current approvals.
    assert record["reviews"]["cuts"] == ids["cuts"]
    assert record["reviews"]["transitions"] == ids["transitions"]
    assert verify_build(folder)["valid"]


def test_unreviewed_cut_blocks_final_candidate(tmp_path):
    p = approved_project(tmp_path)
    # Locally imported members (generated=False assets) get no shortcut.
    with pytest.raises(FilmError, match="current review"):
        compile_final(p)
    status = review_status(p)
    assert status["targets"]["I001"]["state"] == "UNREVIEWED"
    assert not list(p.glob("builds/B*"))


def test_missing_transition_review_blocks(tmp_path):
    p = approved_project(tmp_path)
    timeline = read_canon(p / "timeline/edit.json")
    for entry in timeline["entries"]:
        record_cut_review(p, entry["instance_id"], reviewer=REVIEWER,
                          methods=CUT_METHODS)
    with pytest.raises(FilmError, match="TRANSITION T001: UNREVIEWED"):
        compile_final(p)
    assert not list(p.glob("builds/B*"))


def test_fix_required_decision_blocks(tmp_path):
    p = approved_project(tmp_path)
    record_cut_review(
        p, "I001", reviewer=REVIEWER, methods=CUT_METHODS,
        decision="FIX_REQUIRED",
        unresolved_major_issues=[{"disposition": "FIX_REQUIRED",
                                  "note": "exposure slips at the tail"}])
    record_cut_review(p, "I002", reviewer=REVIEWER, methods=CUT_METHODS)
    record_transition_review(p, "T001", reviewer=REVIEWER,
                             methods=TRANSITION_METHODS)
    status = review_status(p)
    assert status["targets"]["I001"]["state"] == "CHANGES_REQUIRED"
    with pytest.raises(FilmError, match="I001: CHANGES_REQUIRED"):
        compile_final(p)


def test_stale_cut_review_blocks(tmp_path):
    p = approved_project(tmp_path)
    approve_all(p)
    # One changed member opens a new revision; the adopted pin moves and the
    # review bound to revision 1 goes stale.
    folder = tmp_path / "seq_s001_v2"
    make_sequence(folder, count=96, size=SIZE, seed=10)
    rgba_frame(folder / "f0005.png", size=SIZE, seed=4242)
    second = import_frame_sequence(p, "S001", folder=folder)
    assert second["revision"] == 2
    status = review_status(p)
    assert status["targets"]["I001"]["state"] == "STALE"
    assert status["targets"]["I002"]["state"] == "CURRENT"
    with pytest.raises(FilmError, match="I001: STALE"):
        compile_final(p)
    assert not list(p.glob("builds/B*"))
    # Fresh reviews of the current content restore the gate; the transition
    # binds both sequence digests, so it needs a new binding too.
    record_cut_review(p, "I001", reviewer=REVIEWER, methods=CUT_METHODS)
    record_transition_review(p, "T001", reviewer=REVIEWER,
                             methods=TRANSITION_METHODS)
    assert all(row["state"] == "CURRENT"
               for row in review_status(p)["targets"].values())
    compile_final(p)
    assert newest_build(p)[1]["mode"] == "FINAL_CANDIDATE"


def test_stale_transition_review_blocks(tmp_path):
    p = approved_project(tmp_path)
    approve_all(p)
    # Change the recipe: HARD_CUT -> 2-frame LINEAR_INTERIOR_V1 crossfade.
    timeline = read_canon(p / "timeline/edit.json")
    timeline["entries"][0]["transition_out"] = {
        "id": "T001", "type": "CROSSFADE", "to_instance": "I002",
        "overlap_frames": 2, "curve": "LINEAR_INTERIOR_V1"}
    timeline["target_frames"] = 190
    write_canon(p / "timeline/edit.json", timeline)
    config = read(p / "project.yaml")
    config["animation"]["output_frames"] = 190
    write(p / "project.yaml", config)
    status = review_status(p)
    assert status["targets"]["T001"]["state"] == "STALE"
    assert status["targets"]["I001"]["state"] == "CURRENT"
    with pytest.raises(FilmError, match="T001: STALE"):
        compile_final(p)


def test_missing_member_blocks_before_reviews(tmp_path):
    p = approved_project(tmp_path)
    approve_all(p)
    member = p / "animation" / "assets" / "A0001" / "r1" / "f000005.png"
    member.unlink()
    with pytest.raises(FilmError, match="Missing asset member"):
        compile_final(p)
    assert not list(p.glob("builds/B*"))


def test_review_record_validation(tmp_path):
    p = approved_project(tmp_path)
    with pytest.raises(FilmError, match="required methods"):
        record_cut_review(p, "I001", reviewer=REVIEWER,
                          methods=["SLOW_SPAN_REVIEW"])
    with pytest.raises(FilmError, match="named reviewer"):
        record_cut_review(p, "I001", reviewer="  ", methods=CUT_METHODS)
    with pytest.raises(FilmError, match="Unknown review methods"):
        record_cut_review(p, "I001", reviewer=REVIEWER,
                          methods=["I_JUST_LOOKED"])
    with pytest.raises(FilmError, match="No timeline entry"):
        record_cut_review(p, "I999", reviewer=REVIEWER, methods=CUT_METHODS)
    # An accepted limitation needs an explicit reason and scope.
    record_cut_review(
        p, "I001", reviewer=REVIEWER, methods=CUT_METHODS,
        accepted_limitations=[{"disposition": "INTENTIONAL",
                               "note": "fixture note",
                               "reason": "fixture reason",
                               "scope": [0, 4]}])
    assert review_status(p)["targets"]["I001"]["state"] == "CURRENT"
    # A review record carries its digest binding; forged fields fail it.
    records = load_reviews(p)
    assert records[0]["binding_sha256"] == binding_digest(records[0])


def test_approvals_log_is_append_only_and_tamper_evident(tmp_path):
    p = approved_project(tmp_path)
    ids = approve_all(p)
    path = p / "production" / "approvals.jsonl"
    raw = path.read_bytes()
    lines = raw.split(b"\n")
    assert lines[-1] == b"" and len(lines) == 4
    for line, record in zip(lines, load_reviews(p)):
        assert line + b"\n" == canon_bytes(record)
    # Editing a recorded bound field breaks its stored binding digest.
    records = load_reviews(p)
    records[0]["used_source_range"] = [0, 97]
    path.write_bytes(b"".join(canon_bytes(r) for r in records))
    with pytest.raises(FilmError, match="binding_sha256"):
        load_reviews(p)
    # Restore; a partial last line is refused too.
    path.write_bytes(raw + b'{"half')
    with pytest.raises(FilmError, match="single LF"):
        load_reviews(p)
    path.write_bytes(raw)
    assert [r["review_id"] for r in load_reviews(p)] == [
        ids["cuts"]["I001"], ids["transitions"]["T001"], ids["cuts"]["I002"]]


def test_stale_lyric_review_blocks_final_candidate(tmp_path):
    p = approved_project(tmp_path)
    approve_all(p)
    timing = read(p / "lyrics/lyrics_timed.json")
    timing["cues"][0]["end_ms"] += 40
    write(p / "lyrics/lyrics_timed.json", timing)
    with pytest.raises(FilmError, match="lyrics|Lyric"):
        compile_final(p)
    assert not list(p.glob("builds/B*"))


# --- outputs: PNG sequence, clean/subbed split, global subtitle timeline ---

def test_delivery_frames_and_frame_map(tmp_path):
    p = approved_project(tmp_path)
    folder, record, _ = make_build(p)
    names = sorted(f.name for f in (folder / "final_frames").iterdir())
    assert names == [f"F_{i:06d}.png" for i in range(1, 193)]
    rows = [json.loads(line)
            for line in (folder / "frame_map.jsonl").read_text().splitlines()]
    assert len(rows) == 192
    for row in rows:
        assert row["file"] == f"final_frames/F_{row['frame_index'] + 1:06d}.png"
        assert row["output_sha256"] == digest(folder / row["file"])
        assert row["operations"][-1]["type"] == "SUBTITLE_OVERLAY"
        assert len(row["output_sha256"]) == 64
    # Review refs carry the cut ids; the hard cut adds its transition id.
    assert rows[0]["review_refs"] == [record["reviews"]["cuts"]["I001"]]
    assert rows[96]["sources"][0]["shot_id"] == "S002"
    assert record["sequences"]["clean_sequence_root"] \
        != record["sequences"]["subbed_sequence_root"]
    assert record["sequences"]["clean_frames_stored"] is False
    assert not (folder / ".clean_frames").exists()
    assert verify_build(folder)["valid"]


def test_clean_and_subbed_are_distinct_same_version(tmp_path):
    p = approved_project(tmp_path)
    folder, record, _ = make_build(p)
    for name in ("MASTER_CLEAN.mp4", "MASTER_SUBBED.mp4"):
        assert_master(folder / name, 8)
    # Original master AAC in the subbed master.
    config = read(p / "project.yaml")
    assert record["audio"]["sha256"] == digest(p / config["audio"]["path"])
    assert digest(folder / "MASTER_CLEAN.mp4") \
        != digest(folder / "MASTER_SUBBED.mp4")
    assert record["outputs"]["clean"]["sha256"] \
        == digest(folder / "MASTER_CLEAN.mp4")
    assert record["outputs"]["subbed"]["sha256"] \
        == digest(folder / "MASTER_SUBBED.mp4")


def test_subtitles_stay_on_the_global_timeline(tmp_path):
    """Cue milliseconds map onto global frames; off-cue frames are clean."""
    p = approved_project(tmp_path)
    folder, record, _ = make_build(p)
    cues = read(p / "lyrics/lyrics_timed.json")["cues"]
    assert cues[0]["start_ms"] == 0 and cues[0]["end_ms"] == 3920
    assert cues[1]["start_ms"] == 4000 and cues[1]["end_ms"] == 7920
    ass = (folder / "lyrics.ass").read_text()
    events = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert events[0].startswith("Dialogue: 0,0:00:00.00,0:00:03.92")
    assert events[1].startswith("Dialogue: 0,0:00:04.00,0:00:07.92")
    # Frame 50 sits at ~2083ms inside cue 0: the subbed pixels carry text
    # that the clean pixels do not.
    clean_cue = decode_frame(folder / "MASTER_CLEAN.mp4", 50,
                             tmp_path / "clean_cue.png")
    subbed_cue = decode_frame(folder / "MASTER_SUBBED.mp4", 50,
                              tmp_path / "subbed_cue.png")
    assert differing_pixels(clean_cue, subbed_cue) > 0
    # Frame 95 sits at ~3979ms inside the 3920-4000ms gap between cues:
    # both deliveries decode the same picture, codec noise aside.
    clean_gap = decode_frame(folder / "MASTER_CLEAN.mp4", 95,
                             tmp_path / "clean_gap.png")
    subbed_gap = decode_frame(folder / "MASTER_SUBBED.mp4", 95,
                              tmp_path / "subbed_gap.png")
    assert differing_pixels(clean_gap, subbed_gap) <= 8
    # The SRT carries the same reviewed cue milliseconds.
    srt = (folder / "lyrics.srt").read_text()
    assert "00:00:00,000 --> 00:00:03,920" in srt
    assert "00:00:04,000 --> 00:00:07,920" in srt


# --- Build 2: inventory, archive replay, tamper detection ------------------

def test_build2_replays_with_the_live_project_hidden(tmp_path):
    p = approved_project(tmp_path)
    folder, record, _ = make_build(p)
    archive = tmp_path / "archive" / folder.name
    archive.parent.mkdir()
    shutil.copytree(folder, archive)
    shutil.move(str(p), tmp_path / "hidden_project")
    out = tmp_path / "replay_out"
    result = replay_build(archive, out)
    assert result["build_id"] == record["build_id"]
    assert_master(out / "MASTER_SUBBED.mp4", 8)
    assert_master(out / "MASTER_CLEAN.mp4", 8)
    assert result["verified"]["clean_sequence_root"] \
        == record["sequences"]["clean_sequence_root"]
    assert result["verified"]["subbed_sequence_root"] \
        == record["sequences"]["subbed_sequence_root"]
    assert (out / "lyrics.ass").is_file() and (out / "lyrics.srt").is_file()
    assert (out / "frame_map.jsonl").read_text() \
        == (archive / "frame_map.jsonl").read_text()
    # The archived delivery PNGs re-encode to the replayed MP4's frames:
    # frame 50 in the replayed subbed master carries the same cue overlay.
    replayed = decode_frame(out / "MASTER_SUBBED.mp4", 50,
                            tmp_path / "replayed.png")
    original = decode_frame(archive / "MASTER_SUBBED.mp4", 50,
                            tmp_path / "original.png")
    # Identical inputs re-encoded the same way: codec noise only.
    assert differing_pixels(replayed, original) <= 200


def test_build2_inventory_tampering_is_detected(tmp_path):
    p = approved_project(tmp_path)
    folder, record, _ = make_build(p)
    # Untouched copies of the same sealed build for the other two cases:
    # the inventory check and replay read only the build folder itself.
    folder2 = Path(shutil.copytree(folder, tmp_path / "second"))
    folder3 = Path(shutil.copytree(folder, tmp_path / "third"))
    # A changed delivery frame.
    frame = folder / "final_frames" / "F_000050.png"
    data = bytearray(frame.read_bytes())
    data[len(data) // 2] ^= 0xFF
    frame.write_bytes(bytes(data))
    assert not verify_build(folder)["valid"]
    with pytest.raises(FilmError, match="integrity"):
        replay_build(folder, tmp_path / "out_a")

    # A changed archived member byte.
    snap = folder2 / "snapshot" / "animation" / "assets" / "A0001" / "r1"
    target = snap / "f000003.png"
    data = bytearray(target.read_bytes())
    data[100] ^= 0xFF
    target.write_bytes(bytes(data))
    assert not verify_build(folder2)["valid"]
    with pytest.raises(FilmError, match="integrity"):
        replay_build(folder2, tmp_path / "out_b")

    # A tampered frame_map row (rehashed manifest entry stays consistent? no:
    # build.json's file inventory pins frame_map.jsonl bytes too).
    lines = (folder3 / "frame_map.jsonl").read_bytes().split(b"\n")
    lines[4] = lines[4].replace(b"F_000005", b"F_000009")
    (folder3 / "frame_map.jsonl").write_bytes(b"\n".join(lines))
    assert not verify_build(folder3)["valid"]
    with pytest.raises(FilmError, match="integrity"):
        replay_build(folder3, tmp_path / "out_c")


def test_film_review_binds_the_sealed_manifest(tmp_path):
    p = approved_project(tmp_path)
    folder, record, _ = make_build(p)
    manifest_sha = digest(folder / "build.json")
    sealed_files = sorted(f.relative_to(folder)
                          for f in folder.rglob("*") if f.is_file())
    review = record_film_review(p, record["build_id"], reviewer=REVIEWER,
                                methods=FILM_METHODS)
    assert review["scope"] == "FINAL_FILM"
    assert review["build_manifest_sha256"] == manifest_sha
    assert review["deliverable_sha256"] == digest(folder / "MASTER_SUBBED.mp4")
    assert review["frame_sequence_root"] \
        == record["sequences"]["subbed_sequence_root"]
    assert review["edit_digest"] == record["edit_digest"]
    # Recording a review never modifies the sealed build.
    assert digest(folder / "build.json") == manifest_sha
    assert sorted(f.relative_to(folder) for f in folder.rglob("*")
                  if f.is_file()) == sealed_files
    status = film_review_status(p, record["build_id"])
    assert status["state"] == "CURRENT"
    assert status["review_id"] == review["review_id"]
    # Mutating the deliverable makes the film review stale.
    mp4 = folder / "MASTER_SUBBED.mp4"
    with mp4.open("ab") as stream:
        stream.write(b"tampered")
    assert film_review_status(p, record["build_id"])["state"] == "STALE"
    assert not verify_build(folder)["valid"]
    # A draft Preview build is not a reviewable Build 2 record.
    (tmp_path / "draft_case").mkdir()
    p2 = animation_project(tmp_path / "draft_case", shot_count=1, seconds=1)
    make_sequence(tmp_path / "draft_seq", count=24, size=SIZE)
    import_frame_sequence(p2, "S001", folder=tmp_path / "draft_seq")
    compile_preview(p2)
    draft_id = newest_build(p2)[1]["build_id"]
    with pytest.raises(FilmError, match="Build 2"):
        record_film_review(p2, draft_id, reviewer=REVIEWER,
                           methods=FILM_METHODS)


def test_unapproved_film_state_is_reported(tmp_path):
    p = approved_project(tmp_path)
    folder, record, _ = make_build(p)
    assert film_review_status(p, record["build_id"])["state"] == "UNREVIEWED"
    # The candidate state is reported, never claimed as final approval.
    assert record["candidate_state"] == "FINAL_CANDIDATE_READY"
    assert "approval" in record["warnings"][-1]


# --- boundaries kept: Build 1, draft Preview, original inputs --------------

def test_build1_replay_and_draft_preview_unchanged(tmp_path):
    legacy = (tmp_path / "legacy")
    legacy.mkdir()
    p = fixture_project(legacy, seconds=2, shot_count=1)
    compile_preview(p)
    folder, record = newest_build(p)
    replay_dir = tmp_path / "legacy_replay"
    result = replay_build(folder, replay_dir)
    assert_master(replay_dir / "MASTER_SUBBED.mp4", 2)
    assert result["replayed_from"] == record["build_id"]

    draft_root = tmp_path / "draft_root"
    draft_root.mkdir()
    p2 = animation_project(draft_root, shot_count=1, seconds=1)
    make_sequence(tmp_path / "draft_seq", count=24, size=SIZE)
    import_frame_sequence(p2, "S001", folder=tmp_path / "draft_seq")
    compile_preview(p2)
    draft_folder, draft_record = newest_build(p2)
    assert draft_record["document_type"] == "animation_draft_preview"
    with pytest.raises(FilmError, match="not replayable"):
        replay_build(draft_folder, tmp_path / "draft_replay")


def test_compile_leaves_master_lyrics_and_cues_untouched(tmp_path):
    p = approved_project(tmp_path)
    names = ["analysis/audio.json", "lyrics/lyrics_timed.json",
             "lyrics/lyrics_source.txt", "manifest/shots.json",
             "timeline/edit.json"]
    config = read(p / "project.yaml")
    names.append(config["audio"]["path"])
    before = {n: digest(p / n) for n in names}
    approve_all(p)
    make_build(p)
    for name, sha in before.items():
        assert digest(p / name) == sha


def test_cli_review_commands(tmp_path):
    p = approved_project(tmp_path)
    assert cli.main(["animation-reviews", str(p)]) == 0
    assert cli.main(["review-cut", str(p), "I001", "--reviewer", REVIEWER,
                     "--methods", "CUT_FULL_SPEED_PLAYBACK"]) == 0
    assert cli.main(["review-transition", str(p), "T001",
                     "--reviewer", REVIEWER,
                     "--methods", "TRANSITION_FULL_SPEED_PLAYBACK"]) == 0
    assert cli.main(["review-cut", str(p), "I001", "--reviewer", REVIEWER,
                     "--methods", "CUT_FULL_SPEED_PLAYBACK"]) == 0
    folder, record, _ = make_build(p)
    assert cli.main(["approve-film", str(p), "--build", record["build_id"],
                     "--reviewer", REVIEWER,
                     "--methods",
                     "FULL_SPEED_WHOLE_FILM,TECHNICAL_VALIDATION"]) == 0
    assert film_review_status(p, record["build_id"])["state"] == "CURRENT"
    # Legacy projects cannot record animation reviews.
    legacy_root = tmp_path / "legacy_root"
    legacy_root.mkdir()
    legacy = fixture_project(legacy_root, seconds=1, shot_count=1)
    assert cli.main(["review-cut", str(legacy), "I001", "--reviewer", "x",
                     "--methods", "CUT_FULL_SPEED_PLAYBACK"]) == 1


# --- FINAL_FILM staleness and path confinement -----------------------------

def test_film_review_stale_when_delivery_pixels_change(tmp_path):
    p = approved_project(tmp_path)
    folder, record, _ = make_build(p)
    record_film_review(p, record["build_id"], reviewer=REVIEWER,
                       methods=FILM_METHODS)
    assert film_review_status(p, record["build_id"])["state"] == "CURRENT"
    # Same file name, different pixels: the recomputed frame-sequence root
    # no longer matches and the sealed inventory fails too.
    rgba_frame(folder / "final_frames" / "F_000050.png",
               size=(320, 240), seed=999)
    assert film_review_status(p, record["build_id"])["state"] == "STALE"
    # A build that fails inventory cannot be bound to a new review either.
    with pytest.raises(FilmError, match="inventory"):
        record_film_review(p, record["build_id"], reviewer=REVIEWER,
                           methods=FILM_METHODS)


def test_film_review_stale_when_archived_font_changes(tmp_path):
    p = approved_project(tmp_path)
    folder, record, _ = make_build(p)
    record_film_review(p, record["build_id"], reviewer=REVIEWER,
                       methods=FILM_METHODS)
    assert film_review_status(p, record["build_id"])["state"] == "CURRENT"
    exported = Path(record["subtitles"]["font"]["exported_path"]).name
    font = folder / "subtitle_fonts" / exported
    assert font.is_file() and record["subtitles"]["font"]["sha256"]
    font.write_bytes(b"tampered font bytes\n")
    assert film_review_status(p, record["build_id"])["state"] == "STALE"


def test_film_review_stale_when_archived_lyrics_change(tmp_path):
    p = approved_project(tmp_path)
    folder, record, _ = make_build(p)
    record_film_review(p, record["build_id"], reviewer=REVIEWER,
                       methods=FILM_METHODS)
    assert film_review_status(p, record["build_id"])["state"] == "CURRENT"
    ass = folder / "lyrics.ass"
    ass.write_text(ass.read_text() + "Comment: tampered cue\n")
    assert film_review_status(p, record["build_id"])["state"] == "STALE"


def test_replay_refuses_audio_path_escaping_the_build(tmp_path):
    p = approved_project(tmp_path)
    folder, record, _ = make_build(p)
    manifest = read(folder / "build.json")
    original = manifest["audio"]["path"]
    # build.json is not in its own inventory; absolute and parent-escaping
    # audio paths must still be refused before any output is written.
    for bad in (str(tmp_path / "outside.m4a"), "../outside.m4a"):
        manifest["audio"]["path"] = bad
        write(folder / "build.json", manifest)
        assert verify_build(folder)["valid"]
        with pytest.raises(FilmError, match="inside"):
            replay_build(folder, tmp_path / "replay_escape")
        assert not (tmp_path / "replay_escape").exists()
    manifest["audio"]["path"] = original
    write(folder / "build.json", manifest)


def test_replay_refuses_registry_member_escaping_the_snapshot(tmp_path):
    p = approved_project(tmp_path)
    folder, record, _ = make_build(p)
    registry_path = folder / "snapshot" / "manifest" / "animation_assets.json"
    registry = read_canon(registry_path)
    revision = registry["assets"]["A0001"]["revisions"]["1"]
    revision["files"][0]["relative_name"] = "../escape.png"
    write_canon(registry_path, registry)
    # Keep the sealed inventory honest so the traversal itself is what fails.
    manifest = read(folder / "build.json")
    manifest["files"]["snapshot/manifest/animation_assets.json"] = \
        digest(registry_path)
    write(folder / "build.json", manifest)
    assert verify_build(folder)["valid"]
    with pytest.raises(FilmError, match="inside"):
        replay_build(folder, tmp_path / "replay_registry")
    assert not (tmp_path / "replay_registry").exists()


def test_film_review_refuses_escaping_identifiers(tmp_path):
    p = approved_project(tmp_path)
    _, record, _ = make_build(p)
    build_id = record["build_id"]
    for deliverable in ("../x", "snapshot/../build.json", "/etc/passwd"):
        with pytest.raises(FilmError, match="deliverable"):
            record_film_review(p, build_id, reviewer=REVIEWER,
                               methods=FILM_METHODS, deliverable=deliverable)
    for bad_id in ("../outside", "B0001/x", "draft"):
        with pytest.raises(FilmError, match="build id"):
            record_film_review(p, bad_id, reviewer=REVIEWER,
                               methods=FILM_METHODS)
    with pytest.raises(FilmError, match="No build"):
        record_film_review(p, "B9999", reviewer=REVIEWER,
                           methods=FILM_METHODS)
    # The sealed build is untouched and still reviewable.
    record_film_review(p, build_id, reviewer=REVIEWER, methods=FILM_METHODS)
    assert film_review_status(p, build_id)["state"] == "CURRENT"


def test_later_fix_required_supersedes_earlier_approval(tmp_path):
    """Latest decision on identical digests wins — for cuts and the film."""
    p = approved_project(tmp_path)
    record_cut_review(p, "I001", reviewer=REVIEWER, methods=CUT_METHODS)
    assert review_status(p)["targets"]["I001"]["state"] == "CURRENT"
    record_cut_review(
        p, "I001", reviewer=REVIEWER, methods=CUT_METHODS,
        decision="FIX_REQUIRED",
        unresolved_major_issues=[{"disposition": "FIX_REQUIRED",
                                  "note": "edge flicker at the cut point"}])
    assert review_status(p)["targets"]["I001"]["state"] == "CHANGES_REQUIRED"
    # A still-later approval on the same digests restores the gate.
    record_cut_review(p, "I001", reviewer=REVIEWER, methods=CUT_METHODS)
    assert review_status(p)["targets"]["I001"]["state"] == "CURRENT"

    folder, record, _ = make_build(p)
    record_film_review(p, record["build_id"], reviewer=REVIEWER,
                       methods=FILM_METHODS)
    assert film_review_status(p, record["build_id"])["state"] == "CURRENT"
    record_film_review(
        p, record["build_id"], reviewer=REVIEWER, methods=FILM_METHODS,
        decision="FIX_REQUIRED",
        unresolved_major_issues=[{"disposition": "FIX_REQUIRED",
                                  "note": "audio pop near the join"}])
    assert film_review_status(p, record["build_id"])["state"] \
        == "CHANGES_REQUIRED"
