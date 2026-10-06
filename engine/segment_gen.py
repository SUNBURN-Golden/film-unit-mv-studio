"""FRAME_ANIMATION_V1 path-B segment generation (ANIM-010, design 8.3/8.5,
schema 6.1/13, ADR 0001 10).

A path-B segment asks a generation provider to produce the cut-local frames
of an interval `[start, end)` conditioned on declared inputs: the start anchor
image, an optional forced end anchor, master references, pose/layout guides
and masks. This module owns the engine side of that contract:

- capability preflight: every adapter publishes a declaration (reference image
  limits, guide kinds, mask/region, start/end image support and the returned
  endpoint rule, alpha output, native size/aspect/fps/length, operation
  identity for resume, cost unit). A required input that is missing stops with
  NEEDS_MANUAL_WORK; one the adapter cannot honour stops with
  CAPABILITY_UNAVAILABLE. Required inputs are never trimmed to a provider
  limit, and a plan forcing an end anchor is never served by a start-only
  adapter — the end condition is never silently dropped.

- money ordering: quote before submit; a changed quote needs re-approval; the
  reservation and the durable job record exist before any submission, and a
  `job_journal` SUBMIT_INTENT is appended before the first provider side
  effect (schema 13.1).

- UNKNOWN fencing: the job key binds snapshot+operation+range+recipe+runtime;
  attempt ids and submission request ids are separate. An ambiguous acceptance
  never gets a new job or request id — only `segment_reconcile` on the same
  identity may resolve it. Failed or pending reservations stay reserved; they
  are never treated as refunded without provider confirmation.

- returned-clip verification and normalization: the declared endpoint rule is
  honoured (a returned duplicate end anchor is explicitly dropped and
  recorded), a clip shorter than the owned range is rejected (never
  speed-changed or padded), a longer clip needs an explicit used-range
  selection, and source PTS maps to output-local frames by exact rational
  arithmetic (`map_source_frames`, FLOOR_CONTAINMENT_V1). Normalized members
  stage under the shot and `commit_segment_sequence` assembles them into an
  ordinary DRAFT FRAME_SEQUENCE.

The only wired adapter is `fake_segment` (engine/segment_fake.py), a
deterministic local double labelled FAKE/UNQUALIFIED. Other adapters (e.g.
`gemini_video`, declared start-only) exist as capability declarations whose
submit path refuses — this node makes no real provider call, no network
access and no paid generation.
"""
import hashlib
import json
import uuid
from fractions import Fraction
from pathlib import Path

from . import budget
from .animation_assets import (ASSET_ID, _commit_control_image,
                               _decode_png, _pin, _read_source,
                               _register_frame_sequence, _resolve_pin,
                               _store_member, load_registry, save_registry)
from .animation_schema import (SHOT_ID, canon_bytes, check_document,
                               load_animation_timeline,
                               require_animation_profile)
from .core import (FilmError, digest, now, object_hash, project_mutex, read,
                   safe_path, write)
from .frame_sequence import map_source_frames
from .motion_plan import load_shot_plan, plan_path

JOURNAL_TYPE = "job_journal"
JOURNAL_FIELDS = {"document_type", "schema_version", "job_id", "attempt_id",
                  "request_id", "event", "at", "data"}
JOURNAL_EVENTS = {"SUBMIT_INTENT", "SUBMIT_ACCEPTED", "SUBMIT_LOST",
                  "SUBMIT_REJECTED", "STATUS_QUERY", "STATUS_OBSERVED",
                  "IMPORT_INTENT", "IMPORT_OBSERVED", "RETRY_INTENT"}

# WorkerProtocol 1 states (schema 13) plus the one edge a synchronous
# definitive refusal needs: an observed rejection is a confirmed failure, not
# ambiguity, so SUBMITTING may go straight to FAILED_CONFIRMED.
JOB_STATES = {"PLANNED", "RESERVED", "WAITING_USER", "SUBMITTING", "RUNNING",
              "UNKNOWN", "CANCEL_REQUESTED", "CANCEL_CONFIRMED",
              "OUTPUT_PENDING_VERIFY", "FAILED_CONFIRMED", "VERIFIED",
              "ARCHIVED"}
TRANSITIONS = {
    "PLANNED": {"RESERVED"},
    "RESERVED": {"WAITING_USER", "SUBMITTING", "CANCEL_CONFIRMED"},
    "WAITING_USER": {"SUBMITTING", "CANCEL_CONFIRMED"},
    "SUBMITTING": {"RUNNING", "UNKNOWN", "CANCEL_REQUESTED",
                   "FAILED_CONFIRMED"},
    "UNKNOWN": {"RUNNING", "FAILED_CONFIRMED", "OUTPUT_PENDING_VERIFY",
                "CANCEL_CONFIRMED"},
    "RUNNING": {"OUTPUT_PENDING_VERIFY", "FAILED_CONFIRMED", "UNKNOWN",
                "CANCEL_REQUESTED"},
    "OUTPUT_PENDING_VERIFY": {"VERIFIED", "FAILED_CONFIRMED", "UNKNOWN",
                              "CANCEL_REQUESTED"},
    "CANCEL_REQUESTED": {"CANCEL_CONFIRMED", "OUTPUT_PENDING_VERIFY",
                         "UNKNOWN"},
    "VERIFIED": {"ARCHIVED"},
}
# States in which a submission identity is in flight or ambiguous: no new
# attempt and no resubmission; reconcile is the only way forward.
FENCED_STATES = {"SUBMITTING", "UNKNOWN"}
ACTIVE_STATES = {"SUBMITTING", "UNKNOWN", "RUNNING"}

ANCHOR_ROLES = {"KEYPOSE", "BREAKDOWN", "POSE"}
CONTROL_IMPORT_ROLES = {"KEYPOSE", "BREAKDOWN", "POSE", "LAYOUT"}
BILLING_UNITS = {"credits", "USD"}
OPERATION = "SEGMENT_GENERATE_V1"

ENDPOINT_INCLUDED = "START_END_INCLUDED"
ENDPOINT_START_ONLY = "START_ONLY"


def _int(value, what, minimum=0):
    if type(value) is not int or value < minimum:
        raise FilmError(f"{what} must be an integer >= {minimum}")
    return value


def validate_job_journal(document):
    """Structural contract of `job_journal` 1 records (schema 13.1)."""
    check_document(document, JOURNAL_TYPE)
    if type(document) is not dict or set(document.keys()) != JOURNAL_FIELDS:
        raise FilmError(f"job_journal must hold {sorted(JOURNAL_FIELDS)}")
    if type(document["job_id"]) is not str or not document["job_id"]:
        raise FilmError("job_journal job_id must be a non-empty string")
    for key in ("attempt_id", "request_id"):
        if document[key] is not None and type(document[key]) is not str:
            raise FilmError(f"job_journal {key} must be a string or null")
    if document["event"] not in JOURNAL_EVENTS:
        raise FilmError(f"Unknown job_journal event {document['event']}")
    if type(document["at"]) is not str or not document["at"]:
        raise FilmError("job_journal at must be a timestamp string")
    if type(document["data"]) is not dict:
        raise FilmError("job_journal data must be an object")
    return document


# ---------------------------------------------------------------------------
# job storage + journal
# ---------------------------------------------------------------------------

def _job_root(p, shot_id):
    return safe_path(p, f"animation/segment_jobs/{shot_id}")


def _job_file(p, shot_id, job_id):
    return _job_root(p, shot_id) / f"{job_id}.json"


def _journal_file(p, shot_id, job_id):
    return _job_root(p, shot_id) / f"{job_id}.journal.jsonl"


def _save_job(p, job):
    path = _job_file(p, job["shot_id"], job["job_id"])
    job["updated_at"] = now()
    write(path, job)


def _list_job_files(p, shot_id=None):
    root = safe_path(p, "animation/segment_jobs")
    if not root.is_dir():
        return []
    files = []
    for shot_dir in sorted(root.iterdir()):
        if shot_id is not None and shot_dir.name != shot_id:
            continue
        if shot_dir.is_dir():
            files.extend(sorted(shot_dir.glob("seg-*.json")))
    return files


def _find_job(p, job_id):
    for path in _list_job_files(p):
        if path.stem == job_id:
            return read(path)
    raise FilmError(f"Unknown segment job: {job_id}")


def _journal(p, job, event, data):
    """Append one canon job_journal record before/after the provider act."""
    document = {"document_type": JOURNAL_TYPE, "schema_version": 1,
                "job_id": job["job_id"], "attempt_id": job.get("attempt_id"),
                "request_id": job.get("request_id"), "event": event,
                "at": now(), "data": data}
    validate_job_journal(document)
    path = _journal_file(p, job["shot_id"], job["job_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(canon_bytes(document))


def load_job_journal(p, shot_id, job_id):
    """The append-only intent/observation log of one segment job."""
    path = _journal_file(p, shot_id, job_id)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(validate_job_journal(json.loads(line)))
    return records


def _transition(p, job, target, detail=""):
    allowed = TRANSITIONS.get(job["status"], set())
    if target not in allowed:
        raise FilmError(f"Illegal segment job transition "
                        f"{job['status']} -> {target}")
    job["history"].append({"from": job["status"], "to": target,
                           "at": now(), "detail": detail})
    job["status"] = target


# ---------------------------------------------------------------------------
# spec building + capability preflight
# ---------------------------------------------------------------------------

def _cut_entry(timeline, shot_id):
    entries = [e for e in timeline["entries"] if e["shot_id"] == shot_id]
    if not entries:
        raise FilmError(f"{shot_id} has no timeline entry")
    if len(entries) > 1:
        raise FilmError(f"{shot_id} appears in {len(entries)} timeline "
                        "entries; a v1 segment job supports one entry "
                        "per shot")
    return entries[0]


def _pin_record(registry, pin, what):
    _pin(pin, what)
    return _resolve_pin(registry, pin["asset_id"], pin["revision"],
                        pin["content_sha256"])


def _pin_member(p, registry, pin, what):
    """Resolve a pin to its single stored member file (verified bytes)."""
    record = _pin_record(registry, pin, what)
    if len(record["files"]) != 1:
        raise FilmError(f"{what}: a segment input must be a single-file "
                        f"asset, not {record['kind']} "
                        f"{pin['asset_id']} r{pin['revision']}")
    member = record["files"][0]
    path = safe_path(p, member["relative_name"])
    if path.is_symlink() or not path.is_file():
        raise FilmError(f"{what}: stored member is missing: "
                        f"{member['relative_name']}")
    if digest(path) != member["sha256"]:
        raise FilmError(f"{what}: stored member hash changed for "
                        f"{member['relative_name']}")
    return {"pin": dict(pin), "member": member["relative_name"],
            "kind": record["kind"], "sha256": member["sha256"]}


def _control_at(registry, shot_id, frame):
    """The shot's CONTROL_IMAGE revision at a cut-local frame, if pinned."""
    candidates = []
    for asset_id, entry in registry["assets"].items():
        record = entry["revisions"][str(entry["current_revision"])]
        if record["kind"] == "CONTROL_IMAGE" \
                and record["shot_id"] == shot_id \
                and record["frame"] == frame \
                and record["control_role"] in ANCHOR_ROLES:
            candidates.append({"asset_id": asset_id,
                               "revision": entry["current_revision"],
                               "content_sha256": record["content_sha256"]})
    if len(candidates) > 1:
        ids = ", ".join(sorted(c["asset_id"] for c in candidates))
        raise FilmError(f"AMBIGUOUS_ANCHOR: more than one control image is "
                        f"pinned at frame {frame} of {shot_id} ({ids}); "
                        "resolve the duplicate controls first")
    return candidates[0] if candidates else None


def _plan_assets(plan, role):
    return [{k: a[k] for k in ("asset_id", "revision", "content_sha256")}
            for a in plan["assets"] if a["role"] == role]


def build_segment_spec(project, shot_id, start, end, adapter):
    """Resolve one B segment's declared inputs and run capability preflight.

    Missing required inputs stop with NEEDS_MANUAL_WORK; inputs the adapter
    cannot honour stop with CAPABILITY_UNAVAILABLE. Nothing is trimmed, no
    end condition is silently dropped.
    """
    p = Path(project)
    config = require_animation_profile(p)
    if not SHOT_ID.fullmatch(shot_id if type(shot_id) is str else ""):
        raise FilmError("shot_id must be an S001-style identifier")
    shots = read(p / "manifest/shots.json")
    if not any(s["id"] == shot_id for s in shots):
        raise FilmError(f"Unknown shot: {shot_id}")
    timeline = load_animation_timeline(p)
    entry = _cut_entry(timeline, shot_id)
    used_start, used_end = entry["used_source_range"]
    length = used_end - used_start
    fmt = config["format"]
    canvas = {"width": fmt["width"], "height": fmt["height"]}
    plan = load_shot_plan(p, shot_id, length=length, canvas=canvas)
    segment = next((s for s in plan["segments"]
                    if s["path"] == "B" and s["start"] == start
                    and s["end"] == end), None)
    if segment is None:
        raise FilmError(f"SEGMENT_NOT_IN_PLAN: no path-B segment "
                        f"[{start}, {end}) in the stored plan for {shot_id}")
    declared = set(segment["capabilities"])
    caps = adapter.capabilities()
    registry = load_registry(p)

    def missing(what):
        raise FilmError(f"NEEDS_MANUAL_WORK: {what} is required by this "
                        "segment but is not produced/pinned yet")

    def unsupported(what):
        raise FilmError(f"CAPABILITY_UNAVAILABLE: {what}")

    # --- required conditioning inputs (never trimmed to a provider limit)
    masters = [_pin_member(p, registry, pin, "master reference")
               for pin in _plan_assets(plan, "master")]
    if "REFERENCE_IMAGES" in declared and not masters:
        missing("a master reference pin in the shot plan (the segment "
                "declares REFERENCE_IMAGES)")
    start_pin = _control_at(registry, shot_id, start)
    if start_pin is None:
        missing(f"a start anchor control image at frame {start}")
    start_image = _pin_member(p, registry, start_pin, "start anchor")
    wants_end = "START_END_IMAGES" in declared
    end_image = None
    if wants_end:
        if not caps["end_image"]:
            unsupported(f"adapter {caps['adapter_id']} is start-only; a "
                        "plan forcing an end anchor cannot be served and "
                        "the end condition is never silently dropped")
        end_pin = _control_at(registry, shot_id, end)
        if end_pin is None:
            missing(f"an end anchor control image at frame {end}")
        end_image = _pin_member(p, registry, end_pin, "end anchor")
    layouts = [_pin_member(p, registry, pin, "layout guide")
               for pin in _plan_assets(plan, "layout")]
    if "LAYOUT_GUIDE" in declared:
        if caps["layout_guide"] == "NONE":
            unsupported(f"adapter {caps['adapter_id']} accepts no layout "
                        "guide input")
        if not layouts:
            missing("a layout guide pin (segment declares LAYOUT_GUIDE)")
    pose_pins = _plan_assets(plan, "control")
    pose_controls = []
    for asset_id, entry in registry["assets"].items():
        record = entry["revisions"][str(entry["current_revision"])]
        if record["kind"] == "CONTROL_IMAGE" \
                and record["shot_id"] == shot_id \
                and record["control_role"] in {"POSE", "BREAKDOWN"} \
                and start < record["frame"] < end:
            pose_controls.append({"asset_id": asset_id,
                                  "revision": entry["current_revision"],
                                  "content_sha256": record["content_sha256"],
                                  "frame": record["frame"]})
    poses = [_pin_member(p, registry, pin, "pose guide")
             for pin in pose_pins]
    poses += [{"frame": pin["frame"],
               **_pin_member(p, registry,
                             {k: pin[k] for k in
                              ("asset_id", "revision", "content_sha256")},
                             "pose guide")}
              for pin in pose_controls]
    if "POSE_GUIDE" in declared:
        if caps["pose_guide"] == "NONE":
            unsupported(f"adapter {caps['adapter_id']} accepts no pose "
                        "guide input")
        if not poses:
            missing("a pose guide (segment declares POSE_GUIDE)")
    masks = [_pin_member(p, registry, pin, "mask")
             for pin in _plan_assets(plan, "mask")]
    if "MASK_REGION" in declared:
        if not caps["mask_region"]:
            unsupported(f"adapter {caps['adapter_id']} accepts no "
                        "mask/region input")
        if not masks:
            missing("a mask pin (segment declares MASK_REGION)")
    if "ALPHA_OUTPUT" in declared and caps["alpha_output"] != "PRESERVED":
        unsupported(f"adapter {caps['adapter_id']} does not provably "
                    f"preserve alpha ({caps['alpha_output']})")

    # --- capability limits and output envelope
    # Anchors and the mask are their own request inputs; the reference-image
    # limit bounds the conditioning stills a provider will read.
    references = masters + layouts + poses
    limit = caps["reference_images_max"]
    if len(references) > limit:
        unsupported(f"the request carries {len(references)} required "
                    f"reference images but adapter {caps['adapter_id']} "
                    f"accepts at most {limit}; required references are "
                    "never trimmed to fit a provider limit")
    if (caps["native_width"], caps["native_height"]) != \
            (canvas["width"], canvas["height"]):
        unsupported(f"adapter native output {caps['native_width']}x"
                    f"{caps['native_height']} does not match the cut canvas "
                    f"{canvas['width']}x{canvas['height']}; v1 does not "
                    "silently rescale provider output")
    if caps["operation_identity"] not in {"REQUEST_ID", "OPERATION_NAME"}:
        unsupported(f"adapter {caps['adapter_id']} declares no resumable "
                    "operation identity; UNKNOWN fencing would be unsafe")
    if caps["cost_unit"] not in BILLING_UNITS:
        unsupported(f"adapter cost unit {caps['cost_unit']!r} is not a "
                    f"ledger unit {sorted(BILLING_UNITS)}")
    out_fps = fmt["fps"]
    needed = int(Fraction(end - 1 - start) * Fraction(
        caps["native_fps"], out_fps)) + 1
    if not caps["min_returned_frames"] <= needed \
            <= caps["max_returned_frames"]:
        unsupported(f"segment needs {needed} source frames at the "
                    f"adapter's native {caps['native_fps']}fps; declared "
                    f"bounds are {caps['min_returned_frames']}.."
                    f"{caps['max_returned_frames']}")

    inputs = {"start_image": start_image, "end_image": end_image,
              "references": masters, "layout_guide": layouts,
              "pose_guides": poses, "mask": masks}
    plan_sha = digest(safe_path(p, plan_path(shot_id)))
    spec = {"operation": OPERATION, "shot_id": shot_id,
            "segment": {"start": start, "end": end},
            "capabilities": sorted(declared),
            "output": {"width": canvas["width"], "height": canvas["height"],
                       "fps": out_fps},
            "inputs": inputs, "plan_sha256": plan_sha,
            "adapter": {"id": adapter.id,
                        "capabilities_sha256": object_hash(caps)}}
    spec["spec_sha256"] = object_hash(spec)

    def strip(group):
        def pins_only(item):
            return {"pin": item["pin"],
                    **({"frame": item["frame"]} if "frame" in item else {})}
        if type(group) is list:
            return [pins_only(x) for x in group]
        return pins_only(group) if group is not None else None

    job_key = object_hash(
        {"snapshot": {"plan_sha256": plan_sha,
                      "inputs": {k: strip(v) for k, v in inputs.items()}},
         "operation": OPERATION, "range": [start, end],
         "recipe": spec["output"], "runtime": spec["adapter"]})
    return spec, job_key


def _open_job(p, spec, job_key):
    """Return the existing job for this input identity or create PLANNED."""
    job_id = "seg-" + job_key[:20]
    path = _job_file(p, spec["shot_id"], job_id)
    if path.exists():
        return read(path)
    job = {"job_id": job_id, "job_key": job_key, "shot_id": spec["shot_id"],
           "adapter": spec["adapter"]["id"],
           "provider_class": ("FAKE" if spec["adapter"]["id"] == "fake_segment"
                              else "DECLARED"),
           "qualification_state": "UNQUALIFIED", "status": "PLANNED",
           "attempt": 0, "attempt_id": None, "request_id": None,
           "operation_id": None,
           "segment": dict(spec["segment"]),
           "capabilities": spec["capabilities"], "spec": spec,
           "spec_sha256": spec["spec_sha256"],
           "plan_sha256": spec["plan_sha256"], "quote": None,
           "approval": None, "reservation": None,
           "charge_state": "NONE", "result": None, "import": None,
           "failure": None, "history": [], "created_at": now(),
           "updated_at": now()}
    _save_job(p, job)
    return job


def _quote_id(job_key, quote):
    return object_hash({"job_key": job_key, "unit": quote["unit"],
                        "amount": quote["amount"],
                        "detail": quote.get("detail")})


def segment_quote(project, shot_id, start, end, *, adapter_id="fake_segment"):
    """Preflight + quote for one B segment; writes the durable job record.

    The job record exists (status PLANNED) before any submission, and every
    quote call refreshes the stored quote so a provider-side price change is
    visible before approval.
    """
    p = Path(project)
    with project_mutex(p):
        from .segment_fake import make_adapter
        adapter = make_adapter(p, adapter_id)
        spec, job_key = build_segment_spec(p, shot_id, start, end, adapter)
        job = _open_job(p, spec, job_key)
        quote = adapter.quote(spec)
        job["quote"] = {"quote_id": _quote_id(job_key, quote),
                        "unit": quote["unit"], "amount": quote["amount"],
                        "at": now()}
        _save_job(p, job)
        return {"job_id": job["job_id"], "status": job["status"],
                "quote_id": job["quote"]["quote_id"],
                "unit": quote["unit"], "amount": quote["amount"],
                "detail": quote.get("detail"),
                "provider_class": adapter.provider_class,
                "qualification_state": "UNQUALIFIED"}


def _submit_once(p, adapter, job, spec):
    """Journal the intent, persist SUBMITTING, then hand to the provider.

    The attempt and request ids are durably stored before the SUBMIT_INTENT
    record, so even a crash between journal and save can never mint a second
    submission identity for the same input.
    """
    if job["attempt_id"] is None:
        job["attempt"] += 1
        job["attempt_id"] = f"att-{uuid.uuid4().hex[:12]}"
        job["request_id"] = f"req-{uuid.uuid4().hex[:16]}"
        _save_job(p, job)  # durable submission identity before any side effect
    _journal(p, job, "SUBMIT_INTENT",
             {"spec_sha256": spec["spec_sha256"], "quote": job["quote"],
              "reservation": job["reservation"],
              "usage_right": "FAKE_LOCAL_ONLY"
              if adapter.provider_class == "FAKE" else "UNQUALIFIED"})
    _transition(p, job, "SUBMITTING")
    _save_job(p, job)  # durable job + request id before the provider call
    try:
        accepted = adapter.submit(spec, job["request_id"], job["attempt_id"])
    except Exception as exc:
        from .segment_fake import SegmentLostAck, SegmentRejected
        if isinstance(exc, SegmentRejected):
            _journal(p, job, "SUBMIT_REJECTED", {"detail": str(exc)})
            _transition(p, job, "FAILED_CONFIRMED",
                        "provider definitively rejected before acceptance")
            job["failure"] = {"reason": "SUBMISSION_REJECTED",
                              "detail": str(exc), "at": now()}
        elif isinstance(exc, SegmentLostAck):
            _journal(p, job, "SUBMIT_LOST", {"detail": str(exc)})
            _transition(p, job, "UNKNOWN",
                        "submit acknowledgement lost; same identity only")
        else:
            raise
        _save_job(p, job)
        return job
    _journal(p, job, "SUBMIT_ACCEPTED",
             {"operation_id": accepted.get("operation_id")})
    job["operation_id"] = accepted.get("operation_id")
    _transition(p, job, "RUNNING", "provider accepted the request")
    _save_job(p, job)
    return job


def segment_submit(project, shot_id, start, end, *, adapter_id="fake_segment",
                   approver=None, quote_id=None, cap=None, max_retries=0):
    """Approve the current quote, reserve it, then submit — once per input.

    An existing job for the same inputs is resumed, never duplicated: a job
    that is already in flight returns its status, and a fenced
    (SUBMITTING/UNKNOWN) job refuses — reconcile it, never resubmit.
    """
    p = Path(project)
    with project_mutex(p):
        from .segment_fake import make_adapter
        adapter = make_adapter(p, adapter_id)
        spec, job_key = build_segment_spec(p, shot_id, start, end, adapter)
        job = _open_job(p, spec, job_key)
        if job["status"] in FENCED_STATES:
            raise FilmError(
                f"JOB_FENCED: {job['job_id']} is {job['status']}; reconcile "
                "the same job/request identity (segment-reconcile) — a "
                "resubmission with a new id is refused")
        if job["status"] in {"RUNNING", "OUTPUT_PENDING_VERIFY", "VERIFIED",
                             "ARCHIVED"}:
            return {"job_id": job["job_id"], "resumed": True,
                    "status": job["status"], "attempt": job["attempt"],
                    "request_id": job["request_id"],
                    "operation_id": job["operation_id"],
                    "qualification_state": "UNQUALIFIED"}
        if job["status"] in {"FAILED_CONFIRMED", "CANCEL_CONFIRMED",
                             "WAITING_USER"}:
            raise FilmError(
                f"JOB_NEEDS_DECISION: {job['job_id']} is {job['status']}; "
                "continue with an explicit segment-retry or leave it")
        fresh = adapter.quote(spec)
        current = _quote_id(job_key, fresh)
        job["quote"] = {"quote_id": current, "unit": fresh["unit"],
                        "amount": fresh["amount"], "at": now()}
        if quote_id != current:
            _save_job(p, job)
            raise FilmError(
                "QUOTE_CHANGED: the live quote does not match the approved "
                f"id; run segment-quote, review the price and approve the "
                f"new quote id ({current[:16]}...)")
        if type(cap) not in (int, float) or cap < fresh["amount"]:
            _save_job(p, job)
            raise FilmError("APPROVAL_CAP: --cap must declare the approved "
                            "spending ceiling for this unit (>= the quote)")
        _int(max_retries, "max_retries")
        approval = job["approval"]
        if approval is None or approval["quote_id"] != current \
                or approval["approver"] != approver \
                or approval["cap"] != cap \
                or approval["max_retries"] != max_retries:
            job["approval"] = {"approver": approver, "quote_id": current,
                               "cap": cap, "max_retries": max_retries,
                               "at": now()}
        estimate = {"estimate_id": f"segest-{current[:24]}",
                    "billing_unit": fresh["unit"],
                    "worst_case_amount": fresh["amount"] * (1 + max_retries),
                    "max_amount": cap}
        attempt_no = job["attempt"] if job["attempt_id"] \
            else job["attempt"] + 1
        ledger_key = f"{job['job_id']}#a{attempt_no}"
        budget.reserve(p, ledger_key, fresh["amount"], estimate)
        job["reservation"] = {"ledger_key": ledger_key,
                              "amount": fresh["amount"],
                              "unit": fresh["unit"],
                              "estimate_id": estimate["estimate_id"],
                              "at": now(),
                              "note": "never refunded without provider "
                                      "confirmation; a failed or pending "
                                      "reservation stays reserved"}
        job["charge_state"] = "RESERVED"
        if job["status"] == "PLANNED":
            _transition(p, job, "RESERVED", "approval + ledger reservation")
        _submit_once(p, adapter, job, spec)
        return {"job_id": job["job_id"], "resumed": False,
                "status": job["status"], "attempt": job["attempt"],
                "request_id": job["request_id"],
                "operation_id": job["operation_id"],
                "quote_id": current, "reservation": job["reservation"],
                "failure": job["failure"],
                "qualification_state": "UNQUALIFIED"}


def segment_reconcile(project, job_id, *, adapter_id="fake_segment"):
    """Explicit status query on a job's existing identity — the only way to
    resolve a fenced or in-flight job. Never creates a new id."""
    p = Path(project)
    with project_mutex(p):
        from .segment_fake import make_adapter
        adapter = make_adapter(p, adapter_id)
        job = _find_job(p, job_id)
        if job["status"] not in ACTIVE_STATES:
            raise FilmError(f"NOTHING_TO_RECONCILE: {job_id} is "
                            f"{job['status']}; reconcile applies to "
                            "SUBMITTING/UNKNOWN/RUNNING jobs")
        _journal(p, job, "STATUS_QUERY",
                 {"request_id": job["request_id"],
                  "operation_id": job["operation_id"]})
        observed = adapter.status(job["request_id"])
        _journal(p, job, "STATUS_OBSERVED",
                 {k: v for k, v in observed.items() if k != "result"})
        if observed.get("operation_id"):
            job["operation_id"] = observed["operation_id"]
        if observed.get("billed") is not None:
            job["charge_state"] = ("CONFIRMED_BILLED" if observed["billed"]
                                   else "CONFIRMED_UNCHARGED")
        state = observed["state"]
        if state == "NOT_FOUND":
            _transition(p, job, "FAILED_CONFIRMED",
                        "provider confirmed the request was never received")
            job["failure"] = {"reason": "NEVER_RECEIVED", "at": now()}
            job["charge_state"] = "CONFIRMED_UNCHARGED"
        elif state == "RUNNING":
            if job["status"] != "RUNNING":
                _transition(p, job, "RUNNING", "reconciled: still running")
        elif state == "DONE":
            job["result"] = observed["result"]
            _transition(p, job, "OUTPUT_PENDING_VERIFY",
                        "reconciled: output returned, pending verification")
        elif state == "FAILED":
            _transition(p, job, "FAILED_CONFIRMED",
                        "provider confirmed failure")
            job["failure"] = {"reason": "PROVIDER_FAILED",
                              "detail": observed.get("detail"), "at": now()}
        else:
            raise FilmError(f"Unknown provider state: {state}")
        _save_job(p, job)
        return {"job_id": job["job_id"], "status": job["status"],
                "operation_id": job["operation_id"],
                "charge_state": job["charge_state"],
                "qualification_state": "UNQUALIFIED"}


def segment_retry(project, job_id, *, adapter_id="fake_segment", approver=None,
                  quote_id=None):
    """Explicit new attempt on a FAILED_CONFIRMED job — bounded by the
    approved retry cap; re-quotes and re-checks the reservation."""
    p = Path(project)
    with project_mutex(p):
        from .segment_fake import make_adapter
        adapter = make_adapter(p, adapter_id)
        job = _find_job(p, job_id)
        if job["status"] != "FAILED_CONFIRMED":
            raise FilmError(f"RETRY_REQUIRES_FAILURE: {job_id} is "
                            f"{job['status']}; only a confirmed failure may "
                            "be explicitly continued")
        approval = job["approval"]
        if approval is None:
            raise FilmError("RETRY_REQUIRES_APPROVAL")
        allowed = 1 + approval["max_retries"]
        if job["attempt"] + 1 > allowed:
            raise FilmError(f"RETRY_CAP: this job is bounded to "
                            f"{allowed} attempt(s); the cap is a cost "
                            "bound, not a quality signal")
        spec = job["spec"]
        fresh = adapter.quote(spec)
        current = _quote_id(job["job_key"], fresh)
        if quote_id != current:
            raise FilmError("QUOTE_CHANGED: retry requires approving the "
                            "current quote; run segment-quote first")
        job["quote"] = {"quote_id": current, "unit": fresh["unit"],
                        "amount": fresh["amount"], "at": now()}
        if approval["quote_id"] != current:
            if approver is None:
                raise FilmError("RETRY_REQUIRES_APPROVAL: the quote changed; "
                                "an approver must approve the new quote")
            job["approval"] = {**approval, "approver": approver,
                               "quote_id": current, "at": now()}
        estimate = {"estimate_id": f"segest-{current[:24]}",
                    "billing_unit": fresh["unit"],
                    "worst_case_amount": fresh["amount"]
                    * (1 + approval["max_retries"]),
                    "max_amount": approval["cap"]}
        ledger_key = f"{job_id}#a{job['attempt'] + 1}"
        budget.reserve(p, ledger_key, fresh["amount"], estimate)
        job["reservation"] = {"ledger_key": ledger_key,
                              "amount": fresh["amount"],
                              "unit": fresh["unit"],
                              "estimate_id": estimate["estimate_id"],
                              "at": now(),
                              "note": "never refunded without provider "
                                      "confirmation"}
        _journal(p, job, "RETRY_INTENT",
                 {"attempt": job["attempt"] + 1, "approver": approver})
        job["attempt"] += 1
        job["attempt_id"] = f"att-{uuid.uuid4().hex[:12]}"
        job["request_id"] = f"req-{uuid.uuid4().hex[:16]}"
        job["operation_id"] = None
        job["failure"] = None
        # A fresh attempt re-enters SUBMITTING from its confirmed terminal
        # state only through an explicit, journaled user continuation.
        job["status"] = "RESERVED"
        _submit_once(p, adapter, job, spec)
        return {"job_id": job["job_id"], "status": job["status"],
                "attempt": job["attempt"], "request_id": job["request_id"],
                "operation_id": job["operation_id"],
                "qualification_state": "UNQUALIFIED"}


# ---------------------------------------------------------------------------
# returned-clip verification, PTS mapping, staging and commit
# ---------------------------------------------------------------------------

def segment_import(project, job_id, *, adapter_id="fake_segment",
                   source_start=0, source_count=None):
    """Verify a returned clip and stage its frames as cut-local members.

    The declared endpoint rule is honoured (a returned end anchor is dropped
    and recorded); a clip shorter than the owned range fails the job, and a
    longer clip requires an explicit `source_start`/`source_count` used range.
    Source frames map to output-local frames by FLOOR_CONTAINMENT_V1.
    """
    p = Path(project)
    with project_mutex(p):
        require_animation_profile(p)
        job = _find_job(p, job_id)
        if job["status"] == "VERIFIED":
            return {"job_id": job_id, "resumed": True,
                    "status": "VERIFIED", "import": job["import"],
                    "qualification_state": "UNQUALIFIED"}
        if job["status"] != "OUTPUT_PENDING_VERIFY":
            raise FilmError(f"NOT_READY_TO_IMPORT: {job_id} is "
                            f"{job['status']}; reconcile first")
        _journal(p, job, "IMPORT_INTENT", {})
        config = read(p / "project.yaml")
        fmt = config["format"]
        canvas = {"width": fmt["width"], "height": fmt["height"]}
        out_fps = fmt["fps"]
        manifest = job["result"]
        seg = job["segment"]
        length = seg["end"] - seg["start"]
        source_fps = manifest["fps"]
        frames_dir = safe_path(p, manifest["frames_dir"])
        names = manifest["frame_names"]

        def fail(reason, detail):
            _transition(p, job, "FAILED_CONFIRMED", detail)
            job["failure"] = {"reason": reason, "detail": detail,
                              "at": now()}
            _save_job(p, job)
            raise FilmError(f"{reason}: {detail}")

        if len(names) != manifest["frame_count"]:
            fail("RESULT_MANIFEST_MISMATCH",
                 "the provider's returned frame list does not match its "
                 "declared count")
        files = []
        for index, name in enumerate(names):
            member = safe_path(p, f"{manifest['frames_dir']}/{name}")
            if member.is_symlink() or not member.is_file():
                fail("RESULT_MEMBER_MISSING",
                     f"returned member {name} is missing or a symlink")
            if name != f"f{index:06d}.png":
                fail("RESULT_ORDER_MISMATCH",
                     f"returned member {index} is named {name}; the "
                     "provider's frame order is its index")
            data = member.read_bytes()
            try:
                size, _mode = _decode_png(data, name)
            except FilmError as exc:
                fail("RESULT_NOT_PNG", f"member {name}: {exc}")
            if size != (manifest["width"], manifest["height"]):
                fail("RESULT_SIZE_MISMATCH",
                     f"member {name} is {size[0]}x{size[1]}, manifest "
                     f"declares {manifest['width']}x{manifest['height']}")
            if size != (canvas["width"], canvas["height"]):
                fail("RESULT_SIZE_MISMATCH",
                     f"member {name} does not match the cut canvas "
                     f"{canvas['width']}x{canvas['height']}")
            files.append({"path": member, "data": data,
                          "sha256": hashlib.sha256(data).hexdigest()})
        rule = manifest["endpoint_rule"]
        if rule not in {ENDPOINT_INCLUDED, ENDPOINT_START_ONLY}:
            fail("ENDPOINT_RULE_UNKNOWN", f"returned rule {rule}")
        if "START_END_IMAGES" in job["capabilities"] \
                and rule != ENDPOINT_INCLUDED:
            fail("END_ANCHOR_NOT_HONOURED",
                 "the plan forced an end anchor but the returned clip "
                 "declares START_ONLY — the end condition was dropped")
        returned = len(files)
        usable = returned - 1 if rule == ENDPOINT_INCLUDED else returned
        needed = int(Fraction(length - 1)
                     * Fraction(source_fps, out_fps)) + 1
        if usable < needed:
            fail("SHORT_RETURNED_CLIP",
                 f"provider returned {usable} usable source frames but the "
                 f"segment needs {needed} at {source_fps}fps to cover "
                 f"[{seg['start']}, {seg['end']}); no speed-up or "
                 "last-frame hold is applied")
        if source_count is None:
            if usable > needed:
                raise FilmError(
                    "CLIP_LONGER_THAN_NEEDED: the provider returned "
                    f"{usable} usable frames but {needed} cover the range; "
                    "declare the used range with --source-start/"
                    "--source-count")
            source_count = usable
        _int(source_start, "source_start")
        _int(source_count, "source_count", 1)
        if source_count < needed or source_start + source_count > usable:
            raise FilmError(
                "USED_RANGE_INVALID: the declared used range "
                f"[{source_start}, {source_start + source_count}) must "
                f"cover the {needed} needed source frames inside the "
                f"{usable} usable ones; the job stays "
                "OUTPUT_PENDING_VERIFY until a valid range is declared")
        mapping = map_source_frames(source_fps, out_fps, length,
                                    source_start, source_count)
        timeline = load_animation_timeline(p)
        entry = _cut_entry(timeline, job["shot_id"])
        member_base = entry["used_source_range"][0] + seg["start"]
        stage_rel = f"animation/shots/{job['shot_id']}/segment_members/" \
            f"{job_id}"
        members = []
        for local, source_index in enumerate(mapping["frames"]):
            member_index = member_base + local
            chosen = files[source_index]
            rel = f"{stage_rel}/m{member_index:06d}.png"
            _store_member(p, rel, chosen["data"], chosen["sha256"])
            members.append({"member_index": member_index,
                            "source_index": source_index,
                            "sha256": chosen["sha256"], "member": rel})
        job["import"] = {
            "member_first": member_base, "member_last": member_base + length - 1,
            "stage": stage_rel, "members": members,
            "normalization": {
                "rule": mapping["rule"], "source_fps": source_fps,
                "output_fps": out_fps, "returned_frames": returned,
                "usable_frames": usable, "source_start": source_start,
                "source_count": source_count, "endpoint_rule": rule,
                "dropped_end_frame": (returned - 1
                                      if rule == ENDPOINT_INCLUDED
                                      else None),
                "first_pts": manifest.get("first_pts"),
                "mapping": mapping["frames"],
                "review": "normalized playback still requires human review"},
            "imported_at": now()}
        _transition(p, job, "VERIFIED",
                    "returned clip verified and staged for this segment")
        _journal(p, job, "IMPORT_OBSERVED",
                 {"members": len(members), "endpoint_rule": rule})
        _save_job(p, job)
        return {"job_id": job_id, "status": "VERIFIED",
                "member_range": [job["import"]["member_first"],
                                 job["import"]["member_last"] + 1],
                "normalization": job["import"]["normalization"],
                "state": "DRAFT", "qualification_state": "UNQUALIFIED"}


def _verified_jobs(p, shot_id):
    for path in _list_job_files(p, shot_id):
        job = read(path)
        if job["status"] == "VERIFIED" and job["import"]:
            yield job


def commit_segment_sequence(project, shot_id, *, asset_id=None, note=""):
    """Assemble verified B-segment imports into a DRAFT FRAME_SEQUENCE.

    Every member the shot needs — `[0, used_end + after)` — must be covered
    exactly once by a VERIFIED job built against the current plan; gaps and
    overlaps refuse. The result flows through the ordinary resolver,
    normalize/validate and draft Preview paths unchanged.
    """
    p = Path(project)
    with project_mutex(p):
        config = require_animation_profile(p)
        if not SHOT_ID.fullmatch(shot_id if type(shot_id) is str else ""):
            raise FilmError("shot_id must be an S001-style identifier")
        shots = read(p / "manifest/shots.json")
        if not any(s["id"] == shot_id for s in shots):
            raise FilmError(f"Unknown shot: {shot_id}")
        if asset_id is not None and not ASSET_ID.fullmatch(asset_id):
            raise FilmError("asset_id must be an A0001-style identifier")
        timeline = load_animation_timeline(p)
        entry = _cut_entry(timeline, shot_id)
        used_start, used_end = entry["used_source_range"]
        length = used_end - used_start
        fmt = config["format"]
        plan = load_shot_plan(p, shot_id, length=length,
                              canvas={"width": fmt["width"],
                                      "height": fmt["height"]})
        if any(s["path"] != "B" for s in plan["segments"]):
            raise FilmError("MIXED_PATH_UNSUPPORTED: segment jobs assemble "
                            "only all-B segments; other paths produce their "
                            "own sequences")
        plan_sha = digest(safe_path(p, plan_path(shot_id)))
        jobs = list(_verified_jobs(p, shot_id))
        coverage = {}
        dependencies = []
        seen = set()
        for seg in plan["segments"]:
            candidates = [j for j in jobs
                          if j["segment"] == {"start": seg["start"],
                                              "end": seg["end"]}
                          and j["plan_sha256"] == plan_sha]
            if not candidates:
                raise FilmError(
                    "SEGMENT_NOT_PRODUCED: no verified import covers "
                    f"[{seg['start']}, {seg['end']}); stale jobs from an "
                    "older plan revision do not count")
            job = candidates[0]
            for member in job["import"]["members"]:
                index = member["member_index"]
                if index in coverage:
                    raise FilmError(
                        f"SEGMENT_OVERLAP: member {index} is produced by "
                        "two segment jobs; shared anchors belong to the "
                        "next segment once")
                coverage[index] = (job, member)
            for group in job["spec"]["inputs"].values():
                items = group if type(group) is list else [group]
                for item in items:
                    if item is None:
                        continue
                    pin = item["pin"]
                    key = (pin["asset_id"], pin["revision"],
                           pin["content_sha256"])
                    if key not in seen:
                        seen.add(key)
                        dependencies.append(dict(pin))
        needed = range(0, used_end + entry["unused_handles"]["after"])
        missing = [m for m in needed if m not in coverage]
        if missing:
            raise FilmError("MISSING_FRAMES: no verified segment job "
                            f"produced members {missing}")
        members = []
        for index in needed:
            job, member = coverage[index]
            path = safe_path(p, member["member"])
            if path.is_symlink() or not path.is_file():
                raise FilmError(f"DRAFT_MEMBER_MISSING: {member['member']}")
            if digest(path) != member["sha256"]:
                raise FilmError(f"DRAFT_MEMBER_CHANGED: {member['member']}")
            members.append({"source": path, "sha256": member["sha256"],
                            "byte_length": path.stat().st_size,
                            "source_name": f"{job['job_id']}/{index:06d}"})
        provenance = {"type": "SEGMENT_GENERATION",
                      "via": "b_path_adapter",
                      "adapters": sorted({j["adapter"] for j in jobs}),
                      "jobs": sorted(j["job_id"] for j in jobs),
                      "provider_class": "FAKE",
                      "source_maps": [
                          {"job_id": j["job_id"], "segment": j["segment"],
                           "normalization": j["import"]["normalization"]}
                          for j in jobs],
                      "assembled_at": now(), "note": note.strip()}
        result = _register_frame_sequence(
            p, shot_id, members,
            {"width": fmt["width"], "height": fmt["height"]}, provenance,
            dependencies=dependencies, asset_id=asset_id)
        result["qualification_state"] = "UNQUALIFIED"
        result["note"] = ("Assembled from fake-provider segment jobs; "
                          "no artwork approval, review or qualification")
        return result


def segment_jobs(project, shot_id=None):
    """List segment job records for inspection; no state changes."""
    p = Path(project)
    require_animation_profile(p)
    jobs = []
    for path in _list_job_files(p, shot_id):
        job = read(path)
        jobs.append({"job_id": job["job_id"], "shot_id": job["shot_id"],
                     "segment": job["segment"], "status": job["status"],
                     "attempt": job["attempt"],
                     "request_id": job["request_id"],
                     "operation_id": job["operation_id"],
                     "quote": job["quote"],
                     "charge_state": job["charge_state"],
                     "adapter": job["adapter"],
                     "provider_class": job["provider_class"],
                     "qualification_state": job["qualification_state"]})
    return {"jobs": jobs, "note": "FAKE provider jobs only; no real "
            "provider call or paid generation exists in this node"}


# ---------------------------------------------------------------------------
# conditioning controls for B segments
# ---------------------------------------------------------------------------

def import_segment_control(project, shot_id, file, *, role, frame,
                           references=None, asset_id=None, note=""):
    """Import conditioning art (a pose/keypose control) for a B segment.

    Unlike a packet-produced draft member these images are *inputs* to a
    segment job: they register a CONTROL_IMAGE revision but never fill a
    produced frame. A control at `frame == length` is the end anchor that
    conditions the last B segment's landing pose; it is accepted only when a
    stored plan actually forces it (START_END_IMAGES ending at `length`).
    """
    p = Path(project)
    role = role.strip().upper() if type(role) is str else ""
    with project_mutex(p):
        config = require_animation_profile(p)
        if role not in CONTROL_IMPORT_ROLES:
            raise FilmError(f"role must be one of "
                            f"{sorted(CONTROL_IMPORT_ROLES)}")
        _int(frame, "frame")
        if not SHOT_ID.fullmatch(shot_id if type(shot_id) is str else ""):
            raise FilmError("shot_id must be an S001-style identifier")
        shots = read(p / "manifest/shots.json")
        if not any(s["id"] == shot_id for s in shots):
            raise FilmError(f"Unknown shot: {shot_id}")
        if asset_id is not None and not ASSET_ID.fullmatch(asset_id):
            raise FilmError("asset_id must be an A0001-style identifier")
        timeline = load_animation_timeline(p)
        entry = _cut_entry(timeline, shot_id)
        used_start, used_end = entry["used_source_range"]
        length = used_end - used_start
        fmt = config["format"]
        canvas = {"width": fmt["width"], "height": fmt["height"]}
        plan = load_shot_plan(p, shot_id, length=length, canvas=canvas)
        b_segments = [s for s in plan["segments"] if s["path"] == "B"]
        if not b_segments:
            raise FilmError("NO_B_SEGMENT: segment controls condition a "
                            "path-B segment; the stored plan has none")
        if not 0 <= frame < length:
            end_anchor = frame == length and any(
                s["end"] == length and "START_END_IMAGES" in s["capabilities"]
                for s in b_segments)
            if not end_anchor:
                raise FilmError(
                    f"FRAME_OUT_OF_RANGE: control frame {frame} is outside "
                    f"[0, {length}); only a B segment forcing an end anchor "
                    "may take a control at the cut's end frame")
        provided = list(references or [])
        for index, pin in enumerate(provided):
            _pin(pin, f"references[{index}]")
        registry = load_registry(p)
        for index, pin in enumerate(provided):
            try:
                _resolve_pin(registry, pin["asset_id"], pin["revision"],
                             pin["content_sha256"])
            except FilmError:
                raise FilmError(
                    f"REFERENCE_UNKNOWN: references[{index}] "
                    f"{pin['asset_id']} r{pin['revision']} does not resolve "
                    "against the registry")
        data = _read_source(file)
        size, _mode = _decode_png(data, Path(file).name
                                  if not isinstance(file, (bytes, bytearray))
                                  else "<bytes>")
        if size != (canvas["width"], canvas["height"]):
            raise FilmError(
                f"CANVAS_MISMATCH: imported image is {size[0]}x{size[1]}, "
                f"the cut canvas is {canvas['width']}x{canvas['height']}")
        result = _commit_control_image(
            p, registry, shot_id, file, data, role, frame, provided,
            canvas, asset_id=asset_id, note=note)
        save_registry(p, registry)
        return {"shot_id": shot_id, "role": role, "frame": frame,
                "asset_pin": {"asset_id": result["asset_id"],
                              "revision": result["revision"],
                              "content_sha256": result["content_sha256"]},
                "state": "DRAFT", "qualification_state": "UNQUALIFIED",
                "note": "Conditioning input for a B segment; no approval, "
                        "review or produced member"}
