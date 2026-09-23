#!/usr/bin/env python3
"""Event-flow kernel. No LLM, scheduler, provider call or merge operation.

Only a trusted collector may construct snapshots. Ingress payloads are commands,
never evidence. SQLite is the serialization authority; GitHub is its durable
projection. A transport acknowledgement is not a builder/auditor PASS.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class FlowError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise FlowError("duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(FlowError("nonfinite JSON")))
    except (ValueError, TypeError, RecursionError) as exc:
        raise FlowError("invalid JSON") from exc


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise FlowError(message)


def github_pointer(value):
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme == "https" and url.netloc == "github.com" and
                len(url.path.split("/")) >= 5 and not url.query and
                not any(c.isspace() for c in value))
    except ValueError:
        return False


def verify_slack(raw, headers, secret, policy, now):
    """Verify bytes before parsing. Slack retries reuse trigger_id, not timestamps."""
    require(isinstance(secret, bytes) and len(secret) >= 16, "Slack secret unavailable")
    require(isinstance(raw, bytes) and len(raw) <= 65536, "Slack body too large")
    headers = {k.lower(): v for k, v in headers.items()}
    timestamp = headers.get("x-slack-request-timestamp", "")
    require(re.fullmatch(r"[0-9]{1,12}", timestamp) is not None, "invalid Slack timestamp")
    require(abs(now - int(timestamp)) <= 300, "stale Slack request")
    signature = "v0=" + hmac.new(secret, b"v0:" + timestamp.encode() + b":" + raw,
                                 hashlib.sha256).hexdigest()
    require(hmac.compare_digest(signature, headers.get("x-slack-signature", "")),
            "invalid Slack signature")
    try:
        fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True,
                          strict_parsing=True, max_num_fields=32)
    except (ValueError, UnicodeError) as exc:
        raise FlowError("invalid Slack form") from exc
    require(all(len(v) == 1 for v in fields.values()), "duplicate Slack field")
    data = {k: v[0] for k, v in fields.items()}
    for field, configured in (("team_id", "team_ids"), ("api_app_id", "app_ids"),
                              ("user_id", "user_ids"), ("channel_id", "channel_ids")):
        require(data.get(field) in policy.get(configured, []), "unauthorized Slack " + field)
    require(data.get("command") == "/astra", "unknown Slack command")
    parts = data.get("text", "").split()
    require(len(parts) == 3 and parts[0] in {"dispatch", "refresh", "status"},
            "use /astra dispatch|refresh|status PROJECT ISSUE")
    require(parts[1] in policy.get("projects", {}), "unknown project")
    require(re.fullmatch(r"[1-9][0-9]{0,8}", parts[2]) is not None, "invalid issue number")
    trigger = data.get("trigger_id", "")
    require(bool(trigger) and len(trigger) <= 512, "missing Slack trigger_id")
    command = {"operation": parts[0], "repository": policy["projects"][parts[1]],
               "issue": int(parts[2]), "actor": data["user_id"], "channel": data["channel_id"]}
    key = digest(["slack", data["team_id"], data["api_app_id"], trigger])
    # Never persist response_url, token or raw body in the command ledger.
    return key, command


def verify_github(raw, headers, secret, allowed_repositories):
    require(isinstance(secret, bytes) and len(secret) >= 16, "GitHub secret unavailable")
    require(len(raw) <= 1048576, "GitHub body too large")
    headers = {k.lower(): v for k, v in headers.items()}
    signature = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
    require(hmac.compare_digest(signature, headers.get("x-hub-signature-256", "")),
            "invalid GitHub signature")
    delivery = headers.get("x-github-delivery", "")
    require(re.fullmatch(r"[A-Za-z0-9-]{8,128}", delivery) is not None, "invalid delivery ID")
    event = headers.get("x-github-event")
    require(event in {"pull_request", "check_run", "check_suite", "workflow_run", "status", "issue_comment", "pull_request_review"},
            "unsupported GitHub event")
    body = decode(raw)
    require(isinstance(body, dict), "invalid GitHub envelope")
    repository = body.get("repository", {}).get("full_name")
    require(repository in allowed_repositories, "repository not allowed")
    # A webhook is a refresh hint, never an accepted check/review/HEAD result.
    return digest(["github", delivery]), {"operation": "refresh_repository", "repository": repository}


SUBJECT = ("repository", "task_id", "revision", "head", "base", "policy_revision", "task_digest")
DEPTH = {"A0": 0, "A1": 1, "A2": 2, "A3": 3}


def subject(snapshot):
    value = {k: snapshot[k] for k in SUBJECT}
    for key in ("head", "base", "policy_revision", "task_digest"):
        require(isinstance(value[key], str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value[key]),
                "invalid identity digest: " + key)
    require(all(isinstance(value[k], str) and value[k] for k in SUBJECT), "missing subject identity")
    return value


def request(snapshot, kind, identity, designation):
    require(kind in {"REVIEW", "AUDIT", "DISPATCH", "STATUS", "READY", "BLOCKED", "DECISION"}, "unknown action")
    require(isinstance(identity, str) and bool(identity), "missing actor identity")
    body = {"subject": subject(snapshot), "kind": kind, "identity": identity,
            "designation": designation, "task_pointer": snapshot["task_pointer"],
            "pr_pointer": snapshot.get("pr_pointer"), "read_only": kind in {"REVIEW", "AUDIT"}}
    require(github_pointer(body["task_pointer"]), "invalid task pointer")
    if kind in {"STATUS", "READY", "BLOCKED"}:
        body["observation_digest"] = digest({k: snapshot.get(k) for k in
            ("pr_state", "draft", "checks", "review", "audit", "blockers",
             "prerequisites_verified", "dependencies_verified")})
    body["request_id"] = digest(body)
    body["attempt_id"] = 1
    return body


def verify_result(result, expected, authors, author_sessions):
    require(isinstance(result, dict), "result missing")
    for key in ("subject", "request_id", "attempt_id", "identity", "designation"):
        require(result.get(key) == expected.get(key), "stale/mismatched result: " + key)
    # authenticated_actor is populated by the collector from GitHub, not the JSON body.
    require(result.get("authenticated_actor") == expected["identity"], "untrusted result actor")
    session = result.get("session_id")
    require(isinstance(session, str) and bool(session), "session missing")
    require(session == result.get("confirmed_session_id"), "session not bound to delivery receipt")
    require(expected["identity"] not in authors and session not in author_sessions, "author conflict")
    require(result.get("read_only_verified") is True, "read-only evidence absent")
    require(github_pointer(result.get("evidence_pointer")), "durable evidence missing")
    require(result.get("result") in {"PASS", "PASS_WITH_NOTES", "FAIL", "DECISION_REQUIRED"}, "invalid result")
    require(result.get("required_depth") in DEPTH and result.get("verified_depth") in DEPTH, "depth missing")
    require(type(result.get("contract_change")) is bool, "contract-change classification missing")
    require(isinstance(result.get("blockers"), list), "blocker classification missing")
    return result


def assess(snapshot):
    """Fail-closed decision using a fresh trusted snapshot; emits no side effect."""
    try:
        subject(snapshot)
        require(snapshot.get("pr_state") == "open", "PR not open")
        require(snapshot.get("prerequisites_verified") is True, "repository prerequisites unverified")
        require(snapshot.get("dependencies_verified") is True, "dependencies unverified")
        require(snapshot.get("blockers") == [], "unresolved blocker")
        floor = DEPTH[snapshot["audit_floor"]]
        require(floor >= 1, "A0 uses the existing separately authorized mechanical path")
        gate = snapshot["astra_gate"]
        require(gate in {"NONE", "MILESTONE", "ARCHITECTURE", "RELEASE"}, "unknown Astra gate")
        required = snapshot["required_checks"]
        require(isinstance(required, list) and bool(required), "required CI policy absent")
        for expected in required:
            checks = [x for x in snapshot.get("checks", []) if
                      (x.get("name"), x.get("app_id"), x.get("workflow_id")) ==
                      (expected["name"], expected["app_id"], expected["workflow_id"])]
            require(len(checks) == 1, "missing/ambiguous required CI: " + expected["name"])
            check = checks[0]
            require(check.get("head") == snapshot["head"] and check.get("source_verified") is True,
                    "stale/untrusted CI")
            require(check.get("status") == "completed" and check.get("conclusion") == "success",
                    "CI not successful")
        authors, sessions = snapshot["author_identities"], snapshot["author_sessions"]
        require(isinstance(authors, list) and bool(authors) and isinstance(sessions, list),
                "authorship provenance missing")
        assignment = snapshot["reviewer"]
        require(assignment.get("enabled") is True and assignment.get("read_only_verified") is True,
                "reviewer lane not verified")
        require(assignment["identity"] not in authors, "reviewer is an author")
        require(github_pointer(assignment["approval_pointer"]), "reviewer assignment unapproved")
        review_request = request(snapshot, "REVIEW", assignment["identity"], assignment["approval_pointer"])
        if snapshot.get("review") is None:
            return {"state": "REVIEW_REQUIRED", "action": review_request}
        review = verify_result(snapshot["review"], review_request, authors, sessions)
        require(review["result"] in {"PASS", "PASS_WITH_NOTES"} and not review["blockers"], "review not passing")
        if review["contract_change"]:
            return {"state": "DECISION_REQUIRED", "merge_authorized": False,
                    "action": request(snapshot, "DECISION", "ASTRA", "N/A")}
        effective = max(floor, DEPTH[review["required_depth"]])
        require(DEPTH[review["verified_depth"]] >= min(effective, 2), "insufficient review depth")
        if effective == 3:
            gate = "ARCHITECTURE" if gate in {"NONE", "ARCHITECTURE"} else gate + "+ARCHITECTURE"
        if gate != "NONE":
            auditor = snapshot["auditor"]
            require(auditor.get("active") is True and github_pointer(auditor["designation_pointer"]),
                    "auditor designation inactive")
            require(auditor["identity"] not in authors, "auditor author conflict")
            audit_request = request(snapshot, "AUDIT", auditor["identity"], auditor["designation_pointer"])
            # Preserve explicitly required milestone/release scope in the exact request.
            audit_request["gate"] = gate
            if "MILESTONE" in gate or "RELEASE" in gate:
                require(re.fullmatch(r"[0-9a-f]{64}", snapshot.get("audit_scope_digest", "")) is not None,
                        "explicit milestone/release evidence scope required")
            audit_request["scope_digest"] = snapshot.get("audit_scope_digest", digest(subject(snapshot)))
            audit_request["request_id"] = digest({k: v for k, v in audit_request.items() if k != "request_id"})
            if snapshot.get("audit") is None:
                return {"state": "AUDIT_REQUIRED", "effective_depth": effective, "action": audit_request}
            audit = verify_result(snapshot["audit"], audit_request, authors, sessions)
            require(audit.get("gate") == gate and audit.get("scope_digest") == audit_request["scope_digest"],
                    "audit scope changed")
            require(audit["result"] in {"PASS", "PASS_WITH_NOTES"} and not audit["blockers"], "audit not passing")
            require(not audit["contract_change"] and DEPTH[audit["verified_depth"]] >= effective,
                    "audit insufficient/decision required")
        require(snapshot.get("draft") is False, "PR still draft; User transition required")
        return {"state": "READY_FOR_MERGE", "effective_depth": effective,
                "subject": subject(snapshot), "merge_authorized": False}
    except (FlowError, KeyError, TypeError, ValueError) as exc:
        return {"state": "BLOCKED", "reason": str(exc), "merge_authorized": False}


def assign_reviewer(builder, routes, lanes, authors):
    identity = routes.get(builder)
    require(isinstance(identity, str) and bool(identity), "review route not configured")
    lane = lanes.get(identity, {})
    require(lane.get("identity") == identity and lane.get("enabled") is True and
            lane.get("read_only_verified") is True, "configured reviewer not qualified")
    require(identity not in authors, "configured reviewer participated in authorship")
    require(github_pointer(lane.get("approval_pointer")), "reviewer approval missing")
    return dict(lane)



def qualify_lane(report, approval, expected_runtime):
    """Check an operator-collected report. Does not claim to have executed a CLI."""
    required = ("cli", "authentication", "credential_isolation", "durable_session",
                "duplicate_unknown", "trusted_workflow_boundary", "quota_policy")
    require(report.get("builder_id") in {"DEVIN", "GROK_BUILD", "GLM"}, "unknown builder")
    require(report.get("runtime_sha") == expected_runtime, "runtime mismatch")
    for key in ("wrapper_sha256", "binary_sha256"):
        require(isinstance(report.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", report[key]), "hash missing")
    for key in ("harness", "model", "cli_version", "host_id"):
        require(isinstance(report.get(key), str) and bool(report[key]), "provider provenance missing")
    require(report.get("execution_mode") == "PERSISTENT_SUPERVISOR", "local durable supervisor required")
    require(all(report.get("checks", {}).get(k) == "PASS" for k in required), "lane checks incomplete")
    require(github_pointer(report.get("evidence_pointer")), "evidence pointer missing")
    require(approval.get("active") is True and github_pointer(approval.get("pointer")), "approval missing")
    require(approval.get("report_digest") == digest(report), "approval is not bound to exact report")
    require(report.get("harness") != "ZCODE_UNVERIFIED", "unverified ZCode headless surface")
    return {"status": "QUALIFIED_FOR_INDEPENDENT_REVIEW", "report_digest": digest(report),
            "production_enabled": False}


class Store:
    """Protected single-host inbox/outbox. Never use one SQLite DB across hosts."""
    def __init__(self, path, *, initialize=False):
        self.path = Path(path).absolute()
        if initialize:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            with sqlite3.connect(self.path) as db:
                db.executescript("""
                CREATE TABLE inbox (id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE outbox (id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL,
                                     valid INTEGER NOT NULL, receipt TEXT);
                PRAGMA user_version=1;
                """)
        parent = self.path.parent.lstat()
        require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.geteuid() and
                not parent.st_mode & 0o077, "ledger directory must be owner-only")
        info = self.path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, "unsafe flow ledger")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=0.5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA trusted_schema=OFF")
            require(db.execute("PRAGMA user_version").fetchone()[0] == 1, "unknown ledger schema")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def accept(self, event_id, command):
        body = canonical(command)
        with self.transaction() as db:
            old = db.execute("SELECT * FROM inbox WHERE id=?", (event_id,)).fetchone()
            if old:
                require(old["body"] == body, "event ID reused with different command")
                return False
            db.execute("INSERT INTO inbox VALUES (?,?, 'NOT_STARTED')", (event_id, body))
            return True

    def begin_event(self, event_id):
        with self.transaction() as db:
            old = db.execute("SELECT * FROM inbox WHERE id=?", (event_id,)).fetchone()
            require(old is not None, "event missing")
            if old["state"] != "NOT_STARTED":
                return None
            db.execute("UPDATE inbox SET state='PROCESSING' WHERE id=?", (event_id,))
            return decode(old["body"])

    def end_event(self, event_id, succeeded):
        with self.transaction() as db:
            db.execute("UPDATE inbox SET state=? WHERE id=? AND state='PROCESSING'",
                       ("DONE" if succeeded else "BLOCKED", event_id))

    def reserve(self, action):
        body, key = canonical(action), action["request_id"]
        with self.transaction() as db:
            old = db.execute("SELECT * FROM outbox WHERE id=?", (key,)).fetchone()
            if old:
                require(old["body"] == body, "request identity reused with different body")
                return dict(old)
            # An ambiguous old-scope request is not erased by HEAD/designation change.
            for row in db.execute("SELECT body FROM outbox WHERE state IN ('SUBMITTING','UNKNOWN')"):
                previous = decode(row["body"])
                require(not (previous["kind"] == action["kind"] and
                             all(previous["subject"][k] == action["subject"][k]
                                 for k in ("repository", "task_id"))), "unreconciled prior delivery")
            db.execute("INSERT INTO outbox VALUES (?,?,'NOT_STARTED',1,NULL)", (key, body))
            return {"id": key, "state": "NOT_STARTED", "valid": 1, "receipt": None}

    def invalidate(self, request_id):
        with self.transaction() as db:
            # Transport uncertainty is retained even when semantic authority is revoked.
            db.execute("UPDATE outbox SET valid=0 WHERE id=?", (request_id,))

    def send_once(self, action, send, is_current):
        self.reserve(action)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM outbox WHERE id=?", (action["request_id"],)).fetchone()
            if not row["valid"]:
                return {"state": "STALE", "sent": False}
            if row["state"] != "NOT_STARTED":
                return {"state": row["state"], "sent": False}
        # Fresh authority read must occur outside the DB transaction, then CAS below.
        if not is_current(action):
            self.invalidate(action["request_id"])
            return {"state": "STALE", "sent": False}
        with self.transaction() as db:
            changed = db.execute("UPDATE outbox SET state='SUBMITTING' WHERE id=? AND valid=1 AND state='NOT_STARTED'",
                                 (action["request_id"],)).rowcount
            if changed != 1:
                return {"state": "REUSED", "sent": False}
        # A crash here leaves SUBMITTING. Never infer that no external effect happened.
        try:
            receipt = send(action)
            require(isinstance(receipt, dict) and receipt.get("request_id") == action["request_id"] and
                    receipt.get("accepted") is True, "ambiguous/mismatched delivery receipt")
            state = "CONFIRMED"
        except Exception:
            state, receipt = "UNKNOWN", {"reason": "delivery outcome unresolved; reconcile without retry"}
        with self.transaction() as db:
            db.execute("UPDATE outbox SET state=?,receipt=? WHERE id=? AND state='SUBMITTING'",
                       (state, canonical(receipt), action["request_id"]))
        return {"state": state, "sent": True, "receipt": receipt}


class Coordinator:
    """Ports load live GitHub facts; side effects are split by durable action IDs.

The projection must be confirmed before routing. A Slack receipt only confirms
message delivery; it never creates a semantic review/audit result.
"""
    def __init__(self, store, ports):
        self.store, self.ports = store, ports

    def handle(self, event_id):
        command = self.store.begin_event(event_id)
        if command is None:
            return {"state": "EVENT_REUSED"}
        try:
            snapshot = self.ports.load(command)
            verdict = assess(snapshot)
            if command["operation"] == "dispatch":
                require(self.ports.dispatch_authorized(snapshot), "dispatch preconditions missing")
                action = request(snapshot, "DISPATCH", snapshot["builder_identity"], snapshot["approval_pointer"])
            else:
                action = verdict.get("action")
                if action is None:
                    action = request(snapshot, "READY" if verdict["state"] == "READY_FOR_MERGE" else "BLOCKED",
                                     "MECHANICAL", "N/A")
                if command["operation"] == "status":
                    action = request(snapshot, "STATUS", "MECHANICAL", "N/A")
            projection = dict(action, kind="PROJECTION", action_kind=action["kind"],
                              action_request_id=action["request_id"],
                              request_id=digest(["projection", action["request_id"]]))
            current = lambda a: self.ports.is_current(a)
            projected = self.store.send_once(projection, self.ports.project, current)
            if projected["state"] != "CONFIRMED":
                self.store.end_event(event_id, False)
                return {"state": "PROJECTION_" + projected["state"]}
            routed = self.store.send_once(action, self.ports.route, current)
            self.store.end_event(event_id, routed["state"] == "CONFIRMED")
            return {"state": verdict["state"], "delivery": routed["state"]}
        except Exception:
            self.store.end_event(event_id, False)
            raise
