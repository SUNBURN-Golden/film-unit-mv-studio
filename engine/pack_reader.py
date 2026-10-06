"""Sparse, verified FAV1 pack reads (ANIM-013, evolution doc §3-4/§6).

The reader fetches only the member ranges it needs. Every range request
expects an exact 206: the server's `range_start` and `total_length` must
match the pinned index, and the body length must equal the index length —
a short body, wrong offset or wrong declared total is a content mismatch,
never retried. A 200 whole-body answer to a range request is only usable
when a whole-pack fallback cap was declared *before* the request and the
pack fits inside it; then the full-pack SHA-256 is verified and the state
becomes VERIFIED_FULL_PACK. Without a declared cap the response is refused
— no unbounded spool of an unrequested whole object.

Members are hash-checked before any decode; an interrupted transfer leaves
already-verified members in `verified` so a resume does not re-fetch them.
`VERIFIED_MEMBERS` (each fetched member hash-matched) and
`VERIFIED_FULL_PACK` (the whole object hash-matched) stay distinct.
"""
from .core import FilmError
from .fav_pack import sha256_bytes, validate_index
from .archive_manifest import bounded_read

UNVERIFIED = "UNVERIFIED"
VERIFIED_MEMBERS = "VERIFIED_MEMBERS"
VERIFIED_FULL_PACK = "VERIFIED_FULL_PACK"


class PackReader:
    """Member-level verified reader over an archive backend."""

    def __init__(self, backend, pack_object_id, index_document, *,
                 whole_pack_cap=None, retry=None, sleep_fn=None):
        self.backend = backend
        self.pack_object_id = pack_object_id
        self.index = validate_index(index_document)
        self.whole_pack_cap = whole_pack_cap
        self.retry = retry          # None = declared policy omitted: 0 retries
        self.sleep_fn = sleep_fn
        self.state = UNVERIFIED
        self.verified = {}        # member_id -> bytes, in index order
        self._whole = None        # whole pack bytes once VERIFIED_FULL_PACK
        self.fetched_bytes = 0    # real transferred bytes, for tests/status

    # -- helpers -------------------------------------------------------------
    def _members(self):
        return {m["member_id"]: m for m in self.index["members"]}

    def _fetch(self, operation):
        kwargs = {}
        if self.sleep_fn is not None:
            kwargs["sleep_fn"] = self.sleep_fn
        return bounded_read(operation, self.retry, **kwargs)

    def _range(self, offset, length):
        pack_length = self.index["pack_byte_length"]
        if getattr(self.backend, "range_supported", True) is False \
                and (self.whole_pack_cap is None
                     or pack_length > self.whole_pack_cap):
            # The backend can only answer with the whole body and that body
            # is undeclared or over the cap — refuse before it is read.
            raise FilmError(
                "RANGE_FALLBACK_DENIED: the backend cannot serve ranges "
                "and the whole pack is undeclared or exceeds the declared "
                "whole-pack cap; refusing before the body is read")
        result = self._fetch(lambda: self.backend.get_range(
            self.pack_object_id, offset, length))
        if result.status == 206:
            if result.range_start != offset:
                raise FilmError("INPUT_MISMATCH: range response starts at "
                                "the wrong offset")
            if len(result.body) != length:
                raise FilmError("INPUT_MISMATCH: range response is shorter "
                                "than the index member length")
            if result.total_length is None \
                    or result.total_length != pack_length:
                raise FilmError("INPUT_MISMATCH: range response total "
                                "length does not match the pinned "
                                "pack_byte_length")
            self.fetched_bytes += len(result.body)
            return result.body
        if result.status == 200:
            # Whole body answering a range request: only a predeclared cap
            # makes this a permitted fallback. The declared/known total is
            # checked before the body is adopted — an over-cap or
            # wrong-total response is refused, never spooled. Returns None —
            # the caller serves the member from the verified whole body.
            if self.whole_pack_cap is None:
                raise FilmError(
                    "RANGE_FALLBACK_DENIED: the backend answered a range "
                    "request with an undeclared whole pack; refusing the "
                    "unbounded body")
            if result.total_length is None:
                # No declared total: only usable when the pinned pack
                # length sits inside the declared cap and the body is
                # exactly that long — anything else is refused.
                if pack_length > self.whole_pack_cap \
                        or len(result.body) != pack_length:
                    raise FilmError(
                        "INPUT_MISMATCH: a whole-body response without a "
                        "declared total length must equal the pinned "
                        "pack_byte_length inside the declared cap")
            elif result.total_length != pack_length:
                raise FilmError(
                    "INPUT_MISMATCH: whole-body response total length "
                    "does not match the pinned pack_byte_length")
            if pack_length > self.whole_pack_cap \
                    or len(result.body) > self.whole_pack_cap:
                raise FilmError(
                    "CAPACITY_BLOCKED: the whole-pack body exceeds the "
                    "declared cap; refusing it")
            self._adopt_whole(result.body)
            return None
        raise FilmError(f"Unexpected range response status {result.status}")

    def _adopt_whole(self, body):
        pack_length = self.index["pack_byte_length"]
        if len(body) != pack_length:
            raise FilmError("INPUT_MISMATCH: whole-pack body length does "
                            "not match pack_byte_length")
        if sha256_bytes(body) != self.index["pack_sha256"]:
            raise FilmError("Whole-pack fallback hash does not match "
                            "pack_sha256")
        self._whole = body
        self.fetched_bytes += len(body)
        self.state = VERIFIED_FULL_PACK
        return body

    def _slice_member(self, member):
        start, length = member["byte_offset"], member["byte_length"]
        return self._whole[start:start + length]

    # -- reads ----------------------------------------------------------------
    def read_member(self, member_id):
        """Verified bytes for one member; cached once hash-checked."""
        if member_id in self.verified:
            return self.verified[member_id]
        member = self._members().get(member_id)
        if member is None:
            raise FilmError(f"Unknown pack member: {member_id}")
        if self._whole is not None:
            data = self._slice_member(member)
        else:
            data = self._range(member["byte_offset"], member["byte_length"])
            if data is None:
                data = self._slice_member(member)
        # Complete member hash gate before any decode.
        if sha256_bytes(data) != member["sha256"]:
            raise FilmError(f"Member {member_id} SHA-256 mismatch")
        self.verified[member_id] = data
        if self.state == UNVERIFIED:
            self.state = VERIFIED_MEMBERS
        return data

    def read_all(self):
        for member in self.index["members"]:
            self.read_member(member["member_id"])
        return dict(self.verified)

    def verify_full_pack(self):
        """Fetch the whole object and hash it: VERIFIED_FULL_PACK only."""
        if self.state == VERIFIED_FULL_PACK:
            return self._whole
        pack_length = self.index["pack_byte_length"]
        if self.whole_pack_cap is not None \
                and pack_length > self.whole_pack_cap:
            raise FilmError("pack_byte_length exceeds the declared "
                            "whole-pack cap; refusing the fetch")
        result = self._fetch(lambda: self.backend.get_object(
            self.pack_object_id))
        if result.status != 200:
            raise FilmError("Whole-pack fetch requires a 200 body")
        if result.total_length is not None \
                and result.total_length != pack_length:
            raise FilmError("INPUT_MISMATCH: whole-pack fetch declares a "
                            "total length that differs from the pinned "
                            "pack_byte_length")
        return self._adopt_whole(result.body)

    def slice_member(self, member_id):
        """Member bytes from an already VERIFIED_FULL_PACK body."""
        member = self._members().get(member_id)
        if member is None:
            raise FilmError(f"Unknown pack member: {member_id}")
        if self._whole is None:
            return self.read_member(member_id)
        return self._slice_member(member)


def fetch_index(backend, index_object_id, index_sha256, retry=None,
                sleep_fn=None):
    """Fetch the pinned index object and validate it before member reads."""
    from .fav_pack import read_index
    kwargs = {}
    if sleep_fn is not None:
        kwargs["sleep_fn"] = sleep_fn
    result = bounded_read(lambda: backend.get_object(index_object_id),
                          retry, **kwargs)
    if result.status != 200:
        raise FilmError("Index fetch requires a 200 body")
    return read_index(result.body, expected_sha256=index_sha256)
