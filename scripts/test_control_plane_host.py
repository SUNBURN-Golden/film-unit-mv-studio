"""Local fault/concurrency tests; no installed policy, provider or root access needed."""
import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import control_plane_host as host


def packet(index=0, **changes):
    value = {"schema_version": 1, "repository": f"owner/repo{index % 5}", "task_id": f"T-{index}",
             "task_revision": "r1", "builder_id": "DEVIN", "launch_request_id": f"request-{index}",
             "attempt_id": 1, "task_issue": index + 1}
    value.update(changes)
    return value


def reserve_process(path, policy, value):
    return host.Ledger(path).reserve(value, policy)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "admission.sqlite"
        self.now = 1_000_000
        self.ledger = host.Ledger(self.path, clock=lambda: self.now)
        self.ledger.initialize()
        self.policy = {
            "control_uid": 1010, "runner_uid": 1020,
            "builder_uids": {"DEVIN": 1030, "GROK_BUILD": 1040, "GLM": 1050},
            "allowed_repositories": [f"owner/repo{i}" for i in range(5)],
            "enabled_builders": ["DEVIN"], "max_active_sessions": 2,
            "max_launches_per_24h": 4, "ledger_path": str(self.path),
            "wrapper_paths": dict(host.WRAPPERS),
            "boundary_evidence_pointer": "https://github.com/owner/ops/issues/12",
        }
        self.calls = []

    def invoke(self, value, policy):
        self.calls.append(value["launch_request_id"])
        return subprocess.CompletedProcess([], 0, json.dumps(host.result_for(value, "CONFIRMED", session_id="session-" + value["launch_request_id"])))

    def state(self, request="request-0"):
        db = sqlite3.connect(self.path)
        try:
            return db.execute("SELECT state FROM launches WHERE request=?", (request,)).fetchone()[0]
        finally:
            db.close()

    def test_concurrent_processes_share_capacity_across_five_repositories(self):
        with concurrent.futures.ProcessPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(reserve_process, str(self.path), self.policy, packet(i)) for i in range(15)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(sum(admitted for admitted, _ in results), 2)
        self.assertEqual(sum(result["outcome"] == "FAILED_PRESTART" for _, result in results), 13)

    def test_concurrent_duplicate_admits_only_once(self):
        with concurrent.futures.ProcessPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(reserve_process, str(self.path), self.policy, packet()) for _ in range(8)]
            self.assertEqual(sum(future.result(timeout=20)[0] for future in futures), 1)

    def test_duplicate_returns_cached_result_without_second_provider_call(self):
        first = host.launch(packet(), self.policy, self.ledger, self.invoke)
        second = host.launch(dict(reversed(list(packet().items()))), self.policy, self.ledger, self.invoke)
        self.assertEqual(first, second)
        self.assertEqual(self.calls, ["request-0"])
        self.assertEqual(self.state(), "CONFIRMED")

    def test_request_identity_includes_entire_packet(self):
        host.launch(packet(), self.policy, self.ledger, self.invoke)
        for changes in ({"task_issue": 99}, {"task_revision": "r2"}, {"attempt_id": 2}, {"task_id": "different"}):
            with self.subTest(changes=changes), self.assertRaises(host.HostError):
                host.launch(packet(**changes), self.policy, self.ledger, self.invoke)
        self.assertEqual(len(self.calls), 1)

    def test_new_revision_or_issue_cannot_create_second_task_writer(self):
        host.launch(packet(), self.policy, self.ledger, self.invoke)
        other = packet(5, task_id="T-0", task_revision="r2", task_issue=99)
        result = host.launch(other, self.policy, self.ledger, self.invoke)
        self.assertEqual(result["outcome"], "FAILED_PRESTART")
        self.assertIn("task already", result["reason"])
        self.assertEqual(len(self.calls), 1)

    def test_unknown_holds_slot_forever_and_duplicate_never_resends(self):
        self.policy["max_active_sessions"] = 1
        def ambiguous(value, policy):
            self.calls.append("ambiguous")
            raise subprocess.TimeoutExpired("adapter", 180)
        result = host.launch(packet(), self.policy, self.ledger, ambiguous)
        self.now += 86400 * 100
        self.assertEqual(host.launch(packet(), self.policy, self.ledger, self.invoke), result)
        self.assertEqual(host.launch(packet(1), self.policy, self.ledger, self.invoke)["outcome"], "FAILED_PRESTART")
        self.assertEqual(self.state(), "UNKNOWN")
        self.assertEqual(self.calls, ["ambiguous"])

    def test_operator_proven_no_session_releases_slot_without_replay(self):
        self.policy["max_active_sessions"] = 1
        self.ledger.reserve(packet(), self.policy)  # crash before any adapter call
        evidence = "https://github.com/owner/ops/issues/13"
        for options in ({"no_session": True}, {"no_session": True, "sender_fenced": False}):
            with self.assertRaises(host.HostError):
                self.ledger.reconcile("request-0", None, evidence, **options)
        with self.assertRaises(host.HostError):
            self.ledger.reconcile("request-0", None, "", no_session=True, sender_fenced=True)
        result = self.ledger.reconcile("request-0", None, evidence, no_session=True, sender_fenced=True)
        self.assertEqual(result["resolution"], "NO_SESSION_CONFIRMED")
        self.assertIsNone(result["session_id"])
        self.assertEqual(self.state(), "RECONCILED")
        # The first result is retained: replay never authorizes another send.
        self.assertEqual(host.launch(packet(), self.policy, self.ledger, self.invoke)["outcome"], "UNKNOWN")
        self.assertEqual(self.calls, [])
        self.assertEqual(host.launch(packet(1), self.policy, self.ledger, self.invoke)["outcome"], "CONFIRMED")
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute("SELECT sum(admitted) FROM launches").fetchone()[0], 2)
        finally:
            db.close()

    def test_known_session_cannot_be_released_as_no_session(self):
        host.launch(packet(), self.policy, self.ledger, self.invoke)
        with self.assertRaises(host.HostError):
            self.ledger.reconcile("request-0", None, "https://github.com/owner/ops/issues/13",
                                  no_session=True, sender_fenced=True)
        self.assertEqual(self.state(), "CONFIRMED")

    def test_process_interruption_leaves_durable_submitting_record(self):
        self.policy["max_active_sessions"] = 1
        def crash(value, policy):
            self.assertEqual(self.state(), "SUBMITTING")
            raise KeyboardInterrupt("simulated process death after reservation")
        with self.assertRaises(KeyboardInterrupt):
            host.launch(packet(), self.policy, self.ledger, crash)
        restarted = host.Ledger(self.path, clock=lambda: self.now)
        self.assertEqual(host.launch(packet(), self.policy, restarted, self.invoke)["outcome"], "UNKNOWN")
        self.now += 86400 * 100
        self.assertEqual(host.launch(packet(1), self.policy, restarted, self.invoke)["outcome"], "FAILED_PRESTART")
        self.assertEqual(self.state(), "SUBMITTING")
        self.assertEqual(self.calls, [])

    def test_malformed_nonzero_and_wrong_identity_results_are_unknown(self):
        good = host.result_for(packet(), "CONFIRMED", session_id="actual-session")
        cases = [(0, "not JSON"), (1, json.dumps(good)), (0, json.dumps({**good, "task_revision": "stale"})),
                 (0, json.dumps({**good, "attempt_id": True})), (0, json.dumps({**good, "session_id": ""})),
                 (0, json.dumps({**good, "outcome": "FAILED_PRESTART", "reason": "claims a live session"})),
                 (0, json.dumps({**good, "outcome": "UNKNOWN", "session_id": {"bad": "type"}})),
                 (0, json.dumps({**good, "outcome": "FAILED_PRESTART", "session_id": None}))]
        self.policy["max_active_sessions"] = len(cases)
        self.policy["max_launches_per_24h"] = len(cases)
        for index, (code, stdout) in enumerate(cases):
            with self.subTest(index=index):
                # Bind all other fields to this request so the malformed field is decisive.
                if index:
                    output = json.loads(stdout)
                    original = {key: val for key, val in output.items() if key not in host.IDENTITY}
                    output = {**host.result_for(packet(index), "CONFIRMED", session_id="actual-session"), **original}
                    if index == 2:
                        output["task_revision"] = "stale"
                    if index == 3:
                        output["attempt_id"] = True
                    stdout = json.dumps(output)
                result = host.launch(packet(index), self.policy, self.ledger,
                                     lambda value, policy, code=code, stdout=stdout: subprocess.CompletedProcess([], code, stdout))
                self.assertEqual(result["outcome"], "UNKNOWN")
                for field in host.IDENTITY:
                    self.assertEqual(result[field], packet(index)[field])
                self.assertEqual(self.state(f"request-{index}"), "UNKNOWN")

    def test_definite_prestart_failure_releases_slot_but_counts_launch_attempt(self):
        self.policy.update(max_active_sessions=1, max_launches_per_24h=1)
        def fail(value, policy):
            self.calls.append(value["launch_request_id"])
            return subprocess.CompletedProcess([], 0, json.dumps(host.result_for(value, "FAILED_PRESTART", "credentials unavailable; no create request sent")))
        result = host.launch(packet(), self.policy, self.ledger, fail)
        self.assertEqual(result["outcome"], "FAILED_PRESTART")
        result = host.launch(packet(1), self.policy, self.ledger, self.invoke)
        self.assertIn("max_launches_per_24h", result["reason"])
        self.now += 86401
        # Previously denied request stays denied after budget window advances.
        self.assertEqual(host.launch(packet(1), self.policy, self.ledger, self.invoke), result)
        self.assertEqual(host.launch(packet(2), self.policy, self.ledger, self.invoke)["outcome"], "CONFIRMED")
        self.assertEqual(self.calls, ["request-0", "request-2"])

    def test_reconciliation_requires_session_match_and_keeps_request_deduplication(self):
        self.policy["max_active_sessions"] = 1
        original = host.launch(packet(), self.policy, self.ledger, self.invoke)
        with self.assertRaises(host.HostError):
            self.ledger.reconcile("request-0", "wrong", "https://github.com/owner/ops/issues/13")
        self.ledger.reconcile("request-0", original["session_id"], "https://github.com/owner/ops/issues/13")
        self.assertEqual(self.state(), "RECONCILED")
        self.assertEqual(host.launch(packet(), self.policy, self.ledger, self.invoke), original)
        self.assertEqual(host.launch(packet(5, task_id="T-0"), self.policy, self.ledger, self.invoke)["outcome"], "CONFIRMED")
        self.assertEqual(len(self.calls), 2)

    def test_reconciliation_cannot_release_reservation_while_sender_is_in_flight(self):
        def still_sending(value, policy):
            self.assertEqual(self.state(), "SUBMITTING")
            with self.assertRaisesRegex(host.HostError, "still in flight"):
                self.ledger.reconcile("request-0", "session-request-0", "https://github.com/owner/ops/issues/13")
            self.assertEqual(self.state(), "SUBMITTING")
            return self.invoke(value, policy)
        result = host.launch(packet(), self.policy, self.ledger, still_sending)
        self.ledger.reconcile("request-0", result["session_id"], "https://github.com/owner/ops/issues/13")
        self.assertEqual(self.state(), "RECONCILED")

    def test_unknown_release_requires_operator_evidence_but_never_resends_request(self):
        self.ledger.reserve(packet(), self.policy)
        with self.assertRaises(host.HostError):
            self.ledger.reconcile("request-0", "", "https://github.com/owner/ops/issues/13")
        with self.assertRaises(host.HostError):
            self.ledger.reconcile("request-0", "operator-found-session", "PENDING")
        self.ledger.reconcile("request-0", "operator-found-session", "https://github.com/owner/ops/issues/13")
        self.assertEqual(host.launch(packet(), self.policy, self.ledger, self.invoke)["outcome"], "UNKNOWN")
        self.assertEqual(self.calls, [])

    def test_adapter_inherits_fence_after_helper_descriptor_closes(self):
        self.ledger.reserve(packet(), self.policy)
        with self.ledger.inflight_lock() as fd:
            child = subprocess.Popen([sys.executable, "-I", "-c",
                                      "import sys; print('ready', flush=True); sys.stdin.read()"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, pass_fds=(fd,))
            self.addCleanup(lambda: child.kill() if child.poll() is None else None)
            self.assertEqual(child.stdout.readline(), "ready\n")
        # Closing the helper's descriptor models its death; the sending child lives.
        with self.assertRaisesRegex(host.HostError, "still in flight"):
            self.ledger.reconcile("request-0", "verified-session", "https://github.com/owner/ops/issues/13")
        child.communicate(timeout=5)
        self.ledger.reconcile("request-0", "verified-session", "https://github.com/owner/ops/issues/13")
        self.assertEqual(self.state(), "RECONCILED")

    def test_missing_or_corrupt_ledger_never_invokes_provider(self):
        for name, content in (("absent", None), ("corrupt", b"not a database")):
            path = Path(self.temp.name) / name
            if content is not None:
                path.write_bytes(content)
            with self.subTest(name=name), self.assertRaises((host.HostError, sqlite3.Error)):
                host.launch(packet(), self.policy, host.Ledger(path), self.invoke)
        self.assertEqual(self.calls, [])
        self.assertFalse((Path(self.temp.name) / "absent").exists())
        with self.assertRaises(FileExistsError):
            self.ledger.initialize()

    def test_adapter_receives_protected_packet_clean_environment_and_root_cwd(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "never-forward", "GITHUB_OUTPUT": "never-forward", "PYTHONPATH": "/untrusted"}):
            def run(args, **kwargs):
                self.assertEqual(args[0], host.WRAPPERS["DEVIN"])
                path = Path(args[1])
                self.assertEqual(path.parent, self.path.parent)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(path.read_text()), packet())
                self.assertEqual(kwargs["cwd"], "/")
                inherited_fd = kwargs["pass_fds"][0]
                self.assertEqual(kwargs["env"], {**host.CLEAN_ENV, "ASTRA_HOST_INFLIGHT_FD": str(inherited_fd)})
                self.assertEqual(os.fstat(inherited_fd).st_uid, os.geteuid())
                self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
                return self.invoke(packet(), self.policy)
            with patch.object(host.subprocess, "run", side_effect=run):
                host.launch(packet(), self.policy, self.ledger)
        self.assertEqual(list(self.path.parent.glob("packet-*")), [])

    def test_preflight_only_calls_preflight_adapter_without_creating_reservation(self):
        report = {"status": "PASS", "builder_id": "DEVIN", "execution_mode": "REMOTE_SESSION",
                  "parallel_safe": True, "launch_contract_version": 2}
        with patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(report))) as invoke:
            result = host.preflight("DEVIN", self.policy, self.ledger)
        self.assertEqual(invoke.call_args.args[0], [host.WRAPPERS["DEVIN"], "--preflight"])
        self.assertEqual(result["host_admission"], "ENFORCED")
        self.assertEqual(result["boundary_evidence_pointer"], self.policy["boundary_evidence_pointer"])
        db = self.ledger.connect()
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM launches").fetchone()[0], 0)
        finally:
            db.close()
        del report["launch_contract_version"]
        with patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(report))):
            with self.assertRaises(host.HostError):
                host.preflight("DEVIN", self.policy, self.ledger)

    def test_policy_and_unix_identity_boundaries_fail_closed(self):
        host.validate_policy(self.policy)
        for changes in ({"control_uid": self.policy["runner_uid"]}, {"max_active_sessions": True},
                        {"enabled_builders": []}, {"boundary_evidence_pointer": "PENDING"},
                        {"wrapper_paths": {"DEVIN": "/tmp/attacker"}}):
            with self.subTest(changes=changes), self.assertRaises(host.HostError):
                host.validate_policy({**self.policy, **changes})
        with patch.object(host.os, "getuid", return_value=1010), patch.object(host.os, "geteuid", return_value=1010):
            with patch.dict(os.environ, {"SUDO_UID": "1020"}):
                host.authorize_identity(self.policy, "launch")
                host.authorize_identity(self.policy, "preflight")
                for command in ("init", "reconcile"):
                    with self.assertRaises(host.HostError):
                        host.authorize_identity(self.policy, command)
            with patch.dict(os.environ, {"SUDO_UID": "1030"}), self.assertRaises(host.HostError):
                host.authorize_identity(self.policy, "launch")
            with patch.dict(os.environ, {"SUDO_UID": "0"}):
                host.authorize_identity(self.policy, "reconcile")
        with patch.object(host.os, "getuid", return_value=1020), self.assertRaises(host.HostError):
            host.authorize_identity(self.policy, "launch")

    def test_unsafe_files_and_duplicate_json_keys_are_rejected(self):
        path = Path(self.temp.name) / "protected"
        path.write_text("x")
        path.chmod(0o600)
        host.protected_leaf(path, os.getuid(), mode=0o600)
        path.chmod(0o666)
        with self.assertRaises(host.HostError):
            host.protected_leaf(path, os.getuid())
        path.chmod(0o600)
        link = path.with_name("symlink")
        link.symlink_to(path)
        with self.assertRaises(host.HostError):
            host.protected_leaf(link, os.getuid())
        os.link(path, path.with_name("hardlink"))
        with self.assertRaises(host.HostError):
            host.protected_leaf(path, os.getuid())
        with self.assertRaises(host.HostError):
            host.parse_json('{"outcome":"CONFIRMED","outcome":"FAILED_PRESTART"}')


if __name__ == "__main__":
    unittest.main()
