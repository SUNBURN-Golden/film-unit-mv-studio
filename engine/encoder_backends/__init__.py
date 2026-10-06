"""FRAME_ANIMATION_V1 encode/mux/verify driver separation (ANIM-015).

Interfaces (execution design 6.1):

- `FrameSource`: the exact frame contract — a contiguous
  `F_000001.png..F_<count>.png` sequence, 0-based frame index, rational PTS
  `index/fps` and duration `1/fps`. Encoders never fall back to their own
  input defaults for timing.
- `EncoderDriver`: frames -> H.264 codec packets. A driver result is an
  `EncodedVideo` — a timed video-only MP4 or a reordering-free elementary
  stream — never a finished container.
- `media_mux`: video packets + the already-verified audio track -> MP4
  (stream copy; a verified AAC track is never re-encoded).
- `media_verify.MediaVerifier`: encoder-independent checks on the finished
  file against the same DeliveryProfile tolerance for every driver.

Drivers: FFMPEG (the existing CPU libx264 path), GSTREAMER (a real
gst-launch-1.0 subprocess pipeline), NVIDIA_NATIVE and VIDEOTOOLBOX_NATIVE
(real probes that refuse and report hardware-unverified when the SDK/GPU/OS
is absent), QUALIFIED_SERVICE (protocol-only manual export/import; no
network).

`probe` emits `capability_evidence` schema-1 records with registry states
DOCUMENTED_ONLY / QUALIFIED_FOR_SCOPE / STALE / UNAVAILABLE. A name or a
document never qualifies a driver; QUALIFIED_FOR_SCOPE requires a real
fixture encode that passed the MediaVerifier on this host.

`NO_FFMPEG_ENCODING` is true only when a real non-FFMPEG driver produced the
video stream and the verifier passed. Fake drivers in tests are labelled
FAKE/UNQUALIFIED and can never set it.
"""
from pathlib import Path
import hashlib
import tempfile

from PIL import Image

from ..animation_schema import canon_bytes, check_document, write_canon
from ..core import FilmError, digest, now, run
from ..frame_clock import check_fps, frame_filename, frame_index_of
from ..media_mux import mux_video_audio, prepare_audio_track
from ..media_verify import (DELIVERY_PROFILE_MV_H264_AAC_V1, VERIFY_CONTRACT,
                            check_video_packets, sequence_root,
                            verify_delivery)

PROFILE = DELIVERY_PROFILE_MV_H264_AAC_V1
DRIVER_NAMES = ("FFMPEG", "NVIDIA_NATIVE", "VIDEOTOOLBOX_NATIVE",
                "GSTREAMER", "QUALIFIED_SERVICE")
REGISTRY_STATES = ("DOCUMENTED_ONLY", "QUALIFIED_FOR_SCOPE", "STALE",
                   "UNAVAILABLE")


class ManualExportRequired(FilmError):
    """QUALIFIED_SERVICE cannot produce bytes locally; a packet was written."""
    def __init__(self, message, packet=None):
        super().__init__(message)
        self.packet = packet


class FrameSource:
    """The exact frame contract fed to every encoder (schema section 12).

    Files must be the contiguous `F_000001.png..F_<count>.png` delivery
    sequence. Each frame carries 0-based index, rational PTS `index/fps`
    and duration `1/fps`; an encoder that reads raw pixels gets them in
    index order through `rgb_bytes`.
    """

    def __init__(self, frames_dir, fps, width, height, expected_count=None):
        self.dir = Path(frames_dir)
        self.fps = check_fps(fps)
        self.width, self.height = int(width), int(height)
        files = sorted(self.dir.glob("F_*.png"))
        indices = [frame_index_of(f.name) for f in files]
        if indices != list(range(len(indices))):
            raise FilmError(
                f"{self.dir}: delivery sequence must be contiguous "
                "F_000001.png..F_<count>.png")
        if not indices:
            raise FilmError(f"{self.dir}: no delivery frames")
        if expected_count is not None and len(indices) != expected_count:
            raise FilmError(f"{self.dir}: {len(indices)} frames, "
                            f"expected {expected_count}")
        self.count = len(indices)
        self._paths = [self.dir / frame_filename(i) for i in range(self.count)]
        self._rgb = [None] * self.count

    @property
    def seconds(self):
        from fractions import Fraction
        return float(Fraction(self.count, self.fps))

    def path(self, frame_index):
        return self._paths[frame_index]

    def rgb_bytes(self, frame_index):
        """Decoded RGB24 bytes of one frame; the frame must already carry
        the contracted canvas size."""
        if self._rgb[frame_index] is None:
            with Image.open(self._paths[frame_index]) as im:
                rgb = im.convert("RGB")
            if (rgb.width, rgb.height) != (self.width, self.height):
                raise FilmError(
                    f"Frame {frame_index + 1} is {rgb.width}x{rgb.height}; "
                    f"the contract is {self.width}x{self.height} — an "
                    "encoder may not silently rescale")
            self._rgb[frame_index] = rgb.tobytes()
        return self._rgb[frame_index]

    def pixel_digests(self):
        from ..media_verify import frame_pixel_sha256
        return [frame_pixel_sha256(p) for p in self._paths]

    def __iter__(self):
        from fractions import Fraction
        for index in range(self.count):
            yield {"frame_index": index, "path": self._paths[index],
                   "pts": Fraction(index, self.fps),
                   "duration": Fraction(1, self.fps)}


class EncoderDriver:
    """Base contract. Subclasses implement `probe` and `encode`.

    `evidence_class` is "REAL" for production drivers. Test doubles set
    "FAKE"; fake results are always reported UNQUALIFIED and can never set
    NO_FFMPEG_ENCODING.
    """

    name = "?"
    evidence_class = "REAL"

    def probe(self, scope=None):
        """Return a capability_evidence record dict for this host."""
        raise NotImplementedError

    def encode(self, frames: FrameSource, recipe, work_dir):
        """Produce `EncodedVideo` packets in `work_dir`.

        Must raise FilmError with an UNAVAILABLE-style message when the
        driver cannot actually encode on this host — never fake a result.
        """
        raise NotImplementedError


def get_driver(name):
    name = str(name).upper()
    if name == "FFMPEG":
        from .ffmpeg import FFmpegDriver
        return FFmpegDriver()
    if name == "GSTREAMER":
        from .gstreamer import GStreamerDriver
        return GStreamerDriver()
    if name == "NVIDIA_NATIVE":
        from .nvidia_native import NvidiaNativeDriver
        return NvidiaNativeDriver()
    if name == "VIDEOTOOLBOX_NATIVE":
        from .videotoolbox import VideoToolboxDriver
        return VideoToolboxDriver()
    if name == "QUALIFIED_SERVICE":
        from .service import QualifiedServiceDriver
        return QualifiedServiceDriver()
    raise FilmError(f"Unknown encoder driver: {name}")


def _adapter_digest(name):
    return hashlib.sha256(canon_bytes(
        {"driver": name, "package": "film-unit-mv-studio",
         "version": "0.3.0"})).hexdigest()


def capability_evidence(driver_name, probe_result):
    """Build a capability_evidence schema-1 record from a probe result.

    `probe_result` holds: `registry_state`, `qualification` (e.g.
    "real-fixture", "hardware-unverified", "missing-tool", "protocol-only"),
    `reason`, `environment` (what was actually observed), optional `scope`
    and `fixture` digests.
    """
    document = {
        "document_type": "capability_evidence",
        "schema_version": 1,
        "evidence_id": "",  # filled below
        "driver": driver_name,
        "adapter_digest": _adapter_digest(driver_name),
        "account_binding": None,
        "credential_epoch": 0,
        "environment": probe_result.get("environment") or {},
        "operation": probe_result.get("operation",
                                      "encode_video_packets"),
        "scope": probe_result.get("scope"),
        "caps": probe_result.get("caps"),
        "route": "local",
        "transport": "none",
        "fixture": probe_result.get("fixture"),
        "observed_at": probe_result.get("observed_at") or now(),
        "entitlement_basis": probe_result.get(
            "entitlement_basis", "local tool — no account or service"),
        "allowance": {"usd": "NOT_APPLICABLE",
                      "api_credits": "NOT_APPLICABLE",
                      "subscription_units": "NOT_APPLICABLE",
                      "handoff_minutes": "UNKNOWN"},
        "expiry": "Re-probe on any session, device, OS, driver, route or "
                  "limit change; this record never covers an unprobed scope",
        "registry_state": probe_result["registry_state"],
        "qualification": probe_result.get("qualification", "unverified"),
        "reason": probe_result.get("reason", ""),
    }
    document["evidence_id"] = "CE-" + hashlib.sha256(
        canon_bytes(document)).hexdigest()[:16].upper()
    return document


def probe_driver(name, scope=None):
    """Probe one driver and return its capability_evidence record."""
    from ..animation_schema import validate_capability_evidence
    driver = get_driver(name)
    evidence = capability_evidence(driver.name, driver.probe(scope))
    validate_capability_evidence(evidence)
    return evidence


def probe_all(scope=None):
    """Probe every registered driver; returns {name: capability_evidence}."""
    return {name: probe_driver(name, scope) for name in DRIVER_NAMES}


def make_encode_recipe(driver_name, fmt, codec_engine=None, mux_tool=None):
    """An `encode_recipe` schema-1 document (schema section 14).

    Fields are exactly the contracted ones: driver, codec engine, container,
    pixel format, timebase, rate control, color, mux. The rate-control value
    is this driver's own parameter — a CRF number never travels between
    drivers pretending equal quality.
    """
    driver_name = str(driver_name).upper()
    if driver_name not in DRIVER_NAMES:
        raise FilmError(f"Unknown encoder driver: {driver_name}")
    fps = check_fps(fmt["fps"])
    driver = get_driver(driver_name)
    engine, rate_control = driver.codec_contract(fmt)
    recipe = {
        "document_type": "encode_recipe",
        "schema_version": 1,
        "driver": driver_name,
        "codec_engine": engine,
        "container": "mp4",
        "pixel_format": PROFILE["video"]["pixel_format"],
        "timebase": {"num": 1, "den": fps},
        "rate_control": rate_control,
        "color": PROFILE["color"],
        "mux": {"video": "codec packets stream-copied, never re-encoded",
                "audio": "verified master AAC track stream-copied, "
                         "never re-encoded",
                "tool": mux_tool or "ffmpeg -c:v copy -c:a copy",
                "faststart": True},
    }
    check_document(recipe, "encode_recipe")
    return recipe


def encode_digest(sequence_root_value, recipe, verify_contract=VERIFY_CONTRACT,
                  profile=PROFILE):
    """Digest over sequence root + DeliveryProfile + encoder/mux/verify
    contract — never over MP4 bytes."""
    return hashlib.sha256(canon_bytes({
        "sequence_root": sequence_root_value,
        "delivery_profile": profile,
        "encoder_mux_contract": recipe,
        "verify_contract": verify_contract})).hexdigest()


def write_recipe(path, recipe):
    write_canon(path, recipe)
    return path


def encode_delivery(frame_source, recipe, target, *, driver=None,
                    audio_track=None, master=None, work_dir=None,
                    role="subbed", sequence_root_value=None,
                    pixel_compare=True, gate_probe=True, import_path=None):
    """Run one encode -> mux -> verify pass for a delivery file.

    `frame_source` supplies exact frames + PTS. `audio_track` is the result
    of `prepare_audio_track` (shared between clean/subbed and chunk runs so
    a verified track is never re-encoded); `master` is required when no
    prepared track is passed. `import_path` supplies the externally
    produced packet file for the manual QUALIFIED_SERVICE protocol.
    Returns the encode result record.
    """
    work = Path(work_dir) if work_dir else \
        Path(tempfile.mkdtemp(prefix=".encode-", dir=frame_source.dir.parent))
    work.mkdir(parents=True, exist_ok=True)
    driver = driver or get_driver(recipe["driver"])
    if driver.name != recipe["driver"]:
        raise FilmError("Driver/recipe mismatch")
    probe_state = None
    if gate_probe and driver.evidence_class == "REAL":
        probe_state = driver.probe({"width": frame_source.width,
                                    "height": frame_source.height,
                                    "fps": {"num": frame_source.fps,
                                            "den": 1},
                                    "frames": frame_source.count})
        if not getattr(driver, "manual_protocol", False) \
                and probe_state["registry_state"] != "QUALIFIED_FOR_SCOPE":
            # A delivery runs only on a driver whose own real fixture
            # encode passed the verifier on this host; names and docs
            # never qualify.
            raise FilmError(
                f"Driver {driver.name} is {probe_state['registry_state']} "
                f"on this host: {probe_state.get('reason', '')}")
    target = Path(target)
    seconds = frame_source.seconds
    if audio_track is None:
        if master is None:
            raise FilmError("An audio track or a master file is required")
        audio_track = prepare_audio_track(master, seconds, work)
    if import_path is not None:
        # QUALIFIED_SERVICE manual import; import_packets validates the
        # returned packets before the muxer ever sees them.
        encoded = driver.import_packets(import_path, frame_source, work)
    else:
        encoded = driver.encode(frame_source, recipe, work)
    if encoded.get("timed"):
        # Packets that claim the frame clock must already satisfy it; an
        # untimed elementary stream gets its stamps from the muxer and is
        # then held to the same end-to-end verifier.
        check_video_packets(encoded["path"], frame_source.fps,
                            frame_source.width, frame_source.height,
                            frame_source.count,
                            pixel_format=recipe["pixel_format"],
                            codec=PROFILE["video"]["codec"])
    mux_video_audio(encoded, audio_track, target, frame_source.fps, seconds)
    report = verify_delivery(target, frame_source, recipe, seconds,
                             audio_track=audio_track["path"],
                             pixel_compare=pixel_compare)
    root = sequence_root_value or sequence_root(
        role, frame_source.pixel_digests())
    # NO_FFMPEG_ENCODING is demonstrated only when a real, locally probed
    # non-FFmpeg driver produced the video stream on this host. A manually
    # imported service export cannot prove which encoder made it, so it is
    # reported as unverified external evidence, never as a demonstration.
    real_non_ffmpeg = driver.evidence_class == "REAL" \
        and driver.name != "FFMPEG" \
        and not getattr(driver, "manual_protocol", False) \
        and (probe_state or {}).get("registry_state") \
        == "QUALIFIED_FOR_SCOPE"
    if real_non_ffmpeg:
        no_ffmpeg_encoding = True
    elif import_path is not None:
        no_ffmpeg_encoding = "EXTERNAL_UNVERIFIED"
    else:
        no_ffmpeg_encoding = "NOT_DEMONSTRATED"
    return {
        "status": "COMPLETE",
        "driver": driver.name,
        "evidence_class": driver.evidence_class,
        "qualification_state": "UNQUALIFIED"
            if driver.evidence_class == "FAKE"
            else (probe_state or {}).get("registry_state",
                                         "NOT_EVALUATED"),
        "output": str(target),
        "sha256": digest(target),
        "delivery_profile": PROFILE["name"],
        "sequence_root": root,
        "encode_digest": encode_digest(root, recipe),
        "recipe": recipe,
        "audio_track": {"sha256": audio_track["sha256"], "codec": "aac",
                        "source_sha256": audio_track["source_sha256"],
                        "stream_copied": True},
        "no_ffmpeg_encoding": no_ffmpeg_encoding,
        "no_ffmpeg_runtime": "NOT_DEMONSTRATED",
        "verification": report}
