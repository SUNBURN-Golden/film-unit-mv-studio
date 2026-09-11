"""Editable production drafts. No fabricated narrative or generated artwork."""
from pathlib import Path
import html
import math
import textwrap
from PIL import Image, ImageDraw, ImageFont
from .core import FilmError, atomic_text, read, write, safe_path, timecode, validate_manifest


STYLE = {
    "format": {"aspect_ratio": "4:3", "resolution": "1440x1080", "fps": 24},
    "visual_style": {"medium": "limited 2D animation", "background": "warm gray paper", "line": "thin black ink", "shading": "minimal", "camera": "mostly static"},
    "palette": {"paper": "#ECEAE4", "ink": "#181818", "teal": "#48A6A0"},
    "rules": ["no photorealism", "no cinematic dramatic lighting", "no unnecessary camera movement", "no text unless explicitly specified", "emotion should be understated", "movement should be economical"],
    "draft": True,
}


def make_package(project, target_shot_ms=5000):
    p = Path(project)
    if (p / "manifest/shots.json").exists():
        raise FilmError("Production package already exists; edit it instead of overwriting")
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
    write(p / "bible/style_bible.yaml", STYLE)
    write(p / "bible/characters.yaml", {"draft": True, "characters": [
        {"id": "CHAR_A", "variants": ["A_20_FRONT", "A_20_SIDE", "A_40_FRONT", "A_60_FRONT", "A_OLD_FRONT"], "invariants": {"black_triangle_tie": True, "glasses": False}, "behavior": "restrained; minimal facial expression", "reference_images": []},
        {"id": "CHAR_B", "variants": ["B_20_FRONT", "B_20_SIDE", "B_40_POLITICIAN", "B_60_POLITICIAN", "B_OLD_FRONT"], "invariants": {"round_glasses": True}, "behavior": "restrained", "reference_images": []},
    ], "note": "Example cast from the project brief. Replace before real production; no character art generated yet."})
    write(p / "bible/locations.yaml", {"draft": True, "locations": [{"id": f"LOC_{name}", "reference_images": [], "required_views": ["wide", "medium", "prop"]} for name in ["ROOM", "INTERROGATION", "OFFICE", "HOME", "ORPHANAGE", "NURSING_HOME", "POLITICAL_OFFICE", "STAGE"]]})
    atomic_text(p / "bible/story.md", "# Story draft\n\n" + brief + "\n\nThis is the supplied brief. Shot-level story, ages, actions and symbolism require director review. Section labels are not lyric transcription.\n")
    shots = []
    pattern = ["STATIC", "LIMITED_MOTION", "STATIC", "FULL_GENERATIVE", "LIMITED_MOTION"]
    for i, (a, b) in enumerate(zip(boundaries, boundaries[1:])):
        mode = pattern[i % len(pattern)]
        shots.append({
            "id": f"S{i+1:03d}", "sequence": next(s["id"] for s in seq if s["in_ms"] <= a < s["out_ms"]),
            "in_ms": a, "out_ms": b, "duration_ms": b-a,
            "description": f"Draft shot {i+1}. Director to specify the action and emotion.",
            "characters": ["CHAR_A", "CHAR_B"], "locations": ["LOC_HOME", "LOC_POLITICAL_OFFICE"],
            "composition": "vertical split screen", "camera": {"type": "locked", "movement": "none"},
            "motion": {"complexity": "low" if mode != "FULL_GENERATIVE" else "high", "instruction": "Restrained movement. Preserve the approved first frame.", "local_effect": "hold"},
            "references": [f"storyboard/S{i+1:03d}.png"], "render_mode": mode, "renderer": "auto", "status": "storyboard",
            "storyboard_kind": "placeholder", "identity_priority": "high",
        })
    validate_manifest(shots, duration)
    write(p / "manifest/sequence.json", seq)
    write(p / "manifest/shots.json", shots)
    generate_storyboard(p)
    atomic_text(p / "bible/director_request.md", "# Work director handoff\n\nRead input/brief.md, input/lyrics.txt and analysis/audio.json. Write story.md, style_bible.yaml, characters.yaml, locations.yaml and shot descriptions against actual timecodes. Preserve contiguous full-duration coverage. Create character/location references and approved first frames using an image generator. Replace each placeholder via the Control Panel. Review all assets, then LOCK. No paid generation before a concrete batch quote is approved.\n")
    return shots


def font(size):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def placeholder(shot, path):
    # Exact layout diagram / technical slate, not generated character artwork.
    w, h = 1440, 1080
    im = Image.new("RGB", (w, h), "#ECEAE4")
    d = ImageDraw.Draw(im)
    ink, teal, gray = "#181818", "#48A6A0", "#CBC9C3"
    d.text((72, 56), "FILM UNIT   /   TIMING ANIMATIC", font=font(26), fill=ink)
    d.text((72, 130), shot["id"], font=font(100), fill=ink)
    d.text((w-440, 151), f"{timecode(shot['in_ms'])} — {timecode(shot['out_ms'])}", font=font(25), fill=ink)
    d.line((72, 275, w-72, 275), fill=ink, width=2)
    for x, label, identity in [(72, "CHAR_A", "TRIANGLE TIE"), (744, "CHAR_B", "ROUND GLASSES")]:
        d.rectangle((x, 322, x+624, 790), outline=gray, width=2)
        d.text((x+28, 347), "SUBJECT PLACEHOLDER", font=font(19), fill=ink)
        d.ellipse((x+245, 415, x+379, 549), outline=ink, width=3)
        d.rectangle((x+213, 560, x+411, 710), outline=ink, width=3)
        if label == "CHAR_A":
            d.polygon([(x+298, 580), (x+326, 580), (x+312, 625)], fill=ink)
        else:
            d.ellipse((x+264, 458, x+302, 496), outline=ink, width=3)
            d.ellipse((x+322, 458, x+360, 496), outline=ink, width=3)
            d.line((x+302, 475, x+322, 475), fill=ink, width=3)
        d.text((x+28, 735), label + " / " + identity, font=font(21), fill=ink)
    d.rectangle((72, 816, 1368, 827), fill=teal)
    d.text((72, 862), shot["render_mode"].replace("_", " "), font=font(35), fill=ink)
    d.text((72, 923), "LOCKED CAMERA   /   DIAGRAM ONLY   /   NO AI VIDEO", font=font(23), fill=ink)
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
    sheet = Image.new("RGB", (cols * thumb_w, math.ceil(len(shots)/cols) * thumb_h + 70), "#ECEAE4")
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
    atomic_text(p / "storyboard/storyboard.html", '<!doctype html><html lang="en"><meta charset="utf-8"><title>FILM UNIT — Storyboard</title><style>body{background:#eceae4;color:#181818;font:16px system-ui;margin:40px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:24px}img{width:100%}article{border-bottom:1px solid #aaa;padding-bottom:24px}h2{font-size:18px}small{color:#407975}</style><h1>FILM UNIT / Storyboard review</h1><p>Review each first frame and its timing before production LOCK.</p><main>' + ''.join(cards) + '</main></html>')


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
