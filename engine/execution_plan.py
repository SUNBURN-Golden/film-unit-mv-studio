"""FRAME_ANIMATION_V1 ExecutionPlan 1 (ANIM-014, schema §11, exec/storage §5).

`execution/plan.json` is the fixed contract a coordinator executes against:
snapshot digest, operation DAG (output ranges plus halo, worker/route per
operation, recipe/runtime binding), storage profile, encoding contract,
workspace caps, transfer route and edges, resource reservations and the
charge/quality gates.

Two hard rules the schema fixes:

- nothing executes without a plan — `assert_executable` is the gate every
  job submission goes through, and `DIRECT_DRIVE` is never an allowed
  route or transfer_route;
- `capability_evidence_required` blocks `AUTO_PERFORMANCE` candidates that
  have no current-scope evidence, and `allow_additional_charges` never
  authorizes an extra-charge path on its own.

`transport_retry_policy` on an edge is the only place retries exist:
absent means zero retries; present requires every cap field (schema §11 /
evolution §4.2.1) — a partial policy is rejected, not read as unbounded.
"""
import hashlib

from .animation_schema import canon_bytes, check_document, write_canon
from .archive_manifest import LEVELS, RETRY_FIELDS
from .core import FilmError
from .frame_stream import check_rational

PLAN_TYPE = "execution_plan"
PLAN_PATH = "execution/plan.json"

PLAN_FIELDS = {"document_type", "schema_version", "plan_revision",
               "snapshot_digest", "storage", "execution", "encoding",
               "workspace", "operations", "transfer_route",
               "coordinator_location", "transfer_edges",
               "resource_reservations", "quality"}
STORAGE_FIELDS = {"profile", "archive_manifest"}
EXECUTION_FIELDS = {"policy", "allowed_routes",
                    "capability_evidence_required",
                    "allow_additional_charges"}
ENCODING_FIELDS = {"driver_policy", "allowed_drivers", "delivery_profile",
                   "width", "height", "fps", "output_frames"}
WORKSPACE_FIELDS = {"pc_cache_limit_bytes", "worker_scratch_limit_bytes",
                    "on_limit"}
OPERATION_FIELDS = {"operation_id", "kind", "output_range", "halo_ranges",
                    "route", "worker", "recipe_digest", "runtime_contract",
                    "depends_on"}
EDGE_FIELDS = {"edge_id", "from_role", "to_role", "snapshot_digest",
               "object_digest", "index_digest", "member_ids", "byte_ranges",
               "verification", "max_inflight_bytes", "spool_limit_bytes",
               "evidence_ref", "measured_at", "credential_boundary",
               "expiry_binding", "transport_retry_policy", "dependencies",
               "completion_receipt_ref"}
BOUNDARY_FIELDS = {"issuer", "audience", "peer", "connection_digest",
                   "credential_epoch", "job_key", "attempt_id",
                   "object_digest", "member_ids", "ranges", "max_bytes"}
EXPIRY_FIELDS = {"expires_at_ms", "renewal_scope"}
RESERVATION_FIELDS = {"location", "peak_bytes"}
QUALITY_FIELDS = {"frame_contract"}

# Execution routes a worker may live on. DIRECT_DRIVE is a data route, not
# an execution route — and it is excluded from every candidate set until a
# separate ADR and a real user authorization exist.
EXECUTION_ROUTES = {"LOCAL_NATIVE", "REMOTE_CPU", "REMOTE_GPU",
                    "SUBSCRIPTION_CODE_RUNTIME"}
FORBIDDEN_ROUTES = {"DIRECT_DRIVE"}
TRANSFER_ROUTES = {"COORDINATOR_RELAY", "MANUAL_PACKET"}
COORDINATOR_LOCATIONS = {"USER_DESKTOP", "PROTECTED_REMOTE_RELAY"}
EDGE_ROLES = {"ARCHIVE", "COORDINATOR", "WORKER"}
EXECUTION_POLICIES = {"AUTO_PERFORMANCE", "FIXED_ROUTE"}
DRIVERS = {"FFMPEG", "NVIDIA_NATIVE", "VIDEOTOOLBOX_NATIVE", "GSTREAMER",
           "QUALIFIED_SERVICE"}
STORAGE_PROFILES = {"LOCAL_FULL", "DRIVE_BOUNDED"}
ON_LIMIT = {"PAUSE"}
# Grant renewal restores access to the same objects of the same job only;
# it is never a new compute submit.
RENEWAL_SCOPES = {"SAME_JOB_SAME_OBJECT"}


def _int(value, what, minimum=0):
    if type(value) is not int or value < minimum:
        raise FilmError(f"{what} must be an integer >= {minimum}")
    return value


def _sha(value, what, nullable=False):
    if value is None and nullable:
        return value
    if type(value) is not str or len(value) != 64 \
            or any(c not in "0123456789abcdef" for c in value):
        raise FilmError(f"{what} must be a lowercase SHA-256")
    return value


def _str(value, what):
    if type(value) is not str or not value:
        raise FilmError(f"{what} must be a non-empty string")
    return value


def _range(value, what):
    if type(value) is not list or len(value) != 2 \
            or any(type(v) is not int or v < 0 for v in value) \
            or value[1] <= value[0]:
        raise FilmError(f"{what} must be a non-empty [start, end) range")
    return value


def check_retry_policy(policy, what="transport_retry_policy"):
    """Edge retry policy: null means zero retries; present needs every cap."""
    if policy is None:
        return None
    if type(policy) is not dict or set(policy.keys()) != RETRY_FIELDS:
        raise FilmError(f"{what} must hold exactly max_retries, "
                        "backoff_base_ms, max_backoff_ms, max_elapsed_ms, "
                        "max_requests and max_transferred_bytes, or be "
                        "null (no retry)")
    if _int(policy.get("max_retries"), f"{what}.max_retries") > 3:
        raise FilmError(f"{what}.max_retries exceeds the bounded cap 3")
    for field in ("backoff_base_ms", "max_backoff_ms"):
        _int(policy.get(field), f"{what}.{field}")
    for field in ("max_elapsed_ms", "max_requests",
                  "max_transferred_bytes"):
        _int(policy.get(field), f"{what}.{field}", 1)
    return policy


def _check_credential_boundary(boundary, what="credential_boundary"):
    """Secret-free grant metadata only — no token field exists here."""
    if type(boundary) is not dict or set(boundary.keys()) - BOUNDARY_FIELDS:
        raise FilmError(f"{what} holds only the secret-free binding fields")
    for field in ("issuer", "audience", "peer"):
        _str(boundary.get(field), f"{what}.{field}")
    _sha(boundary.get("connection_digest"), f"{what}.connection_digest")
    _int(boundary.get("credential_epoch"), f"{what}.credential_epoch", 1)
    _sha(boundary.get("job_key"), f"{what}.job_key", nullable=True)
    if boundary.get("attempt_id") is not None:
        _int(boundary["attempt_id"], f"{what}.attempt_id")
    _sha(boundary.get("object_digest"), f"{what}.object_digest",
         nullable=True)
    if type(boundary.get("member_ids")) is not list \
            or any(type(m) is not str for m in boundary["member_ids"]):
        raise FilmError(f"{what}.member_ids must be a list of strings")
    if type(boundary.get("ranges")) is not list:
        raise FilmError(f"{what}.ranges must be a list of [start, end) "
                        "pairs")
    for position, rng in enumerate(boundary["ranges"]):
        _range(rng, f"{what}.ranges[{position}]")
    _int(boundary.get("max_bytes"), f"{what}.max_bytes", 1)
    return boundary


def _check_edge(edge, position, snapshot_digest):
    if type(edge) is not dict or set(edge.keys()) != EDGE_FIELDS:
        raise FilmError(f"transfer_edges[{position}] must hold exactly the "
                        "contracted edge fields")
    _str(edge.get("edge_id"), "edge_id")
    for field in ("from_role", "to_role"):
        if edge.get(field) not in EDGE_ROLES:
            raise FilmError(f"edge {edge.get('edge_id')} {field} must be "
                            "ARCHIVE, COORDINATOR or WORKER")
    if edge["from_role"] == edge["to_role"]:
        raise FilmError("A transfer edge needs two distinct roles")
    if edge.get("snapshot_digest") != snapshot_digest:
        raise FilmError("Edge snapshot_digest must equal the plan snapshot; "
                        "edges never bind a different input revision")
    _sha(edge.get("object_digest"), "edge.object_digest", nullable=True)
    _sha(edge.get("index_digest"), "edge.index_digest", nullable=True)
    if type(edge.get("member_ids")) is not list \
            or any(type(m) is not str or not m for m in edge["member_ids"]):
        raise FilmError("edge.member_ids must be a list of strings")
    if type(edge.get("byte_ranges")) is not list:
        raise FilmError("edge.byte_ranges must be a list")
    for index, rng in enumerate(edge["byte_ranges"]):
        _range(rng, f"edge.byte_ranges[{index}]")
    if edge.get("verification") not in LEVELS:
        raise FilmError("edge.verification must be an archive verification "
                        "level")
    _int(edge.get("max_inflight_bytes"), "edge.max_inflight_bytes", 1)
    _int(edge.get("spool_limit_bytes"), "edge.spool_limit_bytes", 1)
    if edge.get("evidence_ref") is not None:
        _str(edge["evidence_ref"], "edge.evidence_ref")
    # Without evidence the edge's measured values are UNKNOWN by contract.
    if edge.get("measured_at") is not None:
        _str(edge["measured_at"], "edge.measured_at")
    _check_credential_boundary(edge.get("credential_boundary"),
                               f"edge {edge['edge_id']} credential_boundary")
    expiry = edge.get("expiry_binding")
    if type(expiry) is not dict or set(expiry.keys()) - EXPIRY_FIELDS:
        raise FilmError("edge.expiry_binding holds only expires_at_ms and "
                        "renewal_scope")
    _int(expiry.get("expires_at_ms"), "expiry_binding.expires_at_ms", 1)
    if expiry.get("renewal_scope") not in RENEWAL_SCOPES:
        raise FilmError("expiry_binding.renewal_scope must be "
                        "SAME_JOB_SAME_OBJECT")
    check_retry_policy(edge.get("transport_retry_policy"),
                       f"edge {edge['edge_id']} transport_retry_policy")
    if type(edge.get("dependencies")) is not list \
            or any(type(d) is not str for d in edge["dependencies"]):
        raise FilmError("edge.dependencies must be a list of edge ids")
    if edge.get("completion_receipt_ref") is not None:
        _str(edge["completion_receipt_ref"], "edge.completion_receipt_ref")
    return edge


def _check_operation(operation, position, allowed_routes, output_frames):
    if type(operation) is not dict \
            or set(operation.keys()) != OPERATION_FIELDS:
        raise FilmError(f"operations[{position}] must hold exactly the "
                        "contracted operation fields")
    _str(operation.get("operation_id"), "operation_id")
    _str(operation.get("kind"), "operation.kind")
    rng = _range(operation.get("output_range"), "operation.output_range")
    if rng[1] > output_frames:
        raise FilmError("operation.output_range exceeds encoding."
                        "output_frames")
    if type(operation.get("halo_ranges")) is not list:
        raise FilmError("operation.halo_ranges must be a list")
    for index, halo in enumerate(operation["halo_ranges"]):
        _range(halo, f"operation.halo_ranges[{index}]")
    if operation.get("route") in FORBIDDEN_ROUTES:
        raise FilmError("DIRECT_DRIVE is never an allowed execution route")
    if operation.get("route") not in allowed_routes:
        raise FilmError(f"operation route {operation.get('route')} is not "
                        "in execution.allowed_routes")
    _str(operation.get("worker"), "operation.worker")
    _sha(operation.get("recipe_digest"), "operation.recipe_digest")
    _str(operation.get("runtime_contract"), "operation.runtime_contract")
    if type(operation.get("depends_on")) is not list \
            or any(type(d) is not str for d in operation["depends_on"]):
        raise FilmError("operation.depends_on must be a list of operation "
                        "ids")
    return operation


def validate_execution_plan(document):
    """Structural contract of `execution/plan.json` (schema §11)."""
    check_document(document, PLAN_TYPE)
    if set(document.keys()) - PLAN_FIELDS:
        raise FilmError("Unknown execution_plan fields")
    _int(document.get("plan_revision"), "plan_revision", 1)
    snapshot_digest = _sha(document.get("snapshot_digest"),
                           "snapshot_digest")
    storage = document.get("storage")
    if type(storage) is not dict or set(storage.keys()) - STORAGE_FIELDS:
        raise FilmError("plan.storage fields are profile and "
                        "archive_manifest")
    if storage.get("profile") not in STORAGE_PROFILES:
        raise FilmError("storage.profile must be LOCAL_FULL or "
                        "DRIVE_BOUNDED")
    if storage.get("archive_manifest") is not None:
        _str(storage["archive_manifest"], "storage.archive_manifest")
    execution = document.get("execution")
    if type(execution) is not dict \
            or set(execution.keys()) != EXECUTION_FIELDS:
        raise FilmError("plan.execution must hold policy, allowed_routes, "
                        "capability_evidence_required and "
                        "allow_additional_charges")
    if execution.get("policy") not in EXECUTION_POLICIES:
        raise FilmError("execution.policy must be AUTO_PERFORMANCE or an "
                        "explicitly fixed route policy")
    allowed = execution.get("allowed_routes")
    if type(allowed) is not list or not allowed \
            or any(r not in EXECUTION_ROUTES for r in allowed):
        raise FilmError("execution.allowed_routes must be a non-empty "
                        "subset of the known execution routes; "
                        "DIRECT_DRIVE is never a candidate")
    if type(execution.get("capability_evidence_required")) is not bool:
        raise FilmError("capability_evidence_required must be a boolean")
    if type(execution.get("allow_additional_charges")) is not bool:
        raise FilmError("allow_additional_charges must be a boolean")
    encoding = document.get("encoding")
    if type(encoding) is not dict or set(encoding.keys()) != ENCODING_FIELDS:
        raise FilmError("plan.encoding fields are fixed")
    _str(encoding.get("driver_policy"), "encoding.driver_policy")
    drivers = encoding.get("allowed_drivers")
    if type(drivers) is not list or not drivers \
            or any(d not in DRIVERS for d in drivers):
        raise FilmError("encoding.allowed_drivers must be a non-empty "
                        "subset of the known drivers")
    _str(encoding.get("delivery_profile"), "encoding.delivery_profile")
    width = _int(encoding.get("width"), "encoding.width", 1)
    _int(encoding.get("height"), "encoding.height", 1)
    check_rational(encoding.get("fps"), "encoding.fps")
    output_frames = _int(encoding.get("output_frames"),
                         "encoding.output_frames", 1)
    workspace = document.get("workspace")
    if type(workspace) is not dict or set(workspace.keys()) != WORKSPACE_FIELDS:
        raise FilmError("plan.workspace fields are fixed")
    _int(workspace.get("pc_cache_limit_bytes"), "workspace.pc_cache_limit_bytes")
    _int(workspace.get("worker_scratch_limit_bytes"),
         "workspace.worker_scratch_limit_bytes")
    if workspace.get("on_limit") not in ON_LIMIT:
        raise FilmError("workspace.on_limit default and only contract is "
                        "PAUSE")
    operations = document.get("operations")
    if type(operations) is not list or not operations:
        raise FilmError("operations must be a non-empty DAG")
    operation_ids = set()
    covered = []
    for position, operation in enumerate(operations):
        _check_operation(operation, position, allowed, output_frames)
        if operation["operation_id"] in operation_ids:
            raise FilmError(f"Duplicate operation_id: "
                            f"{operation['operation_id']}")
        operation_ids.add(operation["operation_id"])
        covered.append(operation["output_range"])
    for position, operation in enumerate(operations):
        for dep in operation["depends_on"]:
            if dep not in operation_ids:
                raise FilmError(f"operation {operation['operation_id']} "
                                f"depends on unknown {dep}")
    # The operation DAG must be acyclic: reject any dependency cycle.
    for operation in operations:
        seen, stack = set(), [operation["operation_id"]]
        while stack:
            node = stack.pop()
            for dep in next(o for o in operations
                            if o["operation_id"] == node)["depends_on"]:
                if dep == operation["operation_id"]:
                    raise FilmError("operation DAG has a dependency cycle")
                if dep not in seen:
                    seen.add(dep)
                    stack.append(dep)
    # Output ranges tile [0, output_frames) exactly once — no gap, no
    # overlap, no silent padding.
    bounds = sorted(tuple(r) for r in covered)
    cursor = 0
    for start, end in bounds:
        if start != cursor:
            raise FilmError("operation output ranges must cover "
                            "[0, output_frames) without gaps or overlap")
        cursor = end
    if cursor != output_frames:
        raise FilmError("operation output ranges must cover "
                        "[0, output_frames) without gaps or overlap")
    if document.get("transfer_route") not in TRANSFER_ROUTES:
        raise FilmError("transfer_route must be COORDINATOR_RELAY or "
                        "MANUAL_PACKET")
    if document.get("coordinator_location") not in COORDINATOR_LOCATIONS:
        raise FilmError("coordinator_location must be USER_DESKTOP or an "
                        "already-authorized PROTECTED_REMOTE_RELAY")
    edges = document.get("transfer_edges")
    if type(edges) is not list:
        raise FilmError("transfer_edges must be a list")
    edge_ids = set()
    for position, edge in enumerate(edges):
        _check_edge(edge, position, snapshot_digest)
        if edge["edge_id"] in edge_ids:
            raise FilmError(f"Duplicate edge_id: {edge['edge_id']}")
        edge_ids.add(edge["edge_id"])
    for edge in edges:
        for dep in edge["dependencies"]:
            if dep not in edge_ids:
                raise FilmError(f"edge {edge['edge_id']} depends on "
                                f"unknown edge {dep}")
    reservations = document.get("resource_reservations")
    if type(reservations) is not list:
        raise FilmError("resource_reservations must be a list")
    for position, reservation in enumerate(reservations):
        if type(reservation) is not dict \
                or set(reservation.keys()) != RESERVATION_FIELDS:
            raise FilmError("resource_reservations entries hold location "
                            "and peak_bytes")
        _str(reservation.get("location"), "reservation.location")
        _int(reservation.get("peak_bytes"), "reservation.peak_bytes", 1)
    quality = document.get("quality")
    if type(quality) is not dict or set(quality.keys()) - QUALITY_FIELDS:
        raise FilmError("plan.quality holds only the frame contract pin")
    if quality.get("frame_contract") != "FRAME_STREAM_V1":
        raise FilmError("quality.frame_contract must be FRAME_STREAM_V1")
    return document


def make_execution_plan(snapshot_digest, operations, *, storage, execution,
                        encoding, workspace, transfer_route="COORDINATOR_RELAY",
                        coordinator_location="USER_DESKTOP",
                        transfer_edges=(), resource_reservations=(),
                        quality=None, plan_revision=1):
    """Assemble and validate an ExecutionPlan 1 document."""
    doc = {"document_type": PLAN_TYPE, "schema_version": 1,
           "plan_revision": plan_revision,
           "snapshot_digest": snapshot_digest,
           "storage": dict(storage),
           "execution": dict(execution),
           "encoding": dict(encoding),
           "workspace": dict(workspace),
           "operations": [dict(o) for o in operations],
           "transfer_route": transfer_route,
           "coordinator_location": coordinator_location,
           "transfer_edges": [dict(e) for e in transfer_edges],
           "resource_reservations": [dict(r) for r in resource_reservations],
           "quality": quality or {"frame_contract": "FRAME_STREAM_V1"}}
    return validate_execution_plan(doc)


def plan_sha(document):
    """Content identity of the fixed plan (self digest is never an input)."""
    return hashlib.sha256(canon_bytes(document)).hexdigest()


def write_plan(path, document):
    validate_execution_plan(document)
    write_canon(path, document)


def job_key(plan, operation):
    """WorkerProtocol job key: hash of snapshot, operation, output range,
    recipe and runtime/driver contract — never attempt or request ids."""
    _sha(plan.get("snapshot_digest"), "plan.snapshot_digest")
    return hashlib.sha256(canon_bytes({
        "snapshot_digest": plan["snapshot_digest"],
        "operation_id": operation["operation_id"],
        "kind": operation["kind"],
        "output_range": operation["output_range"],
        "halo_ranges": operation["halo_ranges"],
        "recipe_digest": operation["recipe_digest"],
        "runtime_contract": operation["runtime_contract"],
    })).hexdigest()


def _evidence_covers(evidence, route, worker):
    """Route/worker scope must be explicitly QUALIFIED_FOR_SCOPE."""
    if type(evidence) is not dict:
        return False
    scope = evidence.get(route) or evidence.get(worker)
    return type(scope) is dict \
        and scope.get("state") == "QUALIFIED_FOR_SCOPE"


def assert_executable(plan, *, evidence=None, additional_charges_approved=False):
    """The gate every submission passes before any side effect.

    - additional charges: `allow_additional_charges: true` still requires a
      separate user approval — the flag alone authorizes nothing;
    - `AUTO_PERFORMANCE` + `capability_evidence_required`: every operation's
      route/worker needs current-scope evidence; absent evidence is
      UNKNOWN, never assumed;
    - a remote-routed operation needs the coordinator-relay edges that move
      its input and output — the default route is never silently swapped.
    """
    execution = plan["execution"]
    if execution["allow_additional_charges"] and not additional_charges_approved:
        raise FilmError(
            "ADDITIONAL_CHARGES_NOT_APPROVED: the plan allows extra charges "
            "only with a separate explicit approval")
    if execution["policy"] == "AUTO_PERFORMANCE" \
            and execution["capability_evidence_required"]:
        missing = [o["operation_id"] for o in plan["operations"]
                   if not _evidence_covers(evidence, o["route"], o["worker"])]
        if missing:
            raise FilmError(
                "CAPABILITY_EVIDENCE_REQUIRED: AUTO_PERFORMANCE candidates "
                f"{missing} have no current-scope evidence; the candidate "
                "is excluded, not assumed")
    if plan["transfer_route"] == "COORDINATOR_RELAY":
        inbound = {e["edge_id"] for e in plan["transfer_edges"]
                   if e["to_role"] == "WORKER"}
        outbound = {e["edge_id"] for e in plan["transfer_edges"]
                    if e["from_role"] == "WORKER"}
        remote = [o["operation_id"] for o in plan["operations"]
                  if o["route"] != "LOCAL_NATIVE"]
        if remote and (not inbound or not outbound):
            raise FilmError(
                "MISSING_TRANSFER_EDGES: remote operations need coordinator "
                "relay edges for input push and output pull")
    return True
