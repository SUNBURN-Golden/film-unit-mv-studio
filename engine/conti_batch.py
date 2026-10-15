"""Hand-off sittings: packets in batches, downloads assigned to slots, a Preview per batch (W00 redesign R7).

Making shots is the one step that stays manual (paste, generate, download, drop).
This module makes it one sitting per batch instead of one trip per shot:

* ``plan_batches`` groups the shots that still need a take — in manifest order —
  into sittings of ``production.sitting_limit`` shots (default 4);
* ``open_batch`` freezes the next sitting as ``qc/batches/SIT001.json`` and writes
  a sheet (``SIT001.md``) with each slot's motion-only prompt, attachments and a
  suggested file name, built by the same ``handoff.video_brief`` the app uses;
* ``assign_downloads`` looks at a downloads folder once and matches video files
  to the sitting's slots by shot id in the file name (or an explicit name → shot
  map). Anything ambiguous refuses the whole assignment before a file is
  imported; the files that match go through the ordinary ``handoff.import_many``
  path, so each is validated, logged and auto-checked against its conti;
* ``preview_batch`` runs the ordinary ``compile_preview`` and writes the sitting's
  metrics record: attempts, subscription units, human minutes, and for each take
  whether that exact take (by sha256) is in the Preview build.

Nothing here contacts a service, scripts a consumer app, polls, retries, or
touches lyrics, cut timing, reviews or LOCK. A Preview is a whole-film build, so
each sitting's Preview also carries the earlier sittings' takes.
"""
from pathlib import Path
import re

from .conti_brief import _shots, conti_metrics, load_check, revision_loop, shot_brief
from .core import FilmError, atomic_text, digest, now, production_block, project_mutex, read, require_legacy_profile, safe_path, write

SCHEMA_VERSION = 1
BATCH_DIR = "qc/batches"
DEFAULT_SITTING_LIMIT = 4
BATCH_ID = re.compile(r"SIT\d{3,}")
NEEDS_TAKE = ("NEEDS_TAKE", "AWAITING_TAKE")


def sitting_limit(p):
    """project.yaml production.sitting_limit, else the default."""
    value = production_block(p).get("sitting_limit")
    return value if type(value) is int and value >= 1 else DEFAULT_SITTING_LIMIT


# ---- records -----------------------------------------------------------------------------

def _path(p, batch_id):
    if not BATCH_ID.fullmatch(str(batch_id or "")):
        raise FilmError(f"batch id must look like SIT001, not {batch_id!r}")
    return safe_path(p, f"{BATCH_DIR}/{batch_id}.json")


def _batches(p):
    folder = safe_path(p, BATCH_DIR)
    rows = [read(f) for f in sorted(folder.glob("SIT*.json"))] if folder.is_dir() else []
    return [r for r in rows if isinstance(r, dict)]


def load_batch(project, batch_id):
    p = Path(project)
    path = _path(p, batch_id)
    if not path.is_file():
        raise FilmError(f"no batch {batch_id}; conti-batch status lists them")
    return read(path)


def _require_open(batch):
    if batch["status"] != "OPEN":
        raise FilmError(f"{batch['id']} is {batch['status']}; open the next sitting with conti-batch open")


def _summary(batch):
    assigned = batch["assigned"]
    last = (batch.get("previews") or [None])[-1]
    return {"id": batch["id"], "status": batch["status"], "slots": [s["shot_id"] for s in batch["slots"]],
            "assigned": sorted(assigned), "open_slots": [s["shot_id"] for s in batch["slots"] if s["shot_id"] not in assigned],
            "preview_build": last and last["build_id"]}


# ---- plan --------------------------------------------------------------------------------

def plan_batches(project, size=None):
    """Group the shots that need a take into sittings; say why every other shot is not in one."""
    p = Path(project)
    if size is not None and (type(size) is not int or size < 1):
        raise FilmError("size must be an integer of at least 1")
    size = size or sitting_limit(p)
    shots = _shots(p)
    candidates, blocked, waiting, done = [], [], [], []
    for shot in shots:
        sid = shot["id"]
        if shot["render_mode"] == "STATIC":
            continue
        loop = revision_loop(p, sid)
        if loop["stage"] == "ACCEPTED":
            done.append(sid)
        elif loop["stage"] in NEEDS_TAKE:
            brief = shot_brief(p, sid)
            reasons = list(brief["warnings"])
            if not brief["frame_ready"]:
                reasons.append("first frame not imported (import-frame)")
            if reasons:
                blocked.append({"shot_id": sid, "reasons": reasons})
            else:
                candidates.append({"shot_id": sid, "seconds": brief["seconds"], "clip_seconds": brief["clip_seconds"],
                                   "stage": loop["stage"], "attempt": loop["attempt"] + 1})
        else:
            waiting.append({"shot_id": sid, "stage": loop["stage"], "next": loop["next"]})
    groups = [{"index": i // size + 1, "shots": [c["shot_id"] for c in candidates[i:i + size]],
               "clip_seconds": sum(c["clip_seconds"] for c in candidates[i:i + size])}
              for i in range(0, len(candidates), size)]
    current = next((b for b in _batches(p) if b["status"] == "OPEN"), None)
    return {"sitting_limit": size, "sittings": groups, "candidates": candidates, "blocked": blocked,
            "waiting_on_checks_or_conti": waiting, "accepted": done,
            "open_batch": current and _summary(current)}


# ---- open --------------------------------------------------------------------------------

def _sheet(p, batch):
    from .handoff import video_brief
    lines = [f"# {batch['id']} — {len(batch['slots'])} shots, one sitting", "",
             "For each shot: paste the prompt into your video app with the attachments, generate, download,",
             "and save the file with the suggested name (any name containing the shot id also works).",
             f"When the downloads are in one folder: `python -m engine.cli conti-batch assign <project> {batch['id']} --folder <folder>`.", ""]
    for slot in batch["slots"]:
        brief = video_brief(p, slot["shot_id"])
        lines += [f"## {slot['shot_id']} — {brief['seconds']} s shot, make a {brief['clip_seconds']} s clip (attempt {slot['attempt']})", "",
                  f"- save as: `{slot['stem']}.mp4`", f"- app: {brief['service']}", f"- start frame: {brief['start_frame']}"]
        if brief["vocal_reference"]:
            lines.append(f"- vocal reference to attach: {brief['vocal_reference']}")
        lines += ["", "Prompt:", "", "```", brief["prompt"], "```", ""]
        if brief["checklist"]:
            lines += ["Checks this take will be judged on:", *[f"- {c}" for c in brief["checklist"]], ""]
    return "\n".join(lines)


def open_batch(project, size=None):
    """Freeze the next sitting. If one is already open it is returned, never a second one."""
    p = Path(project)
    require_legacy_profile(p)
    existing = next((b for b in _batches(p) if b["status"] == "OPEN"), None)
    if existing:
        return {**existing, "reused": True, "sheet": f"{BATCH_DIR}/{existing['id']}.md"}
    plan = plan_batches(p, size)
    if not plan["sittings"]:
        why = "; ".join(f"{b['shot_id']}: {', '.join(b['reasons'])}" for b in plan["blocked"]) or "every shot is accepted or waiting on a check"
        raise FilmError(f"no shot needs a take right now ({why})")
    group = plan["sittings"][0]
    by_id = {c["shot_id"]: c for c in plan["candidates"]}
    with project_mutex(p):
        batch_id = f"SIT{1 + max((int(b['id'][3:]) for b in _batches(p)), default=0):03d}"
        slots = []
        for sid in group["shots"]:
            brief = shot_brief(p, sid)
            slots.append({"shot_id": sid, "in_ms": brief["in_ms"], "out_ms": brief["out_ms"], "seconds": brief["seconds"],
                          "clip_seconds": brief["clip_seconds"], "attempt": by_id[sid]["attempt"],
                          "conti_sha256": brief["conti_sha256"], "stem": f"{batch_id}_{sid}"})
        batch = {"schema_version": SCHEMA_VERSION, "id": batch_id, "status": "OPEN", "opened_at": now(),
                 "sitting_limit": plan["sitting_limit"], "slots": slots, "assigned": {}, "previews": []}
        write(_path(p, batch_id), batch)
        atomic_text(safe_path(p, f"{BATCH_DIR}/{batch_id}.md"), _sheet(p, batch))
    return {**batch, "reused": False, "sheet": f"{BATCH_DIR}/{batch_id}.md"}


# ---- assign ------------------------------------------------------------------------------

def _token(shot_id):
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(shot_id)}(?![0-9])", re.IGNORECASE)


def match_files(shot_ids, batch, files, mapping=None, sha=digest, folder=None):
    """Which file goes to which open slot. Returns (assignments, already, unmatched, problems); pure of imports.

    A file belongs to a shot when the shot id appears in its name as a whole token
    (``S004``, ``SIT001_S004``, ``flow-S004-v2``), or when ``mapping`` says so. It is a
    problem — and the caller refuses everything — when a name carries two shot ids,
    when a mapping contradicts the name, when a mapping points outside the sitting
    or at a missing file, or when two different files claim one slot.
    """
    mapping = mapping or {}
    tokens = {sid: _token(sid) for sid in shot_ids}
    names = {f["name"] for f in files}
    slots = {s["shot_id"] for s in batch["slots"]}
    problems = [f"mapped file {name!r} is not among the {len(files)} scanned files" for name in mapping if name not in names]
    claims, already, unmatched = {}, [], []
    for f in files:
        name = f["name"]
        named = [sid for sid, rx in tokens.items() if rx.search(name)]
        if name in mapping:
            target = mapping[name]
            if target not in slots:
                problems.append(f"{name!r} is mapped to {target!r}, which is not a slot of {batch['id']}")
                continue
            if named and named != [target]:
                problems.append(f"{name!r} is mapped to {target} but its name says {', '.join(named)}")
                continue
            named = [target]
        if len(named) > 1:
            problems.append(f"{name!r} names several shots ({', '.join(named)}); rename it or map it")
        elif not named:
            unmatched.append({"file": name, "reason": "no shot id in the file name (rename it or use --map)"})
        elif named[0] not in slots:
            unmatched.append({"file": name, "reason": f"{named[0]} is not a slot of {batch['id']}"})
        elif named[0] in batch["assigned"]:
            have = batch["assigned"][named[0]]
            if have["file"] == name and f["size"] == have["size"] or sha(Path(folder) / name) == have["sha256"]:
                already.append({"file": name, "shot_id": named[0]})
            else:
                unmatched.append({"file": name, "reason": f"{named[0]} already has a take in {batch['id']}; "
                                  "a retake belongs to the next sitting"})
        else:
            claims.setdefault(named[0], []).append(f)
    assignments = {}
    for sid, rows in claims.items():
        if len({sha(Path(folder) / r["name"]) for r in rows}) > 1:
            problems.append(f"{sid} is claimed by {len(rows)} different files: " + ", ".join(repr(r["name"]) for r in rows))
        else:
            assignments[sid] = rows[0]
    return assignments, already, unmatched, problems


def assign_downloads(project, batch_id, folder=None, minutes=180, mapping=None, apply=True, service=None):
    """Match the folder's recent videos to the sitting's open slots and import them (apply=False: show only)."""
    from . import handoff
    p = Path(project)
    require_legacy_profile(p)
    batch = load_batch(p, batch_id)
    _require_open(batch)
    base = Path(folder).expanduser() if folder else handoff.downloads_folder()
    files = handoff.scan_downloads("video", base, minutes)
    if mapping is not None and (not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items())):
        raise FilmError("the map must be a JSON object of file name → shot id")
    assignments, already, unmatched, problems = match_files(
        [s["id"] for s in _shots(p)], batch, files, mapping, folder=base)
    if problems:
        raise FilmError(f"ambiguous: nothing was imported from {base}:\n- " + "\n- ".join(problems))
    open_slots = [s["shot_id"] for s in batch["slots"] if s["shot_id"] not in batch["assigned"] and s["shot_id"] not in assignments]
    plan = [{"shot_id": sid, "file": f["name"]} for sid, f in sorted(assignments.items())]
    result = {"batch": batch_id, "folder": str(base), "scanned": len(files), "assignments": plan, "already_imported": already,
              "unmatched": unmatched, "open_slots": open_slots, "applied": False, "imported": [], "failed": []}
    if not apply or not assignments:
        return result
    service = service or (production_block(p).get("services") or {}).get("video") or "custom"
    shas = {sid: digest(base / f["name"]) for sid, f in assignments.items()}
    loaders = [(sid, f["name"], (lambda n=f["name"]: handoff.read_download(n, "video", base))) for sid, f in sorted(assignments.items())]
    outcome = handoff.import_many(p, "video", loaders, service)
    imported = {}
    for row in outcome:
        if not row["ok"]:
            result["failed"].append({"shot_id": row["target"], "file": row["file"], "message": row["message"]})
            continue
        sid = row["target"]
        entry = {"file": row["file"], "sha256": shas[sid], "size": assignments[sid]["size"], "imported_at": now(),
                 "service": service, "warnings": row["warnings"]}
        try:
            record = load_check(p, sid, shas[sid])
            entry.update(attempt=record["attempt"], verdict=record["verdict"])
        except FilmError:
            entry.update(attempt=None, verdict=None)    # no conti: nothing was checked
        imported[sid] = entry
        result["imported"].append({"shot_id": sid, **{k: entry[k] for k in ("file", "attempt", "verdict")}})
    with project_mutex(p):
        batch = load_batch(p, batch_id)
        batch["assigned"].update(imported)
        write(_path(p, batch_id), batch)
    result["applied"] = True
    result["open_slots"] = [s["shot_id"] for s in batch["slots"] if s["shot_id"] not in batch["assigned"]]
    return result


# ---- preview and metrics -------------------------------------------------------------------

def _number(value, name):
    if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0):
        raise FilmError(f"{name} must be a nonnegative number")
    return value


def preview_batch(project, batch_id, *, units=None, human_minutes=None, quality="draft"):
    """Build the ordinary Preview and record the sitting's metrics.

    ``units`` and ``human_minutes`` are the sitting's own figures (credits spent and
    minutes at the keyboard) and are added to whatever was recorded per take with
    ``conti-check --units/--human-minutes``; enter a figure in one place only.
    """
    from .compiler import compile_preview
    p = Path(project)
    require_legacy_profile(p)
    _number(units, "units"), _number(human_minutes, "human_minutes")
    batch = load_batch(p, batch_id)
    if not batch["assigned"]:
        raise FilmError(f"{batch_id} has no assigned take yet (conti-batch assign)")
    built = compile_preview(p, quality)
    rows = {r["id"]: r for r in read(Path(built["build_dir"]) / "build.json")["shots"]}
    per_take = []
    for sid, entry in sorted(batch["assigned"].items()):
        try:
            record = load_check(p, sid, entry["sha256"])
        except FilmError:
            record = {}
        row = rows.get(sid, {})
        per_take.append({"shot_id": sid, "take_sha256": entry["sha256"], "attempt": record.get("attempt"),
                         "verdict": record.get("verdict"), "units": record.get("units"),
                         "human_minutes": record.get("human_minutes"),
                         "preview_kind": row.get("kind"), "in_preview": row.get("source_sha256") == entry["sha256"]})
    take_units = sum(t["units"] for t in per_take if t["units"] is not None)
    take_minutes = sum(t["human_minutes"] for t in per_take if t["human_minutes"] is not None)
    done = {s["shot_id"] for s in batch["slots"]} & set(batch["assigned"])
    metrics = {"attempts": len(per_take), "takes_in_preview": sum(t["in_preview"] for t in per_take),
               "units": {"per_take": take_units, "sitting": units or 0, "total": take_units + (units or 0),
                         "takes_without_units": sum(t["units"] is None for t in per_take)},
               "human_minutes": {"per_take": take_minutes, "sitting": human_minutes or 0,
                                 "total": take_minutes + (human_minutes or 0),
                                 "takes_without_minutes": sum(t["human_minutes"] is None for t in per_take)},
               "slots": len(batch["slots"]), "unassigned_slots": [s["shot_id"] for s in batch["slots"] if s["shot_id"] not in done],
               "per_take": per_take,
               "shots_so_far": [r for r in conti_metrics(p)["shots"] if r["shot_id"] in {s["shot_id"] for s in batch["slots"]}]}
    with project_mutex(p):
        batch = load_batch(p, batch_id)
        batch["previews"].append({"at": now(), "build_id": built["build_id"], "output": built["output"], "quality": quality,
                                  "asset_counts": built["asset_counts"], "warnings": built["warnings"], "metrics": metrics})
        if not metrics["unassigned_slots"]:
            batch["status"] = "CLOSED"
            batch["closed_at"] = now()
        write(_path(p, batch_id), batch)
    return {"batch": batch_id, "status": batch["status"], "build_id": built["build_id"], "output": built["output"],
            "asset_counts": built["asset_counts"], "warnings": built["warnings"], "metrics": metrics}


def close_batch(project, batch_id, note=""):
    """Give up on the slots still unassigned: they return to the pool for the next sitting."""
    p = Path(project)
    with project_mutex(p):
        batch = load_batch(p, batch_id)
        _require_open(batch)
        batch.update(status="CLOSED", closed_at=now(), close_note=str(note or "")[:600])
        write(_path(p, batch_id), batch)
    return _summary(batch)


def batch_status(project):
    return [_summary(b) for b in _batches(Path(project))]
