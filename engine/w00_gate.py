"""FRAME_ANIMATION_V1 W00 pilot gate (ANIM-022; design 9.3/10.5).

W00 is the initial production wave — the hardest main-film cuts and the
transition cuts joining two of them, produced first inside the real film,
never a separate pilot work. This module records the per-target pilot
evidence for that wave and reports the next-wave gate OPEN/BLOCKED with
explicit reasons.

Records (all CANON_JSON_V1 append-only logs under production/):

- `w00_pilot` (w00_pilots.jsonl): one pilot record per W00 cut/transition,
  binding the adopted source digest, the sequence digest, the reviewed
  artifact digest (the bound CUT/TRANSITION review's binding hash), the
  capability ref, the render-manifest ref (when computable), the durable
  segment-job result id, the selection reason, the reviewer and the
  KEEP / CHANGE / MIX decision.
- `approver_delegation` (approver_delegations.jsonl): a delegation record
  naming delegator, scope and expiry. Only the primary approver 박준태 or
  an unexpired delegate may author HUMAN pilot/spend/delivery approvals.
- `w00_spend_approval` (w00_spend_approvals.jsonl): the separate
  additional-spend approval a CHANGE/MIX route requires, bound to the
  exact route-decision revision it approves.
- `delivery_approval` (delivery_approvals.jsonl): the governed audit record
  for a sealed clean/subbed deliverable's own FINAL_FILM review — each
  deliverable is approved separately against its own artifact hash.

Gate semantics: the next-wave gate is BLOCKED until every pilot target
holds a CURRENT pilot record, a route decision exists, and — under
KEEP — no current pilot dissents. Under CHANGE/MIX it additionally
re-checks the current PLAN/WAVE locks, settles in-flight jobs, verifies
every ledger reservation still matches its job record, requires a
`w00_spend_approval` bound to the current decision for every outstanding
spend obligation, and refuses while any job sits in an ambiguous
(SUBMITTING/UNKNOWN/CANCEL_REQUESTED) state. Nothing here auto-executes
work; the records are protocol evidence. Synthetic reviewers are declared
`SYNTHETIC_FIXTURE` — their records are development evidence only and the
reported facets stay qualification UNQUALIFIED, acceptance PENDING,
release NOT_AUTHORIZED.

Schema note: `w00_pilot`, `approver_delegation`, `w00_spend_approval` and
`delivery_approval` are provisional types in
docs/adr/0002-provisional-document-types.md. Non-author review of that
amendment is PENDING. They are not an ADR 0001 ratification or a release.
"""
import hashlib
import json
import re
import time
from pathlib import Path

from .animation_assets import load_registry
from .animation_locks import (EVIDENCE_FACETS, EVIDENCE_NOTE, WAVES_PATH,
                              WAVE_ID, _initial_wave, latest_route_decision,
                              load_waves, lock_status)
from .animation_review import (BINDING_FIELDS, LIMITATION_DISPOSITIONS,
                               _build_folder, _deliverable_path,
                               _film_binding_current, _issue_list,
                               bound_targets, film_review_status,
                               load_reviews, record_film_review,
                               review_status)
from .animation_schema import (canon_bytes, check_document,
                               load_animation_timeline,
                               require_animation_profile)
from .builds import list_builds, verify_build
from .core import FilmError, atomic_text, digest, now, project_mutex, read, \
    safe_path

PRIMARY_APPROVER = "박준태"

PILOT_TYPE = "w00_pilot"
PILOTS_PATH = "production/w00_pilots.jsonl"
DELEGATION_TYPE = "approver_delegation"
DELEGATIONS_PATH = "production/approver_delegations.jsonl"
SPEND_TYPE = "w00_spend_approval"
SPEND_PATH = "production/w00_spend_approvals.jsonl"
DELIVERY_TYPE = "delivery_approval"
DELIVERY_PATH = "production/delivery_approvals.jsonl"

REVIEWER_KINDS = ("HUMAN", "SYNTHETIC_FIXTURE")
PILOT_DECISIONS = ("KEEP", "CHANGE", "MIX")
DELIVERABLES = ("MASTER_CLEAN.mp4", "MASTER_SUBBED.mp4")
DELEGATION_SCOPES = ("W00_PILOT", "W00_SPEND", "DELIVERY_APPROVAL", "ALL")
FILM_METHODS = ["FULL_SPEED_WHOLE_FILM", "TECHNICAL_VALIDATION"]
ISSUE_DISPOSITIONS = {"FIX_REQUIRED", *LIMITATION_DISPOSITIONS}

# Sealed-inventory members a font/cue change touches (ADR 0001 §8): the
# subtitle exports and font, the lyric/cue snapshot, the burned delivery
# frames and the subbed master. None of them feeds MASTER_CLEAN.mp4.
SUBTITLE_SIDE_FILES = {"MASTER_SUBBED.mp4", "lyrics.ass", "lyrics.srt",
                       "lyrics_source.txt", "lyrics_timed.json",
                       "subtitle_report.json", "snapshot/input/lyrics.txt",
                       "snapshot/lyrics/lyrics_source.txt",
                       "snapshot/lyrics/lyrics_timed.json"}
SUBTITLE_SIDE_DIRS = ("subtitle_fonts/", "final_frames/")

# Durable job states that fence progression: the submission/cancel outcome
# is unconfirmed — reconcile the same job identity, never a new one.
AMBIGUOUS_STATES = {"SUBMITTING", "UNKNOWN", "CANCEL_REQUESTED"}
# Confirmed but still live; a route change must wait for them to settle.
LIVE_STATES = {"RUNNING", "OUTPUT_PENDING_VERIFY"}
# States holding money or a decision that a changed route must re-approve.
OBLIGATION_STATES = {"RESERVED", "WAITING_USER", "FAILED_CONFIRMED"}

PILOT_ID = re.compile(r"WP[0-9]{4,}")
DELEGATION_ID = re.compile(r"DG[0-9]{4,}")
SPEND_ID = re.compile(r"SA[0-9]{4,}")
DELIVERY_ID = re.compile(r"DA[0-9]{4,}")
SHA256 = re.compile(r"[0-9a-f]{64}")
JOB_ID = re.compile(r"seg-[0-9a-f]{20,}")

PILOT_FIELDS = {"document_type", "schema_version", "pilot_id", "wave",
                "target_kind", "target_id", "reason",
                "source_sha256", "sequence_sha256", "artifact_sha256",
                "capability_ref", "manifest_ref", "job_result_id",
                "review_id", "reviewer", "reviewer_kind", "delegation_id",
                "decision", "issues", "recorded_at", "binding_sha256"}
PILOT_BINDING = ("wave", "target_kind", "target_id", "source_sha256",
                 "sequence_sha256", "artifact_sha256", "review_id")
DELEGATION_FIELDS = {"document_type", "schema_version", "delegation_id",
                     "delegator", "delegate", "scope", "expires_at_ms",
                     "note", "recorded_at"}
SPEND_FIELDS = {"document_type", "schema_version", "approval_id", "wave",
                "decision_id", "jobs", "reason", "approver",
                "reviewer_kind", "delegation_id", "recorded_at"}
DELIVERY_FIELDS = {"document_type", "schema_version", "approval_id",
                   "build_id", "deliverable", "review_id",
                   "deliverable_sha256", "build_manifest_sha256",
                   "frame_sequence_root", "approver", "reviewer_kind",
                   "delegation_id", "decision", "recorded_at"}


def _sha256(document):
    return hashlib.sha256(canon_bytes(document)).hexdigest()


def _sha_or_none(value, field, allow_none=True):
    if value is None and allow_none:
        return
    if type(value) is not str or not SHA256.fullmatch(value):
        raise FilmError(f"{field} must be a lowercase sha256")


def _validate_issues(value):
    _issue_list(value, "issues", ISSUE_DISPOSITIONS, require_reason=False)
    for item in value:
        if item["disposition"] in LIMITATION_DISPOSITIONS:
            if type(item.get("reason")) is not str \
                    or not item["reason"].strip():
                raise FilmError("issues with a limitation disposition need "
                                "an explicit reason")
            scope = item.get("scope")
            range_scope = type(scope) is list and len(scope) == 2 \
                and all(type(v) is int and v >= 0 for v in scope)
            if not range_scope \
                    and not (type(scope) is str and scope.strip()):
                raise FilmError("issues with a limitation disposition need "
                                "a scope: a label or a [start, end] range")


def _pilot_binding(record):
    return _sha256({k: record[k] for k in PILOT_BINDING})


def validate_pilot(record):
    """Structural contract of one `w00_pilot` record."""
    check_document(record, PILOT_TYPE)
    if type(record) is not dict or set(record.keys()) != PILOT_FIELDS:
        raise FilmError("w00_pilot must hold exactly its schema fields "
                        f"(missing {sorted(PILOT_FIELDS - record.keys())}, "
                        f"extra {sorted(set(record) - PILOT_FIELDS)})")
    if not PILOT_ID.fullmatch(record["pilot_id"]
                              if type(record["pilot_id"]) is str else ""):
        raise FilmError(f"Bad pilot id: {record['pilot_id']}")
    if not WAVE_ID.fullmatch(record["wave"]
                             if type(record["wave"]) is str else ""):
        raise FilmError(f"Bad wave id: {record['wave']}")
    if record["target_kind"] not in ("CUT", "TRANSITION"):
        raise FilmError("target_kind must be CUT or TRANSITION")
    for field in ("target_id", "reason", "review_id", "reviewer",
                  "recorded_at"):
        if type(record[field]) is not str or not record[field].strip():
            raise FilmError(f"w00_pilot needs a non-empty {field}")
    _sha_or_none(record["source_sha256"], "source_sha256")
    _sha_or_none(record["sequence_sha256"], "sequence_sha256")
    _sha_or_none(record["artifact_sha256"], "artifact_sha256",
                 allow_none=False)
    ref = record["manifest_ref"]
    if ref is not None and not (
            type(ref) is str and ref.startswith("render_manifest:")
            and SHA256.fullmatch(ref.split(":", 1)[1])):
        raise FilmError("manifest_ref must be 'render_manifest:<sha256>' "
                        "or null")
    if record["capability_ref"] is not None and not (
            type(record["capability_ref"]) is str
            and record["capability_ref"].strip()):
        raise FilmError("capability_ref must be a non-empty string or null")
    if record["job_result_id"] is not None and not (
            type(record["job_result_id"]) is str
            and JOB_ID.fullmatch(record["job_result_id"])):
        raise FilmError("job_result_id must be a seg-<digest> id or null")
    if record["reviewer_kind"] not in REVIEWER_KINDS:
        raise FilmError(f"reviewer_kind must be one of {REVIEWER_KINDS}")
    delegation = record["delegation_id"]
    if delegation is not None and not DELEGATION_ID.fullmatch(
            delegation if type(delegation) is str else ""):
        raise FilmError("delegation_id must be a DG id or null")
    if record["decision"] not in PILOT_DECISIONS:
        raise FilmError(f"decision must be one of {PILOT_DECISIONS}")
    _validate_issues(record["issues"])
    if record["binding_sha256"] != _pilot_binding(record):
        raise FilmError("w00_pilot binding_sha256 does not match the bound "
                        "fields")
    return record


def validate_delegation(record):
    """Structural contract of one `approver_delegation` record."""
    check_document(record, DELEGATION_TYPE)
    if type(record) is not dict or set(record.keys()) != DELEGATION_FIELDS:
        raise FilmError("approver_delegation must hold exactly its schema "
                        "fields")
    if not DELEGATION_ID.fullmatch(record["delegation_id"]
                                   if type(record["delegation_id"]) is str
                                   else ""):
        raise FilmError(f"Bad delegation id: {record['delegation_id']}")
    if record["delegator"] != PRIMARY_APPROVER:
        raise FilmError("a delegation's delegator must be the primary "
                        f"approver {PRIMARY_APPROVER}")
    if type(record["delegate"]) is not str or not record["delegate"].strip() \
            or record["delegate"] == PRIMARY_APPROVER:
        raise FilmError("a delegation needs a delegate distinct from the "
                        "primary approver")
    scope = record["scope"]
    if type(scope) is not list or not scope \
            or any(s not in DELEGATION_SCOPES for s in scope):
        raise FilmError(f"scope must be a non-empty subset of "
                        f"{DELEGATION_SCOPES}")
    if type(record["expires_at_ms"]) is not int \
            or record["expires_at_ms"] <= 0:
        raise FilmError("expires_at_ms must be a positive integer")
    if type(record["note"]) is not str \
            or type(record["recorded_at"]) is not str \
            or not record["recorded_at"].strip():
        raise FilmError("approver_delegation needs note and recorded_at")
    return record


def validate_spend(record):
    """Structural contract of one `w00_spend_approval` record."""
    check_document(record, SPEND_TYPE)
    if type(record) is not dict or set(record.keys()) != SPEND_FIELDS:
        raise FilmError("w00_spend_approval must hold exactly its schema "
                        "fields")
    if not SPEND_ID.fullmatch(record["approval_id"]
                              if type(record["approval_id"]) is str else ""):
        raise FilmError(f"Bad spend approval id: {record['approval_id']}")
    if not WAVE_ID.fullmatch(record["wave"]
                             if type(record["wave"]) is str else ""):
        raise FilmError(f"Bad wave id: {record['wave']}")
    for field in ("decision_id", "reason", "approver", "recorded_at"):
        if type(record[field]) is not str or not record[field].strip():
            raise FilmError(f"w00_spend_approval needs a non-empty {field}")
    if type(record["jobs"]) is not list \
            or any(not JOB_ID.fullmatch(j) if type(j) is str else True
                   for j in record["jobs"]):
        raise FilmError("w00_spend_approval jobs must be seg-<digest> ids")
    if record["reviewer_kind"] not in REVIEWER_KINDS:
        raise FilmError(f"reviewer_kind must be one of {REVIEWER_KINDS}")
    delegation = record["delegation_id"]
    if delegation is not None and not DELEGATION_ID.fullmatch(
            delegation if type(delegation) is str else ""):
        raise FilmError("delegation_id must be a DG id or null")
    return record


def validate_delivery(record):
    """Structural contract of one `delivery_approval` audit record."""
    check_document(record, DELIVERY_TYPE)
    if type(record) is not dict or set(record.keys()) != DELIVERY_FIELDS:
        raise FilmError("delivery_approval must hold exactly its schema "
                        "fields")
    if not DELIVERY_ID.fullmatch(record["approval_id"]
                                 if type(record["approval_id"]) is str
                                 else ""):
        raise FilmError(f"Bad delivery approval id: "
                        f"{record['approval_id']}")
    if record["deliverable"] not in DELIVERABLES:
        raise FilmError(f"deliverable must be one of {DELIVERABLES}")
    for field in ("build_id", "review_id", "approver", "recorded_at"):
        if type(record[field]) is not str or not record[field].strip():
            raise FilmError(f"delivery_approval needs a non-empty {field}")
    _sha_or_none(record["deliverable_sha256"], "deliverable_sha256",
                 allow_none=False)
    _sha_or_none(record["build_manifest_sha256"], "build_manifest_sha256",
                 allow_none=False)
    _sha_or_none(record["frame_sequence_root"], "frame_sequence_root",
                 allow_none=False)
    if record["reviewer_kind"] not in REVIEWER_KINDS:
        raise FilmError(f"reviewer_kind must be one of {REVIEWER_KINDS}")
    delegation = record["delegation_id"]
    if delegation is not None and not DELEGATION_ID.fullmatch(
            delegation if type(delegation) is str else ""):
        raise FilmError("delegation_id must be a DG id or null")
    if record["decision"] not in ("APPROVED", "FIX_REQUIRED"):
        raise FilmError("delivery_approval decision must be APPROVED or "
                        "FIX_REQUIRED")
    return record


# ---------------------------------------------------------------------------
# canonical append-only logs


def _read_log(p, relative, validator):
    path = safe_path(p, relative)
    if not path.exists():
        return []
    lines = path.read_bytes().split(b"\n")
    if lines[-1] != b"":
        raise FilmError(f"{relative} must end with a single LF")
    records = []
    for index, line in enumerate(lines[:-1]):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FilmError(f"{relative} line {index + 1} is not JSON") \
                from exc
        if line + b"\n" != canon_bytes(record):
            raise FilmError(f"{relative} line {index + 1} is not canonical")
        records.append(validator(record))
    return records


def _append_log(p, relative, record, validator, id_field, prefix):
    path = safe_path(p, relative)
    records = _read_log(p, relative, validator)
    used = {r[id_field] for r in records}
    number = 1
    while f"{prefix}{number:04d}" in used:
        number += 1
    record[id_field] = f"{prefix}{number:04d}"
    validator(record)
    prior = path.read_bytes() if path.exists() else b""
    atomic_text(path, prior.decode("utf-8")
                + canon_bytes(record).decode("utf-8"))
    return record


def load_pilots(p):
    return _read_log(p, PILOTS_PATH, validate_pilot)


def load_delegations(p):
    return _read_log(p, DELEGATIONS_PATH, validate_delegation)


def load_spend_approvals(p):
    return _read_log(p, SPEND_PATH, validate_spend)


def load_delivery_approvals(p):
    return _read_log(p, DELIVERY_PATH, validate_delivery)


# ---------------------------------------------------------------------------
# approver policy


def _now_ms(now_ms):
    return int(time.time() * 1000) if now_ms is None else int(now_ms)


def record_delegation(project, *, delegate, scope, expires_at_ms,
                      delegator=PRIMARY_APPROVER, note="", now_ms=None):
    """Record that the primary approver delegates `scope` until expiry."""
    p = Path(project)
    with project_mutex(p):
        require_animation_profile(p)
        delegator = str(delegator or "").strip()
        if delegator != PRIMARY_APPROVER:
            raise FilmError("only the primary approver "
                            f"{PRIMARY_APPROVER} may delegate")
        delegate = str(delegate or "").strip()
        if not delegate or delegate == PRIMARY_APPROVER:
            raise FilmError("a delegation needs a delegate distinct from "
                            "the primary approver")
        scope = [s.strip().upper() for s in (scope or [])]
        if not scope or any(s not in DELEGATION_SCOPES for s in scope):
            raise FilmError(f"scope must be a non-empty subset of "
                            f"{DELEGATION_SCOPES}")
        expires_at_ms = int(expires_at_ms)
        if expires_at_ms <= _now_ms(now_ms):
            raise FilmError("expires_at_ms must be in the future")
        record = {"document_type": DELEGATION_TYPE, "schema_version": 1,
                  "delegation_id": "", "delegator": delegator,
                  "delegate": delegate, "scope": sorted(set(scope)),
                  "expires_at_ms": expires_at_ms, "note": str(note or ""),
                  "recorded_at": now()}
        return _append_log(p, DELEGATIONS_PATH, record, validate_delegation,
                           "delegation_id", "DG")


def _delegation_for(p, delegate, scope, now_ms=None):
    """The latest unexpired delegation covering `scope`, if one exists."""
    found = None
    for record in load_delegations(p):
        if record["delegate"] != delegate:
            continue
        if scope not in record["scope"] and "ALL" not in record["scope"]:
            continue
        if record["expires_at_ms"] <= _now_ms(now_ms):
            continue
        found = record
    return found


def _approver_verdict(p, name, scope, now_ms=None):
    """The W00 approver rule: 박준태 or an unexpired recorded delegate."""
    name = str(name or "").strip()
    if not name:
        return False, "an approver name is required", None
    if name == PRIMARY_APPROVER:
        return True, "primary approver", None
    delegation = _delegation_for(p, name, scope, now_ms)
    if delegation is None:
        return False, (f"{name} is not {PRIMARY_APPROVER} and holds no "
                       f"unexpired delegation covering {scope}"), None
    return True, (f"delegated by {delegation['delegator']} "
                  f"({delegation['delegation_id']})"), \
        delegation["delegation_id"]


def _check_reviewer(p, name, reviewer_kind, scope, delegation_id, now_ms):
    """Enforce the approver rule on a HUMAN record; synthetic fixtures are
    flagged, never authorized as human evidence."""
    reviewer_kind = str(reviewer_kind or "").strip().upper()
    if reviewer_kind not in REVIEWER_KINDS:
        raise FilmError(f"reviewer_kind must be one of {REVIEWER_KINDS}")
    delegation_ref = None
    if reviewer_kind == "HUMAN":
        ok, why, delegation_ref = _approver_verdict(p, name, scope, now_ms)
        if not ok:
            raise FilmError(why)
    if delegation_id is not None and delegation_id != delegation_ref:
        raise FilmError(f"delegation_id {delegation_id} does not match the "
                        f"valid delegation {delegation_ref}")
    return reviewer_kind, delegation_ref


# ---------------------------------------------------------------------------
# pilot targets and current bindings


def pilot_targets(project):
    """The W00 pilot targets: the initial wave's own cuts plus every
    transition that joins two of them."""
    p = Path(project)
    config = require_animation_profile(p)
    timeline = load_animation_timeline(p)
    waves = load_waves(p)
    if waves is None:
        return {"wave": _initial_wave(config), "declared": False,
                "cuts": [], "transitions": []}
    shots = set(waves["waves"][0]["shots"])
    entries = timeline["entries"]
    shot_of = {e["instance_id"]: e["shot_id"] for e in entries}
    cuts = [e["instance_id"] for e in entries if e["shot_id"] in shots]
    transitions = [e["transition_out"]["id"] for e in entries
                   if e["shot_id"] in shots and e.get("transition_out")
                   and shot_of.get(e["transition_out"]["to_instance"])
                   in shots]
    return {"wave": waves["waves"][0]["wave"], "declared": True,
            "cuts": cuts, "transitions": transitions}


def _target_bindings(p, targets):
    """The digests a CURRENT pilot record must bind for each target.

    `source_sha256` is the adopted source content digest (for a transition,
    a digest over both endpoints' sources); `sequence_sha256` the bound
    sequence digest (both endpoints for a transition); `artifact_sha256`
    the bound review's binding hash — the identity of the exact reviewed
    artifact. A target whose review is not CURRENT binds nothing.
    """
    bound = bound_targets(p, strict=False)
    status = review_status(p, strict=False)
    by_id = {r["review_id"]: r for r in load_reviews(p)}
    assignments = load_registry(p).get("assignments") or {}
    out = {}
    for iid in targets["cuts"]:
        row = bound["cuts"].get(iid) or {}
        state = status["targets"].get(iid) or {}
        review = by_id.get(state.get("review_id") or "")
        if "unresolved" in row or state.get("state") != "CURRENT" \
                or review is None:
            out[iid] = None
            continue
        pin = assignments.get(row["shot_id"]) or {}
        out[iid] = {"target_kind": "CUT", "target_id": iid,
                    "source_sha256": pin.get("content_sha256"),
                    "sequence_sha256": row["sequence_digest"],
                    "artifact_sha256": review["binding_sha256"],
                    "review_id": review["review_id"],
                    "shots": [row["shot_id"]]}
    for tid in targets["transitions"]:
        row = bound["transitions"].get(tid) or {}
        state = status["targets"].get(tid) or {}
        review = by_id.get(state.get("review_id") or "")
        from_row = bound["cuts"].get(row.get("from_instance")) or {}
        to_row = bound["cuts"].get(row.get("to_instance")) or {}
        if "unresolved" in row or state.get("state") != "CURRENT" \
                or review is None or "unresolved" in from_row \
                or "unresolved" in to_row:
            out[tid] = None
            continue
        from_shot = from_row["shot_id"]
        to_shot = to_row["shot_id"]
        from_pin = assignments.get(from_shot) or {}
        to_pin = assignments.get(to_shot) or {}
        out[tid] = {
            "target_kind": "TRANSITION", "target_id": tid,
            "source_sha256": _sha256(
                {"kind": "TRANSITION_SOURCES",
                 "from": from_pin.get("content_sha256"),
                 "to": to_pin.get("content_sha256")}),
            "sequence_sha256": _sha256(
                {"kind": "TRANSITION_SEQUENCES",
                 "from": {"digest": row["from_sequence_digest"],
                          "revision": row["from_revision"]},
                 "to": {"digest": row["to_sequence_digest"],
                        "revision": row["to_revision"]}}),
            "artifact_sha256": review["binding_sha256"],
            "review_id": review["review_id"],
            "shots": [from_shot, to_shot]}
    return out


def _render_provenance():
    """engine.render_provenance, or None where its ANIM-017 scheduler
    dependency cannot load (the Unix-only `resource` module on Windows).
    Any other import failure is a real defect and propagates."""
    try:
        from . import render_provenance
    except ModuleNotFoundError as exc:
        if exc.name != "resource":
            raise
        return None
    return render_provenance


def _manifest_ref(p):
    """The current render_manifest digest, or None when the film cannot be
    resolved yet (later-wave shots unproduced) or the manifest builder is
    unavailable on this platform. Never invented."""
    provenance = _render_provenance()
    if provenance is None:
        return None
    try:
        return "render_manifest:" + provenance.manifest_digest(
            provenance.build_manifest(p))
    except (FilmError, OSError, KeyError, TypeError, ValueError):
        return None


def _job_binding(p, job_result_id, binding):
    """A pilot's durable job evidence must be a VERIFIED segment job whose
    shot belongs to the target."""
    from . import segment_gen
    job = segment_gen._find_job(p, job_result_id)
    if job["status"] != "VERIFIED":
        raise FilmError(
            f"job {job_result_id} is {job['status']}; a W00 pilot record "
            "binds a VERIFIED durable job result")
    if job["shot_id"] not in binding["shots"]:
        raise FilmError(
            f"job {job_result_id} belongs to {job['shot_id']}, not to "
            f"{binding['target_id']}'s shots {binding['shots']}")
    return job


def record_pilot(project, target_id, *, decision, reviewer, reviewer_kind,
                 reason, capability_ref=None, job_result_id=None,
                 issues=None, delegation_id=None, now_ms=None):
    """Append a W00 pilot record bound to the target's current review.

    The record binds the adopted source digest, the sequence digest, the
    reviewed-artifact binding hash, the capability and manifest refs, a
    durable VERIFIED job result id (when the target was produced by one),
    the selection reason, the reviewer and the KEEP/CHANGE/MIX decision.
    A SYNTHETIC_FIXTURE reviewer records development evidence only.
    """
    p = Path(project)
    with project_mutex(p):
        require_animation_profile(p)
        targets = pilot_targets(p)
        if not targets["declared"]:
            raise FilmError(f"Declare {WAVES_PATH} before W00 pilot records")
        if target_id not in targets["cuts"] + targets["transitions"]:
            raise FilmError(f"{target_id} is not a {targets['wave']} pilot "
                            f"target (pilots: "
                            f"{targets['cuts'] + targets['transitions']})")
        binding = _target_bindings(p, targets).get(target_id)
        if binding is None:
            raise FilmError(
                f"{target_id} has no current adopted review to bind — the "
                "W00 pilot evidence needs a CURRENT review first")
        decision = str(decision or "").strip().upper()
        if decision not in PILOT_DECISIONS:
            raise FilmError(f"decision must be one of {PILOT_DECISIONS}")
        reviewer = str(reviewer or "").strip()
        if not reviewer:
            raise FilmError("a pilot record needs a named reviewer")
        reviewer_kind, delegation_ref = _check_reviewer(
            p, reviewer, reviewer_kind, "W00_PILOT", delegation_id, now_ms)
        reason = str(reason or "").strip()
        if not reason:
            raise FilmError("a pilot record needs the reason the target "
                            "was selected/reviewed")
        if capability_ref is not None:
            capability_ref = str(capability_ref).strip() or None
        if job_result_id is not None:
            job_result_id = str(job_result_id).strip()
            _job_binding(p, job_result_id, binding)
        issues = [dict(item) for item in issues or []]
        _validate_issues(issues)
        record = {"document_type": PILOT_TYPE, "schema_version": 1,
                  "pilot_id": "", "wave": targets["wave"],
                  "target_kind": binding["target_kind"],
                  "target_id": target_id, "reason": reason,
                  "source_sha256": binding["source_sha256"],
                  "sequence_sha256": binding["sequence_sha256"],
                  "artifact_sha256": binding["artifact_sha256"],
                  "capability_ref": capability_ref,
                  "manifest_ref": _manifest_ref(p),
                  "job_result_id": job_result_id,
                  "review_id": binding["review_id"],
                  "reviewer": reviewer, "reviewer_kind": reviewer_kind,
                  "delegation_id": delegation_ref,
                  "decision": decision, "issues": issues,
                  "recorded_at": now()}
        record["binding_sha256"] = _pilot_binding(record)
        return _append_log(p, PILOTS_PATH, record, validate_pilot,
                           "pilot_id", "WP")


# ---------------------------------------------------------------------------
# additional-spend approvals (W00-ROUTE)


def record_spend_approval(project, *, approver, reviewer_kind, reason,
                          jobs=None, delegation_id=None, now_ms=None):
    """The separate spend approval a CHANGE/MIX route requires.

    Bound to the current route-decision revision: a superseding decision
    makes this record stop satisfying obligations. Approver policy is the
    same as for pilots; SYNTHETIC_FIXTURE is development evidence only.
    """
    p = Path(project)
    with project_mutex(p):
        config = require_animation_profile(p)
        waves = load_waves(p)
        if waves is None:
            raise FilmError(f"Declare {WAVES_PATH} before spend approvals")
        initial = _initial_wave(config)
        decision = latest_route_decision(p, initial)
        if decision is None:
            raise FilmError("no W00 route decision exists to approve spend "
                            "against")
        if decision["decision"] not in ("CHANGE", "MIX"):
            raise FilmError("an additional-spend approval binds a "
                            "CHANGE/MIX route decision only")
        approver = str(approver or "").strip()
        reviewer_kind, delegation_ref = _check_reviewer(
            p, approver, reviewer_kind, "W00_SPEND", delegation_id, now_ms)
        reason = str(reason or "").strip()
        if not reason:
            raise FilmError("a spend approval needs its reason")
        from . import segment_gen
        job_ids = []
        for job_id in jobs or []:
            segment_gen._find_job(p, job_id)  # must name a real job
            job_ids.append(job_id)
        record = {"document_type": SPEND_TYPE, "schema_version": 1,
                  "approval_id": "", "wave": initial,
                  "decision_id": decision["decision_id"], "jobs": job_ids,
                  "reason": reason, "approver": approver,
                  "reviewer_kind": reviewer_kind,
                  "delegation_id": delegation_ref, "recorded_at": now()}
        return _append_log(p, SPEND_PATH, record, validate_spend,
                           "approval_id", "SA")


# ---------------------------------------------------------------------------
# sealed clean/subbed delivery approvals


def approve_delivery(project, build_id, deliverable, *, approver,
                     reviewer_kind, methods=None, decision="APPROVED",
                     unresolved_major_issues=None, accepted_limitations=None,
                     delegation_id=None, now_ms=None):
    """Approve one sealed deliverable — clean and subbed separately.

    Records the deliverable-bound FINAL_FILM review plus a governed audit
    record naming the approver. Approving MASTER_CLEAN.mp4 never approves
    MASTER_SUBBED.mp4: each binds its own artifact hash. Nothing is written
    into the sealed build directory.
    """
    p = Path(project)
    if deliverable not in DELIVERABLES:
        raise FilmError(f"deliverable must be one of {DELIVERABLES} — "
                        "sealed clean and subbed deliveries are approved "
                        "separately")
    with project_mutex(p):
        require_animation_profile(p)
        approver = str(approver or "").strip()
        reviewer_kind, delegation_ref = _check_reviewer(
            p, approver, reviewer_kind, "DELIVERY_APPROVAL", delegation_id,
            now_ms)
        review = record_film_review(
            p, build_id, reviewer=approver,
            methods=list(methods or FILM_METHODS), deliverable=deliverable,
            decision=decision,
            unresolved_major_issues=unresolved_major_issues,
            accepted_limitations=accepted_limitations,
            allow_ungoverned=(reviewer_kind != "HUMAN"), now_ms=now_ms)
        audit = {"document_type": DELIVERY_TYPE, "schema_version": 1,
                 "approval_id": "", "build_id": build_id,
                 "deliverable": deliverable,
                 "review_id": review["review_id"],
                 "deliverable_sha256": review["deliverable_sha256"],
                 "build_manifest_sha256": review["build_manifest_sha256"],
                 "frame_sequence_root": review["frame_sequence_root"],
                 "approver": approver, "reviewer_kind": reviewer_kind,
                 "delegation_id": delegation_ref,
                 "decision": review["decision"], "recorded_at": now()}
        _append_log(p, DELIVERY_PATH, audit, validate_delivery,
                    "approval_id", "DA")
        return {"review": review, "approval": audit}


def _subtitle_side(relative):
    relative = relative.replace("\\", "/")
    return relative in SUBTITLE_SIDE_FILES \
        or relative.startswith(SUBTITLE_SIDE_DIRS)


def _clean_film_binding_current(folder, record):
    """The MASTER_CLEAN.mp4 subset of a sealed FINAL_FILM binding.

    ADR 0001 §8: a font/cue change stales the subbed master and the
    subbed delivery, never the clean master. The clean approval stays
    CURRENT while its clean-relevant bindings verify on disk — the
    deliverable's own digest, the build record, every sealed inventory
    member outside the subtitle side, the edit digest and the audio. The
    bound font/lyrics-review digests, the subtitle-side inventory members
    and the subbed frame-sequence root do not apply to the clean file.
    """
    try:
        fields = {k: record[k] for k in BINDING_FIELDS["FINAL_FILM"]}
        if hashlib.sha256(canon_bytes(fields)).hexdigest() \
                != record["binding_sha256"]:
            return False
        deliverable = _deliverable_path(folder, record["deliverable"])
        if not deliverable.is_file() \
                or digest(deliverable) != record["deliverable_sha256"]:
            return False
        build_file = folder / "build.json"
        if not build_file.is_file() \
                or digest(build_file) != record["build_manifest_sha256"]:
            return False
        if any(not _subtitle_side(error)
               for error in verify_build(folder)["errors"]):
            return False
        build = read(build_file)
        if build["edit_digest"] != record["edit_digest"]:
            return False
        master = safe_path(folder, build["audio"]["path"])
        return master.is_file() \
            and digest(master) == record["audio_sha256"]
    except (AttributeError, FilmError, OSError, KeyError, TypeError,
            ValueError):
        return False


def deliverable_status(project, build_id):
    """Per-deliverable approval state of one sealed build.

    An approval is CURRENT only while its bound digests still verify on
    disk — a new artifact hash needs a new approval; an old approval never
    carries over. `governed` means the governing review was recorded
    through `approve_delivery` under the approver policy: the latest
    still-bound review, or — when later bound reviews only re-approve
    without that audit (the protocol `approve-film` record) — the latest
    governed approval they did not supersede. A later non-approving
    review always ends governance.
    """
    from .animation_review import _approves
    p = Path(project)
    require_animation_profile(p)
    folder = _build_folder(p, build_id)
    build_file = folder / "build.json"
    if not build_file.is_file():
        raise FilmError(f"No build {build_id}")
    manifest_sha = digest(build_file)
    reviews = load_reviews(p)
    audits = [a for a in load_delivery_approvals(p)
              if a["build_id"] == build_id]
    audit_for = {a["review_id"]: a for a in audits}
    out = {}
    for deliverable in DELIVERABLES:
        records = [r for r in reviews if r["scope"] == "FINAL_FILM"
                   and r["build_id"] == build_id
                   and r["deliverable"] == deliverable]
        current = _clean_film_binding_current \
            if deliverable == "MASTER_CLEAN.mp4" \
            else _film_binding_current
        bound = [r for r in records
                 if r["build_manifest_sha256"] == manifest_sha
                 and current(folder, r)]
        latest = bound[-1] if bound else None
        approved = latest is not None and _approves(latest)
        audit = None
        for record in reversed(bound):
            if record["review_id"] in audit_for:
                if record is latest or _approves(record):
                    audit = audit_for[record["review_id"]]
                break
            if not _approves(record):
                break
        out[deliverable] = {
            "deliverable": deliverable,
            "deliverable_sha256":
                digest(folder / deliverable)
                if (folder / deliverable).is_file() else None,
            "state": ("CURRENT" if approved
                      else "CHANGES_REQUIRED" if bound
                      else "STALE" if records else "UNREVIEWED"),
            "review_id": latest["review_id"] if approved else None,
            "governed": audit is not None,
            "governing_review_id": audit["review_id"] if audit else None,
            "approval_id": audit["approval_id"] if audit else None,
            "approver": audit["approver"] if audit else None,
            "reviewer_kind": audit["reviewer_kind"] if audit else None,
            "records": len(records)}
    return {"build_id": build_id, "deliverables": out}


# ---------------------------------------------------------------------------
# the gate


def _jobs(p):
    from .segment_gen import _list_job_files
    return [read(path) for path in _list_job_files(p)]


def _pilot_rows(p, targets, bindings, pilots, manifest_ref):
    counts, latest = {}, {}
    for record in pilots:
        counts[record["target_id"]] = counts.get(record["target_id"], 0) + 1
        latest[record["target_id"]] = record
    target_ids = set(targets["cuts"]) | set(targets["transitions"])
    rows = []
    for target_id in [*targets["cuts"], *targets["transitions"]]:
        record = latest.get(target_id)
        if record is None:
            rows.append({"target_id": target_id, "pilot_id": None,
                         "state": "NO_PILOT_RECORD", "synthetic": None,
                         "decision": None, "review_id": None,
                         "reviewer": None, "records": 0,
                         "manifest_current": None})
            continue
        binding = bindings.get(target_id)
        state = "CURRENT" if binding is not None and _pilot_binding(
            {"wave": targets["wave"], **binding}) \
            == record["binding_sha256"] else "STALE"
        rows.append({**record, "state": state,
                     "synthetic": record["reviewer_kind"]
                     == "SYNTHETIC_FIXTURE",
                     "records": counts[target_id],
                     "manifest_current": None
                     if record["manifest_ref"] is None
                     or manifest_ref is None
                     else record["manifest_ref"] == manifest_ref})
    for record in pilots:
        if record["target_id"] in target_ids:
            continue
        rows.append({**record, "state": "OUT_OF_SCOPE",
                     "synthetic": record["reviewer_kind"]
                     == "SYNTHETIC_FIXTURE",
                     "records": counts[record["target_id"]],
                     "manifest_current": None})
    return rows


def gate_status(project):
    """The W00 next-wave gate: OPEN/BLOCKED with explicit reasons.

    Reports the pilot records per target, lock/review state, closure
    projection, honest quote/usage, UNKNOWN jobs, spend obligations and
    approvals, per-deliverable delivery state and the evidence facets.
    Computes no work and executes nothing.
    """
    provenance = _render_provenance()
    p = Path(project)
    with project_mutex(p):
        config = require_animation_profile(p)
        timeline = load_animation_timeline(p)
        waves = load_waves(p)
        initial = _initial_wave(config)
        locks = lock_status(p)
        reviews = review_status(p, strict=False)
        records = load_reviews(p)
        pilots = load_pilots(p)
        delegations = load_delegations(p)
        spend = load_spend_approvals(p)
        jobs = _jobs(p)
        ledger = read(p / "render/ledger.json", {"jobs": {}}) \
            or {"jobs": {}}
        manifest_ref = _manifest_ref(p)
        targets = pilot_targets(p)
        bindings = _target_bindings(p, targets) if targets["declared"] \
            else {}
        decision = latest_route_decision(p, initial) \
            if targets["declared"] else None
        rows = _pilot_rows(p, targets, bindings, pilots, manifest_ref)
        # OUT_OF_SCOPE rows are append-only history for targets that left
        # the W00 scope; gate reasons consider current targets only.
        scope_rows = [row for row in rows
                      if row["state"] != "OUT_OF_SCOPE"]

        reasons = []
        if not targets["declared"]:
            reasons.append(f"{WAVES_PATH} is not declared — the W00 scope "
                           "is unknown")
        else:
            if locks["plan"]["state"] != "CURRENT":
                reasons.append(f"PLAN_LOCK is {locks['plan']['state']}")
            wave_lock = locks["waves"].get(initial, {})
            if wave_lock.get("state") != "CURRENT":
                reasons.append(f"WAVE_LOCK {initial} is "
                               f"{wave_lock.get('state', 'UNLOCKED')}")
            for row in scope_rows:
                if row["state"] != "CURRENT":
                    detail = ("no pilot record"
                              if row["state"] == "NO_PILOT_RECORD"
                              else "pilot record is stale")
                    reasons.append(f"{row['target_id']}: {detail}")
            if decision is None:
                reasons.append("the W00 route decision is pending")
        # An ambiguous durable job always fences progression — reconcile
        # its existing identity before anything advances.
        unknown = [j for j in jobs if j["status"] in AMBIGUOUS_STATES]
        for job in unknown:
            reasons.append(f"{job['job_id']} is {job['status']} — reconcile "
                           "the existing job identity; progression is "
                           "fenced")
        if decision is not None and decision["decision"] in ("CHANGE",
                                                             "MIX"):
            for wave_id, wave in locks["waves"].items():
                if wave["state"] == "STALE":
                    reasons.append(f"WAVE_LOCK {wave_id} is STALE — the "
                                   "changed route needs the current wave "
                                   "revision")
            for job in jobs:
                if job["status"] in LIVE_STATES:
                    reasons.append(f"{job['job_id']} is {job['status']} — "
                                   "settle the in-flight spend before the "
                                   "route changes")
            for job in jobs:
                reservation = job.get("reservation")
                if not reservation:
                    continue
                entry = ledger["jobs"].get(reservation["ledger_key"])
                amount = (entry or {}).get("reserved_amount",
                                          (entry or {})
                                          .get("reserved_credits"))
                if entry is None or amount != reservation["amount"] \
                        or (entry or {}).get("billing_unit", "credits") \
                        != reservation["unit"]:
                    reasons.append(f"{job['job_id']}'s reservation "
                                   f"{reservation['ledger_key']} does not "
                                   "match the ledger — reconcile before "
                                   "the route changes")
            approvals = [a for a in spend
                         if a["decision_id"] == decision["decision_id"]]
            covered = {j for a in approvals for j in a["jobs"]}
            obligations = [j for j in jobs
                           if j["status"] in OBLIGATION_STATES
                           or (j["status"] == "PLANNED"
                               and j.get("quote"))]
            for job in obligations:
                if job["job_id"] not in covered:
                    reasons.append(
                        f"{job['job_id']} ({job['status']}) needs an "
                        f"additional-spend approval bound to "
                        f"{decision['decision_id']}")
        if decision is not None and decision["decision"] == "KEEP":
            for row in scope_rows:
                if row["state"] == "CURRENT" \
                        and row["decision"] != "KEEP":
                    reasons.append(
                        f"pilot {row['pilot_id']} decided "
                        f"{row['decision']} for {row['target_id']} — a "
                        "wave KEEP must not contradict the pilot records")

        by_id = {r["review_id"]: r for r in records}
        issues = []
        for row in rows:
            if row.get("pilot_id"):
                for item in row.get("issues", []):
                    issues.append({"source": row["pilot_id"],
                                   "target": row["target_id"], **item})
        for record in records:
            key = record.get("instance_id") or record.get("transition_id") \
                or record.get("build_id") or record["scope"]
            for item in record["unresolved_major_issues"]:
                issues.append({"source": record["review_id"],
                               "target": key, **item})
            for item in record["accepted_limitations"]:
                issues.append({"source": record["review_id"],
                               "target": key, **item})

        quotes, quote_totals = [], {}
        usage, usage_totals = [], {}
        for job in jobs:
            quote = job.get("quote")
            if quote:
                quotes.append({"job_id": job["job_id"],
                               "status": job["status"],
                               "quote_id": quote["quote_id"],
                               "unit": quote["unit"],
                               "amount": quote["amount"]})
                quote_totals[quote["unit"]] = round(
                    quote_totals.get(quote["unit"], 0) + quote["amount"], 6)
            reservation = job.get("reservation")
            if reservation:
                usage.append({"job_id": job["job_id"],
                              "status": job["status"],
                              "charge_state": job["charge_state"],
                              "ledger_key": reservation["ledger_key"],
                              "unit": reservation["unit"],
                              "amount": reservation["amount"]})
                usage_totals[reservation["unit"]] = round(
                    usage_totals.get(reservation["unit"], 0)
                    + reservation["amount"], 6)

        builds = [b for b in list_builds(p)
                  if b.get("document_type") == "animation_build"
                  and b.get("mode") == "FINAL_CANDIDATE"
                  and b.get("status") == "COMPLETE"]
        deliveries = {b["build_id"]: deliverable_status(p, b["build_id"])
                      for b in builds}

        open_waves = []
        if decision is not None:
            declared = {w["wave"] for w in waves["waves"]}
            open_waves = [w for w in decision["apply_scope"]["waves"]
                          if w in declared]
        pilot_methods = {}
        for row in rows:
            review = by_id.get(row.get("review_id") or "")
            pilot_methods[row["target_id"]] = review["methods"] \
                if review else []
        return {"kind": "w00_gate", "project": str(p),
                "initial_wave": initial,
                "pilot_targets": targets,
                "pilots": rows,
                "route_decision": decision,
                "open_waves": open_waves,
                "gate": {"state": "BLOCKED" if reasons else "OPEN",
                         "reasons": reasons},
                "locks": {"plan": locks["plan"], "waves": locks["waves"],
                          "final": locks["final"]},
                "reviews": reviews["targets"],
                "playback": {
                    "cuts": {t: {"state": (reviews["targets"].get(t)
                                           or {}).get("state",
                                                      "UNRESOLVED"),
                                 "methods": pilot_methods.get(t, [])}
                             for t in targets["cuts"]},
                    "transitions": {
                        t: {"state": (reviews["targets"].get(t)
                                      or {}).get("state", "UNRESOLVED"),
                            "methods": pilot_methods.get(t, [])}
                        for t in targets["transitions"]},
                    "whole_film": {b["build_id"]: {
                        "film_review": film_review_status(p,
                                                          b["build_id"]),
                        "deliverables":
                            deliveries[b["build_id"]]["deliverables"]}
                        for b in builds}},
                "issues": issues,
                "closure": provenance._current_stale(reviews, locks)
                if provenance is not None else None,
                "quote": {"jobs": quotes, "totals": quote_totals},
                "usage": {"reservations": usage,
                          "reserved_totals": usage_totals},
                "unknown": [{"job_id": j["job_id"], "status": j["status"],
                             "charge_state": j["charge_state"]}
                            for j in unknown],
                "spend": {"obligations": [
                              {"job_id": j["job_id"],
                               "status": j["status"],
                               "quote": j.get("quote")}
                              for j in jobs
                              if j["status"] in OBLIGATION_STATES
                              or (j["status"] == "PLANNED"
                                  and j.get("quote"))],
                          "approvals": spend},
                "deliveries": deliveries,
                "delegations": delegations,
                "manifest_ref": manifest_ref,
                "evidence": {"pilots": len(rows),
                             "synthetic_pilots":
                                 sum(1 for r in rows if r["synthetic"]),
                             "synthetic_delivery_approvals":
                                 sum(1 for a in load_delivery_approvals(p)
                                     if a["reviewer_kind"]
                                     == "SYNTHETIC_FIXTURE")},
                "facets": dict(EVIDENCE_FACETS),
                "note": EVIDENCE_NOTE}


def w00_status(project):
    """CLI payload for `w00-status PROJECT`."""
    return gate_status(Path(project))
