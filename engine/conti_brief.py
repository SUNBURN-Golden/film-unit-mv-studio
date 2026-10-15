"""Shot briefs from the detailed conti, and take check-in against it (W00 redesign R2).

The conti (``engine/conti.py``) says what a shot must show and when. This module
turns it into two things:

1. the generation brief a person pastes into a subscription app — a first-frame
   prompt and a motion-only video prompt in which every camera move, motion beat,
   music hit and lyric line is given in clip-relative seconds, with the clip
   length snapped to what the video app can make;
2. the check-in record for a take that comes back — mechanical checks (duration,
   aspect, resolution, stillness), a five-frame contact sheet, the conti's
   acceptance criteria as PENDING items, and the attempt / subscription-units /
   human-minutes ledger the hard-cut evidence needs.

A judge request carries the criteria and the frame paths to a vision-capable LLM
through its own subscription interface; the pasted answer becomes per-criterion
verdicts. UNSURE stays PENDING so a person confirms only what the judge could not.

Nothing here calls a service, drives a consumer app, or changes shots, lyrics,
reviews or LOCK. Records live under ``qc/conti/<shot>/``.
"""
from fractions import Fraction
from pathlib import Path
import difflib
import hashlib
import json
import re

import numpy as np
from PIL import Image

from .conti import CONTI_SCHEMA, load_conti, missing_fields, persist_request
from .core import (FilmError, atomic_text, ffmpeg, now, object_hash, probe, production_block, project_mutex,
                   read, require_legacy_profile, safe_path, write)
from .director import extract_json
from .packets import aspect_ratio, entry_text, frame_references, _style_words
from .renderers import build_prompt

SCHEMA_VERSION = 1
QA_DIR = "qc/conti"
CLIP_STEPS = (4, 6, 8)          # clip lengths Flow / Veo 3.1 can make
SAMPLE_FRACTIONS = (0, .25, .5, .75, 1)
SAMPLE_WIDTH = 480
STILL_DIFF = 2.0                # mean |Δ| per channel between sampled frames at or below = no visible motion
ASPECT_TOLERANCE = 0.03
VERDICTS = ("PASS", "FAIL", "PENDING")
ANSWER_VERDICTS = {"PASS": "PASS", "FAIL": "FAIL", "UNSURE": "PENDING"}

# Claudia port: a wide shot asks for the shorter `far` identity line instead
# of the full `look` block — close-up detail reads as noise at that distance.
# Entries without `far` (or `look`) are described by what is there; nothing
# invents identity text.
WIDE_SHOT = re.compile(r"\b(?:wide|full[- ]?length|establishing|long shot|EWS|WS|FS)\b", re.IGNORECASE)

JUDGE_SYSTEM = """You are checking one generated music-video take against its storyboard (conti). Reply with ONE JSON object and nothing else.
- Look at every attached frame (they are evenly spaced samples of the clip, first to last).
- For each numbered criterion answer PASS only when the frames clearly show it, FAIL when they clearly contradict it, and UNSURE when the sampled frames cannot settle it (for example a motion that happens between samples).
- Quote what you saw in `reason`; never guess from the prompt text alone.
- Do not invent criteria, do not score mood; the list below is the whole checklist.
Answer in exactly this JSON shape:
{"take": "<take sha256>", "criteria": [{"index": 1, "verdict": "PASS|FAIL|UNSURE", "reason": "what the frames show"}]}"""


# ---- brief ------------------------------------------------------------------------

def _rel(ms, shot):
    """Milliseconds on the song's timeline → seconds from this shot's first frame."""
    return round((ms - shot["in_ms"]) / 1000, 1)


def clip_length(shot, steps=CLIP_STEPS):
    """The shot's own length and the shortest app clip that covers it (None when none does)."""
    seconds = shot["duration_ms"] / 1000
    fit = next((s for s in steps if s + 1e-9 >= seconds), None)
    return {"seconds": round(seconds, 2), "clip_seconds": fit}


def conti_digest(doc):
    """Content digest of a conti — the brief and every take check are pinned to it."""
    if not doc:
        return None
    return object_hash({k: v for k, v in doc.items() if k not in {"updated_at", "source", "request_sha256"}})


def _shots(p):
    shots = read(p / "manifest/shots.json", [])
    if not shots:
        raise FilmError("먼저 제작 패키지를 만들어 샷 목록을 준비하세요")
    return shots


def _shot(p, shot_id):
    shot = next((s for s in _shots(p) if s["id"] == shot_id), None)
    if shot is None:
        raise FilmError(f"Unknown shot ID: {shot_id}")
    return shot


def _bible(p, filename, key, wanted):
    return {r["id"]: r for r in read(p / "bible" / filename, {}).get(key, [])
            if isinstance(r, dict) and r.get("id") in wanted}


def _beats(rows, shot, key="action"):
    return [f"at {_rel(r['at_ms'], shot)} s: {r.get(key) or r.get('hit') or r.get('action')}"
            for r in rows if r.get(key) or r.get("hit") or r.get("action")]


def _identity(entry, wide):
    """The identity text for a frame prompt: `far` on a wide shot when the
    entry carries one, else the full `look` (empty when neither exists)."""
    if wide and str(entry.get("far") or "").strip():
        return str(entry["far"]).strip()
    return str(entry.get("look") or "")


def first_frame_prompt(p, shot, doc, aspect):
    """Still-image prompt for the shot's first frame, from the conti where it is filled in."""
    cast = _bible(p, "characters.yaml", "characters", set(shot.get("characters", [])))
    places = _bible(p, "locations.yaml", "locations", set(shot.get("locations", [])))
    style = _style_words(p)
    cont = doc.get("continuity", {})
    framing = doc.get("framing") or shot.get("composition", "")
    wide = bool(WIDE_SHOT.search(framing or ""))
    lines = [f"Create one still image: the first frame of music-video shot {shot['id']}, {aspect} aspect ratio.",
             f"Scene: {doc.get('scene_goal')}" if doc.get("scene_goal") else shot.get("description", ""),
             f"Framing: {framing}",
             f"Camera: {doc.get('camera', {}).get('type') or (shot.get('camera') or {}).get('type', 'locked')}"]
    for b in doc.get("blocking", []):
        who = cast.get(b["character"], {})
        look = _identity(who, wide) if wide and str(who.get("far") or "").strip() else entry_text(who)
        name = who.get("name") or b["character"]
        lines.append(f"{name}" + (f" ({look})" if look else "") + ": "
                     + "; ".join(x for x in (b.get("pose"), f"expression {b['expression']}" if b.get("expression") else "") if x))
    for place in places.values():
        text = entry_text(place)
        lines.append(f"Location: {text or place['id']}")
    if doc.get("lighting"):
        lines.append(f"Lighting: {doc['lighting']}")
    if doc.get("palette"):
        lines.append("Palette: " + ", ".join(doc["palette"]))
    if cont.get("props") or cont.get("wardrobe"):
        lines.append("Visible: " + "; ".join(x for x in (", ".join(cont.get("props", [])), cont.get("wardrobe", "")) if x))
    if style:
        lines.append(f"Style: {style}")
    lines += ["Keep every character and location identical to the attached reference images.",
              "No text, captions, lyrics, logos or watermarks."]
    return "\n".join(x for x in lines if x)


def _sung(shot, doc):
    """The lyric text the shot answers — the words that start inside it."""
    words = []
    for a in doc.get("lyric_anchors", []):
        if a["start_ms"] < shot["out_ms"] and a["end_ms"] > shot["in_ms"]:
            words.append(str(a["text"]))
    return " ".join(words)


def video_prompt(p, shot, doc, length):
    """Motion-only prompt: the approved first frame already carries look and composition.

    Claudia port: a shot that answers lyric cues is a *sung* shot — it gets
    the lip-sync head quoting the exact window words and the "only the lead
    sings" closer (the measured copy winner over long screenplay prompts);
    every other shot is a *motion* clip and closes on the nobody-sings rule.
    """
    cast = _bible(p, "characters.yaml", "characters", set(shot.get("characters", [])))
    cam = doc.get("camera", {})
    cont = doc.get("continuity", {})
    clip = length["clip_seconds"]
    words = _sung(shot, doc)
    lines = ["Animate this approved first frame. Preserve its exact composition, style and character identities."]
    if words:
        lines.append(f"Sung shot: the lead lip-syncs the attached vocal exactly, every word in time: “{words}”")
    lines += [(f"Make a {clip} s clip; the first {length['seconds']} s are used." if clip
               else f"The shot is {length['seconds']} s long."),
              f"Scene: {doc['scene_goal']}" if doc.get("scene_goal") else shot.get("description", ""),
              f"Camera: {cam.get('type', 'locked')}" + (" — " + "; ".join(_beats(cam.get("moves", []), shot)) if cam.get("moves") else "")]
    for b in doc.get("blocking", []):
        name = cast.get(b["character"], {}).get("name") or b["character"]
        parts = [x for x in (b.get("pose"), b.get("expression")) if x]
        beats = _beats(b.get("motion_beats", []), shot)
        lines.append(f"{name}: " + "; ".join(parts + beats) if parts or beats else f"{name}: holds the pose")
    hits = _beats(doc.get("beat_map", []), shot, "hit")
    if hits:
        lines.append("On the music: " + "; ".join(hits))
    for a in doc.get("lyric_anchors", []):
        start, end = max(a["start_ms"], shot["in_ms"]), min(a["end_ms"], shot["out_ms"])
        lines.append(f"Lyric {_rel(start, shot)}–{_rel(end, shot)} s “{a['text']}”" + (f": {a['intent']}" if a.get("intent") else ""))
    if cont.get("props") or cont.get("wardrobe"):
        lines.append("Keep unchanged: " + "; ".join(x for x in (", ".join(cont.get("props", [])), cont.get("wardrobe", "")) if x))
    if doc.get("transition_out"):
        lines.append(f"Ends: {doc['transition_out']}")
    lines.append("One continuous shot, no cuts. " + (
        "Only the lead sings; every other mouth stays closed." if words
        else "Nobody's lips form words."))
    lines.append("No new text. No new scenes. Only the motion described above.")
    return "\n".join(x for x in lines if x)


def shot_brief(project, shot_id):
    """Everything a person needs to make this shot in a subscription app, pinned to the conti."""
    p = Path(project)
    shots = _shots(p)
    shot = _shot(p, shot_id)
    doc = load_conti(p, shot_id)
    aspect = aspect_ratio(read(p / "project.yaml")["format"])
    first = (shot.get("references") or [f"storyboard/{shot_id}.png"])[0]
    attach, missing = frame_references(p, shot)
    length = clip_length(shot)
    warnings = []
    if doc is None:
        warnings.append("no conti: prompts fall back to the shot manifest")
        frame, motion, criteria = None, build_prompt(p, shot), []
    else:
        i = next(n for n, s in enumerate(shots) if s["id"] == shot_id)
        gaps = missing_fields(doc, first=i == 0, last=i == len(shots) - 1, cast=shot.get("characters", []))
        if gaps:
            warnings.append("conti not READY: " + ", ".join(gaps))
        for rel in doc.get("references", []):
            (attach if safe_path(p, rel).is_file() else missing).append(rel)
        frame, motion, criteria = first_frame_prompt(p, shot, doc, aspect), video_prompt(p, shot, doc, length), list(doc.get("acceptance_criteria", []))
    if shot["render_mode"] == "STATIC":
        motion = None
    elif length["clip_seconds"] is None:
        warnings.append(f"{length['seconds']} s shot exceeds the {CLIP_STEPS[-1]} s clip limit: split it before generating")
    return {"shot_id": shot_id, "in_ms": shot["in_ms"], "out_ms": shot["out_ms"], **length,
            "aspect": aspect, "render_mode": shot["render_mode"], "start_frame": first,
            "frame_ready": shot.get("storyboard_kind") == "imported",
            "sung": bool(doc and _sung(shot, doc)),
            "first_frame_prompt": frame, "video_prompt": motion,
            "attach": list(dict.fromkeys(attach)), "missing_references": list(dict.fromkeys(missing)),
            "checklist": criteria, "conti_sha256": conti_digest(doc), "warnings": warnings}


# ---- take check-in -----------------------------------------------------------------

def _record_dir(p, shot_id):
    return safe_path(p, f"{QA_DIR}/{shot_id}")


def _record_path(p, shot_id, sha):
    return _record_dir(p, shot_id) / f"{sha[:16]}.json"


def _records(p, shot_id):
    folder = _record_dir(p, shot_id)
    rows = [read(f) for f in sorted(folder.glob("*.json"))] if folder.is_dir() else []
    return sorted((r for r in rows if isinstance(r, dict)), key=lambda r: (r.get("attempt", 0), r.get("checked_at", "")))


def _changed(before, after, path=""):
    """Field paths that differ between two conti documents (bookkeeping keys ignored)."""
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for k in sorted(set(before) | set(after)):
            if not path and k in {"updated_at", "source", "request_sha256"}:
                continue
            out += _changed(before.get(k), after.get(k), f"{path}.{k}" if path else k)
        return out
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        out = []
        for i, (x, y) in enumerate(zip(before, after)):
            out += _changed(x, y, f"{path}[{i}]")
        return out
    return [] if before == after else [path]


def record_revision(project, shot_id, instruction, note=""):
    """Store a user's verbatim revision instruction on the take it revises.

    The instruction lands in the latest checked take's own check record
    (``revision_request``) with the conti and both prompts as they stood, so
    no second record file appears under ``qc/conti/<shot>/``. The person (or
    the LLM in the conti discussion) then changes the conti field the
    instruction is about; the next take checked in carries ``revision``:
    the instruction, the revised take, the conti fields that changed and the
    prompt diff — the instruction → conti → prompt → take chain.
    """
    instruction = str(instruction or "").strip()
    if not instruction:
        raise FilmError("an instruction is required")
    p = Path(project)
    with project_mutex(p):
        require_legacy_profile(p)
        doc = load_conti(p, shot_id)
        if doc is None:
            raise FilmError(f"{shot_id}: no conti — settle direction first (conti-draft/conti-submit)")
        rows = _records(p, shot_id)
        if not rows:
            raise FilmError(f"{shot_id}: no checked take to revise — conti-check a take first")
        latest = rows[-1]
        if (latest.get("revision_request") or {}).get("fulfilled_by"):
            raise FilmError(f"{shot_id}: the latest take was already revised; check the new take first")
        _require_attempt_left(p, shot_id, latest)
        brief = shot_brief(p, shot_id)
        latest["revision_request"] = {
            "instruction": instruction[:2000], "note": str(note or "")[:600], "at": now(),
            "conti": doc, "conti_sha256": brief["conti_sha256"],
            "first_frame_prompt": brief["first_frame_prompt"], "video_prompt": brief["video_prompt"],
            "fulfilled_by": None}
        write(_record_path(p, shot_id, latest["take_sha256"]), latest)
    return {"shot_id": shot_id, "revises_take": latest["take_sha256"], "attempt": latest["attempt"],
            **{k: v for k, v in latest["revision_request"].items() if k != "conti"},
            "next": "change the conti field the instruction is about (conti-submit), import the new take, then conti-check"}


def attempt_cap(p):
    """project.yaml production.max_attempts_per_shot, or None when unset or invalid."""
    cap = production_block(p).get("max_attempts_per_shot")
    return cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else None


def _require_attempt_left(p, shot_id, record):
    cap = attempt_cap(p)
    if cap is not None and record["attempt"] >= cap:
        raise FilmError(f"{shot_id}: attempt {record['attempt']} is the per-shot cap {cap} "
                        "(project.yaml production.max_attempts_per_shot): accept the take, stop, "
                        "or raise the cap on purpose (python -m engine.cli production --max-attempts N)")


def _open_request(rows):
    """The newest check record holding an unfulfilled revision request, or None."""
    for r in reversed(rows):
        if (r.get("revision_request") or {}) and not r["revision_request"].get("fulfilled_by"):
            return r
    return None


def _take(p, shot_id, sha):
    entry = read(p / "manifest/assets.json", {"shots": {}}).get("shots", {}).get(shot_id, {})
    for kind in ("draft", "final"):
        rec = entry.get(kind)
        if rec and (sha is None or rec.get("sha256") == sha):
            return {"path": rec["path"], "sha256": rec["sha256"], "kind": kind}
    if sha and safe_path(p, f"render/assets/{sha}.mp4").is_file():
        return {"path": f"render/assets/{sha}.mp4", "sha256": sha, "kind": "stored"}
    raise FilmError(f"{shot_id}: no imported take" + (f" with sha256 {sha}" if sha else "") +
                    "; import one first (import-asset … --kind draft)")


def _sample(path, folder, count, fps):
    """Evenly spaced small JPEG samples, first to last frame. Returns the files written."""
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("frame_*.jpg"):
        old.unlink()
    indices = sorted(set(round((count - 1) * f) for f in SAMPLE_FRACTIONS))
    selection = "+".join(f"eq(n\\,{i})" for i in indices)
    ffmpeg(["-i", path, "-an", "-vf", f"select='{selection}',scale={SAMPLE_WIDTH}:-2",
            "-fps_mode", "vfr", folder / "frame_%02d.jpg"])
    return sorted(folder.glob("frame_*.jpg"))


def _stillness(frames):
    """Largest mean per-channel change between consecutive samples; None with fewer than two."""
    arrays = [np.asarray(Image.open(f).convert("RGB"), dtype=float) for f in frames]
    if len(arrays) < 2 or any(a.shape != arrays[0].shape for a in arrays):
        return None
    return round(max(float(np.abs(b - a).mean()) for a, b in zip(arrays, arrays[1:])), 3)


def _check(name, status, detail):
    return {"check": name, "status": status, "detail": detail}


def _overall(record):
    statuses = [c["status"] for c in record["mechanical"]] + [c["verdict"] for c in record["criteria"]]
    if "FAIL" in statuses:
        return "FAIL"
    if not record["criteria"] or "PENDING" in statuses:
        return "PENDING"
    return "PASS"


def check_take(project, shot_id, *, sha256=None, service="", units=None, human_minutes=None, note=""):
    """Check the shot's imported take against the shot and its conti; write the record.

    Mechanical checks decide FAIL on their own (too short, no visible motion on a
    motion shot). Aspect and resolution only warn — the compiler letterboxes and
    the person decides. Criteria start PENDING. Re-checking the same take keeps
    its attempt number; a new take is the next attempt.
    """
    p = Path(project)
    for name, value in (("units", units), ("human_minutes", human_minutes)):
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0):
            raise FilmError(f"{name} must be a nonnegative number")
    with project_mutex(p):
        require_legacy_profile(p)
        shot = _shot(p, shot_id)
        take = _take(p, shot_id, sha256)
        path = safe_path(p, take["path"])
        if not path.is_file():
            raise FilmError(f"{shot_id}: stored take is missing: {take['path']}")
        doc = load_conti(p, shot_id)
        fmt = read(p / "project.yaml")["format"]
        try:
            info = probe(path)
            video = next(s for s in info["streams"] if s.get("codec_type") == "video")
        except (FilmError, StopIteration, KeyError, ValueError):
            raise FilmError("영상 파일을 읽을 수 없습니다. 깨졌거나 영상이 아닙니다.") from None
        seconds = float(video.get("duration") or info.get("format", {}).get("duration") or 0)
        try:
            fps = float(Fraction(video.get("avg_frame_rate") or "0/1"))
        except (ValueError, ZeroDivisionError):
            fps = 0.0
        fps = fps or float(fmt["fps"])
        count = int(video.get("nb_frames") or 0) or max(1, round(seconds * fps))
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        need = shot["duration_ms"] / 1000
        want = fmt["width"] / fmt["height"]
        checks = [_check("DURATION", "PASS" if seconds + 1 / fps >= need else "FAIL",
                         f"{seconds:.2f} s clip for a {need:.2f} s shot"),
                  _check("ASPECT", "PASS" if width and height and abs(width / height - want) / want <= ASPECT_TOLERANCE else "WARN",
                         f"{width}×{height} vs project {aspect_ratio(fmt)}"),
                  _check("RESOLUTION", "PASS" if height >= fmt["height"] else "WARN",
                         f"{height}p clip for a {fmt['height']}p project")]
        folder = _record_dir(p, shot_id) / take["sha256"][:16]
        frames = _sample(path, folder, count, fps)
        change = _stillness(frames)
        if shot["render_mode"] == "STATIC":
            checks.append(_check("MOTION", "PASS", "static shot: motion not required"))
        elif change is None:
            checks.append(_check("MOTION", "WARN", "could not compare sampled frames"))
        else:
            checks.append(_check("MOTION", "PASS" if change > STILL_DIFF else "FAIL",
                                 f"largest change between samples {change} (threshold {STILL_DIFF})"))
        previous = _records(p, shot_id)
        same = next((r for r in previous if r.get("take_sha256") == take["sha256"]), None)
        attempt = same["attempt"] if same else 1 + max((r.get("attempt", 0) for r in previous), default=0)
        record = {"schema_version": SCHEMA_VERSION, "shot_id": shot_id,
                  "take_sha256": take["sha256"], "take_path": take["path"], "take_kind": take["kind"],
                  "conti_sha256": conti_digest(doc), "attempt": attempt,
                  "service": str(service or "")[:64], "units": units, "human_minutes": human_minutes,
                  "note": str(note or "")[:600], "checked_at": now(),
                  "probe": {"seconds": round(seconds, 2), "width": width, "height": height,
                            "fps": round(fps, 3), "frames": count},
                  "mechanical": checks,
                  "frames": [str(f.relative_to(p)) for f in frames],
                  "criteria": [{"index": i + 1, "text": c, "verdict": "PENDING", "reason": ""}
                               for i, c in enumerate((doc or {}).get("acceptance_criteria", []))],
                  "judgements": [], "reviewer": "",
                  "warnings": [] if doc else ["no conti: there are no acceptance criteria to judge"]}
        cap = attempt_cap(p)
        if cap is not None and attempt > cap:
            record["warnings"].append(f"attempt {attempt} is over the per-shot cap {cap} "
                                      "(project.yaml production.max_attempts_per_shot): stop or decide")
        source = _open_request(previous) if same is None else None
        if source is not None:
            req = source["revision_request"]
            brief = shot_brief(p, shot_id)
            changed = _changed(req.get("conti") or {}, doc or {})
            record["revision"] = {
                "instruction": req["instruction"], "requested_at": req["at"],
                "revises_take": source["take_sha256"], "revises_attempt": source["attempt"],
                "conti_before": req["conti_sha256"], "conti_after": conti_digest(doc),
                "conti_changed": changed,
                "prompt_diff": {k: "\n".join(difflib.unified_diff(
                    (req.get(k) or "").splitlines(), (brief[k] or "").splitlines(), lineterm="", n=1))
                    for k in ("first_frame_prompt", "video_prompt")}}
            if not changed:
                record["warnings"].append("revision: the conti did not change since the instruction — "
                                          "this take answers the same brief")
            req["fulfilled_by"] = take["sha256"]
            write(_record_path(p, shot_id, source["take_sha256"]), source)
        elif same is not None:
            for key in ("revision", "revision_request"):
                if same.get(key):
                    record[key] = same[key]
        record["verdict"] = _overall(record)
        write(_record_path(p, shot_id, take["sha256"]), record)
    return record


def load_check(project, shot_id, sha256=None):
    """The check record for one take (the latest attempt when no sha is given)."""
    p = Path(project)
    rows = _records(p, shot_id)
    if sha256:
        rows = [r for r in rows if r.get("take_sha256") == sha256]
    if not rows:
        raise FilmError(f"{shot_id}: no take has been checked yet (conti-check)")
    return rows[-1]


def record_verdicts(project, shot_id, sha256, verdicts, reviewer, note=""):
    """Settle criteria by index: {index: "PASS"|"FAIL"|"PENDING" | {"verdict":…, "reason":…}}."""
    if not str(reviewer or "").strip():
        raise FilmError("reviewer is required")
    if not isinstance(verdicts, dict) or not verdicts:
        raise FilmError("verdicts must map criterion indexes to PASS/FAIL/PENDING")
    p = Path(project)
    with project_mutex(p):
        record = load_check(p, shot_id, sha256)
        by_index = {c["index"]: c for c in record["criteria"]}
        applied = {}
        for key, value in verdicts.items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                raise FilmError(f"criterion index {key!r} is not a number") from None
            if index not in by_index:
                raise FilmError(f"{shot_id}: no criterion {index}")
            verdict, reason = (value.get("verdict"), value.get("reason", "")) if isinstance(value, dict) else (value, "")
            verdict = str(verdict or "").upper()
            if verdict not in VERDICTS:
                raise FilmError(f"criterion {index}: verdict must be one of {VERDICTS}")
            by_index[index].update(verdict=verdict, reason=str(reason or "")[:600])
            applied[str(index)] = verdict
        entry = {"at": now(), "reviewer": str(reviewer).strip()[:80],
                 "verdicts": applied, "note": str(note or "")[:600]}
        # The criteria on this record are the ones the take was checked
        # against. If the conti was revised since, flag the verdict rather
        # than let it read as an answer to the current conti.
        now_sha = conti_digest(load_conti(p, shot_id))
        if record.get("conti_sha256") != now_sha:
            entry["conti_changed"] = {"checked_against": record.get("conti_sha256"), "now": now_sha}
            warning = (f"verdict recorded against a conti that changed since this take was checked "
                       f"({str(record.get('conti_sha256'))[:12]} → {str(now_sha)[:12]}): it answers the criteria "
                       "as they were then — re-check the take (conti-check) or confirm the criteria still hold")
            if warning not in record.setdefault("warnings", []):
                record["warnings"].append(warning)
        record["judgements"].append(entry)
        record["reviewer"] = str(reviewer).strip()[:80]
        record["verdict"] = _overall(record)
        write(_record_path(p, shot_id, record["take_sha256"]), record)
    return record


# ---- the revision loop: failed criterion → rewrite request → new brief → attempt n+1 ----

REWRITE_SYSTEM = """You are revising ONE shot's conti (storyboard document) for a music video because a generated take failed its checks. Reply with ONE JSON object and nothing else.
- Return the shot's complete conti document (same shot_id, same JSON shape as current_conti), not a patch.
- Change only what is needed to fix the failures: typically blocking[].motion_beats, camera.moves, framing, lighting or pose, so that the prompts built from the conti steer the next take away from the failure.
- Keep every at_ms inside the shot's own range, use only the declared cast, and write text values in {language}.
- Do not weaken, delete or reword acceptance_criteria to make a failure pass. Edit a criterion only when it cannot be checked from the picture, and say so in open_questions.
- Put anything you cannot decide in open_questions; an empty list means the direction is settled.
Answer in exactly this JSON shape:
{schema}"""


def _failures(record):
    """What failed on a checked take: mechanical FAILs and FAILed criteria, with their reasons."""
    out = [{"kind": "mechanical", "check": c["check"], "detail": c["detail"]}
           for c in record.get("mechanical", []) if c["status"] == "FAIL"]
    out += [{"kind": "criterion", "index": c["index"], "text": c["text"], "reason": c.get("reason", "")}
            for c in record.get("criteria", []) if c["verdict"] == "FAIL"]
    return out


def _auto_instruction(failures):
    parts = [f"{f['check']} failed ({f['detail']})" if f["kind"] == "mechanical"
             else f"criterion {f['index']} failed: {f['text']}" + (f" — {f['reason']}" if f["reason"] else "")
             for f in failures]
    return "Auto: " + "; ".join(parts)


def rewrite_request(project, shot_id, language="English"):
    """The paste-out text that asks a subscription LLM to rewrite the conti after a FAILed take.

    Takes the shot's latest checked take, which must carry a FAIL (a mechanical
    check or a criterion), and refuses once the per-shot attempt cap
    (``production.max_attempts_per_shot``) is reached. It stores the failure as
    the take's ``revision_request`` (the same chain ``conti-revision`` writes),
    persists the request text under ``conti/requests/`` and returns it. The
    answer goes through ``conti-submit --request <id>``; the regenerated brief
    and the next ``conti-check`` are attempt n+1 and carry the revision record.
    """
    p = Path(project)
    require_legacy_profile(p)
    doc = load_conti(p, shot_id)
    if doc is None:
        raise FilmError(f"{shot_id}: no conti — settle direction first (conti-draft/conti-submit)")
    rows = _records(p, shot_id)
    if not rows:
        raise FilmError(f"{shot_id}: no checked take to revise — conti-check a take first")
    record = rows[-1]
    failures = _failures(record)
    if not failures:
        raise FilmError(f"{shot_id}: attempt {record['attempt']} has no FAIL (verdict {record['verdict']}) — "
                        "judge or confirm its criteria first (conti-judge-request / conti-verdict)")
    request = record.get("revision_request") or {}
    if not request:
        record_revision(p, shot_id, _auto_instruction(failures), note="auto: failed checks")
    elif request.get("fulfilled_by"):
        raise FilmError(f"{shot_id}: the latest take was already revised; check the new take first")
    else:
        _require_attempt_left(p, shot_id, record)
    brief = shot_brief(p, shot_id)
    cap = attempt_cap(p)
    payload = {"shot_id": shot_id, "attempt": record["attempt"], "next_attempt": record["attempt"] + 1,
               "attempts_left_after_this_one": None if cap is None else cap - record["attempt"] - 1,
               "shot_range_ms": [brief["in_ms"], brief["out_ms"]], "clip_seconds": brief["clip_seconds"],
               "failures": failures, "mechanical": record["mechanical"],
               "judge_notes": [j for j in record.get("judgements", []) if j.get("note")][-3:],
               "current_conti": {k: v for k, v in doc.items() if k not in {"updated_at", "source", "request_sha256"}},
               "current_prompts": {"first_frame": brief["first_frame_prompt"], "video": brief["video_prompt"]}}
    text = (f"{REWRITE_SYSTEM.format(language=language, schema=json.dumps(CONTI_SCHEMA['conti'][0], ensure_ascii=False))}"
            f"\n\n=== 입력 자료 (JSON) ===\n{json.dumps(payload, ensure_ascii=False)}\n\n"
            "위 지시대로 JSON 객체 하나만 답하세요. 설명 문장 없이, 코드 블록 하나 안에 넣어도 됩니다.")
    request_id, request_file = persist_request(p, text)
    with project_mutex(p):
        latest = _records(p, shot_id)[-1]
        latest["revision_request"]["rewrite_request_id"] = request_id
        write(_record_path(p, shot_id, latest["take_sha256"]), latest)
    return {"shot_id": shot_id, "attempt": record["attempt"], "next_attempt": record["attempt"] + 1,
            "attempts_left_after_this_one": payload["attempts_left_after_this_one"], "failures": failures,
            "request_id": request_id, "request_file": request_file, "text": text,
            "next": f"paste it to your LLM, save the JSON answer, then: conti-submit --file <answer> --request {request_id}; "
                    f"conti-brief {shot_id} for the new prompts; make and import take {record['attempt'] + 1}; conti-check"}


def revision_loop(project, shot_id):
    """Where a shot is in the check → rewrite → regenerate loop, computed from its records.

    Stages: NEEDS_TAKE, NEEDS_VERDICTS (criteria still PENDING), ACCEPTED (PASS),
    NEEDS_REWRITE (FAIL, attempts left), CAP_REACHED (FAIL at the attempt cap:
    stop or decide), AWAITING_CONTI_CHANGE (rewrite requested, conti not yet
    changed), AWAITING_TAKE (conti changed: the new brief is ready for attempt n+1).
    """
    p = Path(project)
    _shot(p, shot_id)
    cap = attempt_cap(p)
    rows = _records(p, shot_id)
    base = {"shot_id": shot_id, "cap": cap, "attempt": 0, "verdict": None}
    if not rows:
        return {**base, "stage": "NEEDS_TAKE", "next": f"import a take, then conti-check {shot_id}"}
    latest = rows[-1]
    attempt, verdict = latest["attempt"], latest["verdict"]
    base.update(attempt=attempt, verdict=verdict, take_sha256=latest["take_sha256"])
    now_sha = conti_digest(load_conti(p, shot_id))
    request = latest.get("revision_request") or {}
    if request and not request.get("fulfilled_by"):
        if request.get("conti_sha256") == now_sha:
            return {**base, "stage": "AWAITING_CONTI_CHANGE", "rewrite_request_id": request.get("rewrite_request_id"),
                    "next": "answer the rewrite request (conti-submit --request <id>) or edit the conti field"}
        return {**base, "stage": "AWAITING_TAKE", "next_attempt": attempt + 1,
                "next": f"conti changed: conti-brief {shot_id}, make and import take {attempt + 1}, conti-check"}
    if verdict == "PASS":
        return {**base, "stage": "ACCEPTED", "next": "accepted — nothing left for this shot"}
    if verdict == "FAIL":
        if cap is not None and attempt >= cap:
            return {**base, "stage": "CAP_REACHED",
                    "next": f"attempt {attempt} is the cap {cap}: accept it, stop, or raise production.max_attempts_per_shot"}
        return {**base, "stage": "NEEDS_REWRITE", "failures": _failures(latest),
                "next": f"conti-rewrite-request {shot_id}"}
    return {**base, "stage": "NEEDS_VERDICTS", "next": f"conti-judge-request {shot_id} (or conti-verdict)"}


# ---- the vision judge: paste out, paste back ----------------------------------------

def judge_request(project, shot_id, sha256=None):
    """Text for a vision-capable LLM: the criteria, the brief and the sampled frames to attach.

    The request text is persisted beside the take record
    (``<take sha16>.judge-<request sha16>.txt``) so a pasted answer can always
    be re-read against what was asked.
    """
    p = Path(project)
    record = load_check(p, shot_id, sha256)
    if not record["criteria"]:
        raise FilmError(f"{shot_id}: the conti has no acceptance_criteria to judge")
    brief = shot_brief(p, shot_id)
    payload = {"shot_id": shot_id, "take": record["take_sha256"],
               "clip": record["probe"], "mechanical": record["mechanical"],
               "brief": {"scene": brief["video_prompt"] or brief["first_frame_prompt"], "seconds": brief["seconds"]},
               "criteria": [{"index": c["index"], "text": c["text"]} for c in record["criteria"]],
               "frames": record["frames"]}
    attach = [str(safe_path(p, f)) for f in record["frames"]]
    text = (f"{JUDGE_SYSTEM}\n\n=== 입력 자료 (JSON) ===\n{json.dumps(payload, ensure_ascii=False)}\n\n"
            "=== 함께 올릴 프레임 ===\n" + "\n".join(attach) +
            "\n\n위 지시대로 JSON 객체 하나만 답하세요. 설명 문장 없이, 코드 블록 하나 안에 넣어도 됩니다.")
    # Not *.json: _records loads every *.json here as a take check record.
    request_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    request_path = _record_dir(p, shot_id) / f"{record['take_sha256'][:16]}.judge-{request_sha[:16]}.txt"
    with project_mutex(p):
        atomic_text(request_path, text)
    return {"shot_id": shot_id, "take": record["take_sha256"], "attach": attach,
            "request_file": str(request_path.relative_to(p)), "text": text}


def submit_judgement(project, shot_id, answer, sha256=None, source="llm"):
    """Store a pasted judge answer. UNSURE stays PENDING; the person confirms only those."""
    p = Path(project)
    data = extract_json(answer) if isinstance(answer, str) else answer
    if not isinstance(data, dict) or not isinstance(data.get("criteria"), list) or not data["criteria"]:
        raise FilmError("The answer must hold a non-empty 'criteria' list")
    record = load_check(p, shot_id, sha256)
    if data.get("take") and data["take"] != record["take_sha256"]:
        raise FilmError("The answer names a different take than the one being judged")
    verdicts = {}
    for row in data["criteria"]:
        if not isinstance(row, dict) or "index" not in row:
            raise FilmError("Each criteria entry needs index and verdict")
        verdict = ANSWER_VERDICTS.get(str(row.get("verdict", "")).upper())
        if verdict is None:
            raise FilmError(f"criterion {row['index']}: verdict must be PASS, FAIL or UNSURE")
        verdicts[row["index"]] = {"verdict": verdict, "reason": row.get("reason", "")}
    return record_verdicts(p, shot_id, record["take_sha256"], verdicts, f"{source}", note="judge answer")


# ---- the ledger ------------------------------------------------------------------

def conti_metrics(project):
    """Per shot and in total: attempts, latest verdict, subscription units and human minutes."""
    p = Path(project)
    rows, totals = [], {"shots_checked": 0, "attempts": 0, "passed": 0, "units": 0.0, "human_minutes": 0.0}
    for shot in _shots(p):
        records = _records(p, shot["id"])
        if not records:
            rows.append({"shot_id": shot["id"], "attempts": 0, "verdict": None, "units": 0.0, "human_minutes": 0.0})
            continue
        latest = records[-1]
        units = sum(float(r["units"]) for r in records if r.get("units") is not None)
        minutes = sum(float(r["human_minutes"]) for r in records if r.get("human_minutes") is not None)
        rows.append({"shot_id": shot["id"], "attempts": len(records), "verdict": latest["verdict"],
                     "take_sha256": latest["take_sha256"], "units": units, "human_minutes": minutes,
                     "revisions": sum(1 for r in records if r.get("revision"))})
        totals["shots_checked"] += 1
        totals["attempts"] += len(records)
        totals["passed"] += latest["verdict"] == "PASS"
        totals["units"] += units
        totals["human_minutes"] += minutes
    return {"totals": totals, "shots": rows}
