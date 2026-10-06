"""Hand-off packets for making shot media by hand in subscription apps.

A packet lists, per shot, the prompt to paste, the reference images to attach
and the existing import command for the result. Writing packets contacts no
provider, automates no app and leaves shots, reviews, budgets and LOCK unchanged.

The second half of this module writes FRAME_ANIMATION_V1 A-path work
packets (`animation/packets/<shot>.json`, `animation_work_packet` schema 1):
per shot/segment the master, layout, keypose/breakdown control times, masks,
replacement drawings and segment conditions a person carries to an image app
by hand, plus exactly which inputs the named target actually accepts — inputs
it cannot consume are marked NOT_SENT, never claimed as transmitted.
"""
from math import ceil, gcd
from pathlib import Path
import html
import re
import shlex

from .animation_schema import (check_document, load_animation_timeline,
                               read_canon, require_animation_profile,
                               write_canon)
from .core import (FilmError, atomic_text, digest, project_mutex, read,
                   safe_path, timecode, write)
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


# ---------------------------------------------------------------------------
# FRAME_ANIMATION_V1 A-path work packets (ANIM-009, design 8.2 and 13)
#
# The A path produces every needed frame as an image inside a subscription
# app, conditioned on the fixed master, layout, keypose/breakdown controls,
# masks and replacement conditions the shot plan pins. The work packet is a
# CANON_JSON_V1 `animation_work_packet` document: deterministic, pinned to the
# plan it was written against, and honest about which inputs the named target
# can actually take — an input the site cannot consume is NOT_SENT, and the
# previous produced frame is an auxiliary reference, never the sole basis.
# Nothing here contacts a provider, drives a consumer UI or approves work.

PACKET_TYPE = "animation_work_packet"
PACKET_DIR = "animation/packets"
# Input kinds a manual target may accept. The packet records, per input,
# whether the named target's accepts list covers its token.
ACCEPT_TOKENS = {"prompt_text", "reference_image", "layout_image",
                 "pose_image", "mask_region", "previous_frame"}
MANUAL_TARGET = {"id": "manual", "accepts": ["prompt_text",
                                             "reference_image"],
                 "max_images": None}
REQUEST_KINDS = {"reference", "first_frame", "keypose", "breakdown", "pose",
                 "inbetween"}
INPUT_DISPOSITIONS = {"INCLUDE", "NOT_SENT", "PENDING_PRODUCTION"}

PACKET_FIELDS = {"document_type", "schema_version", "shot_id", "instance_id",
                 "plan_sha256", "plan_revision", "canvas", "fps",
                 "used_source_range", "unused_handles", "segments",
                 "controls", "inputs", "requests", "transport", "import",
                 "warnings", "notes"}
PACKET_SEGMENT_FIELDS = {"start", "end", "path", "capabilities"}
PACKET_CONTROL_FIELDS = {"role", "frame", "pin"}
PACKET_INPUT_FIELDS = {"id", "kind", "pin", "token", "required",
                       "disposition", "reason"}
PACKET_REQUEST_FIELDS = {"request_id", "kind", "frame", "segment",
                         "produces", "inputs", "carries", "aux",
                         "depends_on", "blocked", "prompt"}
PACKET_PRODUCES_FIELDS = {"role", "frame", "fills_member", "fills_input"}
PACKET_TRANSPORT_FIELDS = {"mode", "target", "not_sent", "note"}
CONTROL_SLOT_ROLES = {"KEYPOSE", "BREAKDOWN", "POSE"}
PIN_FIELDS = {"asset_id", "revision", "content_sha256"}
SHA256 = re.compile(r"[0-9a-f]{64}")


def validate_work_packet(document):
    """Structural contract of `animation_work_packet` 1."""
    check_document(document, PACKET_TYPE)
    if type(document) is not dict or set(document.keys()) != PACKET_FIELDS:
        raise FilmError("animation_work_packet must hold "
                        f"{sorted(PACKET_FIELDS)}")
    if type(document["shot_id"]) is not str or not document["shot_id"]:
        raise FilmError("packet shot_id must be a non-empty string")
    if type(document["instance_id"]) is not str \
            or not document["instance_id"]:
        raise FilmError("packet instance_id must be a non-empty string")
    if type(document["plan_sha256"]) is not str \
            or not SHA256.fullmatch(document["plan_sha256"]):
        raise FilmError("packet plan_sha256 must be a lowercase SHA-256")
    if type(document["plan_revision"]) is not int \
            or document["plan_revision"] < 1:
        raise FilmError("packet plan_revision must be a positive integer")
    if type(document["fps"]) is not int or document["fps"] < 1:
        raise FilmError("packet fps must be a positive integer")
    rng = document["used_source_range"]
    if (type(rng) is not list or len(rng) != 2
            or any(type(v) is not int or v < 0 for v in rng)
            or rng[1] <= rng[0]):
        raise FilmError("packet used_source_range must be a [start, end) pair")
    handles = document["unused_handles"]
    if (type(handles) is not dict
            or set(handles.keys()) != {"before", "after"}
            or any(type(v) is not int or v < 0
                   for v in handles.values())):
        raise FilmError("packet unused_handles must hold before/after "
                        "non-negative integers")
    canvas = document["canvas"]
    if (type(canvas) is not dict or type(canvas.get("width")) is not int
            or type(canvas.get("height")) is not int
            or canvas["width"] < 1 or canvas["height"] < 1):
        raise FilmError("packet canvas must hold positive width/height")
    segments = document["segments"]
    if type(segments) is not list or not segments:
        raise FilmError("packet segments must be a non-empty list")
    for index, seg in enumerate(segments):
        if type(seg) is not dict \
                or set(seg.keys()) != PACKET_SEGMENT_FIELDS:
            raise FilmError(f"packet segments[{index}] must hold "
                            f"{sorted(PACKET_SEGMENT_FIELDS)}")
        if seg["path"] != "A":
            raise FilmError("a work packet covers A-path segments only")
    for index, control in enumerate(document["controls"]):
        what = f"packet controls[{index}]"
        if type(control) is not dict \
                or set(control.keys()) != PACKET_CONTROL_FIELDS:
            raise FilmError(f"{what} must hold "
                            f"{sorted(PACKET_CONTROL_FIELDS)}")
        if control["role"] not in CONTROL_SLOT_ROLES:
            raise FilmError(f"{what} role must be one of "
                            f"{sorted(CONTROL_SLOT_ROLES)}")
        if type(control["frame"]) is not int or control["frame"] < 0:
            raise FilmError(f"{what}.frame must be a non-negative integer")
        _packet_pin(control["pin"], f"{what}.pin")
    inputs = {}
    for index, entry in enumerate(document["inputs"]):
        what = f"packet inputs[{index}]"
        if type(entry) is not dict or set(entry.keys()) != PACKET_INPUT_FIELDS:
            raise FilmError(f"{what} must hold {sorted(PACKET_INPUT_FIELDS)}")
        if type(entry["id"]) is not str or not entry["id"]:
            raise FilmError(f"{what}.id must be a non-empty string")
        if entry["id"] in inputs:
            raise FilmError(f"duplicate packet input id {entry['id']}")
        if entry["token"] not in ACCEPT_TOKENS:
            raise FilmError(f"{what}.token must be one of "
                            f"{sorted(ACCEPT_TOKENS)}")
        if entry["disposition"] not in INPUT_DISPOSITIONS:
            raise FilmError(f"{what}.disposition must be one of "
                            f"{sorted(INPUT_DISPOSITIONS)}")
        _packet_pin(entry["pin"], f"{what}.pin")
        if type(entry["required"]) is not bool:
            raise FilmError(f"{what}.required must be a boolean")
        if entry["reason"] is not None \
                and type(entry["reason"]) is not str:
            raise FilmError(f"{what}.reason must be a string or null")
        inputs[entry["id"]] = entry
    if "previous_frame" not in inputs:
        raise FilmError("packet inputs must declare previous_frame")
    request_ids = set()
    for index, request in enumerate(document["requests"]):
        what = f"packet requests[{index}]"
        if type(request) is not dict \
                or set(request.keys()) != PACKET_REQUEST_FIELDS:
            raise FilmError(f"{what} must hold "
                            f"{sorted(PACKET_REQUEST_FIELDS)}")
        if type(request["request_id"]) is not str \
                or not request["request_id"]:
            raise FilmError(f"{what}.request_id must be a non-empty string")
        request_ids.add(request["request_id"])
        if request["kind"] not in REQUEST_KINDS:
            raise FilmError(f"{what}.kind must be one of "
                            f"{sorted(REQUEST_KINDS)}")
        if request["frame"] is not None \
                and type(request["frame"]) is not int:
            raise FilmError(f"{what}.frame must be an integer or null")
        if request["segment"] is not None and (
                type(request["segment"]) is not list
                or len(request["segment"]) != 2
                or any(type(v) is not int for v in request["segment"])):
            raise FilmError(f"{what}.segment must be a [start, end) pair "
                            "or null")
        produces = request["produces"]
        if type(produces) is not dict \
                or not PACKET_PRODUCES_FIELDS >= set(produces.keys()) \
                or not {"role", "frame", "fills_member"} <= set(produces):
            raise FilmError(f"{what}.produces must hold role, frame and "
                            "fills_member (and optionally fills_input)")
        if type(produces["role"]) is not str or not produces["role"]:
            raise FilmError(f"{what}.produces.role must be a string")
        if type(produces["frame"]) is not int \
                and produces["frame"] is not None:
            raise FilmError(f"{what}.produces.frame must be an integer or "
                            "null")
        if type(produces["fills_member"]) is not bool:
            raise FilmError(f"{what}.produces.fills_member must be a "
                            "boolean")
        for field in ("inputs", "carries", "aux", "depends_on", "blocked"):
            if type(request[field]) is not list:
                raise FilmError(f"{what}.{field} must be a list")
        unknown = [i for i in request["inputs"] + request["carries"]
                   + request["aux"] if i not in inputs]
        if unknown:
            raise FilmError(f"{what} names undeclared inputs: {unknown}")
        if not set(request["carries"]) <= set(request["inputs"]):
            raise FilmError(f"{what}.carries must be a subset of inputs")
        if "previous_frame" in request["inputs"]:
            raise FilmError(f"{what} may carry previous_frame only as an "
                            "auxiliary reference")
        if set(request["carries"]) == {"previous_frame"}:
            raise FilmError(f"{what} cannot be based on previous_frame "
                            "alone")
        if type(request["prompt"]) is not str:
            raise FilmError(f"{what}.prompt must be a string")
    for index, request in enumerate(document["requests"]):
        missing = [d for d in request["depends_on"]
                   if d not in request_ids]
        if missing:
            raise FilmError(f"packet requests[{index}] depends on unknown "
                            f"requests {missing}")
    transport = document["transport"]
    if type(transport) is not dict \
            or set(transport.keys()) != PACKET_TRANSPORT_FIELDS:
        raise FilmError(f"packet transport must hold "
                        f"{sorted(PACKET_TRANSPORT_FIELDS)}")
    if transport["mode"] != "MANUAL_PACKET":
        raise FilmError("packet transport mode must be MANUAL_PACKET")
    if type(transport["not_sent"]) is not list:
        raise FilmError("packet transport.not_sent must be a list")
    if type(document["warnings"]) is not list \
            or type(document["notes"]) is not list:
        raise FilmError("packet warnings/notes must be lists")
    if type(document["import"]) is not dict:
        raise FilmError("packet import must be an object of command strings")
    return document


def _packet_pin(value, what):
    if value is None:
        return
    if type(value) is not dict or set(value.keys()) != PIN_FIELDS:
        raise FilmError(f"{what} must be a pin or null")
    if type(value["asset_id"]) is not str or not value["asset_id"]:
        raise FilmError(f"{what}.asset_id must be a non-empty string")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise FilmError(f"{what}.revision must be a positive integer")
    if type(value["content_sha256"]) is not str \
            or not SHA256.fullmatch(value["content_sha256"]):
        raise FilmError(f"{what}.content_sha256 must be a lowercase SHA-256")


def load_work_packet(p, shot_id):
    """Read and re-validate the stored packet for a shot."""
    document = read_canon(safe_path(p, f"{PACKET_DIR}/{shot_id}.json"))
    if document["shot_id"] != shot_id:
        raise FilmError(f"Packet names {document['shot_id']}, not {shot_id}")
    return validate_work_packet(document)


def _check_target(target):
    if target is None:
        return dict(MANUAL_TARGET)
    if type(target) is not dict \
            or set(target.keys()) != {"id", "accepts", "max_images"}:
        raise FilmError("target must hold {id, accepts, max_images}")
    if type(target["id"]) is not str or not target["id"].strip():
        raise FilmError("target.id must be a non-empty string")
    if (type(target["accepts"]) is not list
            or any(t not in ACCEPT_TOKENS for t in target["accepts"])):
        raise FilmError("target.accepts must list tokens from "
                        + ", ".join(sorted(ACCEPT_TOKENS)))
    if target["max_images"] is not None and (
            type(target["max_images"]) is not int
            or target["max_images"] < 1):
        raise FilmError("target.max_images must be null or a positive "
                        "integer")
    return dict(target)


def _role_inputs(plan, role, base, token, required, target):
    """One input entry per plan asset of a role (master/layout/mask/...)."""
    items = [a for a in plan["assets"] if a["role"] == role]
    entries = []
    for index, asset in enumerate(items):
        pin = {k: asset[k] for k in PIN_FIELDS}
        input_id = base if len(items) == 1 else f"{base}:{index}"
        entries.append(_packet_input(input_id, asset["kind"], pin, token,
                                     required, target))
    return entries


def _packet_input(input_id, kind, pin, token, required, target):
    """One input with the disposition the named target forces."""
    accepted = token in target["accepts"]
    if pin is None and kind == "CONTROL_IMAGE":
        disposition = "PENDING_PRODUCTION"
        reason = "control not yet produced/imported" + (
            "" if accepted
            else f"; target '{target['id']}' also accepts no {token} input")
    elif not accepted:
        disposition = "NOT_SENT"
        reason = f"target '{target['id']}' accepts no {token} input"
    else:
        disposition, reason = "INCLUDE", None
    return {"id": input_id, "kind": kind, "pin": pin, "token": token,
            "required": required, "disposition": disposition,
            "reason": reason}


def _request_prompt(shot_id, description, kind, frame, fps, length):
    what = {"reference": "the reference drawing",
            "first_frame": "the first frame",
            "keypose": "the keypose drawing",
            "breakdown": "the breakdown drawing",
            "pose": "the pose drawing",
            "inbetween": "the inbetween frame"}[kind]
    lines = [f"Create one image for music-video shot {shot_id}: {what}.",
             f"It is cut-local frame {frame} of {length} at {fps} fps "
             f"(t = {frame / fps:.3f} s)." if frame is not None else
             "It anchors the shot's reference art.",
             description.strip(),
             "Keep the artwork identical to the master reference and layout "
             "control images.",
             "No text, captions, lyrics, logos or watermarks."]
    return "\n".join(line for line in lines if line)


def _build_work_packet(p, entry, plan, plan_sha, registry, target, fmt,
                       description):
    """One shot's A-path work packet document."""
    shot_id = entry["shot_id"]
    start, end = entry["used_source_range"]
    length = end - start
    handles = entry["unused_handles"]
    fps = fmt["fps"]
    a_segments = [s for s in plan["segments"] if s["path"] == "A"]
    pure_a = all(s["path"] == "A" for s in plan["segments"])
    if pure_a:
        needed = sorted(range(-start, length + handles["after"]))
    else:
        needed = sorted({f for seg in a_segments
                         for f in range(seg["start"], seg["end"])})
    needed_set = set(needed)
    slots = {kp["frame"]: kp["kind"] for kp in plan["keyposes"]}
    control_pins = {}
    for asset_id, asset in registry["assets"].items():
        record = asset["revisions"][str(asset["current_revision"])]
        if record["kind"] == "CONTROL_IMAGE" \
                and record["shot_id"] == shot_id:
            control_pins[(record["control_role"], record["frame"])] = {
                "asset_id": asset_id, "revision": record["revision"],
                "content_sha256": record["content_sha256"]}
    inputs = [_packet_input("prompt", "PROMPT", None, "prompt_text", True,
                            target),
              _packet_input("previous_frame", "PREVIOUS_FRAME", None,
                            "previous_frame", False, target)]
    inputs += _role_inputs(plan, "master", "master", "reference_image",
                           True, target)
    inputs += _role_inputs(plan, "layout", "layout", "layout_image",
                           True, target)
    inputs += _role_inputs(plan, "control", "guide", "pose_image", False,
                           target)
    controls = []
    for frame in sorted(slots):
        kind = slots[frame]
        if not any(seg["start"] <= frame < seg["end"]
                   for seg in a_segments):
            continue
        pin = control_pins.get((kind, frame))
        inputs.append(_packet_input(f"control:{kind}:{frame}",
                                    "CONTROL_IMAGE", pin, "pose_image",
                                    True, target))
        controls.append({"role": kind, "frame": frame, "pin": pin})
    inputs += _role_inputs(plan, "mask", "mask", "mask_region", False,
                           target)
    inputs += _role_inputs(plan, "replacement", "replacement",
                           "reference_image", False, target)
    by_id = {i["id"]: i for i in inputs}
    seg_of = {}
    for seg in a_segments:
        for frame in range(seg["start"], seg["end"]):
            seg_of[frame] = seg
    requests, warnings = [], []
    for frame in needed:
        seg = seg_of.get(frame)
        slot = slots.get(frame)
        if slot in CONTROL_SLOT_ROLES and (seg is not None):
            kind = slot.lower()
        elif frame == 0:
            kind = "first_frame"
        else:
            kind = "inbetween"
        seg_slots = sorted(f for f in slots
                           if seg and seg["start"] <= f < seg["end"])
        request_inputs = ["prompt"]
        request_inputs += [i["id"] for i in inputs
                           if i["id"].startswith(("master", "layout",
                                                  "guide"))]
        if kind in {"keypose", "breakdown", "pose"}:
            request_inputs += [f"control:{slots[f]}:{f}" for f in seg_slots
                               if f < frame]
        else:
            request_inputs += [f"control:{slots[f]}:{f}" for f in seg_slots]
        request_inputs += [i["id"] for i in inputs
                           if i["id"].startswith(("mask", "replacement"))]
        # Prior produced control images this request is sequenced behind.
        depends = [f"{shot_id}:{slots[f].lower()}:{f}"
                   for f in seg_slots
                   if by_id[f"control:{slots[f]}:{f}"]["pin"] is None
                   and (kind not in {"keypose", "breakdown", "pose"}
                        or f < frame)]
        carried, images = [], 0
        for input_id in request_inputs:
            item = by_id[input_id]
            if item["disposition"] != "INCLUDE":
                continue
            if item["kind"] != "PROMPT":
                images += 1
                if target["max_images"] is not None \
                        and images > target["max_images"]:
                    continue
            carried.append(input_id)
        blocked = [f"required input {iid} is not carried by target "
                   f"'{target['id']}' ({by_id[iid]['reason']})"
                   for iid in request_inputs
                   if by_id[iid]["required"] and iid not in carried
                   and by_id[iid]["disposition"] == "NOT_SENT"]
        requests.append({
            "request_id": f"{shot_id}:{kind}:{frame}", "kind": kind,
            "frame": frame,
            "segment": [seg["start"], seg["end"]] if seg else None,
            "produces": {"role": kind, "frame": frame,
                         "fills_member": True},
            "inputs": request_inputs, "carries": carried,
            "aux": ["previous_frame"]
                   if (frame - 1) in needed_set else [],
            "depends_on": depends, "blocked": blocked,
            "prompt": _request_prompt(shot_id, description, kind, frame,
                                      fps, length)})
    anchor = next((f for f in needed if f >= 0), 0)
    for missing, role in (([a for a in plan["assets"] if a["role"] == "master"],
                           "master"),
                          ([a for a in plan["assets"] if a["role"] == "layout"],
                           "layout")):
        if missing:
            continue
        warnings.append(f"{shot_id}: plan declares no '{role}' asset; "
                        "a reference request covers producing it, then pin "
                        "it in the plan")
        requests.insert(0, {
            "request_id": f"{shot_id}:reference:{role}", "kind": "reference",
            "frame": None, "segment": None,
            "produces": {"role": "layout", "frame": anchor,
                         "fills_member": False, "fills_input": role},
            "inputs": ["prompt"], "carries": ["prompt"], "aux": [],
            "depends_on": [], "blocked": [],
            "prompt": _request_prompt(shot_id, description, "reference",
                                      None, fps, length)})
    if not pure_a:
        warnings.append(f"{shot_id}: mixed-path plan; frames outside A "
                        "segments are not covered by this packet")
    pending = [f"{c['role']}@{c['frame']}" for c in controls
               if c["pin"] is None]
    if pending:
        warnings.append(f"{shot_id}: control slots not yet produced: "
                        + ", ".join(pending))
    not_sent = [{"input": i["id"], "reason": i["reason"]}
                for i in inputs if i["disposition"] == "NOT_SENT"]
    blocked_requests = [r["request_id"] for r in requests if r["blocked"]]
    if blocked_requests:
        warnings.append(f"{shot_id}: requests with required inputs the "
                        f"target cannot take: {blocked_requests}")
    target_q = shlex.quote(str(p))
    return {"document_type": PACKET_TYPE, "schema_version": 1,
            "shot_id": shot_id, "instance_id": entry["instance_id"],
            "plan_sha256": plan_sha, "plan_revision": plan["revision"],
            "canvas": {"width": fmt["width"], "height": fmt["height"]},
            "fps": fps, "used_source_range": [start, end],
            "unused_handles": dict(handles),
            "segments": a_segments,
            "controls": controls, "inputs": inputs, "requests": requests,
            "transport": {
                "mode": "MANUAL_PACKET",
                "target": {"id": target["id"],
                           "accepts": list(target["accepts"]),
                           "max_images": target["max_images"]},
                "not_sent": not_sent,
                "note": "A person carries these inputs by hand. The "
                        "software transmits nothing, automates no consumer "
                        "UI and submits no paid request."},
            "import": {
                "control": f"python -m engine.cli import-control {target_q} "
                           f"{shot_id} --role <role> --frame <n> --file "
                           "<image.png> --references "
                           "<asset_id>:<rev>:<sha256>,...",
                "commit": f"python -m engine.cli commit-draft-frames "
                          f"{target_q} {shot_id}"},
            "warnings": warnings,
            "notes": [
                "Request kinds are distinct fields: reference, first_frame, "
                "keypose, breakdown, pose, inbetween — never one mixed "
                "'frames' string.",
                "The previous produced frame is an auxiliary reference "
                "only; no request rests on it alone.",
                "Every produced image imports as DRAFT against this packet; "
                "qualification UNQUALIFIED."]}


def export_work_packets(project, shots=None, wave=None, target=None):
    """Write A-path work packets under `animation/packets/`.

    `shots` limits the selection; `wave` selects a declared production wave;
    the default covers every timeline shot. Shots without a plan or without
    an A-path segment are reported under `skipped`, never silently emitted.
    The packet is pinned to the current plan file digest — a changed plan
    makes it stale until regenerated.
    """
    p = Path(project)
    with project_mutex(p):
        config = require_animation_profile(p)
        timeline = load_animation_timeline(p)
        target = _check_target(target)
        from .animation_assets import load_registry
        registry = load_registry(p)
        if wave is not None:
            from .animation_locks import load_waves, wave_for_shot
            waves = load_waves(p)
            if waves is None:
                raise FilmError("No production waves are declared "
                                "(production/waves.json)")
            selected = [s for s in {e["shot_id"]
                                    for e in timeline["entries"]}
                        if wave_for_shot(waves, s) == wave]
            if not selected:
                raise FilmError(f"{wave} names no timeline shots")
        elif shots:
            selected = list(shots)
        else:
            selected = [e["shot_id"] for e in timeline["entries"]]
        fmt = config["format"]
        descriptions = {s["id"]: s.get("description", "")
                        for s in read(p / "manifest/shots.json", [])}
        out_dir = safe_path(p, PACKET_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        packets, skipped = [], []
        for shot_id in selected:
            entries = [e for e in timeline["entries"]
                       if e["shot_id"] == shot_id]
            if len(entries) != 1:
                raise FilmError(
                    f"{shot_id} must have exactly one timeline entry "
                    f"(found {len(entries)})")
            entry = entries[0]
            start, end = entry["used_source_range"]
            from .motion_plan import load_shot_plan, plan_path
            plan_file = safe_path(p, plan_path(shot_id))
            if not plan_file.is_file():
                skipped.append({"shot_id": shot_id, "reason": "NO_PLAN"})
                continue
            plan = load_shot_plan(
                p, shot_id, length=end - start,
                canvas={"width": fmt["width"], "height": fmt["height"]})
            if not any(s["path"] == "A" for s in plan["segments"]):
                skipped.append({"shot_id": shot_id,
                                "reason": "NO_A_SEGMENTS"})
                continue
            document = _build_work_packet(
                p, entry, plan, digest(plan_file), registry, target, fmt,
                descriptions.get(shot_id, ""))
            validate_work_packet(document)
            write_canon(safe_path(p, f"{PACKET_DIR}/{shot_id}.json"),
                        document)
            packets.append({"shot_id": shot_id,
                            "path": f"{PACKET_DIR}/{shot_id}.json",
                            "requests": len(document["requests"]),
                            "warnings": document["warnings"]})
        atomic_text(out_dir / "index.html",
                    _work_packet_page(p, packets, skipped, target))
        return {"packets": packets, "skipped": skipped,
                "index": f"{PACKET_DIR}/index.html",
                "qualification_state": "UNQUALIFIED",
                "note": "Manual work instructions; no provider contact, "
                        "no approval"}


def _work_packet_page(p, packets, skipped, target):
    """A copy/paste aid for the manual A path — no provider contact."""
    e = lambda value: html.escape(str(value), quote=True)

    def prompt_box(box_id, text):
        return (f'<textarea id="{box_id}" readonly>{e(text)}</textarea>'
                f'<button data-copy="{box_id}">복사</button>')

    cards = []
    for packet in packets:
        document = read_canon(safe_path(p, packet["path"]))
        inputs = {i["id"]: i for i in document["inputs"]}
        rows = []
        for request in document["requests"]:
            carried = ", ".join(
                f"{iid} ({inputs[iid]['kind'].lower()})"
                for iid in request["carries"] if iid != "prompt") or "—"
            not_carried = [f"{iid} [{inputs[iid]['disposition']}]"
                           for iid in request["inputs"]
                           if iid not in request["carries"]
                           and iid != "prompt"]
            label = (f"{request['kind']} · frame {request['frame']}"
                     if request["frame"] is not None
                     else request["kind"])
            rows.append(
                f'<h3>{e(label)}</h3>'
                f'<p>전달: <code>{e(carried)}</code>'
                + (f' · 미전달: <code>{e(", ".join(not_carried))}</code>'
                   if not_carried else "")
                + "</p>"
                + prompt_box(f'{packet["shot_id"]}-{request["request_id"]}',
                             request["prompt"]))
        cards.append(f'<article><h2>{e(packet["shot_id"])} · '
                     f'{packet["requests"]} requests</h2>'
                     + "".join(rows)
                     + f'<p>가져오기: <code>{e(document["import"]["control"])}'
                     f"</code><br>시퀀스 묶기: "
                     f'<code>{e(document["import"]["commit"])}</code></p>'
                     + (f'<ul class="warn">'
                        + "".join(f"<li>{e(w)}</li>"
                                  for w in packet["warnings"])
                        + "</ul>" if packet["warnings"] else "")
                     + "</article>")
    skipped_rows = "".join(
        f"<li>{e(s['shot_id'])}: {e(s['reason'])}</li>" for s in skipped)
    script = ("document.addEventListener('click',ev=>{const b=ev.target."
              "closest('button[data-copy]');if(!b)return;const t=document."
              "getElementById(b.dataset.copy);t.select();(navigator."
              "clipboard?navigator.clipboard.writeText(t.value):Promise."
              "reject()).catch(()=>document.execCommand('copy')).then(()=>"
              "{b.textContent='복사됨';setTimeout(()=>b.textContent='복사'"
              ",1500)})});")
    return ('<!doctype html><html lang="ko"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,'
            'initial-scale=1"><title>FILM UNIT — A-path work packets'
            '</title><style>'
            'body{background:#f3f3f3;color:#181818;font:16px system-ui;'
            'margin:32px auto;max-width:960px;padding:0 16px}'
            'article{border-top:1px solid #aaa;padding:16px 0 24px}'
            'textarea{width:100%;min-height:120px;font:13px ui-monospace,'
            'monospace;box-sizing:border-box}button{margin:6px 0}'
            '.warn{color:#9b2c2c}code{word-break:break-all}</style>'
            '<h1>FILM UNIT / A-path 작업 packet</h1>'
            f'<p>대상: <b>{e(target["id"])}</b>. 각 요청의 프롬프트를 복사해 '
            '구독 앱에서 이미지를 만들고, 결과 파일을 가져오기 명령으로 '
            '역할·시점·참조와 함께 넣으세요. 이 페이지는 아무 서비스도 '
            '호출하지 않고, 소비자 UI를 자동 조작하지 않습니다.</p>'
            + (f'<ul class="warn">건너뜀:{skipped_rows}</ul>'
               if skipped_rows else "")
            + "".join(cards) + f"<script>{script}</script></html>")
