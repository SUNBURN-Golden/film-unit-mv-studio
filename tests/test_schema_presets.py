"""Generic authoring and migrations must preserve existing production approvals."""
from pathlib import Path

import pytest

from engine.core import FilmError, atomic_text, digest, lock_production, read, require_lock, write
from engine.production import load_preset, make_package
from engine.schema import SCHEMA_VERSIONS, migrate_project, schema_metadata


def project_fixture(tmp_path, name="song", preset=None):
    p = tmp_path / name
    for directory in ["input", "analysis", "bible", "characters", "locations", "manifest", "storyboard", "render/final"]:
        (p / directory).mkdir(parents=True)
    # Metadata-only tests do not decode sound or render videos.
    atomic_text(p / "input/master.wav", "unchanged master bytes")
    atomic_text(p / "input/brief.md", "A colorful science-fiction performance")
    atomic_text(p / "input/lyrics.txt", "User-authored words\n두 번째 줄")
    config = {"schema_version": "0.1", "name": name,
              "audio": {"path": "input/master.wav", "sha256": digest(p / "input/master.wav")},
              "format": {"width": 1440, "height": 1080, "fps": 24, "aspect_ratio": "4:3"},
              "budget": {"max_usd": 0}, "extension_setting": {"keep": True}}
    if preset:
        config["production"] = {"preset": preset}
    write(p / "project.yaml", config)
    write(p / "analysis/audio.json", {"duration_ms": 10000, "beat_times_ms": [500, 5010, 9000], "section_boundaries_ms": [0, 10000]})
    return p


def test_neutral_package_does_not_impose_first_films_art_direction(tmp_path):
    p = project_fixture(tmp_path)
    shots = make_package(p)
    style = read(p / "bible/style_bible.yaml")
    assert style["palette"] == {}
    assert style["visual_style"]["medium"] == "Director review required"
    assert shots[0]["characters"] == ["CHAR_01"]
    assert shots[0]["locations"] == ["LOC_01"]
    assert shots[0]["composition"] == "Director review required"
    assert shots[0]["schema_version"] == 2
    assert shots[0]["out_ms"] == 5010  # Preserve measured beat snapping.
    for filename in ["style_bible.yaml", "characters.yaml", "locations.yaml", "directing.yaml", "director_request.md"]:
        text = (p / "bible" / filename).read_text()
        for unwanted in ["CHAR_A", "triangle_tie", "round_glasses", "teal", "INTERROGATION", "split screen", "water_please"]:
            assert unwanted not in text
    assert (p / "storyboard/S001.png").is_file()


def test_subtitle_style_and_font_bytes_invalidate_lock(tmp_path):
    p = project_fixture(tmp_path)
    make_package(p)
    lock_production(p, "Director", mock_only=True)
    config = read(p / "project.yaml")
    config["subtitles"] = {"font_name": "Example", "font_file": "input/font.ttf"}
    atomic_text(p / "input/font.ttf", "font version one")
    write(p / "project.yaml", config)
    with pytest.raises(FilmError):
        require_lock(p, "mock")
    lock_production(p, "Director", mock_only=True)
    atomic_text(p / "input/font.ttf", "font version two")
    with pytest.raises(FilmError):
        require_lock(p, "mock")


@pytest.mark.parametrize("selection", ["argument", "config"])
def test_water_please_is_explicit_preset(tmp_path, selection):
    p = project_fixture(tmp_path, preset="water_please" if selection == "config" else None)
    shots = make_package(p, preset="water_please" if selection == "argument" else None)
    assert read(p / "bible/style_bible.yaml")["palette"]["teal"] == "#48A6A0"
    cast = read(p / "bible/characters.yaml")["characters"]
    assert cast[0]["invariants"]["black_triangle_tie"] is True
    assert cast[1]["invariants"]["round_glasses"] is True
    assert "LOC_ORPHANAGE" in {location["id"] for location in read(p / "bible/locations.yaml")["locations"]}
    assert shots[0]["composition"] == "vertical split screen"
    assert shots[0]["characters"] == ["CHAR_A", "CHAR_B"]


def test_preset_does_not_share_mutable_defaults_and_rejects_paths(tmp_path):
    package = load_preset()
    package["style"]["palette"]["custom"] = "#123456"
    assert load_preset()["style"]["palette"] == {}
    with pytest.raises(FilmError, match="Invalid preset"):
        make_package(project_fixture(tmp_path), preset="../water_please")


def test_migration_preserves_original_files_and_valid_lock_and_is_idempotent(tmp_path):
    p = project_fixture(tmp_path)
    make_package(p, preset="water_please")
    # Mimic a legacy manifest without per-shot schema metadata.
    shots = read(p / "manifest/shots.json")
    for shot in shots:
        shot.pop("schema_version")
    write(p / "manifest/shots.json", shots)
    lock_production(p, "Existing director", mock_only=True)
    atomic_text(p / "render/final/existing_take.mp4", "previously purchased video")
    original_config = (p / "project.yaml").read_bytes()
    before = {str(path.relative_to(p)): digest(path) for path in p.rglob("*") if path.is_file() and path.name != "project.yaml"}
    result = migrate_project(p)
    assert result["changed"] is True
    assert (p / result["backup"]).read_bytes() == original_config
    assert read(p / "project.yaml")["schema_versions"] == SCHEMA_VERSIONS
    assert read(p / "project.yaml")["extension_setting"] == {"keep": True}
    assert all(digest(p / name) == value for name, value in before.items())
    assert require_lock(p, "mock")["reviewer"] == "Existing director"
    config_hash = digest(p / "project.yaml")
    assert migrate_project(p)["changed"] is False
    assert digest(p / "project.yaml") == config_hash
    assert len(list((p / "migrations").glob("*/project.yaml"))) == 1


def test_migration_keeps_extension_schema_and_rejects_future_versions(tmp_path):
    p = project_fixture(tmp_path)
    config = read(p / "project.yaml")
    config["schema_versions"] = {"extension": 27}
    write(p / "project.yaml", config)
    migrate_project(p)
    assert read(p / "project.yaml")["schema_versions"]["extension"] == 27
    assert migrate_project(p)["changed"] is False
    config = read(p / "project.yaml")
    config["schema_versions"]["shot"] = 999
    write(p / "project.yaml", config)
    before = digest(p / "project.yaml")
    with pytest.raises(FilmError, match="Unsupported shot schema"):
        migrate_project(p)
    assert digest(p / "project.yaml") == before


def test_schema_metadata_is_independent_of_package_release():
    data = schema_metadata()
    assert data["schema_version"] == 3
    assert data["schema_versions"] == {"project": 3, "shot": 2, "lyrics": 1, "build": 1, "audio": 1}
    data["schema_versions"]["project"] = 999
    assert schema_metadata()["schema_versions"]["project"] == 3
