"""FRAME_ANIMATION_V1 scope locks, production waves and W00 route decisions.

Schema doc 7.1 (manifest/animation_locks.json) and design doc 9. Scope LOCKs
approve a bounded production range and bind exact digests; they expire (STALE)
when a bound input changes. Validity is recomputed on every check — a record is
never silently reused.

- PLAN_LOCK binds story/master audio/cut composition/frame timeline/style
  intent/roles/schedule/output spec.
- WAVE_LOCK binds the shared intent plus the wave's per-cut plan references,
  exact asset pins and used ranges.
- FINAL_LOCK binds the final edit, adopted cut/transition content, lyrics and
  output format.

Production waves (production/waves.json) declare the production order; the
configured initial wave (W00) is a subset of real main-film cuts, not a
separate pilot. Once every W00 cut has an adopted (CURRENT) review the flow
stops at NEEDS_ROUTE_DECISION; the production lead records a route_decision
document (production/route_decisions.jsonl) selecting KEEP / CHANGE / MIX and
the approved apply scope. Later waves cannot lock until a route decision opens
them. Early failure decisions are allowed with recorded grounds.

These records are protocol evidence: they approve a bounded software scope over
recorded bytes, never artwork approval, qualification or release by themselves.
"""

import hashlib
import json
import re
from pathlib import Path

from .animation_assets import load_registry, resolve_shot_sequence
from .animation_review import (bound_targets, require_current_reviews,
                               sequence_content_digest, shared_intent_digest)
from .animation_schema import (canon_bytes, check_document, load_animation_timeline,
                               read_canon, require_animation_profile, write_canon)
from .core import FilmError, atomic_text, digest, now, project_mutex, read, safe_path
from .lyrics import lyrics_review_fingerprint, timing_fingerprint, validate_lyrics

WAVES_TYPE = "animation_waves"
WAVES_PATH = "production/waves.json"
LOCKS_TYPE = "animation_locks"
LOCKS_PATH = "manifest/animation_locks.json"
ROUTES_TYPE = "route_decision"
ROUTES_PATH = "production/route_decisions.jsonl"

LOCK_SCOPES = ("PLAN_LOCK", "WAVE_LOCK", "FINAL_LOCK")
DECISIONS = ("KEEP", "CHANGE", "MIX")

WAVE_ID = re.compile(r"W[0-9]{2,}")
LOCK_ID = re.compile(r"L[0-9]{4,}")
DECISION_ID = re.compile(r"RD[0-9]{4,}")

LOCKS_FIELDS = {"document_type", "schema_version", "locks"}
LOCK_FIELDS = {"lock_id", "scope", "wave", "revision", "approver", "locked_at",
               "binding", "binding_sha256"}
WAVES_FIELDS = {"document_type", "schema_version", "waves"}
WAVE_FIELDS = {"wave", "shots", "difficulty", "note"}
DIFFICULTY_FIELDS = {"type", "reason", "shots"}
ROUTE_FIELDS = {"document_type", "schema_version", "decision_id", "wave",
                "revision", "decision", "decider", "approver", "decided_at",
                "early", "grounds", "cuts", "difficulty", "checked_types",
                "unchecked_types", "reviewed_conditions", "observations",
                "changes", "cost_time_impact", "apply_scope", "binding_sha256"}
ROUTE_CUT_FIELDS = {"instance_id", "shot_id", "used_source_range",
                    "sequence_revision", "content_sha256", "sequence_digest",
                    "review_id", "adopted"}

SHA256 = re.compile(r"[0-9a-f]{64}")

EVIDENCE_FACETS = {"qualification_state": "UNQUALIFIED",
                   "acceptance_state": "PENDING",
                   "release_state": "NOT_AUTHORIZED"}
EVIDENCE_NOTE = ("Recorded locks, reviews and route decisions are software "
                 "protocol records bound to the referenced bytes. They are "
                 "fixture evidence for the gate flow, not real artwork "
                 "approval, qualification or release authorization.")


def _sha256(document):
    return hashlib.sha256(canon_bytes(document)).hexdigest()


def _file_sha(p, relative):
    path = safe_path(p, relative)
    return digest(path) if path.is_file() else None


def _initial_wave(config):
    wave = (config.get("animation") or {}).get("initial_wave")
    return wave if type(wave) is str and WAVE_ID.fullmatch(wave) else "W00"


# ---------------------------------------------------------------------------
# Production wave declaration (production/waves.json)


def validate_waves(document, config=None, timeline=None):
    check_document(document, WAVES_TYPE)
    if not isinstance(document, dict) or set(document.keys()) != WAVES_FIELDS:
        raise FilmError("animation_waves must contain document_type, schema_version, waves")
    waves = document["waves"]
    if type(waves) is not list or not waves:
        raise FilmError("waves must be a non-empty ordered list")
    seen_waves, seen_shots = set(), set()
    for wave in waves:
        if type(wave) is not dict or set(wave.keys()) - WAVE_FIELDS:
            raise FilmError("Unknown wave field")
        if not {"wave", "shots", "difficulty"} <= set(wave.keys()):
            raise FilmError("Each wave needs wave, shots and difficulty")
        if not WAVE_ID.fullmatch(wave["wave"] if type(wave.get("wave")) is str else ""):
            raise FilmError(f"Bad wave id: {wave.get('wave')}")
        if wave["wave"] in seen_waves:
            raise FilmError(f"Duplicate wave {wave['wave']}")
        seen_waves.add(wave["wave"])
        if type(wave["shots"]) is not list or not wave["shots"]:
            raise FilmError(f"{wave['wave']} must list its shot ids")
        for shot_id in wave["shots"]:
            if type(shot_id) is not str or not re.fullmatch(r"S[0-9]{3,}", shot_id):
                raise FilmError(f"Bad shot id in {wave['wave']}: {shot_id}")
            if shot_id in seen_shots:
                raise FilmError(f"{shot_id} appears in more than one wave")
            seen_shots.add(shot_id)
        if type(wave["difficulty"]) is not list:
            raise FilmError(f"{wave['wave']} difficulty must be a list")
        for item in wave["difficulty"]:
            if (type(item) is not dict or not {"type", "reason"} <= set(item.keys())
                    or set(item.keys()) - DIFFICULTY_FIELDS):
                raise FilmError("Difficulty entries need type and reason")
            if not str(item["type"]).strip() or not str(item["reason"]).strip():
                raise FilmError("Difficulty type and reason are required")
            if "shots" in item:
                if (type(item["shots"]) is not list
                        or any(s not in wave["shots"] for s in item["shots"])):
                    raise FilmError("Difficulty shots must reference this wave's shots")
        if type(wave.get("note", "")) is not str:
            raise FilmError("wave note must be a string")
    if config is not None:
        initial = _initial_wave(config)
        if waves[0]["wave"] != initial:
            raise FilmError(f"The configured initial_wave {initial} must lead the production order")
        if not waves[0]["difficulty"]:
            raise FilmError("The initial wave must name the hard types it verifies")
    if timeline is not None:
        timeline_shots = [entry["shot_id"] for entry in timeline["entries"]]
        unknown = [s for s in seen_shots if s not in set(timeline_shots)]
        if unknown:
            raise FilmError(f"waves.json names shots missing from the timeline: {sorted(unknown)}")
        missing = [s for s in timeline_shots if s not in seen_shots]
        if missing:
            raise FilmError(f"waves.json must cover every timeline shot; unassigned: {missing}")
    return document


def load_waves(p):
    path = safe_path(p, WAVES_PATH)
    if not path.is_file():
        return None
    document = read_canon(path)
    validate_waves(document, require_animation_profile(p), load_animation_timeline(p))
    return document


def declare_waves(project, waves):
    """Write production/waves.json — the production order (design 5.3).

    `waves` is a list of {"wave": "W00", "shots": [...],
    "difficulty": [{"type", "reason"}], "note": ""}. The configured
    initial_wave must lead the order and name its hard types; every timeline
    shot must belong to exactly one wave. W00 is a subset of real main-film
    cuts, never a separate pilot document.
    """
    p = Path(project)
    with project_mutex(p):
        config = require_animation_profile(p)
        timeline = load_animation_timeline(p)
        document = {"document_type": WAVES_TYPE, "schema_version": 1,
                    "waves": waves}
        validate_waves(document, config, timeline)
        write_canon(safe_path(p, WAVES_PATH), document)
    return {"waves": [w["wave"] for w in waves], "path": WAVES_PATH}


def wave_for_shot(waves, shot_id):
    for wave in waves["waves"]:
        if shot_id in wave["shots"]:
            return wave["wave"]
    return None


def _wave_shots(waves, wave_id):
    for wave in waves["waves"]:
        if wave["wave"] == wave_id:
            return list(wave["shots"])
    raise FilmError(f"{wave_id} is not a declared production wave")


# ---------------------------------------------------------------------------
# animation_locks document


def validate_locks(document):
    check_document(document, LOCKS_TYPE)
    if not isinstance(document, dict) or set(document.keys()) != LOCKS_FIELDS:
        raise FilmError("animation_locks must contain document_type, schema_version, locks")
    if type(document["locks"]) is not list:
        raise FilmError("locks must be a list")
    seen_ids, seen_revisions = set(), set()
    for record in document["locks"]:
        if type(record) is not dict or set(record.keys()) != LOCK_FIELDS:
            raise FilmError("Unknown or missing field in a lock record")
        if not LOCK_ID.fullmatch(record["lock_id"] if type(record.get("lock_id")) is str else ""):
            raise FilmError(f"Bad lock id: {record.get('lock_id')}")
        if record["lock_id"] in seen_ids:
            raise FilmError(f"Duplicate lock id {record['lock_id']}")
        seen_ids.add(record["lock_id"])
        if record["scope"] not in LOCK_SCOPES:
            raise FilmError(f"Unknown lock scope {record['scope']}")
        if record["scope"] == "WAVE_LOCK":
            if not WAVE_ID.fullmatch(record["wave"] if type(record.get("wave")) is str else ""):
                raise FilmError("WAVE_LOCK records need a wave id")
        elif record["wave"] is not None:
            raise FilmError("Only WAVE_LOCK records carry a wave")
        if type(record["revision"]) is not int or record["revision"] < 1:
            raise FilmError("Lock revision must be a positive integer")
        key = (record["scope"], record["wave"], record["revision"])
        if key in seen_revisions:
            raise FilmError(f"Duplicate {record['scope']} revision {record['revision']}")
        seen_revisions.add(key)
        if not str(record["approver"]).strip() or not str(record["locked_at"]).strip():
            raise FilmError("Lock records need an approver and a timestamp")
        if type(record["binding"]) is not dict or record["binding"].get("scope") != record["scope"]:
            raise FilmError("Lock binding must carry its scope")
        if record["binding_sha256"] != _sha256(record["binding"]):
            raise FilmError(f"Lock {record['lock_id']} binding hash mismatch")
    return document


def load_locks(p):
    path = safe_path(p, LOCKS_PATH)
    document = read_canon(path) if path.is_file() else {"locks": []}
    if "document_type" not in document:
        document = {"document_type": LOCKS_TYPE, "schema_version": 1, "locks": []}
    return validate_locks(document)


def _audio_sha(p, config):
    actual = digest(safe_path(p, config["audio"]["path"]))
    declared = config["audio"].get("sha256")
    if declared and declared != actual:
        raise FilmError("Master audio hash mismatch with project.yaml")
    return actual


def _output_binding(config):
    return {"format": config["format"],
            "output_frames": config["animation"]["output_frames"]}


def _structure(document):
    """The plan-level projection of the edit: composition, frame ranges,
    handles and transitions. `sequence_revision` is adoption state, bound by
    the WAVE_LOCK/FINAL_LOCK scopes instead."""
    return {"target_frames": document["target_frames"],
            "entries": [{k: entry[k] for k in
                         ("instance_id", "shot_id", "used_source_range",
                          "unused_handles", "transition_out")}
                        for entry in document["entries"]]}


def _plan_binding(p, config, timeline):
    animation = config.get("animation") or {}
    return {"scope": "PLAN_LOCK",
            "audio_sha256": _audio_sha(p, config),
            "edit_sha256": _sha256(_structure(timeline)),
            "intent_sha256": shared_intent_digest(p),
            "shots_sha256": digest(safe_path(p, "manifest/shots.json")),
            "roles_sha256": _file_sha(p, animation.get("roles", "production/roles.json")),
            "schedule_sha256": _file_sha(p, animation.get("schedule", "production/schedule.json")),
            "output": _output_binding(config)}


def _wave_cut_entries(p, timeline, wave_shots):
    assignments = load_registry(p).get("assignments") or {}
    cuts = []
    for entry in timeline["entries"]:
        if entry["shot_id"] not in wave_shots:
            continue
        pin = assignments.get(entry["shot_id"]) or {}
        cuts.append({"instance_id": entry["instance_id"],
                     "shot_id": entry["shot_id"],
                     "sequence_revision": entry["sequence_revision"],
                     "used_source_range": list(entry["used_source_range"]),
                     "unused_handles": dict(entry["unused_handles"]),
                     "asset_id": pin.get("asset_id"),
                     "revision": pin.get("revision"),
                     "content_sha256": pin.get("content_sha256"),
                     "plan_sha256": _file_sha(p, f"animation/shots/{entry['shot_id']}/plan.json")})
    return cuts


def _wave_binding(p, config, timeline, waves, wave_id):
    return {"scope": "WAVE_LOCK", "wave": wave_id,
            "intent_sha256": shared_intent_digest(p),
            "waves_sha256": _sha256(waves),
            "cuts": _wave_cut_entries(p, timeline, set(_wave_shots(waves, wave_id)))}


def _final_binding(p, config, timeline):
    targets = bound_targets(p, document=timeline)
    require_current_reviews(p)
    analysis = read(safe_path(p, "analysis/audio.json"), {}) or {}
    duration_ms = analysis.get("duration_ms")
    if type(duration_ms) is not int:
        raise FilmError("Audio analysis is required for FINAL_LOCK")
    lyric_document, _ = validate_lyrics(p, duration_ms, strict=True)
    return {"scope": "FINAL_LOCK",
            "audio_sha256": _audio_sha(p, config),
            "edit_sha256": _sha256(timeline),
            "intent_sha256": shared_intent_digest(p),
            "reviews_sha256": _sha256({"cuts": targets["cuts"],
                                       "transitions": targets["transitions"]}),
            "lyrics": {"source_sha256": lyric_document["source_sha256"],
                       "timing_sha256": timing_fingerprint(lyric_document),
                       "lyrics_review_sha256": lyrics_review_fingerprint(p, lyric_document)},
            "output": _output_binding(config)}


def _current_bindings(p, config, timeline):
    bindings = {("PLAN_LOCK", None): _plan_binding(p, config, timeline)}
    waves = load_waves(p)
    if waves is not None:
        for wave in waves["waves"]:
            bindings[("WAVE_LOCK", wave["wave"])] = _wave_binding(
                p, config, timeline, waves, wave["wave"])
    try:
        bindings[("FINAL_LOCK", None)] = _final_binding(p, config, timeline)
    except Exception as exc:  # an unsatisfiable final scope is stale, never current
        bindings[("FINAL_LOCK", None)] = {"scope": "FINAL_LOCK",
                                          "uncomputable": str(exc)}
    return bindings, waves


def _changed_fields(old, new):
    changed = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) == new.get(key):
            continue
        if key == "cuts" and type(old.get(key)) is list and type(new.get(key)) is list:
            previous = {c.get("instance_id"): c for c in old[key] if type(c) is dict}
            current = {c.get("instance_id"): c for c in new[key] if type(c) is dict}
            for instance in sorted(set(previous) | set(current)):
                if previous.get(instance) != current.get(instance):
                    changed.append(f"cuts:{instance}")
            continue
        changed.append(str(key))
    return changed


def lock_status(project, timeline=None):
    """Recompute every lock's validity against current project bytes.

    `timeline` may name a candidate `animation_timeline` document (a draft)
    instead of the file on disk, so callers can project which locks a change
    that has not landed yet would make stale.
    """
    p = Path(project)
    config = require_animation_profile(p)
    if timeline is None:
        timeline = load_animation_timeline(p)
    bindings, waves = _current_bindings(p, config, timeline)
    records = load_locks(p)["locks"]

    def report(scope, wave):
        record = None
        for candidate in records:
            if candidate["scope"] == scope and candidate["wave"] == wave:
                if record is None or candidate["revision"] > record["revision"]:
                    record = candidate
        state = {"state": "UNLOCKED", "lock_id": None, "revision": None,
                 "binding_sha256": None, "changed": []}
        if record is None:
            return state
        state.update(lock_id=record["lock_id"], revision=record["revision"],
                     binding_sha256=record["binding_sha256"])
        binding = bindings.get((scope, wave))
        if binding is None:
            state["state"] = "STALE"
            state["changed"] = ["scope"]
            return state
        if record["binding_sha256"] == _sha256(binding):
            state["state"] = "CURRENT"
            return state
        state["state"] = "STALE"
        state["changed"] = _changed_fields(record["binding"], binding)
        return state

    waves_state = {}
    if waves is not None:
        for wave in waves["waves"]:
            waves_state[wave["wave"]] = report("WAVE_LOCK", wave["wave"])
    for record in records:
        if record["scope"] == "WAVE_LOCK" and record["wave"] not in waves_state:
            waves_state[record["wave"]] = {"state": "STALE", "lock_id": record["lock_id"],
                                           "revision": record["revision"],
                                           "binding_sha256": record["binding_sha256"],
                                           "changed": ["wave"]}
    return {"path": LOCKS_PATH,
            "plan": report("PLAN_LOCK", None),
            "waves": waves_state,
            "final": report("FINAL_LOCK", None),
            "records": len(records)}


def _append_lock(p, scope, wave, approver, binding):
    document = load_locks(p)
    revisions = [r["revision"] for r in document["locks"]
                 if r["scope"] == scope and r["wave"] == wave]
    used = {r["lock_id"] for r in document["locks"]}
    number = 1
    while f"L{number:04d}" in used:
        number += 1
    record = {"lock_id": f"L{number:04d}", "scope": scope, "wave": wave,
              "revision": max(revisions, default=0) + 1,
              "approver": approver.strip(), "locked_at": now(),
              "binding": binding, "binding_sha256": _sha256(binding)}
    document["locks"].append(record)
    validate_locks(document)
    write_canon(safe_path(p, LOCKS_PATH), document)
    return record


def record_plan_lock(project, approver):
    """Approve the whole-film plan scope (design 9.1)."""
    p = Path(project)
    if not str(approver).strip():
        raise FilmError("approver is required")
    with project_mutex(p):
        config = require_animation_profile(p)
        timeline = load_animation_timeline(p)
        return _append_lock(p, "PLAN_LOCK", None, approver,
                            _plan_binding(p, config, timeline))


def wave_is_open(p, config, waves, wave_id):
    """A wave may produce only when it leads the order or a route decision
    applied it (design 9.2/9.5)."""
    if wave_id == _initial_wave(config):
        return True, None
    decision = latest_route_decision(p, _initial_wave(config))
    if decision is None:
        return False, f"{wave_id} waits for the {_initial_wave(config)} route decision"
    if wave_id not in decision["apply_scope"]["waves"]:
        return False, (f"{wave_id} is outside the approved apply scope of "
                       f"{decision['decision_id']}")
    return True, decision


def unresolved_wave_shots(p, timeline, waves, wave_id):
    """Wave shots whose adopted sequence cannot be resolved right now."""
    shots = set(_wave_shots(waves, wave_id))
    missing = []
    for entry in timeline["entries"]:
        if entry["shot_id"] not in shots:
            continue
        try:
            resolve_shot_sequence(p, entry["shot_id"], entry["used_source_range"],
                                  entry["unused_handles"], entry["sequence_revision"])
        except FilmError as exc:
            missing.append({"instance_id": entry["instance_id"],
                            "shot_id": entry["shot_id"], "reason": str(exc)})
    return missing


def record_wave_lock(project, wave, approver):
    """Approve one production wave's scope. Binds the wave's exact adopted
    assets; every wave shot's sequence must already resolve."""
    p = Path(project)
    if not str(approver).strip():
        raise FilmError("approver is required")
    with project_mutex(p):
        config = require_animation_profile(p)
        timeline = load_animation_timeline(p)
        waves = load_waves(p)
        if waves is None:
            raise FilmError(f"Declare {WAVES_PATH} before locking a wave")
        _wave_shots(waves, wave)  # wave must be declared
        status = lock_status(p)
        if status["plan"]["state"] != "CURRENT":
            raise FilmError("WAVE_LOCK requires a current PLAN_LOCK")
        open_, reason = wave_is_open(p, config, waves, wave)
        if not open_:
            raise FilmError(reason)
        missing = unresolved_wave_shots(p, timeline, waves, wave)
        if missing:
            detail = ", ".join(f"{m['shot_id']} ({m['reason']})" for m in missing)
            raise FilmError(f"WAVE_LOCK binds the wave's exact assets; unresolved inputs: {detail}")
        return _append_lock(p, "WAVE_LOCK", wave, approver,
                            _wave_binding(p, config, timeline, waves, wave))


def record_final_lock(project, approver):
    """Approve the complete-film scope for the Final candidate (design 9.1)."""
    p = Path(project)
    if not str(approver).strip():
        raise FilmError("approver is required")
    with project_mutex(p):
        config = require_animation_profile(p)
        timeline = load_animation_timeline(p)
        status = lock_status(p)
        if status["plan"]["state"] != "CURRENT":
            raise FilmError("FINAL_LOCK requires a current PLAN_LOCK")
        for wave_id, wave in status["waves"].items():
            if wave["state"] != "CURRENT":
                raise FilmError(f"FINAL_LOCK requires a current WAVE_LOCK for {wave_id}")
        binding = _final_binding(p, config, timeline)  # raises for unreviewed/unresolved inputs
        return _append_lock(p, "FINAL_LOCK", None, approver, binding)


def wave_scope_gate(p, shot_ids):
    """Refuse production review for shots outside a currently locked wave.

    Projects without a declared production/waves.json (pre-ANIM-007) keep the
    ANIM-006 behaviour: reviews bind content only."""
    waves = load_waves(p)
    if waves is None:
        return
    status = lock_status(p)
    if status["plan"]["state"] != "CURRENT":
        raise FilmError(
            f"production requires a current PLAN_LOCK "
            f"(state {status['plan']['state']})")
    for shot_id in shot_ids:
        wave_id = wave_for_shot(waves, shot_id)
        if wave_id is None:
            raise FilmError(f"{shot_id} belongs to no declared production wave")
        state = status["waves"].get(wave_id, {}).get("state", "UNLOCKED")
        if state != "CURRENT":
            raise FilmError(
                f"{shot_id} is outside a locked wave scope ({wave_id} is {state}); "
                "production requires a current WAVE_LOCK")


# ---------------------------------------------------------------------------
# W00 route decisions (production/route_decisions.jsonl)


def route_decisions(p):
    """All route_decision records in append order; malformed lines fail."""
    path = safe_path(p, ROUTES_PATH)
    if not path.exists():
        return []
    lines = path.read_bytes().split(b"\n")
    if lines[-1] != b"":
        raise FilmError("route_decisions.jsonl must end with a single LF")
    records = []
    for index, line in enumerate(lines[:-1]):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as e:
            raise FilmError(f"route_decisions.jsonl line {index + 1} is not JSON") from e
        if line + b"\n" != canon_bytes(record):
            raise FilmError(f"route_decisions.jsonl line {index + 1} is not canonical")
        records.append(validate_route_decision(record))
    return records


def validate_route_decision(record):
    check_document(record, ROUTES_TYPE)
    if set(record.keys()) != ROUTE_FIELDS:
        raise FilmError("Unknown or missing field in a route_decision record")
    if not DECISION_ID.fullmatch(record["decision_id"] if type(record.get("decision_id")) is str else ""):
        raise FilmError(f"Bad decision id: {record.get('decision_id')}")
    if not WAVE_ID.fullmatch(record["wave"] if type(record.get("wave")) is str else ""):
        raise FilmError(f"Bad wave id: {record.get('wave')}")
    if type(record["revision"]) is not int or record["revision"] < 1:
        raise FilmError("route_decision revision must be a positive integer")
    if record["decision"] not in DECISIONS:
        raise FilmError(f"Unknown route decision {record['decision']}")
    for key in ("decider", "approver", "decided_at"):
        if not str(record[key]).strip():
            raise FilmError(f"route_decision needs {key}")
    if type(record["early"]) is not bool:
        raise FilmError("route_decision early must be a boolean")
    if type(record["grounds"]) is not str:
        raise FilmError("route_decision grounds must be a string")
    if record["early"] and not record["grounds"].strip():
        raise FilmError("An early route decision needs recorded grounds")
    if type(record["cuts"]) is not list or not record["cuts"]:
        raise FilmError("route_decision needs the wave's cut list")
    for cut in record["cuts"]:
        if type(cut) is not dict or set(cut.keys()) != ROUTE_CUT_FIELDS:
            raise FilmError("Bad route_decision cut entry")
        if type(cut["adopted"]) is not bool:
            raise FilmError("route_decision cut adopted must be a boolean")
        if cut["adopted"]:
            for key in ("content_sha256", "sequence_digest"):
                if type(cut[key]) is not str or not SHA256.fullmatch(cut[key]):
                    raise FilmError(f"Adopted route_decision cut needs {key}")
            if type(cut["review_id"]) is not str or not cut["review_id"].strip():
                raise FilmError("Adopted route_decision cut needs review_id")
        elif cut["review_id"] is not None:
            raise FilmError("Unadopted cuts cannot carry a review id")
        if (type(cut["used_source_range"]) is not list or len(cut["used_source_range"]) != 2
                or any(type(v) is not int or v < 0 for v in cut["used_source_range"])):
            raise FilmError("route_decision cut needs a used_source_range")
    if type(record["difficulty"]) is not list:
        raise FilmError("route_decision difficulty must be a list")
    for item in record["difficulty"]:
        if type(item) is not dict or not {"type", "reason"} <= set(item.keys()):
            raise FilmError("route_decision difficulty entries need type and reason")
    declared = {item["type"] for item in record["difficulty"]}
    checked, unchecked = record["checked_types"], record["unchecked_types"]
    if (type(checked) is not list or type(unchecked) is not list
            or any(type(t) is not str or not t.strip() for t in checked + unchecked)):
        raise FilmError("checked_types/unchecked_types must be string lists")
    if set(checked) & set(unchecked) or set(checked) | set(unchecked) != declared:
        raise FilmError("checked_types and unchecked_types must partition the declared difficulty types")
    for key in ("reviewed_conditions", "observations", "changes"):
        if type(record[key]) is not list or any(type(s) is not str for s in record[key]):
            raise FilmError(f"route_decision {key} must be a list of strings")
    if type(record["cost_time_impact"]) is not str:
        raise FilmError("route_decision cost_time_impact must be a string")
    scope = record["apply_scope"]
    if type(scope) is not dict or set(scope.keys()) != {"waves"} \
            or type(scope["waves"]) is not list \
            or any(not WAVE_ID.fullmatch(w) if type(w) is str else True for w in scope["waves"]):
        raise FilmError("route_decision apply_scope must be {\"waves\": [...]}")
    bound = {k: record[k] for k in ROUTE_FIELDS - {"document_type", "schema_version",
                                                 "decision_id", "revision", "decided_at",
                                                 "binding_sha256"}}
    if record["binding_sha256"] != _sha256(bound):
        raise FilmError(f"route_decision {record['decision_id']} binding hash mismatch")
    return record


def latest_route_decision(p, wave):
    latest = None
    for record in route_decisions(p):
        if record["wave"] == wave and (latest is None or record["revision"] > latest["revision"]):
            latest = record
    return latest


def _wave_instances(timeline, wave_shots):
    return [entry for entry in timeline["entries"] if entry["shot_id"] in wave_shots]


def record_route_decision(project, wave, *, decision, decider, approver,
                          checked_types, unchecked_types, apply_scope,
                          reviewed_conditions=None, observations=None,
                          changes=None, cost_time_impact="",
                          early=False, grounds=""):
    """Record the production lead's W00 route decision (design 9.3, schema 18).

    Normal decisions require every wave cut to hold a CURRENT adopted review;
    an early failure decision sets early=True with recorded grounds. KEEP
    requires every cut adopted, every declared difficulty type checked and no
    changes, and applies to all later waves. CHANGE/MIX name the waves they
    open. Synthetic callers produce protocol fixtures, not artwork approval.
    """
    p = Path(project)
    if not str(decider).strip() or not str(approver).strip():
        raise FilmError("decider and approver are required")
    with project_mutex(p):
        config = require_animation_profile(p)
        initial = _initial_wave(config)
        if wave != initial:
            raise FilmError(f"Route decisions gate the initial wave {initial}")
        decision = str(decision).upper()
        if decision not in DECISIONS:
            raise FilmError(f"Unknown route decision {decision}")
        timeline = load_animation_timeline(p)
        waves = load_waves(p)
        if waves is None:
            raise FilmError(f"Declare {WAVES_PATH} before a route decision")
        declared = _wave_shots(waves, wave)
        declared_entries = _wave_instances(timeline, set(declared))
        difficulty = [dict(item) for wave_row in waves["waves"] if wave_row["wave"] == wave
                      for item in wave_row["difficulty"]]
        declared_types = {item["type"] for item in difficulty}
        checked = list(checked_types or [])
        unchecked = list(unchecked_types or [])
        unknown = (set(checked) | set(unchecked)) - declared_types
        if unknown:
            raise FilmError(f"Decision names undeclared difficulty types: {sorted(unknown)}")

        from .animation_review import review_status
        status = review_status(p, strict=False)
        assignments = load_registry(p).get("assignments") or {}
        cuts = []
        adopted_all = True
        for entry in declared_entries:
            target = status["targets"].get(entry["instance_id"], {})
            adopted = target.get("state") == "CURRENT"
            adopted_all = adopted_all and adopted
            pin = assignments.get(entry["shot_id"]) or {}
            resolved_digest = None
            if pin:
                try:
                    resolved = resolve_shot_sequence(
                        p, entry["shot_id"], entry["used_source_range"],
                        entry["unused_handles"], entry["sequence_revision"])
                    resolved_digest = sequence_content_digest(resolved["record"])
                except FilmError:
                    resolved_digest = None
            cuts.append({"instance_id": entry["instance_id"], "shot_id": entry["shot_id"],
                         "used_source_range": list(entry["used_source_range"]),
                         "sequence_revision": entry["sequence_revision"],
                         "content_sha256": pin.get("content_sha256"),
                         "sequence_digest": resolved_digest,
                         "review_id": target.get("review_id") if adopted else None,
                         "adopted": adopted})
        if not adopted_all:
            if not early:
                raise FilmError(
                    "A route decision needs every wave cut's adopted review, or "
                    "early=True with recorded grounds")
            if not str(grounds).strip():
                raise FilmError("An early route decision needs recorded grounds")
        if decision == "KEEP":
            if not adopted_all:
                raise FilmError("KEEP requires every wave cut's adopted version")
            if unchecked:
                raise FilmError("KEEP requires every declared difficulty type to be checked")
            if changes:
                raise FilmError("KEEP records no changes")
            if early:
                raise FilmError("KEEP cannot be an early decision")
        if early and not str(grounds).strip():
            raise FilmError("An early route decision needs recorded grounds")

        wave_ids = [w["wave"] for w in waves["waves"]]
        later = [w for w in wave_ids if w != wave]
        if isinstance(apply_scope, list):
            scope_waves = list(apply_scope)
        else:
            scope_waves = list((apply_scope or {}).get("waves") or [])
        bad_scope = [w for w in scope_waves if w not in later]
        if bad_scope:
            raise FilmError(f"apply_scope must name later declared waves: {bad_scope}")
        if len(set(scope_waves)) != len(scope_waves):
            raise FilmError("apply_scope lists a wave twice")
        if decision == "KEEP" and set(scope_waves) != set(later):
            raise FilmError("KEEP applies the route to every later wave")
        if decision in ("CHANGE", "MIX") and not (changes or []):
            raise FilmError("CHANGE/MIX must record what changes")
        if not early and (not (reviewed_conditions or []) or not (observations or [])):
            raise FilmError(
                "A route decision records the reviewed conditions and observation grounds")

        record = {"document_type": ROUTES_TYPE, "schema_version": 1,
                  "wave": wave, "decision": decision,
                  "decider": decider.strip(), "approver": approver.strip(),
                  "early": bool(early), "grounds": grounds,
                  "cuts": cuts, "difficulty": difficulty,
                  "checked_types": checked, "unchecked_types": unchecked,
                  "reviewed_conditions": list(reviewed_conditions or []),
                  "observations": list(observations or []),
                  "changes": list(changes or []),
                  "cost_time_impact": cost_time_impact,
                  "apply_scope": {"waves": scope_waves}}
        record["binding_sha256"] = _sha256(
            {k: record[k] for k in record.keys() if k not in {"document_type",
                                                            "schema_version"}})
        existing = route_decisions(p)
        record["revision"] = max((r["revision"] for r in existing if r["wave"] == wave),
                                 default=0) + 1
        used = {r["decision_id"] for r in existing}
        number = 1
        while f"RD{number:04d}" in used:
            number += 1
        record["decision_id"] = f"RD{number:04d}"
        record["decided_at"] = now()
        validate_route_decision(record)
        path = safe_path(p, ROUTES_PATH)
        prior = path.read_bytes() if path.exists() else b""
        atomic_text(path, prior.decode("utf-8") + canon_bytes(record).decode("utf-8"))
        return record


def route_status(project):
    """Checkpoint state for the initial wave plus honest evidence facets."""
    p = Path(project)
    config = require_animation_profile(p)
    initial = _initial_wave(config)
    waves = load_waves(p)
    decision = latest_route_decision(p, initial)
    open_waves = []
    if waves is not None:
        if decision is not None:
            declared = [w["wave"] for w in waves["waves"]]
            open_waves = [w for w in decision["apply_scope"]["waves"] if w in declared]
    return {"wave": initial,
            "checkpoint": "DECIDED" if decision else "PENDING",
            "decision": decision,
            "open_waves": open_waves,
            "facets": dict(EVIDENCE_FACETS),
            "note": EVIDENCE_NOTE}
