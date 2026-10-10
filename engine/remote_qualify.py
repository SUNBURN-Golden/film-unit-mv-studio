"""ANIM-018 remote integration: Drive → local worker → archive → replay.

The target profile is 240 s · 24 fps · 1920×1080 (5,760 frames). It is not
a project default and it does not convert LEGACY_MV. A run is QUALIFIED
only when `UrllibTransport` completed the real Google path at that exact
profile. Fixture and fake transports stay UNQUALIFIED, evidence stays
WAITING, and acceptance/release are never promoted.

No client file is `GOOGLE_OAUTH_NOT_CONFIGURED` with evidence state
WAITING. This module does not write host records or claim a real run.
"""
from pathlib import Path
import hashlib
import json
import os
import platform
import sys
import shutil
import subprocess
import time

from .core import FilmError, atomic_text
from .drive_oauth import (CLIENT_DEPLOY_OWNER, CLIENT_FILE_ENV, AuthError,
                          assert_no_secrets, client_file_path, connect_google,
                          load_desktop_client, redact)
from .google_transport import MemoryDriveTransport, UrllibTransport

REPO = Path(__file__).resolve().parents[1]
TARGET_SECONDS = 240
TARGET_FPS = 24
TARGET_WIDTH = 1920
TARGET_HEIGHT = 1080
TARGET_FRAMES = TARGET_SECONDS * TARGET_FPS
TARGET_PROFILE = {
    "duration_s": TARGET_SECONDS,
    "fps": {"num": TARGET_FPS, "den": 1},
    "width": TARGET_WIDTH,
    "height": TARGET_HEIGHT,
    "output_frames": TARGET_FRAMES,
    "delivery_profile": "MV_H264_AAC_V1",
    "storage_profile": "DRIVE_BOUNDED",
    "transfer_route": "COORDINATOR_RELAY",
    "coordinator_location": "USER_DESKTOP",
    "worker_route": "LOCAL_NATIVE",
    "direct_drive": False,
}
# Raster the local worker would hold for the target profile (RGBA8).
# A smaller scratch does not downscale the profile; the run stops.
TARGET_RASTER_BYTES = TARGET_WIDTH * TARGET_HEIGHT * 4 * TARGET_FRAMES
DEFAULT_SCRATCH_LIMIT = 256 * 1024 * 1024
FIXTURE_WIDTH = 16
FIXTURE_HEIGHT = 16
FIXTURE_FRAMES = 2
EVOLUTION_SECTION_5 = {
    "document": "docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md",
    "section": "5",
    "candidate_commit": "09e161caa652d75e9617caf632b3b9899be35740",
    "approved_plan_commit": None,
}
MISSING = (
    "FILM_GOOGLE_OAUTH_CLIENT_FILE (Google Desktop app client JSON outside the repo)",
    "interactive consent (PKCE S256, state, drive.file, 127.0.0.1 loopback)",
    "attended real run: 240s/24fps/1920x1080 Drive → local CPU worker → verified PNG/MP4 → archive → replay → offline restore",
)
RETRY = {"max_retries": 2, "backoff_base_ms": 0, "max_backoff_ms": 1,
         "max_elapsed_ms": 5000, "max_requests": 8,
         "max_transferred_bytes": 50_000_000}


def draft_qualification():
    """In-repo record. A real run is not claimed and merge/DONE is not set."""
    return {
        "node_id": "anim-018",
        "node_state": "WAITING",
        "qualification_state": "UNQUALIFIED",
        "acceptance_state": "PENDING",
        "release_state": "NOT_AUTHORIZED",
        "evidence_state": "WAITING",
        "missing": list(MISSING),
        "client_deploy_owner": CLIENT_DEPLOY_OWNER,
        "fake_counts_as_qualification": False,
        "real_run_claimed": False,
        "real_route_completed": False,
        "user_merge": True,
        "astra_auto_merge": False,
        "changes_existing_project_defaults": False,
        "evolution_section_5": dict(EVOLUTION_SECTION_5),
        "target_profile": dict(TARGET_PROFILE),
    }


def archive_panel_caption():
    """Desktop archive panel: fake success is not this node's qualification."""
    q = draft_qualification()
    line = (f"ANIM-018 · evidence {q['evidence_state']} · "
            f"qualification {q['qualification_state']} · "
            f"acceptance {q['acceptance_state']} · "
            f"release {q['release_state']} · "
            f"node {q['node_state']}. "
            "fake 연결은 이 자격을 대신하지 않습니다. "
            "참석 실행은 remote-qualify 입니다.")
    if os.environ.get(CLIENT_FILE_ENV):
        return line + " OAuth client file이 설정되어 있습니다. 자격은 참석 실행 전입니다."
    return line + " GOOGLE_OAUTH_NOT_CONFIGURED"


def qualification_for(*, real_transport, oauth_completed, profile_exact,
                      stages_ok):
    """QUALIFIED only for a finished real Google run at the exact profile.

    A real token exchange without that profile is PARTIAL (oauth scope
    only). Everything else, including a passing fixture, is UNQUALIFIED.
    """
    if real_transport and oauth_completed and profile_exact and stages_ok:
        return "QUALIFIED"
    if real_transport and oauth_completed:
        return "PARTIAL"
    return "UNQUALIFIED"


def facets_for(qualification_state):
    """node/acceptance/release stay put. Evidence is RECORDED only when
    the integration scope itself is QUALIFIED."""
    return {"node_state": "WAITING",
            "qualification_state": qualification_state,
            "acceptance_state": "PENDING",
            "release_state": "NOT_AUTHORIZED",
            "evidence_state": "RECORDED"
            if qualification_state == "QUALIFIED" else "WAITING"}


def target_preflight(scratch_limit_bytes):
    """Stop before allocating the target raster when it does not fit.

    The profile is not downscaled to fit the scratch limit.
    """
    blocked = TARGET_RASTER_BYTES > scratch_limit_bytes
    return {"status": "CAPACITY_BLOCKED" if blocked else "OK",
            "required_raster_bytes": TARGET_RASTER_BYTES,
            "scratch_limit_bytes": scratch_limit_bytes,
            "executed_frames": 0 if blocked else TARGET_FRAMES,
            "downscaled": False}


def default_evidence_dir():
    return Path.home() / ".film-unit" / "anim-018-evidence"


def evidence_dir_ok(path):
    path = Path(path).expanduser().resolve()
    repo = REPO.resolve()
    if path == repo or repo in path.parents:
        raise FilmError(
            "EVIDENCE_PATH_REJECTED: anim-018 evidence is written outside "
            "the repository")
    return path


def source_sha():
    try:
        out = subprocess.check_output(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    return out.decode().strip()


def _installed_client_secret():
    """The Desktop client secret, for the evidence denylist only."""
    path = client_file_path()
    if not path:
        return ""
    return load_desktop_client(path)["client_secret"]


def _runtime():
    ffmpeg_line = None
    try:
        from .core import run
        ffmpeg_line = run(["ffmpeg", "-version"], timeout=20).decode().splitlines()[0]
    except (FilmError, OSError, IndexError, subprocess.TimeoutExpired):
        ffmpeg_line = None
    # `resource` is Unix-only. The archive panel imports this module, so a
    # missing module must not crash the desktop app; peak RSS is then unknown.
    peak_rss = None
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports ru_maxrss in kilobytes; macOS and Windows use bytes.
        peak_rss = rss * 1024 if sys.platform == "linux" else rss
    except (ImportError, AttributeError, OSError, ValueError):
        peak_rss = None
    return {"python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor() or "unknown",
            "gpu": "hardware-unverified",
            "ffmpeg": ffmpeg_line or "UNAVAILABLE",
            "ffmpeg_path": shutil.which("ffmpeg"),
            "process_rss_bytes": peak_rss}


def _png(seed, width, height):
    import io
    from PIL import Image
    image = Image.new("RGBA", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x * 7 + seed * 31) % 256,
                            (y * 5 + seed * 17) % 256,
                            (x * y + seed * 11) % 256, 255)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _write_client(path, secret):
    path.write_text(json.dumps({
        "installed": {
            "client_id": "anim018-test.apps.googleusercontent.com",
            "client_secret": secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://127.0.0.1"]}}), encoding="utf-8")


def _scripted_consent(transport):
    from urllib.parse import parse_qs, urlparse
    captured = {}

    def browser(url):
        captured["url"] = url
        query = parse_qs(urlparse(url).query)
        captured["state"] = query["state"][0]

    from .drive_oauth import ScriptedLoopback
    loop = ScriptedLoopback(lambda: {"code": "anim018-code",
                                     "state": captured["state"]})
    return browser, loop, captured


def _worker(scratch, snapshot, recipe, frames, width, height):
    from .execution_plan import job_key, make_execution_plan
    from .execution_workers import Coordinator
    from .execution_workers.local import LocalWorker
    from .frame_stream import make_contract
    plan = make_execution_plan(
        snapshot, [{
            "operation_id": "op-anim018", "kind": "RENDER_RANGE",
            "output_range": [0, frames], "halo_ranges": [],
            "route": "LOCAL_NATIVE", "worker": "local-anim018",
            "recipe_digest": recipe, "runtime_contract": "python-deterministic-v1",
            "depends_on": []}],
        storage={"profile": "DRIVE_BOUNDED", "archive_manifest": None},
        execution={"policy": "FIXED_ROUTE", "allowed_routes": ["LOCAL_NATIVE"],
                   "capability_evidence_required": False,
                   "allow_additional_charges": False},
        encoding={"driver_policy": "FIXED", "allowed_drivers": ["FFMPEG"],
                  "delivery_profile": "MV_H264_AAC_V1",
                  "width": width, "height": height,
                  "fps": {"num": 24, "den": 1}, "output_frames": frames},
        workspace={"pc_cache_limit_bytes": 1 << 26,
                   "worker_scratch_limit_bytes": 1 << 26, "on_limit": "PAUSE"},
        transfer_route="COORDINATOR_RELAY",
        coordinator_location="USER_DESKTOP",
        transfer_edges=[],
        resource_reservations=[{"location": "USER_DESKTOP",
                                "peak_bytes": 1 << 26}])
    if "DIRECT_DRIVE" in plan["execution"]["allowed_routes"] \
            or plan["transfer_route"] == "DIRECT_DRIVE":
        raise FilmError("DIRECT_DRIVE is not an anim-018 route")
    coordinator = Coordinator(scratch / "coord")
    keys = coordinator.plan_jobs(plan)
    key = keys[0]
    if key != job_key(plan, plan["operations"][0]):
        raise FilmError("worker job key drifted")
    worker = LocalWorker("local-anim018", scratch_limit_bytes=1 << 26)
    contract = make_contract(width, height, frame_range=[0, frames])
    assert coordinator.reserve(key) == "RESERVED"
    coordinator.submit(worker, key, frame_contract=dict(contract))
    coordinator.collect(worker, key)
    if coordinator.jobs[key]["state"] != "OUTPUT_PENDING_VERIFY":
        raise FilmError("local worker did not reach output verification")
    state = coordinator.verify_outputs(key)
    return {"state": state,
            "qualification_state": worker.qualification_state,
            "route": "LOCAL_NATIVE",
            "job": coordinator.jobs[key],
            "worker": worker,
            "direct_drive": False}


def _relay_denied(worker, snapshot):
    from .execution_workers import check_packet, issue_grant
    from .frame_stream import make_contract
    nonce = hashlib.sha256(b"anim-018-relay").hexdigest()
    grant = issue_grant(
        issuer="coordinator", audience=worker.worker_id,
        peer=worker.expected_peer, connection_digest=worker.connection_digest,
        credential_epoch=worker.credential_epoch, job_key="job-anim018",
        attempt_id=1, snapshot_digest=snapshot, ranges=[[0, 2]],
        max_bytes=1 << 20, expires_at_ms=2 ** 62, nonce=nonce)
    packet = {"job_key": "job-anim018", "attempt_id": 1,
              "request_id": "req-anim018", "snapshot_digest": snapshot,
              "plan_revision": 1,
              "operation": {"recipe_digest": hashlib.sha256(b"anim-018").hexdigest()},
              "frame_contract": make_contract(8, 6, frame_range=[0, 2]),
              "grant": grant, "endpoint": worker.endpoint,
              "input_members": [], "input_object_digests": [],
              "output_range": [0, 2], "receipt_nonce": nonce,
              "input_bytes": 64}
    check_packet(packet)
    bad = dict(packet)
    bad["endpoint"] = "other-host"
    try:
        worker.authenticate(bad, 64, now_ms=0)
    except FilmError as exc:
        return "RELAY_AUTH_DENIED" in str(exc)
    return False


def _encode(folder, mp4, frames):
    from .core import ffmpeg
    ffmpeg(["-framerate", "24", "-start_number", "1",
            "-i", str(folder / "F_%06d.png"),
            "-frames:v", str(frames), "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-an", str(mp4)])
    return mp4.read_bytes()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _fail_closed(session, transport, archive_sha, manifest_id):
    """Failure fixtures on a side object. The sealed archive digest stays."""
    from .archive_manifest import bounded_read
    from .storage_backends import ArchiveRequestError
    backend = session.authorized
    side = b"side-bytes-anim018"
    backend.put_object("side-anim018", side)
    sleep = lambda ms: None
    report = {}

    def _window(fn):
        start = len(transport.calls)
        try:
            value = fn()
            error = None
        except (FilmError, ArchiveRequestError) as exc:
            value = None
            error = exc
        return value, error, transport.calls[start:]

    transport.push_fault("alt=media", status=429, reason="rate_limit",
                         retry_after=0)
    _, error, calls = _window(lambda: bounded_read(
        lambda: backend.get_range("side-anim018", 0, 4), RETRY, sleep))
    report["transport_retry_429"] = {
        "retried": error is None and sum(
            1 for call in calls if call["status"] == 429) == 1,
        "requests": len(calls),
        "transferred_bytes": sum(call["response_bytes"] for call in calls)}

    transport.push_fault("alt=media", status=401, reason="authError")
    _, error, calls = _window(lambda: bounded_read(
        lambda: backend.get_range("side-anim018", 0, 4), RETRY, sleep))
    report["auth_401"] = {"retried": len(calls) > 2, "requests": len(calls),
                          "rejected": isinstance(error, ArchiveRequestError)
                          and error.status == 401}

    transport.push_fault("alt=media", status=403,
                         reason="insufficientFilePermissions")
    _, error, calls = _window(lambda: bounded_read(
        lambda: backend.get_range("side-anim018", 0, 4), RETRY, sleep))
    report["permission_403"] = {
        "retried": len(calls) > 2,
        "requests": len(calls),
        "rejected": isinstance(error, ArchiveRequestError)
        and error.status == 403 and not error.retriable()}

    transport.push_fault("alt=media", status=403, reason="rateLimitExceeded")
    _, error, calls = _window(lambda: bounded_read(
        lambda: backend.get_range("side-anim018", 0, 4), RETRY, sleep))
    report["rate_limit_403"] = {
        "retried": error is None and any(call["status"] == 403 for call in calls),
        "requests": len(calls)}

    transport.push_fault("uploadType=resumable", status=503, reason="unavailable")
    before = len(transport.calls)
    create_error = None
    try:
        backend.create_upload_session("side-create-anim018", 4)
    except ArchiveRequestError as exc:
        create_error = exc
    report["create_not_retried"] = {
        "requests": len(transport.calls) - before,
        "rejected": create_error is not None and create_error.status == 503}

    payload = b"0123456789abcdef"
    session_id = backend.create_upload_session("resume-anim018", len(payload))
    transport.push_fault("/resumable/", drop=True)
    dropped = False
    try:
        backend.upload_chunk(session_id, 0, payload)
    except Exception as exc:
        from .storage_backends import ConnectionDropped
        dropped = isinstance(exc, ConnectionDropped)
    status = backend.upload_status(session_id)
    backend.upload_chunk(session_id, status["offset"], payload)
    done = backend.complete_upload(session_id)
    report["upload_resume"] = {
        "dropped": dropped, "resumed_from": status["offset"],
        "completed": done.get("object_id") == "resume-anim018",
        "duplicate_bytes": len(payload)}

    transport.flip_byte("side-anim018")
    tamper = None
    try:
        backend.get_object("side-anim018")
    except FilmError as exc:
        tamper = "INTEGRITY_FAILED" in str(exc)
    transport.flip_byte("side-anim018")
    stored = backend.get_object(manifest_id).body
    measured = hashlib.sha256(stored).hexdigest()
    report["md5_mismatch"] = {"rejected": bool(tamper),
                              "sealed_archive_sha_unchanged": measured == archive_sha,
                              "archive_sha256": measured}
    return report


def run_pipeline(session, transport, scratch, *, width, height, frames,
                 client_secret, scratch_limit_bytes):
    """Drive → local CPU worker → PNG/MP4 → archive → replay → restore.

    `profile_exact` is true only at 1920×1080×5760. Callers that pass a
    fixture size cannot be QUALIFIED.
    """
    from .drive_archive import archive_frames, restore_archive
    from .fav_pack import sha256_bytes
    from .workspace import Workspace
    scratch.mkdir(parents=True, exist_ok=True)
    stages = []
    peak_buffers = 0

    def stage(name, fn):
        nonlocal peak_buffers
        started = time.perf_counter()
        value = fn()
        stages.append({"stage": name,
                       "ms": round((time.perf_counter() - started) * 1000, 3)})
        return value

    pngs = stage("input_png", lambda: [_png(i, width, height) for i in range(frames)])
    peak_buffers = sum(map(len, pngs))
    folder = scratch / "frames"
    folder.mkdir()
    for index, data in enumerate(pngs):
        (folder / f"F_{index + 1:06d}.png").write_bytes(data)
    mp4_bytes = stage("encode_mp4", lambda: _encode(folder, scratch / "out.mp4", frames))
    peak_buffers = max(peak_buffers, len(mp4_bytes))
    snapshot = _sha(b"".join(pngs))
    recipe = hashlib.sha256(b"anim-018-recipe").hexdigest()
    worker = stage("worker", lambda: _worker(
        scratch, snapshot, recipe, frames, width, height))
    access = (session.token_store.load(session.connection_id) or {}).get("access_token")
    job_blob = json.dumps(worker["job"], default=str)
    token_in_worker = bool(access and access in job_blob)

    def _archive():
        members = [{"member_id": f"F{index:03d}", "frame_index": index, "data": data}
                   for index, data in enumerate(pngs)]
        meta = session.metadata()
        connection = {key: meta[key] for key in (
            "connection_id", "account_binding_digest", "credential_epoch")}
        return archive_frames(
            session.authorized, members, profile="DRIVE_BOUNDED",
            connection=connection, min_level="FULL_READBACK",
            sleep_fn=lambda ms: None)

    archived = stage("archive", _archive)
    mp4_id = f"sha256-{sha256_bytes(mp4_bytes)}"

    def _mp4():
        session.authorized.put_object(mp4_id, mp4_bytes)
        return session.authorized.get_object(mp4_id).body

    mp4_back = stage("archive_mp4", _mp4)
    pack_id = archived["pack_object_id"]

    def _timed_read():
        started = time.perf_counter()
        body = session.authorized.get_object(pack_id).body
        return body, (time.perf_counter() - started) * 1000

    cold_body, cold_ms = stage("cold_read", _timed_read)
    warm_body, warm_ms = stage("warm_read", _timed_read)
    workspace = Workspace(scratch / "cache", 32 << 20)
    dest = scratch / "restore"
    replay = scratch / "replay"

    def _restore(target):
        return restore_archive(
            session.authorized, archived["archive"], workspace, target,
            allowed_root=scratch, offline=True, whole_pack_cap=32 << 20,
            sleep_fn=lambda ms: None)

    restored = stage("offline_restore", lambda: _restore(dest))
    replayed = stage("replay", lambda: _restore(replay))
    names = [f"F_{index + 1:06d}.png" for index in range(frames)]
    restore_ok = [ (dest / name).read_bytes() == pngs[index] for index, name in enumerate(names)]
    replay_ok = [(replay / name).read_bytes() == pngs[index] for index, name in enumerate(names)]
    project_created = (scratch / "project.json").exists() or (scratch / "project.yaml").exists()
    relay_ok = _relay_denied(worker["worker"], snapshot)
    failures = stage("failure_injection", lambda: _fail_closed(
        session, transport, archived["archive_sha256"],
        archived["manifest_object_id"]))
    profile_exact = (width, height, frames) == (
        TARGET_WIDTH, TARGET_HEIGHT, TARGET_FRAMES)
    checks = {
        "worker_verified": worker["state"] == "VERIFIED",
        "token_absent": not token_in_worker,
        "relay_auth": relay_ok,
        "mp4_readback": mp4_back == mp4_bytes,
        "cold_warm_bytes": cold_body == warm_body,
        "restore": all(restore_ok),
        "replay": all(replay_ok),
        "offline": bool(restored["offline_capable"] and replayed["offline_capable"]),
        "no_project": not project_created,
        "full_readback": archived["achieved_level"] == "FULL_READBACK",
        "retry_429": failures["transport_retry_429"]["retried"],
        "auth_401": failures["auth_401"]["rejected"] and not failures["auth_401"]["retried"],
        "permission_403": failures["permission_403"]["rejected"]
        and not failures["permission_403"]["retried"],
        "rate_limit_403": failures["rate_limit_403"]["retried"],
        "create_once": failures["create_not_retried"]["rejected"]
        and failures["create_not_retried"]["requests"] == 1,
        "upload_resume": failures["upload_resume"]["dropped"]
        and failures["upload_resume"]["completed"],
        "md5_mismatch": failures["md5_mismatch"]["rejected"],
        "no_direct_drive": worker["direct_drive"] is False,
    }
    problems = [name for name, ok in checks.items() if not ok]
    stages_ok = not problems
    real_transport = isinstance(transport, UrllibTransport)
    oauth_completed = bool(getattr(session, "real_google", False))
    qualification = qualification_for(
        real_transport=real_transport, oauth_completed=oauth_completed,
        profile_exact=profile_exact, stages_ok=stages_ok)
    calls = transport.calls
    runtime = _runtime()
    disk = sum(path.stat().st_size for path in dest.rglob("*") if path.is_file())
    report = draft_qualification()
    report.update(facets_for(qualification))
    report.update({
        "source_sha": source_sha(),
        "protocol_fixture": "PASS" if stages_ok else "FAIL",
        "problems": problems,
        "real_route_completed": qualification == "QUALIFIED",
        "real_run_claimed": qualification == "QUALIFIED",
        "profile_exact": profile_exact,
        "executed": {"width": width, "height": height, "frames": frames,
                     "fps": TARGET_FPS, "delivery_profile": "MV_H264_AAC_V1"},
        "target_preflight": target_preflight(scratch_limit_bytes),
        "runtime": runtime,
        "device": {"cpu": runtime["processor"], "gpu": "hardware-unverified"},
        "toolchain": {"python": runtime["python"], "ffmpeg": runtime["ffmpeg"]},
        "delivery_profile": "MV_H264_AAC_V1",
        "input_snapshot": snapshot,
        "cold_warm": {"cold_ms": round(cold_ms, 3), "warm_ms": round(warm_ms, 3),
                      "samples": 1,
                      "note": "one pair recorded; not a promised speedup"},
        "stage_timeline": stages,
        "peak": {"pc_disk_bytes": disk,
                 "pc_ram_bytes": peak_buffers if runtime["process_rss_bytes"] is None
                 else max(peak_buffers, runtime["process_rss_bytes"]),
                 "vram_bytes": None,
                 "worker_scratch_bytes": width * height * 4 * frames,
                 "spool_bytes": len(cold_body)},
        "transfer": {"requests": len(calls),
                     "sent_bytes": sum(call["request_bytes"] for call in calls),
                     "received_bytes": sum(call["response_bytes"] for call in calls),
                     "archive_write_bytes": sum(map(len, pngs)) + len(mp4_bytes),
                     "archive_read_bytes": len(cold_body) + len(warm_body) + len(mp4_back),
                     "duplicate_bytes": failures["upload_resume"]["duplicate_bytes"]},
        "usage": {"additional_charges": False, "paid_calls": 0,
                  "drive_api_calls": len(calls)},
        "recovery_scope": failures,
        "credential_epoch": session.credential_epoch,
        "artifact_hashes": {"png": [_sha(data) for data in pngs],
                            "mp4": _sha(mp4_bytes),
                            "pack": archived["archive"]["pack"]["sha256"],
                            "archive": archived["archive_sha256"]},
        "worker": {"state": worker["state"], "route": worker["route"],
                   "qualification_state": worker["qualification_state"],
                   "token_in_worker_state": token_in_worker,
                   "relay_auth_redirect_refused": relay_ok},
        "archive": {"achieved_level": archived["achieved_level"],
                    "offline_restore": restored["offline_capable"],
                    "replay_without_project": all(replay_ok) and not project_created},
        "allowed_routes": ["LOCAL_NATIVE"],
        "transfer_route": "COORDINATOR_RELAY",
        "paid_calls": 0,
        "changes_existing_project_defaults": False,
    })
    # A fixture pass must not flip the in-repo draft's meaning: the
    # returned report is the run record, still WAITING unless QUALIFIED.
    if qualification != "QUALIFIED":
        report["evidence_state"] = "WAITING"
        report["real_run_claimed"] = False
        report["real_route_completed"] = False
    secrets = [client_secret, access, *(
        (session.token_store.load(session.connection_id) or {}).get(key)
        for key in ("access_token", "refresh_token"))]
    secrets.extend(session.backend.secret_material())
    return report, [item for item in secrets if item]


def run_fixture(scratch, *, scratch_limit_bytes=DEFAULT_SCRATCH_LIMIT):
    """Offline protocol double. Never QUALIFIED."""
    secret = "anim018-client-secret-value"
    _write_client(scratch / "client.json", secret)
    transport = MemoryDriveTransport()
    browser, loop, _captured = _scripted_consent(transport)
    session = connect_google(
        transport=transport, browser=browser, loopback=loop,
        client_file=str(scratch / "client.json"))
    if session.real_google or isinstance(transport, UrllibTransport):
        raise FilmError("fixture transport must not count as real Google")
    report, secrets = run_pipeline(
        session, transport, scratch / "work", width=FIXTURE_WIDTH,
        height=FIXTURE_HEIGHT, frames=FIXTURE_FRAMES, client_secret=secret,
        scratch_limit_bytes=scratch_limit_bytes)
    if report["qualification_state"] == "QUALIFIED" or report["profile_exact"]:
        raise FilmError("fixture run refused: fake success is not QUALIFIED")
    secrets.extend(["anim018-test.apps.googleusercontent.com", "anim018-code",
                    secret])
    return report, secrets


def waiting_report(detail="GOOGLE_OAUTH_NOT_CONFIGURED"):
    report = draft_qualification()
    report.update({
        "source_sha": source_sha(),
        "protocol_fixture": "NOT_RUN",
        "detail": detail,
        "paid_calls": 0,
        "target_preflight": target_preflight(DEFAULT_SCRATCH_LIMIT),
        "runtime": _runtime(),
    })
    report["device"] = {"cpu": report["runtime"]["processor"],
                        "gpu": "hardware-unverified"}
    report["toolchain"] = {"python": report["runtime"]["python"],
                           "ffmpeg": report["runtime"]["ffmpeg"]}
    return report


def _markdown(report):
    facets = report
    lines = [
        "# ANIM-018 evidence",
        "",
        f"- source: `{facets.get('source_sha', 'UNKNOWN')}`",
        f"- node: {facets['node_state']}",
        f"- qualification: {facets['qualification_state']}",
        f"- acceptance: {facets['acceptance_state']}",
        f"- release: {facets['release_state']}",
        f"- evidence: {facets['evidence_state']}",
        f"- real route completed: {facets.get('real_route_completed', False)}",
        f"- deploy owner: {facets['client_deploy_owner']}",
        "",
        "Missing for a real qualification:",
    ]
    for item in facets.get("missing", []):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("Fake or fixture success does not qualify this node and "
                 "does not authorize a work print or a release.")
    lines.append("")
    return "\n".join(lines)


def write_evidence(directory, report, secrets=()):
    directory = evidence_dir_ok(directory)
    directory.mkdir(parents=True, exist_ok=True)
    clean = redact(report)
    text = json.dumps(clean, indent=2, ensure_ascii=False) + "\n"
    markdown = _markdown(clean)
    assert_no_secrets(text + markdown, secrets)
    atomic_text(directory / "anim018-evidence.json", text)
    atomic_text(directory / "anim018-evidence.md", markdown)
    return clean


def run_attended(scratch_limit_bytes):
    """Real installed-app route. Capacity block does not downscale."""
    pre = target_preflight(scratch_limit_bytes)
    try:
        session = connect_google()
    except (FilmError, AuthError) as exc:
        detail = getattr(exc, "code", None) or "GOOGLE_OAUTH_NOT_CONFIGURED"
        report = waiting_report(detail)
        report["target_preflight"] = pre
        return report, []
    if pre["status"] != "OK":
        report = waiting_report("CAPACITY_BLOCKED")
        report["target_preflight"] = pre
        report.update(facets_for(qualification_for(
            real_transport=True,
            oauth_completed=bool(session.real_google),
            profile_exact=False, stages_ok=False)))
        report["executed"] = {"width": 0, "height": 0, "frames": 0}
        report["real_route_completed"] = False
        report["real_run_claimed"] = False
        report["evidence_state"] = "WAITING"
        token = session.token_store.load(session.connection_id) or {}
        secret = _installed_client_secret()
        return report, [item for item in (
            secret, token.get("access_token"), token.get("refresh_token"))
            if item]
    import tempfile
    secret = _installed_client_secret()
    with tempfile.TemporaryDirectory(prefix="anim018-") as tmp:
        report, secrets = run_pipeline(
            session, session.backend.transport, Path(tmp),
            width=TARGET_WIDTH, height=TARGET_HEIGHT, frames=TARGET_FRAMES,
            client_secret=secret, scratch_limit_bytes=scratch_limit_bytes)
    return report, secrets


def run_remote_qualify(evidence_dir=None, *, fixture=False,
                       scratch_limit_bytes=DEFAULT_SCRATCH_LIMIT):
    """Write redacted evidence. Missing client file is WAITING, exit-safe."""
    directory = evidence_dir_ok(evidence_dir or default_evidence_dir())
    secrets = []
    if fixture:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="anim018-fixture-") as tmp:
            report, secrets = run_fixture(
                Path(tmp), scratch_limit_bytes=scratch_limit_bytes)
    elif not os.environ.get(CLIENT_FILE_ENV):
        report = waiting_report()
    else:
        report, secrets = run_attended(scratch_limit_bytes)
    return write_evidence(directory, report, secrets)
