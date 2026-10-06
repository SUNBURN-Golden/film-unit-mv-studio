"""Fake remote worker for protocol tests (ANIM-014).

This is **not** a remote deployment — no network, no credentials, no paid
resources. It exercises the WorkerProtocol 1 wire contract the
coordinator-relay path would use: a pinned endpoint, scoped grants, bound
receipts and explicit status queries. Every receipt it emits carries
`evidence: {"class": "FAKE_REMOTE"}` and the route stays
`UNQUALIFIED` — a fake PASS never promotes a real remote route.

Fault injection for the failure matrix (all plain attributes):

- `drop_submit_ack`: the worker accepts and runs, but the submit response
  is lost — the coordinator must land in UNKNOWN and reconcile, never
  resubmit.
- `drop_cancel_ack`: the cancel is applied but its answer is lost —
  CANCEL_REQUESTED -> UNKNOWN, then explicit reconcile confirms.
- `dead`: the runtime is unreachable — every call drops; revive with
  `revive()`.
- `lose(request_id)`: the worker forgot the job (runtime termination
  losing state); status reports NOT_FOUND -> FAILED_CONFIRMED.
- `auto_complete`: finish the range inside submit instead of waiting for
  the explicit `complete()` hook.
"""
from . import (Worker, make_receipt, render_frame_bytes)
from ..core import FilmError
from ..frame_stream import contract_buffer_bytes
from ..fav_pack import sha256_bytes
from ..storage_backends import ConnectionDropped


class FakeRemoteWorker(Worker):
    kind = "REMOTE_CPU"
    evidence_class = "FAKE_REMOTE"
    qualification_state = "UNQUALIFIED"

    def __init__(self, worker_id="remote-1", auto_complete=False,
                 scratch_limit_bytes=None, **kwargs):
        super().__init__(worker_id,
                         endpoint="fake-remote://remote-1",
                         **kwargs)
        self.scratch_limit_bytes = scratch_limit_bytes
        self.auto_complete = auto_complete
        self.drop_submit_ack = False
        self.drop_cancel_ack = False
        self.dead = False
        self._jobs = {}
        self._artifacts = {}

    # -- fault injection ------------------------------------------------------
    def _check_live(self):
        if self.dead:
            raise ConnectionDropped("fake remote runtime is unreachable")

    def lose(self, request_id):
        """Runtime termination that forgets the job entirely."""
        self._jobs.pop(request_id, None)

    def revive(self):
        self.dead = False

    def _evidence(self):
        return {"class": self.evidence_class,
                "qualification_state": self.qualification_state}

    def _receipt(self, packet, kind, covered_range, members=()):
        return make_receipt(
            actor=self.worker_id, job_key=packet["job_key"],
            attempt_id=packet["attempt_id"], request_id=packet["request_id"],
            snapshot_digest=packet["snapshot_digest"],
            plan_revision=packet["plan_revision"],
            covered_range=covered_range, nonce=packet["receipt_nonce"],
            kind=kind, grant_digest=packet["grant"]["grant_digest"],
            members=members, evidence=self._evidence())

    # -- protocol ---------------------------------------------------------------
    def preflight(self, packet, now_ms=0):
        self._check_live()
        self.authenticate(packet, packet["input_bytes"], now_ms)
        start, end = packet["output_range"]
        needed = (end - start) * contract_buffer_bytes(
            packet["frame_contract"])
        if self.scratch_limit_bytes is not None \
                and needed > self.scratch_limit_bytes:
            raise FilmError(f"WORKER_SCRATCH_EXCEEDED: {needed} > "
                            f"{self.scratch_limit_bytes}")
        return True

    def _run(self, packet):
        contract = packet["frame_contract"]
        start, end = packet["output_range"]
        artifacts = {}
        members = []
        for index in range(start, end):
            data = render_frame_bytes(index, contract,
                                      packet["snapshot_digest"],
                                      packet["operation"]["recipe_digest"])
            artifacts[index] = data
            members.append({"frame_index": index,
                            "sha256": sha256_bytes(data)})
        self._artifacts[packet["request_id"]] = artifacts
        return members

    def submit(self, packet, now_ms=0):
        self.preflight(packet, now_ms=now_ms)
        request_id = packet["request_id"]
        start, end = packet["output_range"]
        job = {"packet": packet, "state": "RUNNING", "receipts": []}
        self._jobs[request_id] = job
        accepted = self._receipt(packet, "ACCEPTED",
                                 [start, min(start + 1, end)])
        if self.auto_complete:
            members = self._run(packet)
            job["state"] = "COMPLETE"
            job["receipts"].append(
                self._receipt(packet, "COMPLETE", [start, end], members))
        if self.drop_submit_ack:
            # Work was accepted; only the acknowledgement is lost.
            raise ConnectionDropped("submit acknowledgement lost")
        return accepted

    def complete(self, request_id):
        """Test hook: the running remote job finishes its range."""
        self._check_live()
        job = self._jobs.get(request_id)
        if job is None:
            raise FilmError("complete: no such request")
        if job["state"] != "RUNNING":
            raise FilmError(f"complete: job is {job['state']}")
        packet = job["packet"]
        members = self._run(packet)
        job["state"] = "COMPLETE"
        job["receipts"].append(self._receipt(
            packet, "COMPLETE", packet["output_range"], members))

    def fail(self, request_id):
        """Test hook: the job fails and the failure is confirmed."""
        self._check_live()
        job = self._jobs.get(request_id)
        if job is None:
            raise FilmError("fail: no such request")
        job["state"] = "FAILED"
        packet = job["packet"]
        job["receipts"].append(self._receipt(
            packet, "FAILED_CONFIRMED", packet["output_range"]))

    def attach_resume(self, request_id):
        """Attach to the existing request; a fresh submit never happens."""
        self._check_live()
        job = self._jobs.get(request_id)
        if job is None:
            return {"request_id": request_id, "state": "NOT_FOUND"}
        return {"request_id": request_id, "state": job["state"]}

    def status(self, request_id):
        """One explicit status answer for the coordinator's reconcile."""
        self._check_live()
        job = self._jobs.get(request_id)
        if job is None:
            return {"state": "NOT_FOUND", "receipts": []}
        receipts = list(job["receipts"])
        if job["state"] == "COMPLETE":
            return {"state": "COMPLETE", "receipts": receipts}
        if job["state"] == "CANCELLED":
            return {"state": "CANCELLED", "receipts": receipts}
        if job["state"] == "FAILED":
            return {"state": "FAILED", "receipts": receipts}
        return {"state": "RUNNING", "receipts": receipts}

    def collect_receipts(self, request_id):
        job = self._jobs.get(request_id)
        if job is None:
            return []
        receipts = list(job["receipts"])
        job["receipts"] = []
        return receipts

    def cancel(self, request_id):
        self._check_live()
        job = self._jobs.get(request_id)
        if job is None:
            return {"outcome": "NOT_FOUND", "receipts": []}
        if job["state"] == "COMPLETE":
            # Completion was already confirmed before the cancel arrived;
            # the output still has to be verified, not dropped.
            return {"outcome": "COMPLETED_FIRST",
                    "receipts": list(job["receipts"])}
        packet = job["packet"]
        job["state"] = "CANCELLED"
        receipt = self._receipt(packet, "CANCEL_CONFIRMED",
                                packet["output_range"])
        job["receipts"].append(receipt)
        if self.drop_cancel_ack:
            raise ConnectionDropped("cancel acknowledgement lost")
        return {"outcome": "CANCELLED", "receipts": [receipt]}

    def verify_artifact(self, request_id):
        artifacts = self._artifacts.get(request_id)
        if artifacts is None:
            raise FilmError("verify_artifact: no such request")
        return {str(index): sha256_bytes(data)
                for index, data in artifacts.items()}

    def release_workspace(self, job_key):
        for request_id, job in list(self._jobs.items()):
            if job["packet"]["job_key"] == job_key:
                self._artifacts.pop(request_id, None)
        return True
