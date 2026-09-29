"""Image generation over interchangeable providers: fake providers only, no network, no spend."""
import io
from PIL import Image
import pytest
from engine import imagegen
from engine.audio import analyze, synth_test_audio
from engine.core import FilmError, init_project, read, write
from engine.imagegen import ImageProvider, ImageRejected, NothingToDo, fit_aspect
from engine.packets import compact_prompt, reference_prompt
from engine.production import make_package
from engine.renderers import RenderBlocked


def png(size=(64, 64), color="teal"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class Fake(ImageProvider):
    def __init__(self, unit=0.0, refs=False, limit=None, native=None, pid="fake"):
        self.id, self.model, self.unit_usd = pid, "fake-1", unit
        self.supports_references, self.max_references, self.prompt_limit, self.native_aspects = refs, 3 if refs else 0, limit, native
        self.calls, self.plan = [], []   # plan: list of exceptions/None consumed per call

    def generate(self, prompt, references, aspect):
        self.calls.append((prompt, list(references), aspect))
        action = self.plan.pop(0) if self.plan else None
        if isinstance(action, Exception):
            raise action
        return png((64, 64)) if not self.native_aspects else png((160, 90))


@pytest.fixture
def project(tmp_path):
    audio = synth_test_audio(tmp_path / "t.wav", seconds=12)
    p = init_project(tmp_path, "img", audio, "Fixture", synthetic=True, aspect="16:9")
    analyze(p)
    make_package(p)
    config = read(p / "project.yaml")
    config["budget"].update(max_usd=5, max_retry_per_shot=1)
    write(p / "project.yaml", config)
    return p


def describe_cast(p):
    chars = read(p / "bible/characters.yaml")
    chars["characters"][0].update(look="a tall woman with a red coat and short black hair", behavior="calm")
    write(p / "bible/characters.yaml", chars)
    locs = read(p / "bible/locations.yaml")
    locs["locations"][0]["description"] = "a narrow kitchen with a green table"
    write(p / "bible/locations.yaml", locs)


def test_free_provider_needs_no_approval_and_crops_to_the_project_ratio(project):
    fake = Fake(unit=0.0, limit=2048)
    estimate = imagegen.estimate(project, "frames", fake)
    shots = read(project / "manifest/shots.json")
    assert estimate["worst_case_amount"] == 0 and len(estimate["rows"]) == len(shots)
    result = imagegen.generate(project, "frames", fake)      # no approval file exists
    assert sorted(result["generated"]) == sorted(s["id"] for s in shots) and not result["failed"]
    assert all(s["storyboard_kind"] == "imported" for s in read(project / "manifest/shots.json"))
    with Image.open(project / shots[0]["references"][0]) as frame:
        assert abs(frame.width / frame.height - 16 / 9) < 0.02 and frame.size == (64, 36)
    assert all(a == "16:9" for _, _, a in fake.calls)
    with pytest.raises(NothingToDo):
        imagegen.estimate(project, "frames", fake)


def test_paid_provider_needs_the_exact_estimate_approved_and_respects_the_budget(project):
    fake = Fake(unit=0.09, refs=True, native={"16:9", "1:1"})
    with pytest.raises(FilmError, match="승인"):
        imagegen.generate(project, "frames", fake)
    estimate = imagegen.estimate(project, "frames", fake)
    assert estimate["initial_amount"] == round(0.09 * 3, 6) and estimate["worst_case_amount"] == round(0.09 * 3 * 2, 6)
    with pytest.raises(FilmError, match="does not match"):
        imagegen.approve(project, "frames", "wrong")
    imagegen.approve(project, "frames", estimate["estimate_id"])
    shots = read(project / "manifest/shots.json")
    shots[0]["description"] = "Changed after approval"
    write(project / "manifest/shots.json", shots)
    with pytest.raises(FilmError, match="승인"):
        imagegen.generate(project, "frames", fake)
    estimate = imagegen.estimate(project, "frames", fake)
    imagegen.approve(project, "frames", estimate["estimate_id"])
    imagegen.generate(project, "frames", fake)
    assert imagegen.reserved_usd(project) == round(0.09 * 3, 6)
    config = read(project / "project.yaml")
    config["budget"]["max_usd"] = 0.1
    write(project / "project.yaml", config)
    with pytest.raises(FilmError, match="exceeds budget"):
        imagegen.estimate(project, "frames", Fake(unit=0.09), ["S001"])


def test_paid_ambiguity_blocks_but_definitive_rejection_and_free_failures_do_not(project):
    paid = Fake(unit=0.09)
    imagegen.approve(project, "frames", imagegen.estimate(project, "frames", paid)["estimate_id"])
    paid.plan = [TimeoutError("sent, no answer")]
    with pytest.raises(RenderBlocked, match="reconciliation"):
        imagegen.generate(project, "frames", paid)
    with pytest.raises(RenderBlocked, match="unknown outcome"):
        imagegen.generate(project, "frames", paid)
    assert len(paid.calls) == 1

    fresh = Fake(unit=0.09, pid="fake2")
    imagegen.approve(project, "frames", imagegen.estimate(project, "frames", fresh)["estimate_id"])
    fresh.plan = [ImageRejected("quota")]
    with pytest.raises(RenderBlocked, match="quota"):
        imagegen.generate(project, "frames", fresh)
    result = imagegen.generate(project, "frames", fresh)       # nothing was created, so this may run again
    assert not result["failed"] and len(result["generated"]) == 3


def test_free_provider_retries_then_reports_the_shots_that_kept_failing(project):
    free = Fake(unit=0.0)
    imagegen.estimate(project, "frames", free)
    free.plan = [TimeoutError()] * 2        # S001 uses both attempts; the other shots succeed
    result = imagegen.generate(project, "frames", free)
    assert result["failed"] == ["S001"] and len(result["generated"]) == 2
    again = imagegen.estimate(project, "frames", free)
    assert again["rows"][0]["key"] == "S001" and again["after_failed_jobs"]
    assert imagegen.generate(project, "frames", free)["generated"] == ["S001"]


def test_reference_images_are_made_once_and_listed_in_the_bible(project):
    describe_cast(project)
    fake = Fake(unit=0.0, refs=True)
    estimate = imagegen.estimate(project, "references", fake)
    assert [r["key"] for r in estimate["rows"]] == ["CHAR_01", "LOC_01"]
    assert imagegen.generate(project, "references", fake)["generated"] == ["CHAR_01", "LOC_01"]
    assert (project / "characters/CHAR_01.png").is_file() and (project / "locations/LOC_01.png").is_file()
    assert read(project / "bible/characters.yaml")["characters"][0]["reference_images"] == ["characters/CHAR_01.png"]
    with pytest.raises(NothingToDo):
        imagegen.estimate(project, "references", fake)
    frames = Fake(unit=0.0, refs=True)
    imagegen.estimate(project, "frames", frames)
    imagegen.generate(project, "frames", frames)
    assert all(str(project / "characters/CHAR_01.png") in map(str, refs) for _, refs, _ in frames.calls)


def test_empty_descriptions_are_skipped_with_a_reason(project):
    with pytest.raises(NothingToDo) as caught:
        imagegen.estimate(project, "references", Fake(refs=True))
    assert caught.value.warnings and "CHAR_01" in caught.value.warnings[0]


def test_providers_without_reference_input_get_short_self_contained_prompts(project):
    describe_cast(project)
    shots = read(project / "manifest/shots.json")
    shots[0]["description"] = "She pours water into a glass. " * 5
    write(project / "manifest/shots.json", shots)
    fake = Fake(unit=0.0, refs=False, limit=400)
    imagegen.estimate(project, "frames", fake)
    imagegen.generate(project, "frames", fake)
    prompt, refs, _ = fake.calls[0]
    assert len(prompt) <= 400 and refs == [] and "red coat" in prompt and "16:9" in prompt
    assert len(compact_prompt(project, shots[0], "16:9", 120)) <= 120
    assert "front view" in reference_prompt(project, read(project / "bible/characters.yaml")["characters"][0], "character")


def test_fit_aspect_only_crops_when_needed():
    square = png((100, 100))
    with Image.open(io.BytesIO(fit_aspect(square, "16:9"))) as cropped:
        assert cropped.size == (100, 56)
    with Image.open(io.BytesIO(fit_aspect(square, "9:16"))) as cropped:
        assert cropped.size == (56, 100)
    assert Image.open(io.BytesIO(fit_aspect(png((160, 90)), "16:9", {"16:9"}))).size == (160, 90)
    assert Image.open(io.BytesIO(fit_aspect(square, "1:1"))).size == (100, 100)


def test_unsupported_aspect_is_refused_before_any_request(project):
    fake = Fake(unit=0.0, native={"1:1", "4:3"})
    with pytest.raises(FilmError, match="16:9"):
        imagegen.estimate(project, "frames", fake)
    assert not fake.calls
