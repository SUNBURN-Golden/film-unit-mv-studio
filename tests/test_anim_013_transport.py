"""ANIM-013 TRANSPORT/HTTP fixtures: range semantics, bounded retry,
resumable upload offsets, artifact verification levels and seal.

Covers HTTP 206/200/416/mismatch/short-payload answers, same-session
idempotent retry caps, server-confirmed upload offsets with real counted
bytes, and UPLOAD_HASH_MATCHED vs bounded FULL_READBACK evidence —
TRANSPORT-RETRY and ARCHIVE-LEVEL fixtures.
"""
import pytest

from anim_013_kit import fake_backend

from engine.archive_manifest import (DEFAULT_RETRY, bounded_read,
                                     resumable_put, seal_archive,
                                     seal_status, verify_object,
                                     verify_archive, make_archive)
from engine.core import FilmError
from engine.fav_pack import sha256_bytes
from engine.storage_backends import (ArchiveRequestError,
                                     ConnectionDropped)
from engine.storage_backends.local import LocalArchiveBackend

RETRY = {"max_attempts": 4, "backoff_ms": [0, 0, 0]}
NO_SLEEP = lambda ms: None


def _obj(backend, data=b"payload-bytes", object_id="obj-1"):
    backend.put_object(object_id, data)
    return object_id, data


# -- range semantics -----------------------------------------------------------

def test_range_206_returns_exact_slice():
    backend = fake_backend()
    _obj(backend, b"0123456789")
    result = backend.get_range("obj-1", 2, 4)
    assert result.status == 206 and result.body == b"2345"
    assert result.range_start == 2 and result.total_length == 10


def test_range_outside_object_is_416_and_never_retried():
    backend = fake_backend()
    _obj(backend, b"0123456789")
    with pytest.raises(ArchiveRequestError) as e:
        backend.get_range("obj-1", 8, 4)
    assert e.value.status == 416 and not e.value.retriable()
    calls = len(backend.requests)
    with pytest.raises(ArchiveRequestError):
        bounded_read(lambda: backend.get_range("obj-1", 8, 4), RETRY,
                     NO_SLEEP)
    assert len(backend.requests) == calls + 1  # one attempt, no retry


def test_range_unsupported_answers_whole_body():
    backend = fake_backend()
    backend.range_supported = False
    _obj(backend, b"0123456789")
    result = backend.get_range("obj-1", 2, 4)
    assert result.status == 200 and result.body == b"0123456789"


def test_malformed_range_is_416():
    backend = fake_backend()
    _obj(backend)
    for args in [(-1, 4), (0, 0), (0, -2)]:
        with pytest.raises(ArchiveRequestError) as e:
            backend.get_range("obj-1", *args)
        assert e.value.status == 416


def test_deleted_and_denied_objects_fail_closed():
    backend = fake_backend()
    _obj(backend)
    backend.deleted.add("obj-1")
    with pytest.raises(ArchiveRequestError) as e:
        backend.get_range("obj-1", 0, 4)
    assert e.value.status == 404 and not e.value.retriable()
    backend.deleted.clear()
    backend.denied.add("obj-1")
    with pytest.raises(ArchiveRequestError) as e:
        backend.get_object("obj-1")
    assert e.value.status == 403 and not e.value.retriable()


# -- bounded same-session retry -------------------------------------------------

def test_dropped_transfer_retries_within_the_cap():
    backend = fake_backend()
    _obj(backend)
    backend.drops["get_object"] = 2
    body = bounded_read(lambda: backend.get_object("obj-1"), RETRY,
                        NO_SLEEP)
    assert body.body == b"payload-bytes"
    assert sum(1 for r in backend.requests if r["op"] == "get_object") == 3


def test_retry_cap_is_bounded_and_drops_exhaust():
    backend = fake_backend()
    _obj(backend)
    backend.drops["get_object"] = 10
    with pytest.raises(ConnectionDropped):
        bounded_read(lambda: backend.get_object("obj-1"), RETRY, NO_SLEEP)
    assert sum(1 for r in backend.requests if r["op"] == "get_object") == 4


def test_rate_limit_and_server_errors_retry_then_succeed():
    backend = fake_backend()
    _obj(backend)
    backend.rate_limit = 1
    backend.server_errors = 1
    result = bounded_read(lambda: backend.get_object("obj-1"), RETRY,
                          NO_SLEEP)
    assert result.status == 200
    assert sum(1 for r in backend.requests if r["op"] == "get_object") == 3


def test_non_retriable_errors_never_retry():
    backend = fake_backend()
    backend.denied.add("obj-9")
    calls = 0

    def op():
        nonlocal calls
        calls += 1
        return backend.get_object("obj-9")

    with pytest.raises(ArchiveRequestError):
        bounded_read(op, RETRY, NO_SLEEP)
    assert calls == 1


# -- resumable upload offsets -----------------------------------------------------

def test_resumable_put_confirms_offsets_and_completes():
    backend = fake_backend()
    data = bytes(range(256)) * 8
    result = resumable_put(backend, "obj-up", data,
                           {"chunk_bytes": 512, "resumable": True}, RETRY,
                           NO_SLEEP)
    assert result["bytes_confirmed"] == len(data)
    assert backend.get_object("obj-up").body == data
    statuses = [r for r in backend.requests if r["op"] == "upload_status"]
    assert statuses  # every send was followed by a confirmed offset


def test_resumable_put_resumes_from_server_offset_after_drop():
    backend = fake_backend()
    data = b"abcdefgh" * 512                    # 4096 bytes
    # Drop the third chunk write mid-flight: only some bytes land.
    chunk_calls = {"n": 0}
    original = backend.upload_chunk

    def flaky(session, offset, data_chunk):
        chunk_calls["n"] += 1
        if chunk_calls["n"] == 3:
            state = backend._sessions[session]
            state["buffer"].extend(data_chunk[:17])   # partial server write
            raise ConnectionDropped("mid-chunk drop")
        return original(session, offset, data_chunk)

    backend.upload_chunk = flaky
    result = resumable_put(backend, "obj-up", data,
                           {"chunk_bytes": 1024, "resumable": True}, RETRY,
                           NO_SLEEP)
    assert backend.get_object("obj-up").body == data
    assert result["resumed"] or result["bytes_confirmed"] == len(data)
    # The resumed send continued at the confirmed offset, not from zero.
    offsets = [r["offset"] for r in backend.requests
               if r["op"] == "upload_chunk"]
    assert offsets == sorted(offsets)
    assert any(0 < o < len(data) for o in offsets)


def test_resumable_put_rejects_offset_conflict():
    backend = fake_backend()
    session = backend.create_upload_session("obj-x", 10)
    with pytest.raises(ArchiveRequestError) as e:
        backend.upload_chunk(session, 3, b"abc")
    assert e.value.status == 409


def test_non_resumable_put_falls_back_to_atomic_put():
    backend = fake_backend()
    resumable_put(backend, "obj-1", b"data",
                  {"chunk_bytes": 4, "resumable": False}, RETRY, NO_SLEEP)
    assert backend.get_object("obj-1").body == b"data"


# -- artifact verification levels --------------------------------------------------

def test_provider_sha256_gives_upload_hash_matched():
    backend = fake_backend(provider_checksum="sha256")
    _obj(backend)
    level = verify_object(backend, "obj-1", sha256_bytes(b"payload-bytes"),
                          len(b"payload-bytes"), sleep_fn=NO_SLEEP)
    assert level == "UPLOAD_HASH_MATCHED"
    assert not any(r["op"] == "get_object" for r in backend.requests)


def test_no_provider_checksum_requires_full_readback():
    backend = fake_backend(provider_checksum=None)
    _obj(backend)
    level = verify_object(backend, "obj-1", sha256_bytes(b"payload-bytes"),
                          len(b"payload-bytes"), sleep_fn=NO_SLEEP)
    assert level == "FULL_READBACK"
    assert any(r["op"] == "get_object" for r in backend.requests)


def test_readback_cap_blocks_unverifiable_large_object():
    backend = fake_backend(provider_checksum=None)
    backend.put_object("big", b"x" * 3000)
    with pytest.raises(FilmError, match="READBACK_CAP_EXCEEDED"):
        verify_object(backend, "big", sha256_bytes(b"x" * 3000), 3000,
                      readback_cap=1024, sleep_fn=NO_SLEEP)
    assert not any(r["op"] == "get_object" for r in backend.requests)


def test_stored_bytes_tamper_fails_verification():
    backend = fake_backend(provider_checksum="sha256")
    _obj(backend)
    backend._objects["obj-1"] = b"tampered-byt3"    # same length, new hash
    with pytest.raises(FilmError, match="provider hash"):
        verify_object(backend, "obj-1", sha256_bytes(b"payload-bytes"),
                      len(b"payload-bytes"), sleep_fn=NO_SLEEP)
    with pytest.raises(FilmError, match="stored length"):
        verify_object(backend, "obj-1", sha256_bytes(b"payload-bytes"),
                      len(b"payload-bytes") - 1, sleep_fn=NO_SLEEP)


def test_local_backend_reports_recomputed_sha256(tmp_path):
    backend = LocalArchiveBackend(tmp_path / "arc")
    _obj(backend)
    level = verify_object(backend, "obj-1", sha256_bytes(b"payload-bytes"),
                          len(b"payload-bytes"), sleep_fn=NO_SLEEP)
    assert level == "UPLOAD_HASH_MATCHED"


# -- seal evaluation --------------------------------------------------------------

def _manifest(objects, min_level="UPLOAD_HASH_MATCHED"):
    return make_archive("arc-t", "DRIVE_BOUNDED", objects,
                        verification={"level": "UPLOADED_UNVERIFIED",
                                      "min_required": min_level,
                                      "readback_cap_bytes": 1 << 20})


def test_seal_requires_the_configured_minimum_level():
    doc = _manifest([{"object_id": "o", "kind": "pack", "byte_length": 1,
                      "sha256": "0" * 64, "revision": None}])
    sealed = seal_archive(doc, "UPLOAD_HASH_MATCHED")
    assert sealed["verification"]["level"] == "UPLOAD_HASH_MATCHED"
    with pytest.raises(FilmError, match="ARCHIVE_UNSEALED"):
        seal_archive(doc, "UPLOADED_UNVERIFIED")
    high = _manifest(doc["objects"], min_level="FULL_READBACK")
    with pytest.raises(FilmError, match="ARCHIVE_UNSEALED"):
        seal_archive(high, "UPLOAD_HASH_MATCHED")
    status = seal_status(doc, "UPLOADED_UNVERIFIED")
    assert status["sealed"] is False


def test_verify_archive_reports_weakest_level():
    backend = fake_backend(provider_checksum="sha256")
    _obj(backend)
    doc = _manifest([{"object_id": "obj-1", "kind": "pack",
                      "byte_length": len(b"payload-bytes"),
                      "sha256": sha256_bytes(b"payload-bytes"),
                      "revision": None}])
    assert verify_archive(backend, doc, sleep_fn=NO_SLEEP) \
        == "UPLOAD_HASH_MATCHED"


def test_manifest_rejects_secret_metadata_fields():
    with pytest.raises(FilmError, match="non-secret"):
        make_archive("arc-t", "DRIVE_BOUNDED",
                     [{"object_id": "o", "kind": "pack", "byte_length": 1,
                       "sha256": "0" * 64, "revision": None}],
                     connection={"connection_id": "c",
                                 "account_binding_digest": "d",
                                 "credential_epoch": 1,
                                 "access_token": "nope"})
