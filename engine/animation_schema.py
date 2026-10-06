"""FRAME_ANIMATION_V1 document contracts (ADR 0001 schema specification).

New-mode JSON documents carry `document_type` and `schema_version`, are stored
as CANON_JSON_V1 bytes plus one trailing LF, and reject unknown or future
versions. This module owns the canonical encoding and the animation_timeline
validator; it creates no assets, approvals or runtime state.
"""
from pathlib import Path
import json
import re

from .core import FilmError

CANON_INT_MIN = -(2**63)
CANON_INT_MAX = 2**63 - 1

# New-mode document types implemented so far and their only readable versions.
DOCUMENT_SCHEMAS = {
    "animation_timeline": 1,
    "animation_timeline_derived": 1,
    "animation_asset_registry": 1,
    "animation_review": 1,
    "animation_locks": 1,
    "animation_waves": 1,
    "route_decision": 1,
    "animation_build": 2,
    "pack_index": 1,
    "storage_archive": 1,
    "animation_shot_plan": 1,
    "composite_recipe": 1,
    "encode_recipe": 1,
    "capability_evidence": 1,
    "animation_work_packet": 1,
    "animation_draft_frames": 1,
    "job_journal": 1,
    "archive_seal": 1,
    "execution_plan": 1,
    "subscription_entitlement": 1,
    "subscription_packet": 1,
    "subscription_result": 1,
    "capability_measurement": 1,
    "render_manifest": 1,
    "w00_pilot": 1,
    "approver_delegation": 1,
    "w00_spend_approval": 1,
    "delivery_approval": 1,
}

TIMELINE_FIELDS = {"document_type", "schema_version", "target_frames", "entries"}
ENTRY_FIELDS = {"instance_id", "shot_id", "sequence_revision", "used_source_range",
                "unused_handles", "transition_out"}
TRANSITION_FIELDS = {"id", "type", "to_instance", "overlap_frames", "curve"}
DERIVED_FIELDS = {"document_type", "schema_version", "target_frames", "entries",
                  "transitions"}
SHOT_ID = re.compile(r"S[0-9]{3,5}")


def _check_canon(value):
    if value is None or isinstance(value, bool):
        return
    if type(value) is int:
        if not CANON_INT_MIN <= value <= CANON_INT_MAX:
            raise FilmError("CANON_JSON_V1 integer out of range")
        return
    if type(value) is str:
        return
    if type(value) is list:
        for item in value:
            _check_canon(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise FilmError("CANON_JSON_V1 keys must be strings")
            _check_canon(item)
        return
    raise FilmError(f"CANON_JSON_V1 does not allow {type(value).__name__}")


def canon_bytes(document):
    """Sorted-key, separator-free UTF-8 document plus the required trailing LF."""
    _check_canon(document)
    return (json.dumps(document, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def write_canon(path, document):
    from .core import atomic_text
    atomic_text(path, canon_bytes(document).decode("utf-8"))


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FilmError(f"Duplicate key: {key}")
        result[key] = value
    return result


def read_canon(path):
    """Parse a stored document and require byte-exact canonical form."""
    path = Path(path)
    if not path.exists():
        raise FilmError(f"Missing file: {path}")
    raw = path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise FilmError(f"Invalid JSON document: {path}") from e
    _check_canon(document)
    if raw != canon_bytes(document):
        raise FilmError(f"Non-canonical JSON document: {path}")
    return document


def check_document(document, document_type=None):
    """Version gate: only the declared document_type and schema_version read."""
    if type(document) is not dict:
        raise FilmError("Animation document must be an object")
    kind = document.get("document_type")
    if kind not in DOCUMENT_SCHEMAS:
        raise FilmError(f"Unknown document_type: {kind}")
    if document_type is not None and kind != document_type:
        raise FilmError(f"Expected {document_type}, found {kind}")
    version = document.get("schema_version")
    if type(version) is not int or version != DOCUMENT_SCHEMAS[kind]:
        raise FilmError(f"Unsupported {kind} schema_version: {version}")
    return kind


def _int(value, what, minimum=0):
    if type(value) is not int or value < minimum:
        raise FilmError(f"{what} must be an integer >= {minimum}")
    return value


def _entry_span(entry, index, total):
    missing = ENTRY_FIELDS - entry.keys()
    if missing:
        raise FilmError(f"Timeline entry {index} missing fields: {sorted(missing)}")
    if type(entry["instance_id"]) is not str or not entry["instance_id"]:
        raise FilmError("instance_id must be a non-empty string")
    if not SHOT_ID.fullmatch(entry["shot_id"] if type(entry["shot_id"]) is str else ""):
        raise FilmError("shot_id must be an S001-style identifier")
    revision = entry["sequence_revision"]
    if revision is not None and (type(revision) is not int or revision < 0):
        raise FilmError("sequence_revision must be null or a non-negative integer")
    rng = entry["used_source_range"]
    if type(rng) is not list or len(rng) != 2:
        raise FilmError("used_source_range must be a [start, end) pair")
    start = _int(rng[0], "used_source_range start")
    end = _int(rng[1], "used_source_range end")
    if end <= start:
        raise FilmError("Timeline entries must use at least one source frame")
    handles = entry["unused_handles"]
    if type(handles) is not dict or set(handles.keys()) - {"before", "after"}:
        raise FilmError("unused_handles must hold only before/after")
    _int(handles.get("before"), "unused_handles.before")
    _int(handles.get("after"), "unused_handles.after")
    transition = entry["transition_out"]
    overlap = 0
    if index == total - 1:
        if transition is not None:
            raise FilmError("The last timeline entry must have a null transition_out")
    else:
        if type(transition) is not dict or set(transition.keys()) - TRANSITION_FIELDS:
            raise FilmError("Malformed transition_out")
        missing = {"id", "type", "to_instance", "overlap_frames"} - transition.keys()
        if missing:
            raise FilmError(f"Transition missing fields: {sorted(missing)}")
        if type(transition["id"]) is not str or not transition["id"]:
            raise FilmError("Transition id must be a non-empty string")
        overlap = _int(transition["overlap_frames"], "overlap_frames")
        kind = transition["type"]
        if kind == "HARD_CUT":
            if overlap != 0 or "curve" in transition:
                raise FilmError("HARD_CUT requires overlap_frames 0 and no curve")
        elif kind == "CROSSFADE":
            if overlap < 1 or transition.get("curve") != "LINEAR_INTERIOR_V1":
                raise FilmError("CROSSFADE requires overlap >= 1 and LINEAR_INTERIOR_V1")
        else:
            raise FilmError(f"Unknown transition type: {kind}")
    return (start, end), overlap, transition


def validate_animation_timeline(document, output_frames=None):
    """Structural contract of `timeline/edit.json` (schema section 3).

    Returns the derived output layout: per-entry output ranges and transition
    ranges on the global frame clock.
    """
    check_document(document, "animation_timeline")
    if set(document.keys()) - TIMELINE_FIELDS:
        raise FilmError("Unknown animation_timeline fields")
    target = _int(document.get("target_frames"), "target_frames", minimum=1)
    entries = document.get("entries")
    if type(entries) is not list or not entries:
        raise FilmError("animation_timeline entries must be a non-empty list")
    instance_ids, transition_ids, lengths, overlaps, transitions = set(), set(), [], [], []
    spans = []
    for index, entry in enumerate(entries):
        if type(entry) is not dict or set(entry.keys()) - ENTRY_FIELDS:
            raise FilmError(f"Timeline entry {index} has unknown fields or is not an object")
        rng, overlap, transition = _entry_span(entry, index, len(entries))
        if entry["instance_id"] in instance_ids:
            raise FilmError("instance_id must be unique within the timeline")
        instance_ids.add(entry["instance_id"])
        if transition is not None:
            if transition["id"] in transition_ids:
                raise FilmError("Transition id must be unique within the timeline")
            transition_ids.add(transition["id"])
            follower = entries[index + 1]
            if transition["to_instance"] != follower.get("instance_id"):
                raise FilmError("transition_out.to_instance must name the next entry")
        lengths.append(rng[1] - rng[0])
        overlaps.append(overlap)
        transitions.append(transition)
    for index in range(len(entries) - 1):
        overlap = overlaps[index]
        if overlap < 0 or overlap >= min(lengths[index], lengths[index + 1]):
            raise FilmError("Transition overlap must stay inside both neighboring entries")
    for index in range(1, len(entries) - 1):
        if overlaps[index - 1] + overlaps[index] > lengths[index]:
            raise FilmError("Adjacent overlaps exceed an entry's used range")
    produced = sum(lengths) - sum(overlaps)
    if produced != target:
        raise FilmError("target_frames does not match used lengths minus overlaps")
    if output_frames is not None and target != output_frames:
        raise FilmError("Timeline target_frames does not match project output_frames")
    cursor = 0
    layout = []
    for index, entry in enumerate(entries):
        span = [cursor, cursor + lengths[index]]
        layout.append({"instance_id": entry["instance_id"], "shot_id": entry["shot_id"],
                       "output_range": span})
        cursor = span[1] - overlaps[index]
    transition_ranges = []
    for index, transition in enumerate(transitions):
        if transition is None:
            continue
        end = layout[index]["output_range"][1]
        transition_ranges.append({"id": transition["id"], "type": transition["type"],
                                  "from_instance": layout[index]["instance_id"],
                                  "to_instance": transition["to_instance"],
                                  "overlap_frames": transition["overlap_frames"],
                                  "output_range": [end - transition["overlap_frames"], end]})
    # A global frame may be covered by at most two entries (BLOCK-TRIPLE-OVERLAP).
    for left in range(len(layout)):
        for right in range(left + 2, len(layout)):
            a, b = layout[left]["output_range"], layout[right]["output_range"]
            if a[0] < b[1] and b[0] < a[1]:
                raise FilmError("A global frame is covered by three or more entries")
    return {"output_frames": produced, "entries": layout, "transitions": transition_ranges}


def derive_timeline_view(document):
    """Recalculated `timeline/derived.json` view; never written back as truth."""
    layout = validate_animation_timeline(document)
    return {"document_type": "animation_timeline_derived", "schema_version": 1,
            "target_frames": layout["output_frames"],
            "entries": layout["entries"], "transitions": layout["transitions"]}


ENCODE_DRIVERS = {"FFMPEG", "NVIDIA_NATIVE", "VIDEOTOOLBOX_NATIVE",
                  "GSTREAMER", "QUALIFIED_SERVICE"}
REGISTRY_STATES = {"DOCUMENTED_ONLY", "QUALIFIED_FOR_SCOPE", "STALE",
                   "UNAVAILABLE"}
ENCODE_RECIPE_FIELDS = {"document_type", "schema_version", "driver",
                        "codec_engine", "container", "pixel_format",
                        "timebase", "rate_control", "color", "mux"}
CAPABILITY_FIELDS = {"document_type", "schema_version", "evidence_id",
                     "driver", "adapter_digest", "account_binding",
                     "credential_epoch", "environment", "operation", "scope",
                     "caps", "route", "transport", "fixture", "observed_at",
                     "entitlement_basis", "allowance", "expiry",
                     "registry_state", "qualification", "reason"}


def validate_encode_recipe(document):
    """Structural contract of `encode_recipe` 1 (schema section 14).

    The fields are exactly driver, codec engine, container, pixel format,
    timebase, rate control, color and mux — the DeliveryProfile is a shared
    reference, never smuggled into the recipe as an extra field.
    """
    check_document(document, "encode_recipe")
    if set(document.keys()) != ENCODE_RECIPE_FIELDS:
        raise FilmError("encode_recipe must hold exactly the contracted "
                        "fields: driver, codec engine, container, pixel "
                        "format, timebase, rate control, color, mux")
    if document["driver"] not in ENCODE_DRIVERS:
        raise FilmError(f"Unknown encode driver: {document['driver']}")
    if type(document["codec_engine"]) is not str \
            or not document["codec_engine"]:
        raise FilmError("codec_engine must name the engine actually used")
    if document["container"] != "mp4":
        raise FilmError("MV_H264_AAC_V1 delivery uses an mp4 container")
    if type(document["pixel_format"]) is not str \
            or not document["pixel_format"]:
        raise FilmError("pixel_format must be a non-empty string")
    timebase = document["timebase"]
    if type(timebase) is not dict or set(timebase.keys()) != {"num", "den"}:
        raise FilmError("timebase must be a {num, den} rational")
    _int(timebase["num"], "timebase.num", minimum=1)
    _int(timebase["den"], "timebase.den", minimum=1)
    if type(document["rate_control"]) is not dict \
            or not document["rate_control"].get("mode"):
        raise FilmError("rate_control must name the driver-specific mode")
    if type(document["color"]) is not dict or not document["color"]:
        raise FilmError("color must record the recipe-fixed colour policy")
    if type(document["mux"]) is not dict or not document["mux"]:
        raise FilmError("mux must record the mux contract")
    return document


def validate_capability_evidence(document):
    """Structural contract of `capability_evidence` 1 (schema section 15).

    Required: evidence_id, adapter/worker digest, secret-free account
    binding, credential_epoch, session/device/OS/driver environment,
    operation, scope, caps, route/transport, fixture digests, observed
    time, entitlement basis, per-currency allowance, expiry/revalidation
    conditions — plus the registry state which may only be a documented
    state, never an invented program enum.
    """
    check_document(document, "capability_evidence")
    if set(document.keys()) != CAPABILITY_FIELDS:
        raise FilmError("capability_evidence must hold exactly the "
                        "contracted fields")
    if type(document["evidence_id"]) is not str \
            or not document["evidence_id"]:
        raise FilmError("evidence_id must be a non-empty string")
    if type(document["adapter_digest"]) is not str \
            or not document["adapter_digest"]:
        raise FilmError("adapter_digest is required")
    if document["driver"] not in ENCODE_DRIVERS:
        raise FilmError(f"Unknown encode driver: {document['driver']}")
    binding = document["account_binding"]
    if binding is not None and type(binding) is not str:
        raise FilmError("account_binding must be secret-free (digest or "
                        "null, never an email or token)")
    _int(document["credential_epoch"], "credential_epoch")
    if type(document["environment"]) is not dict:
        raise FilmError("environment must record session/device/OS/driver")
    if type(document["operation"]) is not str or not document["operation"]:
        raise FilmError("operation must name what was actually probed")
    if type(document["allowance"]) is not dict:
        raise FilmError("allowance must keep currencies separate")
    if document["registry_state"] not in REGISTRY_STATES:
        raise FilmError(f"Unknown registry_state: "
                        f"{document['registry_state']}")
    if type(document["observed_at"]) is not str \
            or not document["observed_at"]:
        raise FilmError("observed_at is required")
    return document


MEASUREMENT_FIELDS = {"document_type", "schema_version", "measurement_id",
                      "evidence_id", "evidence_digest", "input_digest",
                      "quality_digest", "scope", "cold", "warm",
                      "stage_timeline", "shared_edge", "peaks", "transfer",
                      "cache", "manual", "usage", "samples", "observed_at"}


def validate_capability_measurement(document):
    """Structural contract of `capability_measurement` 1 (ANIM-019
    CAP-MEASURE, execution design 7.2.1/8.1).

    A measurement is only meaningful bound: evidence_id and
    evidence_digest pin the exact CapabilityEvidence the timed runs used
    (a record that drifts from that digest is unusable), input_digest and
    quality_digest pin what ran, and scope records the measured scope —
    never a wider claim. Every section is a required container, but which
    pieces must be filled before an estimate is usable is decided by the
    registry: a partial record stays UNKNOWN, never an implied number.
    """
    check_document(document, "capability_measurement")
    if set(document.keys()) != MEASUREMENT_FIELDS:
        raise FilmError("capability_measurement must hold exactly the "
                        "contracted fields")
    for key in ("measurement_id", "evidence_id"):
        if type(document[key]) is not str or not document[key]:
            raise FilmError(f"{key} must be a non-empty string")
    for key in ("evidence_digest", "input_digest", "quality_digest"):
        value = document[key]
        if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}",
                                                      value):
            raise FilmError(f"{key} must be a sha256 hex digest")
    if type(document["scope"]) is not dict:
        raise FilmError("scope must record the measured scope")
    for key in ("cold", "warm", "shared_edge", "peaks", "transfer",
                "cache", "manual", "usage", "samples"):
        if type(document[key]) is not dict:
            raise FilmError(f"{key} must be an object")
    if type(document["stage_timeline"]) is not list:
        raise FilmError("stage_timeline must be a list of {stage, ms}")
    if type(document["observed_at"]) is not str \
            or not document["observed_at"]:
        raise FilmError("observed_at is required")
    return document


def require_animation_profile(project):
    """Read project.yaml and require the explicit FRAME_ANIMATION_V1 profile.

    Absent or LEGACY_MV projects keep the ms path and cannot create new-mode
    documents; the profile is set only by the explicit animation-init command.
    """
    from .core import production_profile, read
    from .schema import ANIMATION_PROFILE
    p = Path(project)
    config = read(p / "project.yaml")
    if production_profile(config) != ANIMATION_PROFILE:
        raise FilmError("This project is LEGACY_MV; animation assets require an explicit animation-init conversion")
    return config


def load_animation_timeline(project):
    """Read and validate `timeline/edit.json` against the project declaration."""
    from .core import read, safe_path
    p = Path(project)
    config = read(p / "project.yaml")
    animation = config.get("animation")
    if type(animation) is not dict:
        raise FilmError("Project declares no animation section")
    document = read_canon(safe_path(p, animation.get("timeline", "timeline/edit.json")))
    output = animation.get("output_frames")
    if type(output) is not int:
        raise FilmError("animation.output_frames must be an integer")
    validate_animation_timeline(document, output)
    return document
