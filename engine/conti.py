"""Detailed per-shot conti (storyboard) documents — the W00 redesign's first slice.

One ``conti/S<id>.json`` per shot: what the shot must achieve, which lyric cues
and measured beats it answers, framing, camera moves, per-character blocking,
lighting, palette, continuity, references, transitions, concrete acceptance
criteria and open questions. A human and an LLM fill it in discussion; FILM
UNIT only structures, validates and stores it.

A conti is a working document, not an approval. It never changes the shot
manifest, lyric text, lyric timing, reviews or LOCK, and it makes no network
call and no paid request. The paste-out discussion request follows the same
rule as the browser hand-off: the User opens the service in their own browser.
"""
import hashlib
import json
import re
from pathlib import Path

from .core import FilmError, atomic_text, now, project_mutex, read, require_legacy_profile, safe_path, timecode, write
from .director import extract_json

SCHEMA_VERSION = 1
CONTI_DIR = "conti"
MAX_ANCHORS = 24
MAX_BEATS = 32
MAX_BLOCKING = 8
MAX_ITEMS = 24
KEYS = {"schema_version", "shot_id", "updated_at", "source", "scene_goal",
        "lyric_anchors", "beat_map", "framing", "camera", "blocking",
        "lighting", "palette", "continuity", "references", "transition_in",
        "transition_out", "acceptance_criteria", "open_questions", "request_sha256"}
REQUESTS_DIR = "conti/requests"
CONTINUITY_KEYS = {"from_previous", "to_next", "props", "wardrobe"}

CONTI_SCHEMA = {
    "conti": [{
        "schema_version": 1,
        "shot_id": "S001",
        "scene_goal": "what this shot must accomplish, concretely",
        "lyric_anchors": [{"cue_id": "optional", "text": "the lyric line it answers",
                           "start_ms": 0, "end_ms": 0, "intent": "how the picture meets the line"}],
        "beat_map": [{"at_ms": 0, "hit": "what visibly happens on this beat"}],
        "framing": "shot size, angle, lens feel",
        "camera": {"type": "locked|handheld|dolly|pan|tilt|zoom",
                   "moves": [{"at_ms": 0, "action": "camera move starting here"}]},
        "blocking": [{"character": "CHAR_A", "pose": "", "expression": "",
                      "motion_beats": [{"at_ms": 0, "action": "visible motion starting here"}]}],
        "lighting": "sources, direction, mood",
        "palette": ["#RRGGBB or palette name"],
        "continuity": {"from_previous": "what carries over from the shot before",
                       "to_next": "what the next shot must pick up",
                       "props": ["visible prop"], "wardrobe": ""},
        "references": ["project-relative path to a reference image"],
        "transition_in": "how this shot starts (hard cut, match cut, …)",
        "transition_out": "how it ends into the next shot",
        "acceptance_criteria": ["a checkable statement a reviewer can mark pass/fail"],
        "open_questions": []
    }]
}

# Stated to the model because validate_conti rejects the whole answer for a longer value.
LIMITS_NOTE = ("Length limits are enforced and one longer value rejects the whole answer: scene_goal up to 600 characters; "
               "actions, beat hits and transitions up to 200; every other text value, each acceptance_criteria entry and "
               "each open_questions entry included, up to 300; props and cue_id up to 80; palette entries up to 40; camera.type up to 30; at most 24 items per list and 8 blocking entries.")

SYSTEM = """You are the storyboard director's partner for a music video. Reply with ONE JSON object and nothing else.
- Discuss in maximal detail, then answer one conti object per requested shot, exactly the listed ids, in the given JSON shape.
- Never invent lyric text or timing: use only the timed cues and beat times supplied. Anchor only inside each shot's own range.
- Use only the declared character and location ids. Keep continuity with the previous and next shots named in each entry.
- acceptance_criteria must be concrete checkable statements (what a reviewer can mark pass/fail), not mood words.
- open_questions lists anything still undecided; an empty list means the shot's direction is settled.
- Write every text value in {language}.
- {limits}
Answer in exactly this JSON shape:
{schema}""".replace("{limits}", LIMITS_NOTE)


# ---- structure ----------------------------------------------------------------

def _text(value, name, low=0, high=600):
    if not isinstance(value, str):
        raise FilmError(f"{name}: text is required")
    value = value.strip()
    if not low <= len(value) <= high:
        raise FilmError(f"{name}: {low}-{high} characters are required")
    return value


def _list(value, name, cap=MAX_ITEMS):
    if not isinstance(value, list) or len(value) > cap:
        raise FilmError(f"{name}: a list of at most {cap} entries is required")
    return value


def _ms(value, name, in_ms, out_ms):
    if type(value) is not int or not in_ms <= value < out_ms:
        raise FilmError(f"{name}: {value!r} is outside the shot's {in_ms}–{out_ms - 1} ms range")
    return value


def _beats(value, name, in_ms, out_ms, cap=MAX_ITEMS):
    rows = _list(value, name, cap)
    out = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) - {"at_ms", "action", "hit"}:
            raise FilmError(f"{name}[{i}]: an object with at_ms and action/hit only")
        out.append({"at_ms": _ms(row.get("at_ms"), f"{name}[{i}].at_ms", in_ms, out_ms),
                    "action": _text(row.get("action", row.get("hit", "")), f"{name}[{i}].action", 0, 200)})
    return out


def validate_conti(shot, doc):
    """Check one conti document against its manifest shot; return the normalized doc."""
    if not isinstance(doc, dict):
        raise FilmError("A conti entry must be an object")
    unknown = set(doc) - KEYS
    if unknown:
        raise FilmError(f"Unknown conti fields: {sorted(unknown)}")
    sid = shot["id"]
    if doc.get("shot_id") != sid or doc.get("schema_version") != SCHEMA_VERSION:
        raise FilmError(f"{sid}: conti needs shot_id={sid} and schema_version={SCHEMA_VERSION}")
    in_ms, out_ms = shot["in_ms"], shot["out_ms"]

    anchors = _list(doc.get("lyric_anchors", []), "lyric_anchors", MAX_ANCHORS)
    clean_anchors = []
    for i, a in enumerate(anchors):
        if not isinstance(a, dict) or set(a) - {"cue_id", "text", "start_ms", "end_ms", "intent"}:
            raise FilmError(f"lyric_anchors[{i}]: only cue_id/text/start_ms/end_ms/intent")
        start, end = a.get("start_ms"), a.get("end_ms")
        if type(start) is not int or type(end) is not int or not start < end or start >= out_ms or end <= in_ms:
            raise FilmError(f"lyric_anchors[{i}]: needs a start_ms < end_ms range overlapping the shot")
        clean_anchors.append({**({"cue_id": _text(a["cue_id"], f"lyric_anchors[{i}].cue_id", 1, 80)} if a.get("cue_id") else {}),
                             "text": _text(a.get("text"), f"lyric_anchors[{i}].text", 1, 300),
                             "start_ms": start, "end_ms": end,
                             "intent": _text(a.get("intent", ""), f"lyric_anchors[{i}].intent", 0, 300)})

    camera = doc.get("camera") or {}
    if not isinstance(camera, dict) or set(camera) - {"type", "moves"}:
        raise FilmError("camera: an object with type and moves only")

    blocking = _list(doc.get("blocking", []), "blocking", MAX_BLOCKING)
    cast = set(shot.get("characters", []))
    clean_blocking, seen = [], set()
    for i, b in enumerate(blocking):
        if not isinstance(b, dict) or set(b) - {"character", "pose", "expression", "motion_beats"}:
            raise FilmError(f"blocking[{i}]: only character/pose/expression/motion_beats")
        who = _text(b.get("character"), f"blocking[{i}].character", 1, 30)
        if who not in cast:
            raise FilmError(f"blocking[{i}]: {who} is not in {sid}'s character list")
        if who in seen:
            raise FilmError(f"blocking[{i}]: {who} appears twice")
        seen.add(who)
        clean_blocking.append({"character": who,
                               "pose": _text(b.get("pose", ""), f"blocking[{i}].pose", 0, 300),
                               "expression": _text(b.get("expression", ""), f"blocking[{i}].expression", 0, 300),
                               "motion_beats": _beats(b.get("motion_beats", []), f"blocking[{i}].motion_beats", in_ms, out_ms)})

    continuity = doc.get("continuity") or {}
    if not isinstance(continuity, dict) or set(continuity) - CONTINUITY_KEYS:
        raise FilmError("continuity: an object with from_previous/to_next/props/wardrobe only")

    references = _list(doc.get("references", []), "references", MAX_ITEMS)
    clean_refs = []
    for r in references:
        rel = _text(r, "references entry", 1, 300)
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise FilmError("references must be project-relative paths")
        clean_refs.append(rel)

    request_sha = doc.get("request_sha256")
    if request_sha is not None and not (isinstance(request_sha, str) and re.fullmatch(r"[0-9a-f]{64}", request_sha)):
        raise FilmError(f"{sid}: request_sha256 must be a sha256 hex digest")
    return {"schema_version": SCHEMA_VERSION, "shot_id": sid,
            **({"request_sha256": request_sha} if request_sha else {}),
            "updated_at": _text(doc.get("updated_at", ""), "updated_at", 0, 60),
            "source": _text(doc.get("source", "manual"), "source", 0, 64) or "manual",
            "scene_goal": _text(doc.get("scene_goal", ""), "scene_goal", 0, 600),
            "lyric_anchors": clean_anchors,
            "beat_map": [{"at_ms": b["at_ms"], "hit": b["action"]}
                         for b in _beats(doc.get("beat_map", []), "beat_map", in_ms, out_ms, MAX_BEATS)],
            "framing": _text(doc.get("framing", ""), "framing", 0, 300),
            "camera": {"type": _text(camera.get("type", "locked"), "camera.type", 0, 30) or "locked",
                       "moves": _beats(camera.get("moves", []), "camera.moves", in_ms, out_ms)},
            "blocking": clean_blocking,
            "lighting": _text(doc.get("lighting", ""), "lighting", 0, 300),
            "palette": [_text(c, "palette entry", 1, 40) for c in _list(doc.get("palette", []), "palette")],
            "continuity": {"from_previous": _text(continuity.get("from_previous", ""), "continuity.from_previous", 0, 300),
                           "to_next": _text(continuity.get("to_next", ""), "continuity.to_next", 0, 300),
                           "props": [_text(x, "continuity.props entry", 1, 80)
                                     for x in _list(continuity.get("props", []), "continuity.props")],
                           "wardrobe": _text(continuity.get("wardrobe", ""), "continuity.wardrobe", 0, 300)},
            "references": clean_refs,
            "transition_in": _text(doc.get("transition_in", ""), "transition_in", 0, 200),
            "transition_out": _text(doc.get("transition_out", ""), "transition_out", 0, 200),
            "acceptance_criteria": [_text(c, "acceptance_criteria entry", 3, 300)
                                    for c in _list(doc.get("acceptance_criteria", []), "acceptance_criteria")],
            "open_questions": [_text(q, "open_questions entry", 3, 300)
                               for q in _list(doc.get("open_questions", []), "open_questions")]}


# ---- completeness --------------------------------------------------------------

def missing_fields(doc, first=False, last=False, cast=()):
    """What still keeps this shot's direction unsettled. Order matches the document."""
    missing = []
    if not doc.get("scene_goal"):
        missing.append("scene_goal")
    if not doc.get("framing"):
        missing.append("framing")
    if not doc.get("lighting"):
        missing.append("lighting")
    blocked = {b.get("character") for b in doc.get("blocking", [])}
    for who in cast:
        if who not in blocked:
            missing.append(f"blocking.{who}")
    for b in doc.get("blocking", []):
        if not b.get("pose") or not b.get("expression"):
            missing.append(f"blocking.{b['character']}.detail")
    if not first and not doc.get("transition_in"):
        missing.append("transition_in")
    if not last and not doc.get("transition_out"):
        missing.append("transition_out")
    if not doc.get("acceptance_criteria"):
        missing.append("acceptance_criteria")
    if doc.get("open_questions"):
        missing.append("open_questions")
    return missing


# ---- project access -------------------------------------------------------------

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


def conti_path(p, shot_id):
    return safe_path(p, f"{CONTI_DIR}/{shot_id}.json")


def load_conti(project, shot_id):
    path = conti_path(Path(project), shot_id)
    return read(path) if path.exists() else None


def _pick(p, shot_ids):
    shots = _shots(p)
    if shot_ids is None:
        return shots
    wanted = list(shot_ids)
    known = {s["id"] for s in shots}
    bad = [s for s in wanted if s not in known]
    if bad:
        raise FilmError("Unknown shot IDs: " + ", ".join(bad))
    return [s for s in shots if s["id"] in set(wanted)]


def draft_conti(project, shot_ids=None):
    """Write a structural skeleton per shot, seeded from the manifest, timed
    lyrics and measured beats. Never overwrites an existing conti."""
    p = Path(project)
    with project_mutex(p):
        require_legacy_profile(p)
        picked = _pick(p, shot_ids)
        cues = read(p / "lyrics/lyrics_timed.json", {}).get("cues", [])
        beats = read(p / "analysis/audio.json", {}).get("beat_times_ms", [])
        written, kept = [], []
        for shot in picked:
            if conti_path(p, shot["id"]).exists():
                kept.append(shot["id"])
                continue
            doc = {"schema_version": SCHEMA_VERSION, "shot_id": shot["id"],
                   "updated_at": now(), "source": "draft", "scene_goal": "",
                   "lyric_anchors": [{"cue_id": c.get("id", ""), "text": c.get("text", ""),
                                      "start_ms": c["start_ms"], "end_ms": c["end_ms"], "intent": ""}
                                     for c in cues
                                     if isinstance(c, dict)
                                     and type(c.get("start_ms")) is int and type(c.get("end_ms")) is int
                                     and c["start_ms"] < shot["out_ms"] and c["end_ms"] > shot["in_ms"]],
                   "beat_map": [{"at_ms": b, "hit": ""} for b in beats
                                if type(b) is int and shot["in_ms"] <= b < shot["out_ms"]][:MAX_BEATS],
                   "framing": shot.get("composition", ""),
                   "camera": {"type": (shot.get("camera") or {}).get("type", "locked"), "moves": []},
                   "blocking": [{"character": c, "pose": "", "expression": "", "motion_beats": []}
                                for c in shot.get("characters", [])],
                   "lighting": "", "palette": [],
                   "continuity": {"from_previous": "", "to_next": "", "props": [], "wardrobe": ""},
                   "references": [r for r in shot.get("references", []) if isinstance(r, str)],
                   "transition_in": "", "transition_out": "",
                   "acceptance_criteria": [], "open_questions": []}
            write(conti_path(p, shot["id"]), doc)
            written.append(shot["id"])
    return {"written": written, "kept": kept}


def submit_conti(project, doc, source="manual"):
    """Validate one conti document and store it. Returns the shot's status row."""
    p = Path(project)
    with project_mutex(p):
        require_legacy_profile(p)
        sid = doc.get("shot_id") if isinstance(doc, dict) else None
        shot = _shot(p, sid)
        clean = validate_conti(shot, doc)
        clean["source"], clean["updated_at"] = source, now()
        write(conti_path(p, sid), clean)
    return _row(p, shot, clean)


def _row(p, shot, doc):
    shots = _shots(p)
    i = next(n for n, s in enumerate(shots) if s["id"] == shot["id"])
    missing = missing_fields(doc, first=i == 0, last=i == len(shots) - 1, cast=shot.get("characters", []))
    return {"shot_id": shot["id"], "ready": not missing, "missing": missing}


def request_sha(project, request_id):
    """The full sha256 of a persisted conti request, verified against its bytes."""
    p = Path(project)
    rid = str(request_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{16}", rid):
        raise FilmError("request id must be the 16-hex id conti-request printed")
    path = safe_path(p, f"{REQUESTS_DIR}/{rid}.txt")
    if not path.is_file():
        raise FilmError(f"no persisted conti request {rid} under {REQUESTS_DIR}/")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if sha[:16] != rid:
        raise FilmError(f"persisted conti request {rid} was edited after it was written")
    return sha


def persist_request(project, text):
    """Store a paste-out request text under its own hash; returns (id, project-relative file).

    ``submit_answer(..., request_id=id)`` links an answer back to it, whichever
    request (conti discussion, revision rewrite) the text came from.
    """
    p = Path(project)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    path = safe_path(p, f"{REQUESTS_DIR}/{sha[:16]}.txt")
    if not path.exists():
        with project_mutex(p):
            atomic_text(path, text)
    return sha[:16], f"{REQUESTS_DIR}/{sha[:16]}.txt"


def submit_answer(project, answer, source="llm", request_id=None):
    """Store every conti in a discussion answer: {"conti": [...]} or one bare doc.

    The whole batch validates before anything is written, so a bad entry never
    leaves a half-updated set. With ``request_id`` (printed by conti-request)
    each stored conti records ``request_sha256`` — the exact request text that
    produced it stays readable at ``conti/requests/<id>.txt``.
    """
    p = Path(project)
    linked = request_sha(p, request_id) if request_id else None
    data = extract_json(answer) if isinstance(answer, str) else answer
    docs = (data.get("conti") if isinstance(data, dict) and "conti" in data
            else [data] if isinstance(data, dict) and data.get("shot_id") else None)
    if not isinstance(docs, list) or not docs:
        raise FilmError("The answer must hold a non-empty 'conti' list")
    with project_mutex(p):
        require_legacy_profile(p)
        picked = [(validate_conti(_shot(p, d.get("shot_id") if isinstance(d, dict) else None),
                                  {**d, "request_sha256": linked} if linked and isinstance(d, dict) else d), source)
                  for d in docs]
        for clean, src in picked:
            clean["source"], clean["updated_at"] = src, now()
            write(conti_path(p, clean["shot_id"]), clean)
    return [_row(p, _shot(p, c["shot_id"]), c) for c, _ in picked]


def conti_status(project):
    """Per shot: does a conti exist, and what still keeps it from READY."""
    p = Path(project)
    rows = []
    for shot in _shots(p):
        doc = load_conti(p, shot["id"])
        row = _row(p, shot, doc) if doc else {"shot_id": shot["id"], "ready": False, "missing": ["conti"]}
        row["has_conti"] = doc is not None
        rows.append(row)
    return {"total": len(rows), "ready": sum(r["ready"] for r in rows), "shots": rows}


# ---- the paste-out discussion request ---------------------------------------------

def _bible_row(p, filename, key, wanted):
    rows = read(p / "bible" / filename, {}).get(key, [])
    return [r for r in rows if isinstance(r, dict) and r.get("id") in wanted]


def conti_request(project, shot_ids=None, language="English"):
    """The text to paste into a chat service for the detailed-conti discussion.

    Carries each shot's manifest fields, its overlapping timed cues and
    measured beats, the bible rows it uses, its neighbours' direction, and any
    conti already drafted — everything the model needs, nothing it must guess.
    """
    p = Path(project)
    require_legacy_profile(p)
    shots = _shots(p)
    picked = _pick(p, shot_ids)
    cues = read(p / "lyrics/lyrics_timed.json", {}).get("cues", [])
    beats = read(p / "analysis/audio.json", {}).get("beat_times_ms", [])
    style = read(p / "bible/style_bible.yaml", {})
    order = {s["id"]: i for i, s in enumerate(shots)}
    entries = []
    for shot in picked:
        i = order[shot["id"]]
        neighbours = {}
        if i > 0:
            prev, prior = shots[i - 1], load_conti(p, shots[i - 1]["id"]) or {}
            neighbours["previous"] = {"id": prev["id"], "description": prev.get("description", ""),
                                      "scene_goal": prior.get("scene_goal", ""),
                                      "transition_out": prior.get("transition_out", "")}
        if i + 1 < len(shots):
            nxt, after = shots[i + 1], load_conti(p, shots[i + 1]["id"]) or {}
            neighbours["next"] = {"id": nxt["id"], "description": nxt.get("description", ""),
                                  "scene_goal": after.get("scene_goal", ""),
                                  "transition_in": after.get("transition_in", "")}
        entries.append({"id": shot["id"],
                        "start": timecode(shot["in_ms"]), "end": timecode(shot["out_ms"]),
                        "in_ms": shot["in_ms"], "out_ms": shot["out_ms"],
                        "description": shot.get("description", ""),
                        "composition": shot.get("composition", ""),
                        "camera": shot.get("camera", {}), "motion": shot.get("motion", {}),
                        "render_mode": shot.get("render_mode"),
                        "timed_lyrics": [{"cue_id": c.get("id"), "text": c.get("text"),
                                          "start_ms": c["start_ms"], "end_ms": c["end_ms"]}
                                         for c in cues if isinstance(c, dict)
                                         and type(c.get("start_ms")) is int
                                         and c["start_ms"] < shot["out_ms"] and c["end_ms"] > shot["in_ms"]],
                        "beat_times_ms": [b for b in beats if type(b) is int and shot["in_ms"] <= b < shot["out_ms"]][:MAX_BEATS],
                        "characters": _bible_row(p, "characters.yaml", "characters", set(shot.get("characters", []))),
                        "locations": _bible_row(p, "locations.yaml", "locations", set(shot.get("locations", []))),
                        "references": shot.get("references", []),
                        "neighbours": neighbours,
                        "existing_conti": load_conti(p, shot["id"])})
    payload = {"brief": (p / "input/brief.md").read_text(encoding="utf-8") if (p / "input/brief.md").exists() else "",
               "style": {"visual_style": style.get("visual_style", {}), "palette": style.get("palette", {}),
                         "rules": style.get("rules", [])},
               "shots": entries}
    system = SYSTEM.format(language=language, schema=json.dumps(CONTI_SCHEMA, ensure_ascii=False))
    text = (f"{system}\n\n=== 입력 자료 (JSON) ===\n{json.dumps(payload, ensure_ascii=False)}\n\n"
            "위 지시대로 JSON 객체 하나만 답하세요. 설명 문장 없이, 코드 블록 하나 안에 넣어도 됩니다.")
    # Persist the exact request text (Claudia port, docs/CLAUDIA_PORT.md §1.1):
    # keyed by its own hash because one request can cover several shots;
    # conti-submit --request <id> links each answer back to it. Nothing scans
    # conti/ — answers are opened by shot path only.
    request_id, request_file = persist_request(p, text)
    return {"ids": [s["id"] for s in picked], "text": text,
            "request_id": request_id, "request_file": request_file}
