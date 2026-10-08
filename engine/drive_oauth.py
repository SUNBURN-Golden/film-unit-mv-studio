"""Browser Google-login contract for the Drive archive (ANIM-013, ANIM-018).

Execution/storage §3 and schema §17: the user connects through the system
browser, PKCE and state; the minimum scope is `drive.file` — files the app
creates or the user explicitly selects, never whole-Drive access.
`FakeOAuthFlow` simulates the consent screen for protocol tests.

`GoogleOAuthFlow` is the opt-in installed-app flow. The Desktop client JSON
is read only from `FILM_GOOGLE_OAUTH_CLIENT_FILE` (a path outside the repo).
No client file is `GOOGLE_OAUTH_NOT_CONFIGURED` and the evidence state stays
WAITING. A completed fake or fixture exchange is not qualification.

Tokens live only in a supported OS credential store or an explicit
memory-limited session — never in a settings file, project, build, packet
or log. The only persisted fields are the non-secret `connection_id`,
`account_binding_digest` and `credential_epoch` plus scope names. Logout,
permission revocation and account switch raise the epoch and block every
subsequent read, write and upload resume.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
import webbrowser

from pathlib import Path

from .core import FilmError, atomic_text, read
from .google_transport import (UrllibTransport, check_google_url, form_body)
from .storage_backends import ArchiveRequestError

CLIENT_FILE_ENV = "FILM_GOOGLE_OAUTH_CLIENT_FILE"
CLIENT_DEPLOY_OWNER = "JunTae (installed-app secret not trusted as secret)"
AUTH_HOST = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_HOST = "https://oauth2.googleapis.com/token"

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
SECRET_FIELDS = {"token", "access_token", "refresh_token", "id_token",
                 "authorization_code", "client_secret", "session_cookie",
                 "bearer", "upload_uri"}


class AuthError(FilmError):
    """An authorization boundary event; `code` is the machine-readable kind."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# -- OAuth flows ------------------------------------------------------------

class FakeOAuthFlow:
    """Simulates the browser consent screen — never a real Google call.

    `deny=True` models the user refusing consent; `expires_in` models token
    lifetime; `granted_object_ids` is the explicit file selection the fake
    account authorized (None = an empty selection: drive.file never grants
    pre-existing objects the user did not explicitly pick).
    """

    def __init__(self, account="account-a", granted_object_ids=None,
                 deny=False, expires_in=3600.0, now_fn=None):
        self.account = account
        self.granted_object_ids = granted_object_ids
        self.deny = deny
        self.expires_in = expires_in
        self._now = now_fn or time.time

    def authorize(self):
        if self.deny:
            raise AuthError("AUTH_DENIED",
                            "The user declined the Google consent screen")
        granted = sorted(self.granted_object_ids or [])
        return {"account_id": self.account,
                "token": {"access_token": f"fake-access-{uuid.uuid4().hex}",
                          "refresh_token": f"fake-refresh-{uuid.uuid4().hex}",
                          "expires_at": self._now() + self.expires_in,
                          "scope": DRIVE_FILE_SCOPE},
                "granted_object_ids": granted}


def pkce_challenge(verifier):
    """S256 code challenge: base64url(SHA256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _pkce_pair():
    # token_urlsafe(32) is 43 characters, inside the 43..128 PKCE range.
    verifier = secrets.token_urlsafe(32)
    return verifier, pkce_challenge(verifier)


def load_desktop_client(path):
    """Read a Google Desktop-app client JSON. The secret stays in memory."""
    file = Path(path)
    if not file.is_file():
        raise FilmError(
            "GOOGLE_OAUTH_NOT_CONFIGURED: FILM_GOOGLE_OAUTH_CLIENT_FILE "
            "does not point at a Desktop app client JSON. Evidence state "
            "is WAITING")
    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FilmError(
            "GOOGLE_OAUTH_NOT_CONFIGURED: the client file could not be "
            "read as JSON") from exc
    if "web" in document and "installed" not in document:
        raise FilmError(
            "GOOGLE_OAUTH_NOT_CONFIGURED: a web client is refused; use a "
            "Desktop app client")
    installed = document.get("installed")
    if type(installed) is not dict:
        raise FilmError(
            "GOOGLE_OAUTH_NOT_CONFIGURED: the client file has no installed "
            "Desktop app section")
    for field in ("client_id", "client_secret", "auth_uri", "token_uri"):
        if not installed.get(field) or type(installed[field]) is not str:
            raise FilmError(
                "GOOGLE_OAUTH_NOT_CONFIGURED: the Desktop client file is "
                f"missing {field}")
    check_google_url(installed["auth_uri"])
    check_google_url(installed["token_uri"])
    if urlparse(installed["auth_uri"]).hostname != "accounts.google.com":
        raise FilmError(
            "REDIRECT_REFUSED: the auth URI must be accounts.google.com")
    if urlparse(installed["token_uri"]).hostname != "oauth2.googleapis.com":
        raise FilmError(
            "REDIRECT_REFUSED: the token URI must be oauth2.googleapis.com")
    return {"client_id": installed["client_id"],
            "client_secret": installed["client_secret"],
            "auth_uri": installed["auth_uri"],
            "token_uri": installed["token_uri"]}


def client_file_path(explicit=None):
    """The only configuration source: an explicit path or the env file."""
    return explicit or os.environ.get(CLIENT_FILE_ENV) or None


class _LoopHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        # Keep the first OAuth redirect. A later request (favicon) must not
        # replace the code before wait() reads it.
        if any(query.get(key) for key in ("code", "error", "state")):
            with self.server.lock:
                if not self.server.event.is_set():
                    self.server.result = dict(query)
                    self.server.event.set()
        body = (b"<!doctype html><meta charset=utf-8>"
                b"<p>Film Unit received the Google redirect. "
                b"You can close this tab.</p>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # The redirect carries the authorization code. Never log it.
        return


class LocalLoopback:
    """Installed-app redirect listener on 127.0.0.1, one request."""

    def __init__(self):
        self.port = None
        self._httpd = None
        self._thread = None

    def __enter__(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _LoopHandler)
        httpd.event = threading.Event()
        httpd.lock = threading.Lock()
        httpd.result = {}
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def redirect_uri(self):
        return f"http://127.0.0.1:{self.port}/"

    def wait(self, timeout=180):
        if not self._httpd.event.wait(timeout):
            raise AuthError("AUTH_TIMEOUT",
                            "The Google redirect did not arrive on the "
                            "loopback listener")
        return dict(self._httpd.result)

    def close(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __exit__(self, *exc):
        self.close()


class ScriptedLoopback:
    """Test double: returns a query without binding a port or a browser."""

    def __init__(self, query, port=9):
        self._query = query
        self.port = port

    def redirect_uri(self):
        return f"http://127.0.0.1:{self.port}/"

    def wait(self, timeout=None):
        query = self._query() if callable(self._query) else self._query
        return dict(query)

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class GoogleOAuthFlow:
    """Installed-app loopback flow: PKCE S256, state, drive.file only.

    Configuration comes from `FILM_GOOGLE_OAUTH_CLIENT_FILE` or an explicit
    `client_file` path (tests point this at a temp file). An in-memory
    client dict is not a configuration source. Nothing here is QUALIFIED
    until an attended run completes on `UrllibTransport`.
    """

    def __init__(self, client_config=None, *, client_file=None,
                 transport=None, browser=None, loopback=None, now_fn=None):
        if client_config:
            raise FilmError(
                "GOOGLE_OAUTH_NOT_CONFIGURED: client configuration is read "
                "only from FILM_GOOGLE_OAUTH_CLIENT_FILE, a Desktop app "
                "JSON outside the repo. An in-memory client id is not a "
                "connection. Evidence state is WAITING")
        path = client_file_path(client_file)
        if not path:
            raise FilmError(
                "GOOGLE_OAUTH_NOT_CONFIGURED: set "
                "FILM_GOOGLE_OAUTH_CLIENT_FILE to a Desktop app client "
                "JSON outside the repo. Evidence state is WAITING; this "
                "is not a qualified Drive connection")
        self._client = load_desktop_client(path)
        self.transport = transport or UrllibTransport()
        self._browser = browser or webbrowser.open
        self._loopback = loopback
        self._now = now_fn or time.time
        self.completed_on_real_google = False

    def __repr__(self):
        return "GoogleOAuthFlow(configured=True, scope=drive.file)"

    def _exchange(self, redirect_uri, code, verifier):
        body = form_body({
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": self._client["client_id"],
            "client_secret": self._client["client_secret"],
            "redirect_uri": redirect_uri})
        response = self.transport.request(
            "POST", self._client["token_uri"],
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body)
        if 300 <= response.status < 400:
            raise FilmError(
                "REDIRECT_REFUSED: the token endpoint redirect was not "
                "followed")
        if response.status != 200:
            raise AuthError(
                "AUTH_DENIED",
                "The token endpoint refused the authorization code")
        try:
            payload = json.loads(response.body.decode())
        except json.JSONDecodeError as exc:
            raise AuthError("AUTH_DENIED",
                            "The token endpoint did not return JSON") from exc
        scope = payload.get("scope") or ""
        if DRIVE_FILE_SCOPE not in scope.split():
            raise AuthError(
                "SCOPE_MISMATCH",
                "The granted scope is not drive.file; the token was discarded")
        access = payload.get("access_token")
        if not access:
            raise AuthError("AUTH_DENIED",
                            "The token endpoint returned no access token")
        expires_in = int(payload.get("expires_in") or 0)
        return {"access_token": access,
                "refresh_token": payload.get("refresh_token"),
                "expires_at": self._now() + expires_in,
                "scope": DRIVE_FILE_SCOPE}

    def refresh(self, token):
        """Exchange a refresh token. One attempt; not an archive retry."""
        refresh = (token or {}).get("refresh_token")
        if not refresh:
            raise AuthError("AUTH_EXPIRED",
                            "The access token expired and there is no "
                            "refresh token")
        body = form_body({
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": self._client["client_id"],
            "client_secret": self._client["client_secret"]})
        response = self.transport.request(
            "POST", self._client["token_uri"],
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body)
        if response.status == 400:
            raise AuthError("PERMISSION_REVOKED",
                            "The refresh token was rejected")
        if response.status != 200:
            raise AuthError("AUTH_EXPIRED",
                            "The access token could not be refreshed")
        payload = json.loads(response.body.decode())
        access = payload.get("access_token")
        if not access:
            raise AuthError("AUTH_EXPIRED",
                            "The refresh response had no access token")
        expires_in = int(payload.get("expires_in") or 0)
        updated = dict(token)
        updated["access_token"] = access
        updated["expires_at"] = self._now() + expires_in
        if payload.get("refresh_token"):
            updated["refresh_token"] = payload["refresh_token"]
        updated["scope"] = DRIVE_FILE_SCOPE
        return updated

    def _account_id(self, access_token):
        response = self.transport.request(
            "GET", "https://www.googleapis.com/drive/v3/about"
                   "?fields=user/permissionId",
            headers={"Authorization": f"Bearer {access_token}"})
        if response.status != 200:
            raise AuthError("AUTH_DENIED",
                            "Drive did not return an account binding")
        payload = json.loads(response.body.decode())
        account = ((payload.get("user") or {}).get("permissionId"))
        if not account:
            raise AuthError("AUTH_DENIED",
                            "Drive did not return a permission id")
        return str(account)

    def authorize(self):
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        loop = self._loopback or LocalLoopback()
        owns_loop = self._loopback is None
        try:
            if owns_loop:
                loop.__enter__()
            redirect_uri = loop.redirect_uri()
            query = urlencode({
                "client_id": self._client["client_id"],
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": DRIVE_FILE_SCOPE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "access_type": "offline",
                "prompt": "consent"})
            auth_url = f"{self._client['auth_uri']}?{query}"
            # The browser URL carries the challenge and state, never the
            # verifier or the client secret.
            self._browser(auth_url)
            returned = loop.wait(180)
        finally:
            if owns_loop:
                loop.close()
        if returned.get("error"):
            raise AuthError("AUTH_DENIED",
                            "The user declined the Google consent screen")
        if returned.get("state") != state:
            raise AuthError("STATE_MISMATCH",
                            "The redirect state did not match this attempt")
        code = returned.get("code")
        if not code:
            raise AuthError("AUTH_DENIED",
                            "The redirect did not include an authorization code")
        token = self._exchange(redirect_uri, code, verifier)
        account = self._account_id(token["access_token"])
        self.completed_on_real_google = isinstance(
            self.transport, UrllibTransport)
        return {"account_id": account, "token": token,
                "granted_object_ids": []}


# -- token stores -------------------------------------------------------------

class MemoryTokenStore:
    """Explicit memory-limited session store; tokens die with the process."""
    kind = "MEMORY_SESSION"

    def __init__(self):
        self._tokens = {}

    def store(self, connection_id, token):
        self._tokens[connection_id] = dict(token)

    def load(self, connection_id):
        return self._tokens.get(connection_id)

    def revoke(self, connection_id):
        self._tokens.pop(connection_id, None)

    def revoke_all(self):
        self._tokens.clear()


class OsCredentialStore:
    """Supported OS credential store via `keyring`, when the platform has one.

    `keyring` is not a declared dependency: where it is absent the store is
    simply unavailable, and the caller must either block the connection or
    fall back to an explicitly disclosed memory-limited session — never to
    a plaintext settings file.
    """
    kind = "OS_CREDENTIAL_STORE"

    def __init__(self, service="film-unit-drive"):
        try:
            import keyring
            self._keyring = keyring
        except ImportError:
            self._keyring = None
        self._service = service

    @property
    def available(self):
        return self._keyring is not None

    def _need(self):
        if not self.available:
            raise FilmError("CREDENTIAL_STORE_UNAVAILABLE: this OS has no "
                            "supported credential store; use an explicit "
                            "memory-limited session or do not connect")
        return self._keyring

    def store(self, connection_id, token):
        self._need().set_password(self._service, connection_id,
                                  json.dumps(token))

    def load(self, connection_id):
        raw = self._need().get_password(self._service, connection_id)
        return json.loads(raw) if raw else None

    def revoke(self, connection_id):
        if self.available:
            try:
                self._keyring.delete_password(self._service, connection_id)
            except Exception:
                pass


def plaintext_token_store(*_args, **_kwargs):
    """Blocked: OAuth tokens are never written to settings files."""
    raise FilmError("TOKEN_STORE_BLOCKED: an OAuth token in a mode-0600 "
                    "settings file is a forbidden plaintext fallback; "
                    "tokens live in the OS credential store or an explicit "
                    "memory-limited session")


# -- session ------------------------------------------------------------------

def _account_binding(account_id, granted_object_ids):
    """Non-secret local identifier of the verified account + allowed set.

    It is a digest of the account handle and the granted object list — not
    the email, not a token, and never sent to a worker.
    """
    granted = "" if granted_object_ids is None \
        else ",".join(sorted(granted_object_ids))
    material = f"film-unit-drive\0{account_id}\0{granted}".encode()
    return hashlib.sha256(material).hexdigest()


class _AuthorizedBackend:
    """Every archive call passes the session's authz + grant boundary first."""

    def __init__(self, session):
        self._session = session

    @property
    def name(self):
        return self._session.backend.name

    @property
    def range_supported(self):
        # Capability flags pass through so the reader's cap gate can run
        # before a whole-body response is ever requested.
        return getattr(self._session.backend, "range_supported", True)

    def _allowed(self, object_id):
        self._session._check()
        if object_id is not None and not self._session._accessible(object_id):
            raise ArchiveRequestError(
                403, "permission_denied",
                f"Object {object_id} is outside this connection's granted "
                "set; folder selection does not widen drive.file scope")
        return object_id

    def get_object(self, object_id):
        return self._session.backend.get_object(self._allowed(object_id))

    def get_range(self, object_id, offset, length):
        return self._session.backend.get_range(
            self._allowed(object_id), offset, length)

    def object_info(self, object_id):
        return self._session.backend.object_info(self._allowed(object_id))

    def list_objects(self):
        self._session._check()
        ids = self._session.backend.list_objects()
        # drive.file scope: only the explicit grant plus objects this app
        # created in this credential epoch are visible — listing must not
        # widen the scope to the whole store.
        visible = set(self._session.granted_object_ids or ()) \
            | self._session._owned
        return [i for i in ids if i in visible]

    def put_object(self, object_id, data):
        self._session._check()
        result = self._session.backend.put_object(object_id, data)
        if result.get("reused"):
            # The idempotent path means the object already existed; that is
            # only acceptable when it was already inside the grant. It is
            # never adopted into the app-owned set.
            if not self._session._accessible(object_id):
                raise ArchiveRequestError(
                    403, "permission_denied",
                    f"Object {object_id} already exists outside this "
                    "connection's granted set")
            return result
        self._session._owned.add(object_id)
        return result

    def create_upload_session(self, object_id, expected_length):
        self._session._check()
        session_id = self._session.backend.create_upload_session(
            object_id, expected_length)
        self._session._sessions[session_id] = object_id
        return session_id

    def upload_chunk(self, session_id, offset, data):
        self._session._check()
        return self._session.backend.upload_chunk(session_id, offset, data)

    def upload_status(self, session_id):
        self._session._check()
        return self._session.backend.upload_status(session_id)

    def complete_upload(self, session_id):
        self._session._check()
        object_id = self._session._sessions.pop(session_id)
        result = self._session.backend.complete_upload(session_id)
        if result.get("reused"):
            # Same rule as put_object: a pre-existing object outside the
            # grant is refused, and only objects this app actually created
            # become owned.
            if not self._session._accessible(object_id):
                raise ArchiveRequestError(
                    403, "permission_denied",
                    f"Object {object_id} already exists outside this "
                    "connection's granted set")
            return result
        self._session._owned.add(object_id)
        return result


class DriveSession:
    """An authorized Drive connection: credential epoch + token boundary."""

    def __init__(self, connection_id, account_id, granted_object_ids,
                 token_store, backend, now_fn=None, provider="FAKE_DRIVE"):
        self.connection_id = connection_id
        self.account_binding_digest = _account_binding(
            account_id, granted_object_ids)
        self.granted_object_ids = granted_object_ids
        self.credential_epoch = 1
        self.token_store = token_store
        self.backend = backend
        self.provider = provider
        self._now = now_fn or time.time
        self._owned = set()          # objects this app created via this session
        self._sessions = {}          # upload session -> object_id
        self._revoked_reason = None
        self.authorized = _AuthorizedBackend(self)

    def _check(self):
        """Every read, write, submit and upload resume passes this gate."""
        if self._revoked_reason is not None:
            raise AuthError(self._revoked_reason,
                            f"Connection is closed: {self._revoked_reason}")
        token = self.token_store.load(self.connection_id)
        if token is None:
            self._revoked_reason = "AUTH_REVOKED"
            raise AuthError("AUTH_REVOKED", "The connection token is gone")
        if self._now() >= token.get("expires_at", 0):
            refresher = getattr(self, "refresher", None)
            if refresher is None:
                raise AuthError("AUTH_EXPIRED",
                                "The connection token expired")
            refresher()
            token = self.token_store.load(self.connection_id)
            if token is None or self._now() >= token.get("expires_at", 0):
                raise AuthError("AUTH_EXPIRED",
                                "The connection token expired")

    def _accessible(self, object_id):
        """drive.file scope: app-created objects + the user's explicit grant.

        An omitted grant is an empty explicit selection, never allow-all:
        a pre-existing object is reachable only when the user explicitly
        selected it — choosing a folder does not widen the grant.
        """
        granted = self.granted_object_ids or ()
        return object_id in self._owned or object_id in granted

    def covers(self, object_id):
        """Public grant probe: picker UIs check whether an explicit file
        selection (a new consent) is needed before a read or restore."""
        return self._accessible(object_id)

    def _close(self, reason):
        self.token_store.revoke(self.connection_id)
        self.credential_epoch += 1     # epoch bump blocks new work on old epoch
        self._revoked_reason = reason

    def logout(self):
        """User signs out: token + job links dropped, later access blocked."""
        self._close("AUTH_REVOKED")

    def revoke_permissions(self):
        """The user withdrew Drive permission in their Google account."""
        self._close("PERMISSION_REVOKED")

    def switch_account(self):
        """A different account was picked: the old binding is closed."""
        self._close("ACCOUNT_CHANGED")

    def metadata(self):
        """Only non-secret grant metadata may leave this object."""
        return {"connection_id": self.connection_id,
                "account_binding_digest": self.account_binding_digest,
                "credential_epoch": self.credential_epoch,
                "provider": self.provider,
                "scopes": [DRIVE_FILE_SCOPE],
                "token_store": self.token_store.kind}

    def save_metadata(self, home):
        """Persist the non-secret connection record; nothing else is stored."""
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        atomic_text(home / "drive_connection.json",
                    json.dumps(self.metadata(), indent=2,
                               ensure_ascii=False) + "\n")


def connect_drive(flow, token_store, backend, now_fn=None, home=None):
    """Run the consent flow and open an authorized session.

    `flow.authorize()` raises `AUTH_DENIED` when the user refuses. When the
    store is the OS credential store and the platform has none, the caller
    must pass an explicit `MemoryTokenStore` — the plaintext settings
    fallback does not exist.
    """
    if getattr(token_store, "kind", None) == "OS_CREDENTIAL_STORE" \
            and not token_store.available:
        raise FilmError(
            "CREDENTIAL_STORE_UNAVAILABLE: connect through an explicit "
            "memory-limited session instead — tokens are never written to "
            "settings files")
    result = flow.authorize()             # may raise AUTH_DENIED
    connection_id = f"conn-{uuid.uuid4().hex[:12]}"
    token_store.store(connection_id, result["token"])
    session = DriveSession(connection_id, result["account_id"],
                           result["granted_object_ids"], token_store,
                           backend, now_fn=now_fn,
                           provider=getattr(backend, "name", "FAKE_DRIVE"))
    if home is not None:
        session.save_metadata(home)
    return session


def load_connection_metadata(home):
    """Read the persisted non-secret connection record, if any."""
    path = Path(home) / "drive_connection.json"
    return read(path) if path.is_file() else None


def redact(value):
    """Replace secret-keyed fields. Does not echo the removed values."""
    if type(value) is dict:
        return {key: ("[REDACTED]" if key in SECRET_FIELDS else redact(item))
                for key, item in value.items()}
    if type(value) is list:
        return [redact(item) for item in value]
    return value


def assert_no_secrets(text, secrets):
    """Refuse to publish text that still contains a known secret value."""
    for secret in secrets:
        if secret and secret in text:
            raise FilmError(
                "EVIDENCE_LEAK: redacted evidence still contains a secret")
    return text


def connect_google(token_store=None, *, transport=None, browser=None,
                   loopback=None, home=None, now_fn=None, client_file=None):
    """Consent + a Drive backend bound to the same in-memory session.

    The backend is constructed before `connect_drive` mints the connection
    id; the token cell is filled before any Drive call. `real_google` is
    true only when the token exchange used `UrllibTransport`.
    """
    from .storage_backends.drive import GoogleDriveBackend
    store = token_store or MemoryTokenStore()
    flow = GoogleOAuthFlow(client_file=client_file, transport=transport,
                           browser=browser, loopback=loopback, now_fn=now_fn)
    cell = {"id": None}

    def provider():
        if cell["id"] is None:
            raise FilmError(
                "GOOGLE_DRIVE_NOT_CONFIGURED: the session has no token yet")
        return store.load(cell["id"])

    def updater(token):
        store.store(cell["id"], token)

    backend = GoogleDriveBackend(
        provider, transport=flow.transport, token_updater=updater,
        now_fn=now_fn)

    def refresh():
        current = store.load(cell["id"])
        updater(flow.refresh(current))

    session = connect_drive(flow, store, backend, now_fn=now_fn, home=home)
    cell["id"] = session.connection_id
    session.refresher = refresh
    session.real_google = bool(flow.completed_on_real_google)
    return session
