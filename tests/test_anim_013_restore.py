"""ANIM-013 restore fixtures: sparse verified reads, VERIFIED_MEMBERS vs
VERIFIED_FULL_PACK, whole-pack fallback caps, interruption/resume, bounded
cache reuse, restore boundaries and the offline gate — the PACK-SPARSE,
HTTP fallback, INTERRUPT and RESTORE-BOUNDARY fixtures end to end.
"""
import pytest

from anim_013_kit import archived, connect, fake_backend, make_members

from engine.core import FilmError
from engine.drive_archive import (archive_frames, archive_status,
                                  restore_archive)
from engine.fav_pack import index_bytes, make_index, sha256_bytes
from engine.pack_reader import (VERIFIED_FULL_PACK, VERIFIED_MEMBERS,
                                PackReader, fetch_index)
from engine.storage_backends import (ConnectionDropped, RangeResult)
from engine.workspace import Workspace

# A fully declared schema §11 retry policy for the drop/resume fixtures.
RETRY = {"max_retries": 3, "backoff_base_ms": 0, "max_backoff_ms": 0,
         "max_elapsed_ms": 60000, "max_requests": 32,
         "max_transferred_bytes": 1 << 24}


def _reader(result, backend, cap=None):
    index = fetch_index(backend, result["index_object_id"],
                        result["archive"]["pack"]["index_sha256"],
                        sleep_fn=lambda ms: None)
    return PackReader(backend, result["pack_object_id"], index,
                      whole_pack_cap=cap, sleep_fn=lambda ms: None)


def test_sparse_member_reads_fetch_only_needed_ranges():
    result, backend = archived()
    reader = _reader(result, backend)
    index = reader.index
    target = index["members"][1]
    data = reader.read_member(target["member_id"])
    assert sha256_bytes(data) == target["sha256"]
    assert reader.state == VERIFIED_MEMBERS
    ranges = [r for r in backend.requests if r["op"] == "get_range"
              and r["object_id"] == result["pack_object_id"]]
    assert ranges == [{"op": "get_range",
                       "object_id": result["pack_object_id"],
                       "offset": target["byte_offset"],
                       "length": target["byte_length"],
                       "bytes": target["byte_length"], "status": 206}]
    assert reader.fetched_bytes == target["byte_length"]


def test_full_pack_verification_is_a_distinct_state():
    result, backend = archived()
    reader = _reader(result, backend)
    reader.verify_full_pack()
    assert reader.state == VERIFIED_FULL_PACK
    # Members served from the verified whole; no range requests happened.
    assert not any(r["op"] == "get_range" for r in backend.requests)


def test_whole_body_answer_to_range_needs_a_predeclared_cap():
    result, backend = archived()
    backend.range_supported = False
    reader = _reader(result, backend, cap=1 << 22)
    target = reader.index["members"][0]
    data = reader.read_member(target["member_id"])
    assert sha256_bytes(data) == target["sha256"]
    assert reader.state == VERIFIED_FULL_PACK


def test_undeclared_or_over_cap_whole_pack_response_is_refused():
    result, backend = archived()
    pack_length = result["archive"]["pack"]["byte_length"]
    backend.range_supported = False
    reader = _reader(result, backend)          # no cap declared
    with pytest.raises(FilmError, match="RANGE_FALLBACK_DENIED"):
        reader.read_member(reader.index["members"][0]["member_id"])
    reader = _reader(result, backend, cap=pack_length - 1)
    with pytest.raises(FilmError, match="RANGE_FALLBACK_DENIED"):
        reader.read_member(reader.index["members"][0]["member_id"])


def test_whole_pack_cap_refusal_happens_before_any_body_read():
    # The cap gate runs on the known totals — a backend that cannot serve
    # ranges is refused before the whole body is ever requested.
    result, backend = archived()
    index = fetch_index(backend, result["index_object_id"],
                        result["archive"]["pack"]["index_sha256"],
                        sleep_fn=lambda ms: None)
    pack_length = result["archive"]["pack"]["byte_length"]

    def bomb(*_args):
        raise AssertionError("a pack body read was attempted")

    backend.range_supported = False
    backend.get_range = bomb
    backend.get_object = bomb
    reader = PackReader(backend, result["pack_object_id"], index,
                        sleep_fn=lambda ms: None)
    with pytest.raises(FilmError, match="RANGE_FALLBACK_DENIED"):
        reader.read_member(index["members"][0]["member_id"])
    reader = PackReader(backend, result["pack_object_id"], index,
                        whole_pack_cap=pack_length - 1,
                        sleep_fn=lambda ms: None)
    with pytest.raises(FilmError, match="RANGE_FALLBACK_DENIED"):
        reader.read_member(index["members"][0]["member_id"])


def test_206_without_a_declared_total_is_input_mismatch():
    result, backend = archived()
    index = fetch_index(backend, result["index_object_id"],
                        result["archive"]["pack"]["index_sha256"],
                        sleep_fn=lambda ms: None)
    target = index["members"][0]

    def no_total(object_id, offset, length):
        body = backend._objects[object_id][offset:offset + length]
        return RangeResult(206, body, range_start=offset,
                           total_length=None)

    backend.get_range = no_total
    reader = PackReader(backend, result["pack_object_id"], index,
                        sleep_fn=lambda ms: None)
    with pytest.raises(FilmError, match="INPUT_MISMATCH"):
        reader.read_member(target["member_id"])


def test_short_payload_and_wrong_total_are_mismatch_failures():
    result, backend = archived()
    index = fetch_index(backend, result["index_object_id"],
                        result["archive"]["pack"]["index_sha256"],
                        sleep_fn=lambda ms: None)
    target = index["members"][0]
    backend.truncate_next[result["pack_object_id"]] = 3
    reader = PackReader(backend, result["pack_object_id"], index,
                        sleep_fn=lambda ms: None)
    with pytest.raises(FilmError, match="shorter"):
        reader.read_member(target["member_id"])
    backend.bad_total_next[result["pack_object_id"]] = 1
    reader = PackReader(backend, result["pack_object_id"], index,
                        sleep_fn=lambda ms: None)
    with pytest.raises(FilmError, match="total length"):
        reader.read_member(target["member_id"])


def test_modified_member_bytes_fail_the_hash_gate():
    result, backend = archived()
    pack = backend._objects[result["pack_object_id"]]
    index = fetch_index(backend, result["index_object_id"],
                        result["archive"]["pack"]["index_sha256"],
                        sleep_fn=lambda ms: None)
    first = index["members"][0]
    pos = first["byte_offset"]
    tampered = pack[:pos + 20] + bytes([pack[pos + 20] ^ 0xFF]) \
        + pack[pos + 21:]
    backend._objects[result["pack_object_id"]] = tampered
    reader = PackReader(backend, result["pack_object_id"], index,
                        sleep_fn=lambda ms: None)
    with pytest.raises(FilmError, match="SHA-256"):
        reader.read_member(first["member_id"])


def test_deleted_pack_object_fails_closed():
    result, backend = archived()
    backend.deleted.add(result["pack_object_id"])
    reader = _reader(result, backend)
    with pytest.raises(FilmError):
        reader.read_member(reader.index["members"][0]["member_id"])


def test_reordered_index_members_are_rejected_before_reads():
    result, backend = archived()
    index = fetch_index(backend, result["index_object_id"],
                        result["archive"]["pack"]["index_sha256"],
                        sleep_fn=lambda ms: None)
    reordered = dict(index, members=list(reversed(index["members"])))
    with pytest.raises(FilmError):
        PackReader(backend, result["pack_object_id"], reordered,
                   sleep_fn=lambda ms: None)
    # A swapped-in index object also fails: the pinned hash mismatches.
    swapped = make_index(
        bytes(range(64)),
        [{"member_id": "x", "frame_index": 0, "source_role": None,
          "byte_offset": 16, "byte_length": 48,
          "sha256": "0" * 64}],
        image_contract=index["image_contract"], frame_coverage=[0, 1],
        snapshot_digest=None, recipe_digest=None, sequence_digest=None,
        retention_refs=[])
    backend.put_object("idx-evil", index_bytes(swapped))
    with pytest.raises(FilmError, match="hash"):
        fetch_index(backend, "idx-evil",
                    result["archive"]["pack"]["index_sha256"],
                    sleep_fn=lambda ms: None)


def test_member_reads_resume_after_interruption_without_refetching():
    result, backend = archived()
    index = fetch_index(backend, result["index_object_id"],
                        result["archive"]["pack"]["index_sha256"],
                        sleep_fn=lambda ms: None)
    reader = PackReader(backend, result["pack_object_id"], index,
                        retry=dict(RETRY), sleep_fn=lambda ms: None)
    first = index["members"][0]["member_id"]
    reader.read_member(first)
    # Drops beyond the same-session retry cap propagate to the caller.
    backend.drops["get_range"] = 4
    with pytest.raises(ConnectionDropped):
        reader.read_member(index["members"][1]["member_id"])
    # The verified member is still held; a resume reads only the rest.
    assert reader.read_member(first) is reader.verified[first]
    reader.read_member(index["members"][1]["member_id"])
    fetched = [r for r in backend.requests if r["op"] == "get_range"
               and r["object_id"] == result["pack_object_id"]]
    # member 1 + 4 dropped attempts + 1 resumed fetch; nothing refetched
    assert len(fetched) == 6


# -- restore through the workspace --------------------------------------------

def _ws(tmp_path, cap=1 << 20):
    return Workspace(tmp_path / "cache", cap)


def test_restore_writes_generated_names_after_full_verification(tmp_path):
    result, backend = archived()
    out = tmp_path / "restored"
    report = restore_archive(backend, result["archive"], _ws(tmp_path), out)
    assert report["verification"] == VERIFIED_MEMBERS
    assert report["restored"] == [f"F_{i:06d}.png" for i in (1, 2, 3, 4)]
    members = {m["member_id"]: m["data"] for m in make_members()}
    for index, member in enumerate(report["restored"]):
        assert (out / member).read_bytes() == members[f"F{index:03d}"]


def test_restore_selection_reads_only_the_selected_member(tmp_path):
    result, backend = archived()
    out = tmp_path / "restored"
    report = restore_archive(backend, result["archive"], _ws(tmp_path),
                             out, members=["F002"])
    assert report["restored"] == ["F_000001.png"]
    fetched = [r for r in backend.requests if r["op"] == "get_range"]
    assert len(fetched) == 1


def test_interrupted_restore_publishes_nothing_and_cache_survives(tmp_path):
    result, backend = archived()
    out = tmp_path / "restored"
    ws = _ws(tmp_path)
    # Drops beyond the same-session retry cap abort mid-restore: the first
    # member has already landed verified in the workspace.
    dropped = {"armed": True}

    original = backend.get_range

    def flaky(object_id, offset, length):
        calls = sum(1 for r in backend.requests if r["op"] == "get_range")
        if dropped["armed"] and calls >= 1:
            raise ConnectionDropped("mid-restore drop")
        return original(object_id, offset, length)

    backend.get_range = flaky
    with pytest.raises(ConnectionDropped):
        restore_archive(backend, result["archive"], ws, out,
                        sleep_fn=lambda ms: None)
    assert not out.exists()                   # nothing published
    assert ws.used_bytes > 0                  # verified members cached
    dropped["armed"] = False
    backend.get_range = original
    before = sum(1 for r in backend.requests if r["op"] == "get_range")
    report = restore_archive(backend, result["archive"], ws, out)
    assert len(report["restored"]) == 4
    # The resume re-fetched only the members that were not yet verified.
    fetched = [r for r in backend.requests if r["op"] == "get_range"]
    assert len(fetched) - before == 3         # members 1..3; member 0 cached


def test_restore_never_overwrites_existing_outputs(tmp_path):
    result, backend = archived()
    out = tmp_path / "restored"
    out.mkdir()
    (out / "F_000001.png").write_bytes(b"previously approved")
    with pytest.raises(FilmError, match="RESTORE_OVERWRITE_BLOCKED"):
        restore_archive(backend, result["archive"], _ws(tmp_path), out)
    assert (out / "F_000001.png").read_bytes() == b"previously approved"


def test_restore_rejects_destination_outside_allowed_root(tmp_path):
    result, backend = archived()
    with pytest.raises(FilmError, match="RESTORE_PATH_REJECTED"):
        restore_archive(backend, result["archive"], _ws(tmp_path),
                        tmp_path / "elsewhere" / "out",
                        allowed_root=tmp_path / "allowed")


def test_restore_rejects_symlink_destination(tmp_path):
    result, backend = archived()
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(FilmError, match="RESTORE_PATH_REJECTED"):
        restore_archive(backend, result["archive"], _ws(tmp_path), link)
    assert not (real / "F_000001.png").exists()


def test_restore_rejects_symlinked_intermediate_directory(tmp_path):
    # The destination sits under the allowed root lexically, but an
    # existing ancestor is a symlink — resolving first would hide it.
    result, backend = archived()
    real = tmp_path / "real"
    real.mkdir()
    linkdir = tmp_path / "linkdir"
    linkdir.symlink_to(real)
    with pytest.raises(FilmError, match="RESTORE_PATH_REJECTED"):
        restore_archive(backend, result["archive"], _ws(tmp_path),
                        linkdir / "out", allowed_root=tmp_path)
    assert not (real / "out").exists()


def test_restore_rejects_dotdot_destination(tmp_path):
    result, backend = archived()
    with pytest.raises(FilmError, match="RESTORE_PATH_REJECTED"):
        restore_archive(backend, result["archive"], _ws(tmp_path),
                        tmp_path / "a" / ".." / "out",
                        allowed_root=tmp_path)


def test_offline_restore_needs_the_full_pack_first(tmp_path):
    result, backend = archived()
    ws = _ws(tmp_path)
    out = tmp_path / "restored"
    report = restore_archive(backend, result["archive"], ws, out,
                             offline=True,
                             whole_pack_cap=1 << 22)
    assert report["verification"] == VERIFIED_FULL_PACK
    assert report["offline_capable"] is True
    out2 = tmp_path / "restored2"
    # A cap too small for the pack is refused before any write.
    with pytest.raises(FilmError, match="whole-pack cap"):
        restore_archive(backend, result["archive"], _ws(tmp_path),
                        out2, offline=True, whole_pack_cap=1)
    assert not out2.exists()


def test_restore_through_an_authorized_session_respects_grants(tmp_path):
    backend = fake_backend()
    session = connect(backend)
    members = make_members()
    result = archive_frames(session.authorized, members,
                            profile="DRIVE_BOUNDED")
    out = tmp_path / "restored"
    report = restore_archive(session.authorized, result["archive"],
                             _ws(tmp_path), out)
    assert len(report["restored"]) == 4
    session.logout()
    with pytest.raises(FilmError):
        restore_archive(session.authorized, result["archive"],
                        _ws(tmp_path), tmp_path / "out2")


def test_archive_status_reports_objects_and_seal(tmp_path):
    result, backend = archived()
    status = archive_status(backend, result["archive"])
    assert status["sealed"] is True
    assert all(o["present"] and o["byte_length_match"]
               for o in status["objects"])
    assert status["qualification"]["qualification_state"] == "UNQUALIFIED"
    assert status["qualification"]["release_state"] == "NOT_AUTHORIZED"
    backend.deleted.add(result["pack_object_id"])
    status = archive_status(backend, result["archive"])
    pack = next(o for o in status["objects"] if o["kind"] == "pack")
    assert pack["present"] is False


def test_archive_creation_fails_when_verification_fails():
    backend = fake_backend(provider_checksum=None)
    members = make_members()
    with pytest.raises(FilmError, match="READBACK_CAP_EXCEEDED"):
        archive_frames(backend, members, profile="DRIVE_BOUNDED",
                       readback_cap=64)
    # And an archive that cannot reach its min level is never sealed.
    backend = fake_backend(provider_checksum="sha256")
    with pytest.raises(FilmError, match="ARCHIVE_UNSEALED"):
        archive_frames(backend, members, profile="DRIVE_BOUNDED",
                       min_level="FULL_READBACK")
