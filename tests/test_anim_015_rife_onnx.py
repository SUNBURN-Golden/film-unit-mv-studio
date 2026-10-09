"""anim-015-rife-onnx: CPU ONNX in-between adapter on the path-B contract.

Every session here is an injected test double. It is DOCUMENTED_ONLY and
must not qualify. Missing runtime or weights refuse; they never blend.
RIFE does not encode, so no_ffmpeg_encoding stays false.
"""
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from engine import cli
from engine.animation_assets import load_registry
from engine.animation_schema import (read_canon, validate_capability_evidence)
from engine.capability_registry import axis_of
from engine.core import FilmError, digest, read
from engine.encoder_backends import (FrameSource, encode_delivery,
                                     make_encode_recipe)
from engine.frame_clock import frame_filename
from engine.interpolation import (crop_to, pad_to_32, planar_to_uint8,
                                  round_half_even_uint8, timesteps_for_owned,
                                  to_planar)
from engine.interpolation.rife_onnx import (RifeSegmentAdapter,
                                            build_capability_evidence,
                                            cpu_threads, session_is_qualifying)
from engine.model_registry import status as model_status
from engine.motion_plan import save_shot_plan
from engine.segment_gen import (commit_segment_sequence, segment_import,
                                segment_quote, segment_reconcile,
                                segment_submit)
from test_anim_008 import c_project
from test_anim_010 import W, H, FRAMES, b_plan
from test_anim_015 import make_master

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence" / "anim-015-rife" / "demo.json"


class BlendSession:
    """Deterministic blend. Not onnxruntime, so it cannot qualify."""

    film_documented_only = True

    def get_providers(self):
        return ["CPUExecutionProvider"]

    def run(self, _names, feed):
        timestep = float(np.asarray(feed["timestep"]).reshape(-1)[0])
        mixed = (1.0 - timestep) * feed["img0"] + timestep * feed["img1"]
        return [mixed.astype(np.float32)]


def _factory(model_path, providers, threads):
    assert list(providers) == ["CPUExecutionProvider"]
    assert threads == cpu_threads()
    assert model_path is None
    return BlendSession()


def _inject(monkeypatch, **tool):
    monkeypatch.setattr(RifeSegmentAdapter, "session_factory", _factory)
    monkeypatch.setattr(RifeSegmentAdapter, "tool_override", tool or None)


def _png(path, seed):
    arr = np.zeros((H, W, 3), np.uint8)
    arr[..., 0] = (seed * 40) % 256
    arr[..., 1] = np.linspace(0, 200, W, dtype=np.uint8)[None, :]
    arr[..., 2] = np.linspace(seed, 180, H, dtype=np.uint8)[:, None]
    Image.fromarray(arr).save(path)
    return path


def _scene(tmp_path, *, end_anchor=True, segments=None, capabilities=None):
    p = c_project(tmp_path)
    caps = ["START_END_IMAGES"] if capabilities is None else capabilities
    save_shot_plan(p, b_plan([], capabilities=caps, segments=segments))
    from engine.segment_gen import import_segment_control
    import_segment_control(p, "S001", _png(tmp_path / "start.png", 1),
                           role="keypose", frame=0)
    if end_anchor:
        import_segment_control(p, "S001", _png(tmp_path / "end.png", 2),
                               role="keypose", frame=FRAMES)
    return p


def _quote_submit_import(p, start=0, end=FRAMES):
    quote = segment_quote(p, "S001", start, end, adapter_id="rife_onnx")
    submitted = segment_submit(
        p, "S001", start, end, adapter_id="rife_onnx",
        approver="synthetic tester", quote_id=quote["quote_id"], cap=0)
    reconciled = segment_reconcile(p, submitted["job_id"],
                                   adapter_id="rife_onnx")
    imported = segment_import(p, submitted["job_id"], adapter_id="rife_onnx")
    return quote, submitted, reconciled, imported


def test_rife_adapter_returns_both_endpoints_and_import_drops_anchor(
        tmp_path, monkeypatch):
    _inject(monkeypatch)
    p = _scene(tmp_path)
    quote, submitted, reconciled, imported = _quote_submit_import(p)
    assert quote["amount"] == 0 and quote["unit"] == "credits"
    assert quote["provider_class"] == "LOCAL_TOOL"
    assert quote["qualification_state"] == "UNQUALIFIED"
    assert quote["detail"]["returned_frames"] == FRAMES + 1
    assert reconciled["status"] == "OUTPUT_PENDING_VERIFY"
    assert imported["state"] == "DRAFT"
    assert imported["qualification_state"] == "UNQUALIFIED"
    norm = imported["normalization"]
    assert norm["endpoint_rule"] == "START_END_INCLUDED"
    assert norm["returned_frames"] == FRAMES + 1
    assert norm["usable_frames"] == FRAMES
    assert norm["dropped_end_frame"] == FRAMES
    assert norm["mapping"] == list(range(FRAMES))
    job = read(p / "animation/segment_jobs/S001" / f"{submitted['job_id']}.json")
    manifest = job["result"]
    assert manifest["frame_count"] == FRAMES + 1
    assert manifest["tool"]["providers"] == ["CPUExecutionProvider"]
    assert manifest["tool"]["threads"] == cpu_threads()
    assert manifest["tool"]["timesteps"] == timesteps_for_owned(FRAMES)
    assert manifest["tool"]["session"] == "INJECTED_FAKE"
    out = p / manifest["frames_dir"]
    start_sha = job["spec"]["inputs"]["start_image"]["sha256"]
    end_sha = job["spec"]["inputs"]["end_image"]["sha256"]
    assert digest(out / "f000000.png") == start_sha
    assert digest(out / f"f{FRAMES:06d}.png") == end_sha
    assert imported["member_range"] == [0, FRAMES]
    staged = job["import"]["members"]
    assert len(staged) == FRAMES
    assert staged[0]["sha256"] == start_sha
    assert all(member["source_index"] != FRAMES for member in staged)


def test_rife_adapter_refuses_range_crossing_cut(tmp_path, monkeypatch):
    _inject(monkeypatch)
    segments = [
        {"start": 0, "end": 12, "path": "B",
         "capabilities": ["START_END_IMAGES"]},
        {"start": 12, "end": FRAMES, "path": "B",
         "capabilities": ["START_END_IMAGES"]},
    ]
    p = _scene(tmp_path, segments=segments)
    adapter = RifeSegmentAdapter(p)
    spec = {"shot_id": "S001", "segment": {"start": 0, "end": FRAMES},
            "inputs": {"start_image": {"member": "x"},
                       "end_image": {"member": "y"}}}
    with pytest.raises(FilmError, match="crosses a cut"):
        adapter.quote(spec)
    with pytest.raises(FilmError, match="crosses a cut"):
        adapter.submit(spec, "req-cross", "att-cross")
    inside = adapter.quote({"shot_id": "S001", "segment": {"start": 0, "end": 12},
                            "inputs": spec["inputs"]})
    assert inside["detail"]["owned_frames"] == 12
    with pytest.raises(FilmError, match="SEGMENT_NOT_IN_PLAN"):
        segment_quote(p, "S001", 0, FRAMES, adapter_id="rife_onnx")
    assert not (p / "animation" / "rife_onnx" / "output").exists()


def test_rife_adapter_refuses_start_only_plan(tmp_path, monkeypatch):
    _inject(monkeypatch)
    p = _scene(tmp_path, end_anchor=False, capabilities=[])
    with pytest.raises(FilmError, match="CAPABILITY_UNAVAILABLE:.*end anchor"):
        segment_quote(p, "S001", 0, FRAMES, adapter_id="rife_onnx")
    assert not (p / "animation" / "rife_onnx" / "output").exists()


def test_rife_manifest_tool_binding_changes_job_identity(tmp_path, monkeypatch):
    _inject(monkeypatch, model_sha256="a" * 64, onnxruntime_version="1.17.0")
    p = _scene(tmp_path)
    first = segment_quote(p, "S001", 0, FRAMES, adapter_id="rife_onnx")
    monkeypatch.setattr(RifeSegmentAdapter, "tool_override",
                        {"model_sha256": "b" * 64,
                         "onnxruntime_version": "1.18.0"})
    second = segment_quote(p, "S001", 0, FRAMES, adapter_id="rife_onnx")
    assert first["job_id"] != second["job_id"]
    job_a = read(p / "animation/segment_jobs/S001" / f"{first['job_id']}.json")
    job_b = read(p / "animation/segment_jobs/S001" / f"{second['job_id']}.json")
    assert job_a["spec"]["output"]["model_sha256"] == "a" * 64
    assert job_a["spec"]["output"]["onnxruntime_version"] == "1.17.0"
    assert job_b["spec"]["output"]["model_sha256"] == "b" * 64
    assert job_b["spec"]["output"]["onnxruntime_version"] == "1.18.0"
    assert job_a["job_key"] != job_b["job_key"]
    again = segment_quote(p, "S001", 0, FRAMES, adapter_id="rife_onnx")
    assert again["job_id"] == second["job_id"]


def test_rife_unavailable_without_runtime_or_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(RifeSegmentAdapter, "session_factory", None)
    monkeypatch.setattr(RifeSegmentAdapter, "tool_override", None)
    p = _scene(tmp_path)
    caps = RifeSegmentAdapter(p).capabilities()
    assert caps["available"] is False
    assert caps["reason"]
    assert caps["qualification_state"] == "UNQUALIFIED"
    with pytest.raises(FilmError, match="no blend fallback"):
        segment_quote(p, "S001", 0, FRAMES, adapter_id="rife_onnx")
    with pytest.raises(FilmError, match="CAPABILITY_UNAVAILABLE"):
        RifeSegmentAdapter(p).submit(
            {"shot_id": "S001", "segment": {"start": 0, "end": FRAMES},
             "inputs": {"end_image": {"member": "y"},
                        "start_image": {"member": "x"}}},
            "req-missing", "att-missing")
    assert not (p / "animation" / "rife_onnx" / "output").exists()
    assert cli.main(["segment-quote", str(p), "S001", "--start", "0",
                     "--end", str(FRAMES), "--adapter", "rife_onnx"]) == 1
    err = capsys.readouterr().err
    assert "CAPABILITY_UNAVAILABLE" in err
    assert "Unknown segment adapter" not in err
    monkeypatch.setenv("FILMUNIT_MODEL_DIR", str(tmp_path / "models"))
    report = model_status("rife49")
    evidence = report["rife_onnx"]
    validate_capability_evidence(evidence)
    assert evidence["registry_state"] in {"UNAVAILABLE", "DOCUMENTED_ONLY"}
    assert evidence["registry_state"] != "QUALIFIED_FOR_SCOPE"
    assert evidence["operation"] == "interpolate_frame_range"
    assert evidence["caps"]["no_ffmpeg_encoding"] is False
    assert axis_of(evidence) == "COMPOSE"


def test_rife_fake_session_is_documented_only():
    session = BlendSession()
    assert session_is_qualifying(session) is False
    record = build_capability_evidence(
        injected_session=session, output_sha256="ab" * 32)
    validate_capability_evidence(record)
    assert record["registry_state"] == "DOCUMENTED_ONLY"
    assert record["qualification"] == "injected-fake"
    assert record["registry_state"] != "QUALIFIED_FOR_SCOPE"
    assert record["fixture"] is None
    assert record["caps"]["no_ffmpeg_encoding"] is False
    assert record["operation"] == "interpolate_frame_range"
    assert axis_of(record) == "COMPOSE"
    assert axis_of(record) != "ENCODE"


def test_interpolate_operation_maps_to_compose_axis():
    assert axis_of({"operation": "interpolate_frame_range"}) == "COMPOSE"
    assert axis_of({"operation": "encode_video_packets"}) == "ENCODE"
    assert axis_of({"operation": "encode_delivery"}) == "ENCODE"
    assert axis_of({"operation": "interpolate_frame_range"}) != "ENCODE"


def test_rife_pad_crop_roundtrip_and_timesteps():
    source = np.arange(15 * 17 * 3, dtype=np.uint8).reshape(15, 17, 3)
    planar = to_planar(source)
    assert planar.shape == (1, 3, 15, 17)
    assert planar.dtype == np.float32
    padded, size = pad_to_32(planar)
    assert size == (15, 17)
    assert padded.shape[-2] % 32 == 0 and padded.shape[-1] % 32 == 0
    assert np.array_equal(crop_to(padded, size), planar)
    aligned = np.zeros((1, 3, 32, 64), np.float32)
    same, same_size = pad_to_32(aligned)
    assert same.shape == aligned.shape and same_size == (32, 64)
    restored = planar_to_uint8(to_planar(source))
    # uint8 / 255 in float32 is not a byte roundtrip; half-even is tested
    # on exact halfway values below. The pad itself is lossless.
    assert restored.shape == source.shape
    halfway = np.array([0.5, 1.5, 2.5, 3.5], dtype=np.float64) / 255.0
    assert list(round_half_even_uint8(halfway)) == [0, 2, 2, 4]
    assert timesteps_for_owned(4) == ["0/4", "1/4", "2/4", "3/4", "4/4"]
    assert len(timesteps_for_owned(FRAMES)) == FRAMES + 1
    with pytest.raises(FilmError):
        timesteps_for_owned(0)


def test_rife_members_commit_to_sequence_and_ffmpeg_encode_verifies(
        tmp_path, monkeypatch):
    _inject(monkeypatch)
    p = _scene(tmp_path)
    _quote, submitted, _reconciled, imported = _quote_submit_import(p)
    assert imported["state"] == "DRAFT"
    committed = commit_segment_sequence(p, "S001")
    assert committed["state"] == "DRAFT"
    assert committed["frames"] == FRAMES
    assert committed["qualification_state"] == "UNQUALIFIED"
    registry = load_registry(p)
    record = registry["assets"][committed["asset_id"]]["revisions"][
        str(committed["revision"])]
    assert record["kind"] == "FRAME_SEQUENCE"
    assert record["acceptance"]["state"] == "DRAFT"
    assert record["provenance"]["provider_class"] == "LOCAL_TOOL"
    assert "rife_onnx" in record["provenance"]["adapters"]
    folder = tmp_path / "delivery"
    folder.mkdir()
    files = sorted(record["files"], key=lambda item: item["frame_index"])
    assert [item["frame_index"] for item in files] == list(range(FRAMES))
    for item in files:
        shutil.copy(p / item["relative_name"],
                    folder / frame_filename(item["frame_index"]))
    frames = FrameSource(folder, 24, W, H)
    recipe = make_encode_recipe("FFMPEG", {"fps": 24, "width": W, "height": H,
                                           "crf": 18})
    master = make_master(tmp_path / "master.wav", FRAMES / 24)
    encoded = encode_delivery(frames, recipe, tmp_path / "out.mp4",
                              master=master)
    assert encoded["driver"] == "FFMPEG"
    assert encoded["status"] == "COMPLETE"
    assert encoded["no_ffmpeg_encoding"] is not True
    assert encoded["qualification_state"] == "UNQUALIFIED"
    assert encoded["verification"]["valid"] is True
    # The shared end anchor was not imported, so the sequence length is the
    # owned range and the job that produced it is still the draft import.
    assert submitted["qualification_state"] == "UNQUALIFIED"


def test_committed_demo_evidence_does_not_claim_encode_or_qualification():
    document = read_canon(EVIDENCE)
    assert document["no_ffmpeg_encoding"] is False
    assert document["qualification_state"] in {"UNQUALIFIED", "PARTIAL"}
    assert document["qualification_state"] != "QUALIFIED"
    assert document["scope"]["width"] == 1920
    assert document["scope"]["height"] == 1080
    assert document["scope"]["owned_frames"] == 24
    assert document["scope"]["delivery_frames_claimed"] is None
    assert document["throughput_claim"] == "UNQUALIFIED"
    if document["qualification_state"] == "PARTIAL":
        assert document["scope"]["ran"] is True
        assert document["output_sha256"]
    else:
        assert document["scope"]["ran"] is False
        assert document["output_sha256"] is None
    folder = EVIDENCE.parent
    assert not list(folder.glob("*.png"))
    assert not list(folder.glob("*.mp4"))
    assert not list(folder.glob("*.onnx"))
    text = (folder / "COMMANDS.md").read_text(encoding="utf-8")
    assert "rife_onnx" in text
    assert document["node"] == "anim-015-rife-onnx"
