"""Local filesystem archive backend (ANIM-013).

Objects live content-addressed under `root/objects/`. A local directory is
not Drive: ranges are exact file seeks, uploads are atomic file writes, and
`object_info` only reports the stored length — this backend is also the
verifier's read source, so a hash it recomputes locally is
`LOCAL_RECOMPUTED`, never the provider-fixed SHA-256 evidence that would
make UPLOAD_HASH_MATCHED (schema §10.3). Sealing an object on this backend
therefore requires a bounded FULL_READBACK.
"""
from pathlib import Path
import os
import tempfile
import uuid

from ..core import FilmError
from . import (ArchiveRequestError, RangeResult, check_object_id)


class LocalArchiveBackend:
    name = "LOCAL"
    provider_checksum = "LOCAL_RECOMPUTED"

    def __init__(self, root):
        self.root = Path(root)
        self._objects = self.root / "objects"
        self._objects.mkdir(parents=True, exist_ok=True)
        self._sessions = {}

    def _path(self, object_id):
        check_object_id(object_id)
        path = (self._objects / object_id).resolve()
        if not path.is_relative_to(self._objects.resolve()):
            raise FilmError("Object id escapes the archive root")
        return path

    def _stat(self, object_id):
        path = self._path(object_id)
        if not path.is_file():
            raise ArchiveRequestError(404, "not_found",
                                      f"Archive object missing: {object_id}")
        return path, path.stat().st_size

    def _body(self, object_id):
        path, _ = self._stat(object_id)
        return path.read_bytes()

    def list_objects(self):
        return sorted(f.name for f in self._objects.iterdir() if f.is_file())

    def get_object(self, object_id):
        body = self._body(object_id)
        return RangeResult(200, body, total_length=len(body))

    def get_range(self, object_id, offset, length):
        if type(offset) is not int or type(length) is not int \
                or offset < 0 or length < 1:
            raise ArchiveRequestError(416, "range_not_satisfiable",
                                      "Malformed byte range")
        path, size = self._stat(object_id)
        if offset + length > size:
            raise ArchiveRequestError(416, "range_not_satisfiable",
                                      f"Range [{offset}, {offset + length}) "
                                      f"outside object length {size}")
        with path.open("rb") as stream:
            stream.seek(offset)
            body = stream.read(length)
        if len(body) != length:
            raise ArchiveRequestError(416, "range_not_satisfiable",
                                      "Short read inside the stored object")
        return RangeResult(206, body,
                           range_start=offset, total_length=size)

    def put_object(self, object_id, data):
        """Content-addressed write: identical bytes are idempotent, an
        overwrite attempt with different bytes is refused."""
        path = self._path(object_id)
        if path.exists():
            if path.read_bytes() == data:
                return {"object_id": object_id, "reused": True}
            raise FilmError(f"Archive object {object_id} already holds "
                            "different bytes; content changes require a new "
                            "object id")
        fd, temp = tempfile.mkstemp(dir=self._objects, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temp, path)
        finally:
            Path(temp).unlink(missing_ok=True)
        return {"object_id": object_id, "reused": False}

    def object_info(self, object_id):
        # The stored length is metadata; no locally recomputed hash is
        # reported as a provider checksum — that evidence class does not
        # exist on a filesystem backend (schema §10.3).
        _, size = self._stat(object_id)
        return {"byte_length": size,
                "provider_checksum": "LOCAL_RECOMPUTED"}

    # Resumable upload: an upload session confirms its received offset, which
    # is transfer progress — not an archive integrity check.
    def create_upload_session(self, object_id, expected_length):
        check_object_id(object_id)
        session = f"up-{uuid.uuid4().hex[:12]}"
        self._sessions[session] = {"object_id": object_id,
                                   "expected_length": expected_length,
                                   "buffer": bytearray()}
        return session

    def upload_chunk(self, session, offset, data):
        state = self._sessions.get(session)
        if state is None:
            raise ArchiveRequestError(404, "not_found",
                                      "Unknown upload session")
        if offset != len(state["buffer"]):
            raise ArchiveRequestError(409, "offset_conflict",
                                      f"Chunk offset {offset} does not match "
                                      f"confirmed offset {len(state['buffer'])}")
        if offset + len(data) > state["expected_length"]:
            raise ArchiveRequestError(413, "too_large",
                                      "Chunk exceeds the declared object length")
        state["buffer"].extend(data)
        return {"received": len(state["buffer"])}

    def upload_status(self, session):
        state = self._sessions.get(session)
        if state is None:
            raise ArchiveRequestError(404, "not_found",
                                      "Unknown upload session")
        return {"offset": len(state["buffer"]),
                "expected_length": state["expected_length"]}

    def complete_upload(self, session):
        state = self._sessions.pop(session, None)
        if state is None:
            raise ArchiveRequestError(404, "not_found",
                                      "Unknown upload session")
        if len(state["buffer"]) != state["expected_length"]:
            raise FilmError("Upload incomplete at session close")
        return self.put_object(state["object_id"], bytes(state["buffer"]))
