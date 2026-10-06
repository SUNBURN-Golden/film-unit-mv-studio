"""FFMPEG encoder driver (ANIM-015).

The existing CPU libx264 path, refactored behind the EncoderDriver
interface: image2 input with the contracted explicit `-framerate` and
`-start_number 1`, producing a timed video-only MP4 of H.264 codec packets.
Container muxing and audio are the muxer's job, not the encoder's.

Probe = a real fixture encode through the full encode -> mux -> verify
pipeline in a scratch directory; the fixture never substitutes for a
production qualification of other resolutions/durations.
"""
from pathlib import Path
import json
import shutil
import tempfile

import numpy as np
from PIL import Image

from ..core import FilmError, digest, ffmpeg, run
from . import (DELIVERY_PROFILE_MV_H264_AAC_V1, EncoderDriver, FrameSource)


def _ffmpeg_environment():
    env = {"binary": None, "version": None, "ffprobe": None,
           "encoders": []}
    try:
        env["version"] = run(["ffmpeg", "-version"]).decode().splitlines()[0]
        env["binary"] = shutil.which("ffmpeg")
        env["ffprobe"] = shutil.which("ffprobe")
    except (FilmError, OSError):
        return env
    try:
        encoders = run(["ffmpeg", "-hide_banner", "-encoders"]).decode()
        env["encoders"] = [line.split()[1] for line in encoders.splitlines()
                           if line.startswith(" V")]
    except FilmError:
        pass
    return env


def _fixture_frames(folder, count=8, size=(64, 48)):
    """Small synthetic RGB frames — enough to prove the encode path."""
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        px = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        px[..., 0] = (np.arange(size[0])[None, :] + i * 11) % 256
        px[..., 1] = (np.arange(size[1])[:, None] + i * 5) % 256
        px[..., 2] = (i * 7) % 256
        Image.fromarray(px, "RGB").save(folder / f"F_{i + 1:06d}.png")
    return folder


def _fixture_audio(path, seconds, sample_rate=16000):
    t = np.linspace(0, seconds, int(sample_rate * seconds),
                    endpoint=False)
    pcm = (np.sin(2 * np.pi * 220 * t) * 0.25 * 32767).astype(np.int16)
    import soundfile
    soundfile.write(str(path), pcm, sample_rate, subtype="PCM_16")
    return path


def run_fixture_encode(driver, scope=None):
    """Real encode+mux+verify of a tiny synthetic fixture.

    Returns {"fixture": {input/output digests}, "scope": {...}} on success;
    raises FilmError otherwise. Shared by the probes of subprocess-style
    drivers so qualification always comes from an actual run.
    """
    from . import encode_delivery, make_encode_recipe
    size = (64, 48)
    count = 8
    with tempfile.TemporaryDirectory(prefix=".encoder-probe-") as tmp:
        tmp = Path(tmp)
        frames = FrameSource(_fixture_frames(tmp / "frames", count, size),
                             24, *size)
        master = _fixture_audio(tmp / "master.wav", count / 24)
        fmt = {"fps": 24, "width": size[0], "height": size[1],
               "crf": 18}
        recipe = make_encode_recipe(driver.name, fmt)
        result = encode_delivery(frames, recipe, tmp / "fixture.mp4",
                                 driver=driver, master=master,
                                 work_dir=tmp / "work",
                                 role="probe", gate_probe=False)
        return {"fixture": {
                    "input_sequence_root": result["sequence_root"],
                    "output_sha256": result["sha256"],
                    "verification_sha256": digest_bytes(
                        result["verification"])},
                "scope": {"width": size[0], "height": size[1],
                          "fps": {"num": 24, "den": 1}, "frames": count,
                          "codec": "h264",
                          "pixel_format":
                              DELIVERY_PROFILE_MV_H264_AAC_V1["video"]
                              ["pixel_format"],
                          "container": "mp4"}}


def digest_bytes(value):
    import hashlib
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class FFmpegDriver(EncoderDriver):
    name = "FFMPEG"

    def codec_contract(self, fmt):
        return "libx264", {"mode": "crf", "value": int(fmt["crf"]),
                           "preset": "veryfast"}

    def probe(self, scope=None):
        env = _ffmpeg_environment()
        evidence = {"environment": env, "operation":
                    "encode_video_packets"}
        if env["binary"] is None or env["ffprobe"] is None:
            return {**evidence, "registry_state": "UNAVAILABLE",
                    "qualification": "missing-tool",
                    "reason": "ffmpeg/ffprobe binaries not found"}
        if "libx264" not in env["encoders"]:
            return {**evidence, "registry_state": "UNAVAILABLE",
                    "qualification": "missing-encoder",
                    "reason": "ffmpeg build has no libx264 encoder"}
        try:
            outcome = run_fixture_encode(self)
        except FilmError as e:
            return {**evidence, "registry_state": "UNAVAILABLE",
                    "qualification": "fixture-failed",
                    "reason": str(e)[-400:]}
        return {**evidence, "registry_state": "QUALIFIED_FOR_SCOPE",
                "qualification": "real-fixture",
                "reason": "fixture encode+mux+verify passed on this host",
                **outcome}

    def encode(self, frames: FrameSource, recipe, work_dir):
        work_dir = Path(work_dir)
        out = work_dir / "video_packets.mp4"
        fps = frames.fps
        crf = recipe["rate_control"]["value"]
        ffmpeg(["-xerror",
                "-framerate", str(fps), "-start_number", "1",
                "-i", str(frames.dir / "F_%06d.png"),
                "-map", "0:v:0", "-an", "-r", str(fps),
                "-c:v", "libx264", "-preset", "veryfast",
                "-crf", str(crf), "-pix_fmt", recipe["pixel_format"],
                "-threads", "2", out])
        return {"path": out, "timed": True,
                "container": "mp4", "packets": "h264"}
