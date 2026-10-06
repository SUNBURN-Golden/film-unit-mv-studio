"""FRAME_ANIMATION_V1 MediaVerifier (ANIM-015, execution design 6.1-6.2).

The verifier is independent of the encoder: it reads only the delivered file,
the fixed FrameSource contract and the DeliveryProfile, never the encoder's
own completion claim (ADR section 10: a worker or LLM "done" sentence is not
verification). The same checks and the same tolerance apply identically to
every driver — an absent hardware encoder must meet the MV_H264_AAC_V1
numbers, not a relaxed per-driver bound.

Checks performed by `verify_delivery`:

- container parses, exactly one video track and (when required) one audio
  track from the designated original master;
- decoded frame count equals the frame clock, rational PTS is exact CFR
  (`pts * time_base == frame_index / fps`, first PTS 0, last exposure ends at
  `frame_count / fps`); average FPS or `nb_frames` alone never proves the
  interval contract;
- size, codec and pixel format match the recipe, and the colour tags match
  the recipe-fixed policy;
- the audio track is the verified master-based AAC: codec, duration covering
  the timeline and a decoded sample count within tolerance, plus a decoded
  PCM comparison against the verified track;
- the whole file decodes cleanly (integrity);
- decoded pixels match the source frames within the profile tolerance
  (`mean_abs_x1000` and `p95_abs`), identical for every driver.

`DeliveryProfile` MV_H264_AAC_V1 is the existing delivery rule: H.264/AAC
MP4, project size, 24/1 CFR, recipe-fixed colour, original-audio policy.
"""
from pathlib import Path
import hashlib
import json
from fractions import Fraction

import numpy as np
from PIL import Image

from .animation_schema import canon_bytes
from .core import FilmError, digest, ffmpeg, probe, run
from .frame_clock import check_fps, last_exposure_end

# DeliveryProfile MV_H264_AAC_V1 — the shared, recipe-fixed delivery contract.
# `pixel_tolerance` is expressed in integers so the profile can be hashed
# through CANON_JSON_V1: mean is scaled by 1000, p95 in channel units 0-255.
# The numbers are identical for every driver; no driver gets a looser bound.
DELIVERY_PROFILE_MV_H264_AAC_V1 = {
    "name": "MV_H264_AAC_V1",
    "container": "mp4",
    "video": {"codec": "h264", "pixel_format": "yuv420p", "timing": "CFR"},
    "audio": {"codec": "aac", "bitrate": "320k", "source": "ORIGINAL_MASTER",
              "policy": "verified track stream-copied, never re-encoded"},
    "color": {"space": "unspecified", "primaries": "unspecified",
              "transfer": "unspecified", "range": "unspecified",
              "source": "sRGB PNG frames",
              "conversion": "encoder-default RGB->YUV 4:2:0 limited range; "
                            "container colour tags are unspecified"},
    "pixel_tolerance": {"mean_abs_x1000": 8000, "p95_abs": 96},
    "audio_tolerance": {"duration_seconds_x1000": 100, "sample_rate_exact": True},
}

DELIVERY_PROFILES = {DELIVERY_PROFILE_MV_H264_AAC_V1["name"]:
                     DELIVERY_PROFILE_MV_H264_AAC_V1}

# The verify contract participates in encode_digest, so it is explicit which
# checks and tolerance the encode was validated against.
VERIFY_CONTRACT = {
    "verifier": "media_verify.verify_delivery/1",
    "frame_count": "exact decoded packet/frame count",
    "pts": "rational CFR: pts*time_base == frame_index/fps, first 0",
    "size": "recipe width/height",
    "pixel_format": "recipe pixel format",
    "color_tags": "recipe-fixed colour fields",
    "audio": "presence, codec, duration and decoded samples of the "
             "verified master track",
    "integrity": "full decode, error-free",
    "pixels": "decoded frames vs source frames within profile tolerance",
}


def image_pixel_sha256(image):
    """Digest of decoded RGB pixels — encoder-independent frame identity."""
    rgb = image.convert("RGB")
    return hashlib.sha256(
        f"{rgb.width}x{rgb.height}:".encode() + rgb.tobytes()).hexdigest()


def frame_pixel_sha256(path):
    with Image.open(path) as im:
        return image_pixel_sha256(im)


def sequence_root(role, digests):
    """One digest over the ordered (frame_index, pixel sha256) list."""
    return hashlib.sha256(canon_bytes(
        {"role": role,
         "frames": [{"frame_index": index, "sha256": value}
                    for index, value in enumerate(digests)]})).hexdigest()


def _rational(field, stream):
    """ffprobe rate fields like "24/1" as an exact Fraction."""
    value = stream.get(field, "0/0")
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError):
        raise FilmError(f"Unparseable {field}: {value!r}")


def _normalized_color(stream):
    """Colour fields normalized so absent/unknown tags read 'unspecified'."""
    def norm(key):
        value = stream.get(key)
        if value in (None, "", "unknown", "unspecified"):
            return "unspecified"
        return str(value)
    return {"space": norm("color_space"), "primaries": norm("color_primaries"),
            "transfer": norm("color_transfer"), "range": norm("color_range")}


def check_video_packets(path, fps, width, height, frames_expected,
                        pixel_format="yuv420p", codec="h264",
                        color_expect=None, audio_expected=False):
    """Structural check of a packet file: stream shape, size, rate, colour.

    Returns the parsed probe info. Frame-count and PTS checks compare exact
    rationals — average FPS or a file name never substitutes.
    """
    info = probe(path)
    videos = [s for s in info["streams"] if s["codec_type"] == "video"]
    sounds = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if len(videos) != 1:
        raise FilmError(f"{Path(path).name}: expected one video track, "
                        f"found {len(videos)}")
    if len(sounds) != (1 if audio_expected else 0):
        raise FilmError(f"{Path(path).name}: expected "
                        f"{int(audio_expected)} audio track(s), "
                        f"found {len(sounds)}")
    v = videos[0]
    if v.get("codec_name") != codec:
        raise FilmError(f"{Path(path).name}: codec {v.get('codec_name')} "
                        f"is not the contracted {codec}")
    if v.get("pix_fmt") != pixel_format:
        raise FilmError(f"{Path(path).name}: pixel format "
                        f"{v.get('pix_fmt')} is not {pixel_format}")
    if [v.get("width"), v.get("height")] != [width, height]:
        raise FilmError(f"{Path(path).name}: size "
                        f"{v.get('width')}x{v.get('height')} "
                        f"is not the contracted {width}x{height}")
    if int(v.get("nb_frames", 0)) != frames_expected:
        raise FilmError(f"{Path(path).name}: container reports "
                        f"{v.get('nb_frames')} frames, expected "
                        f"{frames_expected}")
    expected_rate = Fraction(check_fps(fps), 1)
    if _rational("r_frame_rate", v) != expected_rate \
            or _rational("avg_frame_rate", v) != expected_rate:
        raise FilmError(f"{Path(path).name}: frame rates "
                        f"{v.get('r_frame_rate')}/{v.get('avg_frame_rate')} "
                        f"are not the contracted {expected_rate}")
    if color_expect is not None:
        actual = _normalized_color(v)
        for field in ("space", "primaries", "transfer", "range"):
            if actual[field] != color_expect[field]:
                raise FilmError(
                    f"{Path(path).name}: colour tag {field} is "
                    f"{actual[field]!r}, the recipe fixes "
                    f"{color_expect[field]!r}")
    return info


def check_video_pts(path, fps, frames_expected):
    """Rational PTS check on every video packet of a finished MP4.

    Packet order is decode order; the display PTS set must be exactly
    `0, 1/fps, 2/fps, ...` and every packet duration must equal `1/fps`.
    """
    raw = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-show_entries", "packet=pts,duration",
               "-of", "json", str(path)])
    packets = json.loads(raw).get("packets", [])
    if len(packets) != frames_expected:
        raise FilmError(f"{Path(path).name}: {len(packets)} video packets "
                        f"decoded, expected {frames_expected}")
    info = probe(path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    time_base = Fraction(v["time_base"])
    step = Fraction(1, check_fps(fps))
    pts_sorted = sorted(int(p["pts"]) for p in packets)
    if pts_sorted[0] != 0:
        raise FilmError(f"{Path(path).name}: first PTS is "
                        f"{pts_sorted[0]}, not 0")
    for index, pts in enumerate(pts_sorted):
        if pts * time_base != step * index:
            raise FilmError(
                f"{Path(path).name}: packet {index} displays at "
                f"{pts * time_base}, the frame clock requires "
                f"{step * index}")
    last_end = pts_sorted[-1] * time_base + \
        sorted(int(p["duration"]) for p in packets)[-1] * time_base
    if last_end != last_exposure_end(frames_expected, fps):
        raise FilmError(f"{Path(path).name}: last exposure ends at "
                        f"{last_end}, not {last_exposure_end(frames_expected, fps)}")
    for packet in packets:
        if int(packet["duration"]) * time_base != step:
            raise FilmError(f"{Path(path).name}: a packet duration is not "
                            f"exactly {step}s — not CFR")
    return pts_sorted


def check_audio_track(path, seconds, expect_present=True):
    """Audio presence, codec and duration covering the timeline.

    Returns the audio stream dict (or None when none is required/found).
    """
    info = probe(path)
    sounds = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if not expect_present:
        if sounds:
            raise FilmError(f"{Path(path).name}: unexpected audio track")
        return None
    if len(sounds) != 1:
        raise FilmError(f"{Path(path).name}: expected exactly one audio "
                        f"track from the original master, found {len(sounds)}")
    sound = sounds[0]
    if sound.get("codec_name") != "aac":
        raise FilmError(f"{Path(path).name}: audio codec "
                        f"{sound.get('codec_name')} is not AAC")
    if not int(sound.get("sample_rate", 0)) > 0:
        raise FilmError(f"{Path(path).name}: audio has no valid sample rate")
    if abs(float(sound.get("duration", 0)) - seconds) > .1:
        raise FilmError(f"{Path(path).name}: audio duration "
                        f"{sound.get('duration')} does not cover the "
                        f"{seconds:.3f}s timeline")
    return sound


def _decoded_pcm(path, stream_selector="a:0"):
    """Decode one audio stream to s16le PCM; returns (bytes, sample_rate,
    channels)."""
    raw = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-i", str(path), "-map", f"0:{stream_selector}",
               "-f", "s16le", "-"])
    info = probe(path)
    sound = next(s for s in info["streams"] if s["codec_type"] == "audio")
    return raw, int(sound["sample_rate"]), int(sound.get("channels", 1))


def check_audio_samples(path, seconds, reference=None):
    """Decoded audio sample count must cover the timeline; when a reference
    audio track (the already-verified mux input) is given, the decoded PCM
    must match it sample-for-sample within a small epsilon — a different or
    truncated audio track is rejected, not re-encoded around.
    """
    pcm, rate, channels = _decoded_pcm(path)
    samples = len(pcm) // (2 * channels)
    if abs(samples / rate - seconds) > .1:
        raise FilmError(f"{Path(path).name}: decoded audio has "
                        f"{samples} samples ({samples / rate:.3f}s), "
                        f"the timeline requires {seconds:.3f}s")
    report = {"decoded_samples": samples, "sample_rate": rate,
              "channels": channels}
    if reference is not None:
        ref_pcm, ref_rate, ref_channels = _decoded_pcm(reference)
        if ref_rate != rate or ref_channels != ref_channels:
            raise FilmError("Audio track rate/channels differ from the "
                            "verified reference track")
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.int64)
        b = np.frombuffer(ref_pcm, dtype=np.int16).astype(np.int64)
        # AAC priming may differ by a fraction of a frame; compare the shared
        # prefix after allowing a small length skew.
        shared = min(len(a), len(b))
        if abs(len(a) - len(b)) > rate * channels // 10:
            raise FilmError("Decoded audio length differs from the verified "
                            "reference track")
        diff = np.abs(a[:shared] - b[:shared])
        report["reference_max_abs"] = int(diff.max()) if shared else 0
        if report["reference_max_abs"] > 64:
            raise FilmError("Decoded audio differs from the verified "
                            "reference track — a substituted track is not "
                            "the original master")
        report["matches_reference"] = True
    return report


def check_integrity(path):
    """Decode the whole file; any decoder error fails the delivery."""
    try:
        run(["ffmpeg", "-v", "error", "-xerror", "-nostdin",
             "-i", str(path), "-f", "null", "-"])
    except FilmError as e:
        raise FilmError(f"{Path(path).name}: file failed a full decode: "
                        f"{e}") from e
    return True


def compare_decoded_frames(path, frame_source, tolerance):
    """Compare every decoded frame against the fixed source frames.

    `frame_source` exposes `width`, `height`, `count` and `rgb_bytes(index)`.
    Returns the worst per-frame errors observed. The same `tolerance`
    (`mean_abs_x1000`, `p95_abs`) applies identically to every driver.
    """
    width, height = frame_source.width, frame_source.height
    count = frame_source.count
    raw = run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
               "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    stride = width * height * 3
    if len(raw) != stride * count:
        raise FilmError(f"{Path(path).name}: decoded to "
                        f"{len(raw) // stride} frames, expected {count}")
    worst_mean, worst_p95 = 0.0, 0.0
    for index in range(count):
        decoded = np.frombuffer(raw[index * stride:(index + 1) * stride],
                                dtype=np.uint8).astype(np.int16)
        source = np.frombuffer(frame_source.rgb_bytes(index),
                               dtype=np.uint8).astype(np.int16)
        if source.size != decoded.size:
            raise FilmError(f"Source frame {index} does not have the "
                            "contracted dimensions")
        diff = np.abs(decoded - source)
        worst_mean = max(worst_mean, float(diff.mean()))
        worst_p95 = max(worst_p95, float(np.percentile(diff, 95)))
    report = {"frames_compared": count,
              "worst_mean_abs_x1000": int(worst_mean * 1000),
              "worst_p95_abs": int(worst_p95),
              "tolerance": dict(tolerance)}
    if worst_mean * 1000 > tolerance["mean_abs_x1000"] \
            or worst_p95 > tolerance["p95_abs"]:
        raise FilmError(
            f"{Path(path).name}: decoded pixels differ from the fixed "
            f"frame sequence (mean {worst_mean:.3f}, p95 {worst_p95:.0f}) "
            f"beyond the delivery tolerance — every driver meets the same "
            "bound")
    return report


def verify_delivery(path, frame_source, recipe, seconds, *,
                    profile=DELIVERY_PROFILE_MV_H264_AAC_V1,
                    audio_track=None, audio_required=True,
                    pixel_compare=True):
    """Full MediaVerifier pass on one delivered MP4.

    Raises FilmError on the first failed check; returns the verification
    report otherwise. `frame_source` carries the exact frame index/PTS
    contract and source pixels; `audio_track` is the already-verified track
    that was muxed in (its decoded PCM is compared, not re-encoded).
    Pixel format and colour are checked against the recipe-fixed values;
    codec, container and tolerances come from the DeliveryProfile.
    """
    path = Path(path)
    check_integrity(path)
    info = check_video_packets(
        path, fps := frame_source.fps, frame_source.width,
        frame_source.height, frame_source.count,
        pixel_format=recipe["pixel_format"],
        codec=profile["video"]["codec"],
        color_expect=recipe["color"],
        audio_expected=audio_required)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    if abs(float(v.get("start_time", 0))) > 0.001:
        raise FilmError("Delivery PTS must start at 0")
    if abs(float(info["format"]["duration"]) - seconds) \
            > 1 / fps + .03:
        raise FilmError("Delivery duration exceeds one-frame tolerance")
    check_video_pts(path, fps, frame_source.count)
    audio_report = None
    if audio_required:
        sound = check_audio_track(path, seconds)
        audio_report = check_audio_samples(
            path, seconds, reference=audio_track)
        audio_report["codec"] = sound.get("codec_name")
        audio_report["duration"] = sound.get("duration")
    pixels = compare_decoded_frames(path, frame_source,
                                    profile["pixel_tolerance"]) \
        if pixel_compare else {"frames_compared": 0, "skipped": True}
    return {"valid": True,
            "file_sha256": digest(path),
            "container": info["format"].get("format_name"),
            "duration": float(info["format"]["duration"]),
            "video": {"codec": v.get("codec_name"),
                      "pix_fmt": v.get("pix_fmt"),
                      "width": v.get("width"), "height": v.get("height"),
                      "frames": int(v.get("nb_frames", 0)),
                      "avg_frame_rate": v.get("avg_frame_rate"),
                      "r_frame_rate": v.get("r_frame_rate"),
                      "color": _normalized_color(v)},
            "audio": audio_report,
            "pixels": pixels}
