"""ANIM-013 OAuth fixtures: refusal, expiry, account binding, permission,
logout/revoke/switch, token-store rules and the disabled real stubs.

No real OAuth client exists and none is requested; every flow is the fake
consent screen. Tokens live in the memory-limited store (or the OS
credential store when one exists) — never in settings files, projects,
builds or logs.
"""
import json
import time

import pytest

from anim_013_kit import connect, fake_backend

from engine.core import FilmError
from engine.drive_oauth import (AuthError, DRIVE_FILE_SCOPE,
                                FakeOAuthFlow, GoogleOAuthFlow,
                                MemoryTokenStore, OsCredentialStore,
                                connect_drive, load_connection_metadata,
                                plaintext_token_store)
from engine.storage_backends import ArchiveRequestError
from engine.storage_backends.drive import GoogleDriveBackend


def test_user_refusal_blocks_the_connection():
    with pytest.raises(AuthError) as e:
        connect_drive(FakeOAuthFlow(deny=True), MemoryTokenStore(),
                      fake_backend())
    assert e.value.code == "AUTH_DENIED"


def test_minimum_scope_is_drive_file_only():
    session = connect()
    assert session.metadata()["scopes"] == [DRIVE_FILE_SCOPE]
    assert "drive.readonly" not in DRIVE_FILE_SCOPE


def test_connected_session_reads_within_its_grant():
    backend = fake_backend()
    backend.put_object("obj-1", b"data")
    session = connect(backend)
    assert session.authorized.get_object("obj-1").body == b"data"


def test_expired_token_blocks_every_operation():
    clock = {"now": 1000.0}
    backend = fake_backend()
    backend.put_object("obj-1", b"data")
    session = connect_drive(
        FakeOAuthFlow(expires_in=60, now_fn=lambda: clock["now"]),
        MemoryTokenStore(), backend, now_fn=lambda: clock["now"])
    assert session.authorized.get_object("obj-1").body == b"data"
    clock["now"] += 120
    with pytest.raises(AuthError) as e:
        session.authorized.get_object("obj-1")
    assert e.value.code == "AUTH_EXPIRED"
    with pytest.raises(AuthError):
        session.authorized.put_object("new", b"x")


def test_logout_blocks_access_and_bumps_credential_epoch():
    backend = fake_backend()
    backend.put_object("obj-1", b"data")
    session = connect(backend)
    epoch = session.credential_epoch
    session.logout()
    assert session.credential_epoch == epoch + 1
    with pytest.raises(AuthError) as e:
        session.authorized.get_object("obj-1")
    assert e.value.code == "AUTH_REVOKED"
    with pytest.raises(AuthError):
        session.authorized.create_upload_session("o", 1)


def test_permission_revocation_blocks_access():
    backend = fake_backend()
    session = connect(backend)
    session.revoke_permissions()
    with pytest.raises(AuthError) as e:
        session.authorized.put_object("o", b"x")
    assert e.value.code == "PERMISSION_REVOKED"


def test_account_switch_closes_the_old_binding():
    first = connect()
    first.switch_account()
    with pytest.raises(AuthError) as e:
        first.authorized.list_objects()
    assert e.value.code == "ACCOUNT_CHANGED"
    second = connect_drive(FakeOAuthFlow(account="account-b"),
                           MemoryTokenStore(), fake_backend())
    assert second.account_binding_digest != first.account_binding_digest
    # The switch bumped the closed connection's epoch — old grants pinned
    # to epoch 1 can no longer authorize anything.
    assert first.credential_epoch == 2


def test_granted_set_limits_reads_to_selected_objects():
    backend = fake_backend()
    backend.put_object("obj-a", b"a")
    backend.put_object("obj-b", b"b")
    session = connect_drive(
        FakeOAuthFlow(granted_object_ids=["obj-a"]),
        MemoryTokenStore(), backend)
    assert session.authorized.get_object("obj-a").body == b"a"
    with pytest.raises(ArchiveRequestError) as e:
        session.authorized.get_object("obj-b")
    assert e.value.status == 403
    # drive.file: objects this app created through the session are allowed.
    session.authorized.put_object("obj-new", b"n")
    assert session.authorized.get_object("obj-new").body == b"n"


def test_metadata_holds_no_secrets(tmp_path):
    session = connect()
    session.save_metadata(tmp_path)
    stored = json.loads((tmp_path / "drive_connection.json").read_text())
    assert set(stored) == {"connection_id", "account_binding_digest",
                           "credential_epoch", "provider", "scopes",
                           "token_store"}
    blob = json.dumps(stored)
    for forbidden in ("access_token", "refresh_token", "authorization_code",
                      "client_secret", "bearer", "upload_uri", "fake-access",
                      "fake-refresh"):
        assert forbidden not in blob
    assert load_connection_metadata(tmp_path)["connection_id"] \
        == session.connection_id


def test_memory_token_store_holds_tokens_in_process_only():
    store = MemoryTokenStore()
    store.store("c1", {"access_token": "x", "expires_at": time.time() + 99})
    assert store.load("c1")["access_token"] == "x"
    store.revoke("c1")
    assert store.load("c1") is None


def test_plaintext_settings_token_store_is_blocked():
    with pytest.raises(FilmError, match="TOKEN_STORE_BLOCKED"):
        plaintext_token_store("/anywhere")


def test_missing_os_credential_store_blocks_connect():
    store = OsCredentialStore()
    if store.available:
        pytest.skip("this box has a supported OS credential store")
    with pytest.raises(FilmError, match="CREDENTIAL_STORE_UNAVAILABLE"):
        connect_drive(FakeOAuthFlow(), store, fake_backend())


def test_os_credential_store_roundtrip_when_available():
    store = OsCredentialStore(service="film-unit-drive-test")
    if not store.available:
        pytest.skip("no supported OS credential store on this box")
    try:
        store.store("c1", {"access_token": "x", "expires_at": 1e15})
        assert store.load("c1")["access_token"] == "x"
        store.revoke("c1")
        assert store.load("c1") is None
    finally:
        store.revoke("c1")


def test_real_google_flow_and_backend_are_disabled_stubs():
    with pytest.raises(FilmError, match="NOT_CONFIGURED"):
        GoogleOAuthFlow()
    with pytest.raises(FilmError, match="UNQUALIFIED"):
        GoogleOAuthFlow({"client_id": "issued-elsewhere"})
    with pytest.raises(FilmError, match="NOT_CONFIGURED"):
        GoogleDriveBackend()
    with pytest.raises(FilmError, match="UNQUALIFIED"):
        GoogleDriveBackend({"client_id": "issued-elsewhere"})
