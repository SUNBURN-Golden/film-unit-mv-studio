"""ANIM-012: the 240-second integrated regression for both production modes.

FRAME_ANIMATION_V1: 8 synthetic shots of 30 s each, every entry uses 762
source frames and every boundary carries a 48-frame CROSSFADE, so
sum(used) - sum(overlap) = 8*762 - 7*48 = 6096 - 336 = 5760 output frames.
The DRAFT_PREVIEW output is decoded for real: nb_read_frames, per-frame PTS
(monotonic, CFR 24/1), duration and the audio track are asserted from
ffprobe, not from container headers alone.

LEGACY_MV: the same 240 s fixture through the existing Build 1 pipeline and
Build 1 replay — existing Preview/Final semantics and reproducibility are the
regression.

Both fixtures use small solid-colour material (64x36 / 320x240) so the
honest decodes stay cheap in CI; both are marked ``slow`` for optional local
filtering but run in the default suite.
"""

from __future__ import annotations

import json
from fractions import Fraction
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from engine.animation_assets import import_frame_sequence
from engine.animation_migrate import animation_init
from engine.animation_preview import compile_draft_preview
from engine.animation_schema import read_canon, write_canon
from engine.builds import replay_build, verify_build
from engine.compiler import compile_preview
from engine.core import probe, read, run, write
from engine.transitions import audit_timeline
from test_compiler_v03 import assert_master, fixture_project, newest_build

SECONDS = 240
FPS = 24
OUTPUT = SECONDS * FPS          # 5760
SHOTS = 8
OVERLAP = 48                    # CROSSFADE frames at every boundary
USED = 762                      # 8*762 - 7*48 = 5760
TOTAL_USED = USED * SHOTS
TOTAL_OVERLAP = OVERLAP * (SHOTS - 1)


def _variant(seed: int) -> bytes:
    image = Image.new("RGB", (64, 36), (seed * 17 % 256, seed * 31 % 256, seed * 53 % 256))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _decoded(path: Path, size: tuple[int, int]):
    """Real decode facts: decoded frame count, ordered PTS, stream shape."""
    info = probe(path)
    video = [s for s in info["streams"] if s["codec_type"] == "video"]
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert len(video) == len(audio) == 1, "output must keep one video + one audio stream"
    video = video[0]
    assert (video["width"], video["height"]) == size
    assert video["r_frame_rate"] == f"{FPS}/1"
    assert video["avg_frame_rate"] == f"{FPS}/1"
    assert abs(float(video["duration"]) - SECONDS) < 0.05, video["duration"]
    assert abs(float(audio[0]["duration"]) - SECONDS) < 0.1, "audio preserved"
    assert abs(float(info["format"]["duration"]) - SECONDS) < 0.05
    # Honest decode pass: -count_frames + -show_frames reads every packet.
    counted = json.loads(run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=nb_read_frames:frame=pts",
        "-of", "json", str(path)]))
    assert int(counted["streams"][0]["nb_read_frames"]) == OUTPUT
    pts = [int(frame.get("pts", frame.get("pkt_pts")))
           for frame in counted["frames"]]
    assert len(pts) == OUTPUT
    assert all(b > a for a, b in zip(pts, pts[1:])), "PTS must be strictly monotonic"
    # CFR: the per-frame step is exactly one 1/24 s unit of the time base.
    tb_num, tb_den = map(int, video["time_base"].split("/"))
    step = Fraction(1, FPS) / Fraction(tb_num, tb_den)
    assert step.denominator == 1, f"non-integral PTS step for {video['time_base']}"
    assert {b - a for a, b in zip(pts, pts[1:])} == {int(step)}


@pytest.mark.slow
def test_frame_animation_v1_240s_crossfade_integration(tmp_path):
    p = fixture_project(tmp_path, seconds=SECONDS, shot_count=SHOTS,
                        lyric_count=2)
    animation_init(p)
    # Shrink the canvas so the honest 5760-frame compose + decode stays cheap;
    # the frame arithmetic under test is identical at any canvas size.
    config = read(Path(p) / "project.yaml")
    config["format"].update(width=64, height=36)
    write(Path(p) / "project.yaml", config)

    # Edit: every shot uses 762 source frames; every boundary crossfades 48,
    # so sum(used) - sum(overlap) = 5760 = target_frames.
    edit_path = Path(p) / "timeline/edit.json"
    timeline = read_canon(edit_path)
    for entry in timeline["entries"]:
        entry["used_source_range"] = [0, USED]
    for index, entry in enumerate(timeline["entries"][:-1]):
        entry["transition_out"] = {"id": f"T{index + 1:03d}",
                                   "type": "CROSSFADE",
                                   "to_instance": f"I{index + 2:03d}",
                                   "overlap_frames": OVERLAP,
                                   "curve": "LINEAR_INTERIOR_V1"}
    write_canon(edit_path, timeline)

    audit = audit_timeline(timeline)
    assert audit["used_source_frames"] == TOTAL_USED
    assert audit["overlap_frames"] == TOTAL_OVERLAP
    assert audit["used_source_frames"] - audit["overlap_frames"] == OUTPUT
    assert audit["coverage"]["uncovered_frames"] == 0
    assert audit["coverage"]["shared_frames"] == TOTAL_OVERLAP
    assert audit["coverage"]["frames_with_three_or_more"] == 0

    # Every shot pins a path-A sequence covering its whole used range.
    variants = [_variant(i) for i in range(16)]
    for index, entry in enumerate(timeline["entries"]):
        folder = tmp_path / f"seq_{entry['shot_id']}"
        folder.mkdir()
        for member in range(USED):
            (folder / f"f{member:06d}.png").write_bytes(
                variants[(index * 7 + member) % len(variants)])
        import_frame_sequence(p, entry["shot_id"], folder=folder)

    result = compile_draft_preview(p)
    assert result["status"] == "COMPLETE"
    assert result["frames"]["total"] == OUTPUT
    assert result["frames"]["resolved_frames"] == OUTPUT
    assert result["frames"]["placeholder_frames"] == 0
    # Frame-map rows: every output frame carries <= 2 sources and exactly the
    # overlap count carries two (the CROSSFADE arithmetic, row by row).
    rows = [json.loads(line)
            for line in (Path(result["build_dir"]) / "draft_frame_map.jsonl")
            .read_text().splitlines() if line.strip()]
    assert len(rows) == OUTPUT
    assert all(len(row["sources"]) <= 2 for row in rows)
    assert sum(1 for row in rows if len(row["sources"]) == 2) == TOTAL_OVERLAP

    output = Path(result["output"])
    assert output.name == "DRAFT_PREVIEW.mp4"
    _decoded(output, (64, 36))


@pytest.mark.slow
def test_legacy_mv_240s_build1_replay_regression(tmp_path):
    p = fixture_project(tmp_path, seconds=SECONDS, shot_count=SHOTS,
                        lyric_count=2)
    compile_preview(p)
    folder, record = newest_build(p)
    assert record["duration_ms"] == SECONDS * 1000
    assert record["mode"] == "PREVIEW"
    assert len(record["shots"]) == SHOTS

    master = Path(folder) / "MASTER_SUBBED.mp4"
    assert_master(master, SECONDS)
    _decoded(master, (320, 240))

    # Build 1 replay: identical manifest, same decoded output.
    replay_dir = tmp_path / "replay"
    replay_build(folder, replay_dir)
    assert_master(replay_dir / "MASTER_SUBBED.mp4", SECONDS)
    _decoded(replay_dir / "MASTER_SUBBED.mp4", (320, 240))
    assert verify_build(folder)["valid"]
