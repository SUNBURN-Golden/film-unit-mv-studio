"""Bounded, journaled resumable upload tracker (ANIM-021).

Schema 13.1 / evolution 4.2.1-4.2.2: an upload checkpoint records the
secret session *reference*, the local object hash, the server-confirmed
offset and the returned object id as separate durable facts. The raw
resumable-session URI is a secret and never enters the journal — it lives
in the caller's `session_store` keyed by the opaque reference the journal
records. A process restart therefore cannot reuse the old session's URI:
the reference is fenced and a new session is opened with a fresh
journaled intent.

Rules the tracker enforces:

- `UPLOAD_INTENT` is journaled before `create_upload_session` runs.
- The confirmed offset advances only on the backend's own
  `upload_status` answer — a server-confirmed number, never a guess.
- A dropped chunk or status response triggers a same-session status
  query before any byte is re-sent; blindly re-POSTing from an assumed
  offset is never done.
- Session expiry (status 404) fences the session and opens an explicit
  new session with a fresh `UPLOAD_SESSION` record — bounded by the
  declared retry allowance.
- A lost `create_upload_session` or non-resumable `put_object` answer is
  never retried on the HTTP allowance: the intent is journaled
  UPLOAD_SESSION_LOST and stays session-unknown until an explicit
  `reconcile` answers what landed — a second session is never minted
  automatically.
- Upload progress is *not* archive verification: `UPLOAD_COMPLETE` is a
  transfer fact; `verify_object` still decides the schema-10.3 level and
  `UPLOADED_UNVERIFIED` never counts as a checkpoint or seal input.
- Request, retry, transferred-byte, elapsed and session caps come from
  the declared transport retry policy and apply across the whole drive
  (session create, every status query, every chunk send and the
  whole-object path); exceeding them raises TRANSPORT_RETRY_EXHAUSTED.
"""
import hashlib
import threading
import time

from .archive_manifest import (DEFAULT_READBACK_CAP, DEFAULT_UPLOAD,
                               verify_object)
from .core import FilmError
from .fav_pack import sha256_bytes
from .storage_backends import ArchiveRequestError, ConnectionDropped


def session_ref(session):
    """The opaque store reference the journal may hold — never the URI."""
    return "sess-" + hashlib.sha256(session.encode("utf-8")).hexdigest()[:24]


class _Budget:
    """One upload drive's declared transport allowance.

    `max_requests`, `max_transferred_bytes` and `max_elapsed_ms` apply
    across the whole `put` — they are not reset per backend call the way
    a per-read bounded policy would be. Every backend request counts,
    every sent chunk byte counts (sent-or-dropped), and response bodies
    count against the transferred cap the same way `bounded_read` counts
    them.
    """

    def __init__(self, retry):
        retry = retry or {}
        self.max_retries = retry.get("max_retries", 0)
        self.max_requests = retry.get("max_requests")
        self.max_bytes = retry.get("max_transferred_bytes")
        self.max_elapsed = retry.get("max_elapsed_ms")
        self.requests = 0
        self.transferred = 0
        self.start = time.monotonic()

    @staticmethod
    def _exhausted(what):
        return FilmError("TRANSPORT_RETRY_EXHAUSTED: the upload exceeded "
                         f"the declared policy's {what}")

    def elapsed_ms(self):
        return (time.monotonic() - self.start) * 1000

    def request(self):
        """One backend request — refused once the caps are spent."""
        if self.max_requests is not None \
                and self.requests >= self.max_requests:
            raise self._exhausted("max_requests cap")
        self.requests += 1
        self.check_elapsed()

    def sent(self, byte_count):
        """Bytes put on the wire (or lost with the answer)."""
        self.transferred += byte_count
        if self.max_bytes is not None \
                and self.transferred > self.max_bytes:
            raise self._exhausted("max_transferred_bytes cap")

    def check_elapsed(self):
        if self.max_elapsed is not None \
                and self.elapsed_ms() > self.max_elapsed:
            raise self._exhausted("max_elapsed_ms cap")

    def check_wait(self, wait_ms):
        if self.max_elapsed is not None \
                and self.elapsed_ms() + wait_ms > self.max_elapsed:
            raise self._exhausted("remaining max_elapsed_ms allowance")


class UploadTracker:
    """Drives resumable uploads against a backend with journaled progress.

    `session_store` maps the opaque session reference to the real session
    URI for the life of this process. It is deliberately not the journal:
    a URI must never be persisted beside the checkpoint.
    """

    def __init__(self, journal=None, backend=None, *, upload=None,
                 retry=None, session_store=None, sleep_fn=None):
        self.journal = journal
        self.backend = backend
        self.upload = dict(upload or DEFAULT_UPLOAD)
        self.retry = retry
        self.sleep = sleep_fn or (lambda ms: None)
        self.session_store = (session_store
                              if session_store is not None else {})
        self.uploads = {}
        self._lock = threading.Lock()
        if journal is not None:
            self._replay(journal.records)

    def _j(self, event, data):
        if self.journal is not None:
            self.journal.append("upload", event, data)

    # -- replay -----------------------------------------------------------------
    def _replay(self, records):
        """Rebuild intent/session state from the durable upload records."""
        for record in records:
            event, data = record["event"], record["data"]
            if record["scope"] != "upload":
                continue            # other owners share the file
            intent = self.uploads.get(data.get("intent_id"))
            if event == "UPLOAD_INTENT":
                intent_id = data["intent_id"]
                if intent is not None and intent["sha256"] != \
                        data["sha256"]:
                    raise FilmError("journal replays an upload intent for "
                                    "different bytes")
                self.uploads[intent_id] = {
                    "intent_id": intent_id, "object_id": data["object_id"],
                    "sha256": data["sha256"],
                    "byte_length": data["byte_length"],
                    "sessions": [], "session": None, "complete": False,
                    "unknown": False, "result_object_id": None}
            elif intent is None:
                raise FilmError(f"journal {event} names unknown upload "
                                f"intent {data.get('intent_id')}")
            elif event == "UPLOAD_SESSION":
                session = {"ref": data["session_ref"],
                           "seq": data["session_seq"],
                           "fenced": False, "confirmed": 0}
                intent["sessions"].append(session)
                intent["session"] = session
            elif event == "UPLOAD_OFFSET":
                for session in intent["sessions"]:
                    if session["ref"] == data["session_ref"]:
                        session["confirmed"] = data["offset"]
            elif event == "UPLOAD_SESSION_LOST":
                intent["unknown"] = True
            elif event == "UPLOAD_SESSION_FENCED":
                if data["session_ref"] is None:
                    # The phantom whose answer was lost is fenced by an
                    # explicit reconcile — the same intent may continue.
                    intent["unknown"] = False
                else:
                    for session in intent["sessions"]:
                        if session["ref"] == data["session_ref"]:
                            session["fenced"] = True
                    if intent["session"] and \
                            intent["session"]["ref"] == data["session_ref"]:
                        intent["session"] = None
            elif event == "UPLOAD_COMPLETE":
                intent["complete"] = True
                intent["unknown"] = False
                intent["result_object_id"] = data["object_id"]

    # -- helpers -----------------------------------------------------------------
    def _intent(self, object_id):
        return self.uploads.get(f"up-{object_id}")

    def _read(self, operation, budget):
        """One metered idempotent read against the upload's budget.

        Same-session status/object queries are the only calls the
        declared retry policy may retry inside a drive: each try counts
        against `max_requests`, every response body against
        `max_transferred_bytes`, and every wait against `max_elapsed_ms`
        — the caps are spent across the whole upload, not per call.
        """
        retry = self.retry or {}
        base_ms = retry.get("backoff_base_ms", 0)
        cap_ms = retry.get("max_backoff_ms", 0)
        failures = 0
        while True:
            budget.request()
            try:
                result = operation()
            except (ConnectionDropped, ArchiveRequestError) as e:
                if isinstance(e, ArchiveRequestError) and not e.retriable():
                    raise
                if failures >= budget.max_retries:
                    raise
                after = getattr(e, "retry_after_ms", None)
                if after is not None and after > cap_ms:
                    raise FilmError(
                        "TRANSPORT_RETRY_EXHAUSTED: the provider's "
                        "Retry-After exceeds the policy's max_backoff_ms") \
                        from e
                wait = after if after is not None \
                    else min(cap_ms, base_ms * (2 ** failures))
                budget.check_wait(wait)
                self.sleep(wait)
                failures += 1
                continue
            budget.sent(len(getattr(result, "body", None) or b""))
            return result

    def _confirmed(self, session, budget):
        """Server-confirmed offset only — a status query, not a guess."""
        return self._read(lambda: self.backend.upload_status(session),
                          budget)["offset"]

    def _fence_session(self, intent, session_entry, reason):
        self._j("UPLOAD_SESSION_FENCED",
                {"intent_id": intent["intent_id"],
                 "session_ref": session_entry["ref"], "reason": reason})
        session_entry["fenced"] = True
        if intent["session"] is session_entry:
            intent["session"] = None

    def _mark_lost(self, intent, stage, error):
        """A create/publish answer was lost: journal the unknown outcome
        and fence the intent — the HTTP allowance never retries it."""
        self._j("UPLOAD_SESSION_LOST", {
            "intent_id": intent["intent_id"], "stage": stage,
            "reason": str(error)})
        intent["unknown"] = True
        return FilmError(
            f"UPLOAD_SESSION_UNKNOWN: the {stage} answer was lost; the "
            "session/object state is undetermined — reconcile the same "
            "intent explicitly, a second create or put is never minted "
            "on the transport retry allowance")

    def _open_session(self, intent, budget):
        """A new resumable session with a fresh journaled intent.

        The old session, if any, is fenced first — expiry never reuses an
        ambiguous session identity. `create_upload_session` is not a
        bounded_read call: a lost answer fences the intent as
        session-unknown instead of minting another session.
        """
        if intent["unknown"]:
            raise FilmError(
                "UPLOAD_SESSION_UNKNOWN: a create/publish answer was "
                "lost; reconcile the intent explicitly before any new "
                "session is opened")
        if intent["session"] is not None:
            self._fence_session(intent, intent["session"],
                                "superseded by a new session")
        max_sessions = (self.retry or {}).get("max_retries", 0) + 1
        if len(intent["sessions"]) >= max_sessions:
            raise FilmError(
                "TRANSPORT_RETRY_EXHAUSTED: the upload's session "
                "allowance is spent; reconcile the object explicitly")
        budget.request()
        try:
            uri = self.backend.create_upload_session(
                intent["object_id"], intent["byte_length"])
        except ConnectionDropped as e:
            raise self._mark_lost(intent, "create_upload_session", e) from e
        ref = session_ref(uri)
        seq = len(intent["sessions"]) + 1
        self._j("UPLOAD_SESSION", {"intent_id": intent["intent_id"],
                                   "session_ref": ref, "session_seq": seq,
                                   "expected_length":
                                   intent["byte_length"]})
        self.session_store[ref] = uri
        entry = {"ref": ref, "seq": seq, "fenced": False, "confirmed": 0}
        intent["sessions"].append(entry)
        intent["session"] = entry
        return uri

    def _begin(self, object_id, data):
        sha = sha256_bytes(data)
        intent = self._intent(object_id)
        if intent is None:
            intent_id = f"up-{object_id}"
            self._j("UPLOAD_INTENT", {"intent_id": intent_id,
                                      "object_id": object_id,
                                      "sha256": sha,
                                      "byte_length": len(data)})
            intent = {"intent_id": intent_id, "object_id": object_id,
                      "sha256": sha, "byte_length": len(data),
                      "sessions": [], "session": None, "complete": False,
                      "unknown": False, "result_object_id": None}
            self.uploads[intent_id] = intent
        elif intent["sha256"] != sha:
            raise FilmError("UPLOAD_IDENTITY_CONFLICT: intent "
                            f"{intent['intent_id']} pins different bytes")
        return intent

    # -- the bounded resumable drive ---------------------------------------------
    def put(self, object_id, data):
        """Upload `data` under `object_id`; idempotent on the same bytes."""
        with self._lock:
            intent = self._begin(object_id, data)
            if intent["complete"]:
                return {"object_id": intent["result_object_id"],
                        "resumed": False,
                        "bytes_confirmed": intent["byte_length"],
                        "intent_id": intent["intent_id"]}
            if intent["unknown"]:
                raise FilmError(
                    "UPLOAD_SESSION_UNKNOWN: a create/publish answer was "
                    "lost for this intent; reconcile it explicitly — a "
                    "second session is never minted on the transport "
                    "retry allowance")
            expected = intent["byte_length"]
            resumed = False
            budget = _Budget(self.retry)
            if not self.upload.get("resumable", True) \
                    or not hasattr(self.backend, "create_upload_session"):
                # A whole-object put is a publish-class side effect: a
                # lost answer fences the intent, it is never retried on
                # the HTTP allowance.
                budget.request()
                try:
                    self.backend.put_object(object_id, data)
                except ConnectionDropped as e:
                    raise self._mark_lost(intent, "put_object", e) from e
                budget.sent(len(data))
                self._j("UPLOAD_COMPLETE", {
                    "intent_id": intent["intent_id"], "object_id": object_id,
                    "session_ref": None})
                intent["complete"] = True
                intent["result_object_id"] = object_id
                return {"object_id": object_id, "resumed": False,
                        "bytes_confirmed": expected,
                        "intent_id": intent["intent_id"]}
            uri = self._uri(intent) or self._open_session(intent, budget)
            entry = intent["session"]
            confirmed = self._confirmed(uri, budget)
            self._offset(intent, entry, confirmed)
            chunk = self.upload.get("chunk_bytes",
                                    DEFAULT_UPLOAD["chunk_bytes"])
            max_retries = (self.retry or {}).get("max_retries", 0)
            failures = 0
            while confirmed < expected:
                if entry["fenced"] or uri is None:
                    # Session loss is never a blind re-POST: fence the old
                    # session and open an explicit new one.
                    uri = self._open_session(intent, budget)
                    entry = intent["session"]
                    confirmed = self._confirmed(uri, budget)
                    self._offset(intent, entry, confirmed)
                    continue
                piece = data[confirmed:confirmed + chunk]
                try:
                    budget.request()
                    try:
                        self.backend.upload_chunk(uri, confirmed, piece)
                    finally:
                        # Sent bytes count against the cap even when the
                        # answer is lost mid-transfer.
                        budget.sent(len(piece))
                    new_confirmed = self._confirmed(uri, budget)
                except ArchiveRequestError as e:
                    if e.status == 404:
                        # Session expiry: fence it; the next loop opens an
                        # explicit new session with a fresh intent.
                        self._fence_session(intent, entry,
                                            f"session gone: {e.reason}")
                        uri = None
                        continue
                    if not e.retriable():
                        raise
                    new_confirmed = self._confirmed(uri, budget)
                except ConnectionDropped:
                    # Response lost: query the same session's confirmed
                    # offset — never resend from a guessed position.
                    try:
                        new_confirmed = self._confirmed(uri, budget)
                    except ArchiveRequestError as e:
                        if e.status == 404:
                            self._fence_session(
                                intent, entry, f"session gone: {e.reason}")
                            uri = None
                            continue
                        raise
                self._offset(intent, entry, new_confirmed)
                if new_confirmed <= confirmed:
                    failures += 1
                    if failures > max_retries:
                        raise FilmError(
                            "TRANSPORT_RETRY_EXHAUSTED: the upload could "
                            "not confirm forward progress within the "
                            "declared retry allowance")
                else:
                    failures = 0
                    resumed = resumed or confirmed > 0
                confirmed = new_confirmed
            self._complete(intent, uri, entry, data, budget)
            return {"object_id": intent["result_object_id"],
                    "resumed": resumed,
                    "bytes_confirmed": confirmed,
                    "intent_id": intent["intent_id"]}

    def _offset(self, intent, entry, confirmed):
        """Journal the server-confirmed offset once and keep the live
        session entry reporting the same number."""
        if confirmed != entry["confirmed"]:
            self._j("UPLOAD_OFFSET", {
                "intent_id": intent["intent_id"],
                "session_ref": entry["ref"],
                "offset": confirmed})
            entry["confirmed"] = confirmed

    def _uri(self, intent):
        """Resolve the live session ref; None when the URI is unknown."""
        entry = intent["session"]
        if entry is None or entry["fenced"]:
            return None
        return self.session_store.get(entry["ref"])

    def _complete(self, intent, uri, entry, data, budget):
        try:
            budget.request()
            self.backend.complete_upload(uri)
        except (ConnectionDropped, ArchiveRequestError) as e:
            # The close-out response was lost or refused: never assume —
            # check whether the object is already stored, else whether the
            # session is still open.
            if isinstance(e, ArchiveRequestError) and not e.retriable() \
                    and e.status != 404:
                raise
            published = False
            try:
                info = self._read(
                    lambda: self.backend.object_info(intent["object_id"]),
                    budget)
                published = info.get("byte_length") == intent["byte_length"]
            except ArchiveRequestError as not_found:
                if not_found.status != 404:
                    raise
            if not published:
                try:
                    status = self._read(
                        lambda: self.backend.upload_status(uri), budget)
                except ArchiveRequestError as gone:
                    if gone.status == 404:
                        # The session is gone and no object landed:
                        # fence it and re-drive the same intent on a new
                        # session — the bytes are re-sent, never assumed.
                        self._fence_session(intent, entry,
                                            "session lost at completion")
                        uri = self._open_session(intent, budget)
                        entry = intent["session"]
                        return self._drive(intent, uri, entry, data, budget)
                    raise
                if status["offset"] == intent["byte_length"]:
                    # Same session, same close — re-issued only after the
                    # status query confirmed the server holds every byte.
                    try:
                        budget.request()
                        self.backend.complete_upload(uri)
                    except (ConnectionDropped, ArchiveRequestError) as e2:
                        raise FilmError(
                            "UPLOAD_RESPONSE_LOST: the retried completion "
                            "answer is also lost; reconcile the same "
                            "intent") from e2
                else:
                    raise FilmError(
                        "UPLOAD_RESPONSE_LOST: the completion answer is "
                        "lost and the object is not published; reconcile "
                        "the same intent")
        self._j("UPLOAD_COMPLETE", {"intent_id": intent["intent_id"],
                                    "object_id": intent["object_id"],
                                    "session_ref": entry["ref"]})
        intent["complete"] = True
        intent["result_object_id"] = intent["object_id"]

    def _drive(self, intent, uri, entry, data, budget):
        """Re-drive a fenced session's remaining bytes on the new session."""
        confirmed = self._confirmed(uri, budget)
        self._offset(intent, entry, confirmed)
        max_retries = (self.retry or {}).get("max_retries", 0)
        chunk = self.upload.get("chunk_bytes", DEFAULT_UPLOAD["chunk_bytes"])
        failures = 0
        while confirmed < intent["byte_length"]:
            piece = data[confirmed:confirmed + chunk]
            try:
                budget.request()
                try:
                    self.backend.upload_chunk(uri, confirmed, piece)
                finally:
                    budget.sent(len(piece))
                new_confirmed = self._confirmed(uri, budget)
            except (ConnectionDropped, ArchiveRequestError) as e:
                if isinstance(e, ArchiveRequestError) and not e.retriable():
                    if e.status == 404:
                        self._fence_session(intent, entry,
                                            "session gone mid-upload")
                        uri = self._open_session(intent, budget)
                        entry = intent["session"]
                        confirmed = self._confirmed(uri, budget)
                        self._offset(intent, entry, confirmed)
                        continue
                    raise
                new_confirmed = self._confirmed(uri, budget)
            self._offset(intent, entry, new_confirmed)
            if new_confirmed <= confirmed:
                failures += 1
                if failures > max_retries:
                    raise FilmError("TRANSPORT_RETRY_EXHAUSTED: re-driven "
                                    "upload stalled")
            else:
                failures = 0
            confirmed = new_confirmed
        return self._complete(intent, uri, entry, data, budget)

    # -- explicit reconciliation -------------------------------------------------
    def reconcile(self, object_id):
        """Resolve an intent fenced session-unknown by a lost answer.

        The only way forward: the backend is asked what actually landed —
        an object-info/hash check, never a blind second create or put.
        When the pinned bytes are already stored the intent completes by
        reuse; when nothing landed the phantom is fenced and the same
        intent may continue on an explicitly journaled new session.
        """
        with self._lock:
            intent = self._intent(object_id)
            if intent is None:
                raise FilmError(f"Unknown upload intent for {object_id}")
            if not intent["unknown"]:
                return {"intent_id": intent["intent_id"],
                        "object_id": object_id,
                        "complete": intent["complete"],
                        "reconciled": False}
            try:
                level = verify_object(
                    self.backend, object_id, intent["sha256"],
                    intent["byte_length"],
                    readback_cap=DEFAULT_READBACK_CAP,
                    retry=self.retry, sleep_fn=self.sleep)
            except ArchiveRequestError as e:
                if e.status != 404:
                    raise
                level = None
            if level is not None:
                self._j("UPLOAD_COMPLETE", {
                    "intent_id": intent["intent_id"],
                    "object_id": object_id, "session_ref": None})
                intent["complete"] = True
                intent["unknown"] = False
                intent["result_object_id"] = object_id
                return {"intent_id": intent["intent_id"],
                        "object_id": object_id, "complete": True,
                        "reconciled": "stored"}
            self._j("UPLOAD_SESSION_FENCED", {
                "intent_id": intent["intent_id"], "session_ref": None,
                "reason": "lost create/publish reconciled: nothing "
                          "landed; the same intent may continue"})
            intent["unknown"] = False
            return {"intent_id": intent["intent_id"],
                    "object_id": object_id, "complete": False,
                    "reconciled": "not_stored"}

    # -- status -------------------------------------------------------------------
    def pending(self):
        """Upload intents never confirmed complete — restart resume work."""
        return [{"intent_id": u["intent_id"], "object_id": u["object_id"],
                 "byte_length": u["byte_length"],
                 "confirmed": (u["session"] or {}).get("confirmed", 0)}
                for u in self.uploads.values() if not u["complete"]]

    def status(self):
        return {intent_id: {"object_id": u["object_id"],
                            "byte_length": u["byte_length"],
                            "complete": u["complete"],
                            "unknown": u["unknown"],
                            "sessions": [{"ref": s["ref"], "seq": s["seq"],
                                          "confirmed": s["confirmed"],
                                          "fenced": s["fenced"]}
                                         for s in u["sessions"]]}
                for intent_id, u in self.uploads.items()}
