"""Google Drive v3 archive backend (ANIM-018).

Opt-in. Without a token provider and an HTTP transport this raises
`GOOGLE_DRIVE_NOT_CONFIGURED` — that is the WAITING evidence state, not a
qualified connection. Drive's md5Checksum (and any unqualified sha256
field) is recorded and checked, but `provider_checksum` stays unset so a
seal still needs bounded FULL_READBACK. Upload session URIs stay in
memory and are never placed in errors.

Retries are not done here. Idempotent GET, Range and upload-status go
through `archive_manifest.bounded_read`; creating a session or a file is
one attempt. A redirect Location on another host is refused.
"""
from urllib.parse import quote
import hashlib
import json
import uuid

from ..core import FilmError
from ..google_transport import check_google_url
from . import (ArchiveRequestError, ConnectionDropped, RangeResult,
               check_object_id)

API = "https://www.googleapis.com"
FOLDER_NAME = "Film Unit Archive"
FOLDER_MIME = "application/vnd.google-apps.folder"


def _md5(data):
    return hashlib.md5(data).hexdigest()


class GoogleDriveBackend:
    name = "GOOGLE_DRIVE"
    # Drive's checksum is not a qualified object SHA-256. Seal uses readback.
    provider_checksum = None
    range_supported = True

    def __init__(self, token_provider=None, *, transport=None,
                 token_updater=None, now_fn=None):
        if not callable(token_provider) or transport is None:
            raise FilmError(
                "GOOGLE_DRIVE_NOT_CONFIGURED: a Drive session needs a token "
                "provider and an HTTP transport. No client file leaves "
                "evidence state WAITING; this backend is not qualified")
        self._provider = token_provider
        self.transport = transport
        self._updater = token_updater
        self._now = now_fn
        self._folder = None
        self._ids = {}
        self._pins = {}
        self._uploads = {}
        self.secret_uris = []

    def secret_material(self):
        """Upload URIs held in memory. Callers scan evidence against these
        and must not write them out."""
        return list(self.secret_uris)

    # -- HTTP -----------------------------------------------------------------
    def _headers(self, extra=None):
        token = self._provider()
        access = (token or {}).get("access_token")
        if not access:
            raise FilmError(
                "GOOGLE_DRIVE_NOT_CONFIGURED: the session has no access token")
        headers = {"Authorization": f"Bearer {access}"}
        if extra:
            headers.update(extra)
        return headers

    def _call(self, method, url, *, headers=None, body=None):
        check_google_url(url)
        try:
            response = self.transport.request(
                method, url, headers=self._headers(headers), body=body)
        except ConnectionDropped:
            raise
        if 300 <= response.status < 400 and response.status != 308:
            raise FilmError(
                "REDIRECT_REFUSED: a Drive response redirect was not followed")
        if response.status >= 400:
            self._fail(response)
        return response

    def _fail(self, response):
        reason = "error"
        try:
            payload = json.loads(response.body.decode() or "{}")
        except json.JSONDecodeError:
            payload = {}
        err = payload.get("error")
        if type(err) is dict:
            errors = err.get("errors") or []
            if errors and type(errors[0]) is dict and errors[0].get("reason"):
                reason = errors[0]["reason"]
        elif type(err) is str:
            reason = err
        mapped = reason
        if response.status == 401:
            mapped = "unauthorized"
        elif response.status == 403:
            if reason in {"rateLimitExceeded", "userRateLimitExceeded",
                          "rate_limit"}:
                mapped = "rate_limit"
            else:
                mapped = "permission_denied"
        elif response.status == 404:
            mapped = "not_found"
        elif response.status == 409:
            mapped = "conflict"
        elif response.status == 416:
            mapped = "range_not_satisfiable"
        elif response.status == 429:
            mapped = "rate_limit"
        elif response.status in {408, 500, 502, 503, 504}:
            mapped = "unavailable"
        retry_after = None
        header = response.headers.get("retry-after")
        if header is not None and str(header).isdigit():
            retry_after = int(header) * 1000
        raise ArchiveRequestError(
            response.status, mapped,
            f"Drive request failed with HTTP {response.status}",
            retry_after_ms=retry_after)

    def _json(self, response):
        if not response.body:
            return {}
        return json.loads(response.body.decode())

    def _list(self, q):
        files = []
        page_token = None
        seen = set()
        while True:
            url = (f"{API}/drive/v3/files?q={quote(q)}&pageSize=100&fields="
                   "nextPageToken,files(id,name,mimeType,size,md5Checksum,"
                   "sha256Checksum,headRevisionId,parents)")
            if page_token:
                if page_token in seen:
                    break
                seen.add(page_token)
                url += "&pageToken=" + quote(page_token)
            payload = self._json(self._call("GET", url))
            files.extend(payload.get("files") or [])
            page_token = payload.get("nextPageToken") or None
            if not page_token:
                break
        return files

    def _folder_id(self):
        if self._folder:
            return self._folder
        q = (f"mimeType='{FOLDER_MIME}' and name='{FOLDER_NAME}' "
             "and trashed=false")
        found = self._list(q)
        if found:
            self._folder = found[0]["id"]
            return self._folder
        response = self._call(
            "POST", f"{API}/drive/v3/files",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"name": FOLDER_NAME,
                             "mimeType": FOLDER_MIME}).encode())
        self._folder = self._json(response)["id"]
        return self._folder

    def _lookup(self, object_id):
        check_object_id(object_id)
        if object_id in self._ids:
            return self._ids[object_id]
        q = (f"name='{object_id}' and '{self._folder_id()}' in parents "
             "and trashed=false")
        found = [item for item in self._list(q)
                 if item.get("name") == object_id
                 and not str(item.get("mimeType", "")).endswith("folder")]
        if not found:
            return None
        if len(found) != 1:
            raise FilmError(
                f"Drive returned more than one file named {object_id}")
        self._ids[object_id] = found[0]["id"]
        return found[0]["id"]

    def _meta(self, file_id):
        url = (f"{API}/drive/v3/files/{file_id}?fields="
               "id,name,size,md5Checksum,sha256Checksum,headRevisionId")
        return self._json(self._call("GET", url))

    def _require_pin(self, object_id, meta):
        pin = self._pins.get(object_id)
        head = meta.get("headRevisionId")
        if pin is not None and head != pin:
            raise ArchiveRequestError(
                409, "input_mismatch",
                f"Drive revision for {object_id} moved off the pinned "
                "revision")
        return head

    def _remember(self, object_id, payload, *, reused):
        file_id = payload.get("id")
        if file_id:
            self._ids[object_id] = file_id
        revision = payload.get("headRevisionId")
        if revision:
            self._pins[object_id] = revision
        return {"object_id": object_id, "reused": reused, "revision": revision}

    # -- reads ----------------------------------------------------------------
    def object_info(self, object_id):
        check_object_id(object_id)
        file_id = self._lookup(object_id)
        if file_id is None:
            raise ArchiveRequestError(404, "not_found",
                                      f"Drive has no object {object_id}")
        meta = self._meta(file_id)
        self._require_pin(object_id, meta)
        info = {"byte_length": int(meta.get("size") or 0),
                "provider_checksum": None,
                "md5": meta.get("md5Checksum"),
                "revision": meta.get("headRevisionId"),
                # Present only when Drive sent one. Not a qualified SHA-256.
                "drive_sha256": meta.get("sha256Checksum")}
        return info

    def get_object(self, object_id):
        check_object_id(object_id)
        file_id = self._lookup(object_id)
        if file_id is None:
            raise ArchiveRequestError(404, "not_found",
                                      f"Drive has no object {object_id}")
        meta = self._meta(file_id)
        self._require_pin(object_id, meta)
        pin = self._pins.get(object_id)
        if pin:
            url = (f"{API}/drive/v3/files/{file_id}/revisions/{pin}?alt=media")
        else:
            url = f"{API}/drive/v3/files/{file_id}?alt=media"
        response = self._call("GET", url)
        body = response.body
        expected = meta.get("md5Checksum")
        if expected and _md5(body) != expected:
            raise FilmError(
                f"INTEGRITY_FAILED: Drive md5 for {object_id} does not "
                "match the downloaded bytes")
        return RangeResult(200, body, total_length=len(body))

    def get_range(self, object_id, offset, length):
        check_object_id(object_id)
        if offset < 0 or length < 1:
            raise ArchiveRequestError(416, "range_not_satisfiable",
                                      "Range is empty or negative")
        file_id = self._lookup(object_id)
        if file_id is None:
            raise ArchiveRequestError(404, "not_found",
                                      f"Drive has no object {object_id}")
        meta = self._meta(file_id)
        self._require_pin(object_id, meta)
        pin = self._pins.get(object_id)
        if pin:
            url = (f"{API}/drive/v3/files/{file_id}/revisions/{pin}?alt=media")
        else:
            url = f"{API}/drive/v3/files/{file_id}?alt=media"
        end = offset + length - 1
        response = self._call(
            "GET", url, headers={"Range": f"bytes={offset}-{end}"})
        if response.status == 200:
            total = response.headers.get("content-length")
            return RangeResult(200, response.body,
                               total_length=int(total) if total else len(response.body))
        if response.status != 206:
            self._fail(response)
        start, total = _content_range(response.headers.get("content-range"))
        return RangeResult(206, response.body, range_start=start,
                           total_length=total)

    def list_objects(self):
        q = f"'{self._folder_id()}' in parents and trashed=false"
        names = []
        for item in self._list(q):
            if str(item.get("mimeType", "")).endswith("folder"):
                continue
            name = item.get("name")
            try:
                check_object_id(name)
            except FilmError:
                continue
            self._ids[name] = item["id"]
            names.append(name)
        return sorted(names)

    # -- writes ---------------------------------------------------------------
    def put_object(self, object_id, data):
        check_object_id(object_id)
        if type(data) is not bytes:
            raise FilmError("Drive objects are stored as bytes")
        file_id = self._lookup(object_id)
        if file_id is not None:
            meta = self._meta(file_id)
            if int(meta.get("size") or -1) == len(data) \
                    and meta.get("md5Checksum") == _md5(data):
                return self._remember(object_id, meta, reused=True)
            raise FilmError(
                f"Archive object {object_id} already holds different bytes")
        session = self.create_upload_session(object_id, len(data))
        if len(data):
            self.upload_chunk(session, 0, data)
        else:
            raise FilmError("Drive upload of an empty object is refused")
        return self.complete_upload(session)

    def create_upload_session(self, object_id, expected_length):
        check_object_id(object_id)
        if type(expected_length) is not int or expected_length < 1:
            raise FilmError("Upload length must be a positive integer")
        meta = {"name": object_id, "parents": [self._folder_id()]}
        response = self._call(
            "POST", f"{API}/upload/drive/v3/files?uploadType=resumable",
            headers={"Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Type": "application/octet-stream",
                     "X-Upload-Content-Length": str(expected_length)},
            body=json.dumps(meta).encode())
        location = response.headers.get("location")
        if not location:
            raise FilmError(
                "Drive resumable session did not return a location")
        try:
            check_google_url(location)
        except FilmError:
            raise FilmError(
                "REDIRECT_REFUSED: the upload session location is not on "
                "a pinned Google host") from None
        self.secret_uris.append(location)
        session = f"up-{uuid.uuid4().hex[:12]}"
        self._uploads[session] = {"uri": location, "object_id": object_id,
                                  "expected": expected_length, "done": False,
                                  "result": None}
        return session

    def upload_chunk(self, session, offset, data):
        state = self._session(session)
        if state["done"]:
            raise FilmError("Upload session is already complete")
        if type(data) is not bytes or not data:
            raise FilmError("Upload chunk must be non-empty bytes")
        end = offset + len(data) - 1
        if end >= state["expected"] or offset < 0:
            raise ArchiveRequestError(413, "too_large",
                                      "Chunk exceeds the declared length")
        response = self.transport.request(
            "PUT", state["uri"],
            headers=self._headers({
                "Content-Range": f"bytes {offset}-{end}/{state['expected']}",
                "Content-Type": "application/octet-stream"}),
            body=data)
        if response.status in (200, 201):
            state["done"] = True
            state["result"] = self._remember(
                state["object_id"], self._json(response), reused=False)
            return {"received": state["expected"]}
        if response.status == 308:
            return {"received": _resume_offset(response.headers.get("range"),
                                               empty_ok=False)}
        if 300 <= response.status < 400:
            raise FilmError(
                "REDIRECT_REFUSED: an upload redirect was not followed")
        self._fail(response)

    def upload_status(self, session):
        state = self._session(session)
        if state["done"]:
            return {"offset": state["expected"],
                    "expected_length": state["expected"]}
        response = self.transport.request(
            "PUT", state["uri"],
            headers=self._headers({
                "Content-Range": f"bytes */{state['expected']}",
                "Content-Length": "0"}),
            body=b"")
        if response.status in (200, 201):
            state["done"] = True
            state["result"] = self._remember(
                state["object_id"], self._json(response), reused=False)
            return {"offset": state["expected"],
                    "expected_length": state["expected"]}
        if response.status == 308:
            return {"offset": _resume_offset(response.headers.get("range"),
                                             empty_ok=True),
                    "expected_length": state["expected"]}
        if 300 <= response.status < 400:
            raise FilmError(
                "REDIRECT_REFUSED: an upload-status redirect was not followed")
        self._fail(response)

    def complete_upload(self, session):
        state = self._session(session)
        if not state["done"]:
            status = self.upload_status(session)
            if status["offset"] != state["expected"] or not state["done"]:
                raise FilmError("Upload incomplete at session close")
        result = state["result"]
        if result is None:
            raise FilmError("Upload incomplete at session close")
        return result

    def delete_object(self, object_id):
        check_object_id(object_id)
        file_id = self._lookup(object_id)
        if file_id is None:
            raise ArchiveRequestError(404, "not_found",
                                      f"Drive has no object {object_id}")
        self._call("DELETE", f"{API}/drive/v3/files/{file_id}")
        self._ids.pop(object_id, None)
        self._pins.pop(object_id, None)
        return {"object_id": object_id, "deleted": True}

    def _session(self, session):
        state = self._uploads.get(session)
        if state is None:
            raise ArchiveRequestError(404, "not_found",
                                      "Unknown upload session")
        return state


def _content_range(header):
    """Parse `bytes start-end/total`. A missing or malformed header is a
    mismatch, not a successful partial read."""
    if not header or not header.startswith("bytes ") or "/" not in header:
        raise ArchiveRequestError(416, "range_not_satisfiable",
                                  "Drive Content-Range is missing")
    span, total_s = header.split("/", 1)
    try:
        start_s, end_s = span.split(" ")[1].split("-")
        start, total = int(start_s), int(total_s)
        int(end_s)
    except (IndexError, ValueError) as exc:
        raise ArchiveRequestError(416, "range_not_satisfiable",
                                  "Drive Content-Range is malformed") from exc
    return start, total


def _resume_offset(header, *, empty_ok):
    """Server-confirmed next offset. No Range header means zero accepted
    bytes, and only the status query may see that."""
    if not header:
        if empty_ok:
            return 0
        raise ArchiveRequestError(409, "conflict",
                                  "Drive chunk response omitted Range")
    try:
        last = int(header.split("-")[-1])
    except ValueError as exc:
        raise ArchiveRequestError(409, "conflict",
                                  "Drive Range header is malformed") from exc
    return last + 1
