"""GSTREAMER encoder driver (ANIM-015).

A real subprocess pipeline: the frame contract feeds raw RGB24 frames to
`gst-launch-1.0` on a bounded fdsrc queue, rawvideoparse attaches the
explicit `framerate=24/1` timing, videoconvert converts to I420, and
x264enc (or openh264enc) emits H.264 packets which h264parse/mp4mux carry
into a timed video-only MP4. Nothing on this path relies on encoder
defaults for timing.

Probe checks the gst-launch-1.0 binary and every required plugin with
gst-inspect-1.0; any miss is recorded UNAVAILABLE — a name in the docs is
never evidence.
"""
from pathlib import Path
import shutil
import subprocess

from ..core import FilmError
from . import EncoderDriver, FrameSource


def _gst_environment():
    env = {"binary": shutil.which("gst-launch-1.0"),
           "inspector": shutil.which("gst-inspect-1.0"),
           "version": None, "elements": {}}
    if env["inspector"] is None:
        return env
    try:
        out = subprocess.run(["gst-inspect-1.0", "--version"],
                             capture_output=True, timeout=30)
        env["version"] = out.stdout.decode(errors="replace").splitlines()[-1]
    except (OSError, subprocess.SubprocessError):
        pass
    for element in ("fdsrc", "rawvideoparse", "videoconvert", "x264enc",
                    "openh264enc", "h264parse", "mp4mux", "filesink",
                    "queue"):
        ok = subprocess.run(["gst-inspect-1.0", "--exists", element],
                            capture_output=True).returncode == 0
        env["elements"][element] = ok
    return env


class GStreamerDriver(EncoderDriver):
    name = "GSTREAMER"

    def _h264_encoder(self, env):
        if env["elements"].get("x264enc"):
            return "x264enc"
        if env["elements"].get("openh264enc"):
            return "openh264enc"
        return None

    def codec_contract(self, fmt):
        # gst x264enc `pass=quant` + `quantizer`; openh264enc
        # `rate-control=quality`. The recipe value is this driver's own
        # parameter — it never pretends to be the same as ffmpeg CRF.
        return "gst:rawvideoparse+videoconvert+x264enc", {
            "mode": "quantizer", "value": int(fmt["crf"])}

    def probe(self, scope=None):
        env = _gst_environment()
        evidence = {"environment": env}
        if env["binary"] is None or env["inspector"] is None:
            return {**evidence, "registry_state": "UNAVAILABLE",
                    "qualification": "missing-tool",
                    "reason": "gst-launch-1.0/gst-inspect-1.0 not found"}
        required = {"fdsrc", "rawvideoparse", "videoconvert", "h264parse",
                    "mp4mux", "filesink", "queue"}
        missing = sorted(e for e in required if not env["elements"].get(e))
        if self._h264_encoder(env) is None:
            missing.append("x264enc|openh264enc")
        if missing:
            return {**evidence, "registry_state": "UNAVAILABLE",
                    "qualification": "missing-plugin",
                    "reason": "missing gstreamer elements: "
                              + ", ".join(missing)}
        from .ffmpeg import run_fixture_encode
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
        env = _gst_environment()
        encoder = self._h264_encoder(env)
        if env["binary"] is None or encoder is None:
            raise FilmError(
                "GSTREAMER is UNAVAILABLE on this host "
                "(gst-launch-1.0 or an H.264 encoder element is missing)")
        fps = frames.fps
        if encoder == "x264enc":
            encoder_part = ["x264enc", "pass=quant",
                            f"quantizer={recipe['rate_control']['value']}",
                            "speed-preset=veryfast"]
        else:
            encoder_part = ["openh264enc", "rate-control=quality",
                            "bitrate=4000000"]
        pipeline = [
            "gst-launch-1.0", "-q",
            "fdsrc", "fd=0", "!",
            "queue", "max-size-buffers=8", "!",
            "rawvideoparse",
            f"width={frames.width}", f"height={frames.height}",
            f"framerate={fps}/1", "format=rgb", "!",
            "videoconvert", "!", "video/x-raw,format=I420", "!",
            *encoder_part, "!",
            "h264parse", "config-interval=-1", "!",
            "mp4mux", "streamable=true", "faststart=false", "!",
            "filesink", f"location={out}",
        ]
        proc = subprocess.Popen(
            [str(a) for a in pipeline], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for index in range(frames.count):
                proc.stdin.write(frames.rgb_bytes(index))
            proc.stdin.close()
            stderr = proc.stderr.read()
            code = proc.wait(timeout=600)
        finally:
            if proc.poll() is None:
                proc.kill()
        if code != 0 or not out.exists():
            raise FilmError("gst-launch-1.0 failed: "
                            + stderr.decode(errors="replace")[-2000:])
        return {"path": out, "timed": True,
                "container": "mp4", "packets": "h264"}
