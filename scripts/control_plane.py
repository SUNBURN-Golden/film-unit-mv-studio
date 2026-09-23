#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / ".github" / "control-plane" / "config.json"
ACTIVATION_PATH = ROOT / ".github" / "control-plane" / "activation.json"
CONTROL_MARKER = "<!-- ASTRA_CONTROL_RECORD_V1 -->"
JSON_BLOCK_RE = re.compile(
    re.escape(CONTROL_MARKER) + r"\s*```json\s*(\{.*?\})\s*```",
    re.DOTALL,
)
FIELD_RE = re.compile(r"^([A-Z][A-Z0-9_]+):\s*(.*?)\s*$")

REQUIRED_TASK_FIELDS = (
    "TASK_ID",
    "PROJECT",
    "REPO",
    "CANONICAL_TASK_POINTER",
    "TASK_REVISION",
    "TASK_SPEC_POINTER",
    "TASK_SPEC_REVISION",
    "APPROVAL_POINTER",
    "AUTHORITATIVE_DOC_POINTERS",
    "EXECUTION_CLASS",
    "BUILDER_ID",
    "CONTROL_RECORD_POINTER",
)
ALLOWED_BUILDERS = ("DEVIN", "GROK_BUILD", "GLM")
ALLOWED_LAUNCH_STATES = (
    "NOT_STARTED",
    "SUBMITTING",
    "CONFIRMED",
    "FAILED_PRESTART",
    "UNKNOWN",
)
HOST_COMMAND = ("/usr/bin/sudo", "-n", "-u", "astra-control", "/opt/astra/bin/astra-host-control")
LAUNCH_IDENTITY = ("repository", "task_id", "task_revision", "builder_id", "launch_request_id", "attempt_id")
RUNTIME_PATHS = (
    "scripts/control_plane.py", "scripts/control_plane_host.py",
    "scripts/control_plane_boundary.py", "scripts/control_plane_boundary_hook.sh",
    "scripts/control_plane_boundary_probe.sh",
    "scripts/test_control_plane.py", "scripts/test_control_plane_host.py",
    "scripts/test_control_plane_boundary.py",
    ".github/control-plane/config.json", ".github/control-plane/host-policy.example.json",
    ".github/control-plane/boundary-policy.example.json",
    ".github/workflows/control-plane-runtime.yml", ".github/workflows/control-plane-ci.yml",
    "docs/CONTROL_PLANE_RUNTIME.md", "AGENTS.md", "RUNBOOKS/DISPATCH.md", "TASKS/TEMPLATE.md",
)


class ControlPlaneError(RuntimeError):
    pass


def load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ControlPlaneError(f"missing file: {path.relative_to(ROOT)}") from exc
    except json.JSONDecodeError as exc:
        raise ControlPlaneError(f"invalid JSON in {path.relative_to(ROOT)}: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlPlaneError(f"{path.relative_to(ROOT)} must contain a JSON object")
    return value


def load_config() -> Dict[str, Any]:
    cfg = load_json(CONFIG_PATH)
    required = {
        "schema_version",
        "project",
        "repository",
        "runner_labels",
        "allowed_builders",
        "enabled_builders",
        "builder_wrappers",
        "control_record_marker",
        "control_record_actor",
        "allowed_task_actors",
        "allowed_dispatch_actors",
    }
    missing = sorted(required - cfg.keys())
    if missing:
        raise ControlPlaneError(f"config missing fields: {', '.join(missing)}")
    if cfg["schema_version"] != 1:
        raise ControlPlaneError("unsupported config schema_version")
    if cfg["control_record_marker"] != CONTROL_MARKER:
        raise ControlPlaneError("control_record_marker mismatch")
    if cfg["control_record_actor"] != "github-actions[bot]":
        raise ControlPlaneError("control_record_actor must be github-actions[bot]")
    for actor_field in ("allowed_task_actors", "allowed_dispatch_actors"):
        actors = cfg[actor_field]
        if not isinstance(actors, list) or not actors or any(not isinstance(a, str) or not a for a in actors):
            raise ControlPlaneError(f"{actor_field} must be a non-empty string list")
    builders = cfg["allowed_builders"]
    if not isinstance(builders, list) or not builders:
        raise ControlPlaneError("allowed_builders must be a non-empty list")
    if set(builders) != set(ALLOWED_BUILDERS):
        raise ControlPlaneError("allowed_builders must be exactly DEVIN, GROK_BUILD, GLM")
    enabled = cfg["enabled_builders"]
    if (not isinstance(enabled, list) or not enabled or
            any(builder not in builders for builder in enabled) or len(set(enabled)) != len(enabled)):
        raise ControlPlaneError("enabled_builders must be an explicit non-empty subset of allowed_builders")
    wrappers = cfg["builder_wrappers"]
    if not isinstance(wrappers, dict):
        raise ControlPlaneError("builder_wrappers must be an object")
    for builder in builders:
        path = wrappers.get(builder)
        if not isinstance(path, str) or not path.startswith("/"):
            raise ControlPlaneError(f"builder wrapper for {builder} must be an absolute path")
    labels = cfg["runner_labels"]
    if not isinstance(labels, list) or "self-hosted" not in labels or "astra-control-plane" not in labels:
        raise ControlPlaneError("runner_labels must include self-hosted and astra-control-plane")
    return cfg


def load_activation() -> Dict[str, Any]:
    value = load_json(ACTIVATION_PATH)
    required = {
        "schema_version",
        "user_activation_approval",
        "user_activation_approval_pointer",
        "implementation_audit",
        "implementation_audit_pointer",
        "runner_preflight",
        "runner_preflight_pointer",
        "runtime_enabled",
        "activated_runtime_sha",
    }
    missing = sorted(required - value.keys())
    if missing:
        raise ControlPlaneError(f"activation missing fields: {', '.join(missing)}")
    if value["schema_version"] != 1:
        raise ControlPlaneError("unsupported activation schema_version")
    if value["user_activation_approval"] not in {"APPROVED", "NOT_APPROVED"}:
        raise ControlPlaneError("invalid user_activation_approval")
    if value["user_activation_approval"] == "APPROVED" and value["user_activation_approval_pointer"] in (None, "", "PENDING", "PENDING_RUNTIME_PR_POINTER"):
        raise ControlPlaneError("approved activation lacks a durable User approval pointer")
    if value["implementation_audit"] not in {"PENDING", "PASS"}:
        raise ControlPlaneError("invalid implementation_audit")
    if value["runner_preflight"] not in {"PENDING", "PASS"}:
        raise ControlPlaneError("invalid runner_preflight")
    if not isinstance(value["runtime_enabled"], bool):
        raise ControlPlaneError("runtime_enabled must be boolean")
    if value["runtime_enabled"] and (
        value["user_activation_approval"] != "APPROVED"
        or value["implementation_audit"] != "PASS"
        or value["runner_preflight"] != "PASS"
    ):
        raise ControlPlaneError("runtime_enabled=true without all activation gates passing")
    return value


def require_text(path: str, substrings: Iterable[str]) -> None:
    text = (ROOT / path).read_text(encoding="utf-8")
    for needle in substrings:
        if needle not in text:
            raise ControlPlaneError(f"{path} missing required contract text: {needle!r}")


def validate_repo() -> None:
    cfg = load_config()
    activation = load_activation()
    if cfg["repository"] != os.environ.get("GITHUB_REPOSITORY", cfg["repository"]):
        raise ControlPlaneError("config repository does not match GITHUB_REPOSITORY")
    require_text(
        "TASKS/TEMPLATE.md",
        ("TASK ENVELOPE v4", "EXECUTION_CLASS: BUILDER_STANDARD", "BUILDER_ID:", "ASTRA_GATE:"),
    )
    require_text(
        "RUNBOOKS/DISPATCH.md",
        (
            "EFFECTIVE_AUDIT_FLOOR",
            "NOT_STARTED/SUBMITTING/CONFIRMED/UNKNOWN",
            "ACCEPTED_AUDITOR_IDENTITY",
            "AUDITOR_DESIGNATION_POINTER",
        ),
    )
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    if "DEVIN_STANDARD" in agents:
        raise ControlPlaneError("AGENTS.md still contains DEVIN_STANDARD")
    if activation["runtime_enabled"]:
        if activation.get("activated_runtime_sha") in (None, "", "PENDING"):
            raise ControlPlaneError("enabled runtime lacks activated_runtime_sha")
        if activation.get("implementation_audit_pointer") in (None, "", "PENDING"):
            raise ControlPlaneError("enabled runtime lacks implementation_audit_pointer")
        if activation.get("runner_preflight_pointer") in (None, "", "PENDING"):
            raise ControlPlaneError("enabled runtime lacks runner_preflight_pointer")


def parse_task_envelope(body: str) -> Dict[str, str]:
    found: Dict[str, str] = {}
    duplicates = set()
    for raw in body.splitlines():
        match = FIELD_RE.match(raw.strip())
        if not match:
            continue
        key, value = match.groups()
        if key in found:
            duplicates.add(key)
        else:
            found[key] = value.strip()
    if duplicates:
        raise ControlPlaneError("duplicate task fields: " + ", ".join(sorted(duplicates)))
    missing = [k for k in REQUIRED_TASK_FIELDS if not found.get(k)]
    if missing:
        raise ControlPlaneError("missing required task fields: " + ", ".join(missing))
    return found


def validate_task(envelope: Dict[str, str], cfg: Dict[str, Any]) -> None:
    if envelope["REPO"] != cfg["repository"]:
        raise ControlPlaneError(f"task REPO {envelope['REPO']!r} != configured repository")
    if envelope["PROJECT"] != cfg["project"]:
        raise ControlPlaneError(f"task PROJECT {envelope['PROJECT']!r} != configured project")
    if envelope["EXECUTION_CLASS"] != "BUILDER_STANDARD":
        raise ControlPlaneError("runtime dispatch currently accepts BUILDER_STANDARD only")
    if envelope["BUILDER_ID"] not in cfg["allowed_builders"]:
        raise ControlPlaneError("BUILDER_ID is not an allowed configured builder")
    if envelope["BUILDER_ID"] not in cfg["enabled_builders"]:
        raise ControlPlaneError("BUILDER_ID is not enabled for this rollout; no automatic fallback")
    if not re.fullmatch(r"[A-Za-z0-9._:/#@+-]+", envelope["TASK_REVISION"]):
        raise ControlPlaneError("TASK_REVISION contains unsupported characters")


def stable_id(kind: str, repository: str, task_id: str, task_revision: str, attempt_id: int) -> str:
    material = f"{kind}\0{repository}\0{task_id}\0{task_revision}\0{attempt_id}".encode()
    return hashlib.sha256(material).hexdigest()[:24]


def new_control_record(envelope: Dict[str, str], cfg: Dict[str, Any]) -> Dict[str, Any]:
    attempt = 1
    return {
        "schema_version": 1,
        "task_id": envelope["TASK_ID"],
        "task_revision": envelope["TASK_REVISION"],
        "project": envelope["PROJECT"],
        "repository": envelope["REPO"],
        "builder_id": envelope["BUILDER_ID"],
        "claim_id": stable_id("claim", cfg["repository"], envelope["TASK_ID"], envelope["TASK_REVISION"], attempt),
        "launch_request_id": stable_id("launch", cfg["repository"], envelope["TASK_ID"], envelope["TASK_REVISION"], attempt),
        "launch_state": "NOT_STARTED",
        "attempt_id": attempt,
        "owner_session_id": None,
        "last_error": None,
    }


def render_control_record(record: Dict[str, Any]) -> str:
    return CONTROL_MARKER + "\n" + "```json\n" + json.dumps(record, indent=2, sort_keys=True) + "\n```"


def parse_control_record(body: str) -> Dict[str, Any]:
    match = JSON_BLOCK_RE.search(body)
    if not match:
        raise ControlPlaneError("control record marker found but JSON block is invalid")
    try:
        record = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ControlPlaneError(f"invalid control record JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise ControlPlaneError("control record must be an object")
    if record.get("launch_state") not in ALLOWED_LAUNCH_STATES:
        raise ControlPlaneError("control record has invalid launch_state")
    return record


class GithubApi:
    def __init__(self, repository: str, token: str):
        self.repository = repository
        self.token = token
        self.base = f"https://api.github.com/repos/{repository}"

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base + path
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "astra-control-plane-v1",
        }
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            raise ControlPlaneError(f"GitHub API {method} {path} failed: {exc.code} {raw}") from exc
        return json.loads(raw) if raw else None

    def issue(self, number: int) -> Dict[str, Any]:
        return self._request("GET", f"/issues/{number}")

    def comments(self, number: int) -> list:
        result = []
        page = 1
        while True:
            chunk = self._request("GET", f"/issues/{number}/comments?per_page=100&page={page}")
            result.extend(chunk)
            if len(chunk) < 100:
                break
            page += 1
        return result

    def create_comment(self, number: int, body: str) -> Dict[str, Any]:
        return self._request("POST", f"/issues/{number}/comments", {"body": body})

    def update_comment(self, comment_id: int, body: str) -> Dict[str, Any]:
        return self._request("PATCH", f"/issues/comments/{comment_id}", {"body": body})


def find_control_comment(comments: list, expected_actor: str) -> Optional[Dict[str, Any]]:
    marker_comments = [c for c in comments if CONTROL_MARKER in (c.get("body") or "")]
    invalid = [
        c for c in marker_comments
        if ((c.get("user") or {}).get("login") or "") != expected_actor
    ]
    if invalid:
        raise ControlPlaneError("control-record marker exists on a comment from an unauthorized actor")
    if len(marker_comments) > 1:
        raise ControlPlaneError("multiple canonical control-record comments found")
    return marker_comments[0] if marker_comments else None


def write_github_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{key}={value}\n")


def require_runtime_enabled() -> Dict[str, Any]:
    activation = load_activation()
    if not activation["runtime_enabled"]:
        raise ControlPlaneError("runtime is fail-closed: runtime_enabled=false")
    validate_repo()
    audited = activation["activated_runtime_sha"]
    if not isinstance(audited, str) or not re.fullmatch(r"[0-9a-f]{40}", audited):
        raise ControlPlaneError("activated_runtime_sha must identify the audited runtime commit")
    # An activation-only commit may follow the audited commit. Product changes
    # do not require re-auditing unchanged control code; control changes do.
    for args in (("merge-base", "--is-ancestor", audited, "HEAD"),
                 ("diff", "--exit-code", audited, "HEAD", "--", *RUNTIME_PATHS),
                 ("diff", "--exit-code", "HEAD", "--", *RUNTIME_PATHS,
                  ".github/control-plane/activation.json")):
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)
        if result.returncode:
            raise ControlPlaneError("current runtime differs from or cannot resolve audited runtime SHA")
    return activation


def host_call(arguments: list, packet: Optional[dict] = None) -> dict:
    # Never forward the Actions token, workflow command files, PATH overrides,
    # Python import configuration or runner HOME into the privileged adapter.
    try:
        completed = subprocess.run(
            [*HOST_COMMAND, *arguments],
            input=json.dumps(packet) if packet is not None else None,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, cwd="/",
            capture_output=True, text=True, check=False, timeout=240,
        )
        if completed.returncode:
            raise ControlPlaneError(f"host control exited {completed.returncode}; inspect protected host evidence")
        report = json.loads(completed.stdout)
        if not isinstance(report, dict):
            raise ValueError("host result is not an object")
        return report
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ControlPlaneError("host control response unavailable or invalid") from exc


def host_preflight(builder_id: Optional[str]) -> None:
    cfg = load_config()
    builders = [builder_id] if builder_id else list(cfg["enabled_builders"])
    failures = []
    reports = []
    for builder in builders:
        if builder not in cfg["enabled_builders"]:
            raise ControlPlaneError(f"builder is not enabled: {builder}")
        try:
            report = host_call(["preflight", "--builder-id", builder])
        except ControlPlaneError as exc:
            failures.append(f"{builder}: {exc}")
            continue
        if report.get("host_admission") != "ENFORCED" or not report.get("boundary_evidence_pointer"):
            failures.append(f"{builder}: protected admission/boundary evidence missing")
            continue
        expected_hash = hashlib.sha256((ROOT / "scripts/control_plane_host.py").read_bytes()).hexdigest()
        if report.get("helper_sha256") != expected_hash:
            failures.append(f"{builder}: installed host helper differs from audited source")
            continue
        if cfg["repository"] not in report.get("allowed_repositories", []):
            failures.append(f"{builder}: repository not enabled by host policy")
            continue
        if report.get("status") != "PASS":
            failures.append(f"{builder}: preflight status is not PASS")
            continue
        if report.get("builder_id") != builder:
            failures.append(f"{builder}: preflight builder_id mismatch")
            continue
        if report.get("execution_mode") not in {"REMOTE_SESSION", "PERSISTENT_SUPERVISOR"}:
            failures.append(
                f"{builder}: execution_mode must prove work survives the dispatch step"
            )
            continue
        if report.get("parallel_safe") is not True:
            failures.append(f"{builder}: parallel_safe must be true")
            continue
        reports.append(report)

    if failures:
        raise ControlPlaneError("host preflight failed: " + " | ".join(failures))

    roots = [r.get("worktree_root") for r in reports if r.get("worktree_root")]
    if roots and len(set(roots)) != len(roots):
        raise ControlPlaneError("builder preflight reports overlapping worktree_root values")

    print(json.dumps({"status": "PASS", "builders": reports}, sort_keys=True))


def prepare_dispatch(issue_number: int, packet_path: Path) -> None:
    cfg = load_config()
    require_runtime_enabled()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ControlPlaneError("GITHUB_TOKEN is required")
    dispatch_actor = os.environ.get("GITHUB_ACTOR", "")
    rerun_actor = os.environ.get("GITHUB_TRIGGERING_ACTOR", dispatch_actor)
    if any(actor not in cfg["allowed_dispatch_actors"] for actor in (dispatch_actor, rerun_actor)):
        raise ControlPlaneError(f"unauthorized dispatch actor: {dispatch_actor!r}")

    api = GithubApi(cfg["repository"], token)
    if issue_number <= 0:
        raise ControlPlaneError("issue_number must be positive")
    issue = api.issue(issue_number)
    if issue.get("state") != "open" or "pull_request" in issue:
        raise ControlPlaneError("canonical task must be an open issue")
    issue_actor = ((issue.get("user") or {}).get("login") or "")
    if issue_actor not in cfg["allowed_task_actors"]:
        raise ControlPlaneError(f"canonical task actor is not allowed: {issue_actor!r}")

    envelope = parse_task_envelope(issue.get("body") or "")
    validate_task(envelope, cfg)
    issue_url = issue.get("html_url") or ""
    if envelope["CANONICAL_TASK_POINTER"] != issue_url:
        raise ControlPlaneError("CANONICAL_TASK_POINTER must equal the canonical issue URL")
    if envelope["CONTROL_RECORD_POINTER"] != issue_url:
        raise ControlPlaneError("CONTROL_RECORD_POINTER must equal the canonical issue URL for runtime v1")

    comment = find_control_comment(api.comments(issue_number), cfg["control_record_actor"])
    if comment:
        record = parse_control_record(comment.get("body") or "")
        if record.get("task_id") != envelope["TASK_ID"] or record.get("task_revision") != envelope["TASK_REVISION"]:
            raise ControlPlaneError("existing control record identity does not match current task")
        if record.get("builder_id") != envelope["BUILDER_ID"]:
            raise ControlPlaneError("builder reassignment requires an explicit fenced control action")
        state = record["launch_state"]
        if state in {"SUBMITTING", "UNKNOWN"}:
            raise ControlPlaneError(f"unresolved {state} launch blocks redispatch")
        if state == "CONFIRMED":
            print("existing confirmed owner; no second launch")
            write_github_output("launch_required", "false")
            return
        if state == "FAILED_PRESTART":
            raise ControlPlaneError("FAILED_PRESTART requires explicit retry authorization/fencing")
    else:
        record = new_control_record(envelope, cfg)
        # Persist the pending launch as NOT_STARTED before consuming send authority.
        comment = api.create_comment(issue_number, render_control_record(record))

    # Duplicates return before provider preflight or any model invocation.
    host_preflight(envelope["BUILDER_ID"])

    # Under the workflow's per-task concurrency lock, consume the one existing
    # pending action by durably moving NOT_STARTED -> SUBMITTING before any
    # external builder wrapper can run.
    record["launch_state"] = "SUBMITTING"
    record["last_error"] = None
    api.update_comment(comment["id"], render_control_record(record))

    packet = {
        "schema_version": 1,
        "repository": cfg["repository"],
        "project": cfg["project"],
        "task_issue": issue_number,
        "task_id": envelope["TASK_ID"],
        "task_revision": envelope["TASK_REVISION"],
        "task_pointer": envelope["CANONICAL_TASK_POINTER"],
        "task_spec_pointer": envelope["TASK_SPEC_POINTER"],
        "task_spec_revision": envelope["TASK_SPEC_REVISION"],
        "approval_pointer": envelope["APPROVAL_POINTER"],
        "authoritative_doc_pointers": envelope["AUTHORITATIVE_DOC_POINTERS"],
        "builder_id": envelope["BUILDER_ID"],
        "launch_request_id": record["launch_request_id"],
        "attempt_id": record["attempt_id"],
        "control_comment_id": comment["id"],
    }
    packet_path.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_github_output("launch_required", "true")
    write_github_output("builder_id", envelope["BUILDER_ID"])
    write_github_output("launch_request_id", record["launch_request_id"])
    write_github_output("control_comment_id", str(comment["id"]))


def bound_result(result: dict, expected: dict) -> dict:
    if any(type(result.get(key)) is not type(expected.get(key)) or
           result.get(key) != expected.get(key) for key in LAUNCH_IDENTITY):
        return {"outcome": "UNKNOWN", "reason": "adapter result launch identity mismatch"}
    return result


def launch_dispatch(packet_path: Path, result_path: Path) -> None:
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    identity = {key: packet[key] for key in LAUNCH_IDENTITY}
    # Persist an exact-attempt sentinel before crossing the external boundary.
    # A crash cannot leave a previous run's success at this path.
    result = {**identity, "outcome": "UNKNOWN", "reason": "host launch did not return"}
    result_path.write_text(json.dumps(result) + "\n", encoding="utf-8")
    try:
        require_runtime_enabled()
        result = bound_result(host_call(["launch"], packet), packet)
    except ControlPlaneError as exc:
        result = {"outcome": "UNKNOWN", "reason": str(exc)}
    result_path.write_text(json.dumps({**identity, **result}) + "\n", encoding="utf-8")


def finalize_dispatch(issue_number: int, result_path: Path, launch_request_id: str) -> str:
    cfg = load_config()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ControlPlaneError("GITHUB_TOKEN is required")
    api = GithubApi(cfg["repository"], token)
    comment = find_control_comment(api.comments(issue_number), cfg["control_record_actor"])
    if not comment:
        raise ControlPlaneError("control record missing during finalize")
    record = parse_control_record(comment.get("body") or "")
    if record.get("launch_request_id") != launch_request_id:
        raise ControlPlaneError("stale finalization request rejected")
    if record["launch_state"] != "SUBMITTING":
        raise ControlPlaneError(f"cannot finalize from launch_state={record['launch_state']}")

    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError("adapter result must be a JSON object")
    except Exception as exc:
        result = {"outcome": "UNKNOWN", "reason": f"malformed/missing adapter result: {exc}"}

    result = bound_result(result, record)
    outcome = result.get("outcome")
    if outcome == "CONFIRMED":
        session = result.get("session_id")
        if not isinstance(session, str) or not session.strip():
            outcome = "UNKNOWN"
            result = {"outcome": "UNKNOWN", "reason": "CONFIRMED without session_id"}
        else:
            record["launch_state"] = "CONFIRMED"
            record["owner_session_id"] = session.strip()
            record["last_error"] = None
    if outcome == "FAILED_PRESTART":
        record["launch_state"] = "FAILED_PRESTART"
        record["last_error"] = str(result.get("reason") or "adapter reported FAILED_PRESTART")
    elif outcome not in {"CONFIRMED", "FAILED_PRESTART"}:
        record["launch_state"] = "UNKNOWN"
        record["last_error"] = str(result.get("reason") or "adapter outcome ambiguous")

    api.update_comment(comment["id"], render_control_record(record))
    return record["launch_state"]


def self_test() -> None:
    sample = """TASK_ID: T-1
PROJECT: SAMPLE
REPO: owner/repo
CANONICAL_TASK_POINTER: https://github.com/owner/repo/issues/1
TASK_REVISION: 7
TASK_SPEC_POINTER: docs/task.md@abc
TASK_SPEC_REVISION: abc
APPROVAL_POINTER: https://github.com/owner/repo/issues/1#issuecomment-1
AUTHORITATIVE_DOC_POINTERS: AGENTS.md
EXECUTION_CLASS: BUILDER_STANDARD
BUILDER_ID: DEVIN
CONTROL_RECORD_POINTER: https://github.com/owner/repo/issues/1
"""
    env = parse_task_envelope(sample)
    assert env["TASK_ID"] == "T-1"
    cfg = {
        "repository": "owner/repo",
        "project": "SAMPLE",
        "allowed_builders": list(ALLOWED_BUILDERS),
        "enabled_builders": ["DEVIN"],
    }
    validate_task(env, cfg)
    record = new_control_record(env, {"repository": "owner/repo"})
    assert record["launch_state"] == "NOT_STARTED"
    rendered = render_control_record(record)
    parsed = parse_control_record(rendered)
    assert parsed["launch_request_id"] == record["launch_request_id"]
    assert stable_id("x", "r", "t", "1", 1) == stable_id("x", "r", "t", "1", 1)

    try:
        parse_task_envelope(sample + "\nTASK_ID: T-2\n")
        raise AssertionError("duplicate field was not rejected")
    except ControlPlaneError:
        pass

    bad = dict(env)
    bad["BUILDER_ID"] = "OTHER"
    try:
        validate_task(bad, cfg)
        raise AssertionError("invalid builder was not rejected")
    except ControlPlaneError:
        pass

    trusted_comment = {
        "body": rendered,
        "user": {"login": "github-actions[bot]"},
    }
    assert find_control_comment([trusted_comment], "github-actions[bot]") is trusted_comment

    try:
        find_control_comment(
            [{"body": rendered, "user": {"login": "untrusted-user"}}],
            "github-actions[bot]",
        )
        raise AssertionError("spoofed control-record actor was not rejected")
    except ControlPlaneError:
        pass

    try:
        find_control_comment([trusted_comment, trusted_comment], "github-actions[bot]")
        raise AssertionError("duplicate control-record comments were not rejected")
    except ControlPlaneError:
        pass

    print("self-test PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-repo")
    sub.add_parser("self-test")

    pre = sub.add_parser("host-preflight")
    pre.add_argument("--builder-id", choices=ALLOWED_BUILDERS)

    prepare = sub.add_parser("prepare-dispatch")
    prepare.add_argument("--issue-number", type=int, required=True)
    prepare.add_argument("--packet", type=Path, required=True)

    launch = sub.add_parser("launch-dispatch")
    launch.add_argument("--packet", type=Path, required=True)
    launch.add_argument("--result", type=Path, required=True)

    finalize = sub.add_parser("finalize-dispatch")
    finalize.add_argument("--issue-number", type=int, required=True)
    finalize.add_argument("--result", type=Path, required=True)
    finalize.add_argument("--launch-request-id", required=True)

    args = parser.parse_args()
    try:
        if args.command == "validate-repo":
            validate_repo()
            print("repository validation PASS")
        elif args.command == "self-test":
            self_test()
        elif args.command == "host-preflight":
            host_preflight(args.builder_id)
        elif args.command == "prepare-dispatch":
            prepare_dispatch(args.issue_number, args.packet)
        elif args.command == "launch-dispatch":
            launch_dispatch(args.packet, args.result)
        elif args.command == "finalize-dispatch":
            final_state = finalize_dispatch(args.issue_number, args.result, args.launch_request_id)
            print(f"final launch state: {final_state}")
            if final_state != "CONFIRMED":
                return 3
        return 0
    except ControlPlaneError as exc:
        print(f"CONTROL_PLANE_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
