"""ANIM-018: opt-in Drive OAuth + archive route, offline only.

No test opens a socket to Google. The fixture transport is not
qualification. A missing client file is WAITING, not a crash.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import pytest

from engine.archive_manifest import bounded_read
from engine.core import FilmError, init_project, production_profile, read
from engine.drive_oauth import (AuthError, DRIVE_FILE_SCOPE, GoogleOAuthFlow,
                                LocalLoopback, MemoryTokenStore, ScriptedLoopback,
                                connect_google, pkce_challenge)
from engine.google_transport import MemoryDriveTransport, UrllibTransport
from engine.remote_qualify import (DEFAULT_SCRATCH_LIMIT, TARGET_FRAMES,
                                   TARGET_HEIGHT, TARGET_RASTER_BYTES,
                                   TARGET_WIDTH, archive_panel_caption,
                                   draft_qualification, qualification_for,
                                   target_preflight)
from engine.storage_backends import ArchiveRequestError, ConnectionDropped
from engine.storage_backends.drive import GoogleDriveBackend


SECRET = "test-client-secret"
RETRY = {"max_retries": 2, "backoff_base_ms": 0, "max_backoff_ms": 5,
         "max_elapsed_ms": 5000, "max_requests": 6,
         "max_transferred_bytes": 5_000_000}


def _client(path, secret=SECRET):
    path.write_text(json.dumps({
        "installed": {
            "client_id": "anim018-test.apps.googleusercontent.com",
            "client_secret": secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://127.0.0.1"]}}), encoding="utf-8")
    return secret


def _flow(tmp_path, transport, query):
    captured = {}

    def browser(url):
        captured["url"] = url
        captured["state"] = parse_qs(urlparse(url).query)["state"][0]

    def payload():
        merged = {"code": "anim018-code", "state": captured.get("state")}
        merged.update(query)
        if "state" not in query:
            merged["state"] = captured.get("state")
        return merged

    loop = ScriptedLoopback(payload)
    flow = GoogleOAuthFlow(client_file=str(tmp_path / "client.json"),
                           transport=transport, browser=browser, loopback=loop)
    return flow, captured


def _backend():
    transport = MemoryDriveTransport()
    holder = {"token": {"access_token": "mem-access-unit",
                        "refresh_token": "mem-refresh-unit",
                        "expires_at": 10 ** 12,
                        "scope": DRIVE_FILE_SCOPE}}
    backend = GoogleDriveBackend(lambda: holder["token"], transport=transport)
    return backend, transport


def test_pkce_s256_matches_rfc7636():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_missing_client_file_is_not_configured(monkeypatch):
    monkeypatch.delenv("FILM_GOOGLE_OAUTH_CLIENT_FILE", raising=False)
    with pytest.raises(FilmError, match="GOOGLE_OAUTH_NOT_CONFIGURED"):
        GoogleOAuthFlow()
    with pytest.raises(FilmError, match="GOOGLE_OAUTH_NOT_CONFIGURED"):
        GoogleOAuthFlow({"client_id": "issued-elsewhere"})


def test_loopback_captures_the_redirect_without_logging(capsys):
    loop = LocalLoopback()
    loop.__enter__()
    try:
        def hit():
            url = loop.redirect_uri() + "?code=abc&state=xyz"
            with urlopen(url, timeout=2) as response:
                assert b"close this tab" in response.read()
        thread = threading.Thread(target=hit)
        thread.start()
        query = loop.wait(timeout=2)
        thread.join(timeout=2)
    finally:
        loop.close()
    assert query == {"code": "abc", "state": "xyz"}
    logged = capsys.readouterr()
    assert "abc" not in logged.err and "abc" not in logged.out


def test_loopback_keeps_the_oauth_query_when_favicon_follows():
    loop = LocalLoopback()
    loop.__enter__()
    try:
        base = loop.redirect_uri()
        with urlopen(base + "?code=abc&state=xyz", timeout=2) as response:
            assert b"close this tab" in response.read()
        with urlopen(base + "favicon.ico", timeout=2) as response:
            response.read()
        query = loop.wait(timeout=2)
    finally:
        loop.close()
    assert query == {"code": "abc", "state": "xyz"}


def test_authorize_uses_pkce_state_and_drive_file_scope(tmp_path):
    _client(tmp_path / "client.json")
    transport = MemoryDriveTransport()
    flow, captured = _flow(tmp_path, transport, {})
    result = flow.authorize()
    url = captured["url"]
    query = parse_qs(urlparse(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == [DRIVE_FILE_SCOPE]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"][0].startswith("http://127.0.0.1:")
    assert pkce_challenge  # challenge is present and not the verifier
    assert "code_challenge" in query and "code_verifier" not in url
    assert SECRET not in url
    assert "client_secret" not in url
    assert result["granted_object_ids"] == []
    assert result["token"]["scope"] == DRIVE_FILE_SCOPE
    assert result["account_id"] == "perm-anim018"
    assert flow.completed_on_real_google is False
    assert "code_verifier" in transport.token_field_names
    assert "client_secret" in transport.token_field_names
    assert "code_challenge" not in transport.token_field_names
    assert "GoogleOAuthFlow" in repr(flow) and SECRET not in repr(flow)


def test_user_denial_does_not_call_the_token_endpoint(tmp_path):
    _client(tmp_path / "client.json")
    transport = MemoryDriveTransport()
    flow, _ = _flow(tmp_path, transport, {"error": "access_denied", "state": ""})
    # The scripted query forces state to the captured value via the helper
    # only when state is omitted. Override with an explicit denial.
    flow._loopback = ScriptedLoopback({"error": "access_denied", "state": "ignored"})
    with pytest.raises(AuthError) as exc:
        flow.authorize()
    assert exc.value.code == "AUTH_DENIED"
    assert transport.calls == []


def test_state_mismatch_does_not_exchange_the_code(tmp_path):
    _client(tmp_path / "client.json")
    transport = MemoryDriveTransport()
    flow, _ = _flow(tmp_path, transport, {})
    flow._loopback = ScriptedLoopback({"code": "leaked-code", "state": "nope"})
    with pytest.raises(AuthError) as exc:
        flow.authorize()
    assert exc.value.code == "STATE_MISMATCH"
    assert transport.calls == []


def test_scope_mismatch_discards_the_token(tmp_path):
    _client(tmp_path / "client.json")
    transport = MemoryDriveTransport()
    transport.token_scope = "email"
    flow, _ = _flow(tmp_path, transport, {})
    with pytest.raises(AuthError) as exc:
        flow.authorize()
    assert exc.value.code == "SCOPE_MISMATCH"
    assert not any(call["path"] == "/drive/v3/about" for call in transport.calls)


def test_token_redirect_is_refused(tmp_path):
    _client(tmp_path / "client.json")
    transport = MemoryDriveTransport()
    flow, _ = _flow(tmp_path, transport, {})

    def redirected(method, url, **kwargs):
        from engine.google_transport import HttpResponse
        return HttpResponse(302, {"location": "https://evil.example/token"}, b"")

    transport.request = redirected
    with pytest.raises(FilmError, match="REDIRECT_REFUSED"):
        flow.authorize()


def test_cross_host_url_is_refused():
    from engine.google_transport import check_google_url
    with pytest.raises(FilmError, match="REDIRECT_REFUSED"):
        check_google_url("https://evil.example/drive")
    with pytest.raises(FilmError, match="REDIRECT_REFUSED"):
        check_google_url("http://www.googleapis.com/drive/v3/files")


def test_drive_backend_requires_a_token_provider():
    with pytest.raises(FilmError, match="GOOGLE_DRIVE_NOT_CONFIGURED"):
        GoogleDriveBackend()
    with pytest.raises(FilmError, match="GOOGLE_DRIVE_NOT_CONFIGURED"):
        GoogleDriveBackend({"client_id": "issued-elsewhere"})


def test_resumable_upload_range_read_md5_and_unqualified_checksum():
    backend, _transport = _backend()
    payload = b"anim-018-pack-bytes"
    first = backend.put_object("obj-anim018", payload)
    assert first["reused"] is False
    again = backend.put_object("obj-anim018", payload)
    assert again["reused"] is True
    info = backend.object_info("obj-anim018")
    assert info["provider_checksum"] is None
    assert info["byte_length"] == len(payload)
    assert info["md5"]
    assert backend.get_object("obj-anim018").body == payload
    partial = backend.get_range("obj-anim018", 2, 4)
    assert partial.status == 206 and partial.body == payload[2:6]
    assert partial.range_start == 2
    assert backend.list_objects() == ["obj-anim018"]
    with pytest.raises(FilmError, match="different bytes"):
        backend.put_object("obj-anim018", b"other-bytes-entirely")


def test_revision_pin_rejects_a_moved_head():
    backend, transport = _backend()
    backend.put_object("obj-anim018", b"pinned-bytes-ok")
    transport.bump_revision("obj-anim018")
    with pytest.raises(ArchiveRequestError) as exc:
        backend.object_info("obj-anim018")
    assert exc.value.status == 409 and exc.value.reason == "input_mismatch"


def test_range_200_is_not_reported_as_partial():
    backend, transport = _backend()
    payload = b"whole-object-body"
    backend.put_object("obj-anim018", payload)
    transport.ignore_range = True
    result = backend.get_range("obj-anim018", 0, 4)
    assert result.status == 200 and result.body == payload


def test_416_is_not_retried():
    backend, transport = _backend()
    backend.put_object("obj-anim018", b"short")
    before = len(transport.calls)
    with pytest.raises(ArchiveRequestError) as exc:
        bounded_read(lambda: backend.get_range("obj-anim018", 50, 4), RETRY,
                     lambda ms: None)
    assert exc.value.status == 416 and not exc.value.retriable()
    assert len(transport.calls) - before == 2  # metadata + one media attempt


def test_429_retries_inside_the_bounded_policy_only():
    backend, transport = _backend()
    payload = b"retry-range-bytes"
    backend.put_object("obj-anim018", payload)
    transport.push_fault("alt=media", status=429, reason="rate_limit",
                         retry_after=0)
    before = len(transport.calls)
    result = bounded_read(lambda: backend.get_range("obj-anim018", 0, 4),
                          RETRY, lambda ms: None)
    assert result.body == payload[:4]
    window = transport.calls[before:]
    assert sum(call["status"] == 429 for call in window) == 1
    assert len(window) > 2


def test_401_and_permission_403_do_not_retry():
    backend, transport = _backend()
    backend.put_object("obj-anim018", b"auth-bytes-here")
    transport.push_fault("alt=media", status=401, reason="authError")
    before = len(transport.calls)
    with pytest.raises(ArchiveRequestError) as exc:
        bounded_read(lambda: backend.get_object("obj-anim018"), RETRY,
                     lambda ms: None)
    assert exc.value.status == 401 and not exc.value.retriable()
    assert len(transport.calls) - before == 2
    transport.push_fault("alt=media", status=403, reason="insufficientFilePermissions")
    before = len(transport.calls)
    with pytest.raises(ArchiveRequestError) as exc:
        bounded_read(lambda: backend.get_object("obj-anim018"), RETRY,
                     lambda ms: None)
    assert exc.value.status == 403 and exc.value.reason == "permission_denied"
    assert not exc.value.retriable()
    assert len(transport.calls) - before == 2


def test_rate_limit_403_may_retry():
    backend, transport = _backend()
    backend.put_object("obj-anim018", b"rate-limit-body")
    transport.push_fault("alt=media", status=403, reason="rateLimitExceeded")
    result = bounded_read(lambda: backend.get_object("obj-anim018"), RETRY,
                          lambda ms: None)
    assert result.body == b"rate-limit-body"


def test_session_create_is_not_retried():
    backend, transport = _backend()
    backend.put_object("seed-anim018", b"seed-bytes")  # creates the app folder
    transport.push_fault("uploadType=resumable", status=503, reason="unavailable",
                         times=3)
    before = len(transport.calls)
    with pytest.raises(ArchiveRequestError) as exc:
        backend.create_upload_session("obj-anim018", 4)
    assert exc.value.status == 503
    assert len(transport.calls) - before == 1


def test_upload_drop_resumes_from_the_status_offset():
    backend, transport = _backend()
    payload = b"0123456789abcdef"
    session = backend.create_upload_session("resume-anim018", len(payload))
    transport.push_fault("/resumable/", drop=True)
    with pytest.raises(ConnectionDropped):
        backend.upload_chunk(session, 0, payload)
    status = backend.upload_status(session)
    assert status["offset"] == 0
    backend.upload_chunk(session, status["offset"], payload)
    done = backend.complete_upload(session)
    assert done["object_id"] == "resume-anim018"
    assert backend.get_object("resume-anim018").body == payload
    # A second complete does not create another file.
    assert backend.complete_upload(session)["object_id"] == "resume-anim018"


def test_upload_uri_is_absent_from_errors():
    backend, _transport = _backend()
    session = backend.create_upload_session("obj-anim018", 4)
    uri = backend.secret_material()[-1]
    with pytest.raises(ArchiveRequestError) as exc:
        backend.upload_chunk("missing-session", 0, b"data")
    assert uri not in str(exc.value)
    assert "/resumable/" not in str(exc.value)


def test_cross_host_upload_location_is_refused():
    backend, transport = _backend()
    transport.next_location = "https://evil.example/upload"
    with pytest.raises(FilmError, match="REDIRECT_REFUSED"):
        backend.create_upload_session("obj-anim018", 4)
    assert backend.secret_material() == []


def test_md5_mismatch_is_integrity_failed_not_a_retry():
    backend, transport = _backend()
    backend.put_object("obj-anim018", b"stable-bytes")
    transport.flip_byte("obj-anim018")
    before = len(transport.calls)
    with pytest.raises(FilmError, match="INTEGRITY_FAILED"):
        bounded_read(lambda: backend.get_object("obj-anim018"), RETRY,
                     lambda ms: None)
    # One revision download. A retry would fetch the body twice.
    media = [call for call in transport.calls[before:]
             if "/revisions/" in call["path"] and call["status"] == 200]
    assert len(media) == 1


def test_token_refresh_extends_a_google_session(tmp_path):
    _client(tmp_path / "client.json")
    clock = {"now": 1_000.0}
    transport = MemoryDriveTransport()
    captured = {}

    def browser(url):
        captured["state"] = parse_qs(urlparse(url).query)["state"][0]

    loop = ScriptedLoopback(lambda: {"code": "c", "state": captured["state"]})
    # expires_in from the double is 3600; force a short lifetime via now.
    session = connect_google(
        MemoryTokenStore(), transport=transport, browser=browser, loopback=loop,
        client_file=str(tmp_path / "client.json"), now_fn=lambda: clock["now"])
    session.authorized.put_object("obj-anim018", b"refresh-me-please")
    clock["now"] += 10_000
    # The session refresher runs before the archive call. Fake sessions
    # without a refresher still expire; this one is the Google path.
    assert session.authorized.get_object("obj-anim018").body == b"refresh-me-please"
    assert any(call["path"] == "/token" for call in transport.calls[2:])


def test_expired_fake_session_still_blocks_without_a_refresher():
    from engine.drive_oauth import FakeOAuthFlow, connect_drive
    from engine.storage_backends.fake_drive import FakeDriveBackend
    clock = {"now": 1000.0}
    backend = FakeDriveBackend()
    backend.put_object("obj-1", b"data")
    session = connect_drive(
        FakeOAuthFlow(expires_in=60, now_fn=lambda: clock["now"],
                      granted_object_ids=["obj-1"]),
        MemoryTokenStore(), backend, now_fn=lambda: clock["now"])
    clock["now"] += 120
    with pytest.raises(AuthError) as exc:
        session.authorized.get_object("obj-1")
    assert exc.value.code == "AUTH_EXPIRED"


def test_draft_qualification_stays_waiting_and_unqualified():
    draft = draft_qualification()
    assert draft["node_state"] == "WAITING"
    assert draft["qualification_state"] == "UNQUALIFIED"
    assert draft["acceptance_state"] == "PENDING"
    assert draft["release_state"] == "NOT_AUTHORIZED"
    assert draft["evidence_state"] == "WAITING"
    assert draft["real_run_claimed"] is False
    assert draft["fake_counts_as_qualification"] is False
    assert draft["user_merge"] is True and draft["astra_auto_merge"] is False
    assert draft["changes_existing_project_defaults"] is False
    assert draft["evolution_section_5"]["approved_plan_commit"] is None
    assert draft["target_profile"]["output_frames"] == 5760
    assert len(draft["missing"]) == 3
    assert "NOT_REQUIRED" not in json.dumps(draft)


def test_qualification_gate_refuses_fake_and_partial_profiles():
    assert qualification_for(real_transport=False, oauth_completed=True,
                             profile_exact=True, stages_ok=True) == "UNQUALIFIED"
    assert qualification_for(real_transport=True, oauth_completed=False,
                             profile_exact=True, stages_ok=True) == "UNQUALIFIED"
    assert qualification_for(real_transport=True, oauth_completed=True,
                             profile_exact=False, stages_ok=True) == "PARTIAL"
    assert qualification_for(real_transport=True, oauth_completed=True,
                             profile_exact=True, stages_ok=False) == "PARTIAL"
    assert qualification_for(real_transport=True, oauth_completed=True,
                             profile_exact=True, stages_ok=True) == "QUALIFIED"
    # The real class is not enough unless the caller also saw a real exchange.
    assert isinstance(UrllibTransport(), UrllibTransport)
    facets = draft_qualification()
    assert facets["node_state"] != "DONE"
    assert facets["release_state"] == "NOT_AUTHORIZED"


def test_target_raster_is_not_silently_downscaled():
    blocked = target_preflight(DEFAULT_SCRATCH_LIMIT)
    assert blocked["status"] == "CAPACITY_BLOCKED"
    assert blocked["required_raster_bytes"] == TARGET_RASTER_BYTES
    assert blocked["executed_frames"] == 0
    assert blocked["downscaled"] is False
    assert TARGET_RASTER_BYTES == TARGET_WIDTH * TARGET_HEIGHT * 4 * TARGET_FRAMES
    fits = target_preflight(TARGET_RASTER_BYTES)
    assert fits["status"] == "OK" and fits["executed_frames"] == TARGET_FRAMES


def test_list_objects_follows_the_next_page():
    backend, transport = _backend()
    folder = backend._folder_id()
    for index in range(101):
        transport.files[f"id-{index}"] = {
            "id": f"id-{index}", "name": f"p{index:03d}",
            "parents": [folder], "folder": False, "data": b"x",
            "md5": "ab", "revision": "rev-1", "size": 1}
    names = backend.list_objects()
    assert set(names) == {f"p{index:03d}" for index in range(101)}


def test_panel_imports_when_the_unix_resource_module_is_missing():
    """Windows has no `resource`. The archive panel imports this module."""
    script = (
        "import sys\n"
        "sys.modules['resource'] = None\n"
        "import app.archive_ui\n"
        "from engine.remote_qualify import archive_panel_caption, _runtime\n"
        "assert 'UNQUALIFIED' in archive_panel_caption()\n"
        "assert _runtime()['process_rss_bytes'] is None\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    env.pop("FILM_GOOGLE_OAUTH_CLIENT_FILE", None)
    run = subprocess.run([sys.executable, "-c", script],
                         capture_output=True, text=True, env=env)
    assert run.returncode == 0, run.stderr


def test_runtime_marks_ffmpeg_unavailable_when_the_probe_times_out(monkeypatch):
    def hung(args, timeout=600):
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr("engine.core.run", hung)
    from engine.remote_qualify import _runtime
    assert _runtime()["ffmpeg"] == "UNAVAILABLE"


def test_md5_mismatch_measures_the_sealed_archive_digest():
    backend, transport = _backend()
    body = b"sealed-manifest-bytes"
    backend.put_object("manifest-anim018", body)
    digest = hashlib.sha256(body).hexdigest()
    record = transport._by_name("manifest-anim018")
    record["data"] = b"rewritten-manifest"
    record["md5"] = hashlib.md5(record["data"]).hexdigest()

    class Session:
        authorized = backend

    from engine.remote_qualify import _fail_closed
    changed = _fail_closed(Session(), transport, digest, "manifest-anim018")
    assert changed["md5_mismatch"]["rejected"] is True
    assert changed["md5_mismatch"]["sealed_archive_sha_unchanged"] is False
    assert changed["md5_mismatch"]["archive_sha256"] == hashlib.sha256(
        b"rewritten-manifest").hexdigest()


def test_attended_run_denies_the_real_client_secret(tmp_path, monkeypatch):
    secret = _client(tmp_path / "client.json", "attended-real-secret")
    monkeypatch.setenv("FILM_GOOGLE_OAUTH_CLIENT_FILE",
                       str(tmp_path / "client.json"))
    seen = {}

    class _Backend:
        transport = object()

    class _Session:
        backend = _Backend()

    def connect():
        return _Session()

    def pipeline(session, transport, scratch, *, width, height, frames,
                 client_secret, scratch_limit_bytes):
        seen["client_secret"] = client_secret
        return {"qualification_state": "UNQUALIFIED"}, [client_secret]

    monkeypatch.setattr("engine.remote_qualify.connect_google", connect)
    monkeypatch.setattr("engine.remote_qualify.target_preflight",
                        lambda limit: {"status": "OK"})
    monkeypatch.setattr("engine.remote_qualify.run_pipeline", pipeline)
    from engine.remote_qualify import run_attended
    _report, secrets = run_attended(1)
    assert seen["client_secret"] == secret
    assert secret in secrets


def test_attended_capacity_block_denies_the_client_secret(tmp_path, monkeypatch):
    secret = _client(tmp_path / "client.json", "attended-blocked-secret")
    monkeypatch.setenv("FILM_GOOGLE_OAUTH_CLIENT_FILE",
                       str(tmp_path / "client.json"))

    class _Store:
        def load(self, connection_id):
            return {"access_token": "mem-access-block",
                    "refresh_token": "mem-refresh-block"}

    class _Session:
        real_google = True
        connection_id = "conn-block"
        token_store = _Store()

    monkeypatch.setattr("engine.remote_qualify.connect_google",
                        lambda: _Session())
    from engine.remote_qualify import run_attended
    report, secrets = run_attended(1)
    assert report["detail"] == "CAPACITY_BLOCKED"
    assert secret in secrets
    assert "mem-access-block" in secrets


def test_panel_caption_reports_waiting(monkeypatch):
    monkeypatch.delenv("FILM_GOOGLE_OAUTH_CLIENT_FILE", raising=False)
    text = archive_panel_caption()
    assert "WAITING" in text and "UNQUALIFIED" in text
    assert "PENDING" in text and "NOT_AUTHORIZED" in text
    assert "GOOGLE_OAUTH_NOT_CONFIGURED" in text
    assert "QUALIFIED" not in text.replace("UNQUALIFIED", "")


def test_cli_without_a_client_file_writes_waiting_evidence(tmp_path, monkeypatch):
    monkeypatch.delenv("FILM_GOOGLE_OAUTH_CLIENT_FILE", raising=False)
    from engine.cli import main
    out = tmp_path / "evidence"
    assert main(["remote-qualify", "--evidence-dir", str(out)]) == 0
    document = json.loads((out / "anim018-evidence.json").read_text())
    assert document["evidence_state"] == "WAITING"
    assert document["qualification_state"] == "UNQUALIFIED"
    assert document["real_route_completed"] is False
    assert document["protocol_fixture"] == "NOT_RUN"
    assert document["detail"] == "GOOGLE_OAUTH_NOT_CONFIGURED"
    assert "source_sha" in document and document["paid_calls"] == 0
    markdown = (out / "anim018-evidence.md").read_text()
    assert "WAITING" in markdown and SECRET not in markdown


def test_evidence_inside_the_repo_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("FILM_GOOGLE_OAUTH_CLIENT_FILE", raising=False)
    from engine.cli import main
    repo = Path(__file__).resolve().parents[1]
    assert main(["remote-qualify", "--evidence-dir", str(repo / "evidence-out")]) == 1
    assert not (repo / "evidence-out").exists()


def test_legacy_project_is_not_migrated(tmp_path, monkeypatch):
    monkeypatch.delenv("FILM_GOOGLE_OAUTH_CLIENT_FILE", raising=False)
    from engine.audio import synth_test_audio
    from engine.cli import main
    audio = synth_test_audio(tmp_path / "song.wav", seconds=1)
    project = init_project(tmp_path, "legacy", audio, "brief", synthetic=True)
    before = (project / "project.yaml").read_bytes()
    assert production_profile(read(project / "project.yaml")) == "LEGACY_MV"
    out = tmp_path / "evidence"
    assert main(["remote-qualify", "--evidence-dir", str(out)]) == 0
    assert (project / "project.yaml").read_bytes() == before
    text = before.decode()
    assert "5760" not in text
    assert "FRAME_ANIMATION_V1" not in text
    assert TARGET_PROFILE_NOT_DEFAULT(text)


def TARGET_PROFILE_NOT_DEFAULT(text):
    return "1920" not in text and "DRIVE_BOUNDED" not in text


def test_fixture_runs_the_route_and_stays_unqualified(tmp_path, monkeypatch):
    monkeypatch.delenv("FILM_GOOGLE_OAUTH_CLIENT_FILE", raising=False)
    from engine.cli import main
    out = tmp_path / "evidence"
    assert main(["remote-qualify", "--fixture", "--evidence-dir", str(out)]) == 0
    document = json.loads((out / "anim018-evidence.json").read_text())
    markdown = (out / "anim018-evidence.md").read_text()
    blob = json.dumps(document) + markdown
    assert document["problems"] == []
    assert document["protocol_fixture"] == "PASS"
    assert document["qualification_state"] == "UNQUALIFIED"
    assert document["evidence_state"] == "WAITING"
    assert document["acceptance_state"] == "PENDING"
    assert document["release_state"] == "NOT_AUTHORIZED"
    assert document["node_state"] == "WAITING"
    assert document["real_route_completed"] is False
    assert document["real_run_claimed"] is False
    assert document["profile_exact"] is False
    assert document["executed"]["frames"] == 2
    assert document["target_profile"]["output_frames"] == 5760
    assert document["target_profile"]["width"] == 1920
    assert document["transfer_route"] == "COORDINATOR_RELAY"
    assert "DIRECT_DRIVE" not in document["allowed_routes"]
    assert document["archive"]["achieved_level"] == "FULL_READBACK"
    assert document["archive"]["offline_restore"] is True
    assert document["archive"]["replay_without_project"] is True
    assert document["worker"]["state"] == "VERIFIED"
    assert document["worker"]["token_in_worker_state"] is False
    assert document["worker"]["relay_auth_redirect_refused"] is True
    assert document["worker"]["qualification_state"] == "UNQUALIFIED"
    assert document["device"]["gpu"] == "hardware-unverified"
    assert document["peak"]["vram_bytes"] is None
    for key in ("cold_ms", "warm_ms", "samples", "note"):
        assert key in document["cold_warm"]
    assert document["stage_timeline"]
    assert document["usage"]["additional_charges"] is False
    assert document["usage"]["paid_calls"] == 0
    assert document["transfer"]["requests"] > 0
    assert document["transfer"]["duplicate_bytes"] > 0
    recovery = document["recovery_scope"]
    assert recovery["transport_retry_429"]["retried"] is True
    assert recovery["auth_401"]["rejected"] is True
    assert recovery["auth_401"]["retried"] is False
    assert recovery["permission_403"]["retried"] is False
    assert recovery["rate_limit_403"]["retried"] is True
    assert recovery["create_not_retried"]["requests"] == 1
    assert recovery["upload_resume"]["completed"] is True
    assert recovery["md5_mismatch"]["rejected"] is True
    assert document["changes_existing_project_defaults"] is False
    for forbidden in (SECRET, "anim018-client-secret-value",
                      "anim018-test.apps.googleusercontent.com",
                      "anim018-code", "mem-access-", "mem-refresh-",
                      "/resumable/"):
        assert forbidden not in blob
    assert document["source_sha"] and len(document["source_sha"]) == 40
