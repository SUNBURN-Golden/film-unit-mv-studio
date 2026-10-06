"""Node film-resource-forecast: pre-execution resource forecast.

Before any work starts, this module turns an ExecutionPlan 1 + the
capability registry (+ optional bound CAP-MEASURE records, subscription
entitlements and a disk observation) into a secret-free forecast:
expected CPU/GPU time, RAM/disk/cache peaks, transfer bytes/requests,
output size and subscription allowance impact. Every line is labelled
with the measurement environment and scope it came from — or UNKNOWN.

Fences (exec/storage §8/§11 resource rows, design §15-17):

- A bound measurement only feeds the forecast while its evidence is
  currently QUALIFIED_FOR_SCOPE *under the supplied current
  observation*. Stored evidence without a fresh observation is STALE —
  its numbers are reported as stale provenance, never as the current
  device: every forecast value falls back to UNKNOWN.
- Quote, subscription inclusion and possible additional charges are
  three separate answers. UNKNOWN inclusion is never presented as "no
  extra charge"; extra charges need the plan's allow flag AND a separate
  explicit approval — this module never approves or consumes anything.
- When a resource is insufficient the forecast *proposes* chunk ranges,
  lower quality or a FIXED_ROUTE alternative. Proposals never execute:
  an unselected paid fallback is never run.
- This is a forecast only: nothing executes, submits or charges; facets
  stay UNQUALIFIED / PENDING / NOT_AUTHORIZED.
"""
import copy
import shutil
from pathlib import Path

from .animation_schema import read_canon
from .capability_registry import (entries as _entries,
                                  measurements as _measurements,
                                  plan_candidates, plan_preflight)
from .core import FilmError, now, project_mutex
from .execution_plan import (plan_sha, validate_execution_plan)
from .execution_workers.subscription import (
    ALLOWANCE_UNITS, PAID_UNITS, charge_check, entitlement_expired,
    quote_digest, validate_entitlement)
from .frame_stream import PIXEL_FORMATS

FORECAST_TYPE = "resource_forecast"
FORECAST_VERSION = 1

FORECAST_FACETS = {"qualification_state": "UNQUALIFIED",
                   "acceptance_state": "PENDING",
                   "release_state": "NOT_AUTHORIZED",
                   "node_state": "IN_PROGRESS"}

# Line bases: where a forecast number came from.
MEASURED = "MEASURED"      # complete bound CAP-MEASURE, currently qualified
STALE = "STALE"            # measurement exists but evidence is not current
DECLARED = "DECLARED"      # plan/entitlement-declared value (cap, reservation)
DERIVED = "DERIVED"        # computed from contracted sizes (raw frame bytes)
UNKNOWN = "UNKNOWN"        # no usable source — never filled by assumption

SUBSCRIPTION_ROUTE = "SUBSCRIPTION_CODE_RUNTIME"
DECODED_PIXEL_FORMAT = "RGBA8"


# --- line labelling ---------------------------------------------------------

def _line(value, basis, *, source=None, reason=None):
    """One forecast line: the value plus exactly where it came from."""
    out = {"value": value, "basis": basis}
    if source is not None:
        out["source"] = source
    if reason:
        out["reason"] = reason
    return out


def _unknown(reason):
    return _line(UNKNOWN, UNKNOWN, reason=reason)


def _measurement_source(record, entry):
    """Provenance of a bound measurement: which environment and scope
    produced it — a past device/session is never silently presented as
    the current observation."""
    environment = (entry or {}).get("environment")
    return {"kind": "CAP-MEASURE",
            "measurement_id": record["measurement_id"],
            "evidence_id": record["evidence_id"],
            "observed_at": (entry or {}).get("observed_at"),
            "environment": environment if environment else "UNOBSERVED",
            "scope": record.get("scope")}


def _declared(what):
    return {"kind": "DECLARED", "declared_by": what}


def _derived(what):
    return {"kind": "DERIVED", "derived_from": what}


def _is_stale(record, entry, state):
    """The measurement exists but may not stand in for the current
    observation: evidence not QUALIFIED_FOR_SCOPE right now, or no
    live entry at all."""
    return record is not None \
        and (state != "QUALIFIED_FOR_SCOPE" or entry is None)


# --- per-candidate rows -----------------------------------------------------

def _frames_of(candidate, plan):
    for op in plan["operations"]:
        if op["operation_id"] == candidate["candidate_id"]:
            return op["output_range"][1] - op["output_range"][0]
    return plan["encoding"]["output_frames"]


def _raw_frame_bytes(plan):
    enc = plan["encoding"]
    return enc["width"] * PIXEL_FORMATS.get(DECODED_PIXEL_FORMAT, 4) \
        * enc["height"]


def _measured_field(record, dotted, source, *, state, entry):
    """One measured field, or an honestly-labelled fallback."""
    cursor = record
    for part in dotted.split("."):
        cursor = (cursor or {}).get(part)
        if cursor is None:
            break
    if _is_stale(record, entry, state):
        return _line(UNKNOWN, STALE, source=source,
                     reason="measurement exists but its evidence is not "
                            "current under the supplied observation — "
                            "a past device/session is never shown as "
                            "the current one")
    if cursor is None or cursor == UNKNOWN:
        return _unknown(f"measurement field {dotted} was never "
                        "observed for this scope")
    return _line(cursor, MEASURED, source=source)


def _stale_record(candidate, verdict, measurement_list, entry_map):
    """The measurement a matched-but-stale evidence record still binds:
    provenance for the row, never a usable estimate."""
    if verdict.get("measurement_id"):
        return None, None
    from .capability_registry import bound_measurement
    known = {eid for eid in entry_map}
    for reason in verdict.get("reasons") or []:
        head = str(reason).split(":", 1)[0]
        entry = entry_map.get(head) if head in known else None
        if entry is None:
            continue
        record = bound_measurement(candidate, entry, measurement_list)
        if record is not None:
            return record, entry
    return None, None


def _candidate_row(verdict, candidate, plan, measurement_list,
                   entry_map):
    """Forecast one preflight candidate: every resource line labelled."""
    measure_map = {m["measurement_id"]: m for m in measurement_list}
    record = measure_map.get(verdict.get("measurement_id"))
    entry = entry_map.get(verdict.get("evidence_id"))
    state = verdict.get("registry_state")
    if record is None:
        record, entry = _stale_record(
            candidate, verdict, measurement_list, entry_map)
    source = _measurement_source(record, entry) if record else None
    frames = _frames_of(candidate, plan)
    row = {"candidate_id": verdict["candidate_id"],
           "kind": verdict.get("kind"),
           "requested_scope": candidate.get("scope"),
           "route": verdict.get("route"),
           "worker": verdict.get("worker"),
           "driver": verdict.get("driver"),
           "axis": verdict.get("axis"),
           "frames": frames,
           "registry_state": state,
           "eligible": verdict.get("eligible"),
           "selectable": verdict.get("selectable"),
           "evidence_id": verdict.get("evidence_id"),
           "measurement_id": record["measurement_id"]
           if record else None,
           "missing_measurement":
               list(verdict.get("missing_measurement") or []),
           "reasons": list(verdict.get("reasons") or [])}
    if record is None:
        row.update({
            "time_ms": {"cold": _unknown("no bound CAP-MEASURE record "
                                        "for this scope"),
                        "warm": _unknown("no bound CAP-MEASURE record "
                                        "for this scope")},
            "peaks": {"ram_bytes": _unknown("never measured"),
                      "disk_bytes": _unknown("never measured"),
                      "vram_bytes": _unknown("never measured")},
            "transfer": {"read_bytes": _unknown("never measured"),
                         "write_bytes": _unknown("never measured"),
                         "requests": _unknown("never measured")},
            "cache": {"hits": _unknown("never measured"),
                      "misses": _unknown("never measured")},
            "usage": {},
            "manual_minutes": _unknown("never measured")})
    else:
        row.update({
            "time_ms": {
                "cold": _measured_field(record, "cold.end_to_end_ms",
                                        source, state=state,
                                        entry=entry),
                "warm": _measured_field(record, "warm.end_to_end_ms",
                                        source, state=state,
                                        entry=entry)},
            "peaks": {
                key: _measured_field(record, f"peaks.{key}", source,
                                     state=state, entry=entry)
                for key in ("ram_bytes", "disk_bytes", "vram_bytes")},
            "transfer": {
                key: _measured_field(record, f"transfer.{key}", source,
                                     state=state, entry=entry)
                for key in ("read_bytes", "write_bytes", "requests")},
            "cache": {
                key: _measured_field(record, f"cache.{key}", source,
                                     state=state, entry=entry)
                for key in ("hits", "misses")},
            "usage": {unit: _measured_field(record, f"usage.{unit}",
                                           source, state=state,
                                           entry=entry)
                      for unit in (record.get("usage") or {})},
            "manual_minutes": _measured_field(
                record, "manual.minutes", source,
                state=state, entry=entry)})
    raw = frames * _raw_frame_bytes(plan)
    row["output"] = {
        "decoded_frame_bytes": _line(
            raw, DERIVED,
            source=_derived(
                f"{frames} frames * {plan['encoding']['width']}x"
                f"{plan['encoding']['height']} {DECODED_PIXEL_FORMAT}")),
        "encoded_bytes": (row["transfer"]["write_bytes"]
                          if record is not None else
                          _unknown("no bound measurement; encoded size "
                                   "is never guessed"))}
    return row


def _sum_lines(rows, getter):
    """Sum a line across rows; UNKNOWN when any row cannot contribute."""
    total, missing = 0, []
    for row in rows:
        value = getter(row)["value"]
        if type(value) is int:
            total += value
        else:
            missing.append(row["candidate_id"])
    return total, missing


def _totals(rows, plan):
    """Plan-level totals — aggregates only over measured lines and says
    so; a partially measured plan is never rounded into a number."""
    def summed(getter, what):
        total, missing = _sum_lines(rows, getter)
        if missing:
            return {"value": UNKNOWN, "basis": UNKNOWN,
                    "measured": total,
                    "missing": missing,
                    "reason": f"{what} is UNKNOWN for {len(missing)} "
                              "candidate(s); totals never borrow"}
        return _line(total, MEASURED,
                     source={"kind": "AGGREGATE",
                             "over": "bound CAP-MEASURE records"})

    def maxed(getter, what):
        best, missing = 0, []
        for row in rows:
            value = getter(row)["value"]
            if type(value) is int:
                best = max(best, value)
            else:
                missing.append(row["candidate_id"])
        if missing and not best:
            return {"value": UNKNOWN, "basis": UNKNOWN,
                    "missing": missing,
                    "reason": f"{what} never measured for any "
                              "candidate"}
        out = {"value": best if not missing else UNKNOWN,
               "basis": MEASURED if not missing else UNKNOWN,
               "measured_max": best,
               "missing": missing,
               "reason": f"per-candidate peaks never sum; {what} is the "
                         "max over measured candidates"}
        if missing:
            out["partial"] = True
        return out

    usage = {}
    for row in rows:
        for unit, line in (row.get("usage") or {}).items():
            if usage.get(unit) == UNKNOWN:
                continue
            if type(line["value"]) is int:
                usage[unit] = usage.get(unit, 0) + line["value"]
            else:
                usage[unit] = UNKNOWN
    manual = [r["manual_minutes"]["value"] for r in rows]
    # The encoder's input is the same frames the compose operations
    # produce, so only operation rows are summed — counting encoder
    # candidates too would count those decoded bytes twice.
    decoded = sum(r["output"]["decoded_frame_bytes"]["value"]
                  for r in rows if r.get("kind") == "operation")
    return {
        "cpu_ms": summed(lambda r: r["time_ms"]["cold"],
                         "cold end_to_end_ms"),
        "gpu_ms": _unknown("no GPU timing source exists on this "
                           "contract — never inferred from CPU"),
        "warm_ms": summed(lambda r: r["time_ms"]["warm"],
                          "warm end_to_end_ms"),
        "peaks": {"ram_bytes": maxed(lambda r: r["peaks"]["ram_bytes"],
                                     "ram_bytes"),
                  "disk_bytes": maxed(lambda r: r["peaks"]["disk_bytes"],
                                      "disk_bytes"),
                  "vram_bytes": maxed(lambda r:
                                      r["peaks"]["vram_bytes"],
                                      "vram_bytes")},
        "transfer": {
            "read_bytes": summed(lambda r: r["transfer"]["read_bytes"],
                                 "transfer reads"),
            "write_bytes": summed(lambda r: r["transfer"]["write_bytes"],
                                  "transfer writes"),
            "requests": summed(lambda r: r["transfer"]["requests"],
                               "transfer requests")},
        "usage": usage,
        "manual_minutes": (
            _line(sum(m for m in manual if type(m) is int), MEASURED)
            if all(type(m) is int for m in manual) and manual else
            {"value": UNKNOWN, "basis": UNKNOWN,
             "reason": "at least one candidate's manual minutes were "
                       "never measured"}),
        "output": {
            "decoded_frame_bytes": _line(
                decoded, DERIVED,
                source=_derived("per-candidate decoded frame buffers")),
            "encoded_bytes": summed(
                lambda r: r["output"]["encoded_bytes"],
                "encoded output")}}


# --- workspace / disk ------------------------------------------------------

def _workspace_check(plan, rows, disk):
    """Declared reservations vs an observed free-space map, and measured
    peaks vs the plan's own caps."""
    checks = []
    for reservation in plan.get("resource_reservations") or []:
        location = reservation["location"]
        needed = reservation["peak_bytes"]
        observed = (disk or {}).get(location) or {}
        free = observed.get("free_bytes")
        if type(free) is not int:
            sufficient = UNKNOWN
            reason = f"no free-space observation for {location}"
        elif free >= needed:
            sufficient, reason = True, None
        else:
            sufficient = False
            reason = (f"{location} free_bytes {free} < declared "
                      f"reservation {needed}")
        checks.append({"location": location,
                       "needed_bytes": _line(
                           needed, DECLARED,
                           source=_declared(
                               "plan.resource_reservations")),
                       "free_bytes": _line(
                           free if type(free) is int else UNKNOWN,
                           MEASURED if type(free) is int else UNKNOWN,
                           source=observed.get("source"),
                           reason=None if type(free) is int else
                           "not observed"),
                       "sufficient": sufficient,
                       "reason": reason})
    caps = plan.get("workspace") or {}
    cap_total = caps.get("pc_cache_limit_bytes", 0) \
        + caps.get("worker_scratch_limit_bytes", 0)
    peaks = [r["peaks"]["disk_bytes"]["value"] for r in rows]
    measured = [v for v in peaks if type(v) is int]
    # A measured peak of 0 is a real measurement — only a missing one
    # is UNKNOWN.
    measured_peak = max(measured) if measured else None
    capacity = {"pc_cache_limit_bytes": _line(
                    caps.get("pc_cache_limit_bytes", 0), DECLARED,
                    source=_declared("plan.workspace")),
                "worker_scratch_limit_bytes": _line(
                    caps.get("worker_scratch_limit_bytes", 0),
                    DECLARED, source=_declared("plan.workspace")),
                "on_limit": caps.get("on_limit"),
                "measured_peak_disk_bytes": _line(
                    measured_peak if measured_peak is not None
                    else UNKNOWN,
                    MEASURED if measured_peak is not None else UNKNOWN,
                    reason=None if measured_peak is not None else
                    "no measured disk peak yet"),
                "peak_within_declared_caps":
                    (measured_peak <= cap_total)
                    if measured_peak is not None else UNKNOWN}
    return {"reservations": checks, "capacity": capacity,
            "shortfalls": [c for c in checks
                           if c["sufficient"] is False]}


# --- subscription / quote ---------------------------------------------------

def _service_of_worker(worker_id):
    if type(worker_id) is str and worker_id.startswith("subscription:"):
        return worker_id.split(":", 2)[1]
    return None


def _chunk_estimate(plan, operation, caps):
    """Packet chunk ranges under declared service caps — DECLARED caps,
    never probed reality."""
    from .execution_packets import contract_for_plan, split_ranges
    if not caps:
        return None
    try:
        return split_ranges(operation["output_range"],
                            contract_for_plan(plan), caps)
    except FilmError:
        return None


def _subscription_section(plan, rows, entitlements, prior_quotes,
                          issued_packets, now_ms):
    """Included-in-subscription vs quote vs possible additional charge —
    three separate answers per service."""
    by_service = {}
    for row in rows:
        if row.get("route") != SUBSCRIPTION_ROUTE:
            continue
        service = _service_of_worker(row.get("worker"))
        by_service.setdefault(service, []).append(row)
    issued = []
    for packet in issued_packets or []:
        ent = packet.get("entitlement") or {}
        issued.append({"packet_id": packet.get("packet_id"),
                       "service": ent.get("service"),
                       "quote_digest": ent.get("quote_digest")})
    services = {}
    for service, service_rows in by_service.items():
        frames = sum(r["frames"] for r in service_rows)
        entitlement = next((e for e in (entitlements or [])
                            if e.get("service") == service), None)
        usage_units = sorted({
            unit for r in service_rows for unit in (r.get("usage") or {})
            if unit in ALLOWANCE_UNITS})
        unmeasured_bases = set()
        for r in service_rows:
            for unit in usage_units:
                line = (r.get("usage") or {}).get(unit) or {}
                if type(line.get("value")) is not int:
                    unmeasured_bases.add(line.get("basis") or "missing")
        usage_known = bool(usage_units) and not unmeasured_bases
        unmeasured_paid = sorted(
            u for u in usage_units if u in PAID_UNITS and any(
                type(((r.get("usage") or {}).get(u) or {}).get("value"))
                is not int for r in service_rows))
        section = {"service": service,
                   "operations": [r["candidate_id"]
                                  for r in service_rows],
                   "frames": frames}
        if entitlement is None:
            section.update({
                "entitlement": "UNREGISTERED",
                "inclusion": UNKNOWN,
                "charge": {"verdict": "INCLUSION_UNKNOWN",
                           "reason": "no entitlement record on file for "
                                     "this service — never shown as "
                                     "free"},
                "quote": {"status": "NO_QUOTE",
                          "quote_digest": None}})
            services[service] = section
            continue
        expired = entitlement_expired(entitlement, now_ms=now_ms)
        output_range = next(
            (op["output_range"] for op in plan["operations"]
             if op["operation_id"]
             == service_rows[0]["candidate_id"]), None)
        chunks = _chunk_estimate(
            plan, {"output_range": output_range},
            entitlement.get("service_caps")) \
            if output_range is not None else None
        if usage_known:
            cost = {u: sum(r["usage"][u]["value"] for r in service_rows)
                    for u in usage_units}
            cost_basis = MEASURED
        else:
            cost = {"subscription_units": frames}
            if chunks is not None:
                cost["handoff_minutes"] = len(chunks)
            cost_basis = DECLARED
        verdict = charge_check(
            entitlement, cost,
            allow_additional_charges=
            plan["execution"]["allow_additional_charges"],
            additional_charges_approved=False)
        if not usage_known:
            # A declared frame-count estimate can never prove there is
            # no extra charge — an unmeasured usage stays unknown, and
            # any paid units of a known or stale measurement are
            # unaccounted for.
            note = ("subscription usage is not measured ("
                    + ", ".join(sorted(unmeasured_bases) or ["missing"])
                    + ") — the declared frame estimate cannot prove "
                    "there is no extra charge; any paid units of a "
                    "known or stale measurement are unaccounted for")
            if unmeasured_paid:
                note += ": " + ", ".join(unmeasured_paid)
            if verdict["verdict"] in {"COVERED",
                                      "EXTRA_CHARGE_APPROVED"}:
                verdict = {"verdict": "USAGE_UNKNOWN",
                           "detail": verdict.get("detail") or {},
                           "reason": note,
                           "declared_estimate_verdict": verdict}
            else:
                verdict = dict(verdict)
                base = (verdict.get("reason") or "").rstrip()
                verdict["reason"] = base + " — " + note if base else note
        current_quote = quote_digest(entitlement)
        prior = (prior_quotes or {}).get(service)
        issued_here = [i for i in issued if i["service"] == service]
        changed = bool(prior and prior != current_quote) or any(
            i["quote_digest"] and i["quote_digest"] != current_quote
            for i in issued_here)
        section.update({
            "entitlement": "EXPIRED" if expired else "CURRENT",
            "entitlement_id": entitlement["entitlement_id"],
            "credential_epoch": entitlement["credential_epoch"],
            "inclusion": entitlement["inclusion"]
            ["execution_in_subscription"],
            "cost_estimate": {"units": cost, "basis": cost_basis,
                              "unmeasured_paid_units": unmeasured_paid},
            "charge": verdict,
            "quote": {"status": "CHANGED" if changed else "CURRENT",
                      "quote_digest": current_quote,
                      "prior_quote_digest": prior,
                      "issued_packets": issued_here},
            "paid_units_in_cost": sorted(
                u for u in cost if u in PAID_UNITS
                and type(cost[u]) is int and cost[u] > 0),
            "packet_chunks": [list(c) for c in chunks]
            if chunks is not None else UNKNOWN})
        services[service] = section
    return services


# --- refusals and alternatives ----------------------------------------------

def _chunk_proposal(plan, free_bytes):
    """Concrete alternative ranges that fit the observed free space,
    budgeted by derived decoded bytes per frame."""
    per_frame = _raw_frame_bytes(plan)
    if type(free_bytes) is not int or free_bytes < per_frame:
        return None
    width = max(1, min(48, free_bytes // per_frame))
    ranges = []
    for op in plan["operations"]:
        start, end = op["output_range"]
        while start < end:
            stop = min(end, start + width)
            ranges.append({"operation_id": op["operation_id"],
                           "frame_range": [start, stop]})
            start = stop
    return {"kind": "CHUNK_RANGES",
            "reason": "split the operation ranges so each chunk's "
                      "scratch fits the observed free space",
            "ranges": ranges,
            "budget": f"{per_frame} derived bytes/frame against "
                      f"{free_bytes} observed free",
            "executes": "NEVER — a proposed range only runs after an "
                        "explicit new plan and selection"}


def _alternatives(plan, rows, subscriptions, workspace):
    """Proposals only. No alternative ever executes itself; a paid
    fallback that was not explicitly selected is never run."""
    proposals = []
    for shortfall in workspace["shortfalls"]:
        free = shortfall["free_bytes"]["value"]
        chunk = _chunk_proposal(
            plan, free if type(free) is int else None)
        if chunk:
            proposals.append(chunk)
        fmt = plan["encoding"]
        proposals.append({
            "kind": "LOWER_QUALITY",
            "reason": f"{shortfall['location']} cannot hold the "
                      "declared reservation",
            "proposal": {"width": max(2, fmt["width"] // 2),
                         "height": max(2, fmt["height"] // 2),
                         "note": "smaller format = new plan revision; "
                                 "never applied silently"},
            "executes": "NEVER"})
        proposals.append({
            "kind": "REDUCE_CACHE_LIMIT",
            "reason": "workspace cap exceeds the observed free space",
            "proposal": "lower pc_cache_limit_bytes / "
                        "worker_scratch_limit_bytes in a new plan "
                        "revision",
            "executes": "NEVER"})
    for service, section in subscriptions.items():
        verdict = (section.get("charge") or {}).get("verdict")
        if section.get("entitlement") == "EXPIRED":
            proposals.append({
                "kind": "RENEW_ENTITLEMENT",
                "reason": f"entitlement for {service} expired",
                "proposal": "renew or re-register the entitlement; "
                            "expired evidence never grants a run",
                "executes": "NEVER"})
        if section.get("quote", {}).get("status") == "CHANGED":
            proposals.append({
                "kind": "REQUOTE",
                "reason": f"the service terms for {service} changed "
                          "after the last quote",
                "proposal": "re-issue the quote and any packets bound "
                            "to the old quote_digest",
                "executes": "NEVER"})
        if verdict in {"ALLOWANCE_EXHAUSTED", "INCLUSION_UNKNOWN",
                       "NOT_INCLUDED"}:
            proposals.append({
                "kind": "ROUTE_OR_CHUNK",
                "reason": f"charge verdict {verdict} for {service}",
                "proposal": "wait for renewal, confirm inclusion, or "
                            "select a different verified route — an "
                            "unselected paid fallback never runs",
                "executes": "NEVER",
                "paid_fallback_auto_run": False})
        if verdict == "ADDITIONAL_CHARGE_REFUSED":
            proposals.append({
                "kind": "EXPLICIT_CHARGE_APPROVAL",
                "reason": f"{service} costs fall outside the "
                          "subscription inclusion",
                "proposal": "a separate explicit approval is required "
                            "on top of allow_additional_charges; the "
                            "forecast never approves or runs it",
                "executes": "NEVER",
                "paid_fallback_auto_run": False})
    selectable = [r for r in rows if r.get("selectable")]
    if plan["execution"]["policy"] == "AUTO_PERFORMANCE" \
            and not selectable:
        proposals.append({
            "kind": "RE_MEASURE",
            "reason": "AUTO_PERFORMANCE has no currently qualified, "
                      "completely measured candidate",
            "proposal": "run perf-benchmark to bind fresh CAP-MEASURE "
                        "records for the current device/session, or pin "
                        "an explicit FIXED_ROUTE",
            "executes": "NEVER"})
    return proposals


def _refusals(plan, rows, subscriptions, workspace):
    """Hard reasons this plan cannot start as-is."""
    refusals = []
    if plan["execution"]["policy"] == "AUTO_PERFORMANCE" \
            and plan["execution"]["capability_evidence_required"] \
            and not any(r.get("selectable") for r in rows):
        refusals.append({
            "code": "AUTO_NO_CANDIDATES",
            "reason": "no candidate is currently qualified with a "
                      "complete bound measurement — AUTO never selects "
                      "on stale or absent evidence"})
    for c in workspace["shortfalls"]:
        refusals.append({"code": "DISK_SHORTFALL",
                         "reason": c["reason"]})
    for service, section in subscriptions.items():
        if section.get("entitlement") == "EXPIRED":
            refusals.append({
                "code": "ENTITLEMENT_EXPIRED",
                "reason": f"subscription entitlement for {service} "
                          "expired"})
        verdict = (section.get("charge") or {}).get("verdict")
        if verdict not in {None, "COVERED", "EXTRA_CHARGE_APPROVED"}:
            refusals.append({
                "code": verdict,
                "reason": (section["charge"].get("reason")
                           or f"charge verdict {verdict}"),
                "service": service})
        if section.get("quote", {}).get("status") == "CHANGED":
            refusals.append({
                "code": "QUOTE_CHANGED",
                "reason": f"the quote for {service} changed after "
                          "issuance; re-quote before any run",
                "service": service})
    return refusals


# --- the forecast -------------------------------------------------------------

def forecast_plan(plan, *, state_dir=None, evidence_list=None,
                  measurement_list=None, observation=None,
                  entitlements=None, disk=None, prior_quotes=None,
                  issued_packets=None, now_ms=None):
    """Secret-free resource forecast for an ExecutionPlan 1.

    `observation` is the *current* environment observation; without it
    every stored evidence record is STALE and measured values degrade to
    UNKNOWN. `disk` maps reservation location -> {"free_bytes": int,
    "source": str}; an absent observation is UNKNOWN, not assumed.
    `issued_packets` accepts previously exported packet dicts so a quote
    change after issuance is caught (QUOTE_CHANGED).
    """
    plan = validate_execution_plan(copy.deepcopy(plan))
    if evidence_list is None:
        evidence_list = _entries(state_dir) if state_dir is not None \
            else []
    if measurement_list is None:
        measurement_list = _measurements(state_dir) \
            if state_dir is not None else []
    preflight = plan_preflight(
        plan, evidence_list=evidence_list,
        measurement_list=measurement_list, observation=observation,
        now_ms=now_ms)
    entry_map = {e["evidence_id"]: e for e in evidence_list}
    candidates = {c["candidate_id"]: c for c in plan_candidates(plan)}
    rows = [_candidate_row(verdict, candidates[verdict["candidate_id"]],
                           plan, measurement_list, entry_map)
            for verdict in preflight["candidates"]]
    subscriptions = _subscription_section(
        plan, rows, entitlements, prior_quotes, issued_packets, now_ms)
    workspace = _workspace_check(plan, rows, disk)
    return {
        "document_type": FORECAST_TYPE,
        "forecast_version": FORECAST_VERSION,
        "generated_at": now(),
        "boundary": "forecast only — nothing executes, submits, "
                    "consumes allowance or charges; proposals require "
                    "explicit selection",
        "plan": {"plan_sha": plan_sha(plan),
                 "plan_revision": plan["plan_revision"],
                 "snapshot_digest": plan["snapshot_digest"],
                 "policy": plan["execution"]["policy"],
                 "allow_additional_charges":
                     plan["execution"]["allow_additional_charges"],
                 "transfer_route": plan["transfer_route"]},
        "operations": rows,
        "totals": _totals(rows, plan),
        "workspace": workspace,
        "subscription": subscriptions,
        "refusals": _refusals(plan, rows, subscriptions, workspace),
        "alternatives": _alternatives(plan, rows, subscriptions,
                                      workspace),
        "unknowns": sorted({
            c for c in preflight.get("unknown_estimate") or []}),
        "facets": dict(FORECAST_FACETS)}


# --- CLI ------------------------------------------------------------------

def _observation_now():
    """The current device/session observation — the only thing that may
    make stored evidence CURRENT for this forecast."""
    from .perf_scheduler import _worker_environment
    return {"environment": _worker_environment()}


def _observed_disk(project):
    usage = shutil.disk_usage(project)
    return {"USER_DESKTOP": {
        "free_bytes": usage.free,
        "source": f"shutil.disk_usage({project})",
        "observed_at": now()}}


def _load_entitlements(state_dir):
    folder = Path(state_dir) / "subscriptions"
    if not folder.is_dir():
        return []
    root = folder.resolve()
    out = []
    for path in sorted(folder.glob("*.json")):
        # Only real files inside the subscriptions dir are read — a
        # symlink or anything resolving outside is never followed.
        if path.is_symlink() \
                or not path.resolve().is_relative_to(root):
            continue
        out.append(validate_entitlement(read_canon(path)))
    return out


def resource_forecast_command(project, *, plan_path=None, state_dir=None,
                              observation=None, observe=True,
                              disk=None, packet_paths=(), now_ms=None):
    """`resource-forecast PROJECT [--plan PATH]` — the plan's forecast.

    The plan comes from --plan, then <project>/execution/plan.json, then
    the default AUTO_PERFORMANCE perf plan for the project's graph.
    """
    p = Path(project).resolve()
    with project_mutex(p):
        if plan_path is not None:
            plan = validate_execution_plan(read_canon(plan_path))
        elif (p / "execution" / "plan.json").is_file():
            plan = validate_execution_plan(
                read_canon(p / "execution" / "plan.json"))
        else:
            from .perf_scheduler import make_perf_plan
            plan = make_perf_plan(p)
        state_dir = Path(state_dir) if state_dir \
            else Path(p) / "render" / "perf" / "registry"
        packets = [read_canon(Path(d) / "packet.json"
                              if Path(d).is_dir() else Path(d))
                   for d in packet_paths]
        forecast = forecast_plan(
            plan,
            state_dir=state_dir if Path(state_dir).is_dir() else None,
            observation=observation
            if observation is not None
            else (_observation_now() if observe else None),
            entitlements=_load_entitlements(state_dir),
            disk=disk or _observed_disk(p),
            issued_packets=packets,
            now_ms=now_ms)
    forecast["project"] = str(p)
    forecast["state_dir"] = str(state_dir)
    return forecast
