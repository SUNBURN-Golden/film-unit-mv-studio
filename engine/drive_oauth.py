"""Browser Google-login contract for the Drive archive (ANIM-013).

Execution/storage §3 and schema §17: the user connects through the system
browser, PKCE and state; the minimum scope is `drive.file` — files the app
creates or the user explicitly selects, never whole-Drive access. No OAuth
client is issued for this program, so `GoogleOAuthFlow` is a disabled stub
and `FakeOAuthFlow` simulates the consent screen for protocol tests.

Tokens live only in a supported OS credential store or an explicit
memory-limited session — never in a settings file, project, build, packet
or log. The only persisted fields are the non-secret `connection_id`,
`account_binding_digest` and `credential_epoch` plus scope names. Logout,
permission revocation and account switch raise the epoch and block every
subsequent read, write and upload resume.
"""
import hashlib
import json
import time
import uuid

from pathlib import Path

from .core import FilmError, atomic_text, read
from .storage_backends import ArchiveRequestError

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
    account authorized (None = every object, the test's folder-level grant).
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
        granted = (sorted(self.granted_object_ids)
                   if self.granted_object_ids is not None else None)
        return {"account_id": self.account,
                "token": {"access_token": f"fake-access-{uuid.uuid4().hex}",
                          "refresh_token": f"fake-refresh-{uuid.uuid4().hex}",
                          "expires_at": self._now() + self.expires_in,
                          "scope": DRIVE_FILE_SCOPE},
                "granted_object_ids": granted}


class GoogleOAuthFlow:
    """Disabled real flow: no issued client means no real OAuth dance."""

    def __init__(self, client_config=None):
        if not client_config:
            raise FilmError(
                "GOOGLE_OAUTH_NOT_CONFIGURED: no OAuth client id is issued "
                "for this program; the fake flow covers protocol checks and "
                "real connectivity stays UNQUALIFIED")
        raise FilmError(
            "GOOGLE_OAUTH_UNQUALIFIED: a client id alone does not qualify "
            "the browser flow; no real Google access is performed")

    def authorize(self):
        raise FilmError("GOOGLE_OAUTH_UNQUALIFIED")


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
        return self._session.backend.list_objects()

    def put_object(self, object_id, data):
        self._session._check()
        result = self._session.backend.put_object(object_id, data)
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
        result = self._session.backend.complete_upload(session_id)
        self._session._owned.add(self._session._sessions.pop(session_id))
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
            raise AuthError("AUTH_EXPIRED", "The connection token expired")

    def _accessible(self, object_id):
        """drive.file scope: app-created objects + the user's explicit grant."""
        if self.granted_object_ids is None:
            return True
        return object_id in self._owned \
            or object_id in self.granted_object_ids

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
