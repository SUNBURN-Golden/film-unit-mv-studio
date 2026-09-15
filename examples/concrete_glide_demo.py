"""Prepare an original, deterministic, offline city animatic; never render Final.

The drawings are geometric blocking boards, not finished painterly artwork or
approved character sheets. Twelve selected shots in a 240s demo use local depth
parallax to test camera timing; the others are intentional storyboard holds.
Run the standard ``engine.cli compile-preview`` separately after preparation.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw

from engine.audio import analyze, synth_test_audio
from engine.core import FilmError, atomic_text, frame_at, init_project, read, validate_manifest, write
from engine.production import font, generate_storyboard, load_preset, make_package
from engine.resolver import register_asset


W, H, FPS = 960, 720, 24
STAGES = [
    ("LOC_WATER_TOWER_ROOF", "01 / ROOF DEPARTURE", "Mira and Tov trace a route across the water-tower roof."),
    ("LOC_SWITCHBACK_ALLEY", "02 / SWITCHBACK DESCENT", "The pair descend into a folded alley; the city narrows around them."),
    ("LOC_UNDER_VIADUCT", "03 / BELOW THE VIADUCT", "Concrete columns interrupt the route and briefly separate the silhouettes."),
    ("LOC_MARKET_SKYLINE", "04 / MARKET PANORAMA", "The route opens above the market; warm windows reveal another path."),
    ("LOC_EMPTY_FOUNDRY", "05 / FOUNDRY PAUSE", "Mira waits while Tov chooses a way through the empty foundry."),
    ("LOC_LANTERN_PASSAGE", "06 / THE WAY HOME", "Tov leads the return through the lantern passage; Mira follows."),
]
VIEWS = ["establishing wide", "spatial approach", "Mira route check", "Tov looks ahead",
         "shared threshold", "depth crossing", "quiet two-shot", "departure wide"]
PALETTE = {"night_indigo": "#17244D", "gunjo_blue": "#304E8A", "concrete": "#7D8794",
           "concrete_light": "#B1B6BE", "ink": "#101728", "window_amber": "#F2B66D",
           "signal_coral": "#DD6F64", "mist": "#BBC8D7"}


def draw_frame(stage, shot_index=0, progress=0.0, moving=False, palette=None):
    """Draw one original geometric camera-blocking frame (RGB, 4:3).

    Three independently displaced depth layers make the local camera draft
    visibly different from zooming a flattened painting. Characters remain
    blocking silhouettes; this does not simulate finished character animation.
    """
    c = {**PALETTE, **(palette or {})}
    im = Image.new("RGB", (W, H), c["night_indigo"])
    d = ImageDraw.Draw(im)
    view = shot_index % len(VIEWS)
    travel = (progress - .5) * 2 if moving else (view - 3.5) * .07
    # The descent moves vertically as well as laterally; distant towers lag.
    cx = travel * (56 if stage in (1, 5) else 90)
    cy = travel * (34 if stage == 1 else -10)

    def line(points, fill, width=2):
        d.line(points, fill=fill, width=width)

    def poly(points, fill, outline=None):
        d.polygon(points, fill=fill, outline=outline)

    # Far depth: a continuous concrete skyline, varied by stage and shot.
    d.rectangle((0, 220, W, 530), fill=c["gunjo_blue"])
    for k in range(-2, 15):
        x = k * 83 - cx * .15
        top = 160 + ((k * 53 + stage * 31) % 170) - cy * .15
        d.rectangle((x, top, x + 66, 535), fill="#263861")
        line([(x + 12, top), (x + 12, top - 29)], c["concrete"], 2)
        for wy in range(int(top) + 25, 500, 35):
            for wx in (x + 12, x + 37):
                if (k + wy // 35) % 3 != 0:
                    d.rectangle((wx, wy, wx + 8, wy + 12), fill=c["window_amber"])
    d.ellipse((756 - cx * .08, 89, 796 - cx * .08, 129), fill=c["mist"])
    if stage in (3, 5):
        # The departure tower stays an orientation landmark on the return.
        tx = (758 if stage == 3 else 481) - cx * .15
        d.rectangle((tx, 340, tx + 63, 407), fill=c["concrete_light"], outline=c["ink"], width=2)
        d.rectangle((tx + 9, 327, tx + 54, 343), fill=c["concrete"], outline=c["ink"], width=2)
        d.rectangle((tx + 19, 318, tx + 44, 329), fill=c["concrete_light"])
        for x in (tx + 9, tx + 52):
            line([(x, 406), (x, 431)], c["ink"], 4)

    # Mid depth: six locations with genuinely different spatial blocking.
    mx, my = cx * .55, cy * .55
    if stage == 0:
        poly([(-40, 460 - my), (650 - mx, 386 - my), (1030, 505), (1030, 680), (-40, 680)], c["concrete"])
        for y in (486, 544, 608):
            line([(0, y), (960, y - 82)], c["ink"], 2)
        tx = 177 - mx
        d.rectangle((tx, 248 - my, tx + 174, 392 - my), fill=c["concrete_light"], outline=c["ink"], width=4)
        d.rectangle((tx + 22, 225 - my, tx + 152, 250 - my), fill=c["concrete"], outline=c["ink"], width=4)
        d.rectangle((tx + 48, 210 - my, tx + 127, 228 - my), fill=c["concrete_light"], outline=c["ink"], width=3)
        for x in (tx + 20, tx + 145):
            line([(x, 388 - my), (x - 15, 489 - my)], c["ink"], 9)
        for y in range(300, 454, 20):
            line([(tx + 88, y - my), (tx + 110, y - my)], c["ink"], 3)
    elif stage in (1, 5):
        vx, vy = 487 - mx * .2, 335 - my
        poly([(0, 95), (vx - 85, vy), (vx - 85, 525), (0, 690)], c["concrete"])
        poly([(960, 82), (vx + 90, vy - 20), (vx + 90, 525), (960, 690)], "#526079")
        poly([(0, 690), (vx - 85, 525), (vx + 90, 525), (960, 690)], "#303E59")
        for i in range(1, 6):
            y = 520 + i * i * 6
            line([(0, y), (960, y)], c["concrete_light"], 2)
        for side in (-1, 1):
            for i in range(4):
                x = vx + side * (135 + i * 82) - mx * .15
                y = 338 - i * 31
                d.rectangle((x - 20, y, x + 18, y + 65), fill=c["window_amber"], outline=c["ink"], width=3)
                line([(x, y - 65), (x, y)], c["ink"], 3)
                if stage == 5:
                    d.ellipse((x - 20, y - 62, x + 20, y - 12), fill=c["window_amber"], outline=c["ink"], width=3)
        if stage == 1:
            # A short stair flight explains the route instead of a generic zoom.
            for i in range(6):
                x, y = 50 + i * 33 - mx, 560 - i * 15 - my
                line([(x, y), (x + 95, y), (x + 95, y - 15)], c["concrete_light"], 5)
    elif stage == 2:
        poly([(0, 120), (960, 204), (960, 315), (0, 262)], c["concrete"])
        line([(0, 163), (960, 244)], c["ink"], 7)
        d.rectangle((0, 549, 960, 690), fill="#46536C")
        for x in (-20, 300, 620, 940):
            x -= mx
            poly([(x, 250), (x + 66, 256), (x + 96, 603), (x - 18, 603)], c["concrete_light"], c["ink"])
            line([(x + 50, 285), (x + 71, 580)], c["concrete"], 3)
        for y in (594, 626, 672):
            line([(0, y), (960, y - 24)], c["mist"], 2)
    elif stage == 3:
        for k in range(-1, 7):
            x = k * 179 - mx
            y = 480 + ((k * 37) % 55)
            d.rectangle((x, y, x + 159, 650), fill=c["concrete"], outline=c["ink"], width=3)
            poly([(x - 8, y), (x + 78, y - 52), (x + 171, y)], c["signal_coral"], c["ink"])
            for j in range(3):
                d.rectangle((x + 20 + 43 * j, y + 26, x + 43 + 43 * j, y + 71), fill=c["window_amber"])
        poly([(0, 572), (350, 520), (960, 620), (960, 695), (0, 695)], "#46536C")
        line([(0, 573), (350, 521), (960, 621)], c["mist"], 5)
    else:  # The foundry is open and quiet, with a broken truss and route gap.
        d.rectangle((0, 523, 960, 690), fill="#48546A")
        for x in (130, 785):
            x -= mx
            d.rectangle((x, 232, x + 52, 561), fill=c["concrete"], outline=c["ink"], width=4)
        line([(120 - mx, 239), (420 - mx, 159), (792 - mx, 239)], c["concrete_light"], 12)
        for x in range(175, 730, 98):
            line([(x - mx, 228), (x + 68 - mx, 189), (x + 94 - mx, 239)], c["concrete"], 5)
        poly([(0, 590), (287, 555), (392, 598), (467, 670), (0, 690)], c["concrete"])
        poly([(527, 665), (590, 565), (960, 598), (960, 690)], c["concrete"])
        line([(369, 588), (592, 571)], c["window_amber"], 4)

    # Fixed-seed hatch marks suggest rough concrete without random glyphs.
    rng = random.Random(740 + stage)
    for _ in range(95):
        x, y = rng.randrange(W), rng.randrange(545, 676)
        line([(x - mx, y), (x + rng.randrange(3, 15) - mx, y - 2)], "#59677E", 1)

    def figure(x, y, scale, who):
        def pts(values):
            return [(x + a * scale, y + b * scale) for a, b in values]

        def box(values, fill):
            a, b, e, f = values
            d.rectangle((x + a * scale, y + b * scale, x + e * scale, y + f * scale), fill=fill)

        skin = c["window_amber"]
        coat = "#BA8C4D" if who == "Mira" else c["signal_coral"]
        # Mira's flat crop + long vest; Tov's loop bun + short jacket + strap.
        box((-13, -118, 13, -94), skin)
        box((-15, -127, 14, -116), c["ink"])
        if who == "Tov":
            d.ellipse((x - 6 * scale, y - 150 * scale, x + 11 * scale, y - 127 * scale), outline=c["ink"], width=max(2, round(6 * scale)))
        poly(pts([(-21, -94), (19, -94), (27, -35 if who == "Mira" else -53), (-26, -35 if who == "Mira" else -53)]), coat, c["ink"])
        for xx in (-13, 11):
            trouser = c["ink"] if who == "Mira" else c["concrete"]
            width = 12 if who == "Mira" else 18
            line(pts([(xx, -39 if who == "Mira" else -53), (xx + (4 if xx > 0 else -5), -5)]), trouser, max(4, round(width * scale)))
        for xx in (-27, 25):
            sleeve = c["gunjo_blue"] if who == "Mira" else coat
            line(pts([(xx, -84), (xx + (9 if xx > 0 else -7), -53)]), sleeve, max(4, round(9 * scale)))
        if who == "Mira":
            box((4, -60, 19, -43), c["ink"])
        else:
            line(pts([(-15, -91), (19, -53)]), c["mist"], max(2, round(5 * scale)))
        line(pts([(-22, -3), (-10, -3)]), c["ink"], max(3, round(5 * scale)))
        line(pts([(9, -3), (25, -3)]), c["ink"], max(3, round(5 * scale)))

    medium = view in (2, 3, 6)
    scale = 1.35 if medium else .78
    y = 650 if medium else 584
    # Blocking positions change with the camera; both IDs retain their design.
    positions = [(390 - cx * .7, "Mira"), (526 - cx * .7, "Tov")]
    if stage == 5:
        positions = [(526 - cx * .7, "Mira"), (390 - cx * .7, "Tov")]
    for x, who in positions:
        figure(x, y - cy * .7, scale * (1.08 if who == "Mira" else .91), who)

    # Near depth: foreground cables/poles move fastest and occasionally occlude.
    px = cx * 1.35
    for base in (-25, 974):
        x = base - px
        line([(x, 65), (x + 38, 685)], c["ink"], 22)
    for yy in (122, 147):
        line([(-80 - px, yy), (200 - px, yy + 38), (505 - px, yy + 48), (1060 - px, yy - 9)], c["ink"], 3)
    if stage in (0, 3):
        line([(-30, 669), (990, 630)], c["ink"], 12)
    # Typography is deliberately authored, never pseudo-signage.
    d.rectangle((0, 0, W, 60), fill=c["ink"])
    d.text((27, 18), "GUNJO CITY  /  " + STAGES[stage][1], font=font(20), fill=c["mist"])
    d.rectangle((0, 688, W, H), fill=c["ink"])
    label = "LOCAL DEPTH / CAMERA TIMING DRAFT" if moving else "INTENTIONAL STORYBOARD HOLD"
    d.text((24, 695), f"{VIEWS[view].upper()}  |  {label}", font=font(13), fill=c["window_amber"])
    return im


def _write_clip(path, shot, stage, view, palette):
    frames = frame_at(shot["out_ms"], FPS) - frame_at(shot["in_ms"], FPS)
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
            "-pixel_format", "rgb24", "-video_size", f"{W}x{H}", "-framerate", str(FPS),
            "-i", "pipe:0", "-an", "-frames:v", str(frames), "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-threads", "2", str(path)]
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for i in range(frames):
            progress = i / max(1, frames - 1)
            # Smooth acceleration, not an attempted imitation of drawn movement.
            smooth = .5 - .5 * math.cos(math.pi * progress)
            proc.stdin.write(draw_frame(stage, view, smooth, True, palette).tobytes())
        proc.stdin.close()
        error = proc.stderr.read().decode("utf-8", "replace")
        if proc.wait() != 0:
            raise FilmError("Local camera draft failed: " + error[-1500:])
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stderr.close()


def prepare_demo(root, name="concrete_glide_demo", seconds=240, render_motion=True):
    """Prepare only: ordinary project/package APIs, original boards, local clips."""
    if not math.isfinite(seconds) or not 1 <= seconds <= 600:
        raise FilmError("Demo duration must be between 1 and 600 seconds")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise FilmError("FFmpeg and ffprobe are required for this local demo")
    preset = load_preset("concrete_glide")
    brief = ("Original Gunjo City blocking animatic. Mira and Tov navigate a roof, alley, "
             "viaduct, market skyline, foundry and lantern passage. Tov leads the return. "
             "Synthetic test audio, no lyrics. Geometric boards and selective depth-parallax "
             "drafts prove cut/camera timing only; final reference art and human review are pending.")
    with tempfile.TemporaryDirectory(prefix="concrete-glide-") as tmp:
        master = synth_test_audio(Path(tmp) / "demo.wav", seconds)
        p = init_project(root, name, master, brief, synthetic=True)
    config = read(p / "project.yaml")
    config["production"] = {"preset": "concrete_glide"}
    config["budget"].update(max_credits=0, max_usd=0)
    write(p / "project.yaml", config)
    analyze(p)
    shots = make_package(p, preset="concrete_glide")
    sequences, planned = [], []
    for stage in range(6):
        group = [(i, s) for i, s in enumerate(shots) if min(5, i * 6 // len(shots)) == stage]
        if not group:
            continue
        sequences.append({"id": f"SEQ{stage + 1:02d}", "in_ms": group[0][1]["in_ms"],
                          "out_ms": group[-1][1]["out_ms"], "title": STAGES[stage][1],
                          "boundary_source": "director draft stage, anchored to measured audio shot cuts"})
        for local, (_, shot) in enumerate(group):
            view = min(7, local * 8 // len(group))
            moving = bool(render_motion and view in (1, 5))
            intention = "descend and track" if stage == 1 else "glide through depth"
            shot.update(sequence=f"SEQ{stage + 1:02d}", description=STAGES[stage][2] + " " + VIEWS[view] + ".",
                        characters=["CHAR_MIRA", "CHAR_TOV"], locations=[STAGES[stage][0]],
                        composition=VIEWS[view], camera={"type": "spatial", "movement": intention if moving else "none",
                        "production_intention": intention, "preview": "local depth parallax" if moving else "intentional hold"},
                        motion={"complexity": "low", "instruction": "Camera blocking only; character silhouettes held for timing.",
                                "local_effect": "hold"}, render_mode="LIMITED_MOTION" if moving else "STATIC",
                        renderer="manual" if moving else "mock", storyboard_kind="schematic", status="storyboard",
                        demo={"kind": "original geometric camera-blocking draft", "motion_candidate": view in (1, 5),
                              "finished_character_animation": False, "stage": stage, "view": view})
            planned.append((shot, stage, view, moving))
    validate_manifest(shots, read(p / "analysis/audio.json")["duration_ms"], FPS)
    write(p / "manifest/sequence.json", sequences)
    write(p / "manifest/shots.json", shots)
    palette = preset["style"].get("palette", {})
    for shot, stage, view, moving in planned:
        draw_frame(stage, view, 0, moving, palette).save(p / shot["references"][0])
    atomic_text(p / "bible/story.md", "# Gunjo City — original blocking story\n\n" + brief + "\n\n" +
                "\n".join(f"{title}: {story}" for _, title, story in STAGES) + "\n\nNo LOCK: references and character action need director review.\n")
    # No character sheets or visual approvals are fabricated by these schematics.
    generate_storyboard(p)
    for shot, stage, view, moving in planned:
        if moving:
            clip = p / "render/draft" / (shot["id"] + "_camera_previs.mp4")
            _write_clip(clip, shot, stage, view, palette)
            register_asset(p, shot, clip, "draft", generated=False)
        print(f"Prepared {shot['id']} / {STAGES[stage][0]} / {'local camera draft' if moving else 'hold'}", file=sys.stderr, flush=True)
    result = {"project": str(p), "shots": len(shots), "duration_ms": read(p / "analysis/audio.json")["duration_ms"],
              "local_motion_drafts": sum(m for _, _, _, m in planned), "synthetic_audio": True,
              "paid_generations": 0, "status": "PREPARED_UNREVIEWED", "preview_kind": "schematic timing animatic"}
    write(p / "demo_preparation.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="projects")
    parser.add_argument("--name", default="concrete_glide_demo")
    parser.add_argument("--seconds", type=float, default=240)
    parser.add_argument("--storyboard-only", action="store_true", help="Prepare holds only; skip optional local depth drafts")
    args = parser.parse_args(argv)
    try:
        result = prepare_demo(args.root, args.name, args.seconds, not args.storyboard_only)
    except (FilmError, OSError, ValueError) as error:
        print(f"CONCRETE GLIDE: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
