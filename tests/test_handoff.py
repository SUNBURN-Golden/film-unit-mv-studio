"""Browser hand-off: copy a request out, paste the answer back, drop the files that come back."""
import io
import json
import os
import shutil
import subprocess
import time

import pytest
from PIL import Image

from fakes import FakeText, WORLD
from engine import director, handoff, providers
from engine.audio import analyze, synth_test_audio
from engine.core import FilmError, init_project, read
from engine.production import make_package


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    audio = synth_test_audio(tmp_path / "t.wav", seconds=30)
    p = init_project(tmp_path, "hand", audio, "A woman remembers her old kitchen.", "첫 줄 가사\n둘째 줄 가사", synthetic=True, aspect="16:9")
    analyze(p)
    make_package(p)
    return p


def png(width=640, height=360, color="teal", fmt="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format=fmt)
    return buffer.getvalue()


def answer_for(request_text):
    """What a chat service would send back, made by the same fake the API path uses."""
    system, _, rest = request_text.partition("\n\n=== 입력 자료 (JSON) ===\n")
    user = rest.rsplit("\n\n위 지시대로", 1)[0]
    return FakeText().complete(system, user)


# ---- services ----------------------------------------------------------------------------

def test_services_are_plain_https_links_for_the_users_own_browser():
    assert {s.id for s in handoff.SERVICES} >= {"chatgpt", "claude", "gemini", "grok"}
    assert all(s.url.startswith("https://") for s in handoff.SERVICES)
    assert {s.id for s in handoff.services_for("video")} >= {"gemini", "flow", "grok"}
    assert [s.id for s in handoff.services_for("text")][:2] == ["chatgpt", "claude"]
    assert handoff.custom_service("https://example.com/app").name == "example.com"
    for bad in ("http://example.com", "javascript:alert(1)", "https://user:pw@example.com", "example.com", ""):
        with pytest.raises(FilmError):
            handoff.custom_service(bad)


def test_the_manual_text_choice_needs_no_key_and_is_never_called_as_an_api():
    manual = providers.get("manual_text")
    assert manual.stage == "text" and manual.cost == "manual" and not manual.fields
    assert providers.status("manual_text")["configured"]
    with pytest.raises(FilmError, match="붙여넣"):
        providers.build_text("manual_text")


# ---- storyboard: paste out, paste back ---------------------------------------------------

def test_the_whole_storyboard_can_be_done_by_copy_and_paste(project):
    total = len(read(project / "manifest/shots.json"))
    assert handoff.storyboard_state(project)["step"] == "world"
    request = handoff.world_request(project, "슬프지만 담담하게", "English")
    assert "Never translate, rewrite or invent lyrics" in request and "old kitchen" in request and "슬프지만 담담하게" in request
    assert "S001" in request and request.rstrip().endswith("코드 블록 하나 안에 넣어도 됩니다.")

    proposal = handoff.submit_world(project, "네, 여기 있습니다:\n```json\n" + json.dumps(WORLD, ensure_ascii=False) + "\n```", "chatgpt", "슬프지만 담담하게", "English")
    assert proposal["provider"] == "handoff:chatgpt" and proposal["world"]["characters"][0]["id"] == "CHAR_A"
    assert handoff.storyboard_state(project)["step"] == "shots"

    batches = 0
    while (nxt := handoff.shots_request(project)):
        batches += 1
        assert len(nxt["ids"]) <= director.BATCH and "exactly the shots" in nxt["text"] and "CHAR_A" in nxt["text"]
        state = handoff.submit_shots(project, answer_for(nxt["text"]))
    assert batches == -(-total // director.BATCH) and state["step"] == "done" and director.complete(project)
    summary = director.accept(project, "Director")          # the existing review-and-accept path
    assert summary["shots"] == total and read(project / "bible/director_log.json")[0]["provider"] == "handoff:chatgpt"
    assert all("Mina" in s["description"] for s in read(project / "manifest/shots.json"))


def test_a_bad_or_partial_answer_changes_nothing_and_says_why(project):
    with pytest.raises(FilmError, match="JSON"):
        handoff.submit_world(project, "죄송하지만 그건 도와드릴 수 없습니다.")
    bad = dict(WORLD, characters=[{"id": "nobody", "look": "x" * 20}])
    with pytest.raises(FilmError, match="CHAR_A"):
        handoff.submit_world(project, json.dumps(bad))
    assert director.pending(project) is None
    handoff.submit_world(project, json.dumps(WORLD), "claude")
    with pytest.raises(FilmError):
        handoff.submit_shots(project, json.dumps({"shots": []}))
    first = handoff.shots_request(project)
    wrong = json.loads(answer_for(first["text"]).split("```json\n")[1].split("\n```")[0])
    wrong["shots"] = wrong["shots"][:-1]
    with pytest.raises(FilmError, match="exactly the requested shots"):
        handoff.submit_shots(project, json.dumps(wrong))
    assert director.pending(project)["shots"] == {}
    assert handoff.shots_request(project)["ids"] == first["ids"]        # the same batch is still asked for


def test_answers_are_taken_in_order_and_an_existing_draft_is_not_overwritten_by_accident(project):
    with pytest.raises(FilmError, match="차례"):
        handoff.submit_shots(project, "{}")
    handoff.submit_world(project, json.dumps(WORLD))
    nxt = handoff.shots_request(project)
    handoff.submit_shots(project, answer_for(nxt["text"]))
    with pytest.raises(FilmError, match="초안 버리기"):
        handoff.submit_world(project, json.dumps(WORLD))
    assert handoff.submit_world(project, json.dumps(WORLD), replace=True)["shots"] == {}


def test_a_draft_from_the_api_can_be_finished_by_hand(project):
    fake = FakeText()
    fake.fail_at = 2                                          # the first shot batch fails
    with pytest.raises(FilmError):
        director.draft(project, fake)
    state = handoff.storyboard_state(project)
    assert state["step"] == "shots" and state["done"] == 0
    assert handoff.shots_request(project)["ids"][0] == "S001"


# ---- images and videos coming back --------------------------------------------------------

def test_a_frame_is_imported_recorded_and_flagged_when_the_shape_is_wrong(project):
    result = handoff.import_frame(project, "S001", png(1280, 720), "Gemini_Generated_Image.png", "gemini")
    assert result["warnings"] == [] and (result["width"], result["height"]) == (1280, 720)
    shots = read(project / "manifest/shots.json")
    assert shots[0]["storyboard_kind"] == "imported" and Image.open(project / "storyboard/S001.png").size == (1280, 720)
    square = handoff.import_frame(project, "S002", png(800, 800), "x.png", "grok")
    assert "화면비" in square["warnings"][0] and "16:9" in square["warnings"][0]
    log = read(project / "render/handoff_log.json")
    assert [(e["kind"], e["target"], e["service"]) for e in log] == [("frame", "S001", "gemini"), ("frame", "S002", "grok")]
    assert len(log[0]["sha256"]) == 64 and log[0]["filename"] == "Gemini_Generated_Image.png"
    assert not any((project / "render/handoff_inbox").glob("*"))      # the staging copy is removed


def test_unsafe_or_broken_files_are_refused_before_they_touch_the_project(project):
    before = (project / "manifest/shots.json").read_bytes()
    cases = [("S001", b"not an image", "a.png", "읽을 수 없"), ("S001", png(), "a.gif", "형식"), ("S001", b"", "a.png", "크기"),
             ("S001", png(100, 100), "small.png", "크기가 맞지"), ("S999", png(), "a.png", "Unknown shot"),
             ("S001", png(), "../../evil.png", None)]
    for shot, data, name, message in cases[:5]:
        with pytest.raises(FilmError, match=message):
            handoff.import_frame(project, shot, data, name)
    handoff.import_frame(project, "S001", *cases[5][1:3])               # a path in the name is only ever a name
    assert not (project.parent / "evil.png").exists() and not (project / "evil.png").exists()
    assert (project / "manifest/shots.json").read_bytes() != before
    assert not any((project / "render/handoff_inbox").glob("*"))


def test_reference_images_attach_to_the_character_once(project):
    handoff.submit_world(project, json.dumps(WORLD))          # accepting the draft fills the character and location lists
    while (nxt := handoff.shots_request(project)):
        handoff.submit_shots(project, answer_for(nxt["text"]))
    director.accept(project, "Director")
    assert {"folder": "characters", "id": "CHAR_A"} in handoff.missing_references(project)
    handoff.import_reference(project, "characters", "CHAR_A", png(512, 512, fmt="JPEG"), "sheet.jpg", "gemini")
    assert (project / "characters/CHAR_A.png").is_file()
    assert read(project / "bible/characters.yaml")["characters"][0]["reference_images"] == ["characters/CHAR_A.png"]
    assert {"folder": "characters", "id": "CHAR_A"} not in handoff.missing_references(project)
    with pytest.raises(FilmError, match="이미 참조"):
        handoff.import_reference(project, "characters", "CHAR_A", png(512, 512), "again.png")
    with pytest.raises(FilmError, match="알 수 없는"):
        handoff.import_reference(project, "characters", "CHAR_ZZZ", png(512, 512), "x.png")
    with pytest.raises(FilmError):
        handoff.import_reference(project, "../bible", "CHAR_A", png(512, 512), "x.png")


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "veo.mp4"
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24:duration=2",
                    "-pix_fmt", "yuv420p", "-y", str(path)], check=True)
    return path


def test_a_video_take_is_checked_stored_as_a_draft_and_logged(project, clip):
    result = handoff.import_video(project, "S001", clip.read_bytes(), "Flow_clip.mp4", "flow")
    assert result["seconds"] == pytest.approx(2, abs=0.2) and result["warnings"] == []
    take = read(project / "manifest/assets.json")["shots"]["S001"]["draft"]
    assert take["path"].startswith("render/assets/") and take["review"]["reviewer"] == ""      # a draft is not an approval
    assert read(project / "render/handoff_log.json")[-1]["kind"] == "video"
    rows = {r["id"]: r for r in handoff.shot_status(project)}
    assert rows["S001"]["video"] and not rows["S002"]["video"]
    with pytest.raises(FilmError, match="읽을 수 없"):
        handoff.import_video(project, "S001", b"not a video at all", "x.mp4")
    with pytest.raises(FilmError, match="형식"):
        handoff.import_video(project, "S001", clip.read_bytes(), "x.exe")
    assert not any((project / "render/handoff_inbox").glob("*"))


# ---- downloads folder ---------------------------------------------------------------------

def test_the_downloads_scan_lists_only_recent_plain_media_files(tmp_path, monkeypatch):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    (folder / "new.png").write_bytes(png())
    (folder / "old.png").write_bytes(png())
    os.utime(folder / "old.png", (time.time() - 86400, time.time() - 86400))
    (folder / "notes.txt").write_text("x")
    (folder / "clip.mp4").write_bytes(b"x")
    (folder / "sub").mkdir()
    if os.name != "nt":
        (folder / "link.png").symlink_to(folder / "new.png")
    monkeypatch.setenv("FILM_UNIT_DOWNLOADS", str(folder))
    assert [f["name"] for f in handoff.scan_downloads("image")] == ["new.png"]
    assert [f["name"] for f in handoff.scan_downloads("video")] == ["clip.mp4"]
    assert [f["name"] for f in handoff.scan_downloads("image", minutes=60 * 48)] == ["new.png", "old.png"]      # newest first
    assert handoff.scan_downloads("image", folder=tmp_path / "missing") == []
    assert handoff.read_download("new.png", "image") == png()
    for bad in ("../secret.png", "sub/x.png", "/etc/passwd", "sub", "nope.png", ""):
        with pytest.raises(FilmError):
            handoff.read_download(bad, "image")
    if os.name != "nt":
        with pytest.raises(FilmError):
            handoff.read_download("link.png", "image")


# ---- prompts to copy and batch import -----------------------------------------------------

def test_briefs_give_the_prompt_to_paste_and_the_files_to_attach(project):
    handoff.submit_world(project, json.dumps(WORLD))
    while (nxt := handoff.shots_request(project)):
        handoff.submit_shots(project, answer_for(nxt["text"]))
    director.accept(project, "Director")
    frame = handoff.frame_brief(project, "S001")
    assert "S001" in frame["prompt"] and "16:9" in frame["prompt"] and "Mina" in frame["prompt"] and len(frame["short_prompt"]) <= 900
    assert frame["attach"] == [] and frame["missing"] == []
    handoff.import_reference(project, "characters", "CHAR_A", png(512, 512), "sheet.png")
    assert handoff.frame_brief(project, "S001")["attach"] == ["characters/CHAR_A.png"]
    video = handoff.video_brief(project, "S001")
    assert video["prompt"] and video["seconds"] >= 1 and video["start_frame"] == "storyboard/S001.png" and not video["frame_ready"]
    assert "Environment reference" in handoff.reference_brief(project, "locations", "LOC_KITCHEN")["prompt"]
    with pytest.raises(FilmError):
        handoff.frame_brief(project, "S999")
    with pytest.raises(FilmError):
        handoff.reference_brief(project, "characters", "CHAR_NOPE")


def test_several_files_are_imported_in_order_and_one_bad_file_does_not_stop_the_rest(project):
    ids = [s["id"] for s in read(project / "manifest/shots.json")]
    assert handoff.default_targets(3, ids[:2]) == [ids[0], ids[1], None]
    files = [("a.png", png()), ("broken.png", b"nope"), ("c.png", png()), ("skipped.png", png())]
    plan = list(zip(handoff.default_targets(4, [ids[0], ids[1], ids[2]]), [f[0] for f in files], [f[1] for f in files]))
    results = handoff.import_many(project, "frame", plan, "gemini")
    assert [(r["file"], r["ok"]) for r in results] == [("a.png", True), ("broken.png", False), ("c.png", True)]
    assert "읽을 수 없" in results[1]["message"]
    kinds = {s["id"]: s.get("storyboard_kind") for s in read(project / "manifest/shots.json")}
    assert kinds[ids[0]] == "imported" and kinds[ids[1]] != "imported" and kinds[ids[2]] == "imported"
    with pytest.raises(FilmError, match="둘 이상"):
        handoff.import_many(project, "frame", [(ids[0], "a.png", png()), (ids[0], "b.png", png())])
    with pytest.raises(FilmError):
        handoff.import_many(project, "audio", [])


def test_large_files_can_be_read_lazily_one_at_a_time(project):
    ids = [s["id"] for s in read(project / "manifest/shots.json")]
    order = []

    def loader(name):
        def load():
            order.append(name)
            return png()
        return load

    results = handoff.import_many(project, "frame", [(ids[0], "a.png", loader("a")), (ids[1], "b.png", loader("b")), (None, "skip.png", loader("skip"))])
    assert [r["ok"] for r in results] == [True, True] and order == ["a", "b"]      # the skipped file was never read

    def failing():
        raise FilmError("다운로드 폴더 안의 일반 파일만 가져올 수 있습니다")
    result = handoff.import_many(project, "frame", [(ids[2], "gone.png", failing)])
    assert result[0]["ok"] is False and "일반 파일" in result[0]["message"]
