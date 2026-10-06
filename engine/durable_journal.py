"""Durable append-only journal for worker/archive side effects (ANIM-021).

Schema 13.1 / execution-storage 9.1: before the first network side effect
of a job the coordinator leaves a `job_journal` 1 SUBMIT_INTENT binding
snapshot, operation, range, halo, recipe, toolchain, quote/entitlement,
reservation, credential epoch and job/attempt/request id. Responses are
observations; only verifier-approved checkpoints may be reused after a
restart. A truncated, duplicated or out-of-order journal is UNKNOWN /
RECONCILIATION_REQUIRED — a corrupt tail is never silently dropped or
"repaired" into success, and nothing is ever appended past it.

Storage format: one `job_journal` document per line, canon JSON with the
required trailing LF, in a single append-only `job_journal.jsonl` file per
owner directory. Every record carries `seq`, the previous record's
`digest` and its own `digest` — sha256 over the canon bytes of the record
without the digest field — so reordering, replay, truncation and edits are
all detected on load. Each append ends in fsync; creating the file fsyncs
its directory once.

Secret-free persistence: journal `data` carries digests and opaque store
references only. A raw request nonce and a resumable-session URI never
appear here — the session URI lives in the caller's session store and the
record holds only its digest reference.
"""
from pathlib import Path
import hashlib
import json
import os
import threading

from .animation_schema import canon_bytes, check_document
from .core import FilmError, now

JOURNAL_TYPE = "job_journal"
RECORD_FIELDS = {"document_type", "schema_version", "scope", "seq",
                 "event", "at", "data", "prev_digest", "digest"}
GENESIS = "0" * 64

# Journal event vocabulary. The scope separates owners sharing one file:
# the coordinator writes scope "coordinator", the upload tracker "upload"
# and the archive committer "commit".
EVENTS = {
    # coordinator (schema 13 identity lifecycle)
    "STATE_SNAPSHOT",            # pre-journal runtime_state migration
    "JOB_PLANNED", "ATTEMPT", "ATTEMPT_ROLLBACK",
    "SUBMIT_INTENT", "SUBMIT_LOST",
    "RECEIPT", "LATE_RECEIPT", "OBSERVATION",
    "VERIFY_FAILED", "CHECKPOINT", "JOB_SEALED",
    "CANCEL_INTENT", "CANCEL_LOST",
    "RESET_COVERAGE", "RESERVATION_RELEASED",
    "STATE",
    # bounded resumable upload tracker
    "UPLOAD_INTENT", "UPLOAD_SESSION", "UPLOAD_OFFSET",
    "UPLOAD_SESSION_LOST", "UPLOAD_SESSION_FENCED", "UPLOAD_COMPLETE",
    # archive commit / seal stages
    "COMMIT_INTENT", "OBJECT_VERIFIED", "OBJECTS_VERIFIED",
    "MANIFEST_INTENT", "MANIFEST_PUBLISHED", "SEALED", "SEAL_UNKNOWN",
    "ORPHAN_DELETED",
}

TAIL_CLEAN = "CLEAN"
TAIL_TRUNCATED = "TRUNCATED"
TAIL_CORRUPT = "CORRUPT"


class JournalCorrupt(FilmError):
    """A journal line failed its canon, chain or shape check."""


def record_digest(record):
    """sha256 of the canon bytes of the record without its digest field."""
    base = {key: value for key, value in record.items() if key != "digest"}
    return hashlib.sha256(canon_bytes(base)).hexdigest()


def validate_record(record):
    """Structural contract of a durable `job_journal` 1 record."""
    check_document(record, JOURNAL_TYPE)
    if type(record) is not dict or set(record.keys()) != RECORD_FIELDS:
        raise JournalCorrupt(
            f"job_journal record must hold {sorted(RECORD_FIELDS)}")
    if type(record["scope"]) is not str or not record["scope"]:
        raise JournalCorrupt("job_journal scope must be a non-empty string")
    if type(record["seq"]) is not int or record["seq"] < 0:
        raise JournalCorrupt("job_journal seq must be an integer >= 0")
    if record["event"] not in EVENTS:
        raise JournalCorrupt(f"Unknown job_journal event {record['event']}")
    if type(record["at"]) is not str or not record["at"]:
        raise JournalCorrupt("job_journal at must be a timestamp string")
    if type(record["data"]) is not dict:
        raise JournalCorrupt("job_journal data must be an object")
    for field in ("prev_digest", "digest"):
        value = record[field]
        if type(value) is not str or len(value) != 64 \
                or any(c not in "0123456789abcdef" for c in value):
            raise JournalCorrupt(f"job_journal {field} must be a lowercase "
                                 "SHA-256")
    if record["digest"] != record_digest(record):
        raise JournalCorrupt("job_journal record digest mismatch")
    return record


def load_journal(path):
    """Read and verify a journal file; never raise on a bad tail.

    Returns {"records": valid prefix, "head": chain tip or None,
             "tail": CLEAN|TRUNCATED|CORRUPT, "tail_reason": str|None}.
    The first failing line stops the scan — everything after it is
    unreadable evidence, not history.
    """
    path = Path(path)
    result = {"records": [], "head": None, "tail": TAIL_CLEAN,
              "tail_reason": None}
    if not path.exists():
        return result
    raw = path.read_bytes()
    if not raw:
        return result
    lines = raw.split(b"\n")
    last = len(lines) - 1
    for index, line in enumerate(lines):
        if not line:
            continue                       # only the post-final-\n remainder
        if index == last and not raw.endswith(b"\n"):
            # A torn tail record is never parsed, even if the bytes happen
            # to complete a document: durability required the newline.
            result["tail"] = TAIL_TRUNCATED
            result["tail_reason"] = "journal ends mid-record"
            break
        if result["tail"] != TAIL_CLEAN:
            break                          # bytes after a truncated tail
        try:
            document = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            result["tail"] = TAIL_CORRUPT
            result["tail_reason"] = f"line is not JSON: {e}"
            break
        try:
            if canon_bytes(document) != line + b"\n":
                raise JournalCorrupt("line is not canonical JSON")
            validate_record(document)
        except (FilmError, ValueError, TypeError) as e:
            result["tail"] = TAIL_CORRUPT
            result["tail_reason"] = str(e)
            break
        expected_seq = len(result["records"])
        if document["seq"] != expected_seq:
            result["tail"] = TAIL_CORRUPT
            result["tail_reason"] = (
                f"record seq {document['seq']} out of order; expected "
                f"{expected_seq}")
            break
        prev = result["head"] or GENESIS
        if document["prev_digest"] != prev:
            result["tail"] = TAIL_CORRUPT
            result["tail_reason"] = "record prev_digest breaks the chain"
            break
        result["records"].append(document)
        result["head"] = document["digest"]
    return result


class DurableJournal:
    """One append-only, fsync'd, hash-chained journal file.

    `fenced` is set on a non-clean tail or a semantic inconsistency the
    owner reports through `fence()`; appends are refused while fenced —
    the corrupt tail is preserved for reconciliation, never overwritten.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        loaded = load_journal(self.path)
        self._records = loaded["records"]
        self.head = loaded["head"]
        self.tail = loaded["tail"]
        self.tail_reason = loaded["tail_reason"]
        self.fence_reason = None
        self._dir_synced = self.path.exists()

    @property
    def exists(self):
        return self.path.exists()

    @property
    def records(self):
        return list(self._records)

    @property
    def fenced(self):
        return self.tail != TAIL_CLEAN or self.fence_reason is not None

    def fence(self, reason):
        """Mark the journal unwritable without touching the file."""
        self.fence_reason = reason

    def _sync_dir(self):
        if self._dir_synced or os.name == "nt":
            self._dir_synced = True
            return
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self._dir_synced = True

    def append(self, scope, event, data):
        """Write one record before the side effect it authorises."""
        if event not in EVENTS:
            raise FilmError(f"Unknown job_journal event: {event}")
        with self._lock:
            if self.fenced:
                raise FilmError(
                    "JOURNAL_RECONCILIATION_REQUIRED: the durable journal "
                    f"is fenced ({self.tail_reason or self.fence_reason}); "
                    "reconcile it before any new side effect")
            record = {"document_type": JOURNAL_TYPE, "schema_version": 1,
                      "scope": scope, "seq": len(self._records),
                      "event": event, "at": now(), "data": data,
                      "prev_digest": self.head or GENESIS}
            record["digest"] = record_digest(record)
            try:
                blob = canon_bytes(record)
            except FilmError as e:
                raise FilmError(f"journal record is not canonical: {e}") \
                    from e
            self.path.parent.mkdir(parents=True, exist_ok=True)
            created = not self.path.exists()
            with self.path.open("ab") as stream:
                stream.write(blob)
                stream.flush()
                os.fsync(stream.fileno())
            if created:
                self._sync_dir()
            self._records.append(record)
            self.head = record["digest"]
            return record

    def status(self):
        return {"path": str(self.path),
                "records": len(self._records),
                "head": self.head,
                "tail": self.tail,
                "tail_reason": self.tail_reason,
                "fenced": self.fenced,
                "fence_reason": self.fence_reason}
