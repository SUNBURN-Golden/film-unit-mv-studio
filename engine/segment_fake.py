"""Deterministic local fake of a path-B segment-generation provider (ANIM-010).

This module simulates the provider side of the segment adapter contract from
docs/FRAME_ANIMATION_V1_DESIGN_KO.md 8.3/8.5 so the engine's capability
preflight, quote/ledger, UNKNOWN fencing and source-PTS mapping can be
exercised end to end without a network, credentials or paid generation.

The fake keeps its "server" state in the project under
`animation/fake_provider/` — a plainly labelled FAKE store. It renders real,
changing PNG frames at its declared native fps/size by interpolating the
conditioning images (a crude deterministic blend plus a moving marker), and
its `behaviors` switches let a test drive every protocol branch:

- `lost_ack`: the submit reply is lost — `lost_ack_delivered` decides whether
  the provider actually recorded the request (UNKNOWN reconciliation paths);
- `complete_after`: status queries needed before the job is done (late
  completion);
- `short_clip`: fewer usable frames than the target range needs;
- `extra_frames`: extra tail frames, forcing an explicit used-range choice;
- `reject`: a definitive refusal before any work was accepted;
- `end_image: false` under `endpoints`: a start-only adapter declaration.

Nothing in this file is a real provider qualification: outputs are FAKE and
UNQUALIFIED regardless of how convincingly the protocol ran.
"""
import hashlib
from fractions import Fraction
from pathlib import Path

from PIL import Image

from .core import FilmError, now, safe_path, read, write

ADAPTER_ID = "fake_segment"
ADAPTER_VERSION = "fake_segment_v1"
PROVIDER_CLASS = "FAKE"
QUALIFICATION = "UNQUALIFIED"


class SegmentRejected(FilmError):
    """The provider definitively refused the request before accepting it."""


class SegmentLostAck(FilmError):
    """The submit acknowledgement never arrived: acceptance is UNKNOWN."""


def _state_path(p):
    return safe_path(p, "animation/fake_provider/state.json")


def _default_state(p):
    config = read(Path(p) / "project.yaml")
    fmt = config["format"]
    return {"adapter": ADAPTER_VERSION, "provider_class": PROVIDER_CLASS,
            "qualification_state": QUALIFICATION,
            "price_credits_per_frame": 2,
            "native": {"width": fmt["width"], "height": fmt["height"],
                       "fps": 30},
            "limits": {"max_reference_images": 6, "min_frames": 2,
                       "max_frames": 96},
            "guides": {"pose": "IMAGE", "layout": "IMAGE",
                       "mask_region": True, "alpha_output": "DROPPED"},
            "endpoints": {"start_image": True, "end_image": True},
            "behaviors": {"reject": False, "lost_ack": False,
                          "lost_ack_delivered": True, "complete_after": 1,
                          "short_clip": False, "extra_frames": 0},
            "requests": {}}


def fake_state(p):
    """The fake provider's server-side state (created with defaults)."""
    path = _state_path(p)
    if not path.exists():
        write(path, _default_state(p))
    return read(path)


def configure_fake(p, **changes):
    """Update fake provider settings/behaviors; dotted keys update sections."""
    state = fake_state(p)
    for key, value in changes.items():
        if type(value) is dict and type(state.get(key)) is dict:
            state[key].update(value)
        else:
            state[key] = value
    write(_state_path(p), state)
    return state


def _needed_source(spec, fps):
    """Source frames covering [0, L/out) — the last output frame's start."""
    seg = spec["segment"]
    length = seg["end"] - seg["start"]
    out = spec["output"]["fps"]
    return int(Fraction(length - 1) * Fraction(fps, out)) + 1


class FakeSegmentAdapter:
    """The fake provider endpoint the engine's B-path adapter drives.

    `capabilities()` is the adapter's honest declaration; `quote`, `submit`
    and `status` model the paid-request lifecycle against local files only.
    """

    id = ADAPTER_ID
    provider_class = PROVIDER_CLASS

    def __init__(self, project):
        self.p = Path(project)
        fake_state(self.p)  # create the labelled FAKE store on first use

    # -- capability declaration -------------------------------------------------

    def capabilities(self):
        state = fake_state(self.p)
        native, limits = state["native"], state["limits"]
        guides, endpoints = state["guides"], state["endpoints"]
        return {"adapter_id": ADAPTER_VERSION,
                "provider_class": PROVIDER_CLASS,
                "qualification_state": QUALIFICATION,
                "reference_images_max": limits["max_reference_images"],
                "pose_guide": guides["pose"], "layout_guide": guides["layout"],
                "mask_region": guides["mask_region"],
                "start_image": endpoints["start_image"],
                "end_image": endpoints["end_image"],
                "returned_endpoint_rule": ("START_END_INCLUDED"
                                           if endpoints["end_image"]
                                           else "START_ONLY"),
                "alpha_output": guides["alpha_output"],
                "native_width": native["width"],
                "native_height": native["height"],
                "native_fps": native["fps"],
                "min_returned_frames": limits["min_frames"],
                "max_returned_frames": limits["max_frames"],
                "operation_identity": "REQUEST_ID",
                "cost_unit": "credits"}

    # -- fake provider internals --------------------------------------------------

    def _save(self, state):
        write(_state_path(self.p), state)

    def _returned_count(self, spec, caps):
        """How many frames this request's clip would carry, before behaviors."""
        length = spec["segment"]["end"] - spec["segment"]["start"]
        fps, out = caps["native_fps"], spec["output"]["fps"]
        needed = _needed_source(spec, fps)
        behaviors = fake_state(self.p)["behaviors"]
        if spec["inputs"]["end_image"] is not None and caps["end_image"]:
            # Both endpoints returned: source frame at T = L/out is included.
            base = int(Fraction(length) * Fraction(fps, out)) + 1
        else:
            base = needed
        if behaviors["short_clip"]:
            base = max(1, needed - 1)
        return base + int(behaviors["extra_frames"])

    # -- provider-side generation --------------------------------------------------

    def _load_condition(self, rel):
        return Image.open(safe_path(self.p, rel)).convert("RGB")

    def _render(self, record):
        """Produce real changing PNG frames for the recorded request."""
        spec = record["spec"]
        native = fake_state(self.p)["native"]
        width, height, fps = native["width"], native["height"], native["fps"]
        count = self._returned_count(spec, self.capabilities())
        start = self._load_condition(
            spec["inputs"]["start_image"]["member"]).resize((width, height))
        end_ref = spec["inputs"]["end_image"]
        end = self._load_condition(end_ref["member"]).resize((width, height)) \
            if end_ref is not None else None
        seed = int.from_bytes(hashlib.sha256(
            (record["request_id"] + spec["spec_sha256"]).encode()).digest()[:4])
        out_dir = safe_path(
            self.p, f"animation/fake_provider/output/{record['request_id']}")
        out_dir.mkdir(parents=True, exist_ok=True)
        marker_rgb = (64 + seed % 128, 64 + (seed >> 8) % 128,
                      64 + (seed >> 16) % 128)
        names = []
        for index in range(count):
            if end is not None and count > 1:
                base = Image.blend(start, end, index / (count - 1))
            else:
                base = start.copy()
            # A marker block whose position and shade move every frame makes
            # each returned member's pixels genuinely differ.
            x = (seed + index * 5) % max(1, width - 8)
            y = (seed >> 4) % max(1, height - 8)
            px = base.load()
            for dy in range(8):
                for dx in range(8):
                    px[x + dx, y + dy] = ((marker_rgb[0] + index * 7) % 256,
                                          (marker_rgb[1] + index * 5) % 256,
                                          (marker_rgb[2] + index * 3) % 256)
            name = f"f{index:06d}.png"
            base.save(out_dir / name)
            names.append(name)
        rule = ("START_END_INCLUDED" if end is not None else "START_ONLY")
        return {"frames_dir": str(out_dir.relative_to(self.p)),
                "frame_names": names, "frame_count": count,
                "fps": fps, "width": width, "height": height,
                "endpoint_rule": rule,
                "first_pts": {"num": 0, "den": 1}}

    # -- provider lifecycle -------------------------------------------------------

    def quote(self, spec):
        """A per-returned-frame fake-credit quote for exactly this spec."""
        state = fake_state(self.p)
        count = self._returned_count(spec, self.capabilities())
        price = state["price_credits_per_frame"]
        return {"unit": "credits", "amount": count * price,
                "detail": {"returned_frames": count,
                           "credits_per_frame": price},
                "provider_class": PROVIDER_CLASS,
                "qualification_state": QUALIFICATION}

    def submit(self, spec, request_id, attempt_id):
        """Record a request server-side, or simulate a lost/rejected ack."""
        state = fake_state(self.p)
        behaviors = state["behaviors"]
        if behaviors["reject"]:
            raise SegmentRejected("FAKE provider refused this submission "
                                  "before any generation was created")
        existing = state["requests"].get(request_id)
        if existing is not None:
            return {"operation_id": existing["operation_id"]}
        record = {"request_id": request_id, "attempt_id": attempt_id,
                  "operation_id": "fakeop-" + hashlib.sha256(
                      request_id.encode()).hexdigest()[:16],
                  "spec": spec, "spec_sha256": spec["spec_sha256"],
                  "submitted_at": now(), "queries": 0, "state": "RUNNING",
                  "billed": self.quote(spec)["amount"] > 0,
                  "result": None}
        state["requests"][request_id] = record
        self._save(state)
        if behaviors["lost_ack"]:
            if not behaviors["lost_ack_delivered"]:
                del state["requests"][request_id]
                self._save(state)
            raise SegmentLostAck("FAKE provider acknowledgement was lost; "
                                 "acceptance is unknown until reconcile")
        return {"operation_id": record["operation_id"]}

    def status(self, request_id):
        """Query the same submission identity; late completion is simulated."""
        state = fake_state(self.p)
        record = state["requests"].get(request_id)
        if record is None:
            return {"state": "NOT_FOUND", "billed": None}
        record["queries"] += 1
        if record["state"] == "RUNNING" and record["queries"] >= \
                state["behaviors"]["complete_after"]:
            record["result"] = self._render(record)
            record["state"] = "DONE"
        self._save(state)
        return {"state": record["state"],
                "operation_id": record["operation_id"],
                "result": record["result"], "billed": record["billed"]}


class DeclaredAdapter:
    """A capability declaration with no wired transport.

    The engine can preflight against what a provider is documented to support,
    but quote/submit/status refuse — no real provider call exists in this node.
    """

    def __init__(self, declaration):
        self.declaration = declaration
        self.id = declaration["adapter_id"]
        self.provider_class = declaration["provider_class"]

    def capabilities(self):
        return dict(self.declaration)

    def _refuse(self):
        raise FilmError(
            f"UNQUALIFIED: segment adapter {self.id} is declared for "
            "capability checks only; no real provider call is wired in this "
            "node (no paid generation, no network)")

    def quote(self, spec):
        self._refuse()

    def submit(self, spec, request_id, attempt_id):
        self._refuse()

    def status(self, request_id):
        self._refuse()


# The existing engine/gemini.py Veo adapter sends one start image plus a
# prompt and returns an mp4 — its declaration is start-only on purpose.
GEMINI_VIDEO_CAPABILITIES = {
    "adapter_id": "gemini_video",
    "provider_class": "DOCUMENTED_ONLY",
    "qualification_state": "UNQUALIFIED",
    "reference_images_max": 1,
    "pose_guide": "NONE", "layout_guide": "NONE", "mask_region": False,
    "start_image": True, "end_image": False,
    "returned_endpoint_rule": "START_ONLY",
    "alpha_output": "DROPPED",
    "native_width": 1280, "native_height": 720, "native_fps": 24,
    "min_returned_frames": 96, "max_returned_frames": 192,
    "operation_identity": "OPERATION_NAME",
    "cost_unit": "USD",
}

SEGMENT_ADAPTERS = {"fake_segment", "gemini_video"}


def make_adapter(project, adapter_id):
    """Instantiate a segment adapter by id; unknown ids are refused."""
    if adapter_id == "fake_segment":
        return FakeSegmentAdapter(project)
    if adapter_id == "gemini_video":
        return DeclaredAdapter(GEMINI_VIDEO_CAPABILITIES)
    raise FilmError(f"Unknown segment adapter {adapter_id!r}; v1 declares "
                    f"{sorted(SEGMENT_ADAPTERS)}")
