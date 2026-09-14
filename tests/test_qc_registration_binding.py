"""QC approval must survive registration only for the inputs actually reviewed.

All videos and semantic scores below are explicit synthetic test fixtures.
"""
from copy import deepcopy

from PIL import Image
import pytest

from engine import pipeline
from engine.core import FilmError, digest, ffmpeg, production_fingerprint, read, write
from engine.qc import SEMANTIC_ITEMS, inspect_clip, save_review
from engine.renderers import RenderBlocked
from engine.resolver import candidates, register_asset

from test_registration_safety import registration_project


def approved_qc(project, source_in_ms=0):
    shot = read(project / "manifest/shots.json")[0]
    clip = project / "render/manual/S001_a0.mp4"
    fmt = read(project / "project.yaml")["format"]
    production_id = production_fingerprint(project)
    record = inspect_clip(clip, shot, project, fmt, True, production_id,
                          source_in_ms=source_in_ms)
    assert record["status"] == "NEEDS_REVIEW"
    save_review(project, record, {item: 100 for item in SEMANTIC_ITEMS},
                "Synthetic fixture reviewer", "TEST ONLY: original visual inputs")
    record = inspect_clip(clip, shot, project, fmt, True, production_id,
                          source_in_ms=source_in_ms)
    assert record["status"] == "PASS"
    return shot, clip, record


def register_reviewed(project, shot, clip, qc):
    return register_asset(
        project, shot, clip, "final", reviewer=qc["reviewer"],
        evidence=qc["evidence_notes"], production_id=qc["production_id"],
        visual_context_id=qc["visual_context_id"],
        qc_review_binding=qc["review_binding"],
        qc_source_in_ms=qc["source_in_ms"],
    )


@pytest.mark.parametrize("changed", ["reference", "shot", "clip"])
def test_registration_rejects_visual_input_changed_after_pass(registration_project, changed):
    project = registration_project
    shot, clip, qc = approved_qc(project)
    if changed == "reference":
        Image.new("RGB", (320, 240), "orange").save(project / shot["references"][0])
    elif changed == "shot":
        shot = deepcopy(shot)
        shot["description"] = "A new direction that the saved QC did not review"
        write(project / "manifest/shots.json", [shot])
    else:
        ffmpeg(["-f", "lavfi", "-i", "color=c=orange:s=320x240:r=24:d=2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "1", clip])
        assert digest(clip) != qc["clip_sha256"]

    with pytest.raises(FilmError, match="changed since clip QC"):
        register_reviewed(project, shot, clip, qc)
    assert not (project / "manifest/assets.json").exists()
    assert not any(row["kind"] == "final" and row["approved"]
                   for row in candidates(project, shot))


def test_registration_validates_raw_window_before_binding_normalized_clip(registration_project):
    project = registration_project
    # QC's source offset describes the raw take. The selected file is already
    # normalized, so its local playback window must still start at zero.
    shot, clip, qc = approved_qc(project, source_in_ms=250)
    record = register_reviewed(project, shot, clip, qc)
    assert record["source_in_ms"] == 0
    assert record["sha256"] == qc["clip_sha256"]
    assert record["review"]["binding"] != qc["review_binding"]
    assert next(row for row in candidates(project, shot) if row["kind"] == "final")["approved"]


@pytest.mark.parametrize("changed", ["reference", "shot"])
def test_pipeline_blocks_changed_review_inputs_without_retry(registration_project, monkeypatch, changed):
    project = registration_project
    first = pipeline.compile_project(project, mode="manual")
    assert first["status"] == "AWAITING_REVIEW_OR_RENDER"
    qc = read(project / "qc/report.json")["shots"][0]
    save_review(project, qc, {item: 100 for item in SEMANTIC_ITEMS},
                "Synthetic fixture reviewer", "TEST ONLY: original visual inputs")
    original_inspect = pipeline.inspect_clip

    def inspect_then_edit(*args, **kwargs):
        result = original_inspect(*args, **kwargs)
        assert result["status"] == "PASS"
        shots = read(project / "manifest/shots.json")
        if changed == "reference":
            Image.new("RGB", (320, 240), "orange").save(project / shots[0]["references"][0])
        else:
            shots[0]["description"] = "Direction changed immediately after QC"
            write(project / "manifest/shots.json", shots)
        return result

    monkeypatch.setattr(pipeline, "inspect_clip", inspect_then_edit)
    with pytest.raises(RenderBlocked, match="Local asset registration failed.*changed since clip QC"):
        pipeline.compile_project(project, mode="manual")
    assert read(project / "qc/report.json")["status"] == "BLOCKED"
    states = list((project / "render/final").glob("*/state.json"))
    assert len(states) == 1
    state = read(states[0])["shots"]["S001"]
    assert state["attempt"] == 0
    assert not state.get("failures")
    shot = read(project / "manifest/shots.json")[0]
    assert not any(row["kind"] == "final" and row["approved"]
                   for row in candidates(project, shot))
    assert not list((project / "render/final").glob("*/S001_a1*"))
