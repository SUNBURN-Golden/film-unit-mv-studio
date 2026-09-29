"""Drive the browser hand-off panels headlessly: paste answers, drop files, pick from downloads."""
import io
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from fakes import FakeText, WORLD
from engine import providers, settings
from engine.audio import analyze, synth_test_audio
from engine.core import init_project, read
from engine.production import make_package

ROOT = Path(__file__).resolve().parents[1]
NAME = "zz_ui_handoff"


def png(width=640, height=360):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "teal").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def screen(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FILM_UNIT_DOWNLOADS", str(tmp_path / "Downloads"))
    (tmp_path / "Downloads").mkdir()
    root = ROOT / "projects"
    root.mkdir(exist_ok=True)
    audio = synth_test_audio(tmp_path / "t.wav", seconds=30)
    project = init_project(root, NAME, audio, "A woman remembers her old kitchen.", "첫 줄 가사", synthetic=True, aspect="16:9")
    analyze(project)
    make_package(project)
    try:
        at = AppTest.from_file(str(ROOT / "app/control_panel.py"), default_timeout=90).run()
        at.sidebar.selectbox[0].select(NAME).run()
        assert not at.exception
        yield at, project
    finally:
        shutil.rmtree(project, ignore_errors=True)


def radio(at, key):
    return next(r for r in at.radio if r.key == key)


def button(at, label):
    return next(b for b in at.button if b.label == label or b.label.startswith(label))


def answer_box(at):
    return next(t for t in at.text_area if t.key and t.key.startswith("hand_answer_"))


def request_text(at):
    return next(c.value for c in at.code if "위 지시대로" in c.value)


def answer_for(request):
    system, _, rest = request.partition("\n\n=== 입력 자료 (JSON) ===\n")
    return FakeText().complete(system, rest.rsplit("\n\n위 지시대로", 1)[0])


def test_the_manual_text_choice_shows_a_copy_and_paste_storyboard(screen):
    at, project = screen
    radio(at, "pick_text").set_value("manual_text").run()
    assert not at.exception and not any("초안 만들기" == b.label for b in at.button)
    assert any("웹사이트에서 콘티 만들기" in m.value for m in at.markdown)
    assert any("로그인하거나 대신 조작하지 않습니다" in c.value for c in at.caption)
    assert "Never translate, rewrite or invent lyrics" in request_text(at)
    options = radio(at, "hand_text_service").options                          # what the User sees
    assert options[:2] == ["ChatGPT", "Claude"] and options[-1] == "다른 사이트" and radio(at, "hand_text_service").value == "chatgpt"

    answer_box(at).set_value("죄송합니다, 도와드릴 수 없어요.").run()
    button(at, "답 확인하고 받기").click().run()
    assert any("JSON" in e.value for e in at.error) and not (project / "bible/director_proposal.json").exists()

    answer_box(at).set_value("```json\n" + json.dumps(WORLD, ensure_ascii=False) + "\n```").run()
    button(at, "답 확인하고 받기").click().run()
    assert not at.exception and not at.error
    assert any("이야기와 인물, 장소를 받았습니다" in s.value for s in at.success)
    assert any("2단계" in m.value for m in at.markdown)

    while any("2단계" in m.value for m in at.markdown):
        answer_box(at).set_value(answer_for(request_text(at))).run()
        button(at, "답 확인하고 받기").click().run()
        assert not at.exception and not at.error
    assert any("모든 샷의 연출을 받았습니다" in s.value for s in at.success)
    before = read(project / "manifest/shots.json")
    button(at, "검토했고").click().run()
    assert not at.exception and not at.error
    after = read(project / "manifest/shots.json")
    assert all(a["in_ms"] == b["in_ms"] and a["out_ms"] == b["out_ms"] for a, b in zip(before, after))
    assert all("Mina" in s["description"] for s in after)
    assert read(project / "bible/director_log.json")[0]["provider"] == "handoff:chatgpt"


def test_connection_tab_explains_how_to_turn_it_on(screen):
    at, project = screen
    assert any("직접 만들기'를 고르면" in i.value for i in at.info)
    assert not [f for f in at.file_uploader if f.key and f.key.startswith("hand_")]
    next(c for c in at.checkbox if c.key == "hand_everything").check().run()
    drops = {f.key for f in at.file_uploader if f.key and f.key.startswith("hand_")}
    assert drops == {"hand_frame_drop", "hand_video_drop", "hand_reference_drop"} - ({"hand_reference_drop"} if not handoff_missing(project) else set())


def handoff_missing(project):
    from engine import handoff
    return handoff.missing_references(project)


def choose_manual(at):
    radio(at, "pick_image").set_value("manual_image").run()
    radio(at, "pick_video").set_value("manual_video").run()


def test_dropped_frames_go_to_shots_in_order_and_can_be_reassigned(screen):
    at, project = screen
    choose_manual(at)
    ids = [s["id"] for s in read(project / "manifest/shots.json")]
    assert len(ids) >= 4 and any(m.value.startswith("남은 것:") for m in at.markdown)
    uploader = next(f for f in at.file_uploader if f.key == "hand_frame_drop")
    uploader.set_value([("a.png", png(), "image/png"), ("b.png", png(800, 800), "image/png"), ("c.png", b"broken", "image/png")]).run()
    picks = [s for s in at.selectbox if s.key and s.key.startswith("hand_frame_pick_")]
    assert [p.value for p in picks] == ids[:3]
    picks[1].select(ids[3]).run()
    button(at, "선택한 3개 넣기").click().run()
    assert not at.exception
    assert any("a.png" in s.value and ids[0] in s.value for s in at.success)
    assert any("화면비" in w.value for w in at.warning)                      # the square image
    assert any("c.png" in e.value and "읽을 수 없" in e.value for e in at.error)
    kinds = {s["id"]: s.get("storyboard_kind") for s in read(project / "manifest/shots.json")}
    assert kinds[ids[0]] == "imported" and kinds[ids[3]] == "imported" and kinds[ids[1]] != "imported" and kinds[ids[2]] != "imported"
    log = read(project / "render/handoff_log.json")
    assert [(e["kind"], e["target"]) for e in log] == [("frame", ids[0]), ("frame", ids[3])]


def test_the_downloads_folder_is_looked_at_only_when_asked(screen, tmp_path):
    at, project = screen
    choose_manual(at)
    (tmp_path / "Downloads/grok-image.png").write_bytes(png())
    (tmp_path / "Downloads/readme.txt").write_text("x")
    assert not any(m.key == "hand_frame_names" for m in at.multiselect)         # nothing scanned yet
    button_scan = next(b for b in at.button if b.key == "hand_frame_scan")
    button_scan.click().run()
    names = next(m for m in at.multiselect if m.key == "hand_frame_names")
    assert names.options == ["grok-image.png"]
    names.select("grok-image.png").run()
    button(at, "선택한 1개 넣기").click().run()
    assert not at.exception and not at.error
    assert read(project / "manifest/shots.json")[0]["storyboard_kind"] == "imported"
    assert read(project / "render/handoff_log.json")[0]["filename"] == "grok-image.png"
    assert (tmp_path / "Downloads/grok-image.png").exists()                      # the original is left alone


def test_a_dropped_video_becomes_a_draft_take(screen, tmp_path):
    at, project = screen
    choose_manual(at)
    clip = tmp_path / "veo.mp4"
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24:duration=2",
                    "-pix_fmt", "yuv420p", "-y", str(clip)], check=True)
    ids = [s["id"] for s in read(project / "manifest/shots.json") if s["render_mode"] != "STATIC"]
    next(f for f in at.file_uploader if f.key == "hand_video_drop").set_value([("Flow clip.mp4", clip.read_bytes(), "video/mp4")]).run()
    button(at, "선택한 1개 넣기").click().run()
    assert not at.exception and not at.error
    take = read(project / "manifest/assets.json")["shots"][ids[0]]["draft"]
    assert take["review"]["reviewer"] == "" and take["path"].startswith("render/assets/")
    entry = read(project / "render/handoff_log.json")[0]
    assert (entry["kind"], entry["target"], entry["filename"]) == ("video", ids[0], "Flow clip.mp4") and len(entry["sha256"]) == 64
