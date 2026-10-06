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
import threading

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
        # The recipe records the encoder element actually chosen on this
        # host and its real rate control: gst x264enc `pass=quant` +
        # `quantizer`; openh264enc `rate-control=quality` + bitrate. The
        # recipe value is this driver's own parameter — it never pretends
        # to be the same as ffmpeg CRF.
        encoder = self._h264_encoder(_gst_environment())
        engine = "gst:rawvideoparse+videoconvert+" \
            + (encoder or "unresolved-no-h264-element")
        if encoder == "openh264enc":
            return engine, {"mode": "rate-control=quality",
                            "bitrate": 4000000}
        if encoder == "x264enc":
            return engine, {"mode": "pass=quant",
                            "quantizer": int(fmt["crf"]),
                            "speed-preset": "veryfast"}
        return engine, {"mode": "unavailable — no H.264 encoder "
                                "element on this host"}

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
        # The encoder named by the recipe's codec_contract is the one used;
        # the element must also exist on this host.
        encoder = recipe["codec_engine"].rsplit("+", 1)[-1]
        if env["binary"] is None or encoder not in ("x264enc",
                                                  "openh264enc") \
                or not env["elements"].get(encoder):
            raise FilmError(
                "GSTREAMER is UNAVAILABLE on this host "
                "(gst-launch-1.0 or the recipe's H.264 encoder element "
                "is missing)")
        fps = frames.fps
        rate_control = recipe["rate_control"]
        if encoder == "x264enc":
            quantizer = rate_control.get("quantizer",
                                         rate_control.get("value"))
            encoder_part = ["x264enc", "pass=quant",
                            f"quantizer={quantizer}",
                            "speed-preset=veryfast"]
        else:
            encoder_part = ["openh264enc", "rate-control=quality",
                            f"bitrate={rate_control.get('bitrate', 4000000)}"]
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
        # Frames are streamed into stdin one at a time while a drain
        # thread keeps stderr moving; stdout is unused, so it is not
        # piped at all. Neither pipe can deadlock.
        proc = subprocess.Popen(
            [str(a) for a in pipeline], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        tail = bytearray()

        def _drain():
            while True:
                chunk = proc.stderr.read(1 << 16)
                if not chunk:
                    return
                tail.extend(chunk)
                del tail[:-65536]

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()
        try:
            try:
                for index in range(frames.count):
                    proc.stdin.write(frames.rgb_bytes(index))
            except (BrokenPipeError, OSError):
                pass  # the pipeline's stderr carries its own error
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                code = proc.wait(timeout=600)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise FilmError("gst-launch-1.0 timed out")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            reader.join(timeout=30)
        if code != 0 or not out.exists():
            raise FilmError("gst-launch-1.0 failed: "
                            + tail.decode(errors="replace")[-2000:])
        return {"path": out, "timed": True,
                "container": "mp4", "packets": "h264"}
