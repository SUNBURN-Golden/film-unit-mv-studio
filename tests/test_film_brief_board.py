"""film-brief-board: engine contracts for engine/brief.py.

Temporary (staged) materials must never feed compile or approval fingerprints;
adoption is an explicit action with a recorded digest; changing an adopted
original reports — but never silently performs — the re-review it requires.
Fixtures are synthetic; no review record here is a real artwork approval.
"""
from pathlib import Path

import pytest

from engine import brief
from engine.audio import synth_test_audio
from engine.compiler import compile_preview, import_asset
from engine.core import (FilmError, digest, ffmpeg, lock_production,
                         production_fingerprint, read,
                         visual_context_fingerprint)
from test_compiler_v03 import fixture_project


def new_project(tmp_path, seconds=2, **kwargs):
    audio = synth_test_audio(tmp_path / "first.wav", seconds=seconds)
    return brief.create_project(tmp_path / "projects", "song_a", audio,
                                "A small brief", lyrics="line one\nline two\n", **kwargs)


def test_staging_records_digest_and_never_touches_fingerprints(tmp_path):
    p = fixture_project(tmp_path)
    before = production_fingerprint(p)
    context = visual_context_fingerprint(p)
    record = brief.stage_material(p, "mood.png", b"fake png bytes", "reference idea")
    assert record["state"] == "temporary" and record["kind"] == "image"
    assert digest(p / record["path"]) == record["sha256"]
    assert (p / "brief" / "staging").is_dir()
    board = brief.load_board(p)
    assert board["staged"][0]["sha256"] == record["sha256"]
    assert production_fingerprint(p) == before
    assert visual_context_fingerprint(p) == context


def test_duplicate_stage_is_refused(tmp_path):
    p = fixture_project(tmp_path)
    brief.stage_material(p, "a.txt", b"same bytes")
    with pytest.raises(FilmError, match="Duplicate"):
        brief.stage_material(p, "b.txt", b"same bytes")


def test_staging_the_adopted_master_bytes_is_a_duplicate(tmp_path):
    p = fixture_project(tmp_path)
    with pytest.raises(FilmError, match="Duplicate"):
        brief.stage_material(p, "copy.wav", (p / "input/master.wav").read_bytes())


def test_validate_audio_accepts_and_reports_duration(tmp_path):
    wav = synth_test_audio(tmp_path / "ok.wav", seconds=2)
    assert brief.validate_audio(wav)["seconds"] == pytest.approx(2.0, abs=0.1)


def test_validate_audio_rejects_wrong_suffix(tmp_path):
    wav = synth_test_audio(tmp_path / "ok.wav", seconds=2)
    flac = tmp_path / "song.flac"
    flac.write_bytes(wav.read_bytes())
    with pytest.raises(FilmError, match="MP3 or WAV"):
        brief.validate_audio(flac)


def test_validate_audio_rejects_corrupt_and_video_only(tmp_path):
    broken = tmp_path / "broken.mp3"
    broken.write_bytes(b"definitely not audio")
    with pytest.raises(FilmError, match="Cannot read"):
        brief.validate_audio(broken)
    mp4 = tmp_path / "clip.mp4"
    ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=64x64:d=1:r=24",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", mp4])
    video_only = tmp_path / "clip.wav"      # video container under an audio name
    video_only.write_bytes(mp4.read_bytes())
    with pytest.raises(FilmError, match="No audio stream"):
        brief.validate_audio(video_only)


def test_validate_audio_rejects_missing_too_short_and_overlong(tmp_path, monkeypatch):
    with pytest.raises(FilmError, match="Missing audio"):
        brief.validate_audio(tmp_path / "none.wav")
    short = synth_test_audio(tmp_path / "short.wav", seconds=0.5)
    with pytest.raises(FilmError, match="1 second and 10 minutes"):
        brief.validate_audio(short)
    wav = synth_test_audio(tmp_path / "ok.wav", seconds=2)
    monkeypatch.setattr(brief, "probe", lambda path: {
        "streams": [{"codec_type": "audio"}], "format": {"duration": "601.0"}})
    with pytest.raises(FilmError, match="1 second and 10 minutes"):
        brief.validate_audio(wav)


def test_adopt_audio_replaces_master_before_analysis_and_preserves_old(tmp_path):
    p = new_project(tmp_path)
    other = synth_test_audio(tmp_path / "other.wav", seconds=3)
    record = brief.stage_material(p, "other.wav", other.read_bytes())
    old_sha = read(p / "project.yaml")["audio"]["sha256"]
    result = brief.adopt_audio(p, record["id"])
    config = read(p / "project.yaml")
    assert config["audio"]["sha256"] == digest(p / "input/master.wav") != old_sha
    assert result["replaced"] == old_sha
    preserved = list((p / "input").glob("master-superseded-*"))
    assert len(preserved) == 1 and digest(preserved[0]) == old_sha
    board = brief.load_board(p)
    assert board["adopted"]["audio"]["sha256"] == config["audio"]["sha256"]
    assert board["adoptions"][-1]["sha256"] == config["audio"]["sha256"]


def test_adopt_audio_refuses_a_different_song_once_measured(tmp_path):
    p = fixture_project(tmp_path)
    original = (p / "input/master.wav").read_bytes()
    other = synth_test_audio(tmp_path / "other.wav", seconds=3)
    record = brief.stage_material(p, "other.wav", other.read_bytes())
    with pytest.raises(FilmError, match="new project"):
        brief.adopt_audio(p, record["id"])
    assert (p / "input/master.wav").read_bytes() == original
    assert read(p / "project.yaml")["audio"]["sha256"] == digest(p / "input/master.wav")


def test_adopt_audio_twice_is_a_duplicate(tmp_path):
    p = new_project(tmp_path)
    other = synth_test_audio(tmp_path / "other.wav", seconds=3)
    record = brief.stage_material(p, "other.wav", other.read_bytes())
    brief.adopt_audio(p, record["id"])
    with pytest.raises(FilmError, match="already adopted"):
        brief.adopt_audio(p, record["id"])


def test_adopt_audio_restores_missing_master_but_refuses_another(tmp_path):
    p = fixture_project(tmp_path)
    data = (p / "input/master.wav").read_bytes()
    (p / "input/master.wav").unlink()
    record = brief.stage_material(p, "restored.wav", data)
    brief.adopt_audio(p, record["id"])
    assert (p / "input/master.wav").read_bytes() == data
    # A different song still cannot take the measured timeline's place.
    other = synth_test_audio(tmp_path / "other.wav", seconds=4)
    record = brief.stage_material(p, "other.wav", other.read_bytes())
    with pytest.raises(FilmError, match="new project"):
        brief.adopt_audio(p, record["id"])
    assert (p / "input/master.wav").read_bytes() == data


def test_adopt_lyrics_archives_timing_and_clears_review(tmp_path):
    p = fixture_project(tmp_path)
    before = read(p / "lyrics/lyrics_timed.json")
    assert before["review"]["reviewer"]
    brief.adopt_text(p, "lyrics", "New first line\nNew second line\n")
    document = read(p / "lyrics/lyrics_timed.json")
    assert document["cues"] == [] and document["review"] is None
    assert len(document["unresolved_row_ids"]) == 2
    history = list((p / "lyrics/history").glob("timing_*.json"))
    assert len(history) == 1 and read(history[0])["cues"] == before["cues"]
    impact = brief.impact_report(p)
    assert impact["lyrics"]["review_state"] == "UNREVIEWED"
    assert any("lyric" in item["target"] for item in impact["needs_review"])


def test_adopting_lyrics_never_derives_timing_from_length(tmp_path):
    p = fixture_project(tmp_path)
    brief.adopt_text(p, "lyrics", "\n".join(f"replacement line {i}" for i in range(40)))
    document = read(p / "lyrics/lyrics_timed.json")
    assert document["cues"] == []
    assert all("start_ms" not in row and "end_ms" not in row for row in document["rows"])
    assert len(document["unresolved_row_ids"]) == 40
    assert (p / "input/lyrics.txt").read_text().count("replacement line") == 40


def test_change_impact_reports_stale_lock_reviews_and_cues(tmp_path):
    p = fixture_project(tmp_path)
    take = tmp_path / "take.mp4"
    take.write_bytes(b"synthetic take bytes; identity only matters here")
    import_asset(p, "S001", take, kind="final", reviewer="R", evidence="checked")
    lock_production(p, "Director")
    impact = brief.impact_report(p)
    assert impact["lock"]["state"] == "CURRENT"
    assert impact["lyrics"]["review_state"] == "CURRENT"
    assert impact["visual_reviews"][0]["state"] == "CURRENT"
    assert impact["needs_review"] == []
    brief.adopt_text(p, "brief", "A completely different brief")
    impact = brief.impact_report(p)
    assert impact["lock"]["state"] == "STALE"
    assert impact["visual_reviews"][0]["state"] == "STALE"
    targets = [item["target"] for item in impact["needs_review"]]
    assert any("LOCK" in t for t in targets) and any("S001" in t for t in targets)
    # The lyric review binds source/cues/font, not the brief: still current.
    assert impact["lyrics"]["review_state"] == "CURRENT"
    # Existing records are preserved, not silently rewritten or inherited.
    assert read(p / "manifest/locks.json")["reviewer"] == "Director"


def test_temporary_materials_never_reach_a_build(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1)
    brief.stage_material(p, "evil.mp3", b"bogus audio bytes")
    brief.stage_note(p, "watch this https://example.invalid/ref — memo only")
    result = compile_preview(p)
    assert result["status"] == "COMPLETE"
    build = Path(result["build_dir"])
    paths = [str(f.relative_to(build)) for f in build.rglob("*")]
    assert not any("staging" in s or "evil" in s for s in paths)


def test_create_project_refuses_to_overwrite_an_existing_package(tmp_path):
    p = fixture_project(tmp_path)   # named compiler_fixture under tmp_path
    manifest = (p / "manifest/shots.json").read_bytes()
    lock_production(p, "Director")
    locked = (p / "manifest/locks.json").read_bytes()
    other = synth_test_audio(tmp_path / "other.wav", seconds=2)
    with pytest.raises(FilmError, match="Project exists"):
        brief.create_project(tmp_path, "compiler_fixture", other, "x")
    assert (p / "manifest/shots.json").read_bytes() == manifest
    assert (p / "manifest/locks.json").read_bytes() == locked


def test_create_project_registers_adopted_inputs(tmp_path):
    audio = synth_test_audio(tmp_path / "a.wav", seconds=2)
    p = brief.create_project(tmp_path / "projects", "song_b", audio,
                             "brief text", "la la\n", emotion="잔잔함")
    board = brief.load_board(p)
    config = read(p / "project.yaml")
    assert board["adopted"]["audio"]["sha256"] == config["audio"]["sha256"]
    assert board["adopted"]["brief"]["sha256"] == digest(p / "input/brief.md")
    assert config["direction"]["emotion"] == "잔잔함"


def test_adopt_reference_records_digest_and_rejects_duplicates(tmp_path):
    p = fixture_project(tmp_path)
    record = brief.stage_material(p, "ref.png", b"pngbytes", "mood")
    entry = brief.adopt_reference(p, record["id"])
    assert entry["path"] == "input/references/ref.png"
    assert digest(p / entry["path"]) == entry["sha256"] == record["sha256"]
    assert brief.load_board(p)["staged"][0]["state"] == "adopted"
    with pytest.raises(FilmError, match="Duplicate"):
        brief.stage_material(p, "ref2.png", b"pngbytes")


def test_adopt_same_text_is_a_duplicate(tmp_path):
    p = fixture_project(tmp_path)
    current = (p / "input/lyrics.txt").read_text(encoding="utf-8")
    with pytest.raises(FilmError, match="Duplicate"):
        brief.adopt_text(p, "lyrics", current)


def test_adopt_text_rejects_empty_brief_and_unknown_field(tmp_path):
    p = fixture_project(tmp_path)
    with pytest.raises(FilmError, match="Brief text is required"):
        brief.adopt_text(p, "brief", "   ")
    with pytest.raises(FilmError, match="Unknown brief field"):
        brief.adopt_text(p, "bogus", "x")


def test_adopt_emotion_writes_existing_project_input(tmp_path):
    p = fixture_project(tmp_path)
    brief.adopt_text(p, "emotion", "잔잔하고 쓸쓸함")
    assert read(p / "project.yaml")["direction"]["emotion"] == "잔잔하고 쓸쓸함"
    assert brief.load_board(p)["adopted"]["emotion"]["text"] == "잔잔하고 쓸쓸함"


def test_discard_removes_only_temporary_materials(tmp_path):
    p = fixture_project(tmp_path)
    record = brief.stage_material(p, "temp.txt", b"temp bytes")
    brief.adopt_reference(p, record["id"])
    with pytest.raises(FilmError, match="temporary"):
        brief.discard_material(p, record["id"])
    other = brief.stage_material(p, "gone.txt", b"other bytes")
    brief.discard_material(p, other["id"])
    assert not (p / other["path"]).exists()
    assert all(r["id"] != other["id"] for r in brief.load_board(p)["staged"])


def test_board_status_shows_limits_pending_and_slate_state(tmp_path):
    p = new_project(tmp_path, emotion="")    # no analysis, no emotion, no refs
    status = brief.board_status(p)
    assert status["limits"]["baseline_seconds"] == 240
    assert status["limits"]["fps"] == 24
    assert status["limits"]["audio_suffixes"] == [".mp3", ".wav"]
    assert status["audio"]["present"] and status["audio"]["matches_config"]
    assert "desired emotion undecided" in status["pending"]
    assert "no adopted references" in status["pending"]
    assert "audio not analyzed" in status["pending"]
    (tmp_path / "second").mkdir()
    fixture = fixture_project(tmp_path / "second")
    fixture_status = brief.board_status(fixture)
    assert fixture_status["audio"]["synthetic"] is True
    assert fixture_status["placeholders"] == []   # fixture uses imported storyboards


def test_missing_master_is_reported_not_hidden(tmp_path):
    p = fixture_project(tmp_path)
    (p / "input/master.wav").unlink()
    impact = brief.impact_report(p)
    assert impact["audio"]["present"] is False
    assert any(item["target"] == "master audio" for item in impact["needs_review"])
