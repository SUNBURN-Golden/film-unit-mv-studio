"""Hand-off packets for making shot media by hand in subscription apps.

A packet lists, per shot, the prompt to paste, the reference images to attach
and the existing import command for the result. Writing packets contacts no
provider, automates no app and leaves shots, reviews, budgets and LOCK unchanged.
"""
from math import ceil, gcd
from pathlib import Path
import html
import shlex

from .core import FilmError, atomic_text, read, safe_path, timecode, write
from .renderers import build_prompt
from .resolver import shot_hash

FRAME_APP = "Gemini 앱 · Nano Banana Pro"
MOTION_APPS = ["Flow · Veo 3.1", "Grok Imagine"]
MAX_REFERENCES = 14  # Nano Banana reference-image limit per request


def aspect_ratio(fmt):
    if fmt.get("aspect_ratio"):
        return fmt["aspect_ratio"]
    g = gcd(fmt["width"], fmt["height"])
    return f"{fmt['width'] // g}:{fmt['height'] // g}"


def frame_prompt(project, shot, aspect):
    p = Path(project)
    style = read(p / "bible/style_bible.yaml")
    cast = [c for c in read(p / "bible/characters.yaml", {}).get("characters", [])
            if c.get("id") in shot.get("characters", [])]
    places = [l for l in read(p / "bible/locations.yaml", {}).get("locations", [])
              if l.get("id") in shot.get("locations", [])]
    return "\n".join([
        f"Create one still image: the first frame of music-video shot {shot['id']}, {aspect} aspect ratio.",
        shot["description"], f"Composition: {shot['composition']}",
        f"Camera: {shot['camera']}", f"Style: {style}",
        f"Characters: {cast}", f"Locations: {places}",
        "Keep every character and location identical to the attached reference images.",
        "No text, captions, lyrics, logos or watermarks.",
    ])


def entry_text(entry):
    """What an entry looks like, in words: look/description plus fixed traits, minus placeholders."""
    parts = [str(entry.get(k, "")).strip() for k in ("look", "description")]
    traits = entry.get("invariants")
    if isinstance(traits, dict):
        parts.append(", ".join(f"{k}={v}" for k, v in traits.items()))
    return "; ".join(x for x in parts if x and "Director review required" not in x)


def _style_words(project):
    visual = read(Path(project) / "bible/style_bible.yaml", {}).get("visual_style", {})
    if not isinstance(visual, dict):
        return ""
    return ", ".join(str(v) for v in visual.values() if v and "Director review required" not in str(v))


def _clip(text, limit):
    return text if not limit or len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0]


def compact_prompt(project, shot, aspect, limit):
    """Short prompt for services with a length cap and no reference-image input."""
    p = Path(project)
    cast = [c for c in read(p / "bible/characters.yaml", {}).get("characters", []) if c.get("id") in shot.get("characters", [])]
    places = [x for x in read(p / "bible/locations.yaml", {}).get("locations", []) if x.get("id") in shot.get("locations", [])]
    style = _style_words(p)
    lines = [f"{aspect} still frame from a music video.", shot["description"].strip(),
             f"Composition: {shot.get('composition', '')}." if shot.get("composition") else "",
             f"Style: {style}." if style else "",
             "Characters: " + " | ".join(f"{c['id']}: {entry_text(c)}" for c in cast) if cast else "",
             "Location: " + " | ".join(f"{x['id']}: {entry_text(x)}" for x in places) if places else "",
             "No text, captions or watermark."]
    return _clip(" ".join(x for x in lines if x), limit)


def reference_prompt(project, entry, kind, limit=None):
    """Reference sheet prompt for one character or location."""
    style = _style_words(project)
    tail = f" Style: {style}." if style else ""
    if kind == "character":
        text = (f"Character reference: {entry_text(entry)}. One figure, full body, front view, neutral standing pose, "
                f"plain light gray background, even lighting, no text, no props.{tail}")
    else:
        text = (f"Environment reference: {entry_text(entry)}. Wide establishing view, empty of people, "
                f"no text.{tail}")
    return _clip(text, limit)


def frame_references(project, shot):
    """Bible reference images for this shot's cast and locations; missing files are reported."""
    p = Path(project)
    entries = [*read(p / "bible/characters.yaml", {}).get("characters", []),
               *read(p / "bible/locations.yaml", {}).get("locations", [])]
    wanted = set(shot.get("characters", [])) | set(shot.get("locations", []))
    found, missing = [], []
    for entry in entries:
        if entry.get("id") not in wanted:
            continue
        for relative in entry.get("reference_images", []):
            (found if safe_path(p, relative).is_file() else missing).append(relative)
    return list(dict.fromkeys(found)), missing


def export_packets(project, shot_ids=None):
    p = Path(project)
    fmt = read(p / "project.yaml")["format"]
    shots = read(p / "manifest/shots.json")
    if shot_ids:
        unknown = set(shot_ids) - {s["id"] for s in shots}
        if unknown:
            raise FilmError("Unknown shot ID: " + ", ".join(sorted(unknown)))
        shots = [s for s in shots if s["id"] in shot_ids]
    aspect = aspect_ratio(fmt)
    target = shlex.quote(str(project))
    warnings = [f"bible/{name} is still marked draft: finish it before generating"
                for name in ("style_bible.yaml", "characters.yaml", "locations.yaml")
                if read(p / "bible" / name, {}).get("draft")]
    packets = []
    for shot in shots:
        # Same first-frame fallback as the resolver when a shot lists no reference.
        first = (shot.get("references") or [f"storyboard/{shot['id']}.png"])[0]
        references, missing = frame_references(p, shot)
        warnings += [f"{shot['id']}: reference image not found: {r}" for r in missing]
        if len(references) > MAX_REFERENCES:
            warnings.append(f"{shot['id']}: {len(references)} reference images exceed the {MAX_REFERENCES}-image limit")
        packet = {"id": shot["id"], "in_ms": shot["in_ms"], "out_ms": shot["out_ms"],
                  "render_mode": shot["render_mode"], "shot_hash": shot_hash(shot),
                  "storyboard": first,
                  "storyboard_kind": shot.get("storyboard_kind", "imported"),
                  "frame": {"app": FRAME_APP, "aspect_ratio": aspect,
                            "prompt": frame_prompt(p, shot, aspect), "references": references,
                            "import": f"python -m engine.cli import-frame {target} {shot['id']} <image>"},
                  "motion": None}
        # STATIC holds are director decisions; their approved frame is the shot.
        if shot["render_mode"] != "STATIC":
            packet["motion"] = {"apps": MOTION_APPS, "min_seconds": ceil(shot["duration_ms"] / 1000),
                                "start_frame": first, "prompt": build_prompt(p, shot),
                                "import": f"python -m engine.cli import-asset {target} {shot['id']} <video> --kind draft"}
        packets.append(packet)
    out = p / "render/packets"
    write(out / "packets.json", {"schema_version": 1, "aspect_ratio": aspect,
                                 "warnings": warnings, "shots": packets})
    atomic_text(out / "index.html", page(packets, warnings, aspect))
    return {"packets": str(out / "index.html"), "shots": len(packets),
            "motions": sum(1 for x in packets if x["motion"]), "warnings": warnings}


def page(packets, warnings, aspect):
    e = lambda value: html.escape(str(value), quote=True)
    # The page lives in render/packets/, two levels below the project root.
    image = lambda relative: f'<img src="../../{e(relative)}" alt="{e(relative)}">'

    def prompt_box(box_id, text):
        return (f'<textarea id="{box_id}" readonly>{e(text)}</textarea>'
                f'<button data-copy="{box_id}">복사</button>')

    cards = []
    for x in packets:
        f, m = x["frame"], x["motion"]
        refs = "".join(f"<li>{image(r)}<code>{e(r)}</code></li>" for r in f["references"]) or "<li>등록된 참조 이미지 없음</li>"
        body = [f'<h2>{e(x["id"])} · {timecode(x["in_ms"])} → {timecode(x["out_ms"])} · {e(x["render_mode"])}</h2>',
                f'<div class="frame">{image(x["storyboard"])}<small>현재 첫 프레임: {e(x["storyboard_kind"])}</small></div>',
                f'<h3>1. 첫 프레임 — {e(f["app"])}</h3>',
                f'<p>화면비 <b>{e(f["aspect_ratio"])}</b>. 아래 참조 이미지를 함께 올리고 프롬프트를 붙여넣으세요.</p>',
                f'<ul class="refs">{refs}</ul>', prompt_box(f'frame-{e(x["id"])}', f["prompt"]),
                f'<p>가져오기: <code>{e(f["import"])}</code></p>']
        if m:
            note = "" if x["storyboard_kind"] == "imported" else '<p class="warn">첫 프레임을 먼저 가져온 뒤 그 이미지로 영상을 만드세요.</p>'
            body += [f'<h3>2. 영상 — {e(" 또는 ".join(m["apps"]))}</h3>', note,
                     f'<p>시작 프레임 <code>{e(m["start_frame"])}</code>, 최소 <b>{m["min_seconds"]}초</b>. '
                     f'서비스가 {e(aspect)} 화면비를 지원하지 않으면 가장 가까운 비율로 만드세요. 컴파일 때 여백을 넣어 맞추고 소리는 제거합니다.</p>',
                     prompt_box(f'motion-{e(x["id"])}', m["prompt"]),
                     f'<p>가져오기: <code>{e(m["import"])}</code></p>']
        else:
            body.append("<p>정지 샷: 승인한 첫 프레임이 이 샷의 완성본입니다.</p>")
        cards.append("<article>" + "".join(body) + "</article>")
    notes = "".join(f"<li>{e(w)}</li>" for w in warnings)
    script = ("document.addEventListener('click',ev=>{const b=ev.target.closest('button[data-copy]');if(!b)return;"
              "const t=document.getElementById(b.dataset.copy);t.select();"
              "(navigator.clipboard?navigator.clipboard.writeText(t.value):Promise.reject())"
              ".catch(()=>document.execCommand('copy')).then(()=>{b.textContent='복사됨';setTimeout(()=>b.textContent='복사',1500)})});")
    return ('<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>FILM UNIT — 작업지시서</title><style>'
            'body{background:#f3f3f3;color:#181818;font:16px system-ui;margin:32px auto;max-width:960px;padding:0 16px}'
            'article{border-top:1px solid #aaa;padding:16px 0 24px}.frame img{max-width:360px;width:100%;display:block;border:1px solid #bbb}'
            '.refs{display:flex;flex-wrap:wrap;gap:12px;list-style:none;padding:0}.refs img{height:96px;display:block}'
            'textarea{width:100%;min-height:160px;font:13px ui-monospace,monospace;box-sizing:border-box}'
            'button{margin:6px 0}.warn{color:#9b2c2c}code{word-break:break-all}</style>'
            '<h1>FILM UNIT / 구독 앱 작업지시서</h1>'
            '<p>샷마다 프롬프트를 복사해 앱에서 만들고, 내려받은 파일을 가져오기 명령으로 넣으세요. '
            f'이 페이지는 아무 서비스도 호출하지 않습니다. 프로젝트 화면비: <b>{e(aspect)}</b>.</p>'
            + (f'<ul class="warn">{notes}</ul>' if notes else "")
            + "".join(cards) + f"<script>{script}</script></html>")
