"""Fake Drive-like archive backend for protocol tests (ANIM-013).

This is **not** a Google implementation. It exercises the archive protocol —
exact range reads, whole-body answers to range requests, resumable upload
offsets, provider checksums and every failure the contract names — with no
network, no OAuth client and no credentials.

Faults are plain attributes tests flip:

- `provider_checksum`: `"sha256"` when the fake endpoint computes a real
  object SHA-256 (UPLOAD_HASH_MATCHED evidence), else `None` — like a
  Drive-class API whose checksum is not a trustworthy SHA-256 — forcing
  the bounded FULL_READBACK path.
- `range_supported=False`: every range request is answered with a 200
  whole body (or 416 for out-of-bounds), never partial bytes.
- `denied`: object ids answered with permission 403; `deleted`: 404.
- `rate_limit`: next N requests answered 429; `server_errors`: next N 503.
- `drops`: dict {operation: remaining drops} raising ConnectionDropped.
- `truncate_next`: dict {object_id: body length} — the next range read of
  that object returns a 206 whose body is shorter than requested.
- `bad_total_next`: dict {object_id: total} — 206 with a wrong declared
  total length.
- `upload_partial`: dict {session counter threshold: bytes accepted} —
  a chunk write accepts only some bytes before dropping, so the server
  confirmed offset lags behind what the client sent.

Every request and its transferred bytes are logged in `requests` so tests
can assert that sparse reads fetched only the needed ranges and that retry
duplicate bytes were actually counted.
"""
from pathlib import Path
import hashlib
import os
import uuid

from ..core import FilmError
from . import (ArchiveRequestError, ConnectionDropped, RangeResult,
               check_object_id)


class FakeDriveBackend:
    name = "FAKE_DRIVE"

    def __init__(self, objects=None, root=None, provider_checksum=None):
        # `root` mirrors objects into `root/objects/` so a CLI run can share
        # one fake archive across processes; in-memory only otherwise.
        self._objects = dict(objects or {})
        self._root = Path(root) if root else None
        if self._root is not None:
            (self._root / "objects").mkdir(parents=True, exist_ok=True)
            for object_id, data in self._objects.items():
                self._store(object_id, data)
        self.provider_checksum = provider_checksum
        self.range_supported = True
        self.denied = set()
        self.deleted = set()
        self.rate_limit = 0
        self.server_errors = 0
        self.retry_after_ms = None
        self.drops = {}
        self.truncate_next = {}
        self.bad_total_next = {}
        self.upload_partial = {}
        self.requests = []
        self._sessions = {}

    # -- plumbing ----------------------------------------------------------
    def _store(self, object_id, data):
        if self._root is not None:
            path = self._root / "objects" / object_id
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(path, flags, 0o600)
            except OSError as e:
                raise FilmError(f"Refusing to write archive object "
                                f"{object_id} through a symlink") from e
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        self._objects[object_id] = data

    def _log(self, op, object_id, **fields):
        self.requests.append({"op": op, "object_id": object_id, **fields})

    def _load(self, object_id):
        if object_id in self._objects:
            return self._objects[object_id]
        if self._root is not None:
            path = self._root / "objects" / object_id
            if path.is_file():
                data = path.read_bytes()
                self._objects[object_id] = data
                return data
        return None

    def _gate(self, op, object_id):
        """Common failure path; request accounting happens here."""
        if self.rate_limit > 0:
            self.rate_limit -= 1
            raise ArchiveRequestError(
                429, "rate_limit", "Fake drive rate limit",
                retry_after_ms=self.retry_after_ms)
        if self.server_errors > 0:
            self.server_errors -= 1
            raise ArchiveRequestError(503, "unavailable",
                                      "Fake drive transient error")
        if self.drops.get(op, 0) > 0:
            self.drops[op] -= 1
            raise ConnectionDropped(f"Fake drive dropped the {op} transfer")
        if object_id is not None:
            if object_id in self.deleted \
                    or (op not in ("put_object", "create_upload_session")
                        and self._load(object_id) is None):
                raise ArchiveRequestError(404, "not_found",
                                          f"Fake drive has no object "
                                          f"{object_id}")
            if object_id in self.denied:
                raise ArchiveRequestError(403, "permission_denied",
                                          f"Fake drive denies {object_id}")

    def list_objects(self):
        if self._root is not None:
            for f in (self._root / "objects").iterdir():
                if f.is_file() and f.name not in self._objects:
                    self._objects[f.name] = f.read_bytes()
        return sorted(self._objects)

    # -- reads -------------------------------------------------------------
    def get_object(self, object_id):
        check_object_id(object_id)
        self._log("get_object", object_id)
        self._gate("get_object", object_id)
        body = self._load(object_id)
        self.requests[-1]["bytes"] = len(body)
        return RangeResult(200, body, total_length=len(body))

    def get_range(self, object_id, offset, length):
        check_object_id(object_id)
        self._log("get_range", object_id, offset=offset, length=length)
        self._gate("get_range", object_id)
        body = self._load(object_id)
        if offset + length > len(body) or offset < 0 or length < 1:
            raise ArchiveRequestError(416, "range_not_satisfiable",
                                      f"Range [{offset}, {offset + length}) "
                                      f"outside object length {len(body)}")
        if not self.range_supported:
            # A backend that ignores Range answers the whole body.
            self.requests[-1]["bytes"] = len(body)
            self.requests[-1]["status"] = 200
            return RangeResult(200, body, total_length=len(body))
        if object_id in self.truncate_next:
            size = self.truncate_next.pop(object_id)
            short = body[offset:offset + min(size, length)]
            self.requests[-1]["bytes"] = len(short)
            return RangeResult(206, short, range_start=offset,
                               total_length=len(body))
        total = self.bad_total_next.pop(object_id, len(body))
        out = body[offset:offset + length]
        self.requests[-1]["bytes"] = len(out)
        self.requests[-1]["status"] = 206
        return RangeResult(206, out, range_start=offset, total_length=total)

    # -- writes ------------------------------------------------------------
    def put_object(self, object_id, data):
        check_object_id(object_id)
        self._log("put_object", object_id)
        self._gate("put_object", object_id)
        existing = self._load(object_id)
        if existing is not None:
            if existing == data:
                return {"object_id": object_id, "reused": True}
            raise FilmError(f"Archive object {object_id} already holds "
                            "different bytes")
        self._store(object_id, data)
        self.requests[-1]["bytes"] = len(data)
        return {"object_id": object_id, "reused": False}

    def object_info(self, object_id):
        check_object_id(object_id)
        self._log("object_info", object_id)
        self._gate("object_info", object_id)
        body = self._load(object_id)
        info = {"byte_length": len(body),
                "provider_checksum": self.provider_checksum}
        if self.provider_checksum == "sha256":
            info["sha256"] = hashlib.sha256(body).hexdigest()
        else:
            # Drive-class metadata: an MD5 is not a SHA-256 integrity proof.
            info["md5"] = hashlib.md5(body).hexdigest()
        return info

    # -- resumable upload ---------------------------------------------------
    def create_upload_session(self, object_id, expected_length):
        check_object_id(object_id)
        self._log("create_upload_session", object_id)
        self._gate("create_upload_session", object_id)
        session = f"up-{uuid.uuid4().hex[:12]}"
        self._sessions[session] = {"object_id": object_id,
                                   "expected_length": expected_length,
                                   "buffer": bytearray()}
        return session

    def upload_chunk(self, session, offset, data):
        self._log("upload_chunk", None, session=session, offset=offset,
                  length=len(data))
        self._gate("upload_chunk", None)
        state = self._sessions.get(session)
        if state is None:
            raise ArchiveRequestError(404, "not_found",
                                      "Unknown upload session")
        if offset != len(state["buffer"]):
            raise ArchiveRequestError(409, "offset_conflict",
                                      f"Chunk offset {offset} does not match "
                                      f"confirmed offset {len(state['buffer'])}")
        partial = self.upload_partial.get(len(self.requests) - 1)
        if partial is not None and partial < len(data):
            state["buffer"].extend(data[:partial])
            raise ConnectionDropped("Fake drive dropped mid-chunk")
        if offset + len(data) > state["expected_length"]:
            raise ArchiveRequestError(413, "too_large",
                                      "Chunk exceeds the declared length")
        state["buffer"].extend(data)
        self.requests[-1]["bytes"] = len(data)
        return {"received": len(state["buffer"])}

    def upload_status(self, session):
        self._log("upload_status", None, session=session)
        self._gate("upload_status", None)
        state = self._sessions.get(session)
        if state is None:
            raise ArchiveRequestError(404, "not_found",
                                      "Unknown upload session")
        return {"offset": len(state["buffer"]),
                "expected_length": state["expected_length"]}

    def complete_upload(self, session):
        self._log("complete_upload", None, session=session)
        self._gate("complete_upload", None)
        state = self._sessions.pop(session, None)
        if state is None:
            raise ArchiveRequestError(404, "not_found",
                                      "Unknown upload session")
        if len(state["buffer"]) != state["expected_length"]:
            raise FilmError("Upload incomplete at session close")
        return self.put_object(state["object_id"], bytes(state["buffer"]))

    def expire_session(self, session):
        """Test hook: the provider forgot a resumable session (expiry)."""
        return self._sessions.pop(session, None) is not None

    # -- approved orphan cleanup -------------------------------------------------
    def delete_object(self, object_id):
        """Delete one stored object; only the committer's explicit
        approval path calls this — nothing else is ever removed."""
        check_object_id(object_id)
        self._log("delete_object", object_id)
        self._gate("delete_object", object_id)
        self._objects.pop(object_id, None)
        if self._root is not None:
            (self._root / "objects" / object_id).unlink(missing_ok=True)
        self.requests[-1]["bytes"] = 0
        return {"object_id": object_id, "deleted": True}
