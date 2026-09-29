"""AI director: a fake text model drafts, the User reviews, nothing changes until accept."""
import json
import pytest
from fakes import FakeText, WORLD
from engine import director, providers
from engine.audio import analyze, synth_test_audio
from engine.core import FilmError, init_project, lock_production, read, require_lock, write
from engine.production import make_package

@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    audio = synth_test_audio(tmp_path / "t.wav", seconds=30)
    p = init_project(tmp_path, "dir", audio, "A woman remembers her old kitchen.", "첫 줄 가사\n둘째 줄 가사", synthetic=True, aspect="16:9")
    analyze(p)
    make_package(p)
    return p


def test_draft_is_saved_for_review_and_changes_nothing_else(project):
    before = {n: (project / n).read_bytes() for n in ("manifest/shots.json", "bible/story.md", "bible/characters.yaml", "input/lyrics.txt")}
    fake = FakeText()
    proposal = director.draft(project, fake)
    shots = read(project / "manifest/shots.json")
    assert set(proposal["shots"]) == {s["id"] for s in shots} and len(fake.calls) == 1 + -(-len(shots) // director.BATCH)
    assert director.pending(project) and director.complete(project)
    for name, data in before.items():
        assert (project / name).read_bytes() == data
    world_prompt = json.loads(fake.calls[0][1])
    assert "old kitchen" in world_prompt["brief"] and world_prompt["lyrics"] == "첫 줄 가사\n둘째 줄 가사"
    assert world_prompt["shots"][0]["id"] == "S001" and "Never translate, rewrite or invent lyrics" in fake.calls[0][0]


def test_accept_writes_the_production_files_with_provenance_and_needs_relock(project):
    lock_production(project, "Director", mock_only=True)
    require_lock(project, "mock")
    write(project / "bible/characters.yaml", {"draft": True, "characters": [
        {"id": "CHAR_A", "variants": [], "invariants": {}, "behavior": "x", "reference_images": ["characters/keep.png"]}]})
    director.draft(project, FakeText())
    before = read(project / "manifest/shots.json")
    with pytest.raises(FilmError, match="검토자"):
        director.accept(project, " ")
    summary = director.accept(project, "Director")
    assert summary["characters"] == 1 and not director.pending(project)
    after = read(project / "manifest/shots.json")
    for old, new in zip(before, after):
        assert (old["id"], old["in_ms"], old["out_ms"], old["duration_ms"], old["references"]) == \
               (new["id"], new["in_ms"], new["out_ms"], new["duration_ms"], new["references"])
        assert new["characters"] == ["CHAR_A"] and new["render_mode"] == "LIMITED_MOTION" and "Mina" in new["description"]
        assert new["motion"]["local_effect"] == "hold" and new["motion"]["instruction"].startswith("She turns")
    chars = read(project / "bible/characters.yaml")
    assert chars["draft"] is False and chars["characters"][0]["reference_images"] == ["characters/keep.png"]
    assert "red coat" in chars["characters"][0]["look"]
    assert read(project / "bible/locations.yaml")["locations"][0]["id"] == "LOC_KITCHEN"
    style = read(project / "bible/style_bible.yaml")
    assert style["draft"] is False and style["format"]["aspect_ratio"] == "16:9" and "medium" in style["visual_style"]
    story = (project / "bible/story.md").read_text(encoding="utf-8")
    assert "오래된 부엌" in story and "A woman remembers" in story and "accepted by Director" in story
    log = read(project / "bible/director_log.json")
    assert log[0]["reviewer"] == "Director" and log[0]["provider"] == "groq" and len(log[0]["proposal_sha256"]) == 64
    with pytest.raises(FilmError, match="unlocked|changed"):
        require_lock(project, "mock")


def test_a_failed_batch_keeps_finished_work_and_can_resume(project, monkeypatch):
    monkeypatch.setattr(director, "BATCH", 2)
    fake = FakeText()
    fake.fail_at = 3          # world ok, first batch ok, second batch fails
    with pytest.raises(FilmError, match="network down"):
        director.draft(project, fake)
    kept = director.pending(project)
    assert kept["world"] and 0 < len(kept["shots"]) < len(read(project / "manifest/shots.json"))
    assert not director.complete(project)
    with pytest.raises(FilmError, match="완성된 제안"):
        director.accept(project, "Director")
    calls_before = len(fake.calls)
    fake.fail_at = None
    proposal = director.draft(project, fake, resume=True)
    assert director.complete(project) and len(fake.calls) - calls_before == 2     # only the missing batches were asked
    fresh = director.draft(project, FakeText(), notes="different notes", resume=True)
    assert fresh["created_at"] != proposal["created_at"]                           # changed inputs start over


@pytest.mark.parametrize("break_it, message", [
    (lambda d: d.update(characters=[]), "1-6 characters"),
    (lambda d: d["characters"][0].update(id="woman"), "CHAR_A"),
    (lambda d: d["characters"][0].update(look="short"), "look"),
    (lambda d: d["locations"].append(dict(d["locations"][0])), "Duplicate"),
    (lambda d: d.update(story=""), "story"),
])
def test_bad_worlds_are_rejected_without_writing_anything(project, break_it, message):
    world = json.loads(json.dumps(WORLD))
    break_it(world)
    with pytest.raises(FilmError, match=message):
        director.draft(project, FakeText(world))
    assert director.pending(project) is None


def test_bad_shot_answers_are_rejected(project):
    world = director.validate_world(WORLD)
    good = {"id": "S001", "description": "Mina pours water into a glass", "characters": ["CHAR_A"], "locations": ["LOC_KITCHEN"],
            "composition": "close-up", "camera": {"type": "locked", "movement": "none"},
            "motion": {"complexity": "low", "instruction": "Water fills the glass slowly"}, "render_mode": "LIMITED_MOTION"}
    assert director.validate_shots({"shots": [good]}, ["S001"], world)["S001"]["render_mode"] == "LIMITED_MOTION"
    for change, message in [({"render_mode": "SLIDESHOW"}, "render_mode"), ({"characters": ["CHAR_Z"]}, "ids from the world"),
                            ({"motion": {"instruction": ""}}, "instruction"), ({"description": "short"}, "description")]:
        with pytest.raises(FilmError, match=message):
            director.validate_shots({"shots": [{**good, **change}]}, ["S001"], world)
    with pytest.raises(FilmError, match="exactly the requested"):
        director.validate_shots({"shots": [good]}, ["S001", "S002"], world)
    still = {**good, "render_mode": "STATIC", "motion": {"complexity": "low", "instruction": ""}}
    assert director.validate_shots({"shots": [still]}, ["S001"], world)["S001"]["render_mode"] == "STATIC"


def test_extract_json_tolerates_fences_and_rejects_prose():
    assert director.extract_json('Sure!\n```json\n{"a": 1}\n```\nDone') == {"a": 1}
    with pytest.raises(FilmError, match="JSON"):
        director.extract_json("I cannot do that")
    with pytest.raises(FilmError, match="JSON"):
        director.extract_json("{broken json")


def test_provider_constraints_reach_the_prompt(project):
    providers.select(project, "video", "gemini_video")
    providers.select(project, "image", "cloudflare_flux")
    fake = FakeText()
    director.draft(project, fake)
    assert "adult" in fake.calls[0][0] and "reference images" in fake.calls[0][0]


def test_timed_lyrics_are_offered_only_when_reviewed_cues_exist(project):
    assert director.cues_between(project, 0, 5000) == []
    write(project / "lyrics/lyrics_timed.json", {"cues": [{"text": "첫 줄 가사", "start_ms": 1000, "end_ms": 3000},
                                                            {"text": "둘째 줄 가사", "start_ms": 9000, "end_ms": 11000}]})
    assert director.cues_between(project, 0, 5000) == ["첫 줄 가사"]
    fake = FakeText()
    director.draft(project, fake)
    first_batch = json.loads(fake.calls[1][1])["shots"]
    assert first_batch[0]["timed_lyrics"] == ["첫 줄 가사"]


def test_discard_removes_only_the_proposal(project):
    director.draft(project, FakeText())
    director.discard(project)
    assert director.pending(project) is None and (project / "bible/story.md").exists()
