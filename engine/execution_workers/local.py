"""Local native worker (ANIM-014).

Executes a frame-range render on this machine through the exact same
packet/grant/FrameStream contract the remote path uses — the job key,
receipts and frame digests are identical for the same plan, which is what
"local and remote share one frame contract" means. The render recipe is
the deterministic `render_frame_bytes` pinned by the plan's
`recipe_digest` + `snapshot_digest`; the coordinator still re-derives
every digest for verification — a worker COMPLETE is never trusted.

Synchronous compute: `submit` runs the range, queues the ACCEPTED receipt
and the COMPLETE receipt in the worker's outbox; `collect_receipts` is
the coordinator's explicit drain — no polling loop exists here.
"""
from . import (Worker, make_receipt, render_frame_bytes)
from ..core import FilmError
from ..frame_stream import contract_buffer_bytes
from ..fav_pack import sha256_bytes


class LocalWorker(Worker):
    kind = "LOCAL_NATIVE"
    evidence_class = "LOCAL_EXECUTION"
    # A real local run still proves only this box, this scope.
    qualification_state = "UNQUALIFIED"

    def __init__(self, worker_id="local-1", scratch_limit_bytes=None,
                 **kwargs):
        super().__init__(worker_id, **kwargs)
        self.scratch_limit_bytes = scratch_limit_bytes
        self._outbox = {}
        self._artifacts = {}
        self._job_workspaces = {}

    def _evidence(self):
        return {"class": self.evidence_class,
                "qualification_state": self.qualification_state}

    def preflight(self, packet, now_ms=0):
        self.authenticate(packet, packet["input_bytes"], now_ms)
        contract = packet["frame_contract"]
        start, end = packet["output_range"]
        needed = (end - start) * contract_buffer_bytes(contract)
        if self.scratch_limit_bytes is not None \
                and needed > self.scratch_limit_bytes:
            raise FilmError(
                f"WORKER_SCRATCH_EXCEEDED: range needs {needed} bytes > "
                f"cap {self.scratch_limit_bytes}")
        return True

    def submit(self, packet, now_ms=0):
        self.preflight(packet, now_ms=now_ms)
        operation = packet["operation"]
        contract = packet["frame_contract"]
        request_id = packet["request_id"]
        job_key = packet["job_key"]
        start, end = packet["output_range"]
        members = []
        artifacts = {}
        for index in range(start, end):
            data = render_frame_bytes(index, contract,
                                      packet["snapshot_digest"],
                                      operation["recipe_digest"])
            artifacts[index] = data
            members.append({"frame_index": index,
                            "sha256": sha256_bytes(data)})
        self._artifacts[request_id] = artifacts
        self._job_workspaces[job_key] = {"request_id": request_id,
                                         "bytes": len(artifacts)
                                         * contract_buffer_bytes(contract)}
        accepted = make_receipt(
            actor=self.worker_id, job_key=job_key,
            attempt_id=packet["attempt_id"], request_id=request_id,
            snapshot_digest=packet["snapshot_digest"],
            plan_revision=packet["plan_revision"],
            covered_range=[start, start + 1] if start < end else [start, end],
            nonce=packet["receipt_nonce"], kind="ACCEPTED",
            grant_digest=packet["grant"]["grant_digest"], members=(),
            evidence=self._evidence())
        complete = make_receipt(
            actor=self.worker_id, job_key=job_key,
            attempt_id=packet["attempt_id"], request_id=request_id,
            snapshot_digest=packet["snapshot_digest"],
            plan_revision=packet["plan_revision"],
            covered_range=[start, end], nonce=packet["receipt_nonce"],
            kind="COMPLETE", grant_digest=packet["grant"]["grant_digest"],
            members=members, evidence=self._evidence())
        self._outbox.setdefault(request_id, []).append(complete)
        return accepted

    def attach_resume(self, request_id):
        """Attach to an existing request — never a fresh submit."""
        if request_id not in self._artifacts:
            raise FilmError("attach_resume: no such request on this worker")
        return {"request_id": request_id, "state": "COMPLETE"}

    def status(self, request_id):
        if request_id in self._artifacts:
            return {"state": "COMPLETE",
                    "receipts": list(self._outbox.get(request_id, []))}
        return {"state": "NOT_FOUND", "receipts": []}

    def collect_receipts(self, request_id):
        outbox = self._outbox.get(request_id, [])
        self._outbox[request_id] = []
        return outbox

    def cancel(self, request_id):
        if request_id in self._artifacts:
            return {"outcome": "COMPLETED_FIRST",
                    "receipts": list(self._outbox.get(request_id, []))}
        return {"outcome": "CANCELLED", "receipts": []}

    def verify_artifact(self, request_id):
        """Worker-side artifact listing; coordinator verification is the
        independent re-derivation, not this answer."""
        artifacts = self._artifacts.get(request_id)
        if artifacts is None:
            raise FilmError("verify_artifact: no such request")
        return {str(index): sha256_bytes(data)
                for index, data in artifacts.items()}

    def release_workspace(self, job_key):
        self._job_workspaces.pop(job_key, None)
        return True
