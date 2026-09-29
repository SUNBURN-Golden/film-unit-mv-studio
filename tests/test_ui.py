"""Drive the real control panel headlessly: pick models with a few clicks, enter keys, run the steps."""
from pathlib import Path
import shutil
import pytest
from fakes import FakeImages, FakeText
from streamlit.testing.v1 import AppTest
from engine import providers, settings
from engine.audio import analyze, synth_test_audio
from engine.core import init_project, read
from engine.production import make_package

ROOT = Path(__file__).resolve().parents[1]
NAME = "zz_ui_test"


@pytest.fixture
def screen(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    for name in ("GROQ_API_KEY", "CLOUDFLARE_API_TOKEN", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    root = ROOT / "projects"
    root.mkdir(exist_ok=True)
    audio = synth_test_audio(tmp_path / "t.wav", seconds=12)
    project = init_project(root, NAME, audio, "UI fixture", synthetic=True, aspect="16:9")
    analyze(project)
    make_package(project)
    try:
        at = AppTest.from_file(str(ROOT / "app/control_panel.py"), default_timeout=90).run()
        at.sidebar.selectbox[0].select(NAME).run()
        assert not at.exception
        yield at, project
    finally:
        shutil.rmtree(project, ignore_errors=True)


def test_each_stage_lists_its_providers_with_cost_and_state(screen):
    at, project = screen
    assert [r.key for r in at.radio if r.key and r.key.startswith("pick_")] == ["pick_text", "pick_image", "pick_video"]
    text = next(r for r in at.radio if r.key == "pick_text")
    assert len(text.options) == len(providers.providers_for("text"))
    assert any("🆓" in o and "Groq" in o and "입력 필요" in o for o in text.options)
    assert any("💰" in o and "Nano Banana" in o for o in next(r for r in at.radio if r.key == "pick_image").options)
    assert any("✋" in o for o in next(r for r in at.radio if r.key == "pick_video").options)


def test_a_model_is_chosen_with_one_click_and_remembered_by_the_project(screen):
    at, project = screen
    next(r for r in at.radio if r.key == "pick_image").set_value("cloudflare_flux").run()
    assert not at.exception and providers.selection(project) == {"image": "cloudflare_flux"}
    assert any("정사각형" in c.value for c in at.caption) or any("정사각형" in w.value for w in at.warning)
    next(r for r in at.radio if r.key == "pick_video").set_value("manual_video").run()
    assert providers.selection(project) == {"image": "cloudflare_flux", "video": "manual_video"}
    reopened = AppTest.from_file(str(ROOT / "app/control_panel.py"), default_timeout=90).run()
    reopened.sidebar.selectbox[0].select(NAME).run()
    assert next(r for r in reopened.radio if r.key == "pick_image").value == "cloudflare_flux"


def test_keys_are_entered_saved_privately_and_never_shown(screen):
    at, project = screen
    next(r for r in at.radio if r.key == "pick_text").set_value("groq").run()
    field = next(t for t in at.text_input if "GROQ_API_KEY" in t.label)
    assert field.value == ""
    field.set_value("gsk-test-token").run()
    next(b for b in at.button if b.label == "저장").click().run()
    assert settings.get_secret("GROQ_API_KEY") == "gsk-test-token" and not at.exception
    assert any("저장했습니다" in s.value for s in at.success)
    assert all("gsk-test-token" not in str(e.value) for e in at.text_input)
    assert next(t for t in at.text_input if "GROQ_API_KEY" in t.label).placeholder.startswith("저장됨")
    assert not any("gsk-test-token" in f.read_text(errors="ignore") for f in project.rglob("*") if f.is_file())
    assert any("연결 정보 입력됨" in o for o in next(r for r in at.radio if r.key == "pick_text").options if "Groq" in o)


def test_connection_test_fills_the_model_list(screen, monkeypatch):
    at, project = screen
    settings.set_secret("GEMINI_API_KEY", "tok")
    at.run()                                       # the list now shows this provider as connected
    monkeypatch.setattr(providers, "check", lambda pid, *a, **k: {"ok": True, "message": "연결되었습니다. 사용할 수 있는 모델 2개", "models": ["g-a", "g-b"]})
    next(r for r in at.radio if r.key == "pick_text").set_value("gemini_text").run()
    next(b for b in at.button if b.label == "연결 테스트").click().run()
    assert any("연결되었습니다" in s.value for s in at.success)
    model = next(s for s in at.selectbox if s.label == "모델")
    assert list(model.options) == ["g-a", "g-b"]
    model.select("g-b").run()
    next(b for b in at.button if b.label == "저장").click().run()
    assert settings.get_settings("gemini_text")["model"] == "g-b"


def test_a_failed_connection_test_says_why(screen, monkeypatch):
    at, project = screen
    monkeypatch.setattr(providers, "check", lambda pid, *a, **k: {"ok": False, "message": "키가 거부되었습니다.", "models": []})
    next(r for r in at.radio if r.key == "pick_image").set_value("cloudflare_flux").run()
    next(b for b in at.button if b.label == "연결 테스트").click().run()
    assert any("거부" in e.value for e in at.error)


def test_project_fit_warning_is_shown_for_an_unsuitable_model(screen):
    at, project = screen
    import engine.core as core
    config = read(project / "project.yaml")
    config["format"].update(aspect_ratio="4:3", width=1440, height=1080)
    core.write(project / "project.yaml", config)
    at.run()
    next(r for r in at.radio if r.key == "pick_video").set_value("gemini_video").run()
    assert any("16:9와 9:16" in w.value for w in at.warning)


def test_autopilot_panel_asks_for_models_then_stops_at_the_first_decision(screen):
    at, project = screen
    next(b for b in at.button if b.label == "다음 단계 실행").click().run()
    assert any("모델을 먼저 고르세요" in m.value for m in at.markdown)
    next(r for r in at.radio if r.key == "pick_image").set_value("manual_image").run()
    next(r for r in at.radio if r.key == "pick_video").set_value("manual_video").run()
    next(b for b in at.button if b.label == "다음 단계 실행").click().run()
    assert not at.exception and any("첫 프레임을 직접 만들어" in m.value for m in at.markdown)
    assert (project / "render/packets/index.html").is_file()


def test_director_panel_asks_for_a_model_first(screen):
    at, project = screen
    assert any("콘티 감독 모델을 먼저 고르세요" in i.value for i in at.info)
    next(r for r in at.radio if r.key == "pick_text").set_value("ollama").run()
    assert any("초안 만들기" == b.label for b in at.button)


def test_director_draft_review_and_accept_from_the_screen(screen, monkeypatch):
    at, project = screen
    monkeypatch.setattr(providers, "selected_text", lambda *a, **k: FakeText())
    next(r for r in at.radio if r.key == "pick_text").set_value("groq").run()
    assert not any(b.label.startswith("검토했고") for b in at.button)
    next(b for b in at.button if b.label == "초안 만들기").click().run()
    assert not at.exception and not at.error
    assert any("초안 검토" in m.value for m in at.markdown)
    assert any("오래된 부엌" in m.value for m in at.markdown)
    assert any("CHAR_A" in str(df.value) for df in at.dataframe)
    before = read(project / "manifest/shots.json")
    next(b for b in at.button if b.label.startswith("검토했고")).click().run()
    assert not at.exception and not at.error
    after = read(project / "manifest/shots.json")
    assert all(a["in_ms"] == b["in_ms"] and a["out_ms"] == b["out_ms"] for a, b in zip(before, after))
    assert all("Mina" in s["description"] for s in after)
    assert read(project / "bible/director_log.json")[0]["reviewer"] == "Director"
    assert not any(b.label.startswith("검토했고") for b in at.button)


def test_paid_images_are_approved_with_one_button_then_generated(screen, monkeypatch):
    at, project = screen
    import engine.core as core
    config = read(project / "project.yaml")
    config["budget"].update(max_usd=5, max_retry_per_shot=1)
    core.write(project / "project.yaml", config)
    fake = FakeImages(unit=0.09)
    monkeypatch.setattr(providers, "build_image", lambda *a, **k: fake)
    next(r for r in at.radio if r.key == "pick_image").set_value("gemini_image").run()
    next(r for r in at.radio if r.key == "pick_video").set_value("mock_video").run()
    next(b for b in at.button if b.label == "다음 단계 실행").click().run()
    assert any("이미지 비용 승인 필요" in m.value for m in at.markdown) and fake.calls == 0
    assert [m.value for m in at.metric if m.label == "재시도 포함 최대"] == ["0.54 USD"]
    next(b for b in at.button if b.label == "이 이미지 비용 승인").click().run()
    next(b for b in at.button if b.label == "다음 단계 실행").click().run()
    assert not at.exception and not at.error and fake.calls == 3
    assert any("첫 프레임 완성" in m.value for m in at.markdown)
