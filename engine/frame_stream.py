"""FRAME_ANIMATION_V1 FrameStream 1 (ANIM-014, schema §12, exec/storage §5.2).

The runtime frame contract. Time truth is the 0-based global frame index
and the rational PTS/duration derived from the plan's fps — a storage
path or encoder name never decides a frame's time.

Every frame carries: 0-based global index, end-exclusive range, rational
PTS and duration, width, height, pixel format, stride, color space,
transfer, range, alpha policy, and `source_digest`/`recipe_digest`
binding it to fixed inputs.

Buffer ownership is explicit: a `FrameBuffer` has one owner and a set of
named consumers; it can only be released after every declared consumer
acknowledged the hand-off, and the producer must not touch it once queued.
`FrameStream` is a bounded queue — when the consumer lags the producer
blocks (`on_limit: PAUSE` semantics); queue capacity is a real reservation,
never silently grown. Each hand-off (`put`, `get`, PNG member ingest)
re-runs the canonical pixel/color check.
"""
from collections import deque
from fractions import Fraction
import threading
import time

from .core import FilmError
from .fav_pack import png_header, sha256_bytes
from .frame_clock import canon_rational

STREAM_CONTRACT = "FRAME_STREAM_V1"

PIXEL_FORMATS = {"RGBA8": 4, "RGB8": 3, "GRAY8": 1}
COLOR_SPACES = {"sRGB"}
TRANSFERS = {"SRGB"}
COLOR_RANGES = {"FULL", "LIMITED"}
ALPHA_POLICIES = {"STRAIGHT", "PREMULTIPLIED", "OPAQUE"}

CONTRACT_FIELDS = {"width", "height", "pixel_format", "stride",
                   "color_space", "transfer", "color_range", "alpha_policy",
                   "fps", "frame_range"}


def _int(value, what, minimum=0):
    if type(value) is not int or value < minimum:
        raise FilmError(f"{what} must be an integer >= {minimum}")
    return value


def _sha(value, what):
    if type(value) is not str or len(value) != 64 \
            or any(c not in "0123456789abcdef" for c in value):
        raise FilmError(f"{what} must be a lowercase SHA-256")
    return value


def _check_range(rng, what):
    if type(rng) is not list or len(rng) != 2 \
            or any(type(v) is not int or v < 0 for v in rng) \
            or rng[1] <= rng[0]:
        raise FilmError(f"{what} must be a non-empty [start, end) range")
    return rng


def check_rational(value, what):
    """The canonical `{"num","den"}` shape; no JSON fractions anywhere.

    The stored value must already be in canonical form — integers only,
    den > 0, gcd 1 — never silently reduced at the hand-off.
    """
    if type(value) is not dict or set(value.keys()) != {"num", "den"}:
        raise FilmError(f"{what} must be a canonical {{num, den}} rational")
    canon = canon_rational(value["num"], value["den"])
    if canon != value:
        raise FilmError(f"{what} must be a reduced rational (gcd 1)")
    if value["num"] < 1:
        raise FilmError(f"{what} must be a positive rate")
    return value


def validate_contract(contract):
    """Canonical pixel/color contract shared by every hand-off point."""
    if type(contract) is not dict or set(contract.keys()) != CONTRACT_FIELDS:
        raise FilmError("FrameStream contract must hold exactly the "
                        "contracted fields")
    width = _int(contract.get("width"), "contract.width", 1)
    height = _int(contract.get("height"), "contract.height", 1)
    fmt = contract.get("pixel_format")
    if fmt not in PIXEL_FORMATS:
        raise FilmError(f"Unknown pixel_format: {fmt}")
    stride = _int(contract.get("stride"), "contract.stride", 1)
    if stride < width * PIXEL_FORMATS[fmt]:
        raise FilmError("contract.stride is smaller than one pixel row")
    if contract.get("color_space") not in COLOR_SPACES:
        raise FilmError("Unknown color_space")
    if contract.get("transfer") not in TRANSFERS:
        raise FilmError("Unknown transfer")
    if contract.get("color_range") not in COLOR_RANGES:
        raise FilmError("Unknown color_range")
    if contract.get("alpha_policy") not in ALPHA_POLICIES:
        raise FilmError("Unknown alpha_policy")
    if fmt == "RGB8" and contract["alpha_policy"] in {"STRAIGHT",
                                                      "PREMULTIPLIED"}:
        raise FilmError("RGB8 frames cannot carry an alpha policy; use "
                        "OPAQUE")
    check_rational(contract.get("fps"), "contract.fps")
    _check_range(contract.get("frame_range"), "contract.frame_range")
    return contract


def make_contract(width, height, *, pixel_format="RGBA8", stride=None,
                  color_space="sRGB", transfer="SRGB", color_range="FULL",
                  alpha_policy="STRAIGHT", fps=None, frame_range):
    """Assemble and validate a FrameStream contract."""
    fps = fps or {"num": 24, "den": 1}
    return validate_contract({
        "width": width, "height": height, "pixel_format": pixel_format,
        "stride": stride if stride is not None
        else width * PIXEL_FORMATS[pixel_format],
        "color_space": color_space, "transfer": transfer,
        "color_range": color_range, "alpha_policy": alpha_policy,
        "fps": check_rational(dict(fps), "fps"),
        "frame_range": list(frame_range)})


def contract_buffer_bytes(contract):
    return contract["stride"] * contract["height"]


def fps_fraction(contract):
    fps = contract["fps"]
    return Fraction(fps["num"], fps["den"])


class FrameBuffer:
    """Owned pixel bytes with explicit consumers and a gated release.

    `consumers` names the parties that must acknowledge the buffer before
    it may be released — the encoder included. `release()` before every
    consumer acknowledged raises rather than freeing; the producer hands
    ownership over on `put` and must not reuse the bytes.
    """

    def __init__(self, data, owner, consumers):
        if not consumers:
            raise FilmError("A frame buffer needs at least one consumer")
        self.data = data
        self.owner = owner
        self.expected = set(consumers)
        self.acknowledged = set()
        self.released = False

    def acknowledge(self, consumer):
        if self.released:
            raise FilmError("Cannot acknowledge a released frame buffer")
        if consumer not in self.expected:
            raise FilmError(f"{consumer} is not a declared buffer consumer")
        self.acknowledged.add(consumer)

    def release(self):
        """Free only after all consumers acknowledged the frame."""
        if self.released:
            raise FilmError("Frame buffer already released")
        missing = self.expected - self.acknowledged
        if missing:
            raise FilmError(
                "Frame buffer released before consumers acknowledged: "
                f"{sorted(missing)} pending")
        self.released = True
        self.data = None


class StreamFrame:
    """One frame on the wire: descriptor + owned buffer."""

    def __init__(self, frame_index, buffer, *, contract, source_digest,
                 recipe_digest):
        fps = fps_fraction(contract)
        self.frame_index = frame_index
        self.pts = Fraction(frame_index) / fps
        self.duration = Fraction(1) / fps
        self.buffer = buffer
        self.source_digest = source_digest
        self.recipe_digest = recipe_digest

    @property
    def pixel_sha256(self):
        if self.buffer.data is None:
            raise FilmError("Frame buffer was released")
        return sha256_bytes(self.buffer.data)


def frame_descriptor(frame):
    """The canon-shaped per-frame record (pixel bytes stay out of it)."""
    return {"frame_index": frame.frame_index,
            "pts": {"num": frame.pts.numerator, "den": frame.pts.denominator},
            "duration": {"num": frame.duration.numerator,
                         "den": frame.duration.denominator},
            "source_digest": frame.source_digest,
            "recipe_digest": frame.recipe_digest,
            "pixel_sha256": frame.pixel_sha256}


def check_frame(frame, contract, name="frame"):
    """Canonical pixel/color check run at every hand-off boundary."""
    if type(frame) is not StreamFrame:
        raise FilmError(f"{name} is not a StreamFrame")
    start, end = contract["frame_range"]
    if not start <= frame.frame_index < end:
        raise FilmError(f"{name} index {frame.frame_index} is outside the "
                        f"contract range [{start}, {end})")
    if frame.buffer.data is None:
        raise FilmError(f"{name} buffer was released before hand-off")
    if len(frame.buffer.data) != contract_buffer_bytes(contract):
        raise FilmError(f"{name} buffer length does not match "
                        "stride*height of the contract")
    _sha(frame.source_digest, f"{name}.source_digest")
    _sha(frame.recipe_digest, f"{name}.recipe_digest")
    expected_pts = Fraction(frame.frame_index) / fps_fraction(contract)
    if frame.pts != expected_pts \
            or frame.duration != Fraction(1) / fps_fraction(contract):
        raise FilmError(f"{name} rational PTS/duration does not match the "
                        "frame clock")
    return True


def check_png_handoff(png_bytes, contract, member_id="frame"):
    """The PNG-pack hand-off: signature, dimensions and pixel format must
    match the stream contract before any decoder sees the bytes."""
    header = png_header(png_bytes, member_id)
    if header["width"] != contract["width"] \
            or header["height"] != contract["height"]:
        raise FilmError(f"PNG member {member_id} dimensions do not match "
                        "the frame contract")
    if header["pixel_format"] != contract["pixel_format"]:
        raise FilmError(f"PNG member {member_id} pixel format "
                        f"{header['pixel_format']} != contract "
                        f"{contract['pixel_format']}")
    return header


class FrameStream:
    """Bounded producer->consumer frame queue with real backpressure.

    `queue_depth` is the only buffering the contract grants. `put` blocks
    while the queue is full — a slow consumer stops the producer instead
    of growing memory. Frames stay owned by their buffer until every
    declared consumer acknowledges; a frame is never dropped while it
    waits in the queue.
    """

    def __init__(self, contract, queue_depth):
        self.contract = validate_contract(contract)
        self.capacity = _int(queue_depth, "queue_depth", 1)
        self._queue = deque()
        self._lock = threading.Condition()
        self._closed = False
        self.produced = 0
        self.consumed = 0

    def make_frame(self, frame_index, data, *, source_digest, recipe_digest,
                   producer, consumers):
        """Build a contract-checked frame owned by `producer`."""
        if type(data) is not bytes:
            data = bytes(data)
        buffer = FrameBuffer(data, owner=producer, consumers=consumers)
        frame = StreamFrame(frame_index, buffer, contract=self.contract,
                            source_digest=source_digest,
                            recipe_digest=recipe_digest)
        check_frame(frame, self.contract)
        return frame

    def put(self, frame, timeout=None):
        """Queue one frame; blocks while at capacity (bounded queue)."""
        check_frame(frame, self.contract)            # producer hand-off
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while len(self._queue) >= self.capacity and not self._closed:
                remaining = None if deadline is None \
                    else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise FilmError("FRAMESTREAM_BACKPRESSURE: bounded queue "
                                    "stayed full; producer paused")
                self._lock.wait(remaining)
            if self._closed:
                raise FilmError("FrameStream is closed")
            frame.buffer.owner = "stream"
            self._queue.append(frame)
            self.produced += 1
            self._lock.notify_all()

    def get(self, timeout=None):
        """Take the next frame in order; None when drained and closed."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while not self._queue and not self._closed:
                remaining = None if deadline is None \
                    else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._lock.wait(remaining)
            if not self._queue:
                return None
            frame = self._queue.popleft()
            frame.buffer.owner = "consumer"
            self.consumed += 1
            self._lock.notify_all()
        check_frame(frame, self.contract)            # consumer hand-off
        return frame

    def close(self):
        """No more puts; queued frames still drain to the consumer."""
        with self._lock:
            self._closed = True
            self._lock.notify_all()

    @property
    def pending(self):
        return len(self._queue)

    def stats(self):
        return {"capacity": self.capacity, "pending": len(self._queue),
                "produced": self.produced, "consumed": self.consumed,
                "closed": self._closed}
