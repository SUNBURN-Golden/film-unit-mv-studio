"""Pure numpy helpers for a CPU frame interpolator.

No session, no weights, and no network. The ONNX adapter lives in
``rife_onnx`` and is the only caller that talks to a runtime.
"""
import numpy as np

from ..core import FilmError


def to_planar(hwc):
    """HWC uint8 RGB -> NCHW float32 in ``[0, 1]``, shape ``(1, 3, H, W)``."""
    arr = np.asarray(hwc)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise FilmError("to_planar expects HWC RGB")
    if arr.dtype != np.uint8:
        raise FilmError("to_planar expects uint8 RGB")
    unit = arr.astype(np.float32) / np.float32(255.0)
    return np.ascontiguousarray(np.transpose(unit, (2, 0, 1))[None, ...])


def round_half_even_uint8(unit):
    """Map ``[0, 1]`` floats to uint8 with round-half-even (banker's)."""
    scaled = np.asarray(unit, dtype=np.float64) * 255.0
    rounded = np.round(scaled)
    return np.clip(rounded, 0, 255).astype(np.uint8)


def planar_to_uint8(planar):
    """NCHW or CHW float RGB in ``[0, 1]`` -> HWC uint8."""
    arr = np.asarray(planar)
    if arr.ndim == 4:
        if arr.shape[0] != 1:
            raise FilmError("planar batch must be 1")
        arr = arr[0]
    if arr.ndim != 3 or arr.shape[0] != 3:
        raise FilmError("planar_to_uint8 expects CHW or NCHW RGB")
    return round_half_even_uint8(np.transpose(arr, (1, 2, 0)))


def pad_to_32(planar):
    """Pad the last two axes up to a multiple of 32 with zeros.

    Returns ``(padded, (height, width))`` where the pair is the size before
    padding. Cropping back to that size is the inverse.
    """
    arr = np.ascontiguousarray(planar, dtype=np.float32)
    if arr.ndim < 2:
        raise FilmError("pad_to_32 expects height and width axes")
    height, width = int(arr.shape[-2]), int(arr.shape[-1])
    if height < 1 or width < 1:
        raise FilmError("pad_to_32 expects a positive frame size")
    pad_h = (32 - height % 32) % 32
    pad_w = (32 - width % 32) % 32
    if pad_h or pad_w:
        widths = [(0, 0)] * (arr.ndim - 2) + [(0, pad_h), (0, pad_w)]
        arr = np.pad(arr, widths, mode="constant", constant_values=0)
    return arr, (height, width)


def crop_to(planar, size):
    """Drop the pad added by :func:`pad_to_32`."""
    height, width = size
    if type(height) is not int or type(width) is not int:
        raise FilmError("crop size must be integers")
    if height < 1 or width < 1:
        raise FilmError("crop size must be positive")
    if planar.shape[-2] < height or planar.shape[-1] < width:
        raise FilmError("crop size exceeds the padded frame")
    return planar[..., :height, :width]


def timesteps_for_owned(owned):
    """Timesteps for an owned range plus the shared end anchor.

    Index ``k`` is ``k/owned`` for ``k`` in ``0 .. owned`` inclusive, so the
    list has ``owned + 1`` entries. ``0/owned`` is the start anchor and
    ``owned/owned`` is the end anchor the importer drops.
    """
    if type(owned) is not int or owned < 1:
        raise FilmError("owned frame count must be an integer >= 1")
    return [f"{k}/{owned}" for k in range(owned + 1)]


def build_manifest(frames_dir, frame_names, fps, width, height, tool):
    """Returned-clip manifest: the fake shape plus a ``tool`` binding."""
    if type(frame_names) is not list or not frame_names:
        raise FilmError("manifest needs frame names")
    if any(type(name) is not str or not name for name in frame_names):
        raise FilmError("manifest frame names must be non-empty strings")
    if type(tool) is not dict:
        raise FilmError("manifest tool binding must be an object")
    return {"frames_dir": frames_dir,
            "frame_names": list(frame_names),
            "frame_count": len(frame_names),
            "fps": fps, "width": width, "height": height,
            "endpoint_rule": "START_END_INCLUDED",
            "first_pts": {"num": 0, "den": 1},
            "tool": dict(tool)}
