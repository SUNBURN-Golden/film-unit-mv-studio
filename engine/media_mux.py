"""FRAME_ANIMATION_V1 Muxer (ANIM-015, execution design 6.1).

The muxer is separate from the encoder: it receives a finished video packet
file (an `EncodedVideo`) plus the already-verified AAC audio track prepared
once from the original master, and places both into the MP4 container.

Rules kept from the contract:

- a verified audio track is stream-copied (`-c:a copy`) — it is never
  re-encoded per output or per chunk;
- the video packets are stream-copied (`-c:v copy`) — muxing never
  re-encodes the encoder's output;
- elementary-stream packets are only accepted when the encoder declares
  presentation order (`reordering_free`), so the muxer can stamp the exact
  rational frame clock itself instead of trusting an encoder default;
- arbitrary MP4 byte concatenation is never a finished movie: fragment or
  chunk assembly would go through this muxer with its own checks, not
  `cat`.
"""
from pathlib import Path

from .core import FilmError, digest, ffmpeg, probe
from .frame_clock import check_fps


def prepare_audio_track(master, seconds, work_dir):
    """Encode the original master once into the verified AAC delivery track.

    The returned dict is the verified track: codec, duration and the source
    hash it was made from. Every output that needs this audio reuses the same
    prepared track by stream copy.
    """
    master, work_dir = Path(master), Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    track = work_dir / "audio_track.m4a"
    ffmpeg(["-xerror", "-i", master, "-vn", "-c:a", "aac", "-b:a", "320k",
            "-t", f"{seconds:.3f}", "-map_metadata", "-1", track])
    verify_audio_track(track, seconds)
    return {"path": track, "sha256": digest(track), "codec": "aac",
            "bitrate": "320k", "seconds": seconds,
            "source_sha256": digest(master)}


def verify_audio_track(track, seconds):
    """The prepared AAC track must be AAC and cover the whole timeline."""
    info = probe(track)
    sounds = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if len(sounds) != 1 or sounds[0].get("codec_name") != "aac":
        raise FilmError("Prepared audio track is not a single AAC stream")
    if abs(float(sounds[0].get("duration", 0)) - seconds) > .1:
        raise FilmError("Prepared audio track does not cover the timeline")
    return sounds[0]


def mux_video_audio(encoded, audio_track, target, fps, seconds):
    """Place encoded video packets and the verified audio track into MP4.

    `encoded` is the driver result: `path` plus `timed` (True for a
    video-only MP4 whose packets already carry the rational frame clock;
    False for a reordering-free elementary stream that is stamped here from
    the FrameSource contract).
    """
    target = Path(target)
    encoded_path = Path(encoded["path"])
    if not encoded_path.is_file() or encoded_path.stat().st_size == 0:
        raise FilmError("Encoder produced no packet file")
    fps = check_fps(fps)
    if encoded.get("timed"):
        video_in = ["-i", str(encoded_path)]
    else:
        if not encoded.get("reordering_free"):
            raise FilmError("An untimed elementary stream must be in "
                            "presentation order — encode without reordering "
                            "or hand the muxer a timed packet file")
        # Exact rational stamps from the frame clock, never encoder defaults.
        video_in = ["-fflags", "+genpts", "-r", str(fps),
                    "-i", str(encoded_path)]
    pending = target.with_suffix(".pending.mp4")
    ffmpeg(["-xerror", *video_in, "-i", str(audio_track["path"]),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "copy",
            "-t", f"{seconds:.3f}",
            "-map_metadata", "-1", "-movflags", "+faststart", pending])
    info = probe(pending)
    videos = [s for s in info["streams"] if s["codec_type"] == "video"]
    sounds = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if len(videos) != 1 or len(sounds) != 1:
        raise FilmError("Muxed container does not hold exactly one video "
                        "and one audio track")
    pending.replace(target)
    return target
