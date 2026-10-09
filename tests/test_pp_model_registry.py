"""Pinned ONNX weight registry. Synthetic bytes only — no network fetch."""
import hashlib
import importlib
import json
import socket
from pathlib import Path

import pytest

from engine import cli
from engine import model_registry as registry
from engine.animation_schema import validate_capability_evidence
from engine.core import FilmError

PIN = "76e4cef9ab42fa7dd4e8f6e4aba47462051e3faa969e4bca6479784fbab0ac6f"
PIN_BYTES = 21458882


def _pin(payload, **overrides):
    digest = hashlib.sha256(payload).hexdigest()
    entry = {
        "id": "rife49",
        "version": "4.9",
        "source_url": "https://example.invalid/rife49.onnx",
        "mirror_url": "https://example.invalid/rife49-mirror.onnx",
        "sha256": digest,
        "bytes": len(payload),
        "license": "MIT",
        "license_url": "https://example.invalid/LICENSE",
        "inputs": [{"name": "img0", "dtype": "float32", "shape": [1]}],
        "outputs": [{"name": "output", "dtype": "float32", "shape": [1]}],
    }
    entry.update(overrides)
    return entry


def test_rife49_pin_is_the_recorded_lfs_hash():
    entry = registry.get_model("rife49")
    assert entry["sha256"] == PIN
    assert entry["bytes"] == PIN_BYTES
    assert entry["version"] == "4.9"
    assert entry["license"] == "MIT"
    assert entry["source_url"] == (
        "https://huggingface.co/edgetools/rife/resolve/main/rife49.onnx")
    assert entry["mirror_url"] == (
        "https://huggingface.co/yuvraj108c/rife-onnx/resolve/main/"
        "rife49_ensemble_True_scale_1_sim.onnx")
    assert entry["license_url"].startswith("https://")
    assert [item["name"] for item in entry["inputs"]] == [
        "img0", "img1", "timestep"]
    assert entry["outputs"][0]["name"] == "output"


def test_repository_ships_no_onnx_and_ci_installs_test_extra_only():
    root = Path(__file__).resolve().parents[1]
    assert list(root.glob("engine/**/*.onnx")) == []
    assert list(root.glob("tests/**/*.onnx")) == []
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "pip install -e '.[test]'" in workflow
    assert "onnxruntime" not in workflow
    assert ".[models]" not in workflow
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'models = ["onnxruntime>=1.17,<2"]' in pyproject
    assert "numpy>=1.24,<3" in pyproject


def test_verify_reports_missing_mismatch_present(tmp_path, monkeypatch):
    payload = b"synthetic-weights-v1"
    monkeypatch.setenv("FILMUNIT_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(registry, "get_model", lambda model_id: _pin(payload))
    missing = registry.verify("rife49")
    assert missing["state"] == "MISSING"
    assert missing["sha256"] == hashlib.sha256(payload).hexdigest()
    assert set(missing) == {"state", "path", "sha256"}

    path = Path(missing["path"])
    path.parent.mkdir(parents=True)
    path.write_bytes(b"wrong-bytes")
    mismatch = registry.verify("rife49")
    assert mismatch["state"] == "MISMATCH"
    assert path.read_bytes() == b"wrong-bytes"

    path.write_bytes(payload)
    present = registry.verify("rife49")
    assert present["state"] == "PRESENT"
    assert path.is_file()


def test_fetch_refuses_mismatch_and_leaves_no_file(tmp_path, monkeypatch):
    payload = b"the-pinned-bytes"
    entry = _pin(payload)
    monkeypatch.setattr(registry, "get_model", lambda model_id: entry)

    def fetcher(url, dest):
        dest.write_bytes(b"not-the-pin")

    with pytest.raises(FilmError, match="mismatch"):
        registry.fetch("rife49", tmp_path, fetcher=fetcher)
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []

    kept = tmp_path / "kept"
    kept.mkdir()
    monkeypatch.setenv("FILMUNIT_MODEL_DIR", str(kept))
    sentinel = kept / "rife49" / f"{entry['sha256']}.onnx"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(payload)
    with pytest.raises(FilmError, match="mismatch"):
        registry.fetch("rife49", kept, fetcher=fetcher)
    assert sentinel.read_bytes() == payload
    assert registry.verify("rife49")["state"] == "PRESENT"


def test_fetch_installs_matching_bytes(tmp_path, monkeypatch):
    payload = b"matching-weights"
    entry = _pin(payload)
    monkeypatch.setattr(registry, "get_model", lambda model_id: entry)
    calls = []

    def fetcher(url, dest):
        calls.append(url)
        dest.write_bytes(payload)

    result = registry.fetch("rife49", tmp_path, fetcher=fetcher)
    assert result["state"] == "PRESENT"
    assert Path(result["path"]).read_bytes() == payload
    assert calls == [entry["source_url"]]
    monkeypatch.setenv("FILMUNIT_MODEL_DIR", str(tmp_path))
    assert registry.verify("rife49")["state"] == "PRESENT"


def test_fetch_rejects_non_https_and_path_escape(tmp_path, monkeypatch):
    payload = b"unused"
    calls = []

    def fetcher(url, dest):
        calls.append(url)
        dest.write_bytes(payload)

    http_entry = _pin(payload, source_url="http://example.invalid/rife49.onnx")
    monkeypatch.setattr(registry, "get_model", lambda model_id: http_entry)
    with pytest.raises(FilmError, match="https"):
        registry.fetch("rife49", tmp_path, fetcher=fetcher)

    file_entry = _pin(payload, mirror_url="file:///tmp/rife49.onnx")
    monkeypatch.setattr(registry, "get_model", lambda model_id: file_entry)
    with pytest.raises(FilmError, match="https"):
        registry.fetch("rife49", tmp_path, fetcher=fetcher)

    monkeypatch.setattr(registry, "get_model", lambda model_id: _pin(payload))
    escaped = tmp_path / "cache" / ".." / ".." / "outside"
    with pytest.raises(FilmError, match="destination"):
        registry.fetch("rife49", escaped, fetcher=fetcher)
    with pytest.raises(FilmError, match="model id"):
        registry.fetch("../rife49", tmp_path, fetcher=fetcher)
    with pytest.raises(FilmError, match="https"):
        registry._join_https("https://example.invalid/a", "http://evil/b")
    assert calls == []
    assert not (tmp_path / "outside").exists()


def test_registry_evidence_is_unavailable_without_model(tmp_path, monkeypatch):
    monkeypatch.setenv("FILMUNIT_MODEL_DIR", str(tmp_path))
    record = registry.evidence("rife49", "interpolate_frame_range")
    validate_capability_evidence(record)
    assert record["document_type"] == "capability_evidence"
    assert record["schema_version"] == 1
    assert record["registry_state"] == "UNAVAILABLE"
    assert record["qualification"] in {"missing-runtime", "missing-model"}
    assert record["registry_state"] != "QUALIFIED_FOR_SCOPE"
    assert record["operation"] == "interpolate_frame_range"

    monkeypatch.setattr(registry, "_onnxruntime_version", lambda: "1.17.0-test")
    missing_model = registry.evidence("rife49", "interpolate_frame_range")
    validate_capability_evidence(missing_model)
    assert missing_model["registry_state"] == "UNAVAILABLE"
    assert missing_model["qualification"] == "missing-model"
    assert missing_model["environment"]["model_state"] == "MISSING"
    assert missing_model["registry_state"] != "QUALIFIED_FOR_SCOPE"

    monkeypatch.setattr(registry, "_onnxruntime_version", lambda: None)
    missing_runtime = registry.evidence("rife49", "interpolate_frame_range")
    assert missing_runtime["qualification"] == "missing-runtime"
    assert missing_runtime["registry_state"] == "UNAVAILABLE"


def test_evidence_never_qualified_for_scope(tmp_path, monkeypatch):
    payload = b"present-but-unprobed"
    monkeypatch.setenv("FILMUNIT_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(registry, "get_model", lambda model_id: _pin(payload))
    monkeypatch.setattr(registry, "_onnxruntime_version",
                        lambda: "1.17.0-test")
    path = registry.model_path("rife49")
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    record = registry.evidence("rife49", "interpolate_frame_range")
    validate_capability_evidence(record)
    assert record["registry_state"] == "DOCUMENTED_ONLY"
    assert record["qualification"] == "present-unprobed"
    assert record["registry_state"] != "QUALIFIED_FOR_SCOPE"
    assert record["fixture"] is None


def test_engine_import_performs_no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("socket call during import")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    importlib.reload(registry)
    importlib.import_module("engine")


def test_cli_model_verify_and_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FILMUNIT_MODEL_DIR", str(tmp_path))
    assert cli.main(["model", "verify", "rife49"]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["state"] == "MISSING"

    assert cli.main(["model", "status", "rife49"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["qualification_state"] == "UNQUALIFIED"
    assert report["acceptance_state"] == "PENDING"
    assert report["release_state"] == "NOT_AUTHORIZED"
    assert report["models"][0]["verify"]["state"] == "MISSING"
    assert report["models"][0]["evidence"]["registry_state"] != \
        "QUALIFIED_FOR_SCOPE"
    validate_capability_evidence(report["models"][0]["evidence"])

    assert cli.main(["model", "verify", "not-a-model"]) == 1
    escaped = tmp_path / ".." / "outside"
    assert cli.main(["model", "fetch", "rife49", "--dest", str(escaped)]) == 1
