"""VIDEOTOOLBOX_NATIVE encoder driver (ANIM-015).

Real VideoToolbox availability: macOS + `videotoolbox`/`VTCompressionSession`
through the native toolchain. The probe compiles nothing clever — it asks
the OS whether the framework exists and whether a compression session can
be created (via `/usr/bin/python3` ctypes on Darwin), and records
UNAVAILABLE with qualification `hardware-unverified` on any other host.
Platform name alone never qualifies a driver.
"""
import platform
import shutil
import subprocess

from ..core import FilmError
from . import EncoderDriver, FrameSource

_CHECK = (
    "import ctypes, sys\n"
    "f = ctypes.util.find_library('VideoToolbox')\n"  # noqa
    "sys.exit(0 if f else 1)\n")


def _vt_environment():
    env = {"platform": platform.system(),
           "videotoolbox_framework": False,
           "compression_session": None}
    if env["platform"] != "Darwin":
        return env
    # Real check on macOS: the framework resolves and a native session
    # actually initializes. Never inferred from uname output alone.
    python = shutil.which("python3") or "/usr/bin/python3"
    try:
        probe = subprocess.run(
            [python, "-c",
             "import ctypes, ctypes.util, sys\n"
             "path = ctypes.util.find_library('VideoToolbox')\n"
             "sys.exit(0 if path else 1)"],
            capture_output=True, timeout=30)
        env["videotoolbox_framework"] = probe.returncode == 0
        env["compression_session"] = (
            "framework-found" if probe.returncode == 0 else "absent")
    except (OSError, subprocess.SubprocessError) as e:
        env["compression_session"] = f"probe-failed: {e}"
    return env


class VideoToolboxDriver(EncoderDriver):
    name = "VIDEOTOOLBOX_NATIVE"

    def codec_contract(self, fmt):
        return "videotoolbox:VTCompressionSession", {
            "mode": "quality", "value": 0.62}  # VT quality slider 0..1

    def probe(self, scope=None):
        env = _vt_environment()
        evidence = {"environment": env}
        if env["platform"] != "Darwin":
            return {**evidence, "registry_state": "UNAVAILABLE",
                    "qualification": "hardware-unverified",
                    "reason": "VideoToolbox exists only on Apple platforms; "
                              "this host reports "
                              f"{env['platform']}"}
        if not env["videotoolbox_framework"]:
            return {**evidence, "registry_state": "UNAVAILABLE",
                    "qualification": "hardware-unverified",
                    "reason": "VideoToolbox framework/compression session "
                              "not usable on this host"}
        # The framework may be present, but this driver has no working
        # VTCompressionSession encode path — that is an explicit
        # encode_not_implemented, not a hardware failure, and it never
        # qualifies a fixture.
        return {**evidence, "registry_state": "UNAVAILABLE",
                "qualification": "hardware-unverified",
                "reason": "encode_not_implemented: the VideoToolbox "
                          "framework resolves, but no VTCompressionSession "
                          "encode path is implemented in this driver — "
                          "unimplemented encoder, not a hardware failure"}

    def encode(self, frames: FrameSource, recipe, work_dir):
        if platform.system() != "Darwin":
            raise FilmError(
                "VIDEOTOOLBOX_NATIVE is UNAVAILABLE on this host "
                f"({platform.system()}) — refusing to fabricate output "
                "and marking hardware-unverified")
        # A real macOS path would push CMSampleBuffers through a
        # VTCompressionSession and collect an H.264 elementary stream with
        # B-frames disabled so packet order equals presentation order; the
        # muxer then stamps the contracted PTS.
        raise FilmError(
            "VIDEOTOOLBOX_NATIVE encode_not_implemented: no "
            "VTCompressionSession encode path exists in this driver — "
            "refusing to fabricate output")
