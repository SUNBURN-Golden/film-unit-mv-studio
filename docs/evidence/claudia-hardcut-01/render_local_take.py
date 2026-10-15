"""Local stand-in for the manual generation step (LOCAL_PROCEDURAL, not an AI generation).

Draws a 2D ink-on-paper character shot with PIL and encodes it with FFmpeg. The
mouth opens with the real vocal stem's energy at song time, the arm gesture,
head turn and camera push come from the conti's blocking beats (clip seconds).

usage: render_local_take.py spec.json out.mp4
spec: {"seconds": 6, "fps": 24, "size": [1920, 1080], "song_t0": 23.5,
       "vocals": "/path/vocals.wav", "beats": [{"t": 0.5, "dur": 0.8, "action": "raise_hand"}, ...],
       "push": [1.0, 1.06], "palette": {...}, "seed": 1}
"""
import json, math, subprocess, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

spec = json.load(open(sys.argv[1]))
out = sys.argv[2]
W, H = spec.get("size", [1920, 1080]); FPS = spec.get("fps", 24); SEC = spec["seconds"]
pal = {"paper": "#ECEAE4", "ink": "#181818", "teal": "#48A6A0", "skin": "#F3DCC6", "hair": "#2A2320",
       **spec.get("palette", {})}
rng = np.random.default_rng(spec.get("seed", 1))

# vocal envelope at song time (10 ms RMS) for the mouth
env = None
if spec.get("vocals"):
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", spec["vocals"], "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(pcm, np.float32); n = len(a) // 160
    env = np.sqrt(np.add.reduceat(a[:n * 160] ** 2, np.arange(0, n * 160, 160)) / 160)
    env = env / (np.percentile(env, 98) + 1e-9)


def mouth_open(t):
    if env is None:
        return 0.0
    i = int((spec.get("song_t0", 0) + t) * 100)
    return float(np.clip(env[max(0, i - 2):i + 3].mean() if 0 <= i < len(env) else 0, 0, 1))


def ease(x):
    x = min(1.0, max(0.0, x)); return x * x * (3 - 2 * x)


def beat_amount(t, action):
    """0..1 progress of an action: rises over `dur`, holds, (optional) returns."""
    v = 0.0
    for b in spec.get("beats", []):
        if b["action"] != action:
            continue
        up = ease((t - b["t"]) / b.get("dur", 0.8))
        down = ease((t - b["t"] - b.get("dur", 0.8) - b.get("hold", 99)) / b.get("dur", 0.8))
        v = max(v, up * (1 - down))
    return v


# paper texture once
tex = (rng.normal(0, 6, (H // 4, W // 4)).clip(-14, 14) + 0).astype(np.int16)
tex = np.kron(tex, np.ones((4, 4), dtype=np.int16))[:H, :W]
base = np.array(Image.new("RGB", (W, H), pal["paper"]), dtype=np.int16) + tex[..., None]
paper = Image.fromarray(base.clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2))


def frame(t):
    S = spec.get("supersample", 2)  # draw at Sx then downsample for clean ink lines
    img = paper.resize((W * S, H * S))
    d = ImageDraw.Draw(img)
    ink, lw = pal["ink"], 5 * S
    # hill pass (아리랑 고개) behind — parallax drift
    drift = -40 * S * t / SEC
    pts = [(x * S + drift, (700 - 160 * math.exp(-((x - 1300) / 420) ** 2) - 60 * math.sin(x / 260)) * S)
           for x in range(-100, W + 200, 20)]
    d.line(pts, fill=ink, width=3 * S)
    d.line([(1250 * S + drift, 560 * S), (1480 * S + drift, 900 * S)], fill=ink, width=2 * S)   # the path up the pass
    # wind lines
    for k in range(5):
        y = (180 + 90 * k) * S; x0 = ((t * 260 + k * 400) % (W + 600) - 300) * S
        d.arc([x0, y, x0 + 260 * S, y + 60 * S], 200, 340, fill=ink, width=2 * S)
    cx, cy = 760 * S, 560 * S
    turn = beat_amount(t, "turn_head")
    hand = beat_amount(t, "raise_hand")
    sway = math.sin(t * 2.1) * 4 * S
    # body (teal jacket)
    d.polygon([(cx - 150 * S, cy + 470 * S), (cx - 110 * S, cy + 110 * S), (cx + 110 * S, cy + 110 * S),
               (cx + 150 * S, cy + 470 * S)], fill=pal["teal"], outline=ink, width=lw)
    # left arm resting
    d.line([(cx - 105 * S, cy + 130 * S), (cx - 150 * S, cy + 330 * S), (cx - 135 * S, cy + 430 * S)], fill=ink, width=lw)
    # right arm: from rest (down) to raised toward the pass
    ang = math.radians(100 - 145 * hand)
    sx, sy = cx + 105 * S, cy + 130 * S
    ex, ey = sx + math.cos(ang) * 170 * S, sy + math.sin(ang) * 170 * S
    ang2 = ang - math.radians(25 * hand)
    hx, hy = ex + math.cos(ang2) * 150 * S, ey + math.sin(ang2) * 150 * S
    d.line([(sx, sy), (ex, ey), (hx, hy)], fill=ink, width=lw)
    d.ellipse([hx - 22 * S, hy - 22 * S, hx + 22 * S, hy + 22 * S], fill=pal["skin"], outline=ink, width=3 * S)
    # neck + head
    hx0 = cx + sway + 26 * S * turn
    d.rectangle([cx - 28 * S, cy + 40 * S, cx + 28 * S, cy + 115 * S], fill=pal["skin"], outline=ink, width=3 * S)
    d.ellipse([hx0 - 120 * S, cy - 210 * S, hx0 + 120 * S, cy + 70 * S], fill=pal["skin"], outline=ink, width=lw)
    # hair: bob with wind-swept strands
    d.chord([hx0 - 132 * S, cy - 228 * S, hx0 + 132 * S, cy + 20 * S], 180, 360, fill=pal["hair"], outline=ink, width=lw)
    for k in range(6):
        y = cy - 150 * S + k * 26 * S; wob = math.sin(t * 5 + k) * 16 * S
        d.line([(hx0 + 120 * S, y), (hx0 + 175 * S + wob, y + 18 * S)], fill=pal["hair"], width=7 * S)
    # face: eyes look toward the pass as the head turns
    ex0 = 22 * S * turn
    for sgn in (-1, 1):
        d.ellipse([hx0 + sgn * 46 * S - 9 * S + ex0, cy - 70 * S, hx0 + sgn * 46 * S + 9 * S + ex0, cy - 52 * S], fill=ink)
    mo = mouth_open(t)
    mw, mh = 34 * S, (4 + 30 * mo) * S
    my = cy + 5 * S
    d.ellipse([hx0 - mw + ex0 * 0.6, my - mh / 2, hx0 + mw + ex0 * 0.6, my + mh / 2], fill="#7A2E2E", outline=ink, width=3 * S)
    img = img.resize((W, H), Image.LANCZOS)
    # camera push-in
    z0, z1 = spec.get("push", [1.0, 1.0])
    z = z0 + (z1 - z0) * ease(t / SEC)
    if abs(z - 1) > 1e-4:
        w2, h2 = W / z, H / z; x0 = (W - w2) / 2; y0 = (H - h2) / 2
        img = img.crop((x0, y0, x0 + w2, y0 + h2)).resize((W, H), Image.LANCZOS)
    return img


p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                      "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                      "-pix_fmt", "yuv420p", "-movflags", "+faststart", out], stdin=subprocess.PIPE)
for k in range(int(round(SEC * FPS))):
    p.stdin.write(frame(k / FPS).tobytes())
p.stdin.close(); p.wait()
sys.exit(p.returncode)
