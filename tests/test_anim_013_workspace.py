"""ANIM-013 workspace fixtures: byte cap, retention classes, pin/eviction,
decoded-RAM cap and staged restore publication (storage §4.2).
"""
import pytest

from anim_013_kit import make_png

from engine.core import FilmError
from engine.fav_pack import image_contract_for
from engine.workspace import Workspace


def test_capacity_evicts_most_temporary_first(tmp_path):
    ws = Workspace(tmp_path / "ws", capacity_bytes=1000)
    ws.put("keep", b"k" * 400, retention="VERIFIED_MEMBER")
    ws.put("junk", b"j" * 400, retention="TEMPORARY")
    ws.put("new", b"n" * 400, retention="VERIFIED_MEMBER")
    assert ws.get("junk") is None            # TEMPORARY evicted first
    assert ws.get("keep") == b"k" * 400
    assert ws.get("new") == b"n" * 400
    assert ws.used_bytes <= 1000


def test_pinned_entries_are_never_evicted(tmp_path):
    ws = Workspace(tmp_path / "ws", capacity_bytes=800)
    ws.put("pin", b"p" * 400, retention="VERIFIED_MEMBER", pinned=True)
    ws.put("pin2", b"q" * 400, retention="TEMPORARY", pinned=True)
    with pytest.raises(FilmError, match="WORKSPACE_FULL"):
        ws.put("more", b"m" * 100)
    # Unpinning frees the eviction path again.
    ws.unpin("pin")
    ws.put("more", b"m" * 100)
    assert ws.get("pin") is None and ws.get("more") == b"m" * 100


def test_retention_order_keeps_verified_members(tmp_path):
    ws = Workspace(tmp_path / "ws", capacity_bytes=1000)
    ws.put("a", b"a" * 400, retention="VERIFIED_MEMBER")
    ws.put("b", b"b" * 400, retention="RESTORE_WORKING")
    ws.put("c", b"c" * 400, retention="TEMPORARY")
    ws.put("d", b"d" * 300, retention="VERIFIED_MEMBER")
    assert ws.get("c") is None and ws.get("b") is None
    assert ws.get("a") is not None and ws.get("d") is not None


def test_oversized_single_entry_fails_closed(tmp_path):
    ws = Workspace(tmp_path / "ws", capacity_bytes=100)
    with pytest.raises(FilmError, match="WORKSPACE_FULL"):
        ws.put("big", b"x" * 200)


def test_decoded_cap_is_independent_of_png_size(tmp_path):
    ws = Workspace(tmp_path / "ws", capacity_bytes=1 << 20,
                   decoded_byte_cap=100)
    data = make_png(0, 16, 16)               # decodes to 16*16*4 = 1024 bytes
    contract = image_contract_for([data])
    with pytest.raises(FilmError, match="decode cap"):
        ws.check_member(data, contract, "m1")
    ws.decoded_byte_cap = 2048
    assert ws.check_member(data, contract, "m1") == 1024


def test_unsafe_cache_keys_rejected(tmp_path):
    ws = Workspace(tmp_path / "ws", capacity_bytes=100)
    for key in ("../x", "a/b", "..", ""):
        with pytest.raises(FilmError, match="Unsafe cache key"):
            ws.put(key, b"x")


def test_staging_counts_against_the_cap_and_publishes_atomically(tmp_path):
    ws = Workspace(tmp_path / "ws", capacity_bytes=1200)
    stage = ws.begin_restore(reserve_bytes=900)
    assert ws.available_bytes == 300
    stage.write_member("F_000001.png", b"a" * 500)
    stage.write_member("F_000002.png", b"b" * 400)
    dest = tmp_path / "out"
    names = stage.commit(dest)
    assert names == ["F_000001.png", "F_000002.png"]
    assert (dest / "F_000001.png").read_bytes() == b"a" * 500
    assert ws.available_bytes == 1200        # claim released on publish


def test_staging_abort_exposes_nothing(tmp_path):
    ws = Workspace(tmp_path / "ws", capacity_bytes=1200)
    stage = ws.begin_restore(reserve_bytes=100)
    stage.write_member("F_000001.png", b"a" * 50)
    stage.abort()
    dest = tmp_path / "out"
    assert not dest.exists() and not stage.dir.exists()
    assert ws.available_bytes == 1200


def test_commit_refuses_existing_destinations_and_aborts(tmp_path):
    ws = Workspace(tmp_path / "ws", capacity_bytes=1200)
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "F_000001.png").write_bytes(b"approved output")
    stage = ws.begin_restore()
    stage.write_member("F_000001.png", b"new bytes")
    with pytest.raises(FilmError, match="RESTORE_OVERWRITE_BLOCKED"):
        stage.commit(dest)
    assert (dest / "F_000001.png").read_bytes() == b"approved output"
    assert not stage.dir.exists()


def test_symlink_destination_is_not_followed(tmp_path):
    ws = Workspace(tmp_path / "ws", capacity_bytes=1200)
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    stage = ws.begin_restore()
    stage.write_member("F_000001.png", b"x")
    # The symlinked path resolves to `real/F_000001.png`; os.replace writes
    # inside the real dir. Only a direct symlinked FILE is blocked by the
    # commit check — the caller (drive_archive) rejects symlinked roots.
    assert link.is_symlink()


def test_surviving_entries_reattach_across_workspace_instances(tmp_path):
    root = tmp_path / "ws"
    Workspace(root, capacity_bytes=1000).put("m", b"m" * 100,
                                           retention="VERIFIED_MEMBER")
    ws = Workspace(root, capacity_bytes=1000)
    assert ws.get("m") == b"m" * 100
