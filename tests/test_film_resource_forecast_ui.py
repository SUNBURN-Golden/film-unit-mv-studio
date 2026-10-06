"""film-resource-forecast UI: the read-only forecast panel in the
control panel — AppTest over a real FRAME_ANIMATION_V1 project.

Shows the per-candidate breakdown with UNKNOWN rows, refusal reasons
and proposed alternatives; a registered fixture measurement renders
MEASURED lines with its provenance. Nothing executes or charges.
"""
import hashlib
import shutil
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from engine import perf_scheduler as ps
from engine.capability_registry import (make_measurement, plan_candidates,
                                        register, save_measurement)
from engine.encoder_backends import capability_evidence
from test_anim_017 import perf_project
import engine.resource_forecast as rf

ROOT = Path(__file__).resolve().parents[1]
NAME = "compiler_fixture"          # perf_project's fixture dir name

CAPS = {"max_input_bytes": 1 << 20, "runtimes": ["python-deterministic-v1"]}


def _evidence(scope):
    env = {"worker_id": "perf-local", "session_id": "sess-ui",
           "device": "box-ui", "os": "linux-6",
           "runtime": "python-deterministic-v1",
           "network": "NONE", "gpu": "NONE"}
    probe = {"registry_state": "QUALIFIED_FOR_SCOPE",
             "qualification": "forecast-ui-fixture",
             "reason": "synthetic probe for the UI fixture",
             "environment": env, "operation": "compose_frame_range",
             "scope": dict(scope), "caps": dict(CAPS)}
    return capability_evidence("QUALIFIED_SERVICE", probe)


def _measurement(evidence, plan, candidate):
    return make_measurement(
        evidence, input_digest=plan["snapshot_digest"],
        quality_digest=candidate.get("recipe_digest") or
        plan["snapshot_digest"],
        scope=dict(candidate["scope"]),
        cold={"end_to_end_ms": 900, "startup_ms": 30,
              "auth_ms": 0, "queue_ms": 0},
        warm={"end_to_end_ms": 90},
        stage_timeline=[{"stage": "compose", "ms": 800}],
        shared_edge={"edge_id": "e", "bytes": 2048, "ms": 8},
        peaks={"disk_bytes": 1 << 20, "ram_bytes": 1 << 21,
               "vram_bytes": "UNKNOWN"},
        transfer={"read_bytes": 2048, "write_bytes": 4096,
                  "requests": 2},
        cache={"hits": 1, "misses": 2}, manual={"minutes": 0},
        usage={"subscription_units": 0},
        samples={"count": 1, "window_ms": 900,
                 "min_ms": 900, "max_ms": 900})


@pytest.fixture
def screen(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    root = ROOT / "projects"
    root.mkdir(exist_ok=True)
    # Build the perf fixture in tmp (it drops sequence folders beside the
    # project), then copy the finished project into the panel's list.
    staged = perf_project(tmp_path)
    project = root / NAME
    shutil.copytree(staged, project)
    try:
        yield project, monkeypatch
    finally:
        shutil.rmtree(project, ignore_errors=True)


def _run_screen():
    at = AppTest.from_file(str(ROOT / "app/control_panel.py"),
                           default_timeout=120).run()
    at.sidebar.selectbox[0].select(NAME).run()
    assert not at.exception
    return at


def test_forecast_panel_shows_unknowns_and_refusals(screen):
    """Empty registry: every row UNKNOWN, the refusal and alternative
    are rendered, facets stay honest."""
    project, _ = screen
    at = _run_screen()
    frames = at.dataframe
    assert frames
    cells = " ".join(str(v) for f in frames for v in f.value.values.ravel())
    assert "UNKNOWN" in cells
    warnings = [w.value for w in at.warning]
    assert any("AUTO_NO_CANDIDATES" in w for w in warnings)
    infos = [i.value for i in at.info]
    assert any("RE_MEASURE" in i for i in infos)
    captions = [c.value for c in at.caption]
    assert any("UNQUALIFIED" in c and "NOT_AUTHORIZED" in c
               for c in captions)


def test_forecast_panel_shows_measured_provenance(screen):
    """A bound measurement renders as MEASURED with its environment
    and scope visible in the provenance expander."""
    project, monkeypatch = screen
    plan = ps.make_perf_plan(project)
    candidates = {c["candidate_id"]: c for c in plan_candidates(plan)}
    state_dir = ps.perf_state_dir(project)
    last = None
    for op in plan["operations"]:
        cand = candidates[op["operation_id"]]
        ev = _evidence(cand["scope"])
        register(state_dir, ev)
        save_measurement(state_dir, _measurement(ev, plan, cand))
        last = ev
    monkeypatch.setattr(rf, "_observation_now",
                        lambda: ps._observation_for(last))
    at = _run_screen()
    cells = " ".join(str(v) for f in at.dataframe
                     for v in f.value.values.ravel())
    assert "MEASURED" in cells
    labels = [e.label for e in at.expander]
    assert any("측정 출처" in label for label in labels)
    jsons = " ".join(str(j.value) for j in at.json)
    assert "CAP-MEASURE" in jsons and "box-ui" in jsons


def test_forecast_panel_is_hidden_for_legacy(tmp_path, monkeypatch):
    """LEGACY_MV projects keep the old panel behaviour — no forecast."""
    from engine.audio import analyze, synth_test_audio
    from engine.core import init_project
    from engine.production import make_package
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    root = ROOT / "projects"
    audio = synth_test_audio(tmp_path / "t.wav", seconds=4)
    project = init_project(root, "zz_forecast_legacy", audio,
                           "legacy UI fixture", synthetic=True)
    try:
        analyze(project)
        make_package(project)
        at = AppTest.from_file(str(ROOT / "app/control_panel.py"),
                               default_timeout=120).run()
        at.sidebar.selectbox[0].select("zz_forecast_legacy").run()
        assert not at.exception
        captions = [c.value for c in at.caption]
        assert any("FRAME_ANIMATION_V1" in c and "자원 예측" in c
                   for c in captions)
        headers = [h.value for h in at.subheader]
        assert any("resource forecast" in h for h in headers)
        assert not at.dataframe or not any(
            "UNKNOWN" in str(v)
            for f in at.dataframe for v in f.value.values.ravel())
    finally:
        shutil.rmtree(project, ignore_errors=True)
