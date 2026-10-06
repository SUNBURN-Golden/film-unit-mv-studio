"""Shared synthetic fixtures for ANIM-014 tests.

A tiny FrameStream contract (8x6 RGBA8, 24/1 fps, frames [0, 8)) and an
ExecutionPlan whose two operations tile that range — local, remote or
mixed routes. No network, no credentials, no paid calls anywhere.
"""
import hashlib

from engine.execution_plan import make_execution_plan
from engine.execution_workers import Coordinator
from engine.execution_workers.fake_remote import FakeRemoteWorker
from engine.execution_workers.local import LocalWorker
from engine.frame_stream import make_contract

SNAPSHOT = hashlib.sha256(b"anim-014-snapshot").hexdigest()
RECIPE = hashlib.sha256(b"anim-014-recipe").hexdigest()
RECIPE_B = hashlib.sha256(b"anim-014-recipe-b").hexdigest()
CONN = hashlib.sha256(b"anim-014-connection").hexdigest()
OBJECT = hashlib.sha256(b"anim-014-input-object").hexdigest()
INPUT_MEMBERS = ["input-member-0", "input-member-1"]

WIDTH, HEIGHT, FRAMES = 8, 6, 8
CONTRACT = make_contract(WIDTH, HEIGHT, frame_range=[0, FRAMES])


def edge(edge_id, from_role, to_role, snapshot=SNAPSHOT,
         object_digest=None, member_ids=(), byte_ranges=()):
    return {"edge_id": edge_id, "from_role": from_role, "to_role": to_role,
            "snapshot_digest": snapshot, "object_digest": object_digest,
            "index_digest": None, "member_ids": list(member_ids),
            "byte_ranges": [list(r) for r in byte_ranges],
            "verification": "UPLOADED_UNVERIFIED",
            "max_inflight_bytes": 1 << 20, "spool_limit_bytes": 1 << 20,
            "evidence_ref": None, "measured_at": None,
            "credential_boundary": {
                "issuer": "coordinator", "audience": to_role.lower(),
                "peer": "coordinator-user-desktop",
                "connection_digest": CONN, "credential_epoch": 1,
                "job_key": None, "attempt_id": None,
                "object_digest": None, "member_ids": [], "ranges": [],
                "max_bytes": 1 << 20},
            "expiry_binding": {"expires_at_ms": 2**62,
                               "renewal_scope": "SAME_JOB_SAME_OBJECT"},
            "transport_retry_policy": None,
            "dependencies": [], "completion_receipt_ref": None}


def operation(operation_id, rng, route, worker, recipe=RECIPE,
              halo=(), depends=()):
    return {"operation_id": operation_id, "kind": "RENDER_RANGE",
            "output_range": list(rng), "halo_ranges": [list(h) for h in halo],
            "route": route, "worker": worker, "recipe_digest": recipe,
            "runtime_contract": "python-deterministic-v1",
            "depends_on": list(depends)}


def make_plan(ops, *, policy="FIXED_ROUTE", evidence_required=False,
              allow_charges=False, transfer_route="COORDINATOR_RELAY",
              edges=None, revision=1, snapshot=SNAPSHOT,
              output_frames=FRAMES):
    routes = sorted({o["route"] for o in ops})
    if edges is None:
        edges = []
        if any(o["route"] != "LOCAL_NATIVE" for o in ops):
            # The inbound edge declares the exact input object/members the
            # relay may push — grants bind to this scope.
            edges = [edge("e-in", "COORDINATOR", "WORKER",
                          object_digest=OBJECT,
                          member_ids=INPUT_MEMBERS,
                          byte_ranges=[[0, 4096]]),
                     edge("e-out", "WORKER", "COORDINATOR",
                          )]
    return make_execution_plan(
        snapshot, ops,
        storage={"profile": "LOCAL_FULL", "archive_manifest": None},
        execution={"policy": policy, "allowed_routes": routes,
                   "capability_evidence_required": evidence_required,
                   "allow_additional_charges": allow_charges},
        encoding={"driver_policy": "FIXED", "allowed_drivers": ["FFMPEG"],
                  "delivery_profile": "MV_H264_AAC_V1", "width": WIDTH,
                  "height": HEIGHT, "fps": {"num": 24, "den": 1},
                  "output_frames": output_frames},
        workspace={"pc_cache_limit_bytes": 1 << 24,
                   "worker_scratch_limit_bytes": 1 << 24,
                   "on_limit": "PAUSE"},
        transfer_route=transfer_route,
        transfer_edges=edges,
        resource_reservations=[{"location": "USER_DESKTOP",
                                "peak_bytes": 1 << 24}],
        plan_revision=revision)


def remote_plan(worker="remote-1", **kwargs):
    """Two remote operations tiling [0, FRAMES)."""
    return make_plan(
        [operation("op-a", [0, FRAMES // 2], "REMOTE_CPU", worker),
         operation("op-b", [FRAMES // 2, FRAMES], "REMOTE_CPU", worker)],
        **kwargs)


def local_plan(worker="local-1", **kwargs):
    return make_plan(
        [operation("op-a", [0, FRAMES // 2], "LOCAL_NATIVE", worker),
         operation("op-b", [FRAMES // 2, FRAMES], "LOCAL_NATIVE", worker)],
        edges=[], **kwargs)


def coordinator(tmp_path, name="state", **kwargs):
    return Coordinator(tmp_path / name, **kwargs)


def drive(c, worker, key, plan, *, verify=True, seal=False):
    """reserve -> submit -> collect -> verify -> (optional) seal."""
    assert c.reserve(key) == "RESERVED"
    c.submit(worker, key, frame_contract=dict(CONTRACT))
    c.collect(worker, key)
    job = c.jobs[key]
    if verify and job["state"] == "OUTPUT_PENDING_VERIFY":
        assert c.verify_outputs(key) == "VERIFIED"
        if seal:
            c.seal(key)
            assert c.jobs[key]["state"] == "ARCHIVED"
    return job


def receipt_for(job, kind, rng, *, members=(), actor=None, nonce=None,
                attempt=None, snapshot=None, revision=None,
                grant=None, evidence=None):
    """A receipt bound like a real worker's, with tamperable fields."""
    from engine.execution_workers import make_receipt
    return make_receipt(
        actor=actor or job["worker_id"], job_key=job["job_key"],
        attempt_id=attempt if attempt is not None else job["attempt_id"],
        request_id=job["request_id"],
        snapshot_digest=snapshot or job["snapshot_digest"],
        plan_revision=revision or job["plan_revision"],
        covered_range=list(rng),
        nonce=nonce if nonce is not None else job["nonce"], kind=kind,
        grant_digest=grant or job["grant_digest"], members=members,
        evidence=evidence or {"class": "FAKE_REMOTE",
                              "qualification_state": "UNQUALIFIED"})


def members_for(c, job, rng):
    """Expected member digests the coordinator independently recomputes."""
    from engine.execution_workers import expected_frame_digest
    return [{"frame_index": i,
             "sha256": expected_frame_digest(i, job["frame_contract"],
                                             job["snapshot_digest"],
                                             job["operation"]
                                             ["recipe_digest"])}
            for i in range(*rng)]
