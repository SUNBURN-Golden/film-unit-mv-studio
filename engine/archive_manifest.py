"""storage_archive manifest and archive transport contract (ANIM-013).

Schema §10/§16: a `storage_archive` document pins every archive object by
content hash and byte length, the archive profile (`LOCAL_FULL` or
`DRIVE_BOUNDED`), a bounded transport retry policy and the verification
level the seal requires. Content identity is the recorded SHA-256 — never
a file name, mtime or server locator.

Transport (evolution doc §4.2.1/4.2.2): same-session idempotent retries
with explicit caps; a dropped upload resumes only after the server-
confirmed offset is re-read, and retried bytes are really counted. A
local `put` hash plus a success response gives checkpoint/seal0 — the
`UPLOADED_UNVERIFIED` level. `UPLOAD_HASH_MATCHED` needs a provider-fixed
object SHA-256; when the provider cannot produce one, a bounded
`FULL_READBACK` — actual re-read bytes inside a declared cap — is
required. Seal is only evaluated when the achieved level meets the
manifest's `min_required`.
"""
import time

from .animation_schema import canon_bytes, check_document
from .core import FilmError
from .storage_backends import ArchiveRequestError, ConnectionDropped
from .fav_pack import sha256_bytes

ARCHIVE_TYPE = "storage_archive"
ARCHIVE_FIELDS = {"document_type", "schema_version", "archive_id",
                  "profile", "created_at", "pack", "objects",
                  "transport", "verification", "connection",
                  "qualification"}
PACK_FIELDS = {"object_id", "index_object_id", "byte_length", "sha256",
               "index_sha256"}
OBJECT_FIELDS = {"object_id", "kind", "byte_length", "sha256", "revision"}
TRANSPORT_FIELDS = {"retry", "upload"}
RETRY_FIELDS = {"max_attempts", "backoff_ms"}
UPLOAD_FIELDS = {"chunk_bytes", "resumable"}
VERIFICATION_FIELDS = {"level", "min_required", "readback_cap_bytes"}
CONNECTION_FIELDS = {"connection_id", "account_binding_digest",
                     "credential_epoch"}
PROFILES = {"LOCAL_FULL", "DRIVE_BOUNDED"}
LEVELS = {"UPLOADED_UNVERIFIED": 0, "UPLOAD_HASH_MATCHED": 1,
          "FULL_READBACK": 2}
DEFAULT_RETRY = {"max_attempts": 4, "backoff_ms": [50, 100, 200]}
DEFAULT_UPLOAD = {"chunk_bytes": 262144, "resumable": True}
DEFAULT_READBACK_CAP = 64 * 1024 * 1024


def _int(value, what, minimum=0):
    if type(value) is not int or value < minimum:
        raise FilmError(f"{what} must be an integer >= {minimum}")
    return value


def _sha(value, what):
    if type(value) is not str or len(value) != 64 \
            or any(c not in "0123456789abcdef" for c in value):
        raise FilmError(f"{what} must be a lowercase SHA-256")
    return value


def make_archive(archive_id, profile, objects, *, pack=None,
                 transport=None, verification=None, connection=None,
                 created_at=None):
    """Assemble and validate a storage_archive manifest."""
    from .core import now
    doc = {"document_type": ARCHIVE_TYPE, "schema_version": 1,
           "archive_id": archive_id, "profile": profile,
           "created_at": created_at or now(),
           "pack": pack, "objects": list(objects),
           "transport": transport or {"retry": dict(DEFAULT_RETRY),
                                      "upload": dict(DEFAULT_UPLOAD)},
           "verification": verification
           or {"level": "UPLOADED_UNVERIFIED",
               "min_required": "UPLOAD_HASH_MATCHED",
               "readback_cap_bytes": DEFAULT_READBACK_CAP},
           "connection": connection,
           "qualification": {
               # Fake-backend evidence never promotes a real connection.
               "node_state": "IN_PROGRESS",
               "qualification_state": "UNQUALIFIED",
               "acceptance_state": "PENDING",
               "release_state": "NOT_AUTHORIZED"}}
    return validate_archive(doc)


def archive_sha(document):
    return sha256_bytes(canon_bytes(document))


def validate_archive(document):
    check_document(document, ARCHIVE_TYPE)
    if set(document.keys()) - ARCHIVE_FIELDS:
        raise FilmError("Unknown storage_archive fields")
    if type(document.get("archive_id")) is not str \
            or not document["archive_id"]:
        raise FilmError("archive_id must be a non-empty string")
    if document.get("profile") not in PROFILES:
        raise FilmError("profile must be LOCAL_FULL or DRIVE_BOUNDED")
    if type(document.get("created_at")) is not str:
        raise FilmError("created_at must be an ISO timestamp string")
    pack = document.get("pack")
    if pack is not None:
        if type(pack) is not dict or set(pack.keys()) != PACK_FIELDS:
            raise FilmError("pack must hold exactly the packed object pins")
        for field in ("object_id", "index_object_id"):
            if type(pack.get(field)) is not str or not pack[field]:
                raise FilmError(f"pack.{field} must be a non-empty string")
        _int(pack.get("byte_length"), "pack.byte_length", 1)
        _sha(pack.get("sha256"), "pack.sha256")
        _sha(pack.get("index_sha256"), "pack.index_sha256")
    objects = document.get("objects")
    if type(objects) is not list or not objects:
        raise FilmError("objects must be a non-empty list")
    seen = set()
    for position, obj in enumerate(objects):
        if type(obj) is not dict or set(obj.keys()) - OBJECT_FIELDS:
            raise FilmError(f"Malformed archive object at position {position}")
        if type(obj.get("object_id")) is not str or not obj["object_id"]:
            raise FilmError("object_id must be a non-empty string")
        if obj["object_id"] in seen:
            raise FilmError(f"Duplicate object_id: {obj['object_id']}")
        seen.add(obj["object_id"])
        if type(obj.get("kind")) is not str or not obj["kind"]:
            raise FilmError("object kind must be a non-empty string")
        _int(obj.get("byte_length"), "object byte_length", 1)
        _sha(obj.get("sha256"), "object sha256")
        if obj.get("revision") is not None \
                and type(obj["revision"]) is not str:
            raise FilmError("object revision must be a string or null")
    if pack is not None:
        for field in ("object_id", "index_object_id"):
            if pack[field] not in seen:
                raise FilmError(f"pack.{field} is not a listed object")
    transport = document.get("transport")
    if type(transport) is not dict or set(transport.keys()) != TRANSPORT_FIELDS:
        raise FilmError("transport must hold exactly retry + upload")
    retry, upload = transport["retry"], transport["upload"]
    if type(retry) is not dict or set(retry.keys()) != RETRY_FIELDS:
        raise FilmError("transport.retry fields are fixed")
    attempts = _int(retry.get("max_attempts"), "retry.max_attempts", 1)
    if attempts > 8:
        raise FilmError("retry.max_attempts exceeds the bounded cap 8")
    backoff = retry.get("backoff_ms")
    if type(backoff) is not list \
            or any(type(v) is not int or v < 0 or v > 60000 for v in backoff):
        raise FilmError("retry.backoff_ms must be bounded millisecond steps")
    if type(upload) is not dict or set(upload.keys()) != UPLOAD_FIELDS:
        raise FilmError("transport.upload fields are fixed")
    _int(upload.get("chunk_bytes"), "upload.chunk_bytes", 1)
    if type(upload.get("resumable")) is not bool:
        raise FilmError("upload.resumable must be a boolean")
    verification = document.get("verification")
    if type(verification) is not dict \
            or set(verification.keys()) != VERIFICATION_FIELDS:
        raise FilmError("verification fields are fixed")
    for field in ("level", "min_required"):
        if verification.get(field) not in LEVELS:
            raise FilmError(f"Unknown verification {field}: "
                            f"{verification.get(field)}")
    cap = verification.get("readback_cap_bytes")
    if cap is not None:
        _int(cap, "readback_cap_bytes", 1)
    qualification = document.get("qualification")
    if type(qualification) is not dict or set(qualification.keys()) != {
            "node_state", "qualification_state", "acceptance_state",
            "release_state"}:
        raise FilmError("qualification must carry the four reporting facets")
    if qualification["node_state"] not in {"NOT_STARTED", "WAITING",
                                           "IN_PROGRESS", "DONE"}:
        raise FilmError("Unknown qualification.node_state")
    if qualification["qualification_state"] not in {
            "NOT_REQUIRED", "UNQUALIFIED", "PARTIAL", "QUALIFIED"}:
        raise FilmError("Unknown qualification_state")
    if qualification["acceptance_state"] not in {
            "NOT_REQUIRED", "PENDING", "ACCEPTED", "REJECTED"}:
        raise FilmError("Unknown acceptance_state")
    if qualification["release_state"] not in {
            "NOT_AUTHORIZED", "NOT_RELEASED", "RELEASED"}:
        raise FilmError("Unknown release_state")
    connection = document.get("connection")
    if connection is not None:
        if type(connection) is not dict \
                or set(connection.keys()) != CONNECTION_FIELDS:
            raise FilmError("connection holds only non-secret grant metadata")
        _int(connection.get("credential_epoch"), "credential_epoch", 1)
        # The fixed field set is itself the no-secrets guarantee: tokens,
        # authorization codes and upload URIs have no field to hide in.
    return document


# -- bounded same-session transport -------------------------------------------

def _sleep_ms(ms):
    if ms:
        time.sleep(ms / 1000)


def bounded_read(operation, retry, sleep_fn=_sleep_ms):
    """Run an idempotent read through the declared retry policy.

    Only interrupted transfers and `retriable()` statuses retry, bounded by
    `max_attempts`; authorization and content mismatches never do.
    """
    attempts = retry.get("max_attempts", 4)
    backoff = list(retry.get("backoff_ms", []))
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except ConnectionDropped:
            if attempt == attempts:
                raise
            sleep_fn(backoff[min(attempt - 1, len(backoff) - 1)]
                     if backoff else 0)
        except ArchiveRequestError as e:
            if not e.retriable() or attempt == attempts:
                raise
            sleep_fn(e.retry_after_ms
                     if e.retry_after_ms is not None
                     else (backoff[min(attempt - 1, len(backoff) - 1)]
                           if backoff else 0))
    raise FilmError("unreachable")


def resumable_put(backend, object_id, data, upload, retry,
                  sleep_fn=_sleep_ms):
    """Upload through a resumable session with server-confirmed offsets.

    On a dropped transfer the confirmed offset is re-queried before any
    byte is re-sent — resumed bytes are real duplicate/transferred bytes,
    never a blind re-POST. Non-resumable upload configs fall back to one
    atomic put (still the checkpoint evidence only).
    """
    expected = len(data)
    if not upload.get("resumable", True) \
            or not hasattr(backend, "create_upload_session"):
        bounded_read(lambda: backend.put_object(object_id, data), retry,
                     sleep_fn)
        return {"object_id": object_id, "resumed": False,
                "bytes_confirmed": expected}
    session = backend.create_upload_session(object_id, expected)
    confirmed = backend.upload_status(session)["offset"]
    chunk = upload.get("chunk_bytes", DEFAULT_UPLOAD["chunk_bytes"])
    max_attempts = retry.get("max_attempts", 4)
    failures = 0
    while confirmed < expected:
        piece = data[confirmed:confirmed + chunk]
        try:
            backend.upload_chunk(session, confirmed, piece)
            confirmed = backend.upload_status(session)["offset"]
            failures = 0
        except ConnectionDropped:
            # Re-read the confirmed offset; send only what is still missing.
            failures += 1
            confirmed = backend.upload_status(session)["offset"]
        except ArchiveRequestError as e:
            if not e.retriable():
                raise
            failures += 1
            confirmed = backend.upload_status(session)["offset"]
        if failures >= max_attempts:
            raise FilmError(
                "TRANSPORT_RETRY_EXHAUSTED: the upload could not confirm "
                "forward progress within the declared attempt cap")
    bounded_read(lambda: backend.complete_upload(session), retry, sleep_fn)
    return {"object_id": object_id, "resumed": confirmed > 0,
            "bytes_confirmed": confirmed}


# -- artifact verification levels ----------------------------------------------

def verify_object(backend, object_id, expected_sha256, expected_length,
                  readback_cap=DEFAULT_READBACK_CAP, retry=None,
                  sleep_fn=_sleep_ms):
    """Classify the strongest evidence available for one stored object.

    A provider-fixed SHA-256 yields UPLOAD_HASH_MATCHED without re-reading.
    Without one, the whole object is re-read inside `readback_cap` and
    hashed for FULL_READBACK; an object beyond the cap cannot be verified
    that way and raises READBACK_CAP_EXCEEDED rather than being spooled.
    """
    retry = retry or DEFAULT_RETRY
    info = bounded_read(lambda: backend.object_info(object_id), retry,
                        sleep_fn)
    if info.get("byte_length") != expected_length:
        raise FilmError(f"Archive object {object_id} stored length "
                        f"{info.get('byte_length')} != manifest "
                        f"{expected_length}")
    if info.get("provider_checksum") == "sha256":
        if info.get("sha256") != expected_sha256:
            raise FilmError(f"Archive object {object_id} provider hash "
                            "mismatch")
        return "UPLOAD_HASH_MATCHED"
    # No trustworthy provider checksum: bounded full readback is required.
    if expected_length > (readback_cap or 0):
        raise FilmError(
            f"READBACK_CAP_EXCEEDED: object {object_id} needs "
            f"{expected_length} readback bytes > cap {readback_cap}; "
            "no provider SHA-256 evidence exists")
    result = bounded_read(lambda: backend.get_object(object_id), retry,
                          sleep_fn)
    if result.status != 200 or len(result.body) != expected_length:
        raise FilmError(f"Archive object {object_id} readback incomplete")
    if sha256_bytes(result.body) != expected_sha256:
        raise FilmError(f"Archive object {object_id} readback hash mismatch")
    return "FULL_READBACK"


def verify_archive(backend, document, readback_cap=None, sleep_fn=_sleep_ms):
    """Verify every manifest object; report the weakest achieved level."""
    transport = document["transport"]
    cap = readback_cap if readback_cap is not None \
        else document["verification"].get("readback_cap_bytes")
    weakest = max(LEVELS.values())
    for obj in document["objects"]:
        level = verify_object(backend, obj["object_id"], obj["sha256"],
                              obj["byte_length"], readback_cap=cap,
                              retry=transport["retry"], sleep_fn=sleep_fn)
        weakest = min(weakest, LEVELS[level])
    return next(name for name, rank in LEVELS.items() if rank == weakest)


def seal_archive(document, achieved_level):
    """Evaluate the seal against the configured minimum verification level.

    A sealed manifest carries the achieved level in `verification.level`;
    when the achieved level is below `min_required` the archive stays
    unsealed and raises ARCHIVE_UNSEALED rather than reporting success.
    """
    if LEVELS[achieved_level] < LEVELS[document["verification"]["min_required"]]:
        raise FilmError(
            f"ARCHIVE_UNSEALED: achieved {achieved_level} does not meet "
            f"min_required {document['verification']['min_required']}")
    sealed = dict(document)
    sealed["verification"] = dict(document["verification"],
                                  level=achieved_level)
    return validate_archive(sealed)


def seal_status(document, achieved_level):
    """Non-raising seal evaluation for status reports."""
    return {"archive_id": document["archive_id"],
            "achieved_level": achieved_level,
            "min_required": document["verification"]["min_required"],
            "sealed": LEVELS[achieved_level]
            >= LEVELS[document["verification"]["min_required"]]}
