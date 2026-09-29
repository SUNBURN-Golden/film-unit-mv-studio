"""Browser hand-off: use subscription web apps by hand, safely.

FILM UNIT never logs in to, drives or scrapes a web app: the services' terms (OpenAI,
Anthropic, xAI) forbid automated access to consumer accounts. The User opens the
service in their own browser, copies a prompt out of FILM UNIT, and brings the result
back by pasting text or dropping image/video files. Everything that comes back is
validated like any other import, recorded in render/handoff_log.json, and never
touches lyrics, cut timing, reviews or LOCK. No function here polls or retries.
"""
from dataclasses import dataclass
from pathlib import Path
import os
import re
import time
import uuid
from urllib.parse import urlsplit

from PIL import Image

from .core import FilmError, digest, now, probe, project_mutex, read, safe_path, write
from . import director
from .packets import aspect_ratio, compact_prompt, frame_prompt, frame_references, reference_prompt
from .renderers import build_prompt

LOG = "render/handoff_log.json"
INBOX = "render/handoff_inbox"
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_TYPES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
MAX_IMAGE_BYTES = 40_000_000
MAX_IMAGE_PIXELS = 64_000_000
MIN_IMAGE_SIDE = 256
MAX_VIDEO_BYTES = 500_000_000
VIDEO_SECONDS = (0.5, 60.0)
ASPECT_TOLERANCE = 0.03
SCAN_LIMIT = 50


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    url: str
    roles: tuple
    note: str


SERVICES = (
    Service("chatgpt", "ChatGPT", "https://chatgpt.com/", ("text", "image"), "글과 이미지. 답을 복사해 가져옵니다."),
    Service("claude", "Claude", "https://claude.ai/", ("text",), "글. 긴 지시문도 잘 받습니다."),
    Service("gemini", "Gemini 앱", "https://gemini.google.com/app", ("text", "image", "video"),
            "글, Nano Banana 이미지, Veo 영상(구독 등급에 따라 다름)."),
    Service("flow", "Google Flow (Veo)", "https://flow.google.com/", ("video", "image"), "Veo로 영상을 만듭니다."),
    Service("grok", "Grok (Imagine)", "https://grok.com/", ("text", "image", "video"), "글, 이미지, 영상."),
    Service("openart", "OpenArt", "https://openart.ai/", ("image", "video"), "여러 이미지·영상 모델을 한곳에서 씁니다."),
)


def services_for(role):
    return [s for s in SERVICES if role in s.roles]


def service(service_id):
    for item in SERVICES:
        if item.id == service_id:
            return item
    raise FilmError(f"Unknown service: {service_id}")


def custom_service(url):
    """Any other https site the User names. The link is only opened by their own browser."""
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise FilmError("https:// 로 시작하는 웹사이트 주소를 입력하세요")
    return Service("custom", parts.hostname, url.strip(), ("text", "image", "video"), "직접 입력한 사이트")


# ---- storyboard text: copy a request out, paste the answer back -------------------------

def _paste_text(system, user):
    return (f"{system}\n\n=== 입력 자료 (JSON) ===\n{user}\n\n"
            "위 지시대로 JSON 객체 하나만 답하세요. 설명 문장 없이, 코드 블록 하나 안에 넣어도 됩니다.")


def _shots(p):
    shots = read(p / "manifest/shots.json", [])
    if not shots:
        raise FilmError("먼저 제작 패키지를 만들어 샷 목록을 준비하세요")
    return shots


def storyboard_state(project):
    """Where the copy-and-paste storyboard stands: 'world', 'shots' or 'done'."""
    p = Path(project)
    total = len(_shots(p))
    proposal = director.pending(p)
    if not proposal:
        return {"step": "world", "done": 0, "total": total, "next_ids": [], "proposal": None}
    remaining = [s["id"] for s in _shots(p) if s["id"] not in proposal["shots"]]
    return {"step": "shots" if remaining else "done", "done": total - len(remaining), "total": total,
            "next_ids": remaining[:director.BATCH], "proposal": proposal}


def world_request(project, notes="", language="English"):
    """The text to paste into a chat service for the first phase."""
    p = Path(project)
    _shots(p)
    system, user = director.world_messages(p, director.context(p, notes, language), language)
    return _paste_text(system, user)


def submit_world(project, answer, service_id="custom", notes="", language="English", replace=False):
    p = Path(project)
    with project_mutex(p):
        existing = director.pending(p)
        if existing and existing["shots"] and not replace:
            raise FilmError("이미 만든 샷 연출이 있는 초안이 있습니다. 먼저 '초안 버리기'를 누르세요.")
        world = director.validate_world(director.extract_json(answer))
        ctx = director.context(p, notes, language)
        proposal = director.new_proposal(ctx, f"handoff:{service_id}", "web", language, notes)
        proposal["world"] = world
        write(p / director.PROPOSAL, proposal)
    return proposal


def shots_request(project):
    """The text for the next batch of shots, with the ids it covers. None when every shot is done."""
    p = Path(project)
    state = storyboard_state(p)
    if state["step"] != "shots":
        return None
    proposal = state["proposal"]
    ctx = director.context(p, proposal.get("notes", ""), proposal["language"])
    batch = [s for s in _shots(p) if s["id"] in set(state["next_ids"])]
    system, user = director.batch_messages(p, ctx, proposal["world"], batch, proposal["language"])
    return {"ids": state["next_ids"], "text": _paste_text(system, user)}


def submit_shots(project, answer):
    """Validate the pasted answer for exactly the next batch and add it to the draft."""
    p = Path(project)
    with project_mutex(p):
        state = storyboard_state(p)
        if state["step"] != "shots":
            raise FilmError("지금은 샷 연출을 받을 차례가 아닙니다. 이야기와 인물을 먼저 붙여넣으세요.")
        proposal = state["proposal"]
        proposal["shots"].update(director.validate_shots(director.extract_json(answer), state["next_ids"], proposal["world"]))
        if len(proposal["shots"]) == state["total"]:
            proposal["warnings"] = director.closing_warnings(_shots(p))
        write(p / director.PROPOSAL, proposal)
    return storyboard_state(p)


# ---- media coming back: drop files or pick from the downloads folder --------------------

def _stage(p, data, filename, allowed, limit):
    """Write incoming bytes to a private inbox under a random name. The User's file name is never a path."""
    extension = Path(filename).suffix.lower()
    if extension not in allowed:
        raise FilmError(f"지원하지 않는 파일 형식입니다({extension or '확장자 없음'}). 가능: " + ", ".join(sorted(allowed)))
    if not data or len(data) > limit:
        raise FilmError(f"파일 크기가 0이거나 {limit // 1_000_000} MB를 넘습니다")
    folder = safe_path(p, INBOX)
    folder.mkdir(parents=True, exist_ok=True)
    staged = folder / (uuid.uuid4().hex + extension)
    staged.write_bytes(data)
    return staged


def check_image(path):
    try:
        with Image.open(path) as image:
            width, height, kind = image.width, image.height, image.format
            image.verify()
    except Exception:
        raise FilmError("이미지 파일을 읽을 수 없습니다. 깨졌거나 이미지가 아닙니다.") from None
    if kind not in {"PNG", "JPEG", "WEBP"}:
        raise FilmError(f"PNG·JPEG·WEBP 이미지만 가져올 수 있습니다({kind}).")
    if min(width, height) < MIN_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
        raise FilmError(f"이미지 크기가 맞지 않습니다({width}×{height}). 한 변은 {MIN_IMAGE_SIDE}px 이상, 전체 6,400만 화소 이하여야 합니다.")
    return {"width": width, "height": height, "format": kind}


def check_video(path):
    try:
        info = probe(path)
    except Exception:
        raise FilmError("영상 파일을 읽을 수 없습니다. 깨졌거나 영상이 아닙니다.") from None
    videos = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not videos:
        raise FilmError("영상 스트림이 없는 파일입니다.")
    seconds = float(info.get("format", {}).get("duration") or 0)
    if not VIDEO_SECONDS[0] <= seconds <= VIDEO_SECONDS[1]:
        raise FilmError(f"영상 길이가 {seconds:.1f}초입니다. {VIDEO_SECONDS[0]}–{VIDEO_SECONDS[1]:.0f}초 클립만 가져옵니다.")
    return {"width": videos[0].get("width"), "height": videos[0].get("height"), "seconds": round(seconds, 2)}


def _aspect_warning(p, width, height, what):
    target = aspect_ratio(read(p / "project.yaml")["format"])
    tw, th = (int(x) for x in target.split(":"))
    if width and height and abs(width / height - tw / th) / (tw / th) > ASPECT_TOLERANCE:
        return f"{what}의 화면비({width}×{height})가 프로젝트({target})와 다릅니다. 컴파일할 때 여백이 생기거나 잘릴 수 있습니다."
    return None


def _log(p, kind, target, service_id, name, sha, details):
    with project_mutex(p):
        log = read(p / LOG, [])
        log.append({"at": now(), "kind": kind, "target": target, "service": service_id, "filename": Path(name).name[:120],
                    "sha256": sha, **details})
        write(p / LOG, log)


def import_frame(project, shot_id, data, filename, service_id="custom"):
    """A first frame made in a web app. Replaces the shot's storyboard image like the import-frame command."""
    from .production import import_frame as store
    p = Path(project)
    staged = _stage(p, data, filename, IMAGE_TYPES, MAX_IMAGE_BYTES)
    try:
        info = check_image(staged)
        store(p, shot_id, staged)
        _log(p, "frame", shot_id, service_id, filename, digest(staged), info)
    finally:
        staged.unlink(missing_ok=True)
    warning = _aspect_warning(p, info["width"], info["height"], "이미지")
    return {"target": shot_id, "warnings": [warning] if warning else [], **info}


def import_reference(project, folder, entry_id, data, filename, service_id="custom"):
    """A character or location reference sheet made in a web app."""
    from . import imagegen
    p = Path(project)
    staged = _stage(p, data, filename, IMAGE_TYPES, MAX_IMAGE_BYTES)
    try:
        info = check_image(staged)
        with Image.open(staged) as original:
            png = staged.with_suffix(".png.tmp")
            original.convert("RGB").save(png, format="PNG")
        try:
            imagegen.import_reference(p, folder, entry_id, png)
        finally:
            png.unlink(missing_ok=True)
        _log(p, "reference", f"{folder}/{entry_id}", service_id, filename, digest(staged), info)
    finally:
        staged.unlink(missing_ok=True)
    return {"target": entry_id, "warnings": [], **info}


def import_video(project, shot_id, data, filename, service_id="custom"):
    """A shot clip made in a web app, stored as a draft take. It is reviewed like any other take."""
    from .compiler import import_asset
    p = Path(project)
    staged = _stage(p, data, filename, VIDEO_TYPES, MAX_VIDEO_BYTES)
    try:
        info = check_video(staged)
        record = import_asset(p, shot_id, staged, kind="draft")
        _log(p, "video", shot_id, service_id, filename, record["sha256"], info)
    finally:
        staged.unlink(missing_ok=True)
    warning = _aspect_warning(p, info["width"], info["height"], "영상")
    return {"target": shot_id, "warnings": [warning] if warning else [], **info}


def downloads_folder():
    override = os.environ.get("FILM_UNIT_DOWNLOADS")
    return Path(override).expanduser() if override else Path.home() / "Downloads"


def scan_downloads(kind, folder=None, minutes=180):
    """One look at recently downloaded files. Only runs when the User asks; nothing is watched or polled."""
    allowed = IMAGE_TYPES if kind == "image" else VIDEO_TYPES
    base = Path(folder) if folder else downloads_folder()
    if not base.is_dir():
        return []
    cutoff, found = time.time() - minutes * 60, []
    for entry in base.iterdir():
        try:
            stat = entry.lstat()
        except OSError:
            continue
        if entry.is_symlink() or not entry.is_file() or entry.suffix.lower() not in allowed or stat.st_mtime < cutoff:
            continue
        found.append({"name": entry.name, "size": stat.st_size, "modified": stat.st_mtime})
    return sorted(found, key=lambda f: f["modified"], reverse=True)[:SCAN_LIMIT]


def read_download(name, kind, folder=None):
    """Bytes of one file the scan listed. The name must be a plain file name inside the folder."""
    base = (Path(folder) if folder else downloads_folder()).resolve()
    if Path(name).name != name or not re.fullmatch(r"[^\x00/\\]{1,255}", name):
        raise FilmError("올바르지 않은 파일 이름입니다")
    path = base / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != base:
        raise FilmError("다운로드 폴더 안의 일반 파일만 가져올 수 있습니다")
    limit = MAX_IMAGE_BYTES if kind == "image" else MAX_VIDEO_BYTES
    if path.stat().st_size > limit:
        raise FilmError(f"파일이 {limit // 1_000_000} MB를 넘습니다")
    return path.read_bytes()


def shot_status(project):
    """Per shot: does it have a first frame from a person/provider, and a video take?"""
    p = Path(project)
    assets = read(p / "manifest/assets.json", {"shots": {}}).get("shots", {})
    rows = []
    for shot in read(p / "manifest/shots.json", []):
        take = assets.get(shot["id"], {})
        rows.append({"id": shot["id"], "render_mode": shot["render_mode"], "seconds": round(shot["duration_ms"] / 1000, 1),
                     "frame": shot.get("storyboard_kind") == "imported",
                     "video": bool(take.get("final") or take.get("draft")),
                     "needs_video": shot["render_mode"] != "STATIC"})
    return rows


def missing_references(project):
    """Characters and locations that still have no reference image."""
    p = Path(project)
    out = []
    for folder, name, key in (("characters", "characters.yaml", "characters"), ("locations", "locations.yaml", "locations")):
        for entry in read(p / "bible" / name, {}).get(key, []):
            have = [r for r in entry.get("reference_images", []) if safe_path(p, r).is_file()]
            if not have:
                out.append({"folder": folder, "id": entry["id"]})
    return out


def _shot(p, shot_id):
    shot = next((s for s in read(p / "manifest/shots.json", []) if s["id"] == shot_id), None)
    if shot is None:
        raise FilmError("Unknown shot ID")
    return shot


def frame_brief(project, shot_id):
    """What to paste into an image service for this shot's first frame, and which files to attach there."""
    p = Path(project)
    shot = _shot(p, shot_id)
    aspect = aspect_ratio(read(p / "project.yaml")["format"])
    references, missing = frame_references(p, shot)
    return {"prompt": frame_prompt(p, shot, aspect), "short_prompt": compact_prompt(p, shot, aspect, 900),
            "aspect": aspect, "attach": references, "missing": missing}


def video_brief(project, shot_id):
    """What to paste into a video service for this shot, starting from its first frame."""
    p = Path(project)
    shot = _shot(p, shot_id)
    first = (shot.get("references") or [f"storyboard/{shot_id}.png"])[0]
    return {"prompt": build_prompt(p, shot), "seconds": -(-shot["duration_ms"] // 1000), "start_frame": first,
            "frame_ready": shot.get("storyboard_kind") == "imported", "render_mode": shot["render_mode"]}


def reference_brief(project, folder, entry_id):
    p = Path(project)
    name, key = ("characters.yaml", "characters") if folder == "characters" else ("locations.yaml", "locations")
    entry = next((e for e in read(p / "bible" / name, {}).get(key, []) if e.get("id") == entry_id), None)
    if entry is None:
        raise FilmError(f"알 수 없는 ID입니다: {entry_id}")
    return {"prompt": reference_prompt(p, entry, "character" if folder == "characters" else "location")}


def default_targets(count, targets):
    """Files taken in order go to the open targets in order; extra files get None (skip)."""
    return [targets[i] if i < len(targets) else None for i in range(count)]


def import_many(project, kind, assignments, service_id="custom"):
    """Import several files at once. assignments: (target, file name, bytes or a function returning bytes) triples;
    a None target skips. A function is called only when its turn comes, so large downloads are never all in memory.

    One bad file never stops the rest; each result says what happened. A target may be used only once.
    """
    if kind not in {"frame", "video", "reference"}:
        raise FilmError("kind must be frame, video or reference")
    chosen = [a[0] for a in assignments if a[0]]
    if len(chosen) != len(set(chosen)):
        raise FilmError("같은 대상에 파일이 둘 이상 지정됐습니다. 하나만 고르세요.")
    results = []
    for target, name, data in assignments:
        if not target:
            continue
        try:
            data = data() if callable(data) else data
            if kind == "frame":
                done = import_frame(project, target, data, name, service_id)
            elif kind == "video":
                done = import_video(project, target, data, name, service_id)
            else:
                folder, _, entry_id = target.partition("/")
                done = import_reference(project, folder, entry_id, data, name, service_id)
            results.append({"file": name, "target": target, "ok": True, "message": "가져왔습니다", "warnings": done["warnings"]})
        except FilmError as exc:
            results.append({"file": name, "target": target, "ok": False, "message": str(exc), "warnings": []})
    return results
