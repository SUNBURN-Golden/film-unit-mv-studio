"""ANIM-019: secret-free capability registry — current state, staleness,
scope-bound eligibility and bound performance measurement (execution
design 3.1 / 7.2.1 / 8.1, schema 15).

One registry over every evidence kind — provider, runtime/worker,
encoder, route/relay and subscription. Entries are CapabilityEvidence 1
documents under `<state_dir>/capability/CE-*.json` (the same directory
the ANIM-016 subscription probes write); measured performance records
are `capability_measurement` 1 documents under `measurements/CM-*.json`
bound to the evidence digest and the exact scope they timed. Nothing
secret lives here — identity is a SHA-256 account-binding digest plus a
credential epoch, never an email, token or key.

Registry state is computed on every read; the stored field is a claim
this module re-checks:

* ``QUALIFIED_FOR_SCOPE`` — a real probe verified exactly this scope.
* ``DOCUMENTED_ONLY`` — the record documents a capability, or a FAKE
  test double ran a fixture; a fixture PASS never qualifies anything.
* ``STALE`` — a bound field (session, device, OS, runtime, driver,
  worker digest, account binding, credential epoch, route, transport,
  operation, caps, allowance) differs from the current observation, or
  a bound lifetime expired. An old PASS is never extended.
* ``UNAVAILABLE`` — a real probe ran and the capability was absent.

Eligibility is axis-scoped and fails closed. The axis comes from the
probed operation, never a self-declared label: ENCODE evidence never
satisfies COMPOSE (CPU is not encode), a CUDA line in an environment
never implies NVENC, an allowance unit never pays a different unit (an
LLM subscription is not GPU/API credit), and a storage connector never
answers a network need (a Drive connector is not sandbox network). The
requested scope must be covered by the probed scope — a 720p/2-frame
fixture never covers 1080p/5760.

AUTO_PERFORMANCE selection additionally requires a complete bound
measurement (CAP-MEASURE): cold/warm end-to-end time, startup/auth/queue
split, stage timeline, shared-edge timing, peak disk/RAM/VRAM, transfer
totals, cache hits, manual minutes and per-unit usage. Any missing piece
leaves the estimate UNKNOWN and yields zero auto-selections.
FIXED_ROUTE keeps the user's explicit choice, labelled as a user choice
— never as an auto-qualified pick.
"""
from pathlib import Path
import hashlib
import re
import time

from .animation_schema import (canon_bytes, read_canon,
                               validate_capability_evidence,
                               validate_capability_measurement,
                               write_canon)
from .core import FilmError, now, safe_path

REGISTRY_STATES = ("DOCUMENTED_ONLY", "QUALIFIED_FOR_SCOPE", "STALE",
                   "UNAVAILABLE")

# Hard capability axes; a probe on one axis never qualifies another.
AXES = ("ENCODE", "COMPOSE", "TRANSFER", "NETWORK", "STORAGE",
        "ENTITLEMENT")

# Which axis a probed operation can ever prove. Unlisted operations are
# UNCLASSIFIED and cover nothing — the map itself is the separation.
_AXIS_BY_OPERATION = {
    "encode_video_packets": "ENCODE",
    "encode_delivery": "ENCODE",
    "compose_frame_range": "COMPOSE",
    "render_frame_range": "COMPOSE",
    "decode_frame_range": "COMPOSE",
    "transfer_object_range": "TRANSFER",
    "relay_packet": "TRANSFER",
    "network_reachability": "NETWORK",
    "drive_connector_grant": "STORAGE",
    "storage_object_access": "STORAGE",
    "account_entitlement": "ENTITLEMENT",
}

# Requested-scope keys compared as an upper bound (requested <= probed);
# every other requested key must equal the probed value exactly.
_BOUND_KEYS = {"frames", "bytes", "max_bytes", "max_input_bytes",
               "max_output_bytes", "max_scratch_bytes", "max_file_count",
               "requests", "count", "lifetime_ms"}

# Bound document fields whose drift stales an entry: account, epoch,
# worker/adapter digest, driver, operation, route, transport, caps and
# allowance (the usage right). Inside `environment` every recorded key
# is a binding — session/device/OS/runtime/driver/worker/network/gpu —
# except the expiry timestamps, which are compared as deadlines instead.
_ENV_SKIP = {"session_expires_at_ms", "expires_at_ms"}
_DOC_BINDINGS = ("account_binding", "credential_epoch",
                 "adapter_digest", "driver", "operation", "route",
                 "transport", "caps", "allowance")

ALLOWANCE_UNITS = ("subscription_units", "compute_units", "api_credits",
                   "usd", "handoff_minutes")

_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")
_STATE_RANK = {"QUALIFIED_FOR_SCOPE": 3, "STALE": 2,
               "DOCUMENTED_ONLY": 1, "UNAVAILABLE": 0}

# CAP-MEASURE sections and the integer members each must carry before
# the measurement can produce an estimate.
_MEASURE_INTS = (
    ("cold", ("end_to_end_ms", "startup_ms", "auth_ms", "queue_ms")),
    ("warm", ("end_to_end_ms",)),
    ("shared_edge", ("bytes", "ms")),
    ("peaks", ("disk_bytes", "ram_bytes", "vram_bytes")),
    ("transfer", ("read_bytes", "write_bytes", "requests")),
    ("cache", ("hits", "misses")),
    ("manual", ("minutes",)),
    ("samples", ("count", "window_ms", "min_ms", "max_ms")),
)

# Execution route -> the evidence transport vocabulary a probe records.
_ROUTE_TRANSPORT = {"LOCAL_NATIVE": "local", "REMOTE_CPU": "relay",
                    "REMOTE_GPU": "relay",
                    "SUBSCRIPTION_CODE_RUNTIME": "MANUAL_PACKET"}


def _now_ms():
    return int(time.time() * 1000)


# --- storage ---------------------------------------------------------------

def registry_dir(state_dir):
    return Path(state_dir) / "capability"


def measurements_dir(state_dir):
    return registry_dir(state_dir) / "measurements"


def register(state_dir, evidence):
    """Validate and store a CapabilityEvidence 1 document; returns its
    canonical path. The stored record is a claim — current_state()
    re-computes it on every read."""
    validate_capability_evidence(evidence)
    evidence_id = evidence["evidence_id"]
    if not _SLUG.fullmatch(evidence_id):
        raise FilmError("evidence_id is used as a path segment — a slug "
                        "only, never free text")
    path = safe_path(registry_dir(state_dir), f"{evidence_id}.json")
    write_canon(path, evidence)
    return path


def entries(state_dir):
    """Every stored evidence document, validated."""
    folder = registry_dir(state_dir)
    if not folder.is_dir():
        return []
    return [validate_capability_evidence(read_canon(p))
            for p in sorted(folder.glob("CE-*.json"))]


def save_measurement(state_dir, measurement):
    validate_capability_measurement(measurement)
    measurement_id = measurement["measurement_id"]
    if not _SLUG.fullmatch(measurement_id):
        raise FilmError("measurement_id is used as a path segment — a "
                        "slug only, never free text")
    path = safe_path(measurements_dir(state_dir),
                     f"{measurement_id}.json")
    write_canon(path, measurement)
    return path


def measurements(state_dir):
    folder = measurements_dir(state_dir)
    if not folder.is_dir():
        return []
    return [validate_capability_measurement(read_canon(p))
            for p in sorted(folder.glob("CM-*.json"))]


def make_measurement(evidence, *, input_digest, quality_digest,
                     scope=None, cold=None, warm=None, stage_timeline=(),
                     shared_edge=None, peaks=None, transfer=None,
                     cache=None, manual=None, usage=None, samples=None,
                     observed_at=None):
    """Build a `capability_measurement` 1 bound to `evidence`.

    `scope` defaults to the evidence's own probed scope — pass the
    narrower scope actually timed when the run covered less.
    """
    document = {
        "document_type": "capability_measurement",
        "schema_version": 1,
        "measurement_id": "",
        "evidence_id": evidence["evidence_id"],
        "evidence_digest": evidence_digest(evidence),
        "input_digest": input_digest,
        "quality_digest": quality_digest,
        "scope": dict(scope) if scope is not None
        else dict(evidence.get("scope") or {}),
        "cold": dict(cold or {}),
        "warm": dict(warm or {}),
        "stage_timeline": [dict(s) for s in stage_timeline],
        "shared_edge": dict(shared_edge or {}),
        "peaks": dict(peaks or {}),
        "transfer": dict(transfer or {}),
        "cache": dict(cache or {}),
        "manual": dict(manual or {}),
        "usage": dict(usage or {}),
        "samples": dict(samples or {}),
        "observed_at": observed_at or now(),
    }
    document["measurement_id"] = "CM-" + hashlib.sha256(
        canon_bytes(document)).hexdigest()[:16].upper()
    return validate_capability_measurement(document)


def evidence_digest(evidence):
    """SHA-256 of the canonical evidence document — the exact binding a
    measurement must reproduce to count."""
    return hashlib.sha256(canon_bytes(evidence)).hexdigest()


# --- axes and scope ----------------------------------------------------------

def axis_of(evidence):
    """The capability axis this evidence can speak for — decided by the
    probed operation, never by a label the document gave itself."""
    return _AXIS_BY_OPERATION.get(evidence.get("operation"),
                                  "UNCLASSIFIED")


def _rational_equal(want, have):
    for value in (want, have):
        if type(value) is not dict \
                or type(value.get("num")) is not int \
                or type(value.get("den")) is not int \
                or value["den"] < 1:
            return False
    return want["num"] * have["den"] == have["num"] * want["den"]


def scope_covers(axis, probed, requested):
    """True only when every requested requirement was actually probed.

    Every key in `requested` must exist in `probed`: bound keys compare
    requested <= probed, rationals by cross-multiplied equality, every
    other value by exact equality. A request key the probe never
    measured fails closed — no superset inference, so a 720p/2-frame
    fixture never covers 1080p/5760. ENCODE delegates to the encoder
    scope contract (codec, pixel format, resolution, rational fps and a
    frame count no larger than tested).
    """
    if axis == "ENCODE":
        from .encoder_backends import scope_covered
        return scope_covered(probed, requested)
    if type(probed) is not dict or type(requested) is not dict \
            or not requested:
        return False
    for key, want in requested.items():
        have = probed.get(key)
        if key in _BOUND_KEYS:
            if type(want) is not int or type(have) is not int \
                    or want < 0 or want > have:
                return False
        elif type(want) is dict or type(have) is dict:
            if not _rational_equal(want, have):
                return False
        elif have != want:
            return False
    return True


# --- staleness and current state ---------------------------------------------

def is_fake(evidence):
    """Fixture/test-double records — they prove a protocol ran, never
    that the real capability exists."""
    env = evidence.get("environment") or {}
    return env.get("fake_runtime") is True \
        or str(evidence.get("qualification") or "").startswith("FAKE")


def staleness_reasons(evidence, observation=None, *, now_ms=None):
    """Machine-readable reasons a stored probe no longer applies.

    `observation` is a fresh probe-shaped dict holding the *current*
    environment/caps/allowance/account_binding/credential_epoch/
    adapter_digest/driver/operation/route/transport and optionally
    expires_at_ms. Any bound field that differs — or an expired session
    or entitlement — makes the evidence STALE; the remedy is re-probe
    or an explicit user wait, never silent reuse of an old PASS.
    """
    reasons = []
    env = evidence.get("environment") or {}
    if now_ms is None:
        now_ms = _now_ms()
    for field, code in (("session_expires_at_ms", "session_expired"),
                        ("expires_at_ms", "environment_expired")):
        bound = env.get(field)
        if bound is not None and now_ms >= bound:
            reasons.append(code)
    if observation is None:
        return reasons
    other = observation.get("environment") or {}
    for field in sorted((set(env) | set(other)) - _ENV_SKIP):
        if env.get(field) != other.get(field):
            reasons.append(f"environment.{field} changed")
    for field in _DOC_BINDINGS:
        if field in observation \
                and evidence.get(field) != observation.get(field):
            reasons.append({"caps": "limits changed"}.get(
                field, f"{field} changed"))
    expires = observation.get("expires_at_ms")
    if expires is not None and now_ms >= expires:
        reasons.append("entitlement_expired")
    return reasons


def current_state(evidence, observation=None, *, now_ms=None):
    """The entry's state computed against the current environment —
    never the stored claim alone.

    Returns {"state", "reasons", "detail"}. A fake test double that
    claims QUALIFIED_FOR_SCOPE is reported STALE when its bindings
    drifted and DOCUMENTED_ONLY otherwise — fixture PASS never reaches
    real qualification.
    """
    validate_capability_evidence(evidence)
    stored = evidence.get("registry_state")
    if stored == "UNAVAILABLE":
        return {"state": "UNAVAILABLE", "reasons": ["probe_unavailable"],
                "detail": evidence.get("reason")}
    stale = staleness_reasons(evidence, observation, now_ms=now_ms)
    if stale:
        return {"state": "STALE", "reasons": stale,
                "detail": evidence.get("reason")}
    if is_fake(evidence):
        return {"state": "DOCUMENTED_ONLY", "reasons": ["fake_evidence"],
                "detail": "fixture/test-double PASS is documentation "
                          "only — never real qualification"}
    if stored == "QUALIFIED_FOR_SCOPE":
        return {"state": "QUALIFIED_FOR_SCOPE", "reasons": [],
                "detail": evidence.get("reason")}
    return {"state": "DOCUMENTED_ONLY",
            "reasons": ["documented_only"],
            "detail": evidence.get("reason")}


# --- allowance ---------------------------------------------------------------

def allowance_missing(evidence, requires):
    """Units the evidence allowance cannot pay. Allowance units are never
    exchanged — subscription_units never covers api_credits, usd or GPU
    time — and an int requirement needs an int allowance at least as
    large (UNLIMITED covers; 0/UNKNOWN/NOT_APPLICABLE never do).
    `requires` maps unit -> int amount, or any non-int meaning "present".
    """
    missing = []
    for unit, want in (requires or {}).items():
        if unit not in ALLOWANCE_UNITS:
            missing.append(f"{unit}:unknown_unit")
            continue
        have = (evidence.get("allowance") or {}).get(unit)
        if have == "UNLIMITED":
            continue
        if type(have) is not int \
                or (have < want if type(want) is int else have <= 0):
            missing.append(unit)
    return missing


# --- measurement completeness ------------------------------------------------

def measurement_missing(measurement):
    """Which CAP-MEASURE pieces are absent — any of them leaves the
    estimate UNKNOWN and the candidate unselectable."""
    missing = []
    if type(measurement) is not dict:
        return ["measurement"]
    for section, keys in _MEASURE_INTS:
        body = measurement.get(section)
        if type(body) is not dict:
            missing.append(section)
            continue
        for key in keys:
            if type(body.get(key)) is not int or body[key] < 0:
                missing.append(f"{section}.{key}")
    edge = measurement.get("shared_edge")
    if type(edge) is dict \
            and type(edge.get("edge_id")) is not str:
        missing.append("shared_edge.edge_id")
    timeline = measurement.get("stage_timeline")
    if type(timeline) is not list or not timeline:
        missing.append("stage_timeline")
    else:
        for index, stage in enumerate(timeline):
            if type(stage) is not dict \
                    or type(stage.get("stage")) is not str \
                    or type(stage.get("ms")) is not int \
                    or stage["ms"] < 0:
                missing.append(f"stage_timeline[{index}]")
    samples = measurement.get("samples")
    if type(samples) is dict and type(samples.get("count")) is int \
            and samples["count"] < 1:
        missing.append("samples.count")
    usage = measurement.get("usage")
    if type(usage) is not dict:
        missing.append("usage")
    else:
        for unit in ALLOWANCE_UNITS:
            if type(usage.get(unit)) is not int or usage[unit] < 0:
                missing.append(f"usage.{unit}")
    return missing


def bound_measurement(candidate, evidence, measurement_list):
    """The measurement bound to exactly this evidence document over a
    scope covering the request. A record whose evidence_digest drifted
    — or that timed a different scope — is unusable, never borrowed."""
    digest = evidence_digest(evidence)
    for record in measurement_list:
        if record.get("evidence_id") != evidence["evidence_id"] \
                or record.get("evidence_digest") != digest:
            continue
        if not scope_covers(candidate["axis"],
                            record.get("scope") or {},
                            candidate.get("scope") or {}):
            continue
        return record
    return None


# --- eligibility -------------------------------------------------------------

def _matches(candidate, evidence):
    """Identity pins of the candidate: axis plus any of driver /
    adapter(worker) digest / account binding / operation / worker it
    declares. Anything it pins must match; nothing is guessed."""
    if axis_of(evidence) != candidate.get("axis"):
        return False
    for field in ("driver", "adapter_digest", "account_binding",
                  "operation"):
        want = candidate.get(field)
        if want is not None and evidence.get(field) != want:
            return False
    worker = candidate.get("worker")
    if worker is not None:
        env = evidence.get("environment") or {}
        match = env.get("worker_id") == worker
        if not match and worker.startswith("subscription:"):
            parts = worker.split(":")
            match = len(parts) > 1 and env.get("service") == parts[1]
        if not match:
            return False
    return True


def evaluate_candidate(candidate, evidence_list, measurement_list=(), *,
                       observation=None, now_ms=None):
    """Current eligibility of one candidate for its requested scope.

    Eligible needs a matching entry that is currently
    QUALIFIED_FOR_SCOPE, covers the requested scope and carries the
    required allowance. Selectable additionally needs a complete bound
    measurement — AUTO_PERFORMANCE only ever picks selectable
    candidates; an UNKNOWN estimate never auto-selects.
    """
    verdict = {"candidate_id": candidate.get("candidate_id"),
               "kind": candidate.get("kind"),
               "axis": candidate.get("axis"),
               "route": candidate.get("route"),
               "worker": candidate.get("worker"),
               "driver": candidate.get("driver"),
               "eligible": False, "evidence_id": None,
               "registry_state": "NO_EVIDENCE", "reasons": [],
               "measurement_id": None, "missing_measurement": [],
               "estimate": "UNKNOWN", "selectable": False}
    matched_states = []
    for ev in evidence_list:
        if not _matches(candidate, ev):
            continue
        state = current_state(ev, observation, now_ms=now_ms)
        matched_states.append(state["state"])
        if state["state"] != "QUALIFIED_FOR_SCOPE":
            verdict["reasons"].extend(
                f"{ev['evidence_id']}:{reason}"
                for reason in (state["reasons"]
                               or [state["state"].lower()]))
            continue
        if not scope_covers(candidate["axis"], ev.get("scope") or {},
                            candidate.get("scope") or {}):
            verdict["reasons"].append(
                f"{ev['evidence_id']}:scope_not_covered")
            continue
        units = allowance_missing(ev, candidate.get("requires"))
        if units:
            verdict["reasons"].append(
                f"{ev['evidence_id']}:allowance_missing:"
                + ",".join(units))
            continue
        verdict["eligible"] = True
        verdict["evidence_id"] = ev["evidence_id"]
        bound = bound_measurement(candidate, ev, measurement_list)
        if bound is None:
            verdict["missing_measurement"] = ["measurement"]
            verdict["reasons"].append("no_bound_measurement")
        else:
            verdict["measurement_id"] = bound["measurement_id"]
            missing = measurement_missing(bound)
            verdict["missing_measurement"] = missing
            if missing:
                verdict["reasons"].append(
                    "measurement_incomplete:" + ",".join(missing))
            else:
                verdict["estimate"] = {
                    "cold_ms": bound["cold"]["end_to_end_ms"],
                    "warm_ms": bound["warm"]["end_to_end_ms"],
                    "time_ms": bound["cold"]["end_to_end_ms"]}
        break
    if matched_states:
        verdict["registry_state"] = max(
            matched_states, key=lambda s: _STATE_RANK.get(s, -1))
    else:
        verdict["reasons"].append("no_matching_evidence")
    verdict["selectable"] = verdict["eligible"] \
        and verdict["estimate"] != "UNKNOWN"
    return verdict


def preflight(candidates, evidence_list, measurement_list=(), *,
              policy="AUTO_PERFORMANCE", observation=None, now_ms=None):
    """Filter candidates to the ones current evidence actually covers.

    Every candidate gets a verdict with machine-readable reasons. Under
    AUTO_PERFORMANCE `selections` holds only candidates that are
    QUALIFIED_FOR_SCOPE *and* carry a complete bound measurement — an
    unverified candidate is never auto-selected. Under FIXED_ROUTE the
    user's explicit choice stays available and is labelled
    FIXED_ROUTE_USER_CHOICE, never auto-qualified.
    """
    verdicts = [evaluate_candidate(c, evidence_list, measurement_list,
                                   observation=observation,
                                   now_ms=now_ms)
                for c in candidates]
    if policy == "AUTO_PERFORMANCE":
        verified = [v for v in verdicts if v["selectable"]]
        selections = [{"candidate_id": v["candidate_id"],
                       "evidence_id": v["evidence_id"],
                       "label": "AUTO_QUALIFIED",
                       "estimate": v["estimate"]}
                      for v in sorted(
                          verified,
                          key=lambda v: v["estimate"]["time_ms"])]
    else:
        selections = [{"candidate_id": v["candidate_id"],
                       "evidence_id": v["evidence_id"],
                       "label": "FIXED_ROUTE_USER_CHOICE",
                       "eligible": v["eligible"],
                       "estimate": v["estimate"]}
                      for v in verdicts]
    return {"policy": policy, "generated_at": now(),
            "candidates": verdicts, "selections": selections,
            "unknown_estimate": [v["candidate_id"] for v in verdicts
                                 if v["estimate"] == "UNKNOWN"]}


# --- ExecutionPlan integration ------------------------------------------------

def plan_candidates(plan):
    """The candidates an ExecutionPlan 1 needs decided: one COMPOSE
    request per operation bound to its worker, route, runtime contract,
    resolution, pixel format and frame count, plus one ENCODE request
    per allowed driver over the plan's delivery scope."""
    enc = plan["encoding"]
    candidates = []
    for op in plan["operations"]:
        start, stop = op["output_range"]
        candidates.append({
            "candidate_id": op["operation_id"], "kind": "operation",
            "axis": "COMPOSE", "route": op["route"],
            "worker": op["worker"],
            "scope": {"operation": "compose_frame_range",
                      "runtime_contract": op["runtime_contract"],
                      "route": _ROUTE_TRANSPORT.get(op["route"],
                                                  op["route"]),
                      "width": enc["width"], "height": enc["height"],
                      "pixel_format": "RGBA8",
                      "frames": stop - start}})
    scope = {"width": enc["width"], "height": enc["height"],
             "fps": dict(enc["fps"]), "codec": "h264",
             "pixel_format": "yuv420p",
             "frames": enc["output_frames"]}
    for driver in enc["allowed_drivers"]:
        candidates.append({"candidate_id": f"encode:{driver}",
                           "kind": "encoder", "axis": "ENCODE",
                           "driver": driver, "scope": dict(scope)})
    return candidates


def gate_evidence(plan, report):
    """Preflight verdicts as the evidence map assert_executable()
    consumes: a route/worker only reports QUALIFIED_FOR_SCOPE when a
    current scope-bound entry backs it — and under AUTO_PERFORMANCE
    only when a complete bound measurement exists, so a plan with
    unverified timing is never auto-executable."""
    policy = plan["execution"]["policy"]
    by_id = {v["candidate_id"]: v for v in report["candidates"]}
    gate = {}
    for op in plan["operations"]:
        verdict = by_id.get(op["operation_id"])
        if verdict is None:
            continue
        ok = verdict["selectable"] if policy == "AUTO_PERFORMANCE" \
            else verdict["eligible"]
        if ok:
            entry = {"state": "QUALIFIED_FOR_SCOPE",
                     "evidence_id": verdict["evidence_id"]}
            gate[op["route"]] = dict(entry)
            gate[op["worker"]] = dict(entry)
    return gate


def plan_preflight(plan, *, state_dir=None, evidence_list=None,
                   measurement_list=None, observation=None, now_ms=None):
    """Current preflight for an ExecutionPlan 1 — the function the
    scheduler/ExecutionPlan uses to filter candidates.

    `evidence` in the report is the gate map assert_executable()
    understands: AUTO_PERFORMANCE admits only eligible candidates with a
    bound complete measurement, FIXED_ROUTE admits scope-eligible user
    choices. Callers may pass evidence/measurement lists directly or let
    them load from `state_dir`'s registry.
    """
    if evidence_list is None:
        if state_dir is None:
            raise FilmError("plan_preflight needs evidence_list or "
                            "a state_dir holding the capability registry")
        evidence_list = entries(state_dir)
    if measurement_list is None:
        measurement_list = measurements(state_dir) if state_dir else []
    report = preflight(plan_candidates(plan), evidence_list,
                       measurement_list,
                       policy=plan["execution"]["policy"],
                       observation=observation, now_ms=now_ms)
    report["evidence"] = gate_evidence(plan, report)
    return report


# --- status ------------------------------------------------------------------

def registry_status(state_dir, *, observation=None, now_ms=None):
    """Every stored entry with its computed current state and reasons —
    the machine-readable truth behind the CLI and the browser panel.

    Facets are reported honestly: this registry records capability
    evidence, and real external qualification/acceptance/release stay
    UNQUALIFIED / PENDING / NOT_AUTHORIZED — a fixture PASS or a
    document never changes that.
    """
    listed = []
    for ev in entries(state_dir):
        state = current_state(ev, observation, now_ms=now_ms)
        listed.append({
            "evidence_id": ev["evidence_id"],
            "axis": axis_of(ev),
            "driver": ev["driver"],
            "operation": ev["operation"],
            "route": ev.get("route"),
            "transport": ev.get("transport"),
            "account_binding": ev.get("account_binding"),
            "credential_epoch": ev.get("credential_epoch"),
            "adapter_digest": ev.get("adapter_digest"),
            "environment": ev.get("environment"),
            "scope": ev.get("scope"),
            "fixture": ev.get("fixture"),
            "allowance": ev.get("allowance"),
            "observed_at": ev.get("observed_at"),
            "qualification": ev.get("qualification"),
            "state": state["state"],
            "reasons": state["reasons"],
            "detail": state.get("detail"),
            "fake": is_fake(ev)})
    measured = [{"measurement_id": m["measurement_id"],
                 "evidence_id": m["evidence_id"],
                 "missing": measurement_missing(m),
                 "observed_at": m["observed_at"]}
                for m in measurements(state_dir)]
    return {"registry": str(registry_dir(state_dir)),
            "entries": listed,
            "measurements": measured,
            "generated_at": now(),
            "facets": {"node_state": "IN_PROGRESS",
                       "qualification_state": "UNQUALIFIED",
                       "acceptance_state": "PENDING",
                       "release_state": "NOT_AUTHORIZED"}}
