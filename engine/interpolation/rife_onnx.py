"""RIFE v4.9 CPU ONNX in-between segment adapter.

RIFE is an interpolator on the path-B segment contract. It is not an
encoder: ``no_ffmpeg_encoding`` stays false, and a missing runtime or a
missing pin is ``CAPABILITY_UNAVAILABLE``. There is no blend fallback and
no silent swap to the fake provider.

An injected session factory is a test double. It is labelled
``DOCUMENTED_ONLY`` and cannot become ``QUALIFIED_FOR_SCOPE``. That registry
state is written only after a real ``onnxruntime.InferenceSession`` ran a
two-frame fixture on this host and the output hash was recorded, and the
scope of that record is the fixture alone.
"""
import hashlib
import io
import os
import platform
import re
import shutil
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image

from ..animation_schema import load_animation_timeline, require_animation_profile
from ..core import FilmError, safe_path, write
from ..model_registry import get_model, model_path, verify
from . import (build_manifest, crop_to, pad_to_32, planar_to_uint8,
               timesteps_for_owned, to_planar)

ADAPTER_ID = "rife_onnx"
MODEL_ID = "rife49"
PROVIDER_CLASS = "LOCAL_TOOL"
QUALIFICATION = "UNQUALIFIED"
PROVIDERS = ["CPUExecutionProvider"]
_SHA = re.compile(r"^[0-9a-f]{64}$")
# capability_evidence schema-1 only admits encode-driver names. This adapter
# is not an encoder; QUALIFIED_SERVICE is the schema's non-hardware slot.
_EVIDENCE_DRIVER = "QUALIFIED_SERVICE"
_FIXTURE_SIZE = 32


def cpu_threads():
    """``min(4, cpu_count)``, at least 1. Recorded on the manifest."""
    count = os.cpu_count() or 1
    return max(1, min(4, int(count)))


def runtime_version():
    """Installed onnxruntime version, or None. Import is deferred."""
    try:
        import onnxruntime as ort
    except ImportError:
        return None
    return str(getattr(ort, "__version__", "unknown"))


def session_is_qualifying(session):
    """True only for a real CPU onnxruntime session, never a test double."""
    if session is None or getattr(session, "film_documented_only", False):
        return False
    try:
        import onnxruntime as ort
    except ImportError:
        return False
    return isinstance(session, ort.InferenceSession)


def default_session_factory(model_path_value, providers, threads):
    """Open the pinned file on CPU only. No other execution provider."""
    if list(providers) != PROVIDERS:
        raise FilmError("CAPABILITY_UNAVAILABLE: RIFE sessions use "
                        "CPUExecutionProvider only")
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = int(threads)
    options.inter_op_num_threads = 1
    return ort.InferenceSession(str(model_path_value), sess_options=options,
                                providers=list(PROVIDERS))


def _tensor_names():
    entry = get_model(MODEL_ID)
    inputs = [item["name"] for item in entry["inputs"]]
    outputs = [item["name"] for item in entry["outputs"]]
    return inputs, outputs[0]


def _run_timestep(session, img0, img1, timestep, output_name, input_names):
    """One padded forward pass. ``timestep`` is a float in ``[0, 1]``."""
    feed = {input_names[0]: img0, input_names[1]: img1,
            input_names[2]: np.asarray([timestep], dtype=np.float32)}
    out = session.run([output_name], feed)[0]
    return out


class RifeOnnxInterpolator:
    """CPU-only RIFE session. The session factory is injectable.

    Endpoints are the anchor images themselves. Interior frames are the
    session output at ``k/owned``, cropped back to the canvas and rounded
    half-even to 8-bit. A factory that is not the default never qualifies.
    """

    def __init__(self, session_factory=None, *, threads=None):
        self.session_factory = session_factory
        self.threads = cpu_threads() if threads is None else int(threads)
        self.providers = list(PROVIDERS)
        self.session = None
        self.injected = session_factory is not None

    def load(self):
        if self.session is not None:
            return self.session
        if self.session_factory is not None:
            self.session = self.session_factory(
                None, list(self.providers), self.threads)
        else:
            if runtime_version() is None:
                raise FilmError(
                    "CAPABILITY_UNAVAILABLE: onnxruntime is not installed "
                    "(optional extra 'models'; CI installs the test extra "
                    "only); no fake substitution and no blend fallback")
            checked = verify(MODEL_ID)
            if checked["state"] != "PRESENT":
                raise FilmError(
                    "CAPABILITY_UNAVAILABLE: pinned weights are "
                    f"{checked['state']}; nothing was deleted; no fake "
                    "substitution and no blend fallback")
            self.session = default_session_factory(
                model_path(MODEL_ID), list(self.providers), self.threads)
            self.injected = False
        providers = list(self.session.get_providers())
        if providers != PROVIDERS:
            raise FilmError(
                "CAPABILITY_UNAVAILABLE: RIFE refuses providers "
                f"{providers}; CPUExecutionProvider only")
        return self.session

    def interpolate(self, start_hwc, end_hwc, owned):
        """``owned + 1`` frames: start, interiors, end anchor."""
        if type(owned) is not int or owned < 1:
            raise FilmError("owned frame count must be an integer >= 1")
        start = np.asarray(start_hwc)
        end = np.asarray(end_hwc)
        if start.shape != end.shape or start.dtype != np.uint8 \
                or end.dtype != np.uint8:
            raise FilmError("RIFE anchors must be matching uint8 RGB frames")
        session = self.load()
        input_names, output_name = _tensor_names()
        img0, size = pad_to_32(to_planar(start))
        img1, _same = pad_to_32(to_planar(end))
        frames = [start]
        for step in timesteps_for_owned(owned)[1:-1]:
            numer, denom = step.split("/")
            timestep = float(Fraction(int(numer), int(denom)))
            raw = _run_timestep(session, img0, img1, timestep,
                                output_name, input_names)
            frames.append(planar_to_uint8(crop_to(raw, size)))
        frames.append(end)
        if len(frames) != owned + 1:
            raise FilmError("RIFE returned a frame count other than owned+1")
        return frames


def _two_frame_output_sha256(session):
    """One midpoint between two 32x32 anchors. Returns the PNG sha256."""
    height = width = _FIXTURE_SIZE
    start = np.zeros((height, width, 3), np.uint8)
    start[..., 0] = np.arange(width, dtype=np.uint8)[None, :]
    end = np.zeros((height, width, 3), np.uint8)
    end[..., 2] = np.arange(height, dtype=np.uint8)[:, None]
    holder = RifeOnnxInterpolator(session_factory=lambda *_a, **_k: session)
    # The lambda is only how the already-opened session is handed in.
    # Qualification still depends on session_is_qualifying(session).
    frames = holder.interpolate(start, end, 2)
    mid = frames[1]
    buffer = io.BytesIO()
    Image.fromarray(mid, mode="RGB").save(buffer, format="PNG")
    return hashlib.sha256(buffer.getvalue()).hexdigest()


def build_capability_evidence(*, injected_session=None, output_sha256=None,
                              write=False):
    """``capability_evidence`` for ``interpolate_frame_range``.

    An injected session stays ``DOCUMENTED_ONLY`` even when an output hash
    is supplied. ``QUALIFIED_FOR_SCOPE`` requires a real session, a present
    pin, and a hash from the two-frame fixture on this host. The scope is
    that fixture (32x32, 2 anchors), never 1080p and never 5760 frames.
    """
    from ..encoder_backends import capability_evidence
    from ..animation_schema import validate_capability_evidence

    entry = get_model(MODEL_ID)
    checked = verify(MODEL_ID)
    version = runtime_version()
    qualified = False
    fixture_hash = None
    reason_error = None
    if injected_session is not None:
        registry_state = "DOCUMENTED_ONLY"
        qualification = "injected-fake"
        reason = ("injected session is a test double; a fixture hash does "
                  "not qualify RIFE")
        if output_sha256 is not None and not _SHA.fullmatch(
                output_sha256 if type(output_sha256) is str else ""):
            raise FilmError("injected fixture hash must be sha256 hex or omitted")
    elif version is None:
        registry_state = "UNAVAILABLE"
        qualification = "missing-runtime"
        reason = ("onnxruntime is not installed (optional extra 'models'; "
                  "CI installs the test extra only)")
    elif checked["state"] != "PRESENT":
        registry_state = "UNAVAILABLE"
        qualification = "missing-model"
        reason = (f"pinned weights are {checked['state']}; "
                  "nothing was deleted")
    else:
        session = None
        try:
            session = default_session_factory(
                model_path(MODEL_ID), list(PROVIDERS), cpu_threads())
            if not session_is_qualifying(session):
                raise FilmError("session is not a real onnxruntime session")
            fixture_hash = _two_frame_output_sha256(session)
        except Exception as exc:
            reason_error = f"{type(exc).__name__}: {exc}"[:500]
            fixture_hash = None
            session = None
        if fixture_hash and _SHA.fullmatch(fixture_hash) \
                and session_is_qualifying(session):
            qualified = True
            registry_state = "QUALIFIED_FOR_SCOPE"
            qualification = "two-frame-fixture"
            reason = ("a real CPU session interpolated one frame between "
                      "two 32x32 anchors on this host; the output hash is "
                      "recorded; this scope does not cover 1080p or 5760 "
                      "frames and it is not encode evidence")
        else:
            registry_state = "UNAVAILABLE"
            qualification = "fixture-failed"
            reason = reason_error or "two-frame fixture produced no hash"
    if registry_state == "QUALIFIED_FOR_SCOPE" and not qualified:
        raise FilmError("RIFE evidence qualifies only after a real fixture")
    scope = {"model_id": MODEL_ID, "sha256": entry["sha256"],
             "bytes": entry["bytes"], "verify": checked["state"],
             "inference": "two-frame-fixture" if qualified else "not-run",
             "width": _FIXTURE_SIZE if qualified else None,
             "height": _FIXTURE_SIZE if qualified else None,
             "frames": 2 if qualified else None,
             "delivery_frames_claimed": None,
             "no_ffmpeg_encoding": False}
    probe = {
        "registry_state": registry_state,
        "qualification": qualification,
        "reason": reason,
        "operation": "interpolate_frame_range",
        "environment": {
            "session": ("real-two-frame" if qualified else
                        "injected-fake" if injected_session is not None
                        else None),
            "device": "cpu",
            "os": platform.platform(),
            "driver": "rife_onnx",
            "encoder": "none",
            "onnxruntime": version or "absent",
            "model_id": MODEL_ID,
            "model_state": checked["state"],
            "network": "none",
            "providers": "CPUExecutionProvider",
            "threads": cpu_threads(),
        },
        "scope": scope,
        "caps": {"onnxruntime": version or "absent",
                 "inference": qualified,
                 "providers": "CPUExecutionProvider",
                 "threads": cpu_threads(),
                 "no_ffmpeg_encoding": False,
                 "axis": "COMPOSE"},
        "fixture": fixture_hash,
        "entitlement_basis": ("local pinned weights — no account, "
                              "no service, no credential"),
    }
    record = validate_capability_evidence(
        capability_evidence(_EVIDENCE_DRIVER, probe))
    if record["registry_state"] == "QUALIFIED_FOR_SCOPE" and (
            record["fixture"] is None or record["scope"]["frames"] != 2):
        raise FilmError("qualified RIFE evidence must name the two-frame hash")
    if write:
        from ..animation_schema import write_canon
        from ..model_registry import model_root
        path = model_root() / "rife_onnx" / f"{record['evidence_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_canon(path, record)
    return record


class RifeSegmentAdapter:
    """Path-B adapter. Submit runs locally and synchronously.

    ``session_factory`` and ``tool_override`` are class attributes so a test
    can inject them before ``make_adapter`` constructs the instance. Leaving
    them ``None`` is the production path: a missing runtime or pin refuses,
    and nothing is blended in their place.
    """

    id = ADAPTER_ID
    provider_class = PROVIDER_CLASS
    session_factory = None
    tool_override = None

    def __init__(self, project, session_factory=None, tool_override=None):
        self.p = Path(project)
        self.session_factory = (session_factory if session_factory is not None
                                else type(self).session_factory)
        self.tool_override = (tool_override if tool_override is not None
                              else type(self).tool_override)
        self.threads = cpu_threads()
        self.providers = list(PROVIDERS)

    def _format(self):
        config = require_animation_profile(self.p)
        return config["format"]

    def tool_binding(self):
        entry = get_model(MODEL_ID)
        if self.session_factory is not None:
            version = "FAKE-SESSION"
        else:
            version = runtime_version() or "absent"
        binding = {"model_id": MODEL_ID,
                   "model_sha256": entry["sha256"],
                   "onnxruntime_version": version}
        override = self.tool_override or {}
        for key in ("model_id", "model_sha256", "onnxruntime_version"):
            value = override.get(key)
            if type(value) is str and value:
                binding[key] = value
        return binding

    def recipe_binding(self):
        """Model pin and ORT version: the job-key recipe digest includes both."""
        return self.tool_binding()

    def _availability(self):
        if self.session_factory is not None:
            return True, ""
        if runtime_version() is None:
            return False, ("onnxruntime is not installed (optional extra "
                           "'models'; CI installs the test extra only)")
        state = verify(MODEL_ID)["state"]
        if state != "PRESENT":
            return False, f"pinned weights are {state}; nothing was deleted"
        return True, ""

    def capabilities(self):
        fmt = self._format()
        binding = self.tool_binding()
        available, reason = self._availability()
        return {"adapter_id": ADAPTER_ID,
                "provider_class": PROVIDER_CLASS,
                "qualification_state": QUALIFICATION,
                "available": available,
                "reason": reason,
                "reference_images_max": 0,
                "pose_guide": "NONE", "layout_guide": "NONE",
                "mask_region": False,
                "start_image": True, "end_image": True,
                "returned_endpoint_rule": "START_END_INCLUDED",
                "alpha_output": "DROPPED",
                "native_width": fmt["width"], "native_height": fmt["height"],
                "native_fps": fmt["fps"],
                "min_returned_frames": 3, "max_returned_frames": 97,
                "operation_identity": "REQUEST_ID",
                "cost_unit": "credits",
                "model_id": binding["model_id"],
                "model_sha256": binding["model_sha256"],
                "onnxruntime_version": binding["onnxruntime_version"]}

    def _state_path(self):
        return safe_path(self.p, "animation/rife_onnx/state.json")

    def _load_state(self):
        path = self._state_path()
        if not path.exists():
            return {"adapter": ADAPTER_ID, "provider_class": PROVIDER_CLASS,
                    "qualification_state": QUALIFICATION, "requests": {}}
        from ..core import read
        return read(path)

    def _save_state(self, state):
        write(self._state_path(), state)

    def _owned(self, spec):
        segment = spec.get("segment") or {}
        start, end = segment.get("start"), segment.get("end")
        if type(start) is not int or type(end) is not int or end <= start:
            raise FilmError("CAPABILITY_UNAVAILABLE: segment range must be "
                            "[start, end) with end > start")
        return start, end, end - start

    def _require_available(self):
        available, reason = self._availability()
        if not available:
            raise FilmError(
                "CAPABILITY_UNAVAILABLE: rife_onnx is unavailable "
                f"({reason}); no fake substitution and no blend fallback")

    def _require_end_anchor(self, spec):
        inputs = spec.get("inputs") if type(spec.get("inputs")) is dict else {}
        if inputs.get("end_image") is None:
            raise FilmError(
                "CAPABILITY_UNAVAILABLE: rife_onnx requires an end anchor; "
                "a start-only plan is refused and the end condition is "
                "never dropped")

    def _require_inside_one_cut(self, spec):
        """Refuse a range that contains a segment boundary or leaves the cut.

        Segment starts inside ``(start, end)`` are cut boundaries: motion
        interpolation does not cross them (design §7). The shot's used range
        is the cut; a range outside ``[0, length)`` leaves it.
        """
        start, end, _owned = self._owned(spec)
        shot_id = spec.get("shot_id")
        config = require_animation_profile(self.p)
        timeline = load_animation_timeline(self.p)
        entry = next((item for item in timeline["entries"]
                      if item["shot_id"] == shot_id), None)
        if entry is None:
            raise FilmError(f"CAPABILITY_UNAVAILABLE: unknown shot {shot_id}")
        used_start, used_end = entry["used_source_range"]
        length = used_end - used_start
        fmt = config["format"]
        from ..motion_plan import load_shot_plan
        plan = load_shot_plan(
            self.p, shot_id, length=length,
            canvas={"width": fmt["width"], "height": fmt["height"]})
        if start < 0 or end > length:
            raise FilmError(
                f"CAPABILITY_UNAVAILABLE: range [{start}, {end}) leaves "
                f"the cut [0, {length})")
        for segment in plan["segments"]:
            boundary = segment["start"]
            if start < boundary < end:
                raise FilmError(
                    f"CAPABILITY_UNAVAILABLE: range [{start}, {end}) "
                    f"crosses a cut at frame {boundary}")

    def _guard(self, spec):
        self._require_available()
        self._require_end_anchor(spec)
        self._require_inside_one_cut(spec)
        return self._owned(spec)

    def quote(self, spec):
        _start, _end, owned = self._guard(spec)
        return {"unit": "credits", "amount": 0,
                "detail": {"returned_frames": owned + 1,
                           "owned_frames": owned,
                           "credits_per_frame": 0},
                "provider_class": PROVIDER_CLASS,
                "qualification_state": QUALIFICATION}

    def _render(self, spec, request_id, owned):
        fmt = self._format()
        width, height = fmt["width"], fmt["height"]
        fps = fmt["fps"]
        inputs = spec["inputs"]
        start_path = safe_path(self.p, inputs["start_image"]["member"])
        end_path = safe_path(self.p, inputs["end_image"]["member"])
        for path, what in ((start_path, "start anchor"),
                           (end_path, "end anchor")):
            if not path.is_file() or path.is_symlink():
                raise FilmError(f"CAPABILITY_UNAVAILABLE: {what} is missing")
        with Image.open(start_path) as image:
            start = np.asarray(image.convert("RGB"))
        with Image.open(end_path) as image:
            end = np.asarray(image.convert("RGB"))
        if start.shape != (height, width, 3) or end.shape != (height, width, 3):
            raise FilmError("CAPABILITY_UNAVAILABLE: anchors must match "
                            "the project canvas; RIFE does not rescale")
        out_rel = f"animation/rife_onnx/output/{request_id}"
        out_dir = safe_path(self.p, out_rel)
        out_dir.mkdir(parents=True, exist_ok=True)
        interpolator = RifeOnnxInterpolator(
            session_factory=self.session_factory, threads=self.threads)
        frames = interpolator.interpolate(start, end, owned)
        names = []
        for index, frame in enumerate(frames):
            name = f"f{index:06d}.png"
            target = out_dir / name
            if index == 0:
                shutil.copyfile(start_path, target)
            elif index == owned:
                shutil.copyfile(end_path, target)
            else:
                Image.fromarray(frame, mode="RGB").save(target, format="PNG")
            names.append(name)
        binding = self.tool_binding()
        tool = {**binding, "providers": list(self.providers),
                "threads": self.threads,
                "timesteps": timesteps_for_owned(owned),
                "session": ("INJECTED_FAKE" if self.session_factory is not None
                            else "onnxruntime")}
        return build_manifest(out_rel, names, fps, width, height, tool)

    def submit(self, spec, request_id, attempt_id):
        if type(request_id) is not str or not request_id \
                or "/" in request_id or request_id in {".", ".."}:
            raise FilmError("request_id must be a single path segment")
        state = self._load_state()
        existing = state["requests"].get(request_id)
        if existing is not None:
            return {"operation_id": existing["operation_id"]}
        _start, _end, owned = self._guard(spec)
        operation_id = "rife-" + hashlib.sha256(
            request_id.encode()).hexdigest()[:16]
        result = self._render(spec, request_id, owned)
        state["requests"][request_id] = {
            "request_id": request_id, "attempt_id": attempt_id,
            "operation_id": operation_id, "state": "DONE",
            "billed": False, "result": result}
        self._save_state(state)
        return {"operation_id": operation_id}

    def status(self, request_id):
        record = self._load_state()["requests"].get(request_id)
        if record is None:
            return {"state": "NOT_FOUND", "billed": None}
        return {"state": record["state"], "operation_id": record["operation_id"],
                "result": record["result"], "billed": record["billed"]}

    def cancel(self, request_id):
        record = self._load_state()["requests"].get(request_id)
        if record is None:
            return {"outcome": "NOT_FOUND", "billed": None}
        if record["state"] == "DONE":
            return {"outcome": "COMPLETED_FIRST", "result": record["result"],
                    "billed": record["billed"]}
        return {"outcome": "CANCELLED", "billed": record["billed"]}
