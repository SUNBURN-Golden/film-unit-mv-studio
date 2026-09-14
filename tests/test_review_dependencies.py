"""Final review dependencies use synthetic media, never actual AI generation.

These are review-state regressions, not claims of artistic or semantic QC.
"""
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess

from PIL import Image
import pytest

from engine.compiler import compile_final, import_asset
from engine.core import (
    FilmError, digest, frame_at, lock_production, object_hash, production_fingerprint, read,
    require_lock, write,
)
from engine.lyrics import save_timing, validate_lyrics
from engine.qc import SEMANTIC_ITEMS, inspect_clip, save_review
from engine.renderers import normalize
from engine.resolver import candidates
from engine.timeline import move_cut

from test_compiler_v03 import assert_master, fixture_project, newest_build, source_clip


def reviewed_project(tmp_path):
    project = fixture_project(tmp_path, seconds=6, shot_count=3, lyric_count=3)
    # Keep extra source duration so a +100 ms cut is not a short-clip test.
    clip = source_clip(tmp_path / "synthetic_review_take.mp4", seconds=3)
    lock_production(project, "Synthetic fixture reviewer", mock_only=False)
    for shot in read(project / "manifest/shots.json"):
        import_asset(project, shot["id"], clip, kind="final",
                     reviewer="Synthetic fixture reviewer",
                     evidence="Synthetic local video; explicitly approved only for this test")
    assert approved_shots(project) == {"S001", "S002", "S003"}
    return project, clip


def approved_shots(project):
    production_id = production_fingerprint(project)
    return {
        shot["id"] for shot in read(project / "manifest/shots.json")
        if any(candidate["kind"] == "final" and candidate["approved"]
               for candidate in candidates(project, shot, production_id))
    }


def relock(project):
    lock_production(project, "Synthetic fixture reviewer", mock_only=False)
    require_lock(project, "manual")


def test_cut_edit_preserves_unrelated_final_review_and_still_requires_relock(tmp_path):
    project, _ = reviewed_project(tmp_path)
    untouched = deepcopy(read(project / "manifest/assets.json")["shots"]["S003"])
    timing_sha = digest(project / "lyrics/lyrics_timed.json")

    move_cut(project, "S001", 2100)
    with pytest.raises(FilmError):
        require_lock(project, "manual")
    with pytest.raises(FilmError):
        compile_final(project)
    relock(project)

    # The two changed shots require review; no blanket invalidation of S003.
    assert approved_shots(project) == {"S003"}
    assert read(project / "manifest/assets.json")["shots"]["S003"] == untouched
    assert digest(project / "lyrics/lyrics_timed.json") == timing_sha
    with pytest.raises(FilmError, match="eligible|approved|review"):
        compile_final(project)


def test_lyric_timing_edit_preserves_all_visual_reviews_after_review_and_relock(tmp_path):
    project, _ = reviewed_project(tmp_path)
    assets_sha = digest(project / "manifest/assets.json")
    master_sha = digest(project / "input/master.wav")
    timing = read(project / "lyrics/lyrics_timed.json")
    timing["cues"][1]["start_ms"] -= 80
    timing["cues"][1]["end_ms"] -= 80
    save_timing(project, timing)

    with pytest.raises(FilmError):
        require_lock(project, "manual")
    with pytest.raises(FilmError):
        validate_lyrics(project, 6000, strict=True)
    save_timing(project, read(project / "lyrics/lyrics_timed.json"),
                reviewer="Synthetic revised timing reviewer")
    with pytest.raises(FilmError):
        require_lock(project, "manual")
    relock(project)

    assert approved_shots(project) == {"S001", "S002", "S003"}
    assert digest(project / "manifest/assets.json") == assets_sha
    assert digest(project / "input/master.wav") == master_sha
    result = compile_final(project)
    folder, record = newest_build(project)
    assert result["mode"] == record["mode"] == "FINAL"
    assert all(shot["approved"] for shot in record["shots"])
    assert_master(folder / "MASTER_CLEAN.mp4", 6)
    assert_master(folder / "MASTER_SUBBED.mp4", 6)


def test_style_edit_invalidates_all_visual_reviews_even_after_relock(tmp_path):
    project, _ = reviewed_project(tmp_path)
    assets_sha = digest(project / "manifest/assets.json")
    style = read(project / "bible/style_bible.yaml")
    style["palette"] = {"background": "#f5d7a0", "ink": "#102040"}
    write(project / "bible/style_bible.yaml", style)

    with pytest.raises(FilmError):
        require_lock(project, "manual")
    relock(project)

    assert approved_shots(project) == set()
    assert digest(project / "manifest/assets.json") == assets_sha
    with pytest.raises(FilmError, match="eligible|approved|review"):
        compile_final(project)


def test_storyboard_byte_edit_invalidates_only_that_shots_final_review(tmp_path):
    project, _ = reviewed_project(tmp_path)
    assets_sha = digest(project / "manifest/assets.json")
    original = digest(project / "storyboard/S001.png")
    Image.new("RGB", (320, 240), "orange").save(project / "storyboard/S001.png")
    assert digest(project / "storyboard/S001.png") != original

    with pytest.raises(FilmError):
        require_lock(project, "manual")
    relock(project)

    assert approved_shots(project) == {"S002", "S003"}
    assert digest(project / "manifest/assets.json") == assets_sha
    with pytest.raises(FilmError, match="eligible|approved|review"):
        compile_final(project)


@pytest.mark.parametrize("change", ["settings", "font_bytes"])
def test_subtitle_settings_and_font_require_lyric_review_but_keep_visual_reviews(tmp_path, change):
    project, clip = reviewed_project(tmp_path)
    if change == "font_bytes":
        # Use a real readable font. Changing only head.fontRevision retains glyph
        # coverage while testing content binding at the same configured pathname.
        font_source = Path(subprocess.run(
            ["fc-match", "-f", "%{file}", "DejaVu Sans"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout)
        target = project / "input/subtitle_font.ttf"
        shutil.copyfile(font_source, target)
        config = read(project / "project.yaml")
        config.setdefault("subtitles", {})["font_file"] = "input/subtitle_font.ttf"
        write(project / "project.yaml", config)
        save_timing(project, read(project / "lyrics/lyrics_timed.json"),
                    reviewer="Synthetic fixture with configured font")
        relock(project)
        for shot_id in ("S001", "S002", "S003"):
            import_asset(project, shot_id, clip, kind="final",
                         reviewer="Synthetic fixture reviewer",
                         evidence="Synthetic local video with initial font configuration")

    assets_sha = digest(project / "manifest/assets.json")
    if change == "settings":
        config = read(project / "project.yaml")
        config.setdefault("subtitles", {})["font_size"] = 14
        write(project / "project.yaml", config)
    else:
        from fontTools.ttLib import TTFont
        old_sha = digest(target)
        with TTFont(target) as font:
            font["head"].fontRevision += 0.125
            font.save(target)
        assert digest(target) != old_sha

    assert approved_shots(project) == {"S001", "S002", "S003"}
    with pytest.raises(FilmError):
        require_lock(project, "manual")
    with pytest.raises(FilmError, match="lyric|Lyric|review"):
        validate_lyrics(project, 6000, strict=True)
    # A global relock must not silently approve the changed caption styling.
    relock(project)
    with pytest.raises(FilmError, match="lyric|Lyric|review"):
        compile_final(project)
    save_timing(project, read(project / "lyrics/lyrics_timed.json"),
                reviewer="Synthetic revised subtitle presentation reviewer")
    with pytest.raises(FilmError):
        require_lock(project, "manual")
    relock(project)

    assert approved_shots(project) == {"S001", "S002", "S003"}
    assert digest(project / "manifest/assets.json") == assets_sha
    assert compile_final(project)["mode"] == "FINAL"


def test_legacy_global_review_is_not_promoted_after_a_production_change(tmp_path):
    project, _ = reviewed_project(tmp_path)
    original_production = production_fingerprint(project)
    registry = read(project / "manifest/assets.json")
    for selections in registry["shots"].values():
        entry = selections["final"]
        entry.pop("reference_hash", None)
        entry["review"] = {
            "reviewer": "Legacy synthetic fixture reviewer",
            "evidence": "Legacy review had only whole-production dependencies",
            "production_id": original_production,
            "binding": object_hash({
                "sha256": entry["sha256"], "shot_hash": entry["shot_hash"],
                "source_in_ms": entry.get("source_in_ms", 0),
                "production_id": original_production,
            }),
        }
    write(project / "manifest/assets.json", registry)
    legacy_sha = digest(project / "manifest/assets.json")
    timing = read(project / "lyrics/lyrics_timed.json")
    timing["cues"][1]["start_ms"] -= 80
    timing["cues"][1]["end_ms"] -= 80
    save_timing(project, timing, reviewer="Synthetic revised timing reviewer")
    relock(project)

    # The old record never captured a scoped dependency snapshot. Do not infer
    # retrospectively that its approval was independent of the changed inputs.
    assert approved_shots(project) == set()
    assert digest(project / "manifest/assets.json") == legacy_sha
    with pytest.raises(FilmError, match="eligible|approved|review"):
        compile_final(project)


def test_renderer_qc_review_uses_scoped_dependencies_and_source_window(tmp_path):
    project, source = reviewed_project(tmp_path)
    shot = read(project / "manifest/shots.json")[0]
    fmt = read(project / "project.yaml")["format"]
    clip = project / "render/final/synthetic_qc_clip.mp4"
    frames = frame_at(shot["out_ms"], fmt["fps"]) - frame_at(shot["in_ms"], fmt["fps"])
    normalize(source, clip, frames, fmt)
    original_production = production_fingerprint(project)
    # generated=True exercises semantic gating using a local color fixture; it
    # does not claim that a video model or automatic artistic evaluator ran.
    record = inspect_clip(clip, shot, project, fmt, generated=True,
                          production_id=original_production)
    assert record["status"] == "NEEDS_REVIEW"
    assert record["technical"]["no_audio"]
    save_review(project, record, {item: 100 for item in SEMANTIC_ITEMS},
                "Synthetic semantic fixture reviewer",
                "TEST ONLY: explicit fixture scores, no artistic evaluation claimed")
    review_file = project / record["review_file"]
    review_sha = digest(review_file)

    timing = read(project / "lyrics/lyrics_timed.json")
    timing["cues"][1]["start_ms"] -= 80
    timing["cues"][1]["end_ms"] -= 80
    save_timing(project, timing, reviewer="Synthetic revised timing reviewer")
    relock(project)
    revised_production = production_fingerprint(project)
    assert revised_production != original_production
    same_visual = inspect_clip(clip, shot, project, fmt, generated=True,
                               production_id=revised_production)
    assert same_visual["status"] == "PASS"
    assert same_visual["review_binding"] == record["review_binding"]
    assert digest(review_file) == review_sha

    changed_window = inspect_clip(clip, shot, project, fmt, generated=True,
                                  production_id=revised_production, source_in_ms=100)
    assert changed_window["status"] == "NEEDS_REVIEW"
    assert changed_window["review_binding"] != record["review_binding"]

    style = read(project / "bible/style_bible.yaml")
    style["palette"] = {"background": "#f5d7a0"}
    write(project / "bible/style_bible.yaml", style)
    relock(project)
    changed_style = inspect_clip(clip, shot, project, fmt, generated=True,
                                 production_id=production_fingerprint(project))
    assert changed_style["status"] == "NEEDS_REVIEW"
    assert changed_style["review_binding"] != record["review_binding"]
    assert digest(review_file) == review_sha
