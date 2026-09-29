"""Model-picker tests: no network, no real key, no spend."""
import base64
import io
import json
import os
import stat
from urllib.error import HTTPError
from PIL import Image
import pytest
from engine import providers, settings
from engine.cloudflare import CloudflareFlux
from engine.core import FilmError, read, write
from engine.imagegen import ImageRejected
from engine.safehttp import SafeHTTP, check_url


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "CLOUDFLARE_API_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "FAL_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "home"


def png_b64(size=(32, 32)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "teal").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


class FakeHTTP:
    def __init__(self, replies=None):
        self.calls, self.replies = [], replies or {}

    def request(self, method, path, payload=None, timeout=60):
        self.calls.append((method, path, payload))
        reply = self.replies.get(path)
        if isinstance(reply, Exception):
            raise reply
        return reply if reply is not None else {}


def http_error(code, body=b'{"error": {"message": "nope"}}'):
    return HTTPError("https://x", code, "err", {}, io.BytesIO(body))


# --- key store -------------------------------------------------------------------

def test_saved_key_is_private_and_environment_wins(home, monkeypatch):
    settings.set_secret("GROQ_API_KEY", "  saved-token  ")
    assert settings.get_secret("GROQ_API_KEY") == "saved-token"
    assert settings.secret_source("GROQ_API_KEY") == "saved"
    assert stat.S_IMODE(os.stat(home / "secrets.json").st_mode) == 0o600
    monkeypatch.setenv("GROQ_API_KEY", "from-env")
    assert settings.get_secret("GROQ_API_KEY") == "from-env" and settings.secret_source("GROQ_API_KEY") == "env"
    monkeypatch.delenv("GROQ_API_KEY")
    settings.delete_secret("GROQ_API_KEY")
    assert settings.get_secret("GROQ_API_KEY") is None


def test_key_and_setting_validation(home):
    for bad in ("two words", "line\nbreak", "", "x" * 600):
        with pytest.raises(FilmError):
            settings.set_secret("GROQ_API_KEY", bad)
    with pytest.raises(FilmError):
        settings.set_secret("lower_case", "value")
    settings.set_settings("groq", {"model": "m1", "empty": " "})
    assert settings.get_settings("groq") == {"model": "m1"}
    with pytest.raises(FilmError):
        settings.set_settings("Bad Id", {})
    with pytest.raises(FilmError):
        settings.set_settings("groq", {"model": "a\nb"})


def test_keys_never_enter_a_project_folder(home, tmp_path):
    settings.set_secret("GEMINI_API_KEY", "secret-value")
    assert "secret-value" not in json.dumps(read(home / "providers.json", {}))
    assert not any("secret-value" in f.read_text(errors="ignore") for f in tmp_path.rglob("*")
                   if f.is_file() and f.name != "secrets.json")


# --- registry ----------------------------------------------------------------------

def test_registry_covers_all_stages_with_labelled_costs():
    for stage in providers.STAGES:
        assert providers.providers_for(stage)
    for p in providers.PROVIDERS.values():
        assert p.cost in providers.COSTS and p.summary
    free_text = {p.id for p in providers.providers_for("text") if p.cost == "free_tier"}
    assert {"gemini_text", "groq", "openrouter", "cloudflare_text"} <= free_text
    assert providers.get("cloudflare_flux").cost == "free_tier" and providers.get("gemini_image").cost == "paid"
    with pytest.raises(FilmError):
        providers.get("nope")


def test_status_tracks_missing_fields_and_sources():
    assert providers.status("cloudflare_flux")["missing"] == ["API 키", "Cloudflare 계정 ID"]
    settings.set_secret("CLOUDFLARE_API_TOKEN", "tok")
    settings.set_settings("cloudflare_flux", {"account_id": "abcdef1234567890"})
    state = providers.status("cloudflare_flux")
    assert state["configured"] and state["keys"] == {"CLOUDFLARE_API_TOKEN": "saved"}
    # The same token also configures the Cloudflare text model once its model is set.
    assert providers.status("cloudflare_text")["missing"] == ["Cloudflare 계정 ID"]
    assert providers.status("manual_image")["configured"] and providers.status("mock_video")["configured"]


def test_selection_is_saved_per_project_and_stage_checked(tmp_path):
    write(tmp_path / "project.yaml", {"name": "x"})
    providers.select(tmp_path, "image", "cloudflare_flux")
    providers.select(tmp_path, "video", "manual_video")
    assert providers.selection(tmp_path) == {"image": "cloudflare_flux", "video": "manual_video"}
    assert providers.chosen(tmp_path, "image").id == "cloudflare_flux" and providers.chosen(tmp_path, "text") is None
    with pytest.raises(FilmError):
        providers.select(tmp_path, "text", "cloudflare_flux")
    assert read(tmp_path / "project.yaml")["name"] == "x"


# --- transport -----------------------------------------------------------------------

def test_transport_only_talks_to_its_own_host(monkeypatch):
    http = SafeHTTP("https://api.example.com/v1", {"Authorization": "Bearer k"})
    with pytest.raises(FilmError):
        http.request("GET", "https://evil.test/v1/models")
    with pytest.raises(FilmError):
        check_url("https://user:pw@api.example.com/x", "api.example.com")
    with pytest.raises(FilmError):
        SafeHTTP("http://api.example.com/v1")            # plain http only for this computer
    assert SafeHTTP("http://localhost:11434/v1", allow_local_http=True).host == "localhost"
    with pytest.raises(FilmError):
        SafeHTTP("http://192.168.1.5:11434/v1", allow_local_http=True)


# --- text adapters -----------------------------------------------------------------------

def test_openai_compatible_text_request_and_models():
    settings.set_secret("GROQ_API_KEY", "tok")
    http = FakeHTTP({"chat/completions": {"choices": [{"message": {"content": '{"ok": 1}'}, "finish_reason": "stop"}]},
                     "models": {"data": [{"id": "b"}, {"id": "models/a"}, {"id": "b"}]}})
    text = providers.build_text("groq", transport=http)
    assert text.complete("sys", "user", 500) == '{"ok": 1}'
    method, path, body = http.calls[0]
    assert (method, path) == ("POST", "chat/completions")
    assert body["model"] == "openai/gpt-oss-120b" and body["max_tokens"] == 500
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}]
    assert text.models() == ["a", "b"]


def test_openai_uses_max_completion_tokens_and_needs_a_model():
    settings.set_secret("OPENAI_API_KEY", "tok")
    with pytest.raises(FilmError, match="모델"):
        providers.build_text("openai", transport=FakeHTTP())
    settings.set_settings("openai", {"model": "some-model"})
    http = FakeHTTP({"chat/completions": {"choices": [{"message": {"content": "x"}}]}})
    providers.build_text("openai", transport=http).complete("s", "u", 100)
    assert "max_completion_tokens" in http.calls[0][2] and "max_tokens" not in http.calls[0][2]


def test_truncated_empty_and_failed_answers_are_explained():
    settings.set_secret("GROQ_API_KEY", "tok-secret")
    text = providers.build_text("groq", transport=FakeHTTP({"chat/completions": {"choices": [{"message": {"content": "{"}, "finish_reason": "length"}]}}))
    with pytest.raises(FilmError, match="잘렸"):
        text.complete("s", "u")
    text = providers.build_text("groq", transport=FakeHTTP({"chat/completions": {"choices": [{"message": {"content": " "}}]}}))
    with pytest.raises(FilmError, match="empty"):
        text.complete("s", "u")
    text = providers.build_text("groq", transport=FakeHTTP({"chat/completions": http_error(429)}))
    with pytest.raises(FilmError, match="한도"):
        text.complete("s", "u")
    text = providers.build_text("groq", transport=FakeHTTP({"chat/completions": http_error(401, b'{"error": {"message": "bad tok-secret"}}')}))
    with pytest.raises(FilmError) as caught:
        text.complete("s", "u")
    assert "거부" in str(caught.value) and "tok-secret" not in str(caught.value)


def test_local_and_custom_endpoints_use_the_configured_address():
    settings.set_settings("ollama", {"model": "llama"})
    text = providers.build_text("ollama")
    assert text.base_url == "http://localhost:11434/v1"
    settings.set_settings("custom_openai", {"base_url": "https://api.example.com/v1", "model": "m"})
    assert providers.build_text("custom_openai").base_url == "https://api.example.com/v1"
    settings.set_settings("custom_openai", {"base_url": "http://192.168.0.9/v1", "model": "m"})
    with pytest.raises(FilmError):
        providers.build_text("custom_openai")


def test_cloudflare_text_needs_a_clean_account_id():
    settings.set_secret("CLOUDFLARE_API_TOKEN", "tok")
    settings.set_settings("cloudflare_text", {"account_id": "abc/../x"})
    with pytest.raises(FilmError):
        providers.build_text("cloudflare_text")
    settings.set_settings("cloudflare_text", {"account_id": "abc123def4567890"})
    assert providers.build_text("cloudflare_text").base_url.endswith("/accounts/abc123def4567890/ai/v1")


class FakeClaude:
    class Messages:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            self.outer.calls.append(kwargs)
            if self.outer.error:
                raise self.outer.error
            return self.outer.reply

    class Models:
        def list(self):
            return [type("M", (), {"id": "claude-opus-5-5"})(), type("M", (), {"id": "claude-haiku-4-5"})()]

    def __init__(self, stop="end_turn", text="{}", error=None):
        self.calls, self.error = [], error
        block = type("B", (), {"type": "text", "text": text})()
        self.reply = type("R", (), {"stop_reason": stop, "content": [block]})()
        self.beta = type("Beta", (), {"messages": FakeClaude.Messages(self)})()
        self.models = FakeClaude.Models()


def test_claude_uses_the_sdk_without_retries_and_with_fallback():
    settings.set_secret("ANTHROPIC_API_KEY", "tok")
    fake = FakeClaude(text='{"a": 1}')
    text = providers.build_text("claude", client=fake)
    assert text.complete("sys", "hello", 2000) == '{"a": 1}'
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["system"] == "sys" and call["max_tokens"] == 2000
    assert call["betas"] == ["server-side-fallback-2026-07-01"] and call["extra_body"] == {"fallbacks": "default"}
    assert call["output_config"] == {"effort": "medium"} and "thinking" not in call and "temperature" not in call
    settings.set_settings("claude", {"model": "claude-haiku-4-5"})
    fake = FakeClaude()
    providers.build_text("claude", client=fake).complete("s", "u")
    assert "betas" not in fake.calls[0] and "output_config" not in fake.calls[0]
    assert providers.build_text("claude", client=fake).models() == ["claude-opus-5-5", "claude-haiku-4-5"]


def test_claude_refusal_truncation_and_errors():
    settings.set_secret("ANTHROPIC_API_KEY", "tok")
    with pytest.raises(FilmError, match="처리하지"):
        providers.build_text("claude", client=FakeClaude(stop="refusal")).complete("s", "u")
    with pytest.raises(FilmError, match="잘렸"):
        providers.build_text("claude", client=FakeClaude(stop="max_tokens")).complete("s", "u")
    limited = type("RateLimit", (Exception,), {"status_code": 429})()
    with pytest.raises(FilmError, match="한도"):
        providers.build_text("claude", client=FakeClaude(error=limited)).complete("s", "u")


def test_connection_check_lists_models_before_a_model_is_chosen():
    settings.set_secret("GEMINI_API_KEY", "tok")
    result = providers.check("gemini_text", transport=FakeHTTP({"models": {"data": [{"id": "models/g1"}, {"id": "models/g2"}]}}))
    assert result["ok"] and result["models"] == ["g1", "g2"]
    settings.delete_secret("GEMINI_API_KEY")
    assert not providers.check("gemini_text", transport=FakeHTTP())["ok"]
    settings.set_secret("GEMINI_API_KEY", "tok")
    bad = providers.check("gemini_text", transport=FakeHTTP({"models": http_error(401)}))
    assert not bad["ok"] and "거부" in bad["message"]
    assert providers.check("manual_image")["ok"]


# --- Cloudflare image -------------------------------------------------------------------------

def test_cloudflare_flux_request_and_response():
    http = FakeHTTP()
    flux = CloudflareFlux(providers.get("cloudflare_flux"), "abc123def4567890", http)
    path = "accounts/abc123def4567890/ai/run/@cf/black-forest-labs/flux-1-schnell"
    http.replies[path] = {"success": True, "result": {"image": png_b64()}}
    data = flux.generate("x" * 3000, [], "16:9")
    assert Image.open(io.BytesIO(data)).size == (32, 32)
    _, called, body = http.calls[0]
    assert called == path and body == {"prompt": "x" * 2048, "steps": 4}
    assert flux.unit_usd == 0 and not flux.supports_references and flux.prompt_limit == 2048
    http.replies[path] = {"success": False, "errors": [{"message": "no quota"}], "result": None}
    with pytest.raises(ImageRejected, match="no quota"):
        flux.generate("a", [], "1:1")
    http.replies[path] = http_error(429)
    with pytest.raises(ImageRejected, match="한도"):
        flux.generate("a", [], "1:1")
    http.replies[path] = http_error(503)
    with pytest.raises(HTTPError):
        flux.generate("a", [], "1:1")
    with pytest.raises(ImageRejected):
        CloudflareFlux(providers.get("cloudflare_flux"), "../bad", http)


def test_cloudflare_connection_test_uses_one_free_image():
    settings.set_secret("CLOUDFLARE_API_TOKEN", "tok")
    settings.set_settings("cloudflare_flux", {"account_id": "abc123def4567890"})
    http = FakeHTTP({"accounts/abc123def4567890/ai/run/@cf/black-forest-labs/flux-1-schnell": {"success": True, "result": {"image": png_b64()}}})
    result = providers.check("cloudflare_flux", transport=http)
    assert result["ok"] and len(http.calls) == 1 and "0.6%" in result["message"]
