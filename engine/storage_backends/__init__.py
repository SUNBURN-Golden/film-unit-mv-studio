"""Archive backend interface (ANIM-013, schema §10, execution/storage §4).

An archive backend stores immutable objects addressed by content. A locator
(object id, revision) is transport data only — content identity is the
SHA-256 recorded in the archive manifest, never a filename, mtime or
server-assigned id. Objects are content-addressed: an overwrite with
different bytes is never applied; changed content is a new object.

Only HTTP-style 206/200 range semantics reach `RangeResult`; every other
status is an `ArchiveRequestError` carrying the status code and the
machine-readable reason the bounded retry policy inspects.
"""
from ..core import FilmError

OBJECT_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"

# Idempotent GET/Range and same-session status queries may retry only these:
# connection interruption, 408, 429, explicit rate-limit 403 and 5xx.
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class ConnectionDropped(FilmError):
    """The transfer stopped mid-flight without a status answer.

    This is the only non-HTTP failure the bounded transport policy may
    retry — after confirming the server-side state (a fresh range read or
    an upload-status query), never by blindly resending the same bytes.
    """


class ArchiveRequestError(FilmError):
    """A backend answered with an HTTP-style status code."""

    def __init__(self, status, reason, message, retry_after_ms=None):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.retry_after_ms = retry_after_ms

    def retriable(self):
        """401, permission 403, 404 and content mismatches never retry."""
        if self.status in (401, 404, 416):
            return False
        if self.status == 403:
            return self.reason == "rate_limit"
        return self.status in RETRYABLE_STATUS


class RangeResult:
    """A successful read response: exact 206 partial range or 200 whole body."""

    def __init__(self, status, body, range_start=None, total_length=None):
        self.status = status                # 206 partial | 200 whole object
        self.body = body
        self.range_start = range_start      # server-confirmed first byte
        self.total_length = total_length    # server-declared object length


def check_object_id(object_id):
    import re
    if type(object_id) is not str or not re.fullmatch(OBJECT_ID, object_id):
        raise FilmError(f"Malformed archive object id: {object_id!r}")
    return object_id
