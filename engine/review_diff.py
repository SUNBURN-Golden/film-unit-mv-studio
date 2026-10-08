"""film-review-diff — bind a review finding to the cut, frames and render.

A finding names one adopted cut (`instance_id` / `shot_id`), a half-open
local frame range, the problem type, and the sequence content digest of
the render it was seen on. A later import is a different render: the
before/after comparison reuses ANIM-020 `plan_rebuild` for the impact
closure, and it keeps two answers apart —

- whether *this finding* is resolved (the cited frames changed, and an
  independent reviewer or the director said so on the new digest);
- whether a full-film playback is still required (any new render digest
  needs one; a cut PASS does not supply it).

A PASS is an acknowledgement whose `artifact_sha256` is the digest it
was recorded against. Nothing in this module copies that acknowledgement
onto another digest, another cut, or a FINAL_FILM review. Author
self-check, independent review and director approval are different
roles: a self-check cannot resolve a finding or pass a cut, and an
independent review cannot record the director's cut PASS.

LEGACY_MV projects are refused by `require_animation_profile` and this
module never writes lyrics, cues, audio, manifest locks or sealed
builds. Records are protocol fixtures only: qualification UNQUALIFIED,
acceptance PENDING, release NOT_AUTHORIZED. A cut PASS appended through
`record_cut_review` binds the new digest and is still not artwork
approval or a final-film approval.
"""
from pathlib import Path
import hashlib
import json
import re

from .animation_assets import load_registry, resolve_shot_sequence
from .animation_review import (METHODS, record_cut_review, review_status,
                               sequence_content_digest)
from .animation_schema import (canon_bytes, load_animation_timeline,
                               require_animation_profile)
from .core import FilmError, atomic_text, now, project_mutex, safe_path
from .render_provenance import plan_rebuild

LOG_PATH = "production/review_diff.jsonl"
SCHEMA = 1
FINDING_TYPE = "review_finding"
REVISION_TYPE = "review_revision"
ACK_TYPE = "review_acknowledgement"

PROBLEM_TYPES = ("CONTACT", "OCCLUSION", "ENDPOINT", "FLICKER",
                 "MOTION", "APPEARANCE", "SUBTITLE", "TECHNICAL")
DISPOSITIONS = ("FIX_REQUIRED", "INTENTIONAL", "ACCEPTED_LIMITATION")
WAIVERS = {"INTENTIONAL", "ACCEPTED_LIMITATION"}
ROLES = ("AUTHOR_SELF_CHECK", "INDEPENDENT_REVIEW", "DIRECTOR_APPROVAL")
DECISIONS = ("CHECKED", "FINDING_RESOLVED", "CUT_PASS")
CUT_METHOD = "CUT_FULL_SPEED_PLAYBACK"

FACETS = {"qualification_state": "UNQUALIFIED",
          "acceptance_state": "PENDING",
          "release_state": "NOT_AUTHORIZED"}

BINDING_KEYS = ("artifact_kind", "artifact_sha256", "asset_id",
                "frame_range", "instance_id", "member_sha256",
                "problem_type", "sequence_revision", "shot_id")

FINDING_FIELDS = {"document_type", "schema_version", "finding_id",
                  "problem_type", "disposition", "note", "reason",
                  "role", "reviewer", "recorded_at",
                  "instance_id", "shot_id", "frame_range",
                  "artifact_kind", "artifact_sha256",
                  "sequence_revision", "asset_id", "member_sha256",
                  "binding_sha256"}
REVISION_FIELDS = {"document_type", "schema_version", "revision_id",
                   "finding_id", "instance_id", "shot_id",
                   "before_sha256", "after_sha256",
                   "before_revision", "after_revision", "frame_range",
                   "changed_member_indices", "finding_frames_changed",
                   "change_class", "closure", "stale", "kept",
                   "pass_inherited", "finding_resolved",
                   "full_playback_required", "role", "reviewer", "note",
                   "recorded_at"}
ACK_FIELDS = {"document_type", "schema_version", "ack_id", "finding_id",
              "instance_id", "shot_id", "role", "reviewer", "decision",
              "methods", "note", "artifact_sha256", "sequence_revision",
              "resolves_finding", "grants_cut_pass",
              "grants_final_approval", "pass_inherited", "recorded_at"}
CLOSURE_FIELDS = {"affected_instances", "compose_ranges", "dirty_frames",
                  "encode_roles", "halo_members", "needed_members",
                  "stale_transitions", "unchanged"}

_SHA = re.compile(r"^[0-9a-f]{64}$")


def _sha256(material):
    return hashlib.sha256(canon_bytes(material)).hexdigest()


def _nonempty(value, message):
    if type(value) is not str or not value.strip():
        raise FilmError(message)
    return value.strip()


def _frame_range(value, used):
    if type(value) is not list or len(value) != 2 \
            or any(type(v) is not int or isinstance(v, bool) for v in value) \
            or value[1] <= value[0]:
        raise FilmError("프레임 범위는 [start, end) 정수 구간이어야 합니다")
    if value[0] < used[0] or value[1] > used[1]:
        raise FilmError(
            f"프레임 범위는 컷의 사용 구간 [{used[0]}, {used[1]}) 안에 "
            "있어야 합니다")
    return [value[0], value[1]]


def _members(record):
    return {f["frame_index"]: f["sha256"] for f in record["files"]}


def _entry(project, instance_id):
    document = load_animation_timeline(project)
    entry = next((e for e in document["entries"]
                  if e["instance_id"] == instance_id), None)
    if entry is None:
        raise FilmError(f"타임라인에 {instance_id} 컷이 없습니다")
    return entry


def _artifact(project, entry):
    """The adopted sequence the cut review would bind right now."""
    resolved = resolve_shot_sequence(
        project, entry["shot_id"], entry["used_source_range"],
        entry["unused_handles"], entry["sequence_revision"])
    record = resolved["record"]
    return {"artifact_sha256": sequence_content_digest(record),
            "sequence_revision": resolved["revision"],
            "asset_id": resolved["asset_id"],
            "members": _members(record),
            "shot_id": entry["shot_id"],
            "instance_id": entry["instance_id"],
            "used_source_range": list(entry["used_source_range"]),
            "record": record}


def _stored_revision(project, asset_id, revision):
    registry = load_registry(project)
    asset = registry["assets"].get(asset_id)
    record = None if asset is None else asset["revisions"].get(str(revision))
    if record is None:
        raise FilmError(f"자산 {asset_id} r{revision}이 레지스트리에 없습니다")
    return record


def _binding(fields):
    return _sha256({k: fields[k] for k in BINDING_KEYS})


def _read_log(project):
    path = safe_path(project, LOG_PATH)
    if not path.exists():
        return []
    lines = path.read_bytes().split(b"\n")
    if lines[-1] != b"":
        raise FilmError("review_diff.jsonl must end with a single LF")
    records = []
    for index, line in enumerate(lines[:-1]):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FilmError(
                f"review_diff.jsonl line {index + 1} is not JSON") from exc
        if line + b"\n" != canon_bytes(record):
            raise FilmError(
                f"review_diff.jsonl line {index + 1} is not canonical")
        _validate(record)
        records.append(record)
    return records


def load_log(project):
    """Every review-diff record in append order."""
    p = Path(project)
    require_animation_profile(p)
    return _read_log(p)


def _own_id(record):
    if record["document_type"] == FINDING_TYPE:
        return record["finding_id"]
    if record["document_type"] == REVISION_TYPE:
        return record["revision_id"]
    return record["ack_id"]


def _append(project, record):
    _validate(record)
    with project_mutex(project):
        records = _read_log(project)
        own = _own_id(record)
        if any(_own_id(existing) == own for existing in records):
            raise FilmError(f"{own} is already recorded")
        raw = b"".join(canon_bytes(r) for r in [*records, record])
        atomic_text(safe_path(project, LOG_PATH), raw.decode("utf-8"))
    return record


def _next_id(records, prefix, field):
    used = {r[field] for r in records if field in r}
    number = 1
    while f"{prefix}{number:04d}" in used:
        number += 1
    return f"{prefix}{number:04d}"


def _check_closure(value):
    if type(value) is not dict or set(value) != CLOSURE_FIELDS:
        raise FilmError("revision closure must carry the impact fields")
    if type(value["unchanged"]) is not bool:
        raise FilmError("closure.unchanged must be a boolean")
    for key in ("affected_instances", "dirty_frames", "encode_roles",
                "stale_transitions"):
        if type(value[key]) is not list:
            raise FilmError(f"closure.{key} must be a list")
    if type(value["compose_ranges"]) is not list:
        raise FilmError("closure.compose_ranges must be a list")
    for key in ("halo_members", "needed_members"):
        if type(value[key]) is not dict:
            raise FilmError(f"closure.{key} must be an object")


def _validate(record):
    if type(record) is not dict:
        raise FilmError("review-diff record must be an object")
    kind = record.get("document_type")
    if record.get("schema_version") != SCHEMA:
        raise FilmError("review-diff schema_version must be 1")
    if kind == FINDING_TYPE:
        if set(record) != FINDING_FIELDS:
            raise FilmError("review_finding fields do not match the schema")
        if record["problem_type"] not in PROBLEM_TYPES:
            raise FilmError(f"unknown problem type {record['problem_type']}")
        if record["disposition"] not in DISPOSITIONS:
            raise FilmError("unknown disposition")
        if record["role"] not in ROLES:
            raise FilmError("unknown review role")
        if record["artifact_kind"] != "SEQUENCE":
            raise FilmError("finding artifact_kind must be SEQUENCE")
        if not _SHA.fullmatch(record["artifact_sha256"] or ""):
            raise FilmError("artifact_sha256 must be a lowercase sha256")
        if record["binding_sha256"] != _binding(record):
            raise FilmError("binding_sha256 does not match the finding")
        if record["disposition"] in WAIVERS and record["problem_type"] == "TECHNICAL":
            raise FilmError("기술 실패는 표현상 수용으로 면제할 수 없습니다")
        if record["disposition"] in WAIVERS and record["role"] == "AUTHOR_SELF_CHECK":
            raise FilmError("작성자 자기 확인은 문제를 면제하거나 승인하지 않습니다")
        return record
    if kind == REVISION_TYPE:
        if set(record) != REVISION_FIELDS:
            raise FilmError("review_revision fields do not match the schema")
        _check_closure(record["closure"])
        if record["pass_inherited"] is not False \
                or record["finding_resolved"] is not False \
                or record["full_playback_required"] is not True:
            raise FilmError(
                "a revision records the closure only — it cannot inherit "
                "a PASS or resolve the finding")
        if record["change_class"] not in {"PICTURE", "SEQUENCE"}:
            raise FilmError("revision change_class must be PICTURE or SEQUENCE")
        return record
    if kind == ACK_TYPE:
        if set(record) != ACK_FIELDS:
            raise FilmError("review_acknowledgement fields do not match the schema")
        if record["role"] not in ROLES or record["decision"] not in DECISIONS:
            raise FilmError("unknown acknowledgement role or decision")
        if record["pass_inherited"] is not False \
                or record["grants_final_approval"] is not False:
            raise FilmError(
                "an acknowledgement cannot inherit a PASS or grant "
                "final-film approval")
        if record["resolves_finding"] is not (record["decision"] == "FINDING_RESOLVED"):
            raise FilmError("resolves_finding does not match the decision")
        if record["grants_cut_pass"] is not (record["decision"] == "CUT_PASS"):
            raise FilmError("grants_cut_pass does not match the decision")
        if not _SHA.fullmatch(record["artifact_sha256"] or ""):
            raise FilmError("acknowledgement artifact_sha256 must be a sha256")
        return record
    raise FilmError(f"unknown review-diff document {kind}")


def _findings(records):
    return [r for r in records if r["document_type"] == FINDING_TYPE]


def _revisions(records, finding_id):
    return [r for r in records
            if r["document_type"] == REVISION_TYPE
            and r["finding_id"] == finding_id]


def _acks(records, finding_id):
    return [r for r in records
            if r["document_type"] == ACK_TYPE and r["finding_id"] == finding_id]


def _require_finding(records, finding_id):
    found = next((r for r in _findings(records) if r["finding_id"] == finding_id),
                 None)
    if found is None:
        raise FilmError(f"지적을 찾을 수 없습니다: {finding_id}")
    return found


def open_finding(project, instance_id, frame_range, problem_type, *,
                 reviewer, role, note, disposition="FIX_REQUIRED",
                 reason=None, artifact_sha256=None):
    """Bind a new finding to the cut's current adopted sequence.

    `artifact_sha256`, when given, must already be that sequence's content
    digest — a hash from another revision is refused rather than stored.
    """
    p = Path(project)
    require_animation_profile(p)
    if problem_type not in PROBLEM_TYPES:
        raise FilmError("문제 유형은 " + ", ".join(PROBLEM_TYPES) + " 중 하나여야 합니다")
    if disposition not in DISPOSITIONS:
        raise FilmError("처분은 " + ", ".join(DISPOSITIONS) + " 중 하나여야 합니다")
    if role not in ROLES:
        raise FilmError("역할은 " + ", ".join(ROLES) + " 중 하나여야 합니다")
    if disposition in WAIVERS and problem_type == "TECHNICAL":
        raise FilmError("기술 실패는 표현상 수용으로 면제할 수 없습니다")
    if disposition in WAIVERS and role == "AUTHOR_SELF_CHECK":
        raise FilmError("작성자 자기 확인은 문제를 면제하거나 승인하지 않습니다")
    if disposition in WAIVERS:
        reason = _nonempty(reason, "면제 이유가 필요합니다")
    else:
        reason = ""
    reviewer = _nonempty(reviewer, "검토자 이름이 필요합니다")
    note = _nonempty(note, "지적 내용이 필요합니다")
    entry = _entry(p, instance_id)
    current = _artifact(p, entry)
    span = _frame_range(list(frame_range), current["used_source_range"])
    if artifact_sha256 is not None \
            and artifact_sha256 != current["artifact_sha256"]:
        raise FilmError("지정한 artifact hash가 현재 채택 시퀀스와 다릅니다")
    members = [{"frame_index": index,
                "sha256": current["members"][index]}
               for index in range(span[0], span[1])]
    record = {"document_type": FINDING_TYPE, "schema_version": SCHEMA,
              "problem_type": problem_type, "disposition": disposition,
              "note": note, "reason": reason, "role": role,
              "reviewer": reviewer, "recorded_at": now(),
              "instance_id": entry["instance_id"],
              "shot_id": entry["shot_id"], "frame_range": span,
              "artifact_kind": "SEQUENCE",
              "artifact_sha256": current["artifact_sha256"],
              "sequence_revision": current["sequence_revision"],
              "asset_id": current["asset_id"], "member_sha256": members}
    record["binding_sha256"] = _binding(record)
    records = _read_log(p)
    if any(r["binding_sha256"] == record["binding_sha256"]
           for r in _findings(records)):
        raise FilmError("같은 컷·프레임·해시·문제 유형의 지적이 이미 있습니다")
    record["finding_id"] = _next_id(records, "RF", "finding_id")
    return _append(p, record)


def _changed(before, after):
    """Member indices whose bytes differ and still exist on the new render."""
    return sorted(index for index in set(before) & set(after)
                  if before[index] != after[index])


def _impact(project, instance_id, changed, whole):
    if not changed and not whole:
        change_class = "NONE"
        plan = plan_rebuild(project, [{"class": "REVIEW_MEMO"}])
    elif whole:
        change_class = "SEQUENCE"
        plan = plan_rebuild(project, [{"class": "SEQUENCE",
                                       "instance_id": instance_id}])
    else:
        change_class = "PICTURE"
        plan = plan_rebuild(project, [{
            "class": "PICTURE", "instance_id": instance_id,
            "member_indices": list(changed)}])
    closure = plan["closure"]
    compact = {
        "affected_instances": list(closure["affected_instances"]),
        "compose_ranges": [list(pair) for pair in closure["compose_ranges"]],
        "dirty_frames": list(closure["dirty_frames"]),
        "encode_roles": list(closure["encode_roles"]),
        "halo_members": {k: list(v) for k, v in closure["halo_members"].items()},
        "needed_members": {k: list(v)
                           for k, v in closure["needed_members"].items()},
        "stale_transitions": list(closure["stale_transitions"]),
        "unchanged": bool(closure["unchanged"])}
    projection = plan["stale_approval_projection"]
    return change_class, compact, list(projection["stale"]), list(projection["kept"])


def _flags(finding, current_sha, acks):
    """Role flags count only acknowledgements of the current digest."""
    current = [a for a in acks if a["artifact_sha256"] == current_sha]
    author = any(a["role"] == "AUTHOR_SELF_CHECK" and a["decision"] == "CHECKED"
                 for a in current)
    independent = any(a["role"] == "INDEPENDENT_REVIEW" for a in current)
    director = any(a["role"] == "DIRECTOR_APPROVAL" for a in current)
    resolved = any(a["resolves_finding"] for a in current)
    cut_pass = any(a["grants_cut_pass"] for a in current)
    waived = finding["disposition"] in WAIVERS \
        and current_sha == finding["artifact_sha256"]
    if waived:
        state = "WAIVED"
    elif resolved:
        state = "RESOLVED"
    elif author:
        state = "SELF_CHECKED"
    else:
        state = "OPEN"
    return {"author_self_check": author,
            "independent_review": independent,
            "director_approval": director,
            "finding_resolved": resolved,
            "finding_state": state,
            "cut_pass_covers_current": cut_pass,
            "waiver_covers_current": waived}


def compare_finding(project, finding_id):
    """Before/after digests, impact closure, and the two review answers.

    Read-only. `finding_resolved` and `full_playback_required` are
    independent: resolving the cited frames does not clear the full-film
    playback, and a director cut PASS does not either.
    """
    p = Path(project)
    require_animation_profile(p)
    records = _read_log(p)
    finding = _require_finding(records, finding_id)
    entry = _entry(p, finding["instance_id"])
    current = _artifact(p, entry)
    before_record = _stored_revision(
        p, finding["asset_id"], finding["sequence_revision"])
    before_members = _members(before_record)
    if sequence_content_digest(before_record) != finding["artifact_sha256"]:
        raise FilmError("보관된 리비전이 지적의 artifact hash와 다릅니다")
    changed = _changed(before_members, current["members"])
    whole = set(before_members) != set(current["members"])
    digest_changed = current["artifact_sha256"] != finding["artifact_sha256"]
    if not digest_changed:
        change_class, closure, stale, kept = _impact(
            p, finding["instance_id"], [], False)
    else:
        # A new digest with no differing member index (or a different
        # index set) is a whole-sequence change. A same-length byte edit
        # stays a PICTURE change of just those members.
        change_class, closure, stale, kept = _impact(
            p, finding["instance_id"], changed, whole or not changed)
    span = set(range(finding["frame_range"][0], finding["frame_range"][1]))
    frames_changed = any(before_members.get(i) != current["members"].get(i)
                         for i in span)
    flags = _flags(finding, current["artifact_sha256"],
                   _acks(records, finding_id))
    playback = digest_changed
    reason = ("채택 시퀀스가 지적 당시 렌더와 다릅니다. 지적 해소와 별도로 "
              "전체 재생 검토가 필요합니다. 컷 PASS는 전체 영화 승인이 "
              "아닙니다." if playback else
              "채택 시퀀스가 지적에 묶인 렌더와 같습니다. 새 전체 재생을 "
              "요구하지 않습니다.")
    reviews = review_status(p, strict=False)
    return {"finding": finding,
            "finding_id": finding_id,
            "before": {"artifact_sha256": finding["artifact_sha256"],
                       "sequence_revision": finding["sequence_revision"],
                       "asset_id": finding["asset_id"]},
            "after": {"artifact_sha256": current["artifact_sha256"],
                      "sequence_revision": current["sequence_revision"],
                      "asset_id": current["asset_id"]},
            "changed": digest_changed,
            "changed_member_indices": changed,
            "finding_frames_changed": frames_changed,
            "change_class": change_class,
            "closure": closure,
            "stale": stale,
            "kept": kept,
            "cut_reviews": [{"id": target, "state": row["state"],
                             "scope": row["scope"]}
                            for target, row in reviews["targets"].items()],
            "pass_inherited": False,
            "full_playback_required": playback,
            "full_playback_reason": reason,
            "final_approval": False,
            "facets": dict(FACETS),
            **flags}


def record_revision(project, finding_id, *, reviewer, role, note):
    """Store the before/after closure for a render that already changed.

    The row itself never resolves the finding and never inherits a PASS.
    Any role may record it — recording the diff is not an approval.
    """
    p = Path(project)
    require_animation_profile(p)
    if role not in ROLES:
        raise FilmError("역할은 " + ", ".join(ROLES) + " 중 하나여야 합니다")
    reviewer = _nonempty(reviewer, "검토자 이름이 필요합니다")
    note = _nonempty(note, "수정 메모가 필요합니다")
    view = compare_finding(p, finding_id)
    if not view["changed"]:
        raise FilmError("채택 시퀀스가 지적 당시와 같아 수정 대조를 기록할 수 없습니다")
    records = _read_log(p)
    if any(r["after_sha256"] == view["after"]["artifact_sha256"]
           for r in _revisions(records, finding_id)):
        raise FilmError("같은 수정 해시에 대한 대조가 이미 있습니다")
    finding = view["finding"]
    record = {"document_type": REVISION_TYPE, "schema_version": SCHEMA,
              "revision_id": _next_id(records, "RR", "revision_id"),
              "finding_id": finding_id,
              "instance_id": finding["instance_id"],
              "shot_id": finding["shot_id"],
              "before_sha256": view["before"]["artifact_sha256"],
              "after_sha256": view["after"]["artifact_sha256"],
              "before_revision": view["before"]["sequence_revision"],
              "after_revision": view["after"]["sequence_revision"],
              "frame_range": list(finding["frame_range"]),
              "changed_member_indices": list(view["changed_member_indices"]),
              "finding_frames_changed": bool(view["finding_frames_changed"]),
              "change_class": view["change_class"],
              "closure": view["closure"],
              "stale": list(view["stale"]),
              "kept": list(view["kept"]),
              "pass_inherited": False,
              "finding_resolved": False,
              "full_playback_required": True,
              "role": role, "reviewer": reviewer, "note": note,
              "recorded_at": now()}
    return _append(p, record)


def _hash_refusal(project, finding, supplied, current_sha):
    if supplied == current_sha:
        return
    document = load_animation_timeline(project)
    for entry in document["entries"]:
        if entry["instance_id"] == finding["instance_id"]:
            continue
        try:
            other = _artifact(project, entry)
        except FilmError:
            continue
        if other["artifact_sha256"] == supplied:
            raise FilmError("다른 컷의 리비전으로는 이 지적을 처리할 수 없습니다")
    if supplied == finding["artifact_sha256"]:
        raise FilmError("옛 렌더의 PASS는 새 렌더로 승계되지 않습니다")
    raise FilmError("지정한 artifact hash가 현재 채택 시퀀스가 아닙니다")


def acknowledge(project, finding_id, *, role, reviewer, decision,
                artifact_sha256, note, methods=None, instance_id=None):
    """Record a role-limited response against one render digest.

    The digest must be the cut's current adopted sequence. The finding's
    old digest, and any other cut's digest, are refused. Author self-check
    can only record CHECKED. Independent review can resolve the finding
    once a revision shows the cited frames changed. Only the director can
    record CUT_PASS, and that pass binds the new digest via
    `record_cut_review` without granting final-film approval.
    """
    p = Path(project)
    require_animation_profile(p)
    if role not in ROLES:
        raise FilmError("역할은 " + ", ".join(ROLES) + " 중 하나여야 합니다")
    if decision not in DECISIONS:
        raise FilmError("결정 값은 " + ", ".join(DECISIONS) + " 중 하나여야 합니다")
    reviewer = _nonempty(reviewer, "검토자 이름이 필요합니다")
    note = _nonempty(note, "기록 내용이 필요합니다")
    records = _read_log(p)
    finding = _require_finding(records, finding_id)
    if instance_id is not None and instance_id != finding["instance_id"]:
        raise FilmError("다른 컷의 리비전으로는 이 지적을 처리할 수 없습니다")
    if role == "AUTHOR_SELF_CHECK" and decision != "CHECKED":
        raise FilmError("작성자 자기 확인은 독립 검토나 감독 승인이 아닙니다")
    if role == "INDEPENDENT_REVIEW" and decision == "CUT_PASS":
        raise FilmError("독립 검토는 감독의 컷 PASS가 아닙니다")
    if decision == "CUT_PASS" and role != "DIRECTOR_APPROVAL":
        raise FilmError("컷 PASS는 감독 승인만 기록할 수 있습니다")
    methods = list(methods or [])
    if any(type(m) is not str or m not in METHODS for m in methods):
        raise FilmError("알 수 없는 검토 방법입니다")
    if decision == "CUT_PASS" and CUT_METHOD not in methods:
        raise FilmError(
            "컷 PASS에는 정상속도 전체 컷 재생(CUT_FULL_SPEED_PLAYBACK)이 "
            "필요합니다")
    if decision != "CUT_PASS" and methods:
        raise FilmError("재생 방법은 컷 PASS에만 기록합니다")
    entry = _entry(p, finding["instance_id"])
    current = _artifact(p, entry)
    _hash_refusal(p, finding, artifact_sha256, current["artifact_sha256"])
    revisions = [r for r in _revisions(records, finding_id)
                 if r["after_sha256"] == current["artifact_sha256"]]
    if decision in {"FINDING_RESOLVED", "CUT_PASS"} and not revisions:
        raise FilmError(
            "새 렌더에 대한 수정 대조가 없어 해소나 컷 PASS를 기록할 수 없습니다")
    if decision == "FINDING_RESOLVED" and not revisions[-1]["finding_frames_changed"]:
        raise FilmError("지적한 프레임이 바뀌지 않아 해소로 기록할 수 없습니다")
    if decision == "CUT_PASS":
        record_cut_review(p, finding["instance_id"], reviewer=reviewer,
                          methods=methods, decision="APPROVED")
    record = {"document_type": ACK_TYPE, "schema_version": SCHEMA,
              "ack_id": _next_id(records, "RA", "ack_id"),
              "finding_id": finding_id,
              "instance_id": finding["instance_id"],
              "shot_id": finding["shot_id"],
              "role": role, "reviewer": reviewer, "decision": decision,
              "methods": methods, "note": note,
              "artifact_sha256": current["artifact_sha256"],
              "sequence_revision": current["sequence_revision"],
              "resolves_finding": decision == "FINDING_RESOLVED",
              "grants_cut_pass": decision == "CUT_PASS",
              "grants_final_approval": False,
              "pass_inherited": False,
              "recorded_at": now()}
    return _append(p, record)


def render_pass_status(project, instance_id):
    """Whether any PASS covers this cut's current render.

    `pass_inherited` is always false. A cut PASS whose digest is not the
    current sequence is listed as stale and does not cover the new render.
    The animation-review state is reported beside it so an old APPROVED
    row that no longer binds is visible as STALE rather than reused.
    """
    p = Path(project)
    require_animation_profile(p)
    entry = _entry(p, instance_id)
    current = _artifact(p, entry)
    records = _read_log(p)
    passes = [r for r in records
              if r["document_type"] == ACK_TYPE and r["grants_cut_pass"]
              and r["instance_id"] == instance_id]
    covering = [r for r in passes
                if r["artifact_sha256"] == current["artifact_sha256"]]
    stale = [r for r in passes
             if r["artifact_sha256"] != current["artifact_sha256"]]
    reviews = review_status(p, strict=False)
    target = reviews["targets"].get(instance_id, {})
    return {"instance_id": instance_id,
            "artifact_sha256": current["artifact_sha256"],
            "sequence_revision": current["sequence_revision"],
            "animation_review_state": target.get("state"),
            "animation_review_id": target.get("review_id"),
            "cut_pass_covers_current": bool(covering),
            "pass_inherited": False,
            "old_pass_covers_new_render": False,
            "stale_pass_ids": [r["ack_id"] for r in stale],
            "final_approval": False,
            "facets": dict(FACETS)}


def carry_pass(project, instance_id):
    """Refuse to move a PASS onto another render.

    There is no success path. Callers that want a PASS on the current
    render record a new director acknowledgement against that digest.
    """
    p = Path(project)
    require_animation_profile(p)
    entry = _entry(p, instance_id)
    current = _artifact(p, entry)
    raise FilmError(
        f"{instance_id}: PASS는 묶인 렌더({current['artifact_sha256'][:12]}) "
        "밖으로 승계되지 않습니다")


def diff_view(project):
    """The finding/revision comparison the review screen renders."""
    p = Path(project)
    config = require_animation_profile(p)
    document = load_animation_timeline(p)
    entries = []
    for entry in document["entries"]:
        try:
            current = _artifact(p, entry)
        except FilmError as exc:
            entries.append({"instance_id": entry["instance_id"],
                            "shot_id": entry["shot_id"], "resolved": False,
                            "error": str(exc)})
            continue
        entries.append({"instance_id": entry["instance_id"],
                        "shot_id": entry["shot_id"], "resolved": True,
                        "used_source_range": current["used_source_range"],
                        "artifact_sha256": current["artifact_sha256"],
                        "sequence_revision": current["sequence_revision"],
                        "frame_count": (current["used_source_range"][1]
                                        - current["used_source_range"][0])})
    records = _read_log(p)
    rows = []
    for finding in _findings(records):
        try:
            rows.append(compare_finding(p, finding["finding_id"]))
        except FilmError as exc:
            rows.append({"finding_id": finding["finding_id"],
                         "finding": finding, "error": str(exc)})
    playback = any(r.get("full_playback_required") for r in rows)
    return {"kind": "review_diff_view",
            "fps": config["format"]["fps"],
            "output_frames": config["animation"]["output_frames"],
            "screen_status": "empty" if not rows else "ready",
            "entries": entries,
            "findings": rows,
            "full_playback_required": playback,
            "not_final_approval": True,
            "facets": dict(FACETS)}
