#!/usr/bin/env python3
"""Explicit-event HTTP integration. Install behind TLS; deployment is separately gated.

No public listener, credentials, project registrations or lane approvals are
created by importing this module. No background polling and no auto-merge.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import time
import threading
import stat
import urllib.error
import urllib.request

# Explicit sibling import supports python -I from an operator-protected installation.
_spec = importlib.util.spec_from_file_location("astra_flow", Path(__file__).with_name("control_plane_flow.py"))
flow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(flow)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise flow.FlowError("API redirects are not permitted")


class Api:
    def __init__(self, github_token, slack_token):
        self.github_token, self.slack_token = github_token, slack_token

    def call(self, method, path, body=None, *, slack=False):
        origin = "https://slack.com/api/" if slack else "https://api.github.com/"
        token = self.slack_token if slack else self.github_token
        flow.require(bool(token), "API credential unavailable")
        flow.require(not path.startswith("/") and ".." not in path and "://" not in path, "unsafe API path")
        request = urllib.request.Request(origin + path,
                    data=None if body is None else flow.canonical(body).encode(), method=method,
                    headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                             "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
                             "User-Agent": "astra-flow/2"})
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(2_000_001)
        flow.require(len(raw) <= 2_000_000, "API response too large")
        value = flow.decode(raw) if raw else {}
        if slack:
            flow.require(value.get("ok") is True, "Slack rejected request")
        return value

    def pages(self, path):
        output = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 21):
            data = self.call("GET", f"{path}{separator}per_page=100&page={page}")
            if isinstance(data, dict):
                flow.require("workflow_runs" in data, "unrecognized pagination envelope")
                data = data["workflow_runs"]
            flow.require(isinstance(data, list), "invalid paginated API result")
            output.extend(data)
            if len(data) < 100:
                return output
        raise flow.FlowError("API pagination limit exceeded; do not accept partial facts")


def unwrap(body, marker):
    prefix = marker + "\n"
    flow.require(isinstance(body, str) and body.startswith(prefix), "missing canonical record marker")
    value = flow.decode(body[len(prefix):])
    flow.require(isinstance(value, dict), "record must be an object")
    return value


class GithubPorts:
    """Read pinned User task bindings, current PR and full required CI from GitHub.

Registrations are operator-protected deployment data, not arbitrary commands.
Each binding points to one User-authored GitHub comment + exact body digest.
Review/audit results use native authenticated GitHub review objects, never Slack
PASS text. Shared author/reviewer GitHub identities are conservatively rejected.
"""
    def __init__(self, api, policy):
        self.api, self.policy = api, policy

    def registration(self, repository, issue):
        key = f"{repository}#{issue}"
        flow.require(key in self.policy.get("registrations", {}), "canonical task not registered")
        return self.policy["registrations"][key]

    def bound_comment(self, repo, binding, marker):
        flow.require(binding.get("actor") in self.policy["user_actors"], "untrusted approval actor")
        comment = self.api.call("GET", f"repos/{repo}/issues/comments/{int(binding['comment_id'])}")
        body = comment.get("body", "")
        flow.require(comment.get("user", {}).get("login") == binding["actor"] and
                     hashlib.sha256(body.encode()).hexdigest() == binding["sha256"],
                     "approval edited, replaced or revoked")
        value = unwrap(body, marker)
        flow.require(value.get("active") is True, "approval inactive")
        return value, comment

    def load(self, command):
        repo, issue = command["repository"], command["issue"]
        binding = self.registration(repo, issue)
        prefix = f"repos/{repo}/"
        task = self.api.call("GET", prefix + f"issues/{issue}")
        flow.require(task.get("state") == "open", "canonical task closed")
        flow.require(hashlib.sha256((task.get("body") or "").encode()).hexdigest() ==
                     binding["issue_body_sha256"], "canonical task body/revision changed")
        flow.require(binding["actor"] in self.policy["user_actors"], "untrusted task author")
        comment = self.api.call("GET", prefix + f"issues/comments/{binding['comment_id']}")
        flow.require(comment.get("user", {}).get("login") == binding["actor"], "task authority changed")
        flow.require(comment.get("issue_url") == f"https://api.github.com/repos/{repo}/issues/{issue}",
                     "binding comment belongs to another issue")
        body = comment.get("body", "")
        flow.require(hashlib.sha256(body.encode()).hexdigest() == binding["sha256"], "task binding changed/revoked")
        snapshot = unwrap(body, "<!-- ASTRA_FLOW_TASK_V1 -->")
        flow.require(snapshot["repository"] == repo and snapshot["task_id"] == binding["task_id"],
                     "repository/task mismatch")
        flow.require(snapshot["task_pointer"] == task["html_url"], "canonical pointer mismatch")
        flow.require(snapshot["approval_pointer"] == comment["html_url"], "approval pointer mismatch")
        # The two durable representations must authorize the same executable task.
        fields = {}
        for line in (task.get("body") or "").splitlines():
            match = re.fullmatch(r"([A-Z][A-Z0-9_]+):\s*(.*?)\s*", line.strip())
            if match:
                key, value = match.groups()
                flow.require(key not in fields, "duplicate issue-envelope field")
                fields[key] = value
        for field, key in (("REPO", "repository"), ("TASK_ID", "task_id"),
                           ("TASK_REVISION", "revision"), ("BUILDER_ID", "builder_id")):
            flow.require(fields.get(field) == snapshot.get(key), "issue/flow identity mismatch: " + field)
        snapshot["issue_body_sha256"] = binding["issue_body_sha256"]
        snapshot["task_digest"] = binding["sha256"]
        snapshot["policy_revision"] = flow.digest(self.policy)
        snapshot["blockers"] = list(snapshot.get("blockers", []))
        snapshot["dependencies_verified"] = True
        for dependency in snapshot["dependencies"]:
            flow.require(dependency["repository"] in self.policy["repositories"], "unknown dependency repo")
            current = self.api.call("GET", f"repos/{dependency['repository']}/pulls/{int(dependency['pr'])}")
            if current.get("merged") is not True or current.get("merge_commit_sha") != dependency["merge_sha"]:
                snapshot["dependencies_verified"] = False
        # Reviewer selection is a fixed approved route, not a model quality judgment.
        lane = flow.assign_reviewer(snapshot["builder_id"], self.policy["review_routes"],
                                    self.policy["review_lanes"], snapshot["author_identities"])
        evidence, approval = self.bound_comment(repo, lane["binding"], "<!-- ASTRA_FLOW_LANE_V1 -->")
        flow.require(evidence["identity"] == lane["identity"] and evidence["read_only_verified"] is True and
                     approval["html_url"] == lane["approval_pointer"], "review lane evidence mismatch")
        snapshot["reviewer"] = lane
        snapshot["auditor"] = {"active": False}
        if "auditor_binding" in binding:
            auditor, pointer = self.bound_comment(repo, binding["auditor_binding"], "<!-- ASTRA_FLOW_AUDITOR_V1 -->")
            flow.require(auditor["task_id"] == snapshot["task_id"] and
                         auditor["revision"] == snapshot["revision"], "auditor scope mismatch")
            auditor["designation_pointer"] = pointer["html_url"]
            snapshot["auditor"] = auditor
        pr_number = snapshot.get("pr_number")
        if not pr_number:
            snapshot.update(head=snapshot["base"], pr_state="not_created", draft=True, checks=[])
            return snapshot
        flow.require(type(pr_number) is int and pr_number > 0, "invalid PR identity")
        pr = self.api.call("GET", prefix + f"pulls/{pr_number}")
        flow.require(pr["base"]["repo"]["full_name"] == repo, "PR repository mismatch")
        flow.require(pr["base"]["sha"] == snapshot["base"], "base moved; refresh task authority")
        snapshot.update(head=pr["head"]["sha"], pr_state=pr["state"], draft=pr["draft"], pr_pointer=pr["html_url"])
        snapshot["author_identities"] = sorted(set(snapshot["author_identities"] + [pr["user"]["login"]]))
        # A current-head User attestation covers repo-specific gates not inferred by this service.
        prerequisite, _ = self.bound_comment(repo, binding["prerequisites"], "<!-- ASTRA_FLOW_PREREQUISITES_V1 -->")
        snapshot["prerequisites_verified"] = (prerequisite.get("subject") == {k: v for k, v in flow.subject(snapshot).items() if k != "policy_revision"} and
                                               prerequisite.get("result") == "PASS")
        # An unresolved GitHub mergeability result never turns into ready.
        if pr.get("mergeable") is not True:
            snapshot["blockers"].append("mergeability_not_verified")
        runs = self.api.pages(prefix + "actions/runs?head_sha=" + snapshot["head"])
        snapshot["checks"] = []
        for expected in snapshot["required_checks"]:
            candidates = [r for r in runs if r.get("workflow_id") == expected["workflow_id"] and
                          r.get("head_sha") == snapshot["head"] and r.get("event") in expected["events"]]
            if not candidates:
                continue
            candidates = [r for r in candidates if r.get("event") != "pull_request" or
                          any(p.get("number") == pr_number for p in r.get("pull_requests", []))]
            if not candidates:
                continue
            run = max(candidates, key=lambda r: (r["id"], r.get("run_attempt", 1)))
            suite = self.api.call("GET", prefix + f"check-suites/{run['check_suite_id']}")
            path = expected["path"]
            flow.require(re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", path), "unsafe workflow path")
            contents = self.api.call("GET", prefix + f"contents/{path}?ref={snapshot['head']}")
            snapshot["checks"].append({"name": expected["name"], "workflow_id": run["workflow_id"],
                "app_id": suite.get("app", {}).get("id"), "head": run["head_sha"],
                "status": run["status"], "conclusion": run["conclusion"],
                "source_verified": (contents.get("sha") == expected["workflow_blob"] and
                                    run.get("path", "").split("@")[0] == path)})
        reviews = self.api.pages(prefix + f"pulls/{pr_number}/reviews")
        snapshot["review"], snapshot["audit"] = None, None
        for phase in ("review", "audit"):
            assignment = snapshot["reviewer"] if phase == "review" else snapshot.get("auditor", {})
            matches = []
            for result in reviews:
                if (result.get("user", {}).get("login") != assignment.get("identity") or
                    result.get("commit_id") != snapshot["head"] or result.get("state") == "DISMISSED"):
                    continue
                marker = "<!-- ASTRA_FLOW_RESULT_V1 -->"
                if not result.get("body", "").startswith(marker + "\n"):
                    continue
                parsed = unwrap(result["body"], marker)
                if parsed.get("phase") != phase or parsed.get("subject") != flow.subject(snapshot):
                    continue
                parsed["authenticated_actor"] = result["user"]["login"]
                # Native review ID is an authenticated submission session, not a model name.
                parsed["session_id"] = parsed["confirmed_session_id"] = f"github-review:{result['id']}"
                parsed["read_only_verified"] = assignment.get("read_only_verified") is True
                parsed["evidence_pointer"] = result["html_url"]
                if parsed.get("result") in {"PASS", "PASS_WITH_NOTES"}:
                    flow.require(result["state"] == "APPROVED", "PASS requires a native approved review")
                matches.append(parsed)
            flow.require(len(matches) <= 1, "ambiguous independent result; require explicit reconciliation")
            snapshot[phase] = matches[0] if matches else None
        return snapshot

    def dispatch_authorized(self, snapshot):
        lane = self.policy.get("lanes", {}).get(snapshot.get("builder_id"), {})
        if not (self.policy.get("enabled") is True and lane.get("enabled") is True and
                snapshot.get("dispatch_authorized") is True and snapshot.get("blockers") == [] and
                snapshot.get("dependencies_verified") is True):
            return False
        approved, _ = self.bound_comment(snapshot["repository"], lane["binding"], "<!-- ASTRA_FLOW_BUILDER_V1 -->")
        flow.require(approved["report"].get("builder_id") == snapshot["builder_id"],
                     "qualified report belongs to another builder")
        flow.qualify_lane(approved["report"], approved["approval"], lane["runtime_sha"])
        return approved.get("independent_audit") == "PASS"

    def is_current(self, action):
        scope = action["subject"]
        command = self.command_for(scope)
        snapshot = self.load(command)
        if flow.subject(snapshot) != scope:
            return False
        kind = action.get("action_kind", action["kind"])
        if kind == "DISPATCH":
            return self.dispatch_authorized(snapshot)
        if kind in {"STATUS", "READY", "BLOCKED"}:
            fresh = flow.request(snapshot, kind, "MECHANICAL", "N/A")
            if fresh["observation_digest"] != action.get("observation_digest"):
                return False
            if kind == "STATUS":
                return True
        verdict = flow.assess(snapshot)
        if kind in {"REVIEW", "AUDIT", "DECISION"}:
            candidate = verdict.get("action", {})
            return all(candidate.get(k) == action.get(k) for k in
                       ("identity", "designation", "subject", "gate", "scope_digest"))
        return (kind == "READY") == (verdict["state"] == "READY_FOR_MERGE")

    def command_for(self, subject):
        keys = [key for key, binding in self.policy["registrations"].items()
                if binding["task_id"] == subject["task_id"] and key.rsplit("#", 1)[0] == subject["repository"]]
        flow.require(len(keys) == 1, "missing/ambiguous canonical task registration")
        return {"repository": subject["repository"], "issue": int(keys[0].rsplit("#", 1)[1]), "operation": "refresh"}

    def project(self, action):
        flow.require(self.policy.get("enabled") is True, "event flow is disabled")
        command = self.command_for(action["subject"])
        published = {k: v for k, v in action.items() if k not in {"action_kind", "action_request_id"}}
        published.update(kind=action["action_kind"], request_id=action["action_request_id"])
        body = "<!-- ASTRA_FLOW_ACTION_V1 -->\n" + flow.canonical(published)
        result = self.api.call("POST", f"repos/{command['repository']}/issues/{command['issue']}/comments", {"body": body})
        flow.require(flow.github_pointer(result.get("html_url")), "GitHub projection receipt missing")
        return {"accepted": True, "request_id": action["request_id"], "pointer": result["html_url"]}

    def route(self, action):
        flow.require(self.policy.get("enabled") is True, "event flow is disabled")
        command = self.command_for(action["subject"])
        repo, issue = command["repository"], command["issue"]
        if action["kind"] == "DISPATCH":
            snapshot = self.load(command)
            flow.require(flow.subject(snapshot) == action["subject"] and self.dispatch_authorized(snapshot),
                         "dispatch scope changed before delivery")
            self.api.call("POST", f"repos/{repo}/actions/workflows/control-plane-runtime.yml/dispatches",
                          {"ref": "main", "inputs": {"operation": "dispatch", "issue_number": str(issue),
                           "expected_task_id": snapshot["task_id"],
                           "expected_task_revision": snapshot["revision"],
                           "expected_builder_id": snapshot["builder_id"],
                           "expected_issue_body_sha256": snapshot["issue_body_sha256"]}})
            # 204 confirms delivery only; runtime v1 owns actual launch admission.
        elif action["kind"] == "REVIEW":
            snapshot = self.load(command)
            self.api.call("POST", f"repos/{repo}/pulls/{snapshot['pr_number']}/requested_reviewers",
                          {"reviewers": [action["identity"]]})
        else:
            channel = self.policy[{"AUDIT": "audit_channel", "DECISION": "decision_channel"}.get(
                action["kind"], "status_channel")]
            flow.require(isinstance(channel, str) and re.fullmatch(r"[CG][A-Z0-9]+", channel), "Slack channel not configured")
            self.api.call("POST", "chat.postMessage", {"channel": channel,
                          "text": f"[{action['kind']}] {repo} {action['subject']['task_id']} @ {action['subject']['head']}\n"
                                  f"Task: {action['task_pointer']}\nRequest: {action['request_id']}\n"
                                  "Status projection only. User-only merge.",
                          "unfurl_links": False, "unfurl_media": False}, slack=True)
        return {"accepted": True, "request_id": action["request_id"]}


class Ingress:
    """WSGI: authenticate, durably enqueue, acknowledge. Work runs after enqueue.

A failed/uncertain processing attempt is never retried by a timer. An operator
can reconcile it; NOT_STARTED rows may be resumed by an explicit event.
"""
    def __init__(self, store, ports, policy, slack_secret, github_secret, executor=None):
        self.store, self.ports, self.policy = store, ports, policy
        self.slack_secret, self.github_secret = slack_secret, github_secret
        self.executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="astra-event")
        self.slots = threading.BoundedSemaphore(16)

    def work(self, event_id, command):
        if command["operation"] == "refresh_repository":
            if self.store.begin_event(event_id) is None:
                return
            try:
                for key in self.policy.get("registrations", {}):
                    repo, issue = key.rsplit("#", 1)
                    if repo == command["repository"]:
                        child = flow.digest([event_id, key])
                        value = {"operation": "refresh", "repository": repo, "issue": int(issue)}
                        self.store.accept(child, value)
                        flow.Coordinator(self.store, self.ports).handle(child)
                self.store.end_event(event_id, True)
            except Exception:
                self.store.end_event(event_id, False)
                raise
        else:
            flow.Coordinator(self.store, self.ports).handle(event_id)

    def __call__(self, environ, start_response):
        try:
            flow.require(self.policy.get("enabled") is True, "event flow disabled")
            flow.require(environ.get("REQUEST_METHOD") == "POST", "POST required")
            length = int(environ.get("CONTENT_LENGTH", "0"))
            flow.require(0 < length <= 1048576, "invalid body length")
            raw = environ["wsgi.input"].read(length)
            flow.require(len(raw) == length, "truncated body")
            headers = {k[5:].replace("_", "-").lower(): v for k, v in environ.items() if k.startswith("HTTP_")}
            if environ.get("PATH_INFO") == "/slack/commands":
                key, command = flow.verify_slack(raw, headers, self.slack_secret, self.policy["slack"], time.time())
            elif environ.get("PATH_INFO") == "/github/events":
                key, command = flow.verify_github(raw, headers, self.github_secret, self.policy["repositories"])
            else:
                raise flow.FlowError("unknown endpoint")
            self.store.accept(key, command)
            # Duplicate delivery can wake an existing NOT_STARTED row, never create a new attempt.
            # begin_event CAS ensures only one worker proceeds; PROCESSING/terminal rows are reused.
            if not self.slots.acquire(blocking=False):
                raise RuntimeError("processing capacity exhausted; retained NOT_STARTED")
            try:
                future = self.executor.submit(self.work, key, command)
                future.add_done_callback(lambda _: self.slots.release())
            except BaseException:
                self.slots.release()
                raise
            start_response("200 OK", [("Content-Type", "application/json")])
            return [flow.canonical({"response_type": "ephemeral", "text": "접수됨. 실행·검증 승인이 아닙니다."}).encode()]
        except (flow.FlowError, ValueError, TypeError):
            start_response("403 Forbidden", [("Content-Type", "application/json")])
            return [b'{"error":"request rejected"}']
        except Exception:
            start_response("503 Service Unavailable", [("Content-Type", "application/json")])
            return [b'{"error":"durable intake unavailable"}']


def create_app(policy_path):
    """Operator-installed WSGI entrypoint; does not start a server or run any builder."""
    path = Path(policy_path).absolute()
    for parent in path.parents:
        info = parent.lstat()
        flow.require(stat.S_ISDIR(info.st_mode) and info.st_uid in {0, os.geteuid()} and
                     not info.st_mode & 0o022, "unprotected policy parent")
    info = path.lstat()
    flow.require(stat.S_ISREG(info.st_mode) and info.st_uid in {0, os.geteuid()} and
                 not info.st_mode & 0o022 and info.st_nlink == 1, "unprotected policy")
    policy = flow.decode(path.read_text())
    flow.require(policy.get("enabled") is True and policy.get("deployment_audit") == "PASS" and
                 flow.github_pointer(policy.get("deployment_approval_pointer")), "deployment not approved")
    for filename in ("control_plane_flow.py", "control_plane_flow_gateway.py"):
        file = Path(__file__).with_name(filename)
        info = file.lstat()
        flow.require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                     "gateway source must be operator-installed, not a task checkout")
        flow.require(hashlib.sha256(file.read_bytes()).hexdigest() == policy["source_sha256"][filename],
                     "installed gateway source differs from approved source")
    # Secret injection belongs to the isolated service account, never builder env.
    api = Api(os.environ["ASTRA_FLOW_GITHUB_TOKEN"], os.environ["ASTRA_FLOW_SLACK_TOKEN"])
    store = flow.Store(policy["ledger_path"])
    return Ingress(store, GithubPorts(api, policy), policy,
                   os.environ["ASTRA_FLOW_SLACK_SIGNING_SECRET"].encode(),
                   os.environ["ASTRA_FLOW_GITHUB_WEBHOOK_SECRET"].encode())
