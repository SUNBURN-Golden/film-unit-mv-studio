"""Synthetic 1920x1080 RIFE segment demo.

Two anchors, 24 owned frames, path-B quote/submit/reconcile/import/commit
with ``--adapter rife_onnx``, then the existing FFMPEG encoder driver.
No GStreamer step. No weight download. A missing runtime or pin is recorded
as UNQUALIFIED and is not replaced with a blend.

The MP4 and PNGs stay in a temporary directory. The committed record is
``docs/evidence/anim-015-rife/demo.json``.
"""
import hashlib
import platform
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from PIL import Image

from engine.animation_migrate import animation_init
from engine.animation_schema import canon_bytes, write_canon
from engine.core import (FilmError, atomic_text, digest, ffmpeg, init_project,
                         now, probe, read, write)
from engine.encoder_backends import (FrameSource, encode_delivery,
                                     make_encode_recipe)
from engine.frame_clock import frame_filename
from engine.interpolation.rife_onnx import build_capability_evidence
from engine.lyrics import prepare_lyrics, save_timing
from engine.model_registry import verify
from engine.motion_plan import save_shot_plan
from engine.segment_gen import (commit_segment_sequence, import_segment_control,
                                segment_import, segment_quote, segment_reconcile,
                                segment_submit)

EVIDENCE = ROOT / "docs" / "evidence" / "anim-015-rife" / "demo.json"
WIDTH, HEIGHT, FPS, OWNED = 1920, 1080, 24, 24


def _source_head():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
            text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


def _anchor(path, seed):
    arr = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    arr[..., 0] = seed * 40
    arr[..., 1] = np.linspace(0, 220, WIDTH, dtype=np.uint8)[None, :]
    arr[..., 2] = np.linspace(seed, 180, HEIGHT, dtype=np.uint8)[:, None]
    Image.fromarray(arr).save(path)
    return path


def _project(root):
    source = root / "synthetic_master.wav"
    ffmpeg(["-f", "lavfi", "-i",
            "sine=frequency=220:sample_rate=16000:duration=1",
            "-c:a", "pcm_s16le", source])
    lyrics = "Synthetic fixture line 01\n"
    p = init_project(root, "rife_demo", source,
                     "Synthetic RIFE demo. Not artwork and not a qualification.",
                     lyrics=lyrics, synthetic=True, aspect="16:9")
    measured_ms = round(float(probe(source)["format"]["duration"]) * 1000)
    if measured_ms != 1000:
        raise FilmError(f"demo audio must be 1000 ms, measured {measured_ms}")
    write(p / "analysis/audio.json", {
        "schema_version": "0.1", "master_sha256": digest(source),
        "duration_ms": measured_ms, "beat_times_ms": [], "onsets_ms": [],
        "section_boundaries_ms": [0, measured_ms],
        "fixture": "Synthetic PCM; duration measured by ffprobe; no feature inference",
    })
    write(p / "manifest/sequence.json", [{
        "id": "SEQ01", "in_ms": 0, "out_ms": measured_ms,
        "title": "Synthetic RIFE demo"}])
    for name in ("style_bible", "characters", "locations"):
        write(p / f"bible/{name}.yaml", {"fixture": "Synthetic test data",
                                         "draft": False})
    atomic_text(p / "bible/story.md", "Synthetic RIFE demo.\n")
    Image.new("RGB", (WIDTH, HEIGHT), (30, 70, 110)).save(
        p / "storyboard/S001.png")
    write(p / "manifest/shots.json", [{
        "id": "S001", "sequence": "SEQ01", "in_ms": 0, "out_ms": measured_ms,
        "duration_ms": measured_ms, "description": "Synthetic RIFE demo",
        "characters": [], "locations": [], "composition": "centered",
        "camera": {"type": "locked", "movement": "none"},
        "motion": {"complexity": "low", "instruction": "Synthetic fixture hold",
                   "local_effect": "hold"},
        "references": ["storyboard/S001.png"],
        "render_mode": "FULL_GENERATIVE", "renderer": "manual",
        "status": "storyboard", "storyboard_kind": "imported",
    }])
    document = prepare_lyrics(p)
    rows = [row for row in document["rows"] if row["kind"] == "lyric"]
    document["cues"] = [{
        "id": "C001", "source_row_id": rows[0]["id"], "text": rows[0]["text"],
        "start_ms": 0, "end_ms": measured_ms - 80}]
    save_timing(p, document,
                reviewer="Synthetic fixture timing; not inferred from vocals")
    animation_init(p)
    fmt = read(p / "project.yaml")["format"]
    if (fmt["width"], fmt["height"], fmt["fps"]) != (WIDTH, HEIGHT, FPS):
        raise FilmError("demo project canvas is not 1920x1080 at 24 fps")
    plan = {
        "document_type": "animation_shot_plan", "schema_version": 1,
        "shot_id": "S001", "revision": 1, "motion_intent": "ANIMATED",
        "story_role": "synthetic rife demo", "emotion_role": "synthetic",
        "canvas": {"width": WIDTH, "height": HEIGHT},
        "background": [10, 20, 30], "rig": None, "assets": [],
        "events": [], "keyposes": [],
        "segments": [{"start": 0, "end": OWNED, "path": "B",
                      "capabilities": ["START_END_IMAGES"]}],
        "tracks": {"camera": {"pivot": [WIDTH // 2, HEIGHT // 2],
                              "transform": None},
                   "layers": {}},
        "preparation": {"work": [], "creator": "anim015_rife_demo",
                        "reviewer": None, "revision_scope": "demo"},
    }
    save_shot_plan(p, plan)
    start = _anchor(root / "start.png", 1)
    end = _anchor(root / "end.png", 2)
    import_segment_control(p, "S001", start, role="keypose", frame=0)
    import_segment_control(p, "S001", end, role="keypose", frame=OWNED)
    return p, start, end


def _run(p, work):
    quote = segment_quote(p, "S001", 0, OWNED, adapter_id="rife_onnx")
    if quote["amount"] != 0 or quote["provider_class"] != "LOCAL_TOOL":
        raise FilmError("rife_onnx quote must be zero credits, LOCAL_TOOL")
    submitted = segment_submit(
        p, "S001", 0, OWNED, adapter_id="rife_onnx",
        approver="synthetic demo", quote_id=quote["quote_id"], cap=0)
    reconciled = segment_reconcile(p, submitted["job_id"],
                                   adapter_id="rife_onnx")
    if reconciled["status"] != "OUTPUT_PENDING_VERIFY":
        raise FilmError(f"reconcile status {reconciled['status']}")
    imported = segment_import(p, submitted["job_id"], adapter_id="rife_onnx")
    if imported["state"] != "DRAFT":
        raise FilmError("imported segment must stay DRAFT")
    if imported["normalization"]["dropped_end_frame"] != OWNED:
        raise FilmError("import must drop the shared end anchor")
    committed = commit_segment_sequence(p, "S001")
    if committed["state"] != "DRAFT" or committed["frames"] != OWNED:
        raise FilmError("committed sequence must be a 24-frame DRAFT")
    from engine.animation_assets import load_registry
    registry = load_registry(p)
    record = registry["assets"][committed["asset_id"]]["revisions"][
        str(committed["revision"])]
    folder = work / "delivery"
    folder.mkdir()
    for item in sorted(record["files"], key=lambda row: row["frame_index"]):
        target = folder / frame_filename(item["frame_index"])
        target.write_bytes((p / item["relative_name"]).read_bytes())
    frames = FrameSource(folder, FPS, WIDTH, HEIGHT)
    recipe = make_encode_recipe("FFMPEG", {"fps": FPS, "width": WIDTH,
                                           "height": HEIGHT, "crf": 18})
    master = p / "input" / "master.wav"
    target = work / "rife-demo.mp4"
    encoded = encode_delivery(frames, recipe, target, master=master)
    if encoded["driver"] != "FFMPEG" or encoded["no_ffmpeg_encoding"] is True:
        raise FilmError("demo encode must stay on FFMPEG with "
                        "no_ffmpeg_encoding false")
    if encoded["verification"]["valid"] is not True:
        raise FilmError("FFMPEG verify did not pass")
    return {"ran": True, "output_sha256": digest(target),
            "verification": {"ok": True, "driver": "FFMPEG",
                             "frames": OWNED,
                             "no_ffmpeg_encoding": False},
            "qualification_state": "PARTIAL"}


def main():
    started = time.perf_counter()
    evidence = build_capability_evidence(write=False)
    weights = verify("rife49")
    outcome = {"ran": False, "output_sha256": None,
               "inputs_sha256": None,
               "verification": {"ok": False, "reason": evidence["reason"]},
               "qualification_state": "UNQUALIFIED"}
    try:
        with tempfile.TemporaryDirectory(prefix="rife-demo-") as raw:
            root = Path(raw)
            project, start, end = _project(root)
            pins = {"start": digest(start), "end": digest(end)}
            outcome["inputs_sha256"] = hashlib.sha256(
                canon_bytes(pins)).hexdigest()
            try:
                done = _run(project, root)
            except FilmError as exc:
                text = str(exc)
                if "CAPABILITY_UNAVAILABLE" not in text:
                    raise
                outcome["verification"] = {
                    "ok": False, "reason": text,
                    "weights": weights["state"],
                    "download": "not attempted",
                    "blend_fallback": False}
            else:
                outcome.update(done)
    except FilmError as exc:
        outcome["verification"] = {"ok": False, "reason": str(exc),
                                   "blend_fallback": False}
        outcome["qualification_state"] = "UNQUALIFIED"
        outcome["ran"] = False
    wall_ms = int((time.perf_counter() - started) * 1000)
    peak_kb = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    document = {
        "schema": 1,
        "node": "anim-015-rife-onnx",
        "host": platform.platform(),
        "source_head": _source_head(),
        "command": "python scripts/anim015_rife_demo.py",
        "scope": {"width": WIDTH, "height": HEIGHT, "fps": FPS,
                  "owned_frames": OWNED, "ran": outcome["ran"],
                  "delivery_frames_claimed": None,
                  "encoding_path": "FFMPEG"},
        "inputs_sha256": outcome["inputs_sha256"],
        "output_sha256": outcome["output_sha256"],
        "verification": outcome["verification"],
        "capability_evidence_id": evidence["evidence_id"],
        "no_ffmpeg_encoding": False,
        "qualification_state": outcome["qualification_state"],
        "throughput_claim": "UNQUALIFIED",
        "wall_time_ms": wall_ms,
        "peak_rss_kb": peak_kb,
        "weights_state": weights["state"],
        "observed_at": now(),
    }
    if document["qualification_state"] == "PARTIAL" and not outcome["ran"]:
        document["qualification_state"] = "UNQUALIFIED"
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    write_canon(EVIDENCE, document)
    print(f"wrote {EVIDENCE}")
    print(f"qualification_state={document['qualification_state']} "
          f"ran={outcome['ran']} no_ffmpeg_encoding=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
