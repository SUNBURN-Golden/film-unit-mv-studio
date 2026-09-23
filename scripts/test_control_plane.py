"""Launch boundary regressions; no provider or GitHub network access."""
import json
import hashlib
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import control_plane as cp


class DispatchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.record = dict(repository="owner/repo", task_id="T1", task_revision="1",
                           builder_id="DEVIN", launch_request_id="request-1", attempt_id=1,
                           launch_state="SUBMITTING", owner_session_id=None)

    def test_no_control_credentials_or_runner_files_reach_host(self):
        response = subprocess.CompletedProcess([], 0, '{"status":"PASS"}', '')
        with patch.dict(os.environ, {"GITHUB_TOKEN": "sensitive", "GITHUB_OUTPUT": "/runner/out",
                                     "PYTHONPATH": "/unsafe", "HOME": "/runner"}), \
             patch.object(cp.subprocess, "run", return_value=response) as run:
            cp.host_call(["preflight", "--builder-id", "DEVIN"])
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        self.assertEqual(run.call_args.kwargs["cwd"], "/")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_runtime_workflow_cannot_import_checkout_shadow_modules(self):
        workflow = (cp.ROOT / ".github/workflows/control-plane-runtime.yml").read_text()
        commands = re.findall(r"\bpython3([^\n]*?)scripts/control_plane\.py", workflow)
        self.assertTrue(commands)
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "scripts/control_plane.py"
            script.parent.mkdir()
            script.write_text(Path(cp.__file__).read_text())
            sentinel = Path(tmp) / "injected"
            (script.parent / "argparse.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).write_text('executed before gates')\n"
                "raise RuntimeError('checkout shadow module executed')\n"
            )
            for flags in set(commands):
                result = subprocess.run(["python3", *shlex.split(flags), str(script), "self-test"],
                                        cwd=tmp, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(sentinel.exists())

    def test_old_success_cannot_survive_response_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, result = Path(tmp)/"packet", Path(tmp)/"result"
            packet.write_text(json.dumps(self.record))
            result.write_text(json.dumps({**self.record, "outcome": "CONFIRMED", "session_id": "old"}))
            with patch.object(cp, "require_runtime_enabled"), \
                 patch.object(cp, "host_call", side_effect=cp.ControlPlaneError("response lost")):
                cp.launch_dispatch(packet, result)
            actual = json.loads(result.read_text())
            self.assertEqual(actual["outcome"], "UNKNOWN")
            self.assertEqual(actual["launch_request_id"], "request-1")
            self.assertNotIn("session_id", actual)

    def test_disabled_runtime_never_reaches_launch_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, result = Path(tmp)/"packet", Path(tmp)/"result"
            packet.write_text(json.dumps(self.record))
            with patch.object(cp, "require_runtime_enabled", side_effect=cp.ControlPlaneError("disabled")), \
                 patch.object(cp, "host_call") as host:
                cp.launch_dispatch(packet, result)
            host.assert_not_called()
            self.assertEqual(json.loads(result.read_text())["outcome"], "UNKNOWN")

    def test_wrong_attempt_result_is_unknown(self):
        for key, wrong in (("launch_request_id", "old"), ("attempt_id", 2), ("attempt_id", True),
                           ("task_revision", "2"), ("builder_id", "GLM")):
            result = {**self.record, "outcome": "CONFIRMED", "session_id": "session", key: wrong}
            self.assertEqual(cp.bound_result(result, self.record)["outcome"], "UNKNOWN")

    def test_stale_finalization_cannot_mutate_control_record(self):
        api = Mock()
        api.comments.return_value = [{"id": 1, "user": {"login": "github-actions[bot]"},
                                      "body": cp.render_control_record(self.record)}]
        with patch.object(cp, "load_config", return_value={"repository": "owner/repo", "control_record_actor": "github-actions[bot]"}), \
             patch.object(cp, "GithubApi", return_value=api), patch.dict(os.environ, {"GITHUB_TOKEN": "token"}):
            with self.assertRaises(cp.ControlPlaneError):
                cp.finalize_dispatch(1, Path("absent-result"), "old-request")
        api.update_comment.assert_not_called()

    def test_duplicate_returns_before_preflight(self):
        issue_url = "https://github.com/owner/repo/issues/1"
        envelope = {key: "value" for key in cp.REQUIRED_TASK_FIELDS}
        envelope.update(TASK_ID="T1", TASK_REVISION="1", BUILDER_ID="DEVIN",
                        CANONICAL_TASK_POINTER=issue_url, CONTROL_RECORD_POINTER=issue_url)
        api = Mock()
        api.issue.return_value = {"state": "open", "html_url": issue_url,
                                  "user": {"login": "owner"}, "body": "\n".join(f"{k}: {v}" for k,v in envelope.items())}
        api.comments.return_value = [{"id": 1, "user": {"login": "github-actions[bot]"},
                                      "body": cp.render_control_record({**self.record, "launch_state": "CONFIRMED"})}]
        cfg = {"repository": "owner/repo", "control_record_actor": "github-actions[bot]",
               "allowed_task_actors": ["owner"], "allowed_dispatch_actors": ["owner"]}
        with patch.object(cp, "load_config", return_value=cfg), patch.object(cp, "require_runtime_enabled"), \
             patch.object(cp, "validate_task"), patch.object(cp, "GithubApi", return_value=api), \
             patch.object(cp, "host_preflight") as preflight, patch.object(cp, "write_github_output"), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "token", "GITHUB_ACTOR": "owner", "GITHUB_TRIGGERING_ACTOR": "owner",
                 "EXPECTED_TASK_ID": "T1", "EXPECTED_TASK_REVISION": "1", "EXPECTED_BUILDER_ID": "DEVIN",
                 "EXPECTED_ISSUE_BODY_SHA256": hashlib.sha256(api.issue.return_value["body"].encode()).hexdigest()}):
            cp.prepare_dispatch(1, Path("must-not-write"))
        preflight.assert_not_called()
        api.update_comment.assert_not_called()

    def test_dispatch_binding_rejects_queued_edits_and_missing_authorization(self):
        body = "TASK_ID: T1\nTASK_REVISION: 1\nBUILDER_ID: DEVIN"
        envelope = dict(TASK_ID="T1", TASK_REVISION="1", BUILDER_ID="DEVIN")
        expected = dict(EXPECTED_TASK_ID="T1", EXPECTED_TASK_REVISION="1", EXPECTED_BUILDER_ID="DEVIN",
                        EXPECTED_ISSUE_BODY_SHA256=hashlib.sha256(body.encode()).hexdigest())
        with patch.dict(os.environ, expected, clear=True):
            cp.verify_dispatch_binding(body, envelope)
            for field in envelope:
                with self.assertRaises(cp.ControlPlaneError):
                    cp.verify_dispatch_binding(body, dict(envelope, **{field: "changed"}))
            with self.assertRaises(cp.ControlPlaneError):
                cp.verify_dispatch_binding(body + "\nnew instructions", envelope)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(cp.ControlPlaneError): cp.verify_dispatch_binding(body, envelope)

    def test_changed_runtime_cannot_reuse_old_activation(self):
        activation = {"runtime_enabled": True, "activated_runtime_sha": "a" * 40}
        responses = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1)]
        with patch.object(cp, "load_activation", return_value=activation), patch.object(cp, "validate_repo"), \
             patch.object(cp.subprocess, "run", side_effect=responses):
            with self.assertRaises(cp.ControlPlaneError):
                cp.require_runtime_enabled()

    def test_dirty_runtime_cannot_reuse_activation(self):
        activation = {"runtime_enabled": True, "activated_runtime_sha": "a" * 40}
        responses = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
                     subprocess.CompletedProcess([], 1)]
        with patch.object(cp, "load_activation", return_value=activation), patch.object(cp, "validate_repo"), \
             patch.object(cp.subprocess, "run", side_effect=responses):
            with self.assertRaises(cp.ControlPlaneError):
                cp.require_runtime_enabled()


if __name__ == "__main__":
    unittest.main()
