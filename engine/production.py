"""Editable production drafts. No fabricated narrative or generated artwork."""
from pathlib import Path
from copy import deepcopy
import html
import math
import re
import sys
import textwrap
from PIL import Image, ImageDraw, ImageFont
from .core import FilmError, atomic_text, read, write, safe_path, timecode, validate_manifest
from .schema import SHOT_SCHEMA


STYLE = {
    "format": {"aspect_ratio": "4:3", "resolution": "1440x1080", "fps": 24},
    "visual_style": {"medium": "Director review required", "background": "Director review required", "camera": "Director review required"},
    "palette": {},
    "rules": ["Follow the approved production bible and shot references", "Director review required before production"],
    "draft": True,
}


def load_preset(name=None):
    """Return fresh draft data; presets never mutate global defaults."""
    if not name or name == "neutral":
        return {
            "style": deepcopy(STYLE),
            "characters": {"draft": True, "characters": [{"id": "CHAR_01", "variants": [], "invariants": {}, "behavior": "Director review required", "reference_images": []}]},
            "locations": {"draft": True, "locations": [{"id": "LOC_01", "description": "Director review required", "reference_images": [], "required_views": ["wide", "medium", "prop"]}]},
            "directing": {"characters": ["CHAR_01"], "locations": ["LOC_01"], "composition": "Director review required", "camera": {"type": "unspecified", "movement": "Director review required"}, "motion": {"complexity": "unspecified", "instruction": "Director review required: specify the subject action and its timing.", "local_effect": "hold"}, "render_mode": "LIMITED_MOTION", "identity_priority": "unspecified", "review_note": "Choose the visual medium, cast, locations, composition and movement for this song. A technical placeholder or local pan/zoom preview does not establish the intended production style."},
        }
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
        raise FilmError("Invalid preset name")
    roots = [Path(__file__).resolve().parent.parent / "presets", Path(sys.prefix) / "share/film-unit/presets"]
    directory = next((root / name for root in roots if (root / name).is_dir()), None)
    if directory is None:
        raise FilmError(f"Unknown production preset: {name}")
    return {key: read(directory / f"{key}.yaml") for key in ["style", "characters", "locations", "directing"]}


def make_package(project, target_shot_ms=5000, preset=None):
    p = Path(project)
    if (p / "manifest/shots.json").exists():
        raise FilmError("Production package already exists; edit it instead of overwriting")
    if type(target_shot_ms) is not int or target_shot_ms <= 0:
        raise FilmError("Target shot duration must be a positive integer in milliseconds")
    config = read(p / "project.yaml")
    preset_name = preset if preset is not None else config.get("production", {}).get("preset", config.get("preset"))
    package = load_preset(preset_name)
    directing = package["directing"]
    audio = read(p / "analysis/audio.json")
    duration = audio["duration_ms"]
    beats = audio["beat_times_ms"]
    boundaries = [0]
    cursor = 0
    while duration - cursor > target_shot_ms + 1000:
        target = cursor + target_shot_ms
        nearby = [b for b in beats if abs(b - target) <= 650 and b > cursor + 1000]
        cursor = min(nearby, key=lambda b: abs(b - target)) if nearby else target
        boundaries.append(cursor)
    boundaries.append(duration)
    # Musical-change candidates are snapped to existing shot boundaries, explicitly a draft.
    section_starts = sorted(set([0] + [min(boundaries[:-1], key=lambda b: abs(b - c)) for c in audio["section_boundaries_ms"][1:-1]]))
    seq = [{"id": f"SEQ{i+1:02d}", "in_ms": a, "out_ms": b, "title": f"Section {i+1} — story review required", "boundary_source": "measured feature change, snapped to draft cut"} for i, (a, b) in enumerate(zip(section_starts, section_starts[1:] + [duration]))]
    brief = (p / "input/brief.md").read_text()
    # Output dimensions belong to the project, even when the art direction is preset.
    fmt = config["format"]
    package["style"]["format"] = {"aspect_ratio": fmt.get("aspect_ratio", f"{fmt['width']}:{fmt['height']}"), "resolution": f"{fmt['width']}x{fmt['height']}", "fps": fmt["fps"]}
    for name, filename in [("style", "style_bible.yaml"), ("characters", "characters.yaml"), ("locations", "locations.yaml"), ("directing", "directing.yaml")]:
        write(p / "bible" / filename, package[name])
    atomic_text(p / "bible/story.md", "# Story draft\n\n" + brief + "\n\nThis is the supplied brief. Shot-level story, ages, actions and symbolism require director review. Section labels are not lyric transcription.\n")
    shots = []
    for i, (a, b) in enumerate(zip(boundaries, boundaries[1:])):
        # The director assigns complex actions or intentional holds after review.
        # A locked camera does not imply a static subject or a free local render.
        mode = directing["render_mode"]
        shots.append({
            "schema_version": SHOT_SCHEMA,
            "id": f"S{i+1:03d}", "sequence": next(s["id"] for s in seq if s["in_ms"] <= a < s["out_ms"]),
            "in_ms": a, "out_ms": b, "duration_ms": b-a,
            "description": f"Draft shot {i+1}. Director to specify the action and emotion.",
            "characters": deepcopy(directing["characters"]), "locations": deepcopy(directing["locations"]),
            "composition": directing["composition"], "camera": deepcopy(directing["camera"]),
            "motion": deepcopy(directing["motion"]),
            "references": [f"storyboard/S{i+1:03d}.png"], "render_mode": mode, "renderer": "auto", "status": "storyboard",
            "storyboard_kind": "placeholder", "identity_priority": directing["identity_priority"],
        })
    validate_manifest(shots, duration, fmt["fps"])
    write(p / "manifest/sequence.json", seq)
    write(p / "manifest/shots.json", shots)
    generate_storyboard(p)
    atomic_text(p / "bible/director_request.md", "# Work director handoff\n\nRead input/brief.md, input/lyrics.txt and analysis/audio.json. Write story.md, style_bible.yaml, characters.yaml, locations.yaml and shot descriptions against actual timecodes. Preserve contiguous full-duration coverage. " + directing["review_note"] + " Create character/location references and approved first frames. Replace each placeholder via the Control Panel. Review all assets, then LOCK. No paid generation before a concrete batch quote is approved.\n")
    return shots


def font(size):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def placeholder(shot, path):
    # Neutral timing slate. It deliberately makes no claims about finished artwork.
    w, h = 1440, 1080
    im = Image.new("RGB", (w, h), "#F3F3F3")
    d = ImageDraw.Draw(im)
    ink, gray = "#181818", "#B8B8B8"
    d.text((72, 56), "FILM UNIT   /   TIMING PREVIEW", font=font(26), fill=ink)
    d.text((72, 130), shot["id"], font=font(100), fill=ink)
    d.text((w-440, 151), f"{timecode(shot['in_ms'])} — {timecode(shot['out_ms'])}", font=font(25), fill=ink)
    d.line((72, 275, w-72, 275), fill=ink, width=2)
    d.rectangle((72, 322, w-72, 790), outline=gray, width=2)
    d.text((100, 352), "DIRECTOR REVIEW REQUIRED", font=font(36), fill=ink)
    lines = ["Characters: " + ", ".join(shot.get("characters", [])),
             "Locations: " + ", ".join(shot.get("locations", [])),
             "Composition: " + shot.get("composition", "Unspecified"),
             shot.get("description", "")]
    y = 434
    for line in lines:
        for wrapped in textwrap.wrap(line, width=72)[:2]:
            d.text((100, y), wrapped, font=font(25), fill=ink)
            y += 40
    d.text((72, 862), shot["render_mode"].replace("_", " "), font=font(35), fill=ink)
    d.text((72, 923), "PLACEHOLDER   /   NO GENERATED ART OR VIDEO", font=font(23), fill=ink)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def generate_storyboard(p, regenerate_id=None):
    p = Path(p)
    shots = read(p / "manifest/shots.json")
    for s in shots:
        path = safe_path(p, s["references"][0])
        if not path.exists() or s["id"] == regenerate_id:
            if s.get("storyboard_kind") != "placeholder":
                raise FilmError("Imported artwork can only be replaced with an uploaded image")
            placeholder(s, path)
    cols, thumb_w, thumb_h = 3, 400, 340
    sheet = Image.new("RGB", (cols * thumb_w, math.ceil(len(shots)/cols) * thumb_h + 70), "#F3F3F3")
    d = ImageDraw.Draw(sheet)
    d.text((20, 18), "FILM UNIT / STORYBOARD REVIEW", font=font(22), fill="#181818")
    cards = []
    for i, s in enumerate(shots):
        img = Image.open(safe_path(p, s["references"][0])).convert("RGB")
        img.thumbnail((380, 285))
        x, y = (i % cols)*thumb_w+10, (i//cols)*thumb_h+70
        sheet.paste(img, (x, y))
        d.text((x, y+293), f"{s['id']}   {timecode(s['in_ms'])}   {s['render_mode']}", font=font(15), fill="#181818")
        image_path = Path(s["references"][0]).name
        cards.append(f'<article><img src="{html.escape(image_path)}"><h2>{s["id"]} · {timecode(s["in_ms"])} → {timecode(s["out_ms"])}</h2><p>{html.escape(s["description"])}</p><small>{s["render_mode"]} · {s.get("storyboard_kind", "imported")}</small></article>')
    sheet.save(p / "storyboard/contact_sheet.jpg", quality=90)
    atomic_text(p / "storyboard/storyboard.html", '<!doctype html><html lang="en"><meta charset="utf-8"><title>FILM UNIT — Storyboard</title><style>body{background:#f3f3f3;color:#181818;font:16px system-ui;margin:40px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:24px}img{width:100%}article{border-bottom:1px solid #aaa;padding-bottom:24px}h2{font-size:18px}small{color:#555}</style><h1>FILM UNIT / Storyboard review</h1><p>Review each first frame and its timing before production LOCK.</p><main>' + ''.join(cards) + '</main></html>')


def import_frame(project, shot_id, source):
    p = Path(project)
    shots = read(p / "manifest/shots.json")
    s = next((s for s in shots if s["id"] == shot_id), None)
    if s is None:
        raise FilmError("Unknown shot")
    image = Image.open(source).convert("RGB")
    image.save(safe_path(p, s["references"][0]))
    s["storyboard_kind"] = "imported"
    write(p / "manifest/shots.json", shots)
    generate_storyboard(p)
