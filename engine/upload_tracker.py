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
- Upload progress is *not* archive verification: `UPLOAD_COMPLETE` is a
  transfer fact; `verify_object` still decides the schema-10.3 level and
  `UPLOADED_UNVERIFIED` never counts as a checkpoint or seal input.
- Request, retry, transferred-byte and session caps come from the
  declared transport retry policy; exceeding them raises
  TRANSPORT_RETRY_EXHAUSTED.
"""
import hashlib
import threading

from .archive_manifest import DEFAULT_UPLOAD, bounded_read
from .core import FilmError
from .fav_pack import sha256_bytes
from .storage_backends import ArchiveRequestError, ConnectionDropped


def session_ref(session):
    """The opaque store reference the journal may hold — never the URI."""
    return "sess-" + hashlib.sha256(session.encode("utf-8")).hexdigest()[:24]


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
                    "result_object_id": None}
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
            elif event == "UPLOAD_SESSION_FENCED":
                for session in intent["sessions"]:
                    if session["ref"] == data["session_ref"]:
                        session["fenced"] = True
                if intent["session"] and \
                        intent["session"]["ref"] == data["session_ref"]:
                    intent["session"] = None
            elif event == "UPLOAD_COMPLETE":
                intent["complete"] = True
                intent["result_object_id"] = data["object_id"]

    # -- helpers -----------------------------------------------------------------
    def _intent(self, object_id):
        return self.uploads.get(f"up-{object_id}")

    def _confirmed(self, session):
        """Server-confirmed offset only — a status query, not a guess."""
        return bounded_read(lambda: self.backend.upload_status(session),
                            self.retry, self.sleep)["offset"]

    def _fence_session(self, intent, session_entry, reason):
        self._j("UPLOAD_SESSION_FENCED",
                {"intent_id": intent["intent_id"],
                 "session_ref": session_entry["ref"], "reason": reason})
        session_entry["fenced"] = True
        if intent["session"] is session_entry:
            intent["session"] = None

    def _open_session(self, intent):
        """A new resumable session with a fresh journaled intent.

        The old session, if any, is fenced first — expiry never reuses an
        ambiguous session identity.
        """
        if intent["session"] is not None:
            self._fence_session(intent, intent["session"],
                                "superseded by a new session")
        max_sessions = (self.retry or {}).get("max_retries", 0) + 1
        if len(intent["sessions"]) >= max_sessions:
            raise FilmError(
                "TRANSPORT_RETRY_EXHAUSTED: the upload's session "
                "allowance is spent; reconcile the object explicitly")
        uri = bounded_read(lambda: self.backend.create_upload_session(
            intent["object_id"], intent["byte_length"]),
            self.retry, self.sleep)
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
                      "result_object_id": None}
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
            expected = intent["byte_length"]
            resumed = False
            if not self.upload.get("resumable", True) \
                    or not hasattr(self.backend, "create_upload_session"):
                bounded_read(
                    lambda: self.backend.put_object(object_id, data),
                    self.retry, self.sleep)
                self._j("UPLOAD_COMPLETE", {
                    "intent_id": intent["intent_id"], "object_id": object_id,
                    "session_ref": None})
                intent["complete"] = True
                intent["result_object_id"] = object_id
                return {"object_id": object_id, "resumed": False,
                        "bytes_confirmed": expected,
                        "intent_id": intent["intent_id"]}
            uri = self._uri(intent) or self._open_session(intent)
            entry = intent["session"]
            confirmed = self._confirmed(uri)
            chunk = self.upload.get("chunk_bytes",
                                    DEFAULT_UPLOAD["chunk_bytes"])
            max_retries = (self.retry or {}).get("max_retries", 0)
            max_bytes = (self.retry or {}).get("max_transferred_bytes")
            sent = 0
            failures = 0
            while confirmed < expected:
                if entry["fenced"] or uri is None:
                    # Session loss is never a blind re-POST: fence the old
                    # session and open an explicit new one.
                    uri = self._open_session(intent)
                    entry = intent["session"]
                    confirmed = self._confirmed(uri)
                    continue
                piece = data[confirmed:confirmed + chunk]
                try:
                    self.backend.upload_chunk(uri, confirmed, piece)
                    sent += len(piece)
                    new_confirmed = self._confirmed(uri)
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
                    new_confirmed = self._confirmed(uri)
                except ConnectionDropped:
                    # Response lost: query the same session's confirmed
                    # offset — never resend from a guessed position.
                    try:
                        new_confirmed = self._confirmed(uri)
                    except ArchiveRequestError as e:
                        if e.status == 404:
                            self._fence_session(
                                intent, entry, f"session gone: {e.reason}")
                            uri = None
                            continue
                        raise
                if max_bytes is not None and sent > max_bytes:
                    raise FilmError(
                        "TRANSPORT_RETRY_EXHAUSTED: upload transferred "
                        "bytes exceed the policy's max_transferred_bytes")
                if new_confirmed != entry["confirmed"]:
                    self._j("UPLOAD_OFFSET", {
                        "intent_id": intent["intent_id"],
                        "session_ref": entry["ref"],
                        "offset": new_confirmed})
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
            self._complete(intent, uri, entry, data)
            return {"object_id": intent["result_object_id"],
                    "resumed": resumed,
                    "bytes_confirmed": confirmed,
                    "intent_id": intent["intent_id"]}

    def _uri(self, intent):
        """Resolve the live session ref; None when the URI is unknown."""
        entry = intent["session"]
        if entry is None or entry["fenced"]:
            return None
        return self.session_store.get(entry["ref"])

    def _complete(self, intent, uri, entry, data):
        try:
            bounded_read(lambda: self.backend.complete_upload(uri),
                         self.retry, self.sleep)
        except (ConnectionDropped, ArchiveRequestError) as e:
            # The close-out response was lost or refused: never assume —
            # check whether the object is already stored, else whether the
            # session is still open.
            if isinstance(e, ArchiveRequestError) and not e.retriable() \
                    and e.status != 404:
                raise
            published = False
            try:
                info = bounded_read(
                    lambda: self.backend.object_info(intent["object_id"]),
                    self.retry, self.sleep)
                published = info.get("byte_length") == intent["byte_length"]
            except ArchiveRequestError as not_found:
                if not_found.status != 404:
                    raise
            if not published:
                try:
                    status = bounded_read(
                        lambda: self.backend.upload_status(uri),
                        self.retry, self.sleep)
                except ArchiveRequestError as gone:
                    if gone.status == 404:
                        # The session is gone and no object landed:
                        # fence it and re-drive the same intent on a new
                        # session — the bytes are re-sent, never assumed.
                        self._fence_session(intent, entry,
                                            "session lost at completion")
                        uri = self._open_session(intent)
                        entry = intent["session"]
                        return self._drive(intent, uri, entry, data)
                    raise
                if status["offset"] == intent["byte_length"]:
                    bounded_read(lambda: self.backend.complete_upload(uri),
                                 self.retry, self.sleep)
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

    def _drive(self, intent, uri, entry, data):
        """Re-drive a fenced session's remaining bytes on the new session."""
        confirmed = self._confirmed(uri)
        max_retries = (self.retry or {}).get("max_retries", 0)
        chunk = self.upload.get("chunk_bytes", DEFAULT_UPLOAD["chunk_bytes"])
        failures = 0
        while confirmed < intent["byte_length"]:
            piece = data[confirmed:confirmed + chunk]
            try:
                self.backend.upload_chunk(uri, confirmed, piece)
                new_confirmed = self._confirmed(uri)
            except (ConnectionDropped, ArchiveRequestError) as e:
                if isinstance(e, ArchiveRequestError) and not e.retriable():
                    if e.status == 404:
                        self._fence_session(intent, entry,
                                            "session gone mid-upload")
                        uri = self._open_session(intent)
                        entry = intent["session"]
                        confirmed = self._confirmed(uri)
                        continue
                    raise
                new_confirmed = self._confirmed(uri)
            if new_confirmed <= confirmed:
                failures += 1
                if failures > max_retries:
                    raise FilmError("TRANSPORT_RETRY_EXHAUSTED: re-driven "
                                    "upload stalled")
            else:
                failures = 0
                self._j("UPLOAD_OFFSET", {
                    "intent_id": intent["intent_id"],
                    "session_ref": entry["ref"], "offset": new_confirmed})
            confirmed = new_confirmed
        return self._complete(intent, uri, entry, data)

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
                            "sessions": [{"ref": s["ref"], "seq": s["seq"],
                                          "confirmed": s["confirmed"],
                                          "fenced": s["fenced"]}
                                         for s in u["sessions"]]}
                for intent_id, u in self.uploads.items()}
