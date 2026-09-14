"""Content fidelity, review invalidation, partial-preview and media guarantees."""
from pathlib import Path
import shutil
import subprocess

import pytest

from engine.core import FilmError, digest, ffmpeg, probe, read, write
from engine.lyrics import (
    burn_subtitles, export_subtitles, prepare_lyrics, save_timing,
    lyrics_review_fingerprint, timing_fingerprint, validate_lyrics,
)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "input").mkdir()
    (tmp_path / "analysis").mkdir()
    (tmp_path / "input/lyrics.txt").write_text("[hook]\nWe belong here\nWe belong here\n", encoding="utf-8")
    write(tmp_path / "project.yaml", {"format": {"width": 320, "height": 240, "fps": 24, "crf": 28}})
    write(tmp_path / "analysis/audio.json", {"duration_ms": 2000})
    return tmp_path


def timed(project, reviewer=""):
    doc = prepare_lyrics(project)
    rows = [row for row in doc["rows"] if row["kind"] == "lyric"]
    doc["cues"] = [
        {"id": f"C{index:03d}", "source_row_id": row["id"],
         "start_ms": index * 800, "end_ms": index * 800 + 700, "text": row["text"]}
        for index, row in enumerate(rows)
    ]
    return save_timing(project, doc, reviewer=reviewer)


def test_source_authority_duplicate_rows_and_no_invented_timing(project):
    doc = prepare_lyrics(project)
    assert not doc["cues"]
    assert [row["id"] for row in doc["rows"]] == ["L0001", "L0002", "L0003"]
    assert len(set(doc["unresolved_row_ids"])) == 2
    assert (project / "lyrics/lyrics_source.txt").read_bytes() == (project / "input/lyrics.txt").read_bytes()
    saved = timed(project, reviewer="Director")
    assert saved["cues"][0]["text"] == saved["cues"][1]["text"]
    assert saved["cues"][0]["source_row_id"] != saved["cues"][1]["source_row_id"]
    assert validate_lyrics(project, 2000, strict=True)[1] == []


def test_source_bytes_preserved_and_change_invalidates_review(project):
    old = timed(project, reviewer="Director")
    source = b"\xef\xbb\xbf[hook]\r\nA new lyric\r\n"
    (project / "input/lyrics.txt").write_bytes(source)
    new = prepare_lyrics(project)
    assert new["cues"] == [] and new["review"] is None
    assert new["source_sha256"] != old["source_sha256"]
    assert (project / "lyrics/lyrics_source.txt").read_bytes() == source
    history = list((project / "lyrics/history").glob("*.json"))
    assert len(history) == 1
    assert read(history[0])["cues"] == old["cues"]
    with pytest.raises(FilmError, match="source changed"):
        save_timing(project, old, reviewer="Director")


@pytest.mark.parametrize("edit,match", [
    (lambda doc: doc["cues"][0].update(text="New words"), "authoritative"),
    (lambda doc: doc["cues"][0].update(start_ms=0.5), "integer-millisecond"),
    (lambda doc: doc["cues"][0].update(start_ms=True), "integer-millisecond"),
    (lambda doc: doc["cues"][1].update(start_ms=600), "Overlapping"),
    (lambda doc: doc["cues"][1].update(end_ms=2001), "integer-millisecond"),
    (lambda doc: doc["cues"][1].update(source_row_id="L9999"), "Unknown source"),
    (lambda doc: doc["cues"][1].update(id="C000"), "unique"),
])
def test_invalid_edits_cannot_be_saved(project, edit, match):
    doc = timed(project)
    before = digest(project / "lyrics/lyrics_timed.json")
    edit(doc)
    with pytest.raises(FilmError, match=match):
        save_timing(project, doc)
    assert digest(project / "lyrics/lyrics_timed.json") == before


def test_partial_preview_and_review_cannot_approve_incomplete_timing(project):
    doc = timed(project)
    doc["cues"].pop()
    save_timing(project, doc)
    preview, warnings = validate_lyrics(project, 2000)
    assert len(preview["cues"]) == 1
    assert preview["unresolved_row_ids"] == ["L0003"]
    assert any("untimed" in warning for warning in warnings)
    with pytest.raises(FilmError, match="Resolve every"):
        save_timing(project, doc, reviewer="Director")
    with pytest.raises(FilmError, match="Final lyrics are incomplete"):
        validate_lyrics(project, 2000, strict=True)


def test_timing_edit_invalidates_review_shot_edit_does_not(project):
    doc = timed(project, reviewer="Director")
    timing_hash = timing_fingerprint(doc)
    review_hash = lyrics_review_fingerprint(project, doc)
    write(project / "manifest/shots.json", [{"id": "S001", "in_ms": 100}])
    write(project / "bible/style.yaml", {"palette": "changed visual palette"})
    assert timing_fingerprint(validate_lyrics(project, 2000, strict=True)[0]) == timing_hash
    assert lyrics_review_fingerprint(project, doc) == review_hash
    assert validate_lyrics(project, 2000, strict=True)[0]["review"] == doc["review"]
    doc["cues"][0]["start_ms"] = 100
    write(project / "lyrics/lyrics_timed.json", doc)
    with pytest.raises(FilmError, match="reviewed"):
        validate_lyrics(project, 2000, strict=True)
    assert validate_lyrics(project, 2000)[0]["review"] is None


def test_legacy_timing_only_review_requires_explicit_subtitle_review(project):
    doc = timed(project, reviewer="Director")
    assert doc["review"]["schema_version"] == 2
    assert doc["review"]["lyrics_review_sha256"] == lyrics_review_fingerprint(project, doc)
    doc["review"].pop("schema_version")
    doc["review"].pop("lyrics_review_sha256")
    write(project / "lyrics/lyrics_timed.json", doc)
    before = digest(project / "lyrics/lyrics_timed.json")
    preview, warnings = validate_lyrics(project, 2000)
    assert preview["review"] is None
    assert preview["cues"] == doc["cues"]
    assert any("subtitle settings and font" in warning for warning in warnings)
    assert digest(project / "lyrics/lyrics_timed.json") == before
    with pytest.raises(FilmError, match="subtitle settings and font"):
        validate_lyrics(project, 2000, strict=True)
    save_timing(project, doc, reviewer="Director")
    assert validate_lyrics(project, 2000, strict=True)[1] == []


@pytest.mark.parametrize("group,field,value", [
    ("subtitles", "font_size", 18),
    ("subtitles", "font_name", "DejaVu Serif"),
    ("subtitles", "margin_bottom", 30),
    ("format", "width", 640),
    ("format", "height", 480),
    ("format", "fps", 30),
])
def test_subtitle_presentation_change_requires_review_without_retiming(project, group, field, value):
    doc = timed(project, reviewer="Director")
    old_binding = doc["review"]["lyrics_review_sha256"]
    config = read(project / "project.yaml")
    config.setdefault(group, {})[field] = value
    write(project / "project.yaml", config)
    preview, warnings = validate_lyrics(project, 2000)
    assert preview["review"] is None
    assert preview["cues"] == doc["cues"]
    assert timing_fingerprint(preview) == doc["review"]["timing_sha256"]
    assert lyrics_review_fingerprint(project, preview) != old_binding
    assert any("subtitle settings and font" in warning for warning in warnings)
    with pytest.raises(FilmError, match="subtitle settings and font"):
        validate_lyrics(project, 2000, strict=True)
    revised = save_timing(project, preview, reviewer="Director")
    assert revised["review"]["lyrics_review_sha256"] != old_binding
    assert validate_lyrics(project, 2000, strict=True)[1] == []


def test_custom_font_content_is_bound_and_project_relocation_preserves_review(project):
    paths = [Path(subprocess.run(["fc-match", "-f", "%{file}", family],
                                capture_output=True, text=True, check=True).stdout)
             for family in ("DejaVu Sans", "DejaVu Serif")]
    assert paths[0].is_file() and paths[1].is_file()
    assert digest(paths[0]) != digest(paths[1])
    target = project / "fonts/captions.ttf"
    target.parent.mkdir()
    shutil.copyfile(paths[0], target)
    config = read(project / "project.yaml")
    config["subtitles"] = {"font_file": "fonts/captions.ttf", "font_name": "Project Caption Font"}
    write(project / "project.yaml", config)
    doc = timed(project, reviewer="Director")
    old_binding = lyrics_review_fingerprint(project, doc)
    file_hashes = {str(path.relative_to(project)): digest(path) for path in project.rglob("*") if path.is_file()}
    assert lyrics_review_fingerprint(project, doc) == old_binding
    assert file_hashes == {str(path.relative_to(project)): digest(path) for path in project.rglob("*") if path.is_file()}

    moved = project.parent / (project.name + "_moved")
    shutil.copytree(project, moved)
    assert lyrics_review_fingerprint(moved, doc) == old_binding
    assert validate_lyrics(moved, 2000, strict=True)[0]["review"] == doc["review"]

    shutil.copyfile(paths[1], target)
    assert lyrics_review_fingerprint(project, doc) != old_binding
    assert validate_lyrics(project, 2000)[0]["review"] is None
    with pytest.raises(FilmError, match="subtitle settings and font"):
        validate_lyrics(project, 2000, strict=True)
    renewed = save_timing(project, doc, reviewer="Director")
    assert renewed["cues"] == doc["cues"]
    assert validate_lyrics(project, 2000, strict=True)[1] == []

    target.unlink()
    preview, warnings = validate_lyrics(project, 2000)
    assert preview["review"] is None
    assert preview["cues"] == doc["cues"]
    assert any("font_file does not exist" in warning for warning in warnings)
    with pytest.raises(FilmError, match="font_file does not exist"):
        validate_lyrics(project, 2000, strict=True)


def test_json_cannot_replace_source_text_and_invalid_preview_is_excluded(project):
    doc = timed(project, reviewer="Director")
    doc["rows"][1]["text"] = "Injected text"
    doc["cues"][0]["text"] = "Injected text"
    write(project / "lyrics/lyrics_timed.json", doc)
    preview, warnings = validate_lyrics(project, 2000)
    assert preview["cues"] == []
    assert any("excluded from Preview" in warning for warning in warnings)
    with pytest.raises(FilmError, match="authoritative"):
        validate_lyrics(project, 2000, strict=True)


def test_repeated_hook_requires_explicit_expansion_and_timing(project):
    (project / "input/lyrics.txt").write_text("[hook]\nRoots remain\n[verse]\nWe belong\n[hook]\n", encoding="utf-8")
    doc = timed(project)
    assert doc["unresolved_row_ids"] == ["L0005"]
    doc["repeat_expansions"] = {"L0005": ["L0002"]}
    doc["cues"].append({"id": "C003", "source_row_id": "L0005:L0002", "start_ms": 1600, "end_ms": 1900, "text": "Roots remain"})
    saved = save_timing(project, doc, reviewer="Director")
    assert not saved["unresolved_row_ids"]
    assert validate_lyrics(project, 2000, strict=True)[1] == []
    doc["repeat_expansions"] = {"L0005": ["L0004"]}
    with pytest.raises(FilmError, match="matching source section"):
        save_timing(project, doc)


def test_long_source_row_can_split_without_altering_text(project):
    (project / "input/lyrics.txt").write_text("We belong here together\n", encoding="utf-8")
    doc = prepare_lyrics(project)
    text = doc["rows"][0]["text"]
    doc["cues"] = [
        {"id": "C1", "source_row_id": "L0001", "start_ms": 0, "end_ms": 700, "char_start": 0, "char_end": 9, "text": text[:9]},
        {"id": "C2", "source_row_id": "L0001", "start_ms": 800, "end_ms": 1600, "char_start": 10, "char_end": len(text), "text": text[10:]},
    ]
    assert not save_timing(project, doc, reviewer="Director")["unresolved_row_ids"]
    doc["cues"][1]["char_start"] = 12
    doc["cues"][1]["text"] = text[12:]
    with pytest.raises(FilmError, match="Resolve every"):
        save_timing(project, doc, reviewer="Director")


def test_subtitles_preserve_ms_and_escape_ass_controls(project):
    (project / "input/lyrics.txt").write_text(r"We {rise}, \move" + "\n", encoding="utf-8")
    doc = timed(project, reviewer="Director")
    doc["cues"][0]["start_ms"] = 123
    doc["cues"][0]["end_ms"] = 987
    save_timing(project, doc, reviewer="Director")
    result = export_subtitles(project, project / "export", 2000, strict=True)
    srt, ass = result["srt"].read_text(), result["ass"].read_text()
    assert "00:00:00,123 --> 00:00:00,987" in srt
    assert r"We {rise}, \move" in srt
    assert "We \\{rise\\}, \\\u2060move" in ass
    assert "Dialogue: 0,0:00:00.12,0:00:00.99" in ass
    assert result["font_report"]["status"] == "verified"
    assert any("centiseconds" in warning for warning in result["warnings"])
    assert (project / "export/subtitle_fonts").is_dir()


def test_korean_missing_font_cannot_masquerade_as_complete_final(project):
    (project / "input/lyrics.txt").write_text("존재를 긍정해\n", encoding="utf-8")
    timed(project, reviewer="Director")
    result = export_subtitles(project, project / "preview", 2000)
    assert result["font_report"]["status"] == "missing_glyphs"
    assert result["font_report"]["missing_codepoints"]
    with pytest.raises(FilmError, match="glyph coverage"):
        export_subtitles(project, project / "final", 2000, strict=True)


def test_burn_preserves_aac_packets_duration_and_safe_paths(project):
    timed(project, reviewer="Director")
    output = project / "directory: with ' quotes"
    export = export_subtitles(project, output, 2000, strict=True)
    clean, subbed = output / "clean.mp4", output / "subbed.mp4"
    ffmpeg(["-f", "lavfi", "-i", "color=c=gray:s=320x240:r=24:d=2", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", clean])
    clean_hash = digest(clean)
    burn_subtitles(clean, export["ass"], subbed, {"fps": 24, "crf": 28})
    assert digest(clean) == clean_hash
    assert probe(subbed)["streams"][0]["nb_frames"] == "48"
    hashes = []
    for path in (clean, subbed):
        audio = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c:a", "copy", "-f", "hash", "-"], capture_output=True, check=True).stdout
        hashes.append(audio)
    assert hashes[0] == hashes[1]


def test_corrupt_successful_encoder_does_not_replace_existing_output(project, monkeypatch):
    from engine import lyrics
    timed(project, reviewer="Director")
    export = export_subtitles(project, project / "export", 2000, strict=True)
    clean, output = project / "clean.mp4", project / "existing.mp4"
    ffmpeg(["-f", "lavfi", "-i", "color=c=gray:s=320x240:r=24:d=1", "-f", "lavfi", "-i", "sine=duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", clean])
    output.write_bytes(b"existing build")
    real_run = lyrics.subprocess.run
    def broken_encoder(args, **kwargs):
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"not a movie")
            return subprocess.CompletedProcess(args, 0, b"", b"")
        return real_run(args, **kwargs)
    monkeypatch.setattr(lyrics.subprocess, "run", broken_encoder)
    with pytest.raises(FilmError):
        burn_subtitles(clean, export["ass"], output, {"fps": 24})
    assert output.read_bytes() == b"existing build"
