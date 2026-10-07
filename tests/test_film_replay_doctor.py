"""film-replay-doctor — replay/rebuild diagnosis, bounded restore honesty,
and credential-free replay packets.

All fixtures are synthetic: Pillow PNG frames, a generated sine master and
the explicit FakeDriveBackend — no network, no OAuth, no credentials. The
Build 2 seals and review bindings exercised here are protocol fixtures, not
artwork approval; every external dependency stays reported as
UNQUALIFIED / PENDING / NOT_AUTHORIZED.
"""
import copy
import json
import shutil

import pytest

from anim_013_kit import archived, make_members
from engine import cli
from engine.animation_schema import canon_bytes
from engine.builds import replay_build
from engine.compiler import compile_final, compile_preview
from engine.core import FilmError, read, write
from engine.fav_pack import index_bytes, sha256_bytes
from engine.replay_doctor import (ARCHIVE_UNAVAILABLE, ARTIFACT_NEW,
                                  BLOCKED, HASH_MISMATCH, INCOMPLETE_BUILD,
                                  INDEX_FAULT, MISSING_ASSET, PACK_FAULT,
                                  REBUILD_IMPOSSIBLE, REBUILD_NEW_RESULT,
                                  REBUILD_SAME_INPUTS, REBUILD_UNKNOWN,
                                  RANGE_FAULT, REPRODUCE_NEW_RESULT,
                                  REPRODUCE_SAME, RESTORE_MEMBER_RANGES,
                                  RESTORE_WHOLE_PACK, SCHEMA_INVALID,
                                  TOOL_VERSION_DIFF, UNSUPPORTED_PROFILE,
                                  build_replay_packet,
                                  compare_replay_output, diagnose_archive,
                                  diagnose_build, read_replay_packet,
                                  restore_plan, validate_replay_packet,
                                  write_replay_packet)
from engine.storage_backends.fake_drive import FakeDriveBackend
from test_anim_003 import make_sequence
from test_anim_006 import approve_all, approved_project
from test_compiler_v03 import fixture_project, newest_build

SIZE = (64, 48)


def classes(report):
    return {p["class"] for p in report["problems"]}


def blocking(report):
    return {p["class"] for p in report["problems"] if p["blocking"]}


def link(report, name):
    return next(r for r in report["linkage"] if r["link"] == name)


# --- sealed Build 2 / Build 1 fixtures ----------------------------------------

@pytest.fixture(scope="module")
def build2_case(tmp_path_factory):
    """One sealed Build 2 FINAL_CANDIDATE inside its live project."""
    root = tmp_path_factory.mktemp("doctor_b2")
    (root / "proj").mkdir()
    p = approved_project(root / "proj")
    approve_all(p)
    assert compile_final(p)["status"] == "COMPLETE"
    folder, record = newest_build(p)
    return p, folder, record


@pytest.fixture()
def build2(tmp_path, build2_case):
    """An isolated copy of the project + sealed build per test."""
    source, build, _ = build2_case
    p = tmp_path / "proj"
    shutil.copytree(source, p)
    return p, p / "builds" / build.name


@pytest.fixture()
def build1(tmp_path):
    """A sealed legacy Build 1 preview (LEGACY_MV, schema 1)."""
    (tmp_path / "legacy").mkdir()
    p = fixture_project(tmp_path / "legacy", seconds=2, shot_count=1)
    assert compile_preview(p)["status"] == "COMPLETE"
    folder, record = newest_build(p)
    assert record.get("document_type") is None \
        and record["schema_version"] == 1
    return p, folder, record


# --- build doctor: healthy linkage + branches ----------------------------------

def test_build2_healthy_diagnosis_links_source_to_encode(build2):
    p, folder = build2
    report = diagnose_build(folder, deep=True)
    assert report["build_kind"] == "BUILD_2"
    assert report["problems"] == []
    assert report["reproduction"]["branch"] == REPRODUCE_SAME
    assert report["inventory"]["checked"] \
        == report["inventory"]["entries"] > 0
    # source / recipe / clean / subbed / encode linkage all resolved
    names = {r["link"] for r in report["linkage"]}
    assert {"source:master_audio", "recipe:edit_digest", "source:members",
            "recipe:frame_map", "clean:clean_sequence_root",
            "subbed:subbed_sequence_root", "subbed:delivery_sequence",
            "subbed:lyrics.ass", "subbed:lyrics_timing",
            "encode:clean_artifact", "encode:subbed_artifact",
            "encode:clean_digest", "encode:subbed_digest",
            "storage_profile",
            "clean:recomposed_root", "subbed:stored_pixel_root"} <= names
    assert all(r["ok"] for r in report["linkage"])
    # live project unchanged → a rebuild would carry the same inputs
    assert report["rebuild"]["state"] == REBUILD_SAME_INPUTS
    assert report["qualification"]["qualification_state"] == "UNQUALIFIED"


def test_build1_legacy_diagnosis_unchanged(build1):
    p, folder, record = build1
    report = diagnose_build(folder)
    assert report["build_kind"] == "BUILD_1"
    assert report["problems"] == []
    assert report["reproduction"]["branch"] == REPRODUCE_SAME
    assert link(report, "source:master_audio")["ok"]
    assert report["rebuild"]["state"] == REBUILD_SAME_INPUTS
    # LEGACY_MV replay itself is untouched by the doctor.
    result = replay_build(folder, folder.parent / "replay_out")
    assert result["replayed_from"] == record["build_id"]


# --- classification ------------------------------------------------------------

def test_missing_asset_is_classified_and_blocking(build2):
    p, folder = build2
    member = next((folder / "snapshot").rglob("f*.png"))
    member.unlink()
    report = diagnose_build(folder)
    assert MISSING_ASSET in blocking(report)
    assert report["reproduction"]["branch"] == BLOCKED


def test_hash_mismatch_is_classified(build2):
    p, folder = build2
    # A member byte tampered after sealing.
    member = next((folder / "snapshot").rglob("f*.png"))
    data = bytearray(member.read_bytes())
    data[len(data) // 2] ^= 0xFF
    member.write_bytes(bytes(data))
    report = diagnose_build(folder)
    assert HASH_MISMATCH in blocking(report)
    assert report["reproduction"]["branch"] == BLOCKED


def test_seal_field_drift_is_hash_mismatch_not_silent(build2):
    p, folder = build2
    # build.json itself is not inventoried — the doctor still detects pins
    # that drifted away from the inventoried bytes.
    record = read(folder / "build.json")
    record["outputs"]["subbed"]["sha256"] = "0" * 64
    write(folder / "build.json", record)
    report = diagnose_build(folder)
    assert HASH_MISMATCH in blocking(report)
    assert link(report, "encode:subbed_artifact")["ok"] is False


def test_tool_version_diff_forces_new_result_branch(build2):
    p, folder = build2
    current = {"python": "3.11.0",
               "ffmpeg": "ffmpeg version 5.1.4 fixture",
               "compiler": "0.3.0"}
    report = diagnose_build(folder, current_toolchain=current)
    assert TOOL_VERSION_DIFF in classes(report)
    # Drift is a warning class: replay can run, but produced bytes are a
    # new artifact — never the sealed build.
    assert report["reproduction"]["branch"] == REPRODUCE_NEW_RESULT
    assert blocking(report) == set()
    assert report["toolchain"]["differences"]


def test_unsupported_profiles_are_classified(build2):
    p, folder = build2
    record = read(folder / "build.json")
    record["storage_profile"] = "GDRIVE_AUTO"
    write(folder / "build.json", record)
    report = diagnose_build(folder)
    assert UNSUPPORTED_PROFILE in blocking(report)
    assert report["reproduction"]["branch"] == BLOCKED

    # DRIVE_BOUNDED is honest: it needs its pinned archive, not a guess.
    record = read(folder / "build.json")
    record["storage_profile"] = "DRIVE_BOUNDED"
    write(folder / "build.json", record)
    report = diagnose_build(folder)
    assert ARCHIVE_UNAVAILABLE in blocking(report)


def test_draft_preview_is_not_a_replayable_build(build2):
    p, folder = build2
    record = read(folder / "build.json")
    record["document_type"] = "animation_draft_preview"
    write(folder / "build.json", record)
    report = diagnose_build(folder)
    assert UNSUPPORTED_PROFILE in blocking(report)
    assert report["build_kind"] is None


def test_invalid_schema_and_corrupt_documents(build2):
    p, folder = build2
    # Invalid schema: a non-canonical frame_map line.
    lines = (folder / "frame_map.jsonl").read_bytes().split(b"\n")
    lines[3] = b'{"z": 1, "a": 2}'
    (folder / "frame_map.jsonl").write_bytes(b"\n".join(lines))
    report = diagnose_build(folder)
    assert SCHEMA_INVALID in blocking(report)
    assert link(report, "recipe:frame_map")["ok"] is False

    # Invalid schema: the snapshot edit is not a timeline document.
    edit = folder / "snapshot" / "timeline" / "edit.json"
    edit.write_bytes(b'{"document_type": "bogus", "schema_version": 9}\n')
    report = diagnose_build(folder)
    assert SCHEMA_INVALID in blocking(report)


def test_incomplete_build_is_classified(build2):
    p, folder = build2
    record = read(folder / "build.json")
    record["status"] = "FAILED"
    write(folder / "build.json", record)
    report = diagnose_build(folder)
    assert INCOMPLETE_BUILD in blocking(report)


def test_deep_recompose_confirms_and_catches_root_drift(build2):
    p, folder = build2
    report = diagnose_build(folder, deep=True)
    assert link(report, "clean:recomposed_root")["ok"]
    assert link(report, "subbed:stored_pixel_root")["ok"]

    # Tamper a member AND its inventory/registry pins are left intact — the
    # byte-level drift is caught at inventory+link time, and the recomposed
    # root additionally proves the recipe no longer reproduces.
    member = next((folder / "snapshot").rglob("f*.png"))
    data = bytearray(member.read_bytes())
    data[len(data) // 2] ^= 0xFF
    member.write_bytes(bytes(data))
    report = diagnose_build(folder, deep=True)
    assert HASH_MISMATCH in blocking(report)


# --- rebuild branch: same inputs vs new result vs impossible --------------------

def test_rebuild_reports_new_result_when_source_changes(build2):
    p, folder = build2
    from engine.animation_assets import import_frame_sequence
    make_sequence(folder.parent.parent / "seq_new", count=96,
                  size=SIZE, seed=77)
    import_frame_sequence(p, "S001", folder=folder.parent.parent / "seq_new")
    report = diagnose_build(folder)
    assert report["rebuild"]["state"] == REBUILD_NEW_RESULT
    assert report["rebuild"]["layers"]["sources"]["state"] == "CHANGED"
    # Acceptance 1: the report's own words — a changed rebuild is never
    # reported as the existing build.
    assert "never" in report["rebuild"]["note"]
    assert folder.name in report["rebuild"]["note"] or "this build" \
        in report["rebuild"]["note"]


def test_rebuild_impossible_when_pins_vanish(build2):
    p, folder = build2
    (p / "manifest" / "animation_assets.json").unlink()
    report = diagnose_build(folder)
    assert report["rebuild"]["state"] == REBUILD_IMPOSSIBLE
    assert report["rebuild"]["layers"]["sources"]["state"] == "MISSING"


def test_rebuild_unknown_when_project_absent(build2):
    p, folder = build2
    stray = folder.parent.parent.parent / "stray_build"
    shutil.copytree(folder, stray)
    report = diagnose_build(stray)
    assert report["rebuild"]["state"] == REBUILD_UNKNOWN
    # The seal itself is still fully diagnosable without the live project.
    assert report["reproduction"]["branch"] == REPRODUCE_SAME


# --- acceptance 1: produced bytes vs the sealed identity -------------------------

def test_replay_output_identical_bytes_are_identical(build2):
    p, folder = build2
    out = folder.parent / "replay_out"
    replay_build(folder, out)
    result = compare_replay_output(folder, out)
    assert result["result"] == "REPRODUCED_IDENTICAL"
    assert result["same_build_bytes"] is True
    assert result["summary"]["new_artifacts"] == 0


def test_differing_replay_bytes_are_new_artifact_never_same_build(build2):
    p, folder = build2
    out = folder.parent / "replay_out"
    replay_build(folder, out)
    # A different encoder/toolchain run produces different bytes.
    mp4 = out / "MASTER_SUBBED.mp4"
    with mp4.open("ab") as stream:
        stream.write(b"different-encode")
    result = compare_replay_output(folder, out)
    assert result["result"] == "REPRODUCED_NEW_ARTIFACT"
    assert result["same_build_bytes"] is False
    row = next(f for f in result["files"] if f["file"] == "MASTER_SUBBED.mp4")
    assert row["verdict"] == ARTIFACT_NEW
    assert row["produced_sha256"] != row["expected_sha256"]
    # The label never calls changed bytes the sealed build.
    assert "NEW" in result["label"] and "identical" not in \
        result["label"].lower()


def test_incomplete_replay_output_is_not_identical(build2):
    p, folder = build2
    out = folder.parent / "replay_out"
    replay_build(folder, out)
    (out / "MASTER_SUBBED.mp4").unlink()
    result = compare_replay_output(folder, out)
    assert result["result"] == "INCOMPLETE"
    assert result["same_build_bytes"] is False
    assert "MASTER_SUBBED.mp4" in result["not_produced"]


# --- archive doctor: bounded restore honesty ------------------------------------

@pytest.fixture()
def archive_case():
    result, backend = archived(members=make_members(6))
    return result["archive"], backend, result


def test_restore_plan_uses_bounded_member_ranges(archive_case):
    doc, backend, _ = archive_case
    plan = restore_plan(_fetch_index(doc, backend))
    assert plan["branch"] == RESTORE_MEMBER_RANGES
    assert plan["unrequested_bytes"] == 0
    assert plan["member_bytes"] == sum(r[1] for r in plan["byte_ranges"])


def _fetch_index(doc, backend):
    from engine.pack_reader import fetch_index
    pack = doc["pack"]
    return fetch_index(backend, pack["index_object_id"],
                       pack["index_sha256"])


def test_bounded_restore_never_silently_becomes_full_download(archive_case):
    doc, backend, result = archive_case
    pack_id = result["pack_object_id"]
    backend.range_supported = False
    backend.requests.clear()
    report = diagnose_archive(doc, backend)
    # Acceptance 2: refused outright — the undeclared body is never read.
    assert report["restore"]["branch"] == BLOCKED
    assert report["restore"]["reason"] == "RANGE_UNSUPPORTED"
    assert not any(r["object_id"] == pack_id
                   and r["op"] in ("get_object", "get_range")
                   for r in backend.requests)


def test_whole_pack_branch_requires_a_declared_cap(archive_case):
    doc, backend, result = archive_case
    pack_id = result["pack_object_id"]
    pack_len = doc["pack"]["byte_length"]
    backend.range_supported = False
    # Cap below the pack size: still refused, body never fetched.
    backend.requests.clear()
    report = diagnose_archive(doc, backend, whole_pack_cap=pack_len - 1)
    assert report["restore"]["branch"] == BLOCKED
    assert report["restore"]["reason"] == "CAPACITY_BLOCKED"
    assert not any(r["object_id"] == pack_id
                   and r["op"] in ("get_object", "get_range")
                   for r in backend.requests)
    # Declared cap ≥ pack: the explicit WHOLE_PACK_VERIFIED branch.
    report = diagnose_archive(doc, backend, whole_pack_cap=pack_len)
    assert report["restore"]["branch"] == RESTORE_WHOLE_PACK
    assert report["branch"] == RESTORE_WHOLE_PACK
    assert report["transfer"]["fetched_bytes"] == pack_len
    assert report["transfer"]["verification_state"] == "VERIFIED_FULL_PACK"


def test_member_ranges_fetch_exactly_the_selected_bytes(archive_case):
    doc, backend, result = archive_case
    index = _fetch_index(doc, backend)
    picked = [m["member_id"] for m in index["members"][:2]]
    backend.requests.clear()
    report = diagnose_archive(doc, backend, members=picked)
    assert report["branch"] == RESTORE_MEMBER_RANGES
    wanted = {m["member_id"]: m for m in index["members"]
              if m["member_id"] in set(picked)}
    expected = sum(m["byte_length"] for m in wanted.values())
    # Only range reads of the selected members hit the pack — the rest of
    # the pack bytes are never transferred.
    assert report["transfer"]["fetched_bytes"] == expected
    assert report["transfer"]["unrequested_bytes"] == 0
    # Object metadata calls are fine; the pack body moves only as exact
    # member ranges — never a whole-object transfer.
    transfers = [r for r in backend.requests
                 if r["object_id"] == result["pack_object_id"]
                 and r["op"] in ("get_range", "get_object")]
    assert all(r["op"] == "get_range" for r in transfers)
    assert sum(r.get("bytes", 0) for r in transfers) == expected
    assert sorted(r["member_id"] for r in report["members"]) \
        == sorted(picked)


def test_corrupt_pack_is_classified(archive_case):
    doc, backend, result = archive_case
    index = _fetch_index(doc, backend)
    member = index["members"][0]
    # Corrupt the stored pack bytes inside the first member's range.
    data = bytearray(backend._objects[result["pack_object_id"]])
    data[member["byte_offset"] + member["byte_length"] // 2] ^= 0xFF
    backend._objects[result["pack_object_id"]] = bytes(data)
    report = diagnose_archive(doc, backend)
    assert report["branch"] == BLOCKED
    assert PACK_FAULT in blocking(report)
    fault = next(m for m in report["members"]
                 if m["member_id"] == member["member_id"])
    assert fault["state"] == "FAULT"


def test_wrong_index_pin_is_classified(archive_case):
    doc, backend, result = archive_case
    backend._objects[result["index_object_id"]] = b"tampered index"
    report = diagnose_archive(doc, backend)
    assert report["branch"] == BLOCKED
    assert INDEX_FAULT in blocking(report)


def test_invalid_index_schema_is_classified(archive_case):
    doc, backend, result = archive_case
    # A canonically stored but schema-invalid index behind a matching pin.
    bad = copy.deepcopy(_fetch_index(doc, backend))
    bad["schema_version"] = 9
    raw = index_bytes(bad)
    bad_id = f"sha256-{sha256_bytes(raw)}"
    backend.put_object(bad_id, raw)
    doc = copy.deepcopy(doc)
    doc["pack"]["index_object_id"] = bad_id
    doc["pack"]["index_sha256"] = sha256_bytes(raw)
    doc["objects"][1].update(object_id=bad_id, byte_length=len(raw),
                            sha256=sha256_bytes(raw))
    report = diagnose_archive(doc, backend)
    assert report["branch"] == BLOCKED
    assert INDEX_FAULT in blocking(report)


def test_invalid_range_responses_are_classified(archive_case):
    doc, backend, result = archive_case
    pack_id = result["pack_object_id"]
    # A short 206 body.
    backend.truncate_next[pack_id] = 3
    report = diagnose_archive(doc, backend)
    assert report["branch"] == BLOCKED
    assert RANGE_FAULT in blocking(report)
    # A wrong declared total length.
    backend2 = FakeDriveBackend(objects=dict(backend._objects))
    backend2.bad_total_next[pack_id] = 999999
    report = diagnose_archive(doc, backend2)
    assert RANGE_FAULT in blocking(report)


def test_invalid_manifest_schema_is_classified(archive_case):
    doc, backend, result = archive_case
    bad = copy.deepcopy(doc)
    bad["schema_version"] = 7
    report = diagnose_archive(bad, backend)
    assert report["branch"] == BLOCKED
    assert SCHEMA_INVALID in blocking(report)
    extra = copy.deepcopy(doc)
    extra["api_key"] = "fixture"
    report = diagnose_archive(extra, backend)
    assert SCHEMA_INVALID in blocking(report)


def test_offline_restore_is_the_explicit_whole_pack_branch(archive_case):
    doc, backend, result = archive_case
    index = _fetch_index(doc, backend)
    # Offline replay can only ever be the whole sealed object — it must be
    # declared and capped, not smuggled in by a range fallback.
    plan = restore_plan(index, offline=True)
    assert plan["branch"] == BLOCKED
    assert plan["reason"] == "RANGE_UNSUPPORTED"
    plan = restore_plan(index, offline=True,
                        whole_pack_cap=index["pack_byte_length"])
    assert plan["branch"] == RESTORE_WHOLE_PACK


# --- acceptance 3: the replay packet carries no credentials ----------------------

def test_replay_packet_is_canonical_and_credential_free(build2):
    p, folder = build2
    packet = build_replay_packet(folder)
    validate_replay_packet(packet)
    assert packet["document_type"] == "replay_packet"
    assert packet["schema_version"] == 1
    assert packet["credentials"] == {"embedded": "NEVER", "required": []}
    # Content pins only — members, edit, frame_map, deliverables.
    roles = {i["role"] for i in packet["inputs"]}
    assert {"master_audio", "timeline_edit", "asset_registry",
            "frame_map", "source_member", "delivery_frame"} <= roles
    assert all(i["sha256"] and i["byte_length"] > 0
               for i in packet["inputs"])
    mp4s = [d for d in packet["deliverables"]
            if d["file"].endswith(".mp4")]
    assert mp4s and all(d["expectation"] ==
                        "CONTENT_EQUAL_BYTES_MAY_DIFFER" for d in mp4s)
    # The serialized bytes themselves carry nothing token-shaped.
    raw = canon_bytes(packet)
    for needle in (b"token", b"Bearer", b"ya29.", b"secret", b"oauth",
                   b"PRIVATE KEY"):
        assert needle not in raw
    assert packet["reproduction"]["branch"] == REPRODUCE_SAME
    assert packet["qualification"]["acceptance_state"] == "PENDING"


def test_replay_packet_round_trip_and_rejection(build2):
    p, folder = build2
    packet = build_replay_packet(folder)
    path = write_replay_packet(folder.parent / "packet.json", packet)
    assert path.read_bytes() == canon_bytes(packet)
    loaded = read_replay_packet(path)
    assert loaded["packet_id"] == packet["packet_id"]

    # Secret-shaped keys and values are refused.
    injected = copy.deepcopy(packet)
    injected["notes"].append("Authorization: Bearer abcdef1234567890")
    with pytest.raises(FilmError, match="credential"):
        validate_replay_packet(injected)
    injected = copy.deepcopy(packet)
    injected["restore"]["note"] = "ya29.A0ARrdaM_fixture_token_value"
    with pytest.raises(FilmError, match="credential"):
        validate_replay_packet(injected)
    injected = copy.deepcopy(packet)
    injected["credentials"]["required"] = [
        {"kind": "ARCHIVE_READ", "in_packet": True, "note": "x"}]
    with pytest.raises(FilmError, match="never embedded"):
        validate_replay_packet(injected)
    injected = copy.deepcopy(packet)
    injected["credentials"]["embedded"] = "OPTIONAL"
    with pytest.raises(FilmError, match="NEVER"):
        validate_replay_packet(injected)


def test_packet_for_blocked_build_reports_branch(build2):
    p, folder = build2
    (folder / "lyrics.ass").unlink()
    packet = build_replay_packet(folder)
    assert packet["reproduction"]["branch"] == BLOCKED
    assert "MISSING_ASSET" in packet["reproduction"]["blocking"]


# --- CLI smoke -------------------------------------------------------------------

def test_cli_build_doctor_packet_and_check(build2, tmp_path, capsys):
    p, folder = build2
    assert cli.main(["build-doctor", str(folder)]) == 0
    capsys.readouterr()
    out = tmp_path / "packet.json"
    assert cli.main(["replay-packet", str(folder),
                     "--output", str(out)]) == 0
    capsys.readouterr()
    loaded = read_replay_packet(out)
    assert loaded["build"]["build_id"] == folder.name
    produced = tmp_path / "produced"
    replay_build(folder, produced)
    assert cli.main(["replay-check", str(folder), str(produced)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["result"] == "REPRODUCED_IDENTICAL"


def test_cli_archive_doctor(tmp_path, capsys):
    result, backend = archived(members=make_members(5),
                               backend=FakeDriveBackend(root=tmp_path / "arc"))
    root = tmp_path / "arc"
    manifests = root / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    manifest = manifests / "sealed.json"
    manifest.write_bytes(canon_bytes(result["archive"]))
    assert cli.main(["archive-doctor", str(manifest)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["branch"] == RESTORE_MEMBER_RANGES
    assert output["transfer"]["unrequested_bytes"] == 0
