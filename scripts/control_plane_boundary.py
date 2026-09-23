#!/usr/bin/env python3
"""Trusted-workflow admission boundary for self-hosted control runners.

This module is executed by the ACTIONS_RUNNER_HOOK_JOB_STARTED pre-job hook
(control_plane_boundary_hook.sh) before any job step, action pre-entrypoint or
job container is initialized.  It admits a job only when every pinned claim of
one policy rule matches the claims supplied by the runner process environment
and the webhook payload file (GITHUB_EVENT_PATH).

Claims come only from the runner-supplied environment (GITHUB_*/RUNNER_*
names) and the server-generated event payload; workflow `env`, task text and
arbitrary process environment never authorize a job.  Every failure is a deny:
missing policy, digest mismatch, malformed event, absent claim or no matching
rule all refuse the job.

A nonzero exit alone does not stop `if: always()` steps (the hook is injected
as one pre-job step), so the hook script terminates the Runner.Worker process
after a deny decision; `deny-job` implements that lookup.  `verify-install`
checks the installed hook/policy digests, modes and environment wiring.

Subcommands: enforce, deny-job, worker-pid, verify-install, check-env,
policy-digest, claims, self-test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

POLICY_ENV = "ASTRA_BOUNDARY_POLICY"
POLICY_SHA_ENV = "ASTRA_BOUNDARY_POLICY_SHA256"
EVIDENCE_ENV = "ASTRA_BOUNDARY_EVIDENCE_DIR"
HOOK_ENV = "ACTIONS_RUNNER_HOOK_JOB_STARTED"

ENV_CLAIM_RE = re.compile(r"^(GITHUB|RUNNER)_[A-Z0-9_]+$")
EVENT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
CLAIM_RE = re.compile(r"^(env|event):(.+)$")
POLICY_ID_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
SCALAR = (str, int, bool)

DENY = 2
ERROR = 3


class BoundaryError(RuntimeError):
    pass


def parse_json(text: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise BoundaryError("duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(BoundaryError("nonfinite JSON")))
    except (ValueError, TypeError) as exc:
        raise BoundaryError("invalid JSON") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def valid_claim_ref(ref: Any) -> bool:
    if not isinstance(ref, str):
        return False
    match = CLAIM_RE.match(ref)
    if not match:
        return False
    kind, rest = match.groups()
    if kind == "env":
        return bool(ENV_CLAIM_RE.match(rest))
    if not rest or rest.startswith(".") or rest.endswith("."):
        return False
    return all(EVENT_SEGMENT_RE.match(part) for part in rest.split("."))


def load_policy(path: Path) -> Dict[str, Any]:
    try:
        policy = parse_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BoundaryError(f"policy file missing: {path}") from exc
    except OSError as exc:
        raise BoundaryError(f"policy unreadable: {exc}") from exc
    if not isinstance(policy, dict):
        raise BoundaryError("policy must be one JSON object")
    allowed = {"schema_version", "policy_id", "description", "required_claims", "allow"}
    extra = set(policy) - allowed
    if extra:
        raise BoundaryError(f"unknown policy keys: {sorted(extra)}")
    if policy.get("schema_version") != 1:
        raise BoundaryError("unsupported policy schema_version")
    if not isinstance(policy.get("policy_id"), str) or not POLICY_ID_RE.match(policy["policy_id"]):
        raise BoundaryError("policy_id must match %s" % POLICY_ID_RE.pattern)
    if "description" in policy and not isinstance(policy["description"], str):
        raise BoundaryError("description must be a string")
    required = policy.get("required_claims", [])
    if not isinstance(required, list) or any(not valid_claim_ref(r) for r in required):
        raise BoundaryError("required_claims must be a list of valid claim references")
    rules = policy.get("allow")
    if not isinstance(rules, list) or not rules:
        raise BoundaryError("allow must be a nonempty list of rules")
    seen_names = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - {"name", "claims"}:
            raise BoundaryError("each rule may only contain name and claims")
        name = rule.get("name")
        if not isinstance(name, str) or not POLICY_ID_RE.match(name) or name in seen_names:
            raise BoundaryError("rule names must be unique policy-id-safe strings")
        seen_names.add(name)
        claims = rule.get("claims")
        if not isinstance(claims, dict) or not claims:
            raise BoundaryError("rule claims must be a nonempty object")
        for ref, expected in claims.items():
            if not valid_claim_ref(ref):
                raise BoundaryError(f"invalid claim reference: {ref!r}")
            if isinstance(expected, list):
                if not expected or any(not isinstance(v, SCALAR) or isinstance(v, float) for v in expected):
                    raise BoundaryError(f"rule {name}: claim {ref} list must hold scalars")
            elif not isinstance(expected, SCALAR) or isinstance(expected, float):
                raise BoundaryError(f"rule {name}: claim {ref} must be a scalar or scalar list")
    return policy


def resolve_event(event: Any, path: str) -> Any:
    node = event
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                return None
            node = node[part]
        elif isinstance(node, list) and part.isdecimal():
            index = int(part)
            if index >= len(node):
                return None
            node = node[index]
        else:
            return None
    return node


def collect_claims(env: Dict[str, str], event: Any) -> Dict[str, Any]:
    claims = {}
    for key, value in env.items():
        if ENV_CLAIM_RE.match(key):
            claims[f"env:{key}"] = value
    if isinstance(event, dict):
        def walk(node, prefix):
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{prefix}.{k}" if prefix else k)
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{prefix}.{i}")
            elif isinstance(node, SCALAR) and not isinstance(node, float):
                claims[f"event:{prefix}"] = node
        walk(event, "")
    return claims


def load_event(path: Optional[str]) -> Any:
    if not path:
        return None
    try:
        return parse_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, BoundaryError):
        return None


def evaluate(policy: Dict[str, Any], claims: Dict[str, Any]) -> Tuple[bool, Optional[str], List[str]]:
    """Return (allowed, matched_rule_name, reasons). Deny on any gap."""
    for ref in policy.get("required_claims", []):
        if claims.get(ref) in (None, ""):
            return False, None, [f"required claim absent: {ref}"]
    reasons = []
    for rule in policy["allow"]:
        mismatches = []
        for ref, expected in rule["claims"].items():
            observed = claims.get(ref)
            options = expected if isinstance(expected, list) else [expected]
            if observed is None or observed == "":
                mismatches.append(f"{ref}: absent")
            elif not any(type(observed) is type(opt) and observed == opt for opt in options):
                mismatches.append(f"{ref}: {observed!r} not in {options!r}")
        if not mismatches:
            return True, rule["name"], []
        reasons.append(f"rule {rule['name']}: " + "; ".join(mismatches))
    return False, None, reasons


def referenced_claims(policy: Dict[str, Any]) -> List[str]:
    refs = set(policy.get("required_claims", []))
    for rule in policy["allow"]:
        refs.update(rule["claims"])
    base = ["env:GITHUB_RUN_ID", "env:GITHUB_RUN_ATTEMPT", "env:GITHUB_JOB",
            "env:GITHUB_EVENT_NAME", "env:GITHUB_REPOSITORY", "env:GITHUB_REF",
            "env:GITHUB_SHA", "env:GITHUB_WORKFLOW_REF", "env:RUNNER_NAME"]
    return sorted(refs | set(base))


def write_record(evidence_dir: Path, record: Dict[str, Any]) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    info = os.stat(evidence_dir)
    if info.st_mode & 0o002:
        raise BoundaryError("evidence directory must not be world-writable")
    run_id = re.sub(r"[^A-Za-z0-9._-]", "_", str(record.get("run_id") or "norun"))
    job = re.sub(r"[^A-Za-z0-9._-]", "_", str(record.get("job") or "nojob"))
    target = evidence_dir / f"decision-{int(time.time())}-{run_id}-{job}-{os.getpid()}.json"
    target.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def cmd_enforce(args: argparse.Namespace) -> int:
    env = os.environ
    policy_path = args.policy or env.get(POLICY_ENV)
    expected_sha = args.policy_sha256 or env.get(POLICY_SHA_ENV)
    evidence_dir = args.evidence_dir or env.get(EVIDENCE_ENV)
    record: Dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "decision": "DENY",
        "run_id": env.get("GITHUB_RUN_ID"),
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
        "job": env.get("GITHUB_JOB"),
        "event_name": env.get("GITHUB_EVENT_NAME"),
        "repository": env.get("GITHUB_REPOSITORY"),
        "ref": env.get("GITHUB_REF"),
        "sha": env.get("GITHUB_SHA"),
        "workflow_ref": env.get("GITHUB_WORKFLOW_REF"),
        "runner_name": env.get("RUNNER_NAME"),
        "reasons": [],
    }

    def finish(decision: str, reasons: List[str], extra: Optional[Dict[str, Any]] = None) -> int:
        record["decision"] = decision
        record["reasons"] = reasons
        if extra:
            record.update(extra)
        if evidence_dir:
            try:
                record_path = write_record(Path(evidence_dir), record)
                print(f"ASTRA-BOUNDARY decision record: {record_path}")
            except (OSError, BoundaryError) as exc:
                print(f"ASTRA-BOUNDARY evidence write failed: {exc}", file=sys.stderr)
                record["decision"] = "DENY"
                print(json.dumps(record, sort_keys=True))
                return ERROR
        print(json.dumps(record, sort_keys=True))
        return 0 if record["decision"] == "ALLOW" else DENY

    if not policy_path or not expected_sha or not evidence_dir:
        return finish("DENY", ["boundary configuration incomplete: policy path, "
                               "policy sha256 and evidence dir are all required"])
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        return finish("DENY", ["policy sha256 is not a lowercase hex digest"])
    try:
        policy_file = Path(policy_path)
        actual_sha = sha256_file(policy_file)
    except OSError as exc:
        return finish("DENY", [f"policy unreadable: {exc}"])
    if actual_sha != expected_sha:
        return finish("DENY", [f"policy sha256 mismatch: {actual_sha} != {expected_sha}"],
                      {"policy_sha256": actual_sha})
    record["policy_sha256"] = actual_sha
    try:
        policy = load_policy(policy_file)
    except BoundaryError as exc:
        return finish("DENY", [f"policy invalid: {exc}"])
    record["policy_id"] = policy["policy_id"]

    hook_declared = env.get(HOOK_ENV)
    if args.self_path:
        if not hook_declared:
            return finish("DENY", [f"{HOOK_ENV} is not set in the runner environment"])
        try:
            if Path(hook_declared).resolve() != Path(args.self_path).resolve():
                return finish("DENY", [f"{HOOK_ENV}={hook_declared} does not match hook {args.self_path}"])
        except OSError as exc:
            return finish("DENY", [f"hook path unresolvable: {exc}"])

    event = load_event(env.get("GITHUB_EVENT_PATH"))
    if event is None:
        record["event_file"] = "unavailable"
    claims = collect_claims(env, event)
    record["observed_claims"] = {ref: claims.get(ref) for ref in referenced_claims(policy)}
    allowed, rule_name, reasons = evaluate(policy, claims)
    return finish("ALLOW" if allowed else "DENY",
                  [] if allowed else reasons or ["no allow rule matched"],
                  {"matched_rule": rule_name})


def _read_proc(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _proc_ppid(stat_text: str) -> Optional[int]:
    try:
        return int(stat_text.rsplit(")", 1)[1].split()[1])
    except (IndexError, ValueError):
        return None


def worker_pids(proc_root: Path = Path("/proc")) -> List[int]:
    """Ancestors of this process whose comm is Runner.Worker."""
    found = []
    pid = os.getppid()
    seen = set()
    while pid and pid > 1 and pid not in seen:
        seen.add(pid)
        comm = _read_proc(proc_root / str(pid) / "comm")
        if comm is None:
            break
        if comm.strip().startswith("Runner.Worker"):
            found.append(pid)
        stat_text = _read_proc(proc_root / str(pid) / "stat")
        if stat_text is None:
            break
        pid = _proc_ppid(stat_text) or 0
    return found


def all_worker_pids(proc_root: Path = Path("/proc")) -> List[int]:
    found = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        comm = _read_proc(entry / "comm")
        if comm and comm.strip().startswith("Runner.Worker"):
            found.append(int(entry.name))
    return sorted(found)


def cmd_deny_job(args: argparse.Namespace) -> int:
    """Terminate the Runner.Worker hosting this denied job.

    A failed pre-job step alone cannot stop `always()` steps; the worker must
    die before any job code runs.  Ancestor lookup is preferred; a same-UID
    proc scan is the fallback (the runner account only ever owns its own
    worker).
    """
    proc_root = Path(args.proc_root)
    targets = worker_pids(proc_root)
    source = "ancestor"
    if not targets:
        targets = [p for p in all_worker_pids(proc_root) if p != os.getpid()]
        source = "proc-scan"
    if not targets:
        print("ASTRA-BOUNDARY deny-job: no Runner.Worker process found", file=sys.stderr)
        return ERROR
    for pid in targets:
        try:
            os.kill(pid, signal.SIGKILL)
            print(f"ASTRA-BOUNDARY deny-job: killed Runner.Worker pid={pid} via {source}")
        except OSError as exc:
            print(f"ASTRA-BOUNDARY deny-job: kill {pid} failed: {exc}", file=sys.stderr)
            return ERROR
    return 0


def cmd_worker_pid(_args: argparse.Namespace) -> int:
    print(json.dumps({"ancestors": worker_pids(), "all": all_worker_pids()}))
    return 0


def _check_protected_file(path: Path, label: str, owner_uid: Optional[int]) -> List[str]:
    problems = []
    try:
        info = path.lstat()
    except OSError as exc:
        return [f"{label} missing/unreadable: {exc}"]
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        problems.append(f"{label} must be a single regular file: {path}")
    if info.st_mode & 0o022:
        problems.append(f"{label} is group/world-writable: {path} mode {info.st_mode & 0o777:o}")
    if owner_uid is not None and info.st_uid != owner_uid:
        problems.append(f"{label} owner uid {info.st_uid} != required {owner_uid}")
    return problems


def cmd_verify_install(args: argparse.Namespace) -> int:
    """Structural checks on the installed boundary. Deny-by-default."""
    problems: List[str] = []
    hook = Path(args.hook)
    policy = Path(args.policy)
    if not hook.is_absolute() or not policy.is_absolute() or ".." in hook.parts or ".." in policy.parts:
        problems.append("hook and policy paths must be absolute and normalized")
    problems += _check_protected_file(hook, "hook", args.owner_uid)
    problems += _check_protected_file(policy, "policy", args.owner_uid)
    evaluator = hook.parent / "control_plane_boundary.py"
    problems += _check_protected_file(evaluator, "evaluator", args.owner_uid)
    protected = ((hook, "hook"), (policy, "policy"), (evaluator, "evaluator"))
    for candidate, label in protected:
        for parent in candidate.parents:
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
                problems.append(f"{label} parent is replaceable: {parent}")
            if args.owner_uid is not None and info.st_uid != args.owner_uid:
                problems.append(f"{label} parent has untrusted owner: {parent}")
            if args.writable_check and os.access(parent, os.W_OK):
                problems.append(f"{label} parent writable by this security context: {parent}")
    expected_evaluator = getattr(args, "evaluator_sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_evaluator or ""):
        problems.append("externally pinned evaluator sha256 required")
    elif evaluator.is_file() and sha256_file(evaluator) != expected_evaluator:
        problems.append("evaluator sha256 mismatch")
    if not problems:
        if args.hook_sha256 and sha256_file(hook) != args.hook_sha256:
            problems.append("hook sha256 mismatch")
        if args.policy_sha256 and sha256_file(policy) != args.policy_sha256:
            problems.append("policy sha256 mismatch")
        try:
            text = hook.read_text(encoding="utf-8", errors="replace")
            for token in ("control_plane_boundary.py", "enforce", "deny-job"):
                if token not in text:
                    problems.append(f"hook does not invoke expected boundary step: {token}")
        except OSError as exc:
            problems.append(f"hook unreadable: {exc}")
    if args.forbid_prefix:
        prefix = Path(args.forbid_prefix)
        for candidate, label in protected:
            try:
                candidate.resolve().relative_to(prefix.resolve())
                problems.append(f"{label} must not live under job-writable prefix {prefix}")
            except ValueError:
                pass
    if args.writable_check:
        for candidate, label in protected:
            if os.access(candidate, os.W_OK):
                problems.append(f"{label} is writable by this security context")
    if args.env_file:
        env_file = Path(args.env_file)
        problems += _check_protected_file(env_file, "runner env", args.owner_uid)
        for candidate in (env_file, *env_file.parents):
            info = candidate.lstat() if candidate.exists() else None
            if info is not None and (info.st_mode & 0o022 or
                    (args.owner_uid is not None and info.st_uid != args.owner_uid)):
                problems.append(f"runner env path is not protected: {candidate}")
            if args.writable_check and os.access(candidate, os.W_OK):
                problems.append(f"runner env path writable by runner: {candidate}")
        try:
            lines = env_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            problems.append(f"env file unreadable: {exc}")
            lines = []
        seen = set()
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, sep, value = stripped.partition("=")
            if not sep:
                problems.append(f"{env_file}:{lineno}: not a KEY=VALUE line")
                continue
            key = key.strip()
            if key in seen:
                problems.append(f"{env_file}:{lineno}: duplicate key {key}")
            seen.add(key)
            # Runner loads .env before spawning bash. Unknown keys could run code
            # before this hook (BASH_ENV/LD_PRELOAD); a blacklist is insufficient.
            if key not in {"LANG", "LC_ALL", "TZ"}:
                problems.append(f"{env_file}:{lineno}: unsupported runner env key {key}")
            if key == HOOK_ENV or key.startswith("ASTRA_BOUNDARY_") or key in args.env_forbid:
                problems.append(f"{env_file}:{lineno}: boundary key {key} must not be set here")
            elif key in args.env_require and value.strip() != args.env_require[key]:
                problems.append(f"{env_file}:{lineno}: {key} must equal {args.env_require[key]}")
        for key in args.env_require:
            if key not in seen:
                problems.append(f"{env_file}: required boundary key {key} absent")
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}")
        return DENY
    print("verify-install PASS")
    return 0


def cmd_check_env(args: argparse.Namespace) -> int:
    """Assert the boundary environment of the launching process. Run inside
    the launch context before the listener starts (fail-closed start)."""
    env = os.environ
    problems = []
    if env.get(HOOK_ENV) != args.hook:
        problems.append(f"{HOOK_ENV} must equal {args.hook} (got {env.get(HOOK_ENV)!r})")
    if env.get(POLICY_ENV) != args.policy:
        problems.append(f"{POLICY_ENV} must equal {args.policy}")
    if env.get(POLICY_SHA_ENV) != args.policy_sha256:
        problems.append(f"{POLICY_SHA_ENV} must equal the pinned policy digest")
    if any(key in env for key in ("ASTRA_BOUNDARY_PYTHON", "ASTRA_BOUNDARY_KILL", "ASTRA_BOUNDARY_FLUSH_SECONDS")):
        problems.append("boundary execution overrides are forbidden")
    if any(key in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"} or
           key.startswith(("LD_", "DYLD_", "BASH_FUNC_", "PYTHON")) for key in env):
        problems.append("shell/interpreter/loader startup injection environment forbidden")
    if not env.get(EVIDENCE_ENV):
        problems.append(f"{EVIDENCE_ENV} must be set")
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}")
        return DENY
    print("check-env PASS")
    return 0


def cmd_policy_digest(args: argparse.Namespace) -> int:
    print(sha256_file(Path(args.file)))
    return 0


def cmd_claims(_args: argparse.Namespace) -> int:
    event = load_event(os.environ.get("GITHUB_EVENT_PATH"))
    print(json.dumps(collect_claims(os.environ, event), indent=2, sort_keys=True))
    return 0


def self_test() -> None:
    policy = {
        "schema_version": 1,
        "policy_id": "self-test",
        "required_claims": ["env:GITHUB_RUN_ID"],
        "allow": [{"name": "ok", "claims": {"env:GITHUB_REF": "refs/heads/main",
                                            "event:repository.id": 1365416872}}],
    }
    env = {"GITHUB_RUN_ID": "1", "GITHUB_REF": "refs/heads/main", "HOME": "/x"}
    event = {"repository": {"id": 1365416872}}
    claims = collect_claims(env, event)
    assert claims["env:GITHUB_REF"] == "refs/heads/main"
    assert "env:HOME" not in claims
    assert claims["event:repository.id"] == 1365416872
    allowed, name, _ = evaluate(policy, claims)
    assert allowed and name == "ok"
    bad = dict(claims)
    bad["env:GITHUB_REF"] = "refs/heads/evil"
    allowed, _, reasons = evaluate(policy, bad)
    assert not allowed and reasons
    bad2 = dict(claims)
    bad2["event:repository.id"] = "1365416872"
    allowed, _, _ = evaluate(policy, bad2)
    assert not allowed, "int claim must not equal str expectation"
    missing = dict(claims)
    del missing["env:GITHUB_RUN_ID"]
    allowed, _, reasons = evaluate(policy, missing)
    assert not allowed and "required claim absent" in reasons[0]
    print("boundary self-test PASS")


def main() -> int:
    parser = argparse.ArgumentParser(prog="control_plane_boundary")
    sub = parser.add_subparsers(dest="command", required=True)

    enforce = sub.add_parser("enforce")
    enforce.add_argument("--policy")
    enforce.add_argument("--policy-sha256")
    enforce.add_argument("--evidence-dir")
    enforce.add_argument("--self", dest="self_path")

    deny = sub.add_parser("deny-job")
    deny.add_argument("--proc-root", default="/proc")

    sub.add_parser("worker-pid")

    verify = sub.add_parser("verify-install")
    verify.add_argument("--hook", required=True)
    verify.add_argument("--policy", required=True)
    verify.add_argument("--evaluator-sha256", required=True)
    verify.add_argument("--hook-sha256")
    verify.add_argument("--policy-sha256")
    verify.add_argument("--owner-uid", type=int)
    verify.add_argument("--forbid-prefix")
    verify.add_argument("--writable-check", action="store_true")
    verify.add_argument("--env-file")
    verify.add_argument("--env-forbid", nargs="*", default=[HOOK_ENV, POLICY_ENV, POLICY_SHA_ENV])
    verify.add_argument("--env-require", action="store", default={},
                        type=lambda s: dict(kv.split("=", 1) for kv in s.split(",")),
                        help="comma-separated KEY=VALUE requirements for --env-file")

    check = sub.add_parser("check-env")
    check.add_argument("--hook", required=True)
    check.add_argument("--policy", required=True)
    check.add_argument("--policy-sha256", required=True)

    digest = sub.add_parser("policy-digest")
    digest.add_argument("file")

    sub.add_parser("claims")
    sub.add_parser("self-test")

    args = parser.parse_args()
    try:
        if args.command == "enforce":
            return cmd_enforce(args)
        if args.command == "deny-job":
            return cmd_deny_job(args)
        if args.command == "worker-pid":
            return cmd_worker_pid(args)
        if args.command == "verify-install":
            return cmd_verify_install(args)
        if args.command == "check-env":
            return cmd_check_env(args)
        if args.command == "policy-digest":
            return cmd_policy_digest(args)
        if args.command == "claims":
            return cmd_claims(args)
        if args.command == "self-test":
            self_test()
            return 0
    except BoundaryError as exc:
        print(f"BOUNDARY_ERROR: {exc}", file=sys.stderr)
        return ERROR
    return ERROR


if __name__ == "__main__":
    raise SystemExit(main())
