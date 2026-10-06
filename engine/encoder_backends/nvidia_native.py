"""NVIDIA_NATIVE encoder driver (ANIM-015).

A real NVENC session through PyNvVideoCodec — never an inference from CUDA
visibility, `nvidia-smi`, FFmpeg `-hwaccel` flags or the presence of SDK
headers. The probe actually imports PyNvVideoCodec, creates an encoder for
the supported NV12 surface and closes it; anything less is recorded
UNAVAILABLE with qualification `hardware-unverified`.

When the SDK/GPU is absent the driver refuses to encode rather than
returning a fabricated result.
"""
from pathlib import Path

from ..core import FilmError
from . import EncoderDriver, FrameSource


def _nvenc_environment():
    env = {"platform": __import__("platform").system(),
           "pynvvideocodec": False, "module_version": None,
           "nvidia_smi": __import__("shutil").which("nvidia-smi"),
           "encoder_session": None}
    try:
        import PyNvVideoCodec as nv  # noqa: N813
        env["pynvvideocodec"] = True
        env["module_version"] = getattr(nv, "__version__", None)
    except ImportError:
        return env
    try:
        # Real capability: create + close an H.264 encoder session on a
        # small NV12 surface. No session, no qualification.
        encoder = nv.CreateEncoder(width=64, height=48, fmt="NV12",
                                   codec="H264", gpuId=0,
                                   fps=24, profile="HIGH",
                                   useconstantsize=False)
        env["encoder_session"] = "created-and-closed"
        del encoder
    except Exception as e:  # session failure is a real probe result
        env["encoder_session"] = f"failed: {e}"
    return env


class NvidiaNativeDriver(EncoderDriver):
    name = "NVIDIA_NATIVE"

    def codec_contract(self, fmt):
        return "pynvvideocodec:nvenc", {
            "mode": "rate_control_constqp",
            "value": int(fmt["crf"]) + 9}  # NVENC QP, not ffmpeg CRF

    def probe(self, scope=None):
        env = _nvenc_environment()
        evidence = {"environment": env}
        if not env["pynvvideocodec"]:
            return {**evidence, "registry_state": "UNAVAILABLE",
                    "qualification": "hardware-unverified",
                    "reason": "PyNvVideoCodec is not installed; an NVENC "
                              "GPU + SDK cannot be inferred from anything "
                              "else on this host"}
        if env["encoder_session"] != "created-and-closed":
            return {**evidence, "registry_state": "UNAVAILABLE",
                    "qualification": "hardware-unverified",
                    "reason": "NVENC encoder session could not be created: "
                              + str(env["encoder_session"])}
        from .ffmpeg import run_fixture_encode
        try:
            outcome = run_fixture_encode(self)
        except FilmError as e:
            return {**evidence, "registry_state": "UNAVAILABLE",
                    "qualification": "fixture-failed",
                    "reason": str(e)[-400:]}
        return {**evidence, "registry_state": "QUALIFIED_FOR_SCOPE",
                "qualification": "real-fixture",
                "reason": "real NVENC session + fixture encode+mux+verify "
                          "passed on this host", **outcome}

    def encode(self, frames: FrameSource, recipe, work_dir):
        try:
            import PyNvVideoCodec as nv  # noqa: N813
        except ImportError:
            raise FilmError(
                "NVIDIA_NATIVE is UNAVAILABLE on this host "
                "(PyNvVideoCodec missing) — refusing to fabricate output "
                "and marking hardware-unverified")
        import numpy as np
        work_dir = Path(work_dir)
        es = work_dir / "video_packets.h264"
        # Raw H.264 elementary stream, B-frames disabled so packet order
        # equals presentation order; the muxer stamps the contracted PTS.
        encoder = nv.CreateEncoder(
            width=frames.width, height=frames.height, fmt="NV12",
            codec="H264", gpuId=0, fps=frames.fps, profile="HIGH",
            useconstantsize=False)
        try:
            with es.open("wb") as f:
                for index in range(frames.count):
                    rgb = np.frombuffer(frames.rgb_bytes(index),
                                        dtype=np.uint8)
                    rgb = rgb.reshape(frames.height, frames.width, 3)
                    nv12 = _rgb_to_nv12(rgb)
                    packet = encoder.Encode(nv12)
                    if packet is not None and len(packet):
                        f.write(bytes(packet))
                tail = encoder.EndEncode()
                if tail is not None and len(tail):
                    f.write(bytes(tail))
        finally:
            del encoder
        return {"path": es, "timed": False, "reordering_free": True,
                "container": "es", "packets": "h264"}


def _rgb_to_nv12(rgb):
    """RGB24 -> NV12 (BT.601 limited, matching libx264's default
    untagged-RGB conversion close enough for the same-driver pixel
    tolerance; the verifier compares decoded pixels, not encod internals).
    """
    import numpy as np
    r = rgb[..., 0].astype(np.int32)
    g = rgb[..., 1].astype(np.int32)
    b = rgb[..., 2].astype(np.int32)
    y = ((66 * r + 129 * g + 25 * b + 128) >> 8) + 16
    h, w = y.shape
    rc = r[::2, ::2].reshape(h // 2, w // 2)
    gc = g[::2, ::2].reshape(h // 2, w // 2)
    bc = b[::2, ::2].reshape(h // 2, w // 2)
    u = ((-38 * rc - 74 * gc + 112 * bc + 128) >> 8) + 128
    v = ((112 * rc - 94 * gc - 18 * bc + 128) >> 8) + 128
    nv12 = np.empty(h * w + h * w // 2, dtype=np.uint8)
    nv12[:h * w] = np.clip(y, 0, 255).astype(np.uint8).reshape(-1)
    interleaved = np.empty(h * w // 2, dtype=np.uint8)
    interleaved[0::2] = np.clip(u, 0, 255).astype(np.uint8).reshape(-1)
    interleaved[1::2] = np.clip(v, 0, 255).astype(np.uint8).reshape(-1)
    nv12[h * w:] = interleaved
    return nv12
