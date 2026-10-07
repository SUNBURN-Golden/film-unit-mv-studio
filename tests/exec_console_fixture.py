"""Standalone AppTest driver for app/execution_console.py.

Not a pytest module — `streamlit run` entry point used by
tests/test_film_execution_console_ui.py. Environment:

- EXEC_FIXTURE_DIR      writable scratch dir (state/ + commit/ inside)
- EXEC_FIXTURE_SCENARIO empty | nodir | running | cancel_requested |
                        race | unknown | disconnect | verify_fails |
                        fenced | commit | pipeline | project_running |
                        project_nodir
                        (project_* drives the panel the way the control
                        panel does — project=, no explicit state_dir)

Everything is built on the local fakes (FakeRemoteWorker /
FakeDriveBackend) — no network, no credentials, UNQUALIFIED.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import streamlit as st

from app.execution_console import render_execution_console

base = Path(os.environ["EXEC_FIXTURE_DIR"])
scenario = os.environ.get("EXEC_FIXTURE_SCENARIO", "empty")
state_dir = base / "render" / "execution" \
    if scenario == "project_running" else base / "state"
commit_dir = base / "commit"


def _build():
    from engine.execution_workers import Coordinator
    from engine.execution_workers.fake_remote import FakeRemoteWorker
    from engine.archive_commit import ArchiveCommitter
    from engine.storage_backends.fake_drive import FakeDriveBackend
    from anim_013_kit import make_members, make_png
    from anim_014_kit import CONTRACT, receipt_for, remote_plan

    class SlowCancel(FakeRemoteWorker):
        def cancel(self, request_id):
            self._check_live()
            if request_id not in self._jobs:
                return {"outcome": "NOT_FOUND", "receipts": []}
            return {"outcome": "RUNNING", "receipts": []}

    if scenario != "nodir":
        state_dir.mkdir(parents=True, exist_ok=True)
    commit_dir.mkdir(parents=True, exist_ok=True)
    c = Coordinator(state_dir) if state_dir.is_dir() else None
    worker = (SlowCancel() if scenario == "cancel_requested"
              else FakeRemoteWorker())
    backend = FakeDriveBackend(provider_checksum="sha256")

    if scenario in {"running", "cancel_requested", "race", "unknown",
                    "disconnect", "verify_fails", "fenced",
                    "project_running"}:
        plan = remote_plan()
        key = c.plan_jobs(plan)[0]
        c.reserve(key)
        if scenario == "unknown":
            worker.drop_submit_ack = True
            c.submit(worker, key, frame_contract=dict(CONTRACT))
        elif scenario == "race":
            worker.auto_complete = True
            c.submit(worker, key, frame_contract=dict(CONTRACT))
            c.collect(worker, key)
            c.request_cancel(worker, key)
        elif scenario == "disconnect":
            c.submit(worker, key, frame_contract=dict(CONTRACT))
            worker.dead = True
            c.reconcile(worker, key)     # dropped status answer -> UNKNOWN
        elif scenario == "verify_fails":
            c.submit(worker, key, frame_contract=dict(CONTRACT))
            job = c.jobs[key]
            bad = [{"frame_index": i, "sha256": "0" * 64}
                   for i in range(*job["output_range"])]
            c.receive_receipt(receipt_for(job, "COMPLETE",
                                          job["output_range"],
                                          members=bad), worker)
        elif scenario == "fenced":
            c.submit(worker, key, frame_contract=dict(CONTRACT))
            raw = (state_dir / "job_journal.jsonl").read_bytes()
            (state_dir / "job_journal.jsonl").write_bytes(raw[:-10])
            c = Coordinator(state_dir)      # replays a torn tail
        else:
            c.submit(worker, key, frame_contract=dict(CONTRACT))
            if scenario == "cancel_requested":
                c.request_cancel(worker, key)

    if scenario == "commit":
        cc = ArchiveCommitter(commit_dir, backend,
                              upload={"chunk_bytes": 128,
                                      "resumable": True},
                              retry={"max_retries": 0})
        snap = __import__("hashlib").sha256(b"console-snapshot").hexdigest()
        ck = cc.begin_commit(
            "build-1",
            [{"kind": "frames", "members": make_members(3),
              "snapshot_digest": snap,
              "recipe_digest":
                  __import__("hashlib").sha256(b"console-recipe").hexdigest(),
              "sequence_digest":
                  __import__("hashlib").sha256(b"seq").hexdigest()},
             {"kind": "preview", "data": make_png(9)}],
            snapshot_digest=snap,
            recipe_digest=__import__("hashlib").sha256(
                b"console-recipe").hexdigest(),
            toolchain_digest=__import__("hashlib").sha256(
                b"console-toolchain").hexdigest(),
            required_kinds=("frames", "preview"), coverage=[0, 3])
        cc.run(ck)

    perf_roots = []
    if scenario == "pipeline":
        # a fully rendered, delivery-verified snapshot with no archive
        # commit recorded yet — the pipeline row is work in hand
        perf = base / "perf"
        perf.mkdir()
        (perf / "state.json").write_text(json.dumps({
            "state_version": 1,
            "input": {"snapshot_digest":
                      __import__("hashlib").sha256(
                          b"console-snapshot").hexdigest()},
            "clean": {"sequence_root": "c" * 64, "frames": [{"key": "k"}]},
            "subbed": {"sequence_root": "s" * 64},
            "encodes": {"clean": {"artifact_sha256": "a" * 64,
                                  "verification": {"valid": True}},
                        "subbed": {"artifact_sha256": "b" * 64,
                                   "verification": {"valid": True}}},
            "generated_at": "2026-01-01T00:00:00Z"}), encoding="utf-8")
        perf_roots = [perf]

    return {"coordinator": c, "worker": worker, "state_dir": state_dir,
            "commit_dir": commit_dir, "backend": backend,
            "perf_roots": perf_roots}


if "exec_fixture" not in st.session_state:
    st.session_state["exec_fixture"] = _build()
fx = st.session_state["exec_fixture"]

kwargs = dict(
    coordinator=fx["coordinator"],
    journal_dirs=[fx["commit_dir"]],
    workers={fx["worker"].worker_id: fx["worker"]}
    if fx["worker"] else {},
    backends={str(fx["commit_dir"]): fx["backend"]},
    perf_roots=fx["perf_roots"])

if scenario in ("project_running", "project_nodir"):
    # the control-panel shape: the project path only — the panel must
    # derive <project>/render/execution itself and never offer a
    # free-text path field
    render_execution_console(project=base, **kwargs)
else:
    render_execution_console(state_dir=fx["state_dir"], **kwargs)
