"""FRAME_ANIMATION_V1 manual execution packets (ANIM-016, exec §7.3,
module map §10.1 `execution_packets.py`).

A `subscription_packet` is the fixed hand-off the user carries into a
subscription service UI: plan identity, snapshot hash, input index with
hashes, required files/permissions, the pinned worker script (version +
sha256 — the app ships it, nothing downloads or runs arbitrary code),
the execution unit (job/attempt/request + frame range), the expected
output digests, the verification conditions and the receipt format.

The route is MANUAL: a consumer site with no official execution API is
never driven by UI automation, a standing daemon or polling — the user
runs the shipped script inside the service and the app imports the
result through `import_result`, which re-verifies every byte and feeds
the receipt through the Coordinator's existing receipt/coverage/verify
path. Stale, foreign or partial results are rejected there and here.
"""
from pathlib import Path
import hashlib
import shutil

from . import subscription_worker
from .animation_schema import (canon_bytes, check_document, read_canon,
                               write_canon)
from .core import FilmError, digest, now, safe_path
from .execution_plan import plan_sha
from .execution_workers import (check_receipt, expected_frame_digest,
                                render_frame_bytes)
from .execution_workers.subscription import (audit_worker_source,
                                             charge_check, consume,
                                             entitlement_expired,
                                             entitlement_sha,
                                             evidence_registry_state,
                                             list_evidence, quote_digest,
                                             route_label,
                                             subscription_scope_covered,
                                             worker_script_sha256,
                                             worker_script_version)
from .fav_pack import png_header, sha256_bytes
from .frame_stream import make_contract, contract_buffer_bytes

PACKET_TYPE = "subscription_packet"
RESULT_TYPE = "subscription_result"
SCRIPT_NAME = "film_unit_worker.py"

PACKET_FIELDS = {"document_type", "schema_version", "packet_id", "service",
                 "route", "worker", "execution_unit", "plan",
                 "frame_contract", "inputs", "expected_output",
                 "verification", "receipt", "entitlement", "limits",
                 "issued_at"}
ROUTE_FIELDS = {"transfer_route", "route_label", "automation"}
PACKET_WORKER_FIELDS = {"worker_id", "script", "version", "sha256"}
UNIT_FIELDS = {"job_key", "attempt_id", "request_id", "packet_index",
               "packet_count", "output_range", "frame_range"}
PLAN_FIELDS = {"plan_sha", "plan_revision", "snapshot_digest",
               "operation_id", "kind", "recipe_digest", "runtime_contract"}
INPUTS_FIELDS = {"files", "permissions"}
INPUT_FILE_FIELDS = {"path", "sha256", "bytes"}
PERMISSION_FIELDS = {"read", "write", "network", "gpu"}
OUTPUT_FIELDS = {"dir", "members", "member_count", "bytes"}
OUTPUT_MEMBER_FIELDS = {"frame_index", "sha256"}
VERIFY_FIELDS = {"independent", "conditions"}
RECEIPT_FIELDS = {"format", "actor", "nonce_digest", "grant_digest",
                  "evidence"}
PACKET_ENTITLEMENT_FIELDS = {"digest", "account_binding",
                             "credential_epoch", "quote_digest", "service"}

RESULT_FIELDS = {"document_type", "schema_version", "packet_id",
                 "job_key", "attempt_id", "request_id", "snapshot_digest",
                 "plan_revision", "actor", "worker_script_sha256",
                 "worker_script_version", "session_epoch", "frame_range",
                 "outputs", "receipt_nonce", "grant_digest", "state",
                 "error", "produced_at", "environment"}
RESULT_OUTPUT_FIELDS = {"member_id", "frame_index", "file", "file_sha256",
                        "pixel_sha256"}
RESULT_STATES = {"COMPLETE", "FAILED"}

# caps needed to promise the packet fits the service at all.
REQUIRED_CAPS = ("max_input_bytes", "max_output_bytes",
                 "max_file_count", "max_frames_per_packet")


def _sha(value, what, nullable=False):
    if value is None and nullable:
        return value
    if type(value) is not str or len(value) != 64 \
            or any(c not in "0123456789abcdef" for c in value):
        raise FilmError(f"{what} must be a lowercase SHA-256")
    return value


def _int(value, what, minimum=0):
    if type(value) is not int or value < minimum:
        raise FilmError(f"{what} must be an integer >= {minimum}")
    return value


def _range(value, what):
    if type(value) is not list or len(value) != 2 \
            or any(type(v) is not int or v < 0 for v in value) \
            or value[1] <= value[0]:
        raise FilmError(f"{what} must be a non-empty [start, end) range")
    return value


def validate_subscription_packet(document):
    """Structural contract of `subscription_packet` 1."""
    check_document(document, PACKET_TYPE)
    if set(document.keys()) != PACKET_FIELDS:
        raise FilmError("subscription_packet must hold exactly "
                        f"{sorted(PACKET_FIELDS)}")
    _str(document["packet_id"], "packet_id")
    _str(document["service"], "packet.service")
    route = document["route"]
    if type(route) is not dict or set(route.keys()) != ROUTE_FIELDS:
        raise FilmError(f"packet.route must hold {sorted(ROUTE_FIELDS)}")
    if route["transfer_route"] != "MANUAL_PACKET":
        raise FilmError("a subscription packet is always MANUAL_PACKET")
    if route["route_label"] not in {"AUTOMATIC", "MANUAL"}:
        raise FilmError("packet.route.route_label must be AUTOMATIC or "
                        "MANUAL — the labelling is explicit")
    if route["automation"] != "NONE":
        raise FilmError("consumer service UIs are never automated; "
                        "route.automation is NONE")
    worker = document["worker"]
    if type(worker) is not dict or set(worker.keys()) != PACKET_WORKER_FIELDS:
        raise FilmError(f"packet.worker must hold "
                        f"{sorted(PACKET_WORKER_FIELDS)}")
    _str(worker["worker_id"], "packet.worker.worker_id")
    if worker["script"] != SCRIPT_NAME:
        raise FilmError(f"the shipped worker is {SCRIPT_NAME}")
    _int(worker["version"], "worker.version", 1)
    _sha(worker["sha256"], "worker.sha256")
    unit = document["execution_unit"]
    if type(unit) is not dict or set(unit.keys()) != UNIT_FIELDS:
        raise FilmError(f"packet.execution_unit must hold "
                        f"{sorted(UNIT_FIELDS)}")
    _sha(unit["job_key"], "execution_unit.job_key")
    _int(unit["attempt_id"], "execution_unit.attempt_id", 1)
    _str(unit["request_id"], "execution_unit.request_id")
    _int(unit["packet_index"], "execution_unit.packet_index")
    _int(unit["packet_count"], "execution_unit.packet_count", 1)
    if unit["packet_index"] >= unit["packet_count"]:
        raise FilmError("packet_index >= packet_count")
    _range(unit["output_range"], "execution_unit.output_range")
    chunk = _range(unit["frame_range"], "execution_unit.frame_range")
    if chunk[0] < unit["output_range"][0] \
            or chunk[1] > unit["output_range"][1]:
        raise FilmError("frame_range must stay inside output_range")
    plan = document["plan"]
    if type(plan) is not dict or set(plan.keys()) != PLAN_FIELDS:
        raise FilmError(f"packet.plan must hold {sorted(PLAN_FIELDS)}")
    for field in ("plan_sha", "snapshot_digest", "recipe_digest"):
        _sha(plan[field], f"plan.{field}")
    _int(plan["plan_revision"], "plan.plan_revision", 1)
    for field in ("operation_id", "kind", "runtime_contract"):
        _str(plan[field], f"plan.{field}")
    from .frame_stream import validate_contract
    validate_contract(document["frame_contract"])
    inputs = document["inputs"]
    if type(inputs) is not dict or set(inputs.keys()) != INPUTS_FIELDS:
        raise FilmError("packet.inputs must hold files and permissions")
    seen = set()
    for position, entry in enumerate(inputs["files"]):
        if type(entry) is not dict \
                or set(entry.keys()) != INPUT_FILE_FIELDS:
            raise FilmError(f"inputs.files[{position}] must hold "
                            f"{sorted(INPUT_FILE_FIELDS)}")
        _str(entry["path"], "inputs.files[].path")
        if entry["path"] != entry["path"].lstrip("./") \
                or ".." in Path(entry["path"]).parts \
                or Path(entry["path"]).is_absolute():
            raise FilmError("input paths stay inside the packet dir")
        if entry["path"] in seen:
            raise FilmError(f"duplicate input path {entry['path']}")
        seen.add(entry["path"])
        _sha(entry["sha256"], "inputs.files[].sha256")
        _int(entry["bytes"], "inputs.files[].bytes", 1)
    permissions = inputs["permissions"]
    if type(permissions) is not dict \
            or set(permissions.keys()) != PERMISSION_FIELDS:
        raise FilmError(f"inputs.permissions must hold "
                        f"{sorted(PERMISSION_FIELDS)}")
    for field in ("read", "write"):
        if type(permissions[field]) is not list:
            raise FilmError(f"permissions.{field} must be a list")
    if permissions["network"] != "NONE" or permissions["gpu"] != "NONE":
        raise FilmError("the packet contract is no-network, no-GPU")
    expected = document["expected_output"]
    if type(expected) is not dict or set(expected.keys()) != OUTPUT_FIELDS:
        raise FilmError(f"expected_output must hold "
                        f"{sorted(OUTPUT_FIELDS)}")
    members = expected["members"]
    if type(members) is not list \
            or expected["member_count"] != len(members):
        raise FilmError("expected_output.members must match member_count")
    indices = []
    for member in members:
        if type(member) is not dict \
                or set(member.keys()) != OUTPUT_MEMBER_FIELDS:
            raise FilmError("expected_output members hold "
                            "frame_index and sha256")
        _int(member["frame_index"], "member.frame_index")
        _sha(member["sha256"], "member.sha256")
        indices.append(member["frame_index"])
    if indices != list(range(chunk[0], chunk[1])):
        raise FilmError("expected_output must cover frame_range exactly")
    _int(expected["bytes"], "expected_output.bytes", 1)
    verification = document["verification"]
    if type(verification) is not dict \
            or set(verification.keys()) != VERIFY_FIELDS:
        raise FilmError("packet.verification holds independent and "
                        "conditions")
    if type(verification["conditions"]) is not list:
        raise FilmError("verification.conditions must be a list")
    receipt = document["receipt"]
    if type(receipt) is not dict or set(receipt.keys()) != RECEIPT_FIELDS:
        raise FilmError(f"packet.receipt must hold {sorted(RECEIPT_FIELDS)}")
    if receipt["format"] != "worker_protocol_1":
        raise FilmError("receipt.format is worker_protocol_1")
    _str(receipt["actor"], "receipt.actor")
    _sha(receipt["nonce_digest"], "receipt.nonce_digest")
    _sha(receipt["grant_digest"], "receipt.grant_digest")
    entitlement = document["entitlement"]
    if type(entitlement) is not dict \
            or set(entitlement.keys()) != PACKET_ENTITLEMENT_FIELDS:
        raise FilmError(f"packet.entitlement must hold "
                        f"{sorted(PACKET_ENTITLEMENT_FIELDS)}")
    _sha(entitlement["digest"], "entitlement.digest")
    _sha(entitlement["account_binding"], "entitlement.account_binding")
    _sha(entitlement["quote_digest"], "entitlement.quote_digest")
    _int(entitlement["credential_epoch"], "entitlement.credential_epoch", 1)
    if entitlement["service"] != document["service"]:
        raise FilmError("packet.entitlement.service must match "
                        "packet.service")
    limits = document["limits"]
    if type(limits) is not dict:
        raise FilmError("packet.limits must record the declared caps")
    if limits.get("network") != "NONE" or limits.get("gpu") != "NONE":
        raise FilmError("packet.limits must declare network/gpu NONE")
    _str(document["issued_at"], "issued_at")
    return document


def validate_subscription_result(document):
    """Structural contract of `subscription_result` 1 — the manifest the
    shipped worker script writes next to its outputs."""
    check_document(document, RESULT_TYPE)
    if set(document.keys()) != RESULT_FIELDS:
        raise FilmError(f"subscription_result must hold exactly "
                        f"{sorted(RESULT_FIELDS)}")
    _str(document["packet_id"], "result.packet_id")
    _sha(document["job_key"], "result.job_key")
    _int(document["attempt_id"], "result.attempt_id", 1)
    _str(document["request_id"], "result.request_id")
    _sha(document["snapshot_digest"], "result.snapshot_digest")
    _int(document["plan_revision"], "result.plan_revision", 1)
    _str(document["actor"], "result.actor")
    _sha(document["worker_script_sha256"], "result.worker_script_sha256")
    _int(document["worker_script_version"],
         "result.worker_script_version", 1)
    _int(document["session_epoch"], "result.session_epoch", 1)
    _range(document["frame_range"], "result.frame_range")
    seen = set()
    for position, out in enumerate(document["outputs"]):
        if type(out) is not dict \
                or set(out.keys()) != RESULT_OUTPUT_FIELDS:
            raise FilmError(f"result.outputs[{position}] must hold "
                            f"{sorted(RESULT_OUTPUT_FIELDS)}")
        _str(out["member_id"], "output.member_id")
        _int(out["frame_index"], "output.frame_index")
        if out["frame_index"] in seen:
            raise FilmError("duplicate output frame_index")
        seen.add(out["frame_index"])
        _str(out["file"], "output.file")
        _sha(out["file_sha256"], "output.file_sha256")
        _sha(out["pixel_sha256"], "output.pixel_sha256")
    _sha(document["receipt_nonce"], "result.receipt_nonce")
    _sha(document["grant_digest"], "result.grant_digest")
    if document["state"] not in RESULT_STATES:
        raise FilmError("result.state must be COMPLETE or FAILED")
    if document["error"] is not None \
            and type(document["error"]) is not str:
        raise FilmError("result.error must be a string or null")
    _str(document["produced_at"], "result.produced_at")
    if type(document["environment"]) is not dict:
        raise FilmError("result.environment must record the run env")
    return document


def _str(value, what):
    if type(value) is not str or not value:
        raise FilmError(f"{what} must be a non-empty string")
    return value


def contract_for_plan(plan):
    """The FrameStream contract the plan's encoding block pins."""
    enc = plan["encoding"]
    return make_contract(enc["width"], enc["height"],
                         fps=enc["fps"],
                         frame_range=[0, enc["output_frames"]])


def split_ranges(output_range, contract, caps, input_bytes=0,
                 input_count=0, frame_sizes=None):
    """Chunk an output range so each packet fits the declared caps.

    `frame_sizes` maps frame index -> actual encoded PNG bytes; when a
    size is known the byte budget uses it — the raw frame buffer size
    only stands in for members whose encoded size was never measured.
    Refuses — never silently truncates — when even one frame cannot fit
    (PACKET_LIMIT), and refuses an unknown limit set entirely
    (CAPACITY_UNKNOWN): an unverified limit is never shown as fitting.
    """
    if not caps:
        raise FilmError("CAPACITY_UNKNOWN: the service's file limits were "
                        "never probed or declared; refusing to claim the "
                        "packet fits")
    missing = [field for field in REQUIRED_CAPS if caps.get(field) is None]
    if missing:
        raise FilmError(f"CAPACITY_UNKNOWN: service limits {missing} are "
                        "not declared; the packet cannot promise to fit")
    frame_bytes = contract_buffer_bytes(contract)
    # Each packet dir carries packet.json, the worker script, the staged
    # inputs and the produced output members.
    limit = min(caps["max_frames_per_packet"],
                caps["max_file_count"] - 2 - input_count)
    if limit < 1:
        raise FilmError("PACKET_LIMIT: a single frame does not fit the "
                        "service's declared file limits; the range "
                        "cannot be split further — refusing")
    if input_bytes > caps["max_input_bytes"]:
        raise FilmError("PACKET_LIMIT: declared inputs alone exceed "
                        "max_input_bytes; refusing")
    budget = caps["max_output_bytes"]
    chunks = []
    start, end = output_range
    cursor = start
    while cursor < end:
        stop = cursor
        used = 0
        while stop < end and stop - cursor < limit:
            size = frame_sizes.get(stop, frame_bytes) \
                if frame_sizes else frame_bytes
            if used + size > budget:
                break
            used += size
            stop += 1
        if stop == cursor:
            raise FilmError("PACKET_LIMIT: a single frame does not fit "
                            "the service's declared file limits; the "
                            "range cannot be split further — refusing")
        chunks.append([cursor, stop])
        cursor = stop
    return chunks


def _packet_id(job_key, frame_range):
    return "pkt-" + hashlib.sha256(canon_bytes(
        {"job_key": job_key, "frame_range": frame_range})).hexdigest()[:16]


def _packet_doc(job, plan, operation, entitlement, worker, contract,
                frame_range, packet_index, packet_count, input_entries,
                input_bytes, caps, issued_at, frame_sizes=None):
    start, end = frame_range
    members = [{"frame_index": index,
                "sha256": expected_frame_digest(
                    index, contract, job["snapshot_digest"],
                    operation["recipe_digest"])}
               for index in range(start, end)]
    packet = {
        "document_type": PACKET_TYPE, "schema_version": 1,
        "packet_id": _packet_id(job["job_key"], frame_range),
        "service": entitlement["service"],
        "route": {"transfer_route": "MANUAL_PACKET",
                  "route_label": route_label(entitlement, worker),
                  "automation": "NONE"},
        "worker": {"worker_id": worker.worker_id,
                   "script": SCRIPT_NAME,
                   "version": worker_script_version(),
                   "sha256": worker_script_sha256()},
        "execution_unit": {
            "job_key": job["job_key"],
            "attempt_id": job["attempt_id"],
            "request_id": job["request_id"],
            "packet_index": packet_index,
            "packet_count": packet_count,
            "output_range": list(job["output_range"]),
            "frame_range": list(frame_range)},
        "plan": {"plan_sha": plan_sha(plan),
                 "plan_revision": plan["plan_revision"],
                 "snapshot_digest": plan["snapshot_digest"],
                 "operation_id": operation["operation_id"],
                 "kind": operation["kind"],
                 "recipe_digest": operation["recipe_digest"],
                 "runtime_contract": operation["runtime_contract"]},
        "frame_contract": dict(contract),
        "inputs": {"files": input_entries,
                   "permissions": {"read": ["packet.json", SCRIPT_NAME]
                                   + [e["path"] for e in input_entries],
                                   "write": ["outputs/",
                                             "result_manifest.json",
                                             "receipt.json"],
                                   "network": "NONE", "gpu": "NONE"}},
        "expected_output": {"dir": "outputs",
                            "members": members,
                            "member_count": len(members),
                            "bytes": sum(
                                (frame_sizes or {}).get(
                                    index,
                                    contract_buffer_bytes(contract))
                                for index in range(start, end))},
        "verification": {
            "independent": "the coordinator re-derives every member "
                           "digest from the pinned snapshot/recipe; a "
                           "worker COMPLETE is never trusted",
            "conditions": [
                "each PNG member parses and matches the frame contract",
                "file sha256 and decoded pixel sha256 match the manifest",
                "receipt binds job/attempt/request/snapshot/nonce digest",
                "coverage of the job's output_range completes via every "
                "packet's verified members"]},
        "receipt": {"format": "worker_protocol_1",
                    "actor": worker.worker_id,
                    "nonce_digest": job["nonce"],
                    "grant_digest": job["grant_digest"],
                    "evidence": {"class": worker.evidence_class,
                                 "qualification_state": "UNQUALIFIED",
                                 "service": entitlement["service"],
                                 "route_label":
                                 route_label(entitlement, worker)}},
        "entitlement": {"digest": entitlement_sha(entitlement),
                        "account_binding": entitlement["account_binding"],
                        "credential_epoch": entitlement["credential_epoch"],
                        "quote_digest": quote_digest(entitlement),
                        "service": entitlement["service"]},
        "limits": dict(caps, network="NONE", gpu="NONE"),
        "issued_at": issued_at}
    return validate_subscription_packet(packet)


def export_manual_packets(coordinator, plan, key, entitlement_path, worker,
                          out_dir, *, contract=None, input_files=(),
                          caps=None, cost=None,
                          additional_charges_approved=False,
                          issued_at=None):
    """Drive a MANUAL_PACKET job to packet export.

    RESERVED -> WAITING_USER -> SUBMITTING (the user's explicit attach
    mints the attempt/request identity the packet binds), then the
    packet dir(s) are written — split by the declared caps when the
    range exceeds them. Charge gates run before any file lands:
    inclusion UNKNOWN is never shown as free, paid units are refused
    while additional charges are not allowed, and exhaustion stops
    without a substitute route.
    """
    from .execution_workers.subscription import (assert_same_binding,
                                                 validate_entitlement)
    job = coordinator.jobs.get(key)
    if job is None:
        raise FilmError(f"Unknown job key: {key}")
    if worker.worker_id != job["worker_id"]:
        raise FilmError("SUBSTITUTE_WORKER_FENCED: the plan binds this "
                        "range to a different worker")
    operation = job["operation"]
    if operation["route"] != "SUBSCRIPTION_CODE_RUNTIME" \
            or plan["transfer_route"] != "MANUAL_PACKET":
        raise FilmError("packet export is the MANUAL_PACKET path for a "
                        "SUBSCRIPTION_CODE_RUNTIME operation")
    entitlement_path = Path(entitlement_path)
    entitlement = validate_entitlement(read_canon(entitlement_path))
    assert_same_binding(entitlement, worker)
    now_ms = coordinator.now_ms()
    if entitlement_expired(entitlement, now_ms=now_ms):
        raise FilmError("STALE: the subscription entitlement expired — "
                        "re-register or renew before issuing packets "
                        "against it")
    contract = contract or contract_for_plan(plan)
    caps = caps if caps is not None else dict(entitlement["service_caps"])
    if operation["runtime_contract"] not in (caps.get("runtimes") or ()):
        raise FilmError(
            "CAPABILITY_REFUSED: the runtime contract "
            f"{operation['runtime_contract']} was never probed on this "
            "service — a CPU-only runtime cannot take a GPU plan")
    if job["state"] == "RESERVED":
        coordinator.waiting_user(key)
    if job["state"] == "WAITING_USER":
        coordinator.attach_resume(key, frame_contract=dict(contract))
    if job["state"] != "SUBMITTING":
        raise FilmError(f"packet export needs a SUBMITTING manual job, "
                        f"got {job['state']}")
    if job.get("packet_attempt") == job["attempt_id"]:
        raise FilmError("PACKET_EXISTS: a packet set was already issued "
                        "for this attempt; a new run needs the explicit "
                        "resume path")
    input_entries = []
    input_bytes = 0
    staged = []
    for source in input_files:
        source = Path(source)
        if not source.is_file():
            raise FilmError(f"Missing packet input: {source}")
        name = f"inputs/{source.name}"
        entry = {"path": name, "sha256": digest(source),
                 "bytes": source.stat().st_size}
        input_entries.append(entry)
        input_bytes += entry["bytes"]
        staged.append((source, name))
    # The frame recipe is deterministic, so the encoded PNG size of
    # every output member is already known — budget the split by those
    # bytes, not the raw frame buffer.
    frame_sizes = {}
    for index in range(*job["output_range"]):
        frame_sizes[index] = len(subscription_worker.png_bytes(
            contract["width"], contract["height"],
            contract["pixel_format"],
            render_frame_bytes(index, contract, job["snapshot_digest"],
                               operation["recipe_digest"])))
    chunks = split_ranges(job["output_range"], contract, caps,
                          input_bytes + 4096, len(input_entries),
                          frame_sizes=frame_sizes)
    # Probed evidence, when it exists, decides what a packet may ask
    # for — declared service_caps never widen what a probe observed.
    state_dir = getattr(coordinator, "state_dir", None) \
        or entitlement_path.parent.parent
    scoped = [ev for ev in list_evidence(state_dir)
              if ev.get("scope")
              and (ev.get("environment") or {}).get("service")
              == entitlement["service"]
              and ev.get("account_binding")
              == entitlement["account_binding"]]
    if scoped:
        usable = [ev for ev in scoped
                  if evidence_registry_state(ev, now_ms=now_ms)[0]
                  != "STALE"]
        if not usable:
            raise FilmError("STALE: every probed evidence record for "
                            "this service is expired — re-probe or wait "
                            "for the user")
        requested = {"service": entitlement["service"],
                     "usage_path": entitlement["usage_path"],
                     "operation": "compose_frame_range",
                     "runtime_contract": operation["runtime_contract"],
                     "pixel_format": contract["pixel_format"],
                     "network": "NONE", "gpu": "NONE",
                     "route": "MANUAL_PACKET",
                     "frames": max(stop - start for start, stop in chunks),
                     "max_input_bytes": input_bytes + 4096}
        if not any(subscription_scope_covered(ev["scope"], requested)
                   for ev in usable):
            raise FilmError(
                "CAPABILITY_REFUSED: no live probed scope covers this "
                "packet — a CPU-only runtime never takes a GPU plan on "
                "the strength of declared service_caps")
    total_frames = job["output_range"][1] - job["output_range"][0]
    cost = cost or {"subscription_units": total_frames,
                    "handoff_minutes": len(chunks)}
    verdict = charge_check(
        entitlement, cost,
        allow_additional_charges=
        plan["execution"]["allow_additional_charges"],
        additional_charges_approved=additional_charges_approved)
    if verdict["verdict"] not in {"COVERED", "EXTRA_CHARGE_APPROVED"}:
        raise FilmError(f"{verdict['verdict']}: {verdict['reason']}")
    script = Path(__file__).resolve().parent / "subscription_worker.py"
    audit_worker_source(script.read_bytes())
    out_dir = Path(out_dir)
    dirs = []
    for index, frame_range in enumerate(chunks):
        packet = _packet_doc(job, plan, operation, entitlement, worker,
                             contract, frame_range, index, len(chunks),
                             input_entries, input_bytes, caps,
                             issued_at or now(), frame_sizes=frame_sizes)
        packet_dir = out_dir / packet["packet_id"]
        packet_dir.mkdir(parents=True, exist_ok=False)
        (packet_dir / "inputs").mkdir(exist_ok=True)
        write_canon(packet_dir / "packet.json", packet)
        shutil.copyfile(script, packet_dir / SCRIPT_NAME)
        for source, name in staged:
            target = packet_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        dirs.append(str(packet_dir))
    consume(entitlement, cost)
    write_canon(entitlement_path, entitlement)
    job["packet_attempt"] = job["attempt_id"]
    job["packet_ids"] = [_packet_id(key, rng) for rng in chunks]
    coordinator._persist()
    return {"job_key": key, "attempt_id": job["attempt_id"],
            "request_id": job["request_id"], "route_label":
            route_label(entitlement, worker), "packets": dirs,
            "packet_count": len(dirs), "charge": verdict,
            "state": job["state"]}


def import_result(coordinator, worker, packet_dir, result_dir, *,
                  entitlement=None):
    """Verify a returned packet result and feed its receipt to the
    coordinator. Every check is independent of the worker's claims:
    manifest binding, script hash, PNG/member hashes, pixel digests and
    the receipt's job/attempt/snapshot/nonce binding. Stale, foreign or
    partial claims are refused."""
    packet_dir = Path(packet_dir)
    result_dir = Path(result_dir)
    packet = validate_subscription_packet(
        read_canon(packet_dir / "packet.json"))
    key = packet["execution_unit"]["job_key"]
    job = coordinator.jobs.get(key)
    if job is None:
        raise FilmError("FOREIGN_RESULT: no coordinator job for this "
                        "packet")
    if job["worker_id"] != worker.worker_id \
            or packet["receipt"]["actor"] != worker.worker_id:
        raise FilmError("FOREIGN_RESULT: the result is bound to a "
                        "different worker")
    manifest = validate_subscription_result(
        read_canon(result_dir / "result_manifest.json"))
    for field in ("job_key", "attempt_id", "request_id"):
        if manifest[field] != packet["execution_unit"][field]:
            raise FilmError(f"FOREIGN_RESULT: manifest {field} does not "
                            "match the packet")
    if manifest["packet_id"] != packet["packet_id"] \
            or manifest["snapshot_digest"] \
            != packet["plan"]["snapshot_digest"] \
            or manifest["plan_revision"] != packet["plan"]["plan_revision"]:
        raise FilmError("FOREIGN_RESULT: manifest does not match the "
                        "packet's plan binding")
    if manifest["actor"] != packet["receipt"]["actor"]:
        raise FilmError("FOREIGN_RESULT: manifest actor mismatch")
    if manifest["receipt_nonce"] != packet["receipt"]["nonce_digest"] \
            or manifest["grant_digest"] != packet["receipt"]["grant_digest"]:
        raise FilmError("FOREIGN_RESULT: manifest is not bound to this "
                        "attempt's nonce/grant digest")
    if packet["worker"]["sha256"] != worker_script_sha256():
        raise FilmError("FOREIGN_RESULT: the packet pins a worker script "
                        "that is not the app-shipped script")
    if manifest["worker_script_sha256"] != worker_script_sha256():
        raise FilmError("FOREIGN_RESULT: a different worker script "
                        "produced this result")
    if manifest["state"] != "COMPLETE":
        raise FilmError(f"result state is {manifest['state']}: "
                        f"{manifest.get('error') or 'no detail'}")
    if manifest["frame_range"] != packet["execution_unit"]["frame_range"]:
        raise FilmError("FOREIGN_RESULT: result frame_range does not "
                        "match the packet")
    if entitlement is not None:
        if entitlement_expired(entitlement,
                               now_ms=coordinator.now_ms()):
            raise FilmError("STALE: the subscription entitlement expired "
                            "— re-register or renew; the result is not "
                            "accepted against a voided grant window")
        if manifest["session_epoch"] != entitlement["credential_epoch"]:
            raise FilmError("SESSION_EXPIRED: the run happened under a "
                            "voided session; re-attach explicitly — the "
                            "job is never resubmitted under a new "
                            "identity")
        if manifest["session_epoch"] \
                != packet["entitlement"]["credential_epoch"]:
            raise FilmError("SESSION_EXPIRED: packet and entitlement "
                            "epochs disagree")
        if quote_digest(entitlement) \
                != packet["entitlement"]["quote_digest"]:
            raise FilmError("QUOTE_CHANGED: the service terms changed "
                            "after this packet was issued; export a new "
                            "packet")
    contract = packet["frame_contract"]
    declared = {o["frame_index"]: o for o in manifest["outputs"]}
    if len(declared) != len(manifest["outputs"]):
        raise FilmError("duplicate output frame_index in manifest")
    lo, hi = manifest["frame_range"]
    if sorted(declared) != list(range(lo, hi)):
        raise FilmError("PARTIAL_RESULT: outputs do not cover the "
                        "manifest's declared range")
    for index in range(lo, hi):
        entry = declared[index]
        target = safe_path(result_dir, entry["file"])
        if not target.is_file():
            raise FilmError(f"PARTIAL_RESULT: missing output "
                            f"{entry['file']}")
        if digest(target) != entry["file_sha256"]:
            raise FilmError(f"INTEGRITY_FAILED: {entry['file']} bytes do "
                            "not match the manifest")
        header = png_header(target.read_bytes(), entry["file"])
        if header["width"] != contract["width"] \
                or header["height"] != contract["height"]:
            raise FilmError("INTEGRITY_FAILED: output dimensions do not "
                            "match the frame contract")
        from PIL import Image
        mode = {"RGBA8": "RGBA", "RGB8": "RGB", "GRAY8": "L"}[
            contract["pixel_format"]]
        with Image.open(target) as im:
            pixels = im.convert(mode)
        if sha256_bytes(pixels.tobytes()) != entry["pixel_sha256"]:
            raise FilmError("INTEGRITY_FAILED: decoded pixels do not "
                            "match the manifest")
    receipt = check_receipt(read_canon(result_dir / "receipt.json"))
    members = receipt["members"]
    member_indices = [m["frame_index"] for m in members]
    if member_indices != list(range(*receipt["covered_range"])):
        raise FilmError("PARTIAL_RESULT: receipt members must cover the "
                        "covered range contiguously")
    if member_indices != list(range(lo, hi)):
        raise FilmError("PARTIAL_RESULT: receipt coverage does not match "
                        "the manifest")
    for index in range(lo, hi):
        if declared[index]["pixel_sha256"] \
                != members[index - lo]["sha256"]:
            raise FilmError("INTEGRITY_FAILED: receipt member digest "
                            "does not match the verified output")
    coordinator.receive_receipt(receipt, worker)
    job = coordinator.jobs[key]
    return {"job_key": key, "state": job["state"],
            "coverage": coordinator.coverage(key),
            "receipt_id": receipt["receipt_id"],
            "imported_frames": len(members)}


def fixture_packet_dir(path, *, frames=2, width=8, height=6):
    """A tiny self-contained packet for probing the shipped worker script.

    The job identity is synthetic — this packet is a probe fixture, not a
    coordinator job; the fake runtime executes it and the probe records
    whether the produced digests matched.
    """
    from .frame_stream import make_contract as _make
    digest_zero = hashlib.sha256(b"anim-016-fixture").hexdigest()
    contract = _make(width, height, frame_range=[0, frames])
    recipe = hashlib.sha256(b"fixture-recipe").hexdigest()
    members = [{"frame_index": index,
                "sha256": expected_frame_digest(index, contract,
                                                digest_zero, recipe)}
               for index in range(frames)]
    packet = {
        "document_type": PACKET_TYPE, "schema_version": 1,
        "packet_id": "pkt-fixture",
        "service": "probe-fixture",
        "route": {"transfer_route": "MANUAL_PACKET",
                  "route_label": "MANUAL", "automation": "NONE"},
        "worker": {"worker_id": "subscription:probe",
                   "script": SCRIPT_NAME,
                   "version": worker_script_version(),
                   "sha256": worker_script_sha256()},
        "execution_unit": {
            "job_key": digest_zero, "attempt_id": 1,
            "request_id": "req-fixture", "packet_index": 0,
            "packet_count": 1, "output_range": [0, frames],
            "frame_range": [0, frames]},
        "plan": {"plan_sha": digest_zero, "plan_revision": 1,
                 "snapshot_digest": digest_zero,
                 "operation_id": "op-fixture", "kind": "RENDER_RANGE",
                 "recipe_digest": recipe,
                 "runtime_contract": "python-deterministic-v1"},
        "frame_contract": dict(contract),
        "inputs": {"files": [],
                   "permissions": {"read": ["packet.json", SCRIPT_NAME],
                                   "write": ["outputs/",
                                             "result_manifest.json",
                                             "receipt.json"],
                                   "network": "NONE", "gpu": "NONE"}},
        "expected_output": {"dir": "outputs", "members": members,
                            "member_count": len(members),
                            "bytes": frames
                            * contract_buffer_bytes(contract)},
        "verification": {"independent": "probe fixture — coordinator "
                                        "re-derivation compared",
                         "conditions": ["digests match "
                                        "render_frame_bytes"]},
        "receipt": {"format": "worker_protocol_1",
                    "actor": "subscription:probe",
                    "nonce_digest": hashlib.sha256(b"fixture-nonce")
                    .hexdigest(),
                    "grant_digest": hashlib.sha256(b"fixture-grant")
                    .hexdigest(),
                    "evidence": {"class": "FAKE_SUBSCRIPTION",
                                 "qualification_state": "UNQUALIFIED"}},
        "entitlement": {"digest": digest_zero,
                        "account_binding": digest_zero,
                        "credential_epoch": 1,
                        "quote_digest": digest_zero,
                        "service": "probe-fixture"},
        "limits": {"network": "NONE", "gpu": "NONE",
                   "max_input_bytes": 1 << 20,
                   "max_output_bytes": 1 << 20,
                   "max_file_count": 64,
                   "max_frames_per_packet": 8,
                   "max_scratch_bytes": 1 << 20,
                   "drive_read": "NONE", "drive_write": "NONE",
                   "runtimes": ["python-deterministic-v1"]},
        "issued_at": "fixture"}
    validate_subscription_packet(packet)
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    write_canon(path / "packet.json", packet)
    shutil.copyfile(Path(__file__).resolve().parent /
                    "subscription_worker.py", path / SCRIPT_NAME)
    return path
