"""Standalone AppTest driver for app/execution_console.py.

Not a pytest module — `streamlit run` entry point used by
tests/test_film_execution_console_ui.py. Environment:

- EXEC_FIXTURE_DIR      writable scratch dir (state/ + commit/ inside)
- EXEC_FIXTURE_SCENARIO empty | nodir | running | cancel_requested |
                        race | unknown | fenced | commit

Everything is built on the local fakes (FakeRemoteWorker /
FakeDriveBackend) — no network, no credentials, UNQUALIFIED.
"""
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
state_dir = base / "state"
commit_dir = base / "commit"


def _build():
    from engine.execution_workers import Coordinator
    from engine.execution_workers.fake_remote import FakeRemoteWorker
    from engine.archive_commit import ArchiveCommitter
    from engine.storage_backends.fake_drive import FakeDriveBackend
    from anim_013_kit import make_members, make_png
    from anim_014_kit import CONTRACT, remote_plan

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
                    "fenced"}:
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

    return {"coordinator": c, "worker": worker, "state_dir": state_dir,
            "commit_dir": commit_dir, "backend": backend}


if "exec_fixture" not in st.session_state:
    st.session_state["exec_fixture"] = _build()
fx = st.session_state["exec_fixture"]

render_execution_console(
    state_dir=fx["state_dir"], coordinator=fx["coordinator"],
    journal_dirs=[fx["commit_dir"]],
    workers={fx["worker"].worker_id: fx["worker"]}
    if fx["worker"] else {},
    backends={str(fx["commit_dir"]): fx["backend"]})
