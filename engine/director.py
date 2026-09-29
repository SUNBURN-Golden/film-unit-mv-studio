"""AI director: drafts story, cast, locations and per-shot direction for the User to review.

Two phases keep each answer short enough for small free models: first the world
(story, style, characters, locations), then the shots in batches. Nothing is
written into the production files until the User accepts the proposal, and the
director never touches cut timing, lyric text, lyric timing, reviews or LOCK.
"""
import hashlib
import json
from pathlib import Path
import re

from .core import FilmError, atomic_text, now, object_hash, project_mutex, read, safe_path, timecode, write

RENDER_MODES = {"STATIC", "LIMITED_MOTION", "FULL_GENERATIVE"}
COMPLEXITY = {"low", "medium", "high"}
CHAR_ID = re.compile(r"CHAR_[A-Z0-9_]{1,20}")
LOC_ID = re.compile(r"LOC_[A-Z0-9_]{1,24}")
BATCH = 12
PROPOSAL = "bible/director_proposal.json"
LOG = "bible/director_log.json"

WORLD_SCHEMA = {
    "story": "3-8 sentences in Korean: what happens across the whole song and how it ends",
    "style": {"visual_style": {"medium": "", "background": "", "line": "", "shading": "", "camera": ""},
              "palette": {"name": "#RRGGBB"}, "rules": ["short visual rule"]},
    "characters": [{"id": "CHAR_A", "name": "", "look": "fixed visual traits repeated in every shot: age, face, hair, clothing colors, distinctive items",
                    "behavior": "how this person moves and emotes", "invariants": {"trait": "value"}, "variants": []}],
    "locations": [{"id": "LOC_A", "description": "fixed visual traits of this place", "required_views": ["wide", "medium", "prop"]}],
}
SHOT_SCHEMA = {"shots": [{"id": "S001", "description": "one concrete visible action or emotion",
                          "characters": ["CHAR_A"], "locations": ["LOC_A"], "composition": "framing and layout",
                          "camera": {"type": "locked|handheld|dolly|pan|tilt|zoom", "movement": "none or a short phrase"},
                          "motion": {"complexity": "low|medium|high", "instruction": "what visibly moves and when"},
                          "render_mode": "STATIC|LIMITED_MOTION|FULL_GENERATIVE"}]}

WORLD_SYSTEM = """You are the director's assistant for a music-video compiler. Reply with ONE JSON object and nothing else.
- Never translate, rewrite or invent lyrics, and never assign timing to lyrics.
- Do not use real people, brands or existing copyrighted characters.
- Character ids look like CHAR_A, location ids like LOC_ROOM (uppercase letters, digits, underscores).
- Use 1 to 6 characters and 1 to 8 locations, only what the story needs.
- Write story in Korean. Write every other text value in {language}, because those values become image and video prompts.
- Each character's look must be concrete and repeatable: the same details will be pasted into every shot.
{constraints}
Answer in exactly this JSON shape:
{schema}"""

SHOTS_SYSTEM = """You are the director's assistant for a music-video compiler. Reply with ONE JSON object and nothing else.
You direct exactly the shots listed by the user, using only the character and location ids from the given world.
- Every shot needs a concrete, visible action or emotion, not a mood word. Keep continuity with neighbouring shots.
- render_mode: LIMITED_MOTION for restrained animation, FULL_GENERATIVE for full action, STATIC only when a held still image is a deliberate creative choice.
- For LIMITED_MOTION and FULL_GENERATIVE, motion.instruction must say what visibly moves and when.
- Never change ids, timing or lyrics. Do not add or skip shots.
- Write every text value in {language}.
{constraints}
Answer in exactly this JSON shape:
{schema}"""


def extract_json(text):
    """The first JSON object in the answer, tolerating code fences and chatter."""
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise FilmError("모델이 JSON을 돌려주지 않았습니다. 다른 모델을 고르거나 다시 시도하세요.")
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        raise FilmError("모델의 답이 올바른 JSON이 아닙니다. 다시 시도하거나 다른 모델을 고르세요.") from None
    if not isinstance(value, dict):
        raise FilmError("The answer must be a JSON object")
    return value


def _text(value, name, low=1, high=1200):
    if not isinstance(value, str) or not low <= len(value.strip()) <= high:
        raise FilmError(f"{name}: text of {low}-{high} characters is required")
    return value.strip()


def validate_world(data):
    story = _text(data.get("story"), "story", 20, 8000)
    style = data.get("style")
    if not isinstance(style, dict) or not isinstance(style.get("visual_style"), dict):
        raise FilmError("style.visual_style must be an object")
    visual = {str(k): _text(str(v), f"visual_style.{k}", 1, 300) for k, v in style["visual_style"].items() if str(v).strip()}
    if not visual:
        raise FilmError("style.visual_style is empty")
    palette = style.get("palette", {})
    rules = style.get("rules", [])
    if not isinstance(palette, dict) or not isinstance(rules, list) or len(rules) > 12:
        raise FilmError("style.palette must be an object and style.rules a list of at most 12")
    characters, locations = [], []
    for entry in data.get("characters") or []:
        if not isinstance(entry, dict) or not CHAR_ID.fullmatch(str(entry.get("id", ""))):
            raise FilmError("Each character needs an id like CHAR_A")
        traits = entry.get("invariants", {})
        if not isinstance(traits, dict):
            raise FilmError(f"{entry['id']}: invariants must be an object")
        characters.append({"id": entry["id"], "name": str(entry.get("name", "")).strip()[:80],
                           "look": _text(entry.get("look"), f"{entry['id']}.look", 10, 800),
                           "behavior": _text(entry.get("behavior", "restrained"), f"{entry['id']}.behavior", 1, 300),
                           "invariants": {str(k)[:40]: str(v)[:120] for k, v in traits.items()},
                           "variants": [str(v)[:60] for v in entry.get("variants", []) if isinstance(entry.get("variants"), list)]})
    for entry in data.get("locations") or []:
        if not isinstance(entry, dict) or not LOC_ID.fullmatch(str(entry.get("id", ""))):
            raise FilmError("Each location needs an id like LOC_ROOM")
        views = entry.get("required_views") or ["wide", "medium", "prop"]
        locations.append({"id": entry["id"], "description": _text(entry.get("description"), f"{entry['id']}.description", 10, 800),
                          "required_views": [str(v)[:30] for v in views][:6] if isinstance(views, list) else ["wide"]})
    if not 1 <= len(characters) <= 6 or not 1 <= len(locations) <= 8:
        raise FilmError("The world needs 1-6 characters and 1-8 locations")
    for group in (characters, locations):
        if len({e["id"] for e in group}) != len(group):
            raise FilmError("Duplicate character or location ids")
    return {"story": story, "style": {"visual_style": visual, "palette": {str(k)[:40]: str(v)[:20] for k, v in palette.items()},
                                      "rules": [str(r)[:200] for r in rules]},
            "characters": characters, "locations": locations}


def validate_shots(data, expected, world):
    rows = data.get("shots")
    if not isinstance(rows, list):
        raise FilmError("shots must be a list")
    ids = [r.get("id") for r in rows if isinstance(r, dict)]
    if sorted(ids) != sorted(expected):
        raise FilmError("The answer must contain exactly the requested shots: " + ", ".join(expected))
    char_ids, loc_ids = {c["id"] for c in world["characters"]}, {x["id"] for x in world["locations"]}
    out = {}
    for row in rows:
        sid = row["id"]
        mode = row.get("render_mode")
        if mode not in RENDER_MODES:
            raise FilmError(f"{sid}: render_mode must be one of {sorted(RENDER_MODES)}")
        for key, allowed in (("characters", char_ids), ("locations", loc_ids)):
            listed = row.get(key, [])
            if not isinstance(listed, list) or any(x not in allowed for x in listed):
                raise FilmError(f"{sid}: {key} must use ids from the world")
        camera, motion = row.get("camera") or {}, row.get("motion") or {}
        if not isinstance(camera, dict) or not isinstance(motion, dict):
            raise FilmError(f"{sid}: camera and motion must be objects")
        complexity = str(motion.get("complexity", "low")).lower()
        instruction = str(motion.get("instruction", "")).strip()
        if mode != "STATIC" and len(instruction) < 10:
            raise FilmError(f"{sid}: motion.instruction is required for an animated shot")
        out[sid] = {"description": _text(row.get("description"), f"{sid}.description", 10, 600),
                    "characters": list(row.get("characters", [])), "locations": list(row.get("locations", [])),
                    "composition": _text(row.get("composition", "medium shot"), f"{sid}.composition", 1, 300),
                    "camera": {"type": str(camera.get("type", "locked"))[:30], "movement": str(camera.get("movement", "none"))[:120]},
                    "motion": {"complexity": complexity if complexity in COMPLEXITY else "low", "instruction": instruction[:400]},
                    "render_mode": mode}
    return out


def lyric_text(p):
    path = p / "input/lyrics.txt"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def cues_between(p, in_ms, out_ms):
    """Timed lyric lines overlapping a shot, when reviewed timing exists."""
    cues = read(p / "lyrics/lyrics_timed.json", {}).get("cues", [])
    return [c["text"] for c in cues if isinstance(c, dict) and c.get("start_ms", 0) < out_ms and c.get("end_ms", 0) > in_ms]


def constraints_for(project):
    from . import providers
    lines = []
    video, image = providers.chosen(project, "video"), providers.chosen(project, "image")
    if video and video.id == "gemini_video":
        lines.append("- Every character that is animated must be an adult; the video model rejects minors.")
        lines.append("- Avoid shots that need more than 8 seconds of continuous action.")
    if image and image.id == "cloudflare_flux":
        lines.append("- The image model cannot see reference images, so every identifying detail must be in the character's look.")
    return "\n".join(lines)


def context(project, notes, language):
    p = Path(project)
    audio = read(p / "analysis/audio.json", {})
    shots = read(p / "manifest/shots.json")
    return {"brief": (p / "input/brief.md").read_text(encoding="utf-8") if (p / "input/brief.md").exists() else "",
            "lyrics": lyric_text(p), "notes": notes.strip(), "language": language,
            "song": {"duration_seconds": round(audio.get("duration_ms", 0) / 1000, 1), "tempo_bpm": audio.get("tempo_bpm"),
                     "section_starts_seconds": [round(x / 1000, 1) for x in audio.get("section_boundaries_ms", [])]},
            "aspect_ratio": read(p / "project.yaml")["format"].get("aspect_ratio"),
            "shots": [{"id": s["id"], "start": timecode(s["in_ms"]), "end": timecode(s["out_ms"]),
                       "seconds": round(s["duration_ms"] / 1000, 1),
                       "song_position": f"{round(100 * s['in_ms'] / max(audio.get('duration_ms', 1), 1))}%"} for s in shots]}


def world_messages(p, ctx, language):
    """(system, user) for the first phase: story, style, characters, locations."""
    system = WORLD_SYSTEM.format(language=language, constraints=constraints_for(p), schema=json.dumps(WORLD_SCHEMA, ensure_ascii=False))
    return system, json.dumps(ctx, ensure_ascii=False)


def batch_messages(p, ctx, world, batch, language):
    """(system, user) for one batch of shots, given the accepted world."""
    payload = {"world": world, "song": ctx["song"], "aspect_ratio": ctx["aspect_ratio"], "lyrics_full": ctx["lyrics"],
               "shots": [{**next(x for x in ctx["shots"] if x["id"] == s["id"]),
                          "timed_lyrics": cues_between(p, s["in_ms"], s["out_ms"])} for s in batch]}
    system = SHOTS_SYSTEM.format(language=language, constraints=constraints_for(p), schema=json.dumps(SHOT_SCHEMA, ensure_ascii=False))
    return system, json.dumps(payload, ensure_ascii=False)


def new_proposal(ctx, provider_id, model, language, notes):
    ident = object_hash({"context": ctx, "provider": provider_id, "model": model})
    return {"context_id": ident, "created_at": now(), "provider": provider_id, "model": model,
            "language": language, "notes": notes.strip(), "world": None, "shots": {}, "warnings": []}


def closing_warnings(shots):
    if any(s["duration_ms"] > 8000 for s in shots):
        return ["8초를 넘는 샷이 있습니다. Veo는 최대 8초라 05 · TIMELINE에서 나눠야 합니다."]
    return []


def draft(project, provider, notes="", language="English", resume=False, progress=None):
    """Ask the text provider for a proposal and save it for review. Returns the proposal."""
    p = Path(project)
    shots = read(p / "manifest/shots.json")
    if not shots:
        raise FilmError("먼저 제작 패키지를 만들어 샷 목록을 준비하세요")
    ctx = context(p, notes, language)
    fresh = new_proposal(ctx, provider.provider.id, provider.model, language, notes)
    proposal = read(p / PROPOSAL, {})
    if not (resume and proposal.get("context_id") == fresh["context_id"]):
        proposal = fresh
    if proposal["world"] is None:
        if progress:
            progress("이야기와 인물, 장소를 만드는 중")
        system, user = world_messages(p, ctx, language)
        proposal["world"] = validate_world(extract_json(provider.complete(system, user)))
        write(p / PROPOSAL, proposal)
    world = proposal["world"]
    remaining = [s for s in shots if s["id"] not in proposal["shots"]]
    for start in range(0, len(remaining), BATCH):
        batch = remaining[start:start + BATCH]
        if progress:
            progress(f"샷 {batch[0]['id']}–{batch[-1]['id']} 연출 중")
        system, user = batch_messages(p, ctx, world, batch, language)
        proposal["shots"].update(validate_shots(extract_json(provider.complete(system, user)), [s["id"] for s in batch], world))
        write(p / PROPOSAL, proposal)  # A later failure keeps the batches already paid for.
    proposal["warnings"] = closing_warnings(shots)
    write(p / PROPOSAL, proposal)
    return proposal


def pending(project):
    proposal = read(Path(project) / PROPOSAL, {})
    return proposal if proposal.get("world") else None


def complete(project):
    proposal = pending(project)
    ids = {s["id"] for s in read(Path(project) / "manifest/shots.json")}
    return bool(proposal) and set(proposal["shots"]) == ids


def accept(project, reviewer):
    """Write the reviewed proposal into the production files. Returns a summary."""
    if not reviewer.strip():
        raise FilmError("검토자 이름을 입력하세요")
    p = Path(project)
    with project_mutex(p):
        proposal = pending(p)
        shots = read(p / "manifest/shots.json")
        if not proposal or set(proposal["shots"]) != {s["id"] for s in shots}:
            raise FilmError("완성된 제안이 없거나 샷 목록이 제안 이후 바뀌었습니다. 다시 만드세요")
        world = proposal["world"]
        style = read(p / "bible/style_bible.yaml", {})
        style.update(world["style"])
        style["draft"] = False
        write(p / "bible/style_bible.yaml", style)
        old = {c["id"]: c for c in read(p / "bible/characters.yaml", {}).get("characters", [])}
        chars = [{**c, "reference_images": old.get(c["id"], {}).get("reference_images", [])} for c in world["characters"]]
        write(p / "bible/characters.yaml", {"draft": False, "characters": chars})
        old = {x["id"]: x for x in read(p / "bible/locations.yaml", {}).get("locations", [])}
        places = [{**x, "reference_images": old.get(x["id"], {}).get("reference_images", [])} for x in world["locations"]]
        write(p / "bible/locations.yaml", {"draft": False, "locations": places})
        directing = read(p / "bible/directing.yaml", {})
        directing.update(characters=[c["id"] for c in chars], locations=[x["id"] for x in places])
        write(p / "bible/directing.yaml", directing)
        brief = (p / "input/brief.md").read_text(encoding="utf-8") if (p / "input/brief.md").exists() else ""
        atomic_text(p / "bible/story.md", f"# Story\n\n{world['story']}\n\n## Brief (supplied)\n\n{brief}\n\n"
                    f"_Drafted by {proposal['provider']} ({proposal['model']}); reviewed and accepted by {reviewer.strip()} on {now()}._\n")
        for shot in shots:
            new = proposal["shots"][shot["id"]]
            shot.update(description=new["description"], characters=new["characters"], locations=new["locations"],
                        composition=new["composition"], camera=new["camera"], render_mode=new["render_mode"])
            motion = dict(shot.get("motion", {}))
            motion.update(complexity=new["motion"]["complexity"], instruction=new["motion"]["instruction"])
            motion.setdefault("local_effect", "hold")
            shot["motion"] = motion
        write(p / "manifest/shots.json", shots)
        digest = hashlib.sha256(json.dumps(proposal, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        log = read(p / LOG, [])
        log.append({"accepted_at": now(), "reviewer": reviewer.strip(), "provider": proposal["provider"],
                    "model": proposal["model"], "proposal_sha256": digest, "shots": len(shots)})
        write(p / LOG, log)
        (p / PROPOSAL).unlink()
    return {"shots": len(shots), "characters": len(chars), "locations": len(places),
            "note": "제작 패키지가 바뀌었습니다. 참조 이미지·첫 프레임을 만들고 다시 LOCK하세요."}


def discard(project):
    (Path(project) / PROPOSAL).unlink(missing_ok=True)
