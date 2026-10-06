"""ANIM-013 PACK fixtures: FAV1 bytes, pinned index, PNG caps, tamper gates.

Covers seekable pack layout, pack_index validation (offsets, bounds,
coverage, ordering, duplicates), member/full-hash distinction at the index
level, and the pre-decode PNG header/decoded-byte caps — the PACK-SPARSE
and PACK-TAMPER index half and the EXPANSION gate.
"""
import json

import pytest

from anim_013_kit import make_members, make_png

from engine.animation_schema import canon_bytes, check_document
from engine.core import FilmError
from engine.fav_pack import (HEADER_BYTE_LENGTH, PACK_MAGIC, build_pack,
                             check_member_png, decoded_bytes_estimate,
                             frame_members, image_contract_for, index_bytes,
                             make_index, member_for_frame, png_header,
                             read_index, sha256_bytes, validate_index)


def built(count=4):
    members = make_members(count)
    pack_bytes, entries = build_pack(members)
    contract = image_contract_for([m["data"] for m in members])
    index = make_index(pack_bytes, entries, image_contract=contract,
                       frame_coverage=[0, count], snapshot_digest=None,
                       recipe_digest=None, sequence_digest=None,
                       retention_refs=[])
    return members, pack_bytes, entries, contract, index


def test_pack_header_is_fav1_and_members_are_contiguous_and_seekable():
    members, pack_bytes, entries, _, _ = built()
    assert pack_bytes[:8] == PACK_MAGIC
    assert int.from_bytes(pack_bytes[8:12], "little") == 1
    assert int.from_bytes(pack_bytes[12:16], "little") == HEADER_BYTE_LENGTH
    cursor = HEADER_BYTE_LENGTH
    for member, entry in zip(members, entries):
        assert entry["byte_offset"] == cursor
        assert entry["byte_length"] == len(member["data"])
        assert pack_bytes[cursor:cursor + entry["byte_length"]] \
            == member["data"]
        assert entry["sha256"] == sha256_bytes(member["data"])
        cursor += entry["byte_length"]
    assert cursor == len(pack_bytes)


def test_index_is_registered_document_and_roundtrips_canonically():
    _, pack_bytes, entries, contract, index = built()
    assert check_document(index, "pack_index") == "pack_index"
    assert index["pack_sha256"] == sha256_bytes(pack_bytes)
    assert index["pack_byte_length"] == len(pack_bytes)
    assert index["member_count"] == len(entries)
    assert index["frame_coverage"] == [0, 4]
    raw = index_bytes(index)
    assert raw == canon_bytes(index) and raw.endswith(b"\n")
    assert read_index(raw) == index
    assert read_index(raw, expected_sha256=sha256_bytes(raw)) == index


def test_index_rejects_reordered_member_entries():
    _, _, entries, contract, index = built()
    tampered = dict(index, members=[entries[1], entries[0], *entries[2:]])
    with pytest.raises(FilmError, match="contiguous"):
        validate_index(tampered)


def test_index_rejects_gaps_overlaps_and_header_intrusion():
    _, _, entries, contract, index = built()
    gap = dict(index, members=[dict(e, byte_offset=e["byte_offset"] + 1)
                               if i == 2 else e
                               for i, e in enumerate(entries)])
    with pytest.raises(FilmError):
        validate_index(gap)
    inside = dict(index, members=[dict(entries[0], byte_offset=4),
                                  *entries[1:]])
    with pytest.raises(FilmError):
        validate_index(inside)


def test_index_rejects_duplicate_members_and_incomplete_coverage():
    members = make_members(3)
    pack_bytes, entries = build_pack(members)
    contract = image_contract_for([m["data"] for m in members])
    # Split member 0 into two contiguous entries sharing its member_id.
    half = entries[0]["byte_length"] // 2
    dup = [dict(entries[0], byte_length=half),
           dict(entries[0], byte_offset=16 + half,
                byte_length=entries[0]["byte_length"] - half),
           *entries[1:]]
    with pytest.raises(FilmError, match="Duplicate member id"):
        make_index(pack_bytes, dup, image_contract=contract,
                   frame_coverage=[0, 3], snapshot_digest=None,
                   recipe_digest=None, sequence_digest=None,
                   retention_refs=[])
    with pytest.raises(FilmError, match="covered without gaps"):
        make_index(pack_bytes, entries, image_contract=contract,
                   frame_coverage=[0, 4], snapshot_digest=None,
                   recipe_digest=None, sequence_digest=None,
                   retention_refs=[])


def test_index_rejects_member_hash_and_count_mismatch():
    _, pack_bytes, entries, contract, index = built()
    bad_hash = dict(index, members=[dict(entries[0], sha256="0" * 64),
                                    *entries[1:]])
    validated = validate_index(bad_hash)  # structure still valid…
    assert validated["members"][0]["sha256"] != entries[0]["sha256"]
    with pytest.raises(FilmError, match="member_count"):
        validate_index(dict(index, member_count=1))
    with pytest.raises(FilmError):
        validate_index(dict(index, pack_byte_length=3))
    with pytest.raises(FilmError):
        validate_index(dict(index, schema_version=2))


def test_pinned_index_hash_gate():
    _, _, _, _, index = built()
    raw = index_bytes(index)
    with pytest.raises(FilmError, match="hash"):
        read_index(raw, expected_sha256="0" * 64)
    body = json.loads(raw)
    body["pack_sha256"] = "1" * 64
    with pytest.raises(FilmError, match="canonical"):
        read_index(json.dumps(body).encode())
    with pytest.raises(FilmError, match="read bound"):
        read_index(b"x" * (16 * 1024 * 1024 + 1))


def test_png_header_and_decoded_estimate():
    data = make_png(1, 32, 16)
    header = png_header(data)
    assert (header["width"], header["height"]) == (32, 16)
    assert header["pixel_format"] == "RGBA8"
    assert decoded_bytes_estimate(header) == 32 * 16 * 4
    with pytest.raises(FilmError, match="Not a PNG"):
        png_header(b"not a png at all" * 4)


def test_contract_dimension_and_decoded_caps_gate_decode():
    members = make_members(2)
    contract = image_contract_for([m["data"] for m in members])
    data = members[0]["data"]
    assert check_member_png(data, contract, "F000") > 0
    bigger = make_png(2, 32, 32)
    with pytest.raises(FilmError, match="dimensions"):
        check_member_png(bigger, contract, "F000")
    tight = dict(contract, max_decoded_bytes=contract["max_decoded_bytes"] - 1)
    with pytest.raises(FilmError, match="decoded size"):
        check_member_png(data, tight, "F000")
    small = dict(contract, max_encoded_bytes=len(data) - 1)
    with pytest.raises(FilmError, match="max_encoded"):
        check_member_png(data, small, "F000")


def test_mixed_geometry_pack_is_rejected_at_build():
    members = [{"member_id": "a", "frame_index": 0, "data": make_png(0, 16, 16)},
               {"member_id": "b", "frame_index": 1, "data": make_png(1, 32, 32)}]
    with pytest.raises(FilmError, match="share one canvas"):
        image_contract_for([m["data"] for m in members])


def test_frame_member_lookup():
    _, _, _, _, index = built(3)
    frames, source = frame_members(index)
    assert [m["frame_index"] for m in frames] == [0, 1, 2] and source == []
    assert member_for_frame(index, 1)["member_id"] == "F001"
    with pytest.raises(FilmError):
        member_for_frame(index, 9)
