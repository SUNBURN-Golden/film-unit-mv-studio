"""FILM shot board — 콘티·타임라인·프레임·전환의 통합 탐색.

The `film-shot-board` node: a cut workbench where picking a cut moves the
timeline, the integer frame clock, the review/lock projection and the asset
usage panel together. FRAME_ANIMATION_V1 only — LEGACY_MV projects never
reach this module, and nothing here converts one.

Everything the board shows is read back from the existing documents and
validators, never re-derived independently:

- cut order, used source ranges and pairwise transitions come from the
  canonical `animation_timeline` (`timeline/edit.json`) and
  `audit_timeline` — the same layout the compiler consumes;
- frame numbers, display numbers and `F_#######` file names come from the
  integer frame clock (`frame_clock`), so what the board displays is exactly
  what the frame map writes;
- a transition record lives on the outgoing entry (`transition_out`), while
  the shared output range is composed by the later op (`_op_ranges`); the
  boundary fixture reports both facts;
- asset usage resolves through the registry pin (`_resolve_metadata`), and
  review/lock state comes from `review_status`/`lock_status` unmodified.

`propose_edit` builds a candidate `animation_timeline` for a numeric edit —
a boundary shift funded by the neighboring cut's unused source frames, or a
pairwise transition change funded by a chosen side — revalidates it with the
locked schema validator and reports a diff against the adopted cut plus the
stale-approval projection the change would produce. `apply_edit` writes the
document under the project mutex only when the base document and pins are
unchanged, then recomputes `timeline/derived.json`. Edits touch
`timeline/edit.json` only: lyrics, cues, manifest shots, audio and locks are
never written, and a changed draft inherits no existing approval — the
post-apply review/lock states are recomputed and reported.

All records here are synthetic protocol records: qualification UNQUALIFIED,
acceptance PENDING, release NOT_AUTHORIZED.
"""
from pathlib import Path
import copy
import hashlib

from .animation_assets import (load_draft_frames, load_registry,
                               resolve_shot_sequence, _resolve_pin)
from .animation_locks import lock_status
from .animation_review import review_status
from .animation_schema import (canon_bytes, derive_timeline_view,
                               load_animation_timeline,
                               require_animation_profile, write_canon)
from .core import FilmError, digest, project_mutex, read, safe_path
from .frame_clock import (check_fps, display_number, exposure_window,
                          frame_filename, pts_end, pts_start)
from .frame_sequence import member_map_for_entry
from .motion_plan import load_shot_plan, plan_path
from .perf_scheduler import _op_ranges, _resolve_metadata
from .transitions import (CROSSFADE, audit_timeline, frame_contributors)

DERIVED_REL = "timeline/derived.json"
HARD_CUT = "HARD_CUT"

FACETS = {"qualification_state": "UNQUALIFIED",
          "acceptance_state": "PENDING",
          "release_state": "NOT_AUTHORIZED"}

TRANSITION_TYPES = (HARD_CUT, CROSSFADE)
FUND_SIDES = ("OUTGOING", "INCOMING")


def _sha256(document):
    return hashlib.sha256(canon_bytes(document)).hexdigest()


def _timeline_path(p, config):
    return safe_path(p, config["animation"].get("timeline",
                                                "timeline/edit.json"))


def _member_count(p, registry, entry):
    """The adopted revision's member count, or None when unresolvable."""
    pin = registry["assignments"].get(entry["shot_id"])
    if pin is None:
        return None
    expected = entry.get("sequence_revision")
    if expected is not None and pin["revision"] != expected:
        return None
    try:
        record = _resolve_pin(registry, pin["asset_id"], pin["revision"],
                              pin["content_sha256"])
    except FilmError:
        return None
    return len(record["files"])


def _sync_handle(p, registry, entry, side):
    """Record the true spare extent on the touched side of one entry.

    `unused_handles` declares the unused source frames around the used
    range; after a boundary move or transition funding, the touched side's
    declaration is rewritten to the adopted revision's actual spare count —
    `before` = frames before the used start, `after` = frames after the used
    end — so the record is honest rather than merely consistent.
    """
    count = _member_count(p, registry, entry)
    if count is None:
        raise FilmError(
            f"{entry['shot_id']}의 채택 시퀀스를 확인할 수 없어 "
            "소스 여유분을 계산할 수 없습니다")
    start, end = entry["used_source_range"]
    spare = start if side == "before" else count - end
    if spare < 0:
        raise FilmError(
            f"{entry['shot_id']}의 소스가 짧습니다: 채택 리비전은 "
            f"{count}프레임뿐입니다")
    entry["unused_handles"][side] = spare


def _entry_index(entries, instance_id):
    for index, entry in enumerate(entries):
        if entry["instance_id"] == instance_id:
            return index
    raise FilmError(f"Unknown timeline instance: {instance_id}")


def _apply_edits(p, document, instance_id, *, boundary_shift=None,
                 transition=None):
    """Apply one boundary shift and/or one transition change to a copy.

    Returns (proposed_document, changes). A boundary shift of `delta` frames
    moves the outgoing boundary: `delta > 0` shows `delta` more frames of
    the left cut and starts the right cut `delta` frames later; `delta < 0`
    is the mirror. A transition change funds a bigger overlap from the
    chosen side's unused source frames (OUTGOING extends the left cut's
    tail; INCOMING extends the right cut's head) and releases frames back to
    the funded side when the overlap shrinks. Validation runs after all
    edits so combined changes are judged as one document.
    """
    registry = load_registry(p)
    entries = document["entries"]
    index = _entry_index(entries, instance_id)
    proposed = copy.deepcopy(document)
    pentries = proposed["entries"]
    changes = []
    touched = set()

    def resync(entry, side):
        key = (entry["instance_id"], side)
        if key not in touched:
            _sync_handle(p, registry, entry, side)
            touched.add(key)

    if boundary_shift is not None:
        if type(boundary_shift) is not int or boundary_shift == 0:
            raise FilmError("경계 이동은 0이 아닌 정수 프레임이어야 합니다")
        if index >= len(pentries) - 1:
            raise FilmError("마지막 컷에는 이동할 수 있는 뒤 경계가 없습니다")
        left, right = pentries[index], pentries[index + 1]
        left["used_source_range"] = [left["used_source_range"][0],
                                     left["used_source_range"][1]
                                     + boundary_shift]
        right["used_source_range"] = [right["used_source_range"][0]
                                      + boundary_shift,
                                      right["used_source_range"][1]]
        resync(left, "after")
        resync(right, "before")
        changes.append({"op": "boundary_shift", "delta": boundary_shift,
                        "left": left["instance_id"],
                        "right": right["instance_id"]})

    if transition is not None:
        if index >= len(pentries) - 1:
            raise FilmError("마지막 컷에는 transition_out이 없습니다")
        spec = dict(transition)
        kind = spec.get("type")
        if kind not in TRANSITION_TYPES:
            raise FilmError("전환 종류는 HARD_CUT 또는 CROSSFADE입니다")
        overlap = spec.get("overlap_frames")
        if type(overlap) is not int or overlap < 0:
            raise FilmError("겹침 프레임은 0 이상의 정수입니다")
        fund = spec.get("fund", "OUTGOING")
        if fund not in FUND_SIDES:
            raise FilmError("겹침 출처는 OUTGOING 또는 INCOMING입니다")
        left, right = pentries[index], pentries[index + 1]
        record = dict(left["transition_out"])
        delta = overlap - record["overlap_frames"]
        if delta:
            # A bigger overlap borrows source frames; a smaller one returns
            # them to the side that funded it.
            if fund == "OUTGOING":
                left["used_source_range"] = [
                    left["used_source_range"][0],
                    left["used_source_range"][1] + delta]
                resync(left, "after")
            else:
                right["used_source_range"] = [
                    right["used_source_range"][0] - delta,
                    right["used_source_range"][1]]
                resync(right, "before")
        record["type"] = kind
        record["overlap_frames"] = overlap
        if kind == CROSSFADE:
            record["curve"] = "LINEAR_INTERIOR_V1"
        else:
            record.pop("curve", None)
        left["transition_out"] = record
        changes.append({"op": "transition", "transition_id": record["id"],
                        "type": kind, "overlap_frames": overlap,
                        "fund": fund})

    return proposed, changes


def _entry_snapshot(entry, row):
    return {"instance_id": entry["instance_id"], "shot_id": entry["shot_id"],
            "output_range": list(row["output_range"]),
            "used_source_range": list(entry["used_source_range"]),
            "unused_handles": dict(entry["unused_handles"]),
            "sequence_revision": entry["sequence_revision"],
            "transition_out": (dict(entry["transition_out"])
                               if entry["transition_out"] else None)}


def _diff(before_document, before_audit, proposed, proposed_audit):
    """Per-entry adopted-vs-draft comparison; only changed entries listed."""
    before_rows = {r["instance_id"]: r for r in before_audit["entries"]}
    after_rows = ({r["instance_id"]: r for r in proposed_audit["entries"]}
                  if proposed_audit else {})
    rows = []
    for before, after in zip(before_document["entries"],
                             proposed["entries"]):
        snapshot_a = _entry_snapshot(before, before_rows[before["instance_id"]])
        snapshot_b = _entry_snapshot(
            after, after_rows.get(after["instance_id"],
                                  {"output_range": None}))
        if snapshot_a != snapshot_b:
            rows.append({"instance_id": after["instance_id"],
                         "shot_id": after["shot_id"],
                         "adopted": snapshot_a, "draft": snapshot_b})
    return rows


def _rebuild_changes(changes, proposed, before_document):
    """The ANIM-020 change-class list equivalent to this draft."""
    before = {e["instance_id"]: e for e in before_document["entries"]}
    out = []
    edited_transitions = {c.get("transition_id") for c in changes
                          if c["op"] == "transition"}
    for entry in proposed["entries"]:
        old = before[entry["instance_id"]]
        if entry["used_source_range"] != old["used_source_range"] \
                or entry["unused_handles"] != old["unused_handles"]:
            out.append({"class": "USED_RANGE",
                        "instance_id": entry["instance_id"],
                        "used_source_range": list(entry["used_source_range"]),
                        "unused_handles": dict(entry["unused_handles"])})
        if (entry.get("transition_out") or {}).get("id") in \
                edited_transitions:
            updates = {k: v for k, v in entry["transition_out"].items()
                       if k not in ("id", "to_instance")}
            if "curve" not in entry["transition_out"]:
                updates["curve"] = None
            out.append({"class": "TRANSITION",
                        "transition_id": entry["transition_out"]["id"],
                        "set": updates})
    return out


def _projection(p, document, proposed, exposure=None):
    """Review/lock states after the draft, projected with the same engines.

    The projection runs the real `review_status`/`lock_status` against the
    candidate document, so what the draft panel shows is exactly what the
    engines will report after apply — a STALE target keeps its old record
    as evidence and never counts as an approval.
    """
    current = review_status(p, exposure=exposure, strict=False)
    projected = review_status(p, exposure=exposure, strict=False,
                              document=proposed)
    reviews = {}
    for target, entry in projected["targets"].items():
        reviews[target] = {"scope": entry["scope"],
                           "now": current["targets"].get(target, {})
                           .get("state", "UNREVIEWED"),
                           "after": entry["state"]}
    locks_now = lock_status(p)
    locks_after = lock_status(p, timeline=proposed)

    def pair(scope, wave):
        name = scope if wave is None else f"{scope}:{wave}"
        now_state = (locks_now["waves"].get(wave) if wave
                     else locks_now[{"PLAN_LOCK": "plan",
                                     "FINAL_LOCK": "final"}[scope]])
        after_state = (locks_after["waves"].get(wave) if wave
                       else locks_after[{"PLAN_LOCK": "plan",
                                         "FINAL_LOCK": "final"}[scope]])
        return name, {"now": (now_state or {}).get("state"),
                      "after": (after_state or {}).get("state")}

    locks = dict([pair("PLAN_LOCK", None), pair("FINAL_LOCK", None)]
                 + [pair("WAVE_LOCK", w) for w in locks_after["waves"]])
    return {"reviews": reviews, "locks": locks}


def _pins(registry, document):
    pins = {}
    for entry in document["entries"]:
        pin = registry["assignments"].get(entry["shot_id"])
        pins[entry["instance_id"]] = ({"asset_id": pin["asset_id"],
                                      "revision": pin["revision"],
                                      "content_sha256": pin["content_sha256"]}
                                     if pin else None)
    return pins


def _lyrics_fingerprint(p):
    """Byte hashes of the lyric documents the board promises not to move."""
    source = safe_path(p, "input/lyrics.txt")
    timing = safe_path(p, "lyrics/lyrics_timed.json")
    document = read(timing, {}) if timing.is_file() else {}
    return {"source_sha256": digest(source) if source.is_file() else None,
            "timing_sha256": digest(timing) if timing.is_file() else None,
            "cues": len((document or {}).get("cues", []))}


def propose_edit(project, instance_id, *, boundary_shift=None,
                 transition=None, exposure=None):
    """A candidate `animation_timeline` for one numeric edit — never written.

    The draft carries the full candidate document, the adopted-vs-draft
    diff, the projected review/lock states and (when every referenced
    sequence resolves) the ANIM-020 rebuild closure for the same change.
    An invalid candidate is returned with `errors` — it can be inspected but
    `apply_edit` refuses it.
    """
    if boundary_shift is None and transition is None:
        raise FilmError("편집할 경계 이동 또는 전환 변경을 지정하세요")
    p = Path(project)
    config = require_animation_profile(p)
    output_frames = config["animation"]["output_frames"]
    document = load_animation_timeline(p)
    registry = load_registry(p)
    errors = []
    try:
        proposed, changes = _apply_edits(
            p, document, instance_id, boundary_shift=boundary_shift,
            transition=transition)
    except FilmError as exc:
        return {"kind": "shot_board_draft", "valid": False,
                "instance_id": instance_id, "errors": [str(exc)],
                "request": {"boundary_shift": boundary_shift,
                            "transition": transition},
                "facets": dict(FACETS)}
    try:
        proposed_audit = audit_timeline(proposed, output_frames)
    except FilmError as exc:
        proposed_audit = None
        errors.append(str(exc))
    for entry in proposed["entries"]:
        before_entry = next(e for e in document["entries"]
                            if e["instance_id"] == entry["instance_id"])
        if entry == before_entry:
            continue
        try:
            resolve_shot_sequence(p, entry["shot_id"],
                                  entry["used_source_range"],
                                  entry["unused_handles"],
                                  entry["sequence_revision"])
        except FilmError as exc:
            errors.append(f"{entry['instance_id']}: {exc}")
    valid = not errors and proposed_audit is not None
    draft = {"kind": "shot_board_draft", "valid": valid,
             "instance_id": instance_id, "errors": errors,
             "base_sha256": _sha256(document),
             "base_pins": _pins(registry, document),
             "document": proposed, "changes": changes,
             "request": {"boundary_shift": boundary_shift,
                         "transition": transition},
             "diff": _diff(document, audit_timeline(document), proposed,
                           proposed_audit),
             "produced_frames": (proposed_audit["output_frames"]
                                 if proposed_audit else None),
             "coverage": (proposed_audit["coverage"]
                          if proposed_audit else None),
             "lyrics": _lyrics_fingerprint(p),
             "facets": dict(FACETS)}
    if valid:
        draft["projection"] = _projection(p, document, proposed,
                                          exposure=exposure)
        try:
            from .render_provenance import plan_rebuild
            plan = plan_rebuild(
                p, _rebuild_changes(changes, proposed, document),
                exposure=exposure)
            draft["closure"] = plan["closure"]
            draft["stale_approval_projection"] = \
                plan["stale_approval_projection"]
        except FilmError as exc:
            draft["closure"] = {"unavailable": str(exc)}
    else:
        draft["projection"] = {"skipped":
                               "초안이 유효하지 않아 재검수 범위를 계산하지 "
                               "못했습니다"}
        draft["closure"] = None
    return draft


def apply_edit(project, draft):
    """Write a validated draft as `timeline/edit.json` under the project mutex.

    The draft must still describe the current document and pins — anything
    that changed since `propose_edit` refuses here rather than silently
    landing on a different base. Only the timeline document and its
    recalculated derived view are written; lyrics, cues, manifest shots,
    audio and lock records are untouched. The report names the scopes that
    are now stale — a changed draft never inherits the old approval.
    """
    if not isinstance(draft, dict) or draft.get("kind") != "shot_board_draft":
        raise FilmError("컷 작업대 초안이 아닙니다")
    if not draft.get("valid"):
        raise FilmError("유효하지 않은 초안은 적용할 수 없습니다: "
                        + "; ".join(draft.get("errors") or ["unknown"]))
    p = Path(project)
    with project_mutex(p):
        config = require_animation_profile(p)
        output_frames = config["animation"]["output_frames"]
        document = load_animation_timeline(p)
        if _sha256(document) != draft["base_sha256"]:
            raise FilmError("초안을 만든 뒤 타임라인이 바뀌었습니다 — "
                            "다시 초안을 만드세요")
        registry = load_registry(p)
        current_pins = _pins(registry, document)
        for iid, pin in draft["base_pins"].items():
            if current_pins.get(iid) != pin:
                raise FilmError("초안을 만든 뒤 컷의 채택 자산이 바뀌었습니다 — "
                                "다시 초안을 만드세요")
        request = draft["request"]
        proposed, changes = _apply_edits(
            p, document, draft["instance_id"],
            boundary_shift=request["boundary_shift"],
            transition=request["transition"])
        audit = audit_timeline(proposed, output_frames)
        for entry, before_entry in zip(proposed["entries"],
                                       document["entries"]):
            if entry == before_entry:
                continue
            resolve_shot_sequence(p, entry["shot_id"],
                                  entry["used_source_range"],
                                  entry["unused_handles"],
                                  entry["sequence_revision"])
        lyrics_before = _lyrics_fingerprint(p)
        write_canon(_timeline_path(p, config), proposed)
        write_canon(safe_path(p, DERIVED_REL), derive_timeline_view(proposed))
        reviews = review_status(p, strict=False)
        locks = lock_status(p)
        stale = [f"{e['scope']}:{t}" for t, e in reviews["targets"].items()
                 if e["state"] == "STALE"]
        if locks["plan"]["state"] == "STALE":
            stale.append("PLAN_LOCK")
        stale += [f"WAVE_LOCK:{w}" for w, s in locks["waves"].items()
                  if s["state"] == "STALE"]
        if locks["final"]["state"] == "STALE":
            stale.append("FINAL_LOCK")
        return {"kind": "shot_board_edit_applied",
                "changes": changes,
                "timeline_sha256": _sha256(proposed),
                "output_frames": audit["output_frames"],
                "coverage": audit["coverage"],
                "stale": sorted(stale),
                "lyrics": {"unchanged": lyrics_before == _lyrics_fingerprint(p),
                           **lyrics_before},
                "facets": dict(FACETS)}


def _plan_summary(p, shot_id, length, canvas):
    """Stored motion-plan summary for display; stale plans are reported."""
    if not safe_path(p, plan_path(shot_id)).is_file():
        return None
    try:
        document = load_shot_plan(p, shot_id, length=length, canvas=canvas)
    except FilmError as exc:
        return {"error": str(exc)}
    tracks = document["tracks"]
    return {"keyposes": len(document.get("keyposes", [])),
            "segments": len(document.get("segments", [])),
            "camera": bool(tracks["camera"]["transform"]),
            "layers": {lid: {"drawings": len(track["drawings"]),
                             "exposure_slots": len(track["exposure"]),
                             "animated": bool(track["transform"]),
                             "masked": bool(track["mask"])}
                       for lid, track in tracks["layers"].items()}}


def board_view(project):
    """The shot-workbench read model over the canonical documents.

    One row per timeline entry in edit order, each carrying the integer
    output range, 1-based display numbers, the exact rational exposure
    window, the resolved asset pin, draft-ledger coverage, plan summary,
    storyboard reference and review/lock state. `boundaries` is the pairwise
    transition list with its output range and the boundary frames on each
    side — the UI fixture materializes it into per-frame ownership rows.
    """
    p = Path(project)
    config = require_animation_profile(p)
    fps = check_fps(config["format"]["fps"])
    output_frames = config["animation"]["output_frames"]
    document = load_animation_timeline(p)
    audit = audit_timeline(document, output_frames)
    reviews = review_status(p, strict=False)
    locks = lock_status(p)
    registry = load_registry(p)
    shots = {s["id"]: s for s in read(p / "manifest/shots.json", []) or []}
    lyric_info = _lyrics_fingerprint(p)

    transition_state = {}
    for transition in audit["transitions"]:
        state = reviews["targets"].get(transition["id"], {})
        transition_state[transition["id"]] = state.get("state")

    rows = []
    for position, (entry, row) in enumerate(zip(document["entries"],
                                                audit["entries"])):
        iid, shot_id = entry["instance_id"], entry["shot_id"]
        s0, s1 = row["output_range"]
        meta = {"resolved": False}
        canvas = None
        try:
            found = _resolve_metadata(p, shot_id,
                                      entry["used_source_range"],
                                      entry["unused_handles"],
                                      entry["sequence_revision"])
            record = found["record"]
            canvas = record.get("canvas")
            meta = {"resolved": True,
                    "asset_id": found["asset_id"],
                    "revision": found["revision"],
                    "content_sha256": found["content_sha256"],
                    "kind": record["kind"],
                    "member_count": len(record["files"]),
                    "canvas": canvas}
        except FilmError as exc:
            meta["error"] = str(exc)
        ledger = load_draft_frames(p, shot_id)
        draft_frames = sorted({e["frame"] for e in ledger["entries"]})
        shot = shots.get(shot_id, {})
        storyboard = next((r for r in shot.get("references", [])
                           if safe_path(p, r).is_file()), None)
        start, end = (pts_start(s0, fps), pts_end(s1 - 1, fps))
        rows.append({
            "position": position,
            "instance_id": iid,
            "shot_id": shot_id,
            "output_range": [s0, s1],
            "display_range": [display_number(s0), display_number(s1 - 1)],
            "length_frames": s1 - s0,
            "used_source_range": list(entry["used_source_range"]),
            "unused_handles": dict(entry["unused_handles"]),
            "sequence_revision": entry["sequence_revision"],
            "exposure_window": {
                "start": [start.numerator, start.denominator],
                "end": [end.numerator, end.denominator]},
            "transition_out": (dict(entry["transition_out"])
                               if entry["transition_out"] else None),
            "pin": meta,
            "draft_frames": {"recorded": len(draft_frames),
                             "frames": draft_frames},
            "plan": _plan_summary(p, shot_id, s1 - s0, canvas),
            "storyboard": {"path": storyboard,
                           "kind": shot.get("storyboard_kind"),
                           "description": shot.get("description")},
            "review": (reviews["targets"].get(iid, {})
                       .get("state", "UNREVIEWED")),
            "transition_review": (transition_state.get(
                (entry.get("transition_out") or {}).get("id")))})

    boundaries = []
    for index, transition in enumerate(audit["transitions"]):
        s0, s1 = transition["output_range"]
        before = s0 - 1 if s0 > 0 else None
        after = s1 if s1 < audit["output_frames"] else None
        boundaries.append({
            "index": index,
            "id": transition["id"],
            "type": transition["type"],
            "from_instance": transition["from_instance"],
            "to_instance": transition["to_instance"],
            "owner": transition["from_instance"],
            "overlap_frames": transition["overlap_frames"],
            "output_range": [s0, s1],
            "display_range": ([display_number(s0), display_number(s1 - 1)]
                              if s1 > s0 else None),
            "boundary_frames": {
                "before": {"frame_index": before,
                           "display": (display_number(before)
                                      if before is not None else None),
                           "file": (frame_filename(before)
                                    if before is not None else None)},
                "after": {"frame_index": after,
                          "display": (display_number(after)
                                      if after is not None else None),
                          "file": (frame_filename(after)
                                   if after is not None else None)}},
            "review": transition_state.get(transition["id"],
                                           "UNREVIEWED")})

    counts = {}
    for target, entry in reviews["targets"].items():
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
    return {"kind": "shot_board_view", "project": str(p),
            "fps": fps, "output_frames": output_frames,
            "target_frames": document["target_frames"],
            "timeline_sha256": _sha256(document),
            "used_source_frames": audit["used_source_frames"],
            "overlap_frames": audit["overlap_frames"],
            "coverage": audit["coverage"],
            "entries": rows,
            "transitions": boundaries,
            "instances": [r["instance_id"] for r in rows],
            "review_counts": counts,
            "locks": {"plan": locks["plan"]["state"],
                      "waves": {w: s["state"]
                                for w, s in locks["waves"].items()},
                      "final": locks["final"]["state"]},
            "lyrics": lyric_info,
            "facets": dict(FACETS)}


def board_entry(view, instance_id):
    """One entry of a `board_view`, keyed by the stable instance id.

    Selection everywhere on the board is the `instance_id` itself, so a
    timeline reorder or a reload can move `position` but never move the
    identity the user selected.
    """
    for row in view["entries"]:
        if row["instance_id"] == instance_id:
            return row
    raise FilmError(f"Unknown timeline instance: {instance_id}")


def boundary_window(project, index, margin=2):
    """Per-frame ownership rows around one pairwise transition (UI fixture).

    Each output frame in `[start - margin, end + margin)` lists every
    contributing instance with its weight as the canonical `[num, den]`
    pair, the resolved member index, the compose-op owner (the later entry's
    op owns the shared range; the outgoing tail is read as its halo), and
    the transition-record owner (the outgoing entry, which holds
    `transition_out`). Boundary frames on each side are included so the
    display shows `F_#######` names with integer frame indices only.
    """
    p = Path(project)
    config = require_animation_profile(p)
    fps = check_fps(config["format"]["fps"])
    document = load_animation_timeline(p)
    audit = audit_timeline(document)
    transitions = audit["transitions"]
    if type(index) is not int or not 0 <= index < len(transitions):
        raise FilmError(f"Unknown transition index: {index}")
    transition = transitions[index]
    entries = {e["instance_id"]: e for e in document["entries"]}
    endpoints = {transition["from_instance"], transition["to_instance"]}
    member_maps = {iid: member_map_for_entry(entries[iid])
                   for iid in endpoints}
    locators = {}
    for iid in endpoints:
        entry = entries[iid]
        try:
            found = _resolve_metadata(p, entry["shot_id"],
                                      entry["used_source_range"],
                                      entry["unused_handles"],
                                      entry["sequence_revision"])
        except FilmError:
            continue
        locators[iid] = {f.get("frame_index"): f["relative_name"]
                         for f in found["record"]["files"]}
    op_owner = {}
    for op in _op_ranges(audit):
        for frame in range(*op["output_range"]):
            op_owner[frame] = op["instance_id"]
    s0, s1 = transition["output_range"]
    start = max(0, s0 - margin)
    end = min(audit["output_frames"], s1 + margin)
    frames = []
    for frame in range(start, end):
        pairs, tr = frame_contributors(audit, frame)
        contributors = []
        for row, weight in pairs:
            local = frame - row["output_range"][0]
            member = member_maps[row["instance_id"]][local]["member"]
            contributors.append({
                "instance_id": row["instance_id"],
                "shot_id": row["shot_id"],
                "local_frame_index": local,
                "member": member,
                "member_path": locators.get(row["instance_id"], {})
                .get(member),
                "weight": [weight.numerator, weight.denominator],
                "side": ("sole" if tr is None else
                         "outgoing"
                         if row["instance_id"] == tr["from_instance"]
                         else "incoming")})
        frames.append({
            "frame_index": frame,
            "display": display_number(frame),
            "file": frame_filename(frame),
            "time": {"start": [pts_start(frame, fps).numerator,
                               pts_start(frame, fps).denominator],
                     "end": [pts_end(frame, fps).numerator,
                             pts_end(frame, fps).denominator]},
            "op_owner": op_owner.get(frame),
            "transition": tr["id"] if tr else None,
            "contributors": contributors})
    window = exposure_window(frames[0]["frame_index"], fps)
    window["end"] = exposure_window(frames[-1]["frame_index"], fps)["end"]
    return {"kind": "shot_board_boundary",
            "transition": dict(transition),
            "record_owner": transition["from_instance"],
            "overlap_op_owner": transition["to_instance"],
            "boundary_frames": {
                "before": s0 - 1 if s0 > 0 else None,
                "after": s1 if s1 < audit["output_frames"] else None},
            "window": window,
            "frames": frames,
            "facets": dict(FACETS)}
