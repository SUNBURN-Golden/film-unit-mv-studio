#!/usr/bin/python3 -I
"""Installed, protected host admission boundary. No scheduler or automatic release."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

POLICY_PATH = Path("/etc/astra/control-plane-host.json")
INSTALLED_PATH = Path("/opt/astra/bin/astra-host-control")
WRAPPERS = {"DEVIN": "/opt/astra/bin/astra-builder-devin",
            "GROK_BUILD": "/opt/astra/bin/astra-builder-grok-build",
            "GLM": "/opt/astra/bin/astra-builder-glm"}
IDENTITY = ("repository", "task_id", "task_revision", "builder_id", "launch_request_id", "attempt_id")
ACTIVE = ("SUBMITTING", "CONFIRMED", "UNKNOWN")
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


class HostError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise HostError("duplicate JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(HostError("nonfinite JSON")))
        if not isinstance(value, dict):
            raise HostError("expected one JSON object")
        return value
    except (ValueError, TypeError) as exc:
        raise HostError("invalid JSON") from exc


def evidence_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        return (parsed.scheme in {"http", "https"} and bool(host) and not parsed.username
                and host not in {"localhost", "example.com", "example.org", "example.net"}
                and not host.endswith((".invalid", ".example")) and not any(c.isspace() for c in value))
    except ValueError:
        return False


def protected_leaf(path, uid, *, directory=False, mode=None):
    info = path.lstat()
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or info.st_uid != uid or info.st_mode & 0o022:
        raise HostError(f"unsafe ownership/type/permissions: {path}")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise HostError(f"required mode {mode:o}: {path}")
    if not directory and info.st_nlink != 1:
        raise HostError(f"hard-linked protected file: {path}")


def protected_root_path(path, *, directory=False):
    if not path.is_absolute() or ".." in path.parts:
        raise HostError("protected paths must be absolute and normalized")
    for parent in reversed(path.parents):
        protected_leaf(parent, 0, directory=True)
    protected_leaf(path, 0, directory=directory)


def validate_policy(policy):
    ids = [policy.get("control_uid"), policy.get("runner_uid")]
    builders = policy.get("builder_uids")
    if not isinstance(builders, dict) or set(builders) != set(WRAPPERS):
        raise HostError("builder_uids must name all three builders")
    ids.extend(builders.values())
    if any(type(uid) is not int or uid <= 0 for uid in ids) or len(set(ids)) != len(ids):
        raise HostError("control, runner and builder Unix UIDs must be distinct and non-root")
    for name in ("max_active_sessions", "max_launches_per_24h"):
        if type(policy.get(name)) is not int or policy[name] < 1:
            raise HostError(f"{name} must be a positive integer")
    repos, enabled = policy.get("allowed_repositories"), policy.get("enabled_builders")
    if (not isinstance(repos, list) or not repos or
            any(not isinstance(repo, str) or len(repo.split("/")) != 2 or not all(repo.split("/")) for repo in repos)):
        raise HostError("allowed_repositories must contain owner/repository names")
    if (not isinstance(enabled, list) or not enabled or
            any(builder not in WRAPPERS for builder in enabled)):
        raise HostError("enabled_builders must be a nonempty subset of the three builders")
    if policy.get("wrapper_paths") != WRAPPERS:
        raise HostError("wrapper_paths must match fixed installed adapter paths")
    if not evidence_url(policy.get("boundary_evidence_pointer")):
        raise HostError("provisioned boundary evidence URL is required")
    path = policy.get("ledger_path")
    if not isinstance(path, str) or not Path(path).is_absolute() or ".." in Path(path).parts:
        raise HostError("ledger_path must be absolute and normalized")


def authorize_identity(policy, command):
    if os.getuid() != policy["control_uid"] or os.geteuid() != policy["control_uid"]:
        raise HostError("helper must run as the configured non-root control identity")
    caller = os.environ.get("SUDO_UID", "")
    if not caller.isdecimal():
        raise HostError("helper requires a sudo-authenticated caller")
    caller = int(caller)
    if caller in policy["builder_uids"].values() or caller == policy["control_uid"]:
        raise HostError("builder/control identities cannot invoke admission commands")
    if command in {"init", "reconcile"} and caller == policy["runner_uid"]:
        raise HostError("operator-only command; runner is forbidden")


def load_host_policy(command):
    protected_root_path(POLICY_PATH)
    policy = parse_json(POLICY_PATH.read_text(encoding="utf-8"))
    validate_policy(policy)
    authorize_identity(policy, command)
    if Path(__file__).absolute() != INSTALLED_PATH:
        raise HostError("helper must execute from its protected installed path")
    protected_root_path(INSTALLED_PATH)
    for builder in policy["enabled_builders"]:
        wrapper = WRAPPERS[builder]
        protected_root_path(Path(wrapper))
        if not os.access(wrapper, os.X_OK):
            raise HostError(f"adapter is not executable: {wrapper}")
    ledger = Path(policy["ledger_path"])
    protected_root_path(ledger.parent.parent, directory=True)
    protected_leaf(ledger.parent, policy["control_uid"], directory=True, mode=0o700)
    for file in [ledger, *(Path(str(ledger) + suffix) for suffix in ("-journal", "-wal", "-shm", ".inflight.lock"))]:
        if command == "init" and file == ledger and not file.exists() and not file.is_symlink():
            continue
        if file == ledger or file.exists() or file.is_symlink():
            protected_leaf(file, policy["control_uid"], mode=0o600)
    return policy


def validate_packet(packet, policy):
    if type(packet.get("schema_version")) is not int or packet["schema_version"] != 1:
        raise HostError("unsupported launch packet schema")
    for field in IDENTITY[:-1]:
        value = packet.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 4096 or "\0" in value:
            raise HostError(f"invalid packet field: {field}")
    if type(packet.get("attempt_id")) is not int or packet["attempt_id"] < 1:
        raise HostError("attempt_id must be a positive integer")
    if packet["repository"] not in policy["allowed_repositories"]:
        raise HostError("repository is not admitted by host policy")
    if packet["builder_id"] not in policy["enabled_builders"]:
        raise HostError("builder is not enabled by host policy")
    canonical(packet)


def result_for(packet, outcome, reason=None, session_id=None):
    result = {key: packet[key] for key in IDENTITY}
    result.update(outcome=outcome, session_id=session_id)
    if reason:
        result["reason"] = reason
    return result


class Ledger:
    """Filesystem policy is checked by CLI; this class also supports isolated unit tests."""
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock

    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA trusted_schema=OFF")
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] != 1 or db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise HostError("ledger schema unavailable or corrupt")
            db.execute("SELECT packet, state, result, admitted, created, evidence FROM launches LIMIT 0")
        except Exception:
            db.close()
            raise
        return db

    def initialize(self):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        db = sqlite3.connect(str(self.path), isolation_level=None)
        try:
            db.executescript("""
                PRAGMA synchronous=FULL;
                BEGIN IMMEDIATE;
                CREATE TABLE launches (
                    request TEXT PRIMARY KEY, repository TEXT NOT NULL, task TEXT NOT NULL,
                    packet TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
                    ('SUBMITTING','CONFIRMED','UNKNOWN','FAILED_PRESTART','RECONCILED')),
                    result TEXT NOT NULL, admitted INTEGER NOT NULL CHECK(admitted IN (0,1)),
                    created REAL NOT NULL, evidence TEXT);
                CREATE UNIQUE INDEX one_active_task ON launches(repository, task)
                    WHERE state IN ('SUBMITTING','CONFIRMED','UNKNOWN');
                PRAGMA user_version=1;
                COMMIT;
            """)
        finally:
            db.close()

    @contextmanager
    def inflight_lock(self, *, exclusive=False):
        # Shared across launch invocations; reconciliation requires every invocation
        # to have stopped sending before an operator can release an unresolved slot.
        fd = os.open(str(self.path) + ".inflight.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise HostError("unsafe in-flight lock file")
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HostError("launch or operator reconciliation is still in flight") from exc
            yield fd
        finally:
            os.close(fd)

    def reserve(self, packet, policy):
        validate_packet(packet, policy)
        body, request = canonical(packet), packet["launch_request_id"]
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM launches WHERE request=?", (request,)).fetchone()
            if previous:
                if previous["packet"] != body:
                    raise HostError("launch_request_id reused with a different packet")
                db.commit()
                return False, parse_json(previous["result"])
            active = "state IN ('SUBMITTING','CONFIRMED','UNKNOWN')"
            reason = None
            if db.execute(f"SELECT 1 FROM launches WHERE repository=? AND task=? AND {active}",
                          (packet["repository"], packet["task_id"])).fetchone():
                reason = "task already has an active or unresolved owner"
            elif db.execute(f"SELECT count(*) FROM launches WHERE {active}").fetchone()[0] >= policy["max_active_sessions"]:
                reason = "host max_active_sessions reached"
            elif db.execute("SELECT count(*) FROM launches WHERE admitted=1 AND created>=?",
                            (self.clock() - 86400,)).fetchone()[0] >= policy["max_launches_per_24h"]:
                reason = "host max_launches_per_24h reached"
            state = "FAILED_PRESTART" if reason else "SUBMITTING"
            result = result_for(packet, "FAILED_PRESTART" if reason else "UNKNOWN",
                                reason or "durable SUBMITTING reservation; outcome unresolved")
            db.execute("INSERT INTO launches VALUES (?,?,?,?,?,?,?,?,NULL)",
                       (request, packet["repository"], packet["task_id"], body, state,
                        canonical(result), int(reason is None), self.clock()))
            db.commit()  # FULL synchronous commit precedes every provider invocation.
            return reason is None, result
        finally:
            db.close()

    def finalize(self, packet, result):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE launches SET state=?, result=? WHERE request=? AND packet=? AND state='SUBMITTING'",
                                 (result["outcome"], canonical(result), packet["launch_request_id"], canonical(packet))).rowcount
            if changed != 1:
                raise HostError("reservation changed before finalization; operator reconciliation required")
            db.commit()
        finally:
            db.close()

    def reconcile(self, request, session, evidence, *, no_session=False, sender_fenced=False):
        with self.inflight_lock(exclusive=True):
            return self._reconcile(request, session, evidence, no_session=no_session, sender_fenced=sender_fenced)

    def _reconcile(self, request, session, evidence, *, no_session=False, sender_fenced=False):
        if not evidence_url(evidence):
            raise HostError("operator-verified terminal session and durable evidence URL required")
        if no_session:
            if session is not None or sender_fenced is not True:
                raise HostError("no-session reconciliation requires explicit sender fencing and no session ID")
        elif not isinstance(session, str) or not session.strip():
            raise HostError("terminal session ID is required")
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM launches WHERE request=?", (request,)).fetchone()
            if row is None or row["state"] not in ACTIVE:
                raise HostError("no active reservation to reconcile")
            known = parse_json(row["result"]).get("session_id")
            if no_session and (known is not None or row["state"] == "CONFIRMED"):
                raise HostError("known session cannot be reconciled as no-session")
            if not no_session and known is not None and known != session:
                raise HostError("reconciliation session does not match recorded owner")
            resolution = "NO_SESSION_CONFIRMED" if no_session else "SESSION_TERMINAL"
            db.execute("UPDATE launches SET state='RECONCILED', evidence=? WHERE request=?",
                       (canonical({"resolution": resolution, "session_id": session,
                                   "sender_fenced": sender_fenced, "terminal_evidence": evidence,
                                   "at": self.clock()}), request))
            db.commit()
            return {"status": "RECONCILED", "resolution": resolution, "launch_request_id": request,
                    "session_id": session, "evidence": evidence}
        finally:
            db.close()


def adapter_launch(packet, policy, lock_fd):
    # Packet stays within the control-owned directory, never the caller's workspace.
    with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", prefix="packet-",
                                     dir=Path(policy["ledger_path"]).parent) as packet_file:
        packet_file.write(canonical(packet))
        packet_file.flush()
        return subprocess.run([WRAPPERS[packet["builder_id"]], packet_file.name],
                              capture_output=True, text=True, timeout=180, cwd="/", pass_fds=(lock_fd,),
                              env={**CLEAN_ENV, "ASTRA_HOST_INFLIGHT_FD": str(lock_fd)})


def launch(packet, policy, ledger, invoke=None):
    with ledger.inflight_lock() as lock_fd:
        return launch_held(packet, policy, ledger, invoke, lock_fd)


def launch_held(packet, policy, ledger, invoke, lock_fd):
    admitted, previous = ledger.reserve(packet, policy)
    if not admitted:
        return previous
    try:
        completed = adapter_launch(packet, policy, lock_fd) if invoke is None else invoke(packet, policy)
        if completed.returncode != 0:
            raise HostError("adapter exited nonzero; launch outcome ambiguous")
        result = parse_json(completed.stdout)
        if any(type(result.get(key)) is not type(packet[key]) or result.get(key) != packet[key] for key in IDENTITY):
            raise HostError("adapter result identity mismatch")
        outcome, session = result.get("outcome"), result.get("session_id")
        if outcome not in {"CONFIRMED", "FAILED_PRESTART", "UNKNOWN"}:
            raise HostError("adapter returned invalid outcome")
        if session is not None and (not isinstance(session, str) or not session.strip()):
            raise HostError("adapter returned invalid session_id")
        if result.get("reason") is not None and not isinstance(result["reason"], str):
            raise HostError("adapter returned invalid reason")
        if outcome == "CONFIRMED" and (not isinstance(session, str) or not session.strip()):
            raise HostError("adapter confirmation lacks actual session_id")
        if outcome == "FAILED_PRESTART" and (session is not None or not isinstance(result.get("reason"), str) or not result["reason"].strip()):
            raise HostError("adapter FAILED_PRESTART lacks definite prestart reason or claims a session")
        result = result_for(packet, outcome, result.get("reason"), session)
    except Exception as exc:
        result = result_for(packet, "UNKNOWN", f"adapter outcome unresolved: {type(exc).__name__}: {exc}")
    ledger.finalize(packet, result)
    return result


def preflight(builder, policy, ledger):
    if builder not in policy["enabled_builders"]:
        raise HostError("builder is not enabled by host policy")
    db = ledger.connect()
    db.close()
    completed = subprocess.run([WRAPPERS[builder], "--preflight"], capture_output=True,
                               text=True, timeout=90, cwd="/", env=CLEAN_ENV)
    report = parse_json(completed.stdout)
    if (completed.returncode != 0 or report.get("status") != "PASS" or report.get("builder_id") != builder
            or report.get("execution_mode") not in {"REMOTE_SESSION", "PERSISTENT_SUPERVISOR"}
            or report.get("parallel_safe") is not True or type(report.get("launch_contract_version")) is not int
            or report["launch_contract_version"] != 2):
        raise HostError("adapter preflight failed")
    report.update(host_admission="ENFORCED", boundary_evidence_pointer=policy["boundary_evidence_pointer"],
                  allowed_repositories=policy["allowed_repositories"],
                  helper_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight").add_argument("--builder-id", choices=tuple(WRAPPERS), required=True)
    commands.add_parser("launch")
    commands.add_parser("init")
    reconcile_parser = commands.add_parser("reconcile")
    for flag in ("launch-request-id", "evidence"):
        reconcile_parser.add_argument("--" + flag, required=True)
    outcome = reconcile_parser.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--session-id")
    outcome.add_argument("--no-session", action="store_true")
    reconcile_parser.add_argument("--sender-fenced", action="store_true")
    args, packet = parser.parse_args(argv), None
    os.umask(0o077)
    try:
        if args.command == "launch":
            raw = sys.stdin.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise HostError("launch packet too large")
            packet = parse_json(raw)
        policy = load_host_policy(args.command)
        ledger = Ledger(policy["ledger_path"])
        if args.command == "init":
            ledger.initialize()
            result = {"status": "INITIALIZED"}
        elif args.command == "preflight":
            result = preflight(args.builder_id, policy, ledger)
        elif args.command == "launch":
            result = launch(packet, policy, ledger)
        else:
            result = ledger.reconcile(args.launch_request_id, args.session_id, args.evidence,
                                      no_session=args.no_session, sender_fenced=args.sender_fenced)
        print(canonical(result))
        return 0
    except (HostError, OSError, sqlite3.Error, ValueError, TypeError, subprocess.SubprocessError) as exc:
        print(f"HOST_CONTROL_ERROR: {exc}", file=sys.stderr)
        result = {"status": "ERROR", "reason": str(exc)}
        if args.command == "launch":
            result.update({key: packet[key] for key in IDENTITY if isinstance(packet, dict) and key in packet})
            result.update(outcome="UNKNOWN", session_id=None)
        print(canonical(result))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
