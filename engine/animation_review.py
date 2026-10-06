"""FRAME_ANIMATION_V1 review records (ANIM-006; design 10, schema 7.2).

Every human review of a cut, a transition or the integrated film is one
CANON_JSON_V1 document appended to `production/approvals.jsonl` — an
append-only log where each line is a canonical `animation_review` record.
A record authorizes only the exact digests it binds:

- `CUT` binds the shot instance, the adopted sequence's content digest
  (ordered member bytes plus exposure/composite recipe refs), the used
  source range and unused handles, the MotionPlan digest, the selected
  asset revision and the shared visual-intent digest;
- `TRANSITION` binds both adopted sequences, the output join and the
  transition recipe (type, curve, overlap, output range);
- `FINAL_FILM` binds the sealed build manifest, the actual deliverable
  MP4, the delivery frame-sequence root, the edit digest and the
  audio/lyrics-review/font digests.

Any change to a bound input makes the record stale. Stale or missing
reviews never authorize a Final candidate, are never revived from cache
and are never inherited from the legacy approval files — this module reads
only `production/approvals.jsonl`. Imported (`generated=False`) members get
no shortcut either: the record itself is what counts. These records are
review-protocol data; they are not evidence that a real human reviewed
artwork, and they confer no production qualification or release authority.
"""
from pathlib import Path
import hashlib
import json
import re

from .animation_assets import resolve_shot_sequence
from .animation_schema import (canon_bytes, check_document,
                               load_animation_timeline,
                               require_animation_profile)
from .core import FilmError, atomic_text, digest, now, read, safe_path
from .exposure import schedule_digest
from .transitions import audit_timeline

REVIEW_TYPE = "animation_review"
REVIEW_SCHEMA = 1
APPROVALS_PATH = "production/approvals.jsonl"

SCOPES = ("CUT", "TRANSITION", "FINAL_FILM")
DECISIONS = {"APPROVED", "FIX_REQUIRED"}
LIMITATION_DISPOSITIONS = {"INTENTIONAL", "ACCEPTED_LIMITATION"}

# The documented review methods (design 10.1): whole-cut normal-speed
# playback, slow/frame review of problem spans, adjacent-cut normal-speed
# playback, whole-film playback, issue-frame review and technical checks.
METHODS = {"CUT_FULL_SPEED_PLAYBACK", "SLOW_SPAN_REVIEW",
           "TRANSITION_FULL_SPEED_PLAYBACK", "FULL_SPEED_WHOLE_FILM",
           "ISSUE_FRAME_REVIEW", "TECHNICAL_VALIDATION"}
REQUIRED_METHODS = {"CUT": {"CUT_FULL_SPEED_PLAYBACK"},
                    "TRANSITION": {"TRANSITION_FULL_SPEED_PLAYBACK"},
                    "FINAL_FILM": {"FULL_SPEED_WHOLE_FILM",
                                   "TECHNICAL_VALIDATION"}}

COMMON_FIELDS = {"document_type", "schema_version", "review_id", "scope",
                 "reviewer", "methods", "decision", "unresolved_major_issues",
                 "accepted_limitations", "reviewed_at", "binding_sha256"}

BINDING_FIELDS = {
    "CUT": ("instance_id", "shot_id", "sequence_digest", "asset_id",
            "revision", "used_source_range", "unused_handles",
            "motion_plan_sha256", "exposure_digest", "intent_sha256"),
    "TRANSITION": ("transition_id", "type", "curve", "overlap_frames",
                   "output_range", "from_instance", "to_instance",
                   "from_sequence_digest", "to_sequence_digest",
                   "from_revision", "to_revision"),
    "FINAL_FILM": ("build_id", "build_manifest_sha256", "deliverable",
                   "deliverable_sha256", "frame_sequence_root", "edit_digest",
                   "audio_sha256", "lyrics_review_sha256", "font_sha256"),
}

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
KEY_FIELDS = {"CUT": "instance_id", "TRANSITION": "transition_id",
              "FINAL_FILM": "build_id"}


def _sha_or_none(value, field, allow_none=False):
    if value is None and allow_none:
        return
    if type(value) is not str or not SHA_RE.fullmatch(value):
        raise FilmError(f"review {field} must be a lowercase sha256")


def _issue_list(value, field, dispositions, require_reason):
    if type(value) is not list:
        raise FilmError(f"review {field} must be a list")
    for item in value:
        if type(item) is not dict \
                or set(item.keys()) - {"disposition", "note", "reason", "scope"}:
            raise FilmError(f"Malformed {field} entry")
        if item.get("disposition") not in dispositions:
            raise FilmError(f"{field} disposition must be one of "
                            f"{sorted(dispositions)}")
        if type(item.get("note")) is not str or not item["note"].strip():
            raise FilmError(f"{field} entries need a non-empty note")
        if require_reason:
            if type(item.get("reason")) is not str or not item["reason"].strip():
                raise FilmError(f"{field} entries need an explicit reason")
            scope = item.get("scope")
            range_scope = type(scope) is list and len(scope) == 2 \
                and all(type(v) is int and v >= 0 for v in scope)
            if not range_scope and not (type(scope) is str and scope.strip()):
                raise FilmError(f"{field} entries need a scope: a label or a "
                                "[start, end] frame range")


def binding_digest(record):
    """sha256 over exactly the scope's bound fields."""
    scope = record["scope"]
    return hashlib.sha256(canon_bytes(
        {k: record[k] for k in BINDING_FIELDS[scope]})).hexdigest()


def validate_review(record):
    """Structural contract of one `animation_review` record (schema 7.2)."""
    check_document(record, REVIEW_TYPE)
    scope = record.get("scope")
    if scope not in SCOPES:
        raise FilmError(f"Unknown review scope: {scope}")
    expected = COMMON_FIELDS | set(BINDING_FIELDS[scope])
    if type(record) is not dict or set(record.keys()) != expected:
        raise FilmError(f"{scope} review must hold exactly its schema fields "
                        f"(missing {sorted(expected - record.keys())}, extra "
                        f"{sorted(set(record.keys()) - expected)})")
    if type(record["review_id"]) is not str or not record["review_id"]:
        raise FilmError("review_id must be a non-empty string")
    if type(record["reviewer"]) is not str or not record["reviewer"].strip():
        raise FilmError("A review needs a named reviewer")
    methods = record["methods"]
    if type(methods) is not list or not methods \
            or any(type(m) is not str for m in methods):
        raise FilmError("methods must be a non-empty list of method names")
    unknown = set(methods) - METHODS
    if unknown:
        raise FilmError(f"Unknown review methods: {sorted(unknown)}")
    missing = REQUIRED_METHODS[scope] - set(methods)
    if missing:
        raise FilmError(f"{scope} review is missing required methods: "
                        f"{sorted(missing)}")
    if record["decision"] not in DECISIONS:
        raise FilmError(f"decision must be one of {sorted(DECISIONS)}")
    _issue_list(record["unresolved_major_issues"], "unresolved_major_issues",
                {"FIX_REQUIRED"}, require_reason=False)
    _issue_list(record["accepted_limitations"], "accepted_limitations",
                LIMITATION_DISPOSITIONS, require_reason=True)
    if type(record["reviewed_at"]) is not str or not record["reviewed_at"]:
        raise FilmError("reviewed_at must be a timestamp string")
    fields = BINDING_FIELDS[scope]
    for field in fields:
        value = record[field]
        if field in {"motion_plan_sha256", "exposure_digest", "font_sha256",
                     "lyrics_review_sha256"}:
            _sha_or_none(value, field, allow_none=True)
        elif field.endswith("_sha256") or field.endswith("_digest") \
                or field.endswith("_root"):
            _sha_or_none(value, field)
        elif field in {"revision", "from_revision", "to_revision"}:
            if type(value) is not int or value < 1:
                raise FilmError(f"review {field} must be a positive integer")
        elif field == "overlap_frames":
            if type(value) is not int or value < 0:
                raise FilmError("review overlap_frames must be an integer >= 0")
        elif field in {"used_source_range", "output_range"}:
            if type(value) is not list or len(value) != 2 \
                    or any(type(v) is not int or v < 0 for v in value) \
                    or value[1] < value[0]:
                raise FilmError(f"review {field} must be a [start, end] range")
        elif field == "unused_handles":
            if type(value) is not dict \
                    or any(type(value.get(k)) is not int or value[k] < 0
                           for k in ("before", "after")):
                raise FilmError("review unused_handles must hold before/after")
        elif field == "type" and scope == "TRANSITION":
            if value not in {"HARD_CUT", "CROSSFADE"}:
                raise FilmError(f"Unknown transition type: {value}")
        elif field == "curve":
            if value is not None and value != "LINEAR_INTERIOR_V1":
                raise FilmError(f"Unknown transition curve: {value}")
        elif type(value) is not str or not value:
            raise FilmError(f"review {field} must be a non-empty string")
    if record["binding_sha256"] != binding_digest(record):
        raise FilmError("binding_sha256 does not match the bound fields")
    return record


def load_reviews(project):
    """All review records in append order; a malformed line fails the log."""
    p = Path(project)
    path = p / APPROVALS_PATH
    if not path.exists():
        return []
    lines = path.read_bytes().split(b"\n")
    if lines[-1] != b"":
        raise FilmError("approvals.jsonl must end with a single LF")
    records = []
    for index, line in enumerate(lines[:-1]):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as e:
            raise FilmError(f"approvals.jsonl line {index + 1} is not JSON") from e
        if line + b"\n" != canon_bytes(record):
            raise FilmError(f"approvals.jsonl line {index + 1} is not canonical")
        validate_review(record)
        records.append(record)
    return records


def append_review(project, record):
    """Validate and append one review record to the append-only log."""
    p = Path(project)
    require_animation_profile(p)
    validate_review(record)
    records = load_reviews(p)
    if any(r["review_id"] == record["review_id"] for r in records):
        raise FilmError(f"review_id {record['review_id']} is already recorded")
    raw = b"".join(canon_bytes(r) for r in [*records, record])
    atomic_text(p / APPROVALS_PATH, raw.decode("utf-8"))
    return record


def _next_review_id(records):
    used = {r["review_id"] for r in records}
    number = 1
    while f"RV{number:04d}" in used:
        number += 1
    return f"RV{number:04d}"


def _issue(issues):
    return [dict(item) for item in issues or []]


def _record(scope, fields, reviewer, methods, decision,
            unresolved_major_issues, accepted_limitations, review_id):
    record = {"document_type": REVIEW_TYPE, "schema_version": REVIEW_SCHEMA,
              "review_id": review_id, "scope": scope,
              **fields, "reviewer": reviewer, "methods": list(methods),
              "decision": decision,
              "unresolved_major_issues": _issue(unresolved_major_issues),
              "accepted_limitations": _issue(accepted_limitations),
              "reviewed_at": now()}
    record["binding_sha256"] = binding_digest(record)
    return record


def sequence_content_digest(record):
    """Ordered member bytes plus exposure/composite recipe refs (design 8.2).

    The sequence's content identity is the ordered (frame_index, member
    sha256) list of one pinned revision plus its canvas and recipe refs —
    never a path, a job state or a review marker.
    """
    frames = [{"frame_index": f["frame_index"], "sha256": f["sha256"]}
              for f in sorted(record["files"], key=lambda f: f["frame_index"])]
    return hashlib.sha256(canon_bytes(
        {"kind": record["kind"], "asset_id": record["asset_id"],
         "revision": record["revision"], "canvas": record["canvas"],
         "frames": frames,
         "exposure_recipe_ref": record.get("exposure_recipe_ref"),
         "composite_recipe_ref": record.get("composite_recipe_ref")}
    )).hexdigest()


def edit_digest(document):
    """Content digest of the `animation_timeline` edit document."""
    return hashlib.sha256(canon_bytes(document)).hexdigest()


def shared_intent_digest(p):
    """Shared visual-intent digest; per-shot media stays out (design 10.2).

    Same inputs as the legacy visual context fingerprint — brief, lyric
    source, bibles, characters/locations, output format — but hashed as a
    CANON_JSON_V1 document, the new profile's hash layer.
    """
    p = Path(p)
    config = read(p / "project.yaml")
    paths = {"input/brief.md", "input/lyrics.txt", "bible/story.md",
             "bible/style_bible.yaml", "bible/characters.yaml",
             "bible/locations.yaml", "bible/directing.yaml"}
    for folder in ("characters", "locations"):
        paths.update(str(f.relative_to(p)) for f in (p / folder).rglob("*")
                     if f.is_file())
    files = {}
    for relative in sorted(paths):
        path = safe_path(p, relative)
        files[relative] = digest(path) if path.is_file() else None
    return hashlib.sha256(canon_bytes(
        {"format": config["format"], "files": files})).hexdigest()


def _motion_plan_sha256(p, shot_id):
    plan = p / "animation/shots" / shot_id / "plan.json"
    return digest(plan) if plan.is_file() else None


def _exposure_digest(schedules):
    if schedules is None:
        return None
    if type(schedules) is not list:
        raise FilmError("exposure schedules must be a list of schedules")
    return hashlib.sha256(canon_bytes(
        {"schedules": [schedule_digest(s) for s in schedules]})).hexdigest()


def bound_targets(p, exposure=None, strict=True):
    """The current CUT/TRANSITION bound fields for the whole edit.

    Every timeline entry must resolve to its pinned sequence — a review can
    only bind real content. `exposure` maps an instance_id to the drawing
    track schedules used for that cut (default: the identity ones map).
    With strict=False an unresolvable entry yields an {"unresolved": reason}
    placeholder instead of raising, so scope/status reporting can inspect a
    partially produced timeline; reviews still only ever bind real content.
    """
    p = Path(p)
    require_animation_profile(p)
    document = load_animation_timeline(p)
    audit = audit_timeline(document)
    entries = {e["instance_id"]: e for e in document["entries"]}
    intent = shared_intent_digest(p)
    cuts, sequences = {}, {}
    for row in audit["entries"]:
        entry = entries[row["instance_id"]]
        try:
            resolved = resolve_shot_sequence(
                p, entry["shot_id"], entry["used_source_range"],
                entry["unused_handles"], entry["sequence_revision"])
        except FilmError as exc:
            if strict:
                raise
            cuts[row["instance_id"]] = {
                "instance_id": row["instance_id"], "shot_id": entry["shot_id"],
                "unresolved": str(exc)}
            continue
        record = resolved["record"]
        seq = {"digest": sequence_content_digest(record),
               "revision": resolved["revision"]}
        sequences[row["instance_id"]] = seq
        cuts[row["instance_id"]] = {
            "instance_id": row["instance_id"], "shot_id": entry["shot_id"],
            "sequence_digest": seq["digest"],
            "asset_id": resolved["asset_id"],
            "revision": resolved["revision"],
            "used_source_range": list(entry["used_source_range"]),
            "unused_handles": dict(entry["unused_handles"]),
            "motion_plan_sha256": _motion_plan_sha256(p, entry["shot_id"]),
            "exposure_digest": _exposure_digest(
                (exposure or {}).get(row["instance_id"])),
            "intent_sha256": intent}
    doc_entries = {t["id"]: t for e in document["entries"]
                   if e.get("transition_out") for t in [e["transition_out"]]}
    transitions = {}
    for transition in audit["transitions"]:
        declared = doc_entries[transition["id"]]
        if transition["from_instance"] not in sequences \
                or transition["to_instance"] not in sequences:
            transitions[transition["id"]] = {
                "transition_id": transition["id"],
                "from_instance": transition["from_instance"],
                "to_instance": transition["to_instance"],
                "unresolved": "an endpoint cut is unresolved"}
            continue
        outgoing = sequences[transition["from_instance"]]
        incoming = sequences[transition["to_instance"]]
        transitions[transition["id"]] = {
            "transition_id": transition["id"], "type": transition["type"],
            "curve": declared.get("curve"),
            "overlap_frames": transition["overlap_frames"],
            "output_range": list(transition["output_range"]),
            "from_instance": transition["from_instance"],
            "to_instance": transition["to_instance"],
            "from_sequence_digest": outgoing["digest"],
            "to_sequence_digest": incoming["digest"],
            "from_revision": outgoing["revision"],
            "to_revision": incoming["revision"]}
    return {"cuts": cuts, "transitions": transitions,
            "edit_digest": edit_digest(document)}





def _approves(record):
    return (record["decision"] == "APPROVED"
            and not record["unresolved_major_issues"])


def review_status(project, exposure=None, strict=True):
    """Per-target state: CURRENT, CHANGES_REQUIRED, STALE or UNREVIEWED.

    A target is STALE when records exist for it but none binds the current
    digests — a stale record is evidence of an old review, never an
    approval. UNREVIEWED means no record names the target at all; a record
    that binds the current digests without approving (FIX_REQUIRED or open
    issues) reports CHANGES_REQUIRED, which blocks just as hard. With
    strict=False unresolvable entries report UNRESOLVED instead of raising.
    """
    p = Path(project)
    targets = bound_targets(p, exposure, strict=strict)
    records = load_reviews(p)
    status = {}
    for scope, key in (("cuts", "CUT"), ("transitions", "TRANSITION")):
        for target, fields in targets[scope].items():
            candidates = [r for r in records
                          if r["scope"] == key
                          and r[KEY_FIELDS[key]] == target]
            if "unresolved" in fields:
                status[target] = {"scope": key, "binding_sha256": None,
                                  "state": "UNRESOLVED", "review_id": None,
                                  "records": len(candidates),
                                  "reason": fields["unresolved"]}
                continue
            bound = hashlib.sha256(canon_bytes(fields)).hexdigest()
            matching = [r for r in candidates
                        if r["binding_sha256"] == bound]
            current = [r for r in matching if _approves(r)]
            status[target] = {
                "scope": key, "binding_sha256": bound,
                "state": ("CURRENT" if current
                          else "CHANGES_REQUIRED" if matching
                          else "STALE" if candidates else "UNREVIEWED"),
                "review_id": current[-1]["review_id"] if current else None,
                "records": len(candidates)}
    return {"targets": status, "edit_digest": targets["edit_digest"],
            "records": len(records)}


def require_current_reviews(project, exposure=None):
    """The review ids authorizing the current edit, or a blocking error.

    Every cut and every transition needs a current APPROVED record; a
    missing member, an unreviewed target or a stale binding each refuse —
    no fallback, no revival (BLOCK-UNREVIEWED / BLOCK-STALE-APPROVAL).
    """
    status = review_status(project, exposure)
    blocked = [f"{row['scope']} {target}: {row['state']}"
               for target, row in status["targets"].items()
               if row["state"] != "CURRENT"]
    if blocked:
        raise FilmError(
            "Final candidate requires a current review for every cut and "
            "transition; unreviewed or stale bindings block ("
            + "; ".join(blocked) + ")")
    return {"cuts": {t: r["review_id"] for t, r in status["targets"].items()
                     if r["scope"] == "CUT"},
            "transitions": {t: r["review_id"]
                            for t, r in status["targets"].items()
                            if r["scope"] == "TRANSITION"},
            "edit_digest": status["edit_digest"]}


def record_cut_review(project, instance_id, *, reviewer, methods,
                      decision="APPROVED", unresolved_major_issues=None,
                      accepted_limitations=None, exposure=None):
    """Append a CUT review bound to the instance's current adopted content."""
    p = Path(project)
    cuts = bound_targets(p, exposure, strict=False)["cuts"]
    if instance_id not in cuts:
        raise FilmError(f"No timeline entry {instance_id}")
    if "unresolved" in cuts[instance_id]:
        raise FilmError(f"{instance_id} has no resolvable adopted sequence: "
                        f"{cuts[instance_id]['unresolved']}")
    from .animation_locks import wave_scope_gate
    wave_scope_gate(p, [cuts[instance_id]["shot_id"]])
    record = _record("CUT", cuts[instance_id], reviewer, methods, decision,
                     unresolved_major_issues, accepted_limitations,
                     _next_review_id(load_reviews(p)))
    return append_review(p, record)


def record_transition_review(project, transition_id, *, reviewer, methods,
                             decision="APPROVED", unresolved_major_issues=None,
                             accepted_limitations=None, exposure=None):
    """Append a TRANSITION review bound to both pinned sequences + recipe."""
    p = Path(project)
    targets = bound_targets(p, exposure, strict=False)
    transitions = targets["transitions"]
    if transition_id not in transitions:
        raise FilmError(f"No transition {transition_id}")
    if "unresolved" in transitions[transition_id]:
        raise FilmError(f"{transition_id} has no resolvable endpoint sequence: "
                        f"{transitions[transition_id]['unresolved']}")
    from .animation_locks import wave_scope_gate
    endpoint = transitions[transition_id]
    wave_scope_gate(p, [targets["cuts"][endpoint["from_instance"]]["shot_id"],
                        targets["cuts"][endpoint["to_instance"]]["shot_id"]])
    record = _record("TRANSITION", transitions[transition_id], reviewer,
                     methods, decision, unresolved_major_issues,
                     accepted_limitations,
                     _next_review_id(load_reviews(p)))
    return append_review(p, record)


def record_film_review(project, build_id, *, reviewer, methods,
                       deliverable="MASTER_SUBBED.mp4", decision="APPROVED",
                       unresolved_major_issues=None,
                       accepted_limitations=None):
    """Append a FINAL_FILM review bound to a sealed Build 2 manifest.

    Reads the immutable build inputs — it never writes into the sealed
    build directory. Binding the deliverable sha256, frame-sequence root,
    manifest digest, edit digest and the audio/lyrics/font digests makes
    this record stale the moment any of them is replaced.
    """
    p = Path(project)
    require_animation_profile(p)
    folder = p / "builds" / build_id
    record_path = folder / "build.json"
    if not record_path.is_file():
        raise FilmError(f"No build {build_id}")
    build = read(record_path)
    if build.get("document_type") != "animation_build" \
            or build.get("schema_version") != 2 \
            or build.get("status") != "COMPLETE":
        raise FilmError("FINAL_FILM review binds a COMPLETE Build 2 record")
    target = folder / deliverable
    if not target.is_file():
        raise FilmError(f"Build {build_id} has no deliverable {deliverable}")
    fields = {
        "build_id": build_id,
        "build_manifest_sha256": digest(record_path),
        "deliverable": deliverable,
        "deliverable_sha256": digest(target),
        "frame_sequence_root":
            build["sequences"]["subbed_sequence_root"],
        "edit_digest": build["edit_digest"],
        "audio_sha256": build["audio"]["sha256"],
        "lyrics_review_sha256":
            build["lyrics"].get("lyrics_review_sha256"),
        "font_sha256": build["subtitles"]["font"].get("sha256")}
    record = _record("FINAL_FILM", fields, reviewer, methods, decision,
                     unresolved_major_issues, accepted_limitations,
                     _next_review_id(load_reviews(p)))
    return append_review(p, record)


def film_review_status(project, build_id):
    """The current FINAL_FILM approval for a build, if one exists."""
    records = [r for r in load_reviews(project)
               if r["scope"] == "FINAL_FILM" and r["build_id"] == build_id]
    if not records:
        return {"state": "UNREVIEWED", "review_id": None}
    folder = Path(project) / "builds" / build_id
    manifest = folder / "build.json"
    current = []
    if manifest.is_file():
        manifest_sha = digest(manifest)
        for r in records:
            if r["build_manifest_sha256"] == manifest_sha and _approves(r):
                fields = {k: r[k] for k in BINDING_FIELDS["FINAL_FILM"]}
                deliverable = folder / r["deliverable"]
                if deliverable.is_file() \
                        and digest(deliverable) == r["deliverable_sha256"] \
                        and hashlib.sha256(canon_bytes(fields)).hexdigest() \
                        == r["binding_sha256"]:
                    current.append(r)
    return {"state": "CURRENT" if current else "STALE",
            "review_id": current[-1]["review_id"] if current else None,
            "records": len(records)}
