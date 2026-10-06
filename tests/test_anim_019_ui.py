"""ANIM-019 browser-UI fixture: the read-only capability registry panel
in the control panel — AppTest over real stored evidence.

Evidence lives under FILM_UNIT_HOME/capability; fake entries are shown
DOCUMENTED_ONLY and the facets keep reporting UNQUALIFIED / PENDING /
NOT_AUTHORIZED. No network, credentials or real probes.
"""
import hashlib
import shutil
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from engine.animation_schema import canon_bytes, validate_capability_evidence
from engine.audio import analyze, synth_test_audio
from engine.capability_registry import register
from engine.core import init_project
from engine.production import make_package

ROOT = Path(__file__).resolve().parents[1]
NAME = "zz_anim019_ui"


def _evidence(worker="w-ui", state="QUALIFIED_FOR_SCOPE",
              qualification="real-fixture", fake=False):
    env = {"service": "svc-ui", "worker_id": worker,
           "session_id": "sess-ui", "session_epoch": 1,
           "device": "box-ui", "os": "linux-6",
           "runtime": "python-deterministic-v1", "driver": "drv-ui",
           "network": "NONE", "gpu": "NONE"}
    if fake:
        env["fake_runtime"] = True
    doc = {
        "document_type": "capability_evidence", "schema_version": 1,
        "evidence_id": "", "driver": "QUALIFIED_SERVICE",
        "adapter_digest": hashlib.sha256(b"anim-019-ui").hexdigest(),
        "account_binding": None, "credential_epoch": 0,
        "environment": env, "operation": "compose_frame_range",
        "scope": {"operation": "compose_frame_range",
                  "runtime_contract": "python-deterministic-v1",
                  "route": "relay", "width": 8, "height": 6,
                  "pixel_format": "RGBA8", "frames": 8},
        "caps": {}, "route": "relay", "transport": "local-spawn",
        "fixture": None,
        "observed_at": "2026-10-01T00:00:00+00:00",
        "entitlement_basis": "ui fixture — secret-free",
        "allowance": {"subscription_units": 0, "compute_units": 0,
                      "api_credits": 0, "usd": 0, "handoff_minutes": 0},
        "expiry": "STALE on any bound-field change or expiry",
        "registry_state": state, "qualification": qualification,
        "reason": "panel fixture record"}
    doc["evidence_id"] = "CE-" + hashlib.sha256(
        canon_bytes(doc)).hexdigest()[:16].upper()
    return validate_capability_evidence(doc)


@pytest.fixture
def screen(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    register(tmp_path / "home", _evidence())
    register(tmp_path / "home", _evidence(
        worker="w-fake", fake=True,
        qualification="FAKE — test double"))
    root = ROOT / "projects"
    root.mkdir(exist_ok=True)
    audio = synth_test_audio(tmp_path / "t.wav", seconds=6)
    project = init_project(root, NAME, audio, "UI fixture",
                           synthetic=True, aspect="16:9")
    analyze(project)
    make_package(project)
    try:
        at = AppTest.from_file(str(ROOT / "app/control_panel.py"),
                               default_timeout=90).run()
        at.sidebar.selectbox[0].select(NAME).run()
        assert not at.exception
        yield at
    finally:
        shutil.rmtree(project, ignore_errors=True)


def test_capability_panel_lists_states(screen):
    at = screen
    bodies = [m.value for m in at.markdown]
    assert any("QUALIFIED_FOR_SCOPE" in b for b in bodies)
    assert any("DOCUMENTED_ONLY" in b and "FAKE" in b for b in bodies)
    captions = [c.value for c in at.caption]
    assert any("UNQUALIFIED" in c and "NOT_AUTHORIZED" in c
               for c in captions)
    # every listed entry carries an expander keeping digests+bindings
    assert any("해시" in e.label for e in at.expander)


def test_capability_panel_shows_reasons(screen):
    at = screen
    captions = [c.value for c in at.caption]
    assert any("사유: fake_evidence" in c for c in captions)
