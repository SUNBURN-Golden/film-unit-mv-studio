"""HTTP transport for the opt-in Google Drive route (ANIM-018).

`UrllibTransport` talks only to the Google hosts the installed-app flow
and Drive v3 use, and it never follows a redirect. `MemoryDriveTransport`
is an offline double of those same calls for tests and `--fixture`. It is
not a Google connection: `real` is false, and a run that uses it cannot
be marked QUALIFIED.
"""
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import hashlib
import json
import uuid

from .core import FilmError
from .storage_backends import ConnectionDropped

ALLOWED_HOSTS = frozenset({
    "accounts.google.com",
    "oauth2.googleapis.com",
    "www.googleapis.com",
})


class HttpResponse:
    def __init__(self, status, headers=None, body=b""):
        self.status = int(status)
        self.headers = {str(k).lower(): v for k, v in dict(headers or {}).items()}
        if isinstance(body, str):
            body = body.encode()
        self.body = body or b""


def check_google_url(url):
    """Refuse any URL that is not https on the pinned Google hosts."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_HOSTS \
            or parsed.username or parsed.password:
        raise FilmError(
            "REDIRECT_REFUSED: Google calls stay on the pinned https hosts")
    return url


def redact_url(url):
    """A path safe to put in a report. Resumable session ids are secrets."""
    path = urlparse(url).path or "/"
    marker = "/resumable/"
    if marker in path:
        path = path.split(marker)[0] + marker + "[REDACTED]"
    return path


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """One attempt per call. Redirects are not followed. `real` is true."""

    real = True

    def __init__(self, timeout=60):
        self.timeout = timeout
        self.calls = []

    def request(self, method, url, *, headers=None, body=None, timeout=None):
        check_google_url(url)
        req = Request(url, data=body, headers=dict(headers or {}),
                      method=method)
        opener = build_opener(_NoRedirect())
        try:
            with opener.open(req, timeout=timeout or self.timeout) as resp:
                result = HttpResponse(resp.status, resp.headers, resp.read())
        except HTTPError as exc:
            raw = exc.read() if exc.fp is not None else b""
            # 308 is Drive's resumable "incomplete" status, not a redirect
            # we follow. Every other 3xx is refused.
            if 300 <= exc.code < 400 and exc.code != 308:
                raise FilmError(
                    "REDIRECT_REFUSED: a Google response redirect was not "
                    "followed") from exc
            result = HttpResponse(exc.code, exc.headers, raw)
        except (URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise ConnectionDropped(
                "Drive transfer dropped before a status") from exc
        self.calls.append({"method": method, "path": redact_url(url),
                           "status": result.status,
                           "request_bytes": len(body or b""),
                           "response_bytes": len(result.body)})
        return result


class MemoryDriveTransport:
    """Offline Drive v3 + token-endpoint double. `real` is false.

    Faults are consumed in order when the URL contains `match`. A dropped
    fault raises `ConnectionDropped` and does not change stored bytes.
    """

    real = False

    def __init__(self):
        self.files = {}          # file id -> record
        self.sessions = {}       # resumable id -> session
        self.calls = []
        self.faults = []
        self.permission_id = "perm-anim018"
        self.token_scope = "https://www.googleapis.com/auth/drive.file"
        self.ignore_range = False
        self._seq = 0
        self._refresh = None
        self.token_field_names = []
        self.next_location = None

    def push_fault(self, match, *, status=503, reason="unavailable",
                   drop=False, retry_after=None, times=1):
        for _ in range(times):
            self.faults.append({
                "match": match, "status": status, "reason": reason,
                "drop": drop, "retry_after": retry_after})

    def bump_revision(self, name):
        record = self._by_name(name)
        self._seq += 1
        record["revision"] = f"rev-{self._seq}"
        return record["revision"]

    def flip_byte(self, name):
        record = self._by_name(name)
        data = bytearray(record["data"])
        data[0] ^= 0xFF
        record["data"] = bytes(data)

    def _by_name(self, name):
        for record in self.files.values():
            if record["name"] == name and not record.get("folder"):
                return record
        raise FilmError(f"memory drive has no file named {name}")

    def _log(self, method, url, body, result):
        self.calls.append({
            "method": method, "path": redact_url(url),
            "status": result.status,
            "request_bytes": len(body or b""),
            "response_bytes": len(result.body)})

    def _take_fault(self, url):
        for index, fault in enumerate(self.faults):
            if fault["match"] in url:
                return self.faults.pop(index)
        return None

    def request(self, method, url, *, headers=None, body=None, timeout=None):
        check_google_url(url)
        fault = self._take_fault(url)
        if fault is not None:
            if fault["drop"]:
                self.calls.append({
                    "method": method, "path": redact_url(url),
                    "status": 0, "request_bytes": len(body or b""),
                    "response_bytes": 0})
                raise ConnectionDropped("memory drive dropped the transfer")
            payload = {"error": {"code": fault["status"], "errors": [
                {"reason": fault["reason"]}]}}
            headers_out = {}
            if fault["retry_after"] is not None:
                headers_out["retry-after"] = str(fault["retry_after"])
            result = HttpResponse(fault["status"], headers_out,
                                  json.dumps(payload).encode())
            self._log(method, url, body, result)
            return result
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host == "oauth2.googleapis.com":
            result = self._token(body or b"")
        else:
            result = self._drive(method, parsed, headers or {}, body or b"")
        self._log(method, url, body, result)
        return result

    def _token(self, body):
        fields = parse_qs(body.decode())
        self.token_field_names = sorted(fields)
        grant = (fields.get("grant_type") or [""])[0]
        if grant == "refresh_token":
            supplied = (fields.get("refresh_token") or [""])[0]
            if not self._refresh or supplied != self._refresh:
                return HttpResponse(400, {}, json.dumps(
                    {"error": "invalid_grant"}).encode())
            access = f"mem-access-{uuid.uuid4().hex}"
        else:
            verifier = (fields.get("code_verifier") or [""])[0]
            if len(verifier) < 43:
                return HttpResponse(400, {}, json.dumps(
                    {"error": "invalid_grant"}).encode())
            access = f"mem-access-{uuid.uuid4().hex}"
            self._refresh = f"mem-refresh-{uuid.uuid4().hex}"
        payload = {"access_token": access, "expires_in": 3600,
                   "scope": self.token_scope, "token_type": "Bearer"}
        if self._refresh:
            payload["refresh_token"] = self._refresh
        return HttpResponse(200, {}, json.dumps(payload).encode())

    def _authorized(self, headers):
        auth = headers.get("Authorization") or headers.get("authorization")
        if not auth or not auth.startswith("Bearer "):
            return HttpResponse(401, {}, json.dumps(
                {"error": {"code": 401, "errors": [
                    {"reason": "authError"}]}}).encode())
        return None

    def _drive(self, method, parsed, headers, body):
        denied = self._authorized(headers)
        if denied is not None:
            return denied
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/drive/v3/about":
            return HttpResponse(200, {}, json.dumps(
                {"user": {"permissionId": self.permission_id}}).encode())
        if path == "/upload/drive/v3/files":
            return self._start_session(body)
        if path.startswith("/upload/drive/v3/resumable/"):
            sid = path.rsplit("/", 1)[-1]
            return self._upload(sid, headers, body)
        if path == "/drive/v3/files" and method == "POST":
            return self._create_meta(body)
        if path == "/drive/v3/files" and method == "GET":
            return self._list(query)
        if path.startswith("/drive/v3/files/"):
            return self._file(method, path, query, headers)
        return HttpResponse(404, {}, json.dumps(
            {"error": {"code": 404, "errors": [
                {"reason": "notFound"}]}}).encode())

    def _create_meta(self, body):
        meta = json.loads(body.decode() or "{}")
        self._seq += 1
        file_id = f"gfolder-{self._seq}" if meta.get("mimeType", "").endswith(
            "folder") else f"gfile-{self._seq}"
        folder = meta.get("mimeType") == "application/vnd.google-apps.folder"
        self.files[file_id] = {
            "id": file_id, "name": meta.get("name"),
            "parents": list(meta.get("parents") or []),
            "folder": folder, "data": b"", "md5": None,
            "revision": f"rev-{self._seq}", "size": 0}
        return HttpResponse(200, {}, json.dumps(
            {"id": file_id, "name": meta.get("name")}).encode())

    def _start_session(self, body):
        meta = json.loads(body.decode() or "{}")
        sid = uuid.uuid4().hex
        length = None
        self.sessions[sid] = {"meta": meta, "buf": bytearray(),
                              "done": False, "file_id": None}
        if self.next_location is not None:
            location = self.next_location
            self.next_location = None
        else:
            location = f"https://www.googleapis.com/upload/drive/v3/resumable/{sid}"
        self.sessions[sid]["length_hint"] = length
        return HttpResponse(200, {"location": location}, b"")

    def _upload(self, sid, headers, body):
        state = self.sessions.get(sid)
        if state is None:
            return HttpResponse(404, {}, json.dumps(
                {"error": {"code": 404, "errors": [
                    {"reason": "notFound"}]}}).encode())
        content_range = None
        for key, value in headers.items():
            if key.lower() == "content-range":
                content_range = value
        if not content_range:
            return HttpResponse(400, {}, b"")
        # Status query: bytes */total
        if content_range.startswith("bytes */"):
            total = int(content_range.split("/", 1)[1])
            state["total"] = total
            if state["done"]:
                return self._file_json(state["file_id"], 200)
            # No Range header means the session has accepted zero bytes.
            if not state["buf"]:
                return HttpResponse(308, {}, b"")
            last = len(state["buf"]) - 1
            return HttpResponse(
                308, {"range": f"bytes=0-{last}"}, b"")
        # bytes start-end/total
        span, total_s = content_range.split("/", 1)
        start_s, end_s = span.split(" ")[1].split("-")
        start, end, total = int(start_s), int(end_s), int(total_s)
        if start != len(state["buf"]):
            return HttpResponse(409, {}, json.dumps(
                {"error": {"code": 409, "errors": [
                    {"reason": "offsetConflict"}]}}).encode())
        piece = body or b""
        if len(piece) != (end - start + 1):
            return HttpResponse(400, {}, b"")
        state["buf"].extend(piece)
        state["total"] = total
        if len(state["buf"]) < total:
            last = len(state["buf"]) - 1
            return HttpResponse(308, {"range": f"bytes=0-{last}"}, b"")
        if len(state["buf"]) > total:
            return HttpResponse(400, {}, b"")
        return self._finalize(state)

    def _finalize(self, state):
        data = bytes(state["buf"])
        meta = state["meta"]
        self._seq += 1
        file_id = f"gfile-{self._seq}"
        record = {"id": file_id, "name": meta.get("name"),
                  "parents": list(meta.get("parents") or []),
                  "folder": False, "data": data,
                  "md5": hashlib.md5(data).hexdigest(),
                  "revision": f"rev-{self._seq}", "size": len(data)}
        self.files[file_id] = record
        state["done"] = True
        state["file_id"] = file_id
        return self._file_json(file_id, 200)

    def _file_json(self, file_id, status):
        record = self.files[file_id]
        payload = {"id": record["id"], "name": record["name"],
                   "size": str(record["size"]),
                   "md5Checksum": record["md5"],
                   "headRevisionId": record["revision"]}
        return HttpResponse(status, {}, json.dumps(payload).encode())

    def _list(self, query):
        q = query.get("q", [""])[0] if isinstance(query, dict) else query
        page_size = 100
        start = 0
        if isinstance(query, dict):
            raw_size = (query.get("pageSize") or ["100"])[0]
            if str(raw_size).isdigit() and int(raw_size) > 0:
                page_size = int(raw_size)
            token = (query.get("pageToken") or [None])[0]
            if token and str(token).isdigit():
                start = int(token)
        matched = []
        for record in self.files.values():
            if record["folder"]:
                if "application/vnd.google-apps.folder" not in q:
                    continue
            elif "application/vnd.google-apps.folder" in q:
                continue
            if "trashed=false" in q and record.get("trashed"):
                continue
            name = _quoted(q, "name")
            if name is not None and record["name"] != name:
                continue
            parent = _parent(q)
            if parent is not None and parent not in record["parents"]:
                continue
            matched.append(self._public(record))
        matched.sort(key=lambda item: (item.get("name") or "", item.get("id") or ""))
        page = matched[start:start + page_size]
        payload = {"files": page}
        if start + page_size < len(matched):
            payload["nextPageToken"] = str(start + page_size)
        return HttpResponse(200, {}, json.dumps(payload).encode())

    def _public(self, record):
        payload = {"id": record["id"], "name": record["name"],
                   "mimeType": "application/vnd.google-apps.folder"
                   if record["folder"] else "application/octet-stream",
                   "headRevisionId": record["revision"],
                   "parents": list(record["parents"])}
        if not record["folder"]:
            payload["size"] = str(record["size"])
            payload["md5Checksum"] = record["md5"]
        return payload

    def _file(self, method, path, query, headers):
        parts = path.strip("/").split("/")
        # drive/v3/files/{id}[/revisions/{rev}]
        file_id = parts[3]
        record = self.files.get(file_id)
        if record is None:
            return HttpResponse(404, {}, json.dumps(
                {"error": {"code": 404, "errors": [
                    {"reason": "notFound"}]}}).encode())
        if method == "DELETE":
            self.files.pop(file_id, None)
            return HttpResponse(204, {}, b"")
        revision = None
        if len(parts) >= 6 and parts[4] == "revisions":
            revision = parts[5]
            if revision != record["revision"]:
                return HttpResponse(404, {}, json.dumps(
                    {"error": {"code": 404, "errors": [
                        {"reason": "notFound"}]}}).encode())
        if query.get("alt", [""])[0] == "media":
            return self._media(record, headers)
        return HttpResponse(200, {}, json.dumps(self._public(record)).encode())

    def _media(self, record, headers):
        data = record["data"]
        total = len(data)
        range_header = None
        for key, value in headers.items():
            if key.lower() == "range":
                range_header = value
        if range_header and not self.ignore_range:
            # bytes=start-end
            span = range_header.split("=", 1)[1]
            start_s, end_s = span.split("-")
            start, end = int(start_s), int(end_s)
            if start < 0 or end < start or start >= total:
                return HttpResponse(416, {"content-range": f"bytes */{total}"},
                                    b"")
            end = min(end, total - 1)
            piece = data[start:end + 1]
            return HttpResponse(
                206, {"content-range": f"bytes {start}-{end}/{total}"}, piece)
        return HttpResponse(200, {"content-length": str(total)}, data)


def _quoted(q, field):
    needle = f"{field}='"
    if needle not in q:
        return None
    rest = q.split(needle, 1)[1]
    return rest.split("'", 1)[0]


def _parent(q):
    needle = " in parents"
    if needle not in q:
        return None
    before = q.split(needle, 1)[0]
    if "'" not in before:
        return None
    return before.rsplit("'", 2)[-2]


def form_body(fields):
    return urlencode(fields).encode()
