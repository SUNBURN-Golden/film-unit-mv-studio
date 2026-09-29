"""Provider registry behind the model picker.

Three stages, each with several interchangeable providers:
  text  - AI director that drafts story, characters, locations and shot directions
  image - character/location reference images and first frames
  video - shot clips (existing renderers)
Adding a provider is a registry entry plus, at most, one small adapter class.
"""
from dataclasses import dataclass
import itertools
import json
from pathlib import Path

from .core import FilmError, project_mutex, read, write
from .safehttp import LOCAL_HOSTS, SafeHTTP, host_of
from .settings import get_secret, get_settings, secret_source

STAGES = {"text": "콘티 감독 (글)", "image": "참조 이미지 · 첫 프레임", "video": "영상"}
COSTS = {"free_tier": "무료 한도", "paid": "유료", "local": "내 컴퓨터 (무료)", "manual": "직접 만들기", "test": "테스트"}


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    secret: bool = False
    default: str = ""
    help: str = ""
    optional: bool = False
    choices: tuple = ()


@dataclass(frozen=True)
class Provider:
    id: str
    stage: str
    label: str
    cost: str
    summary: str
    kind: str
    fields: tuple = ()
    notes: tuple = ()
    docs: str = ""
    base_url: str = ""
    renderer: str = ""
    token_param: str = "max_tokens"


def _key(name, help="", label="API 키"):
    return Field(name, label, secret=True, help=help)


def _model(default="", help="", choices=()):
    return Field("model", "모델", default=default, help=help, choices=choices)


LIMITS = "무료 한도의 수치와 조건은 제공처가 바꿀 수 있습니다. 한도를 넘으면 그날은 실패로 끝나고 요금은 나오지 않습니다."
_ALL = [
    Provider("claude", "text", "Claude (Anthropic)", "paid", "이야기와 연출 문장이 자연스럽습니다. 요금은 사용한 만큼 나옵니다(구독과 별도).", "claude",
             (_key("ANTHROPIC_API_KEY", "console.anthropic.com에서 발급"),
              _model("claude-opus-5-5", choices=("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"))),
             ("곡 하나에 대략 수십 센트 이하로 예상됩니다(모델과 샷 수에 따라 다름).", "설치가 필요합니다: pip install anthropic"),
             "https://docs.anthropic.com"),
    Provider("gemini_text", "text", "Gemini (Google AI Studio)", "free_tier", "글 모델은 무료 한도가 있습니다. 이미지·영상과 같은 키를 씁니다.", "openai_compat",
             (_key("GEMINI_API_KEY", "aistudio.google.com/apikey"), _model("gemini-3.8-flash", "모델 이름은 자주 바뀝니다. 연결 테스트 후 목록에서 고르세요")),
             (LIMITS, "무료 등급을 쓰면 입력 내용이 Google 서비스 개선에 쓰일 수 있습니다(가격표 기준)."),
             "https://ai.google.dev/gemini-api/docs/openai", "https://generativelanguage.googleapis.com/v1beta/openai"),
    Provider("groq", "text", "Groq", "free_tier", "빠른 오픈 모델을 무료 한도 안에서 씁니다.", "openai_compat",
             (_key("GROQ_API_KEY", "console.groq.com"), _model("openai/gpt-oss-120b")),
             (LIMITS,), "https://console.groq.com/docs", "https://api.groq.com/openai/v1"),
    Provider("openrouter", "text", "OpenRouter (무료 모델)", "free_tier", "이름 끝이 :free인 모델은 무료입니다. openrouter/free는 무료 모델 중 하나를 자동 선택합니다.", "openai_compat",
             (_key("OPENROUTER_API_KEY", "openrouter.ai/keys"), _model("openrouter/free")),
             (LIMITS, "무료 모델은 분당·일일 요청 제한이 낮습니다."), "https://openrouter.ai/docs", "https://openrouter.ai/api/v1"),
    Provider("cloudflare_text", "text", "Cloudflare Workers AI", "free_tier", "하루 10,000 뉴런까지 무료입니다. 이미지 모델과 같은 토큰을 씁니다.", "openai_compat",
             (_key("CLOUDFLARE_API_TOKEN", "Workers AI 토큰"), Field("account_id", "Cloudflare 계정 ID"),
              _model("@cf/meta/llama-3.3-70b-instruct-fp8-fast")),
             (LIMITS,), "https://developers.cloudflare.com/workers-ai/",
             "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"),
    Provider("openai", "text", "OpenAI (ChatGPT API)", "paid", "ChatGPT 구독과 별도 과금입니다.", "openai_compat",
             (_key("OPENAI_API_KEY", "platform.openai.com"), _model(help="연결 테스트 후 목록에서 고르세요")),
             (), "https://platform.openai.com/docs", "https://api.openai.com/v1", token_param="max_completion_tokens"),
    Provider("xai", "text", "xAI Grok API", "paid", "SuperGrok 구독에는 API가 포함되지 않습니다. API는 별도 과금입니다.", "openai_compat",
             (_key("XAI_API_KEY", "console.x.ai"), _model(help="연결 테스트 후 목록에서 고르세요")),
             (), "https://docs.x.ai", "https://api.x.ai/v1"),
    Provider("ollama", "text", "Ollama (내 컴퓨터)", "local", "내 컴퓨터에서 도는 모델입니다. 무료이고 내용이 밖으로 나가지 않습니다.", "openai_compat",
             (Field("base_url", "주소", default="http://localhost:11434/v1"), _model(help="ollama list에 나오는 이름")),
             ("Ollama가 실행 중이어야 하고, 성능은 컴퓨터에 따라 다릅니다.",), "https://ollama.com", "{base_url}"),
    Provider("lmstudio", "text", "LM Studio (내 컴퓨터)", "local", "내 컴퓨터에서 도는 모델입니다. 무료입니다.", "openai_compat",
             (Field("base_url", "주소", default="http://localhost:1234/v1"), _model(help="LM Studio에서 불러온 모델 이름")),
             ("LM Studio의 로컬 서버가 켜져 있어야 합니다.",), "https://lmstudio.ai", "{base_url}"),
    Provider("custom_openai", "text", "직접 입력 (OpenAI 호환)", "paid", "OpenAI 호환 주소를 알고 있는 다른 서비스를 씁니다.", "openai_compat",
             (Field("base_url", "주소 (https://…/v1)"), Field("CUSTOM_LLM_API_KEY", "API 키", secret=True, optional=True), _model()),
             (), "", "{base_url}"),
    Provider("manual_text", "text", "직접 붙여넣기 (구독 웹사이트에서)", "manual",
             "ChatGPT·Claude·Gemini·Grok 웹사이트를 내 브라우저에서 직접 열어 지시문을 붙여넣고, 답을 가져옵니다. 키도 자동 로그인도 필요 없습니다.", "manual",
             (), ("구독 요금 안에서 쓸 수 있습니다. 앱은 그 사이트에 로그인하거나 대신 조작하지 않고, 복사·붙여넣기는 사용자가 합니다.",
                  "샷이 많으면 12개씩 나눠 여러 번 붙여넣습니다.")),

    Provider("cloudflare_flux", "image", "FLUX.1 schnell (Cloudflare Workers AI)", "free_tier", "무료로 계속 쓸 수 있는 이미지 모델입니다.", "cloudflare_flux",
             (_key("CLOUDFLARE_API_TOKEN", "Workers AI 토큰"), Field("account_id", "Cloudflare 계정 ID")),
             ("하루 10,000 뉴런까지 무료입니다. 1024px 한 장에 약 58 뉴런이라 하루 약 170장입니다(매일 0시 UTC 초기화).",
              "정사각형만 만들어서, 16:9 프로젝트에서는 위아래를 잘라 씁니다(약 1024×576).",
              "참조 이미지를 받지 않아 인물 일관성이 약합니다. 인물 묘사가 프롬프트에 글로 들어갑니다.",
              "프롬프트는 2,048자까지입니다.", LIMITS), "https://developers.cloudflare.com/workers-ai/models/flux-1-schnell/"),
    Provider("gemini_image", "image", "Nano Banana 2 (Google Gemini)", "paid", "품질이 높고 참조 이미지를 최대 14장까지 받아 인물이 일정합니다.", "gemini_image",
             (_key("GEMINI_API_KEY", "aistudio.google.com/apikey"),),
             ("무료 한도가 없습니다. 1K 이미지 한 장에 약 $0.067입니다(Google 가격표).",
              "Google AI Ultra의 월 개발자 크레딧으로 결제할 수 있습니다."), "https://ai.google.dev/gemini-api/docs/image-generation"),
    Provider("manual_image", "image", "직접 만들기 (앱에서 만들어 가져오기)", "manual", "Gemini 앱·Grok 등 구독 앱에서 직접 만든 이미지를 가져옵니다.", "manual",
             (), ("'구독 앱 작업지시서'가 샷마다 프롬프트를 정리해 줍니다.",)),

    Provider("gemini_video", "video", "Veo 3.1 Lite (Google Gemini)", "paid", "사진 한 장에서 4~8초 영상을 만듭니다.", "renderer",
             (_key("GEMINI_API_KEY", "aistudio.google.com/apikey"),),
             ("무료 한도가 없습니다. 720p는 1초에 $0.05입니다(Google 가격표).", "16:9 또는 9:16 프로젝트만 지원하고 성인 인물만 허용됩니다."),
             "https://ai.google.dev/gemini-api/docs/video", renderer="gemini"),
    Provider("fal_video", "video", "Wan 2.2 Turbo (fal)", "paid", "5초 이하 샷을 만듭니다.", "renderer",
             (_key("FAL_KEY", "fal.ai/dashboard/keys"),), ("가격 근거의 유효 기간이 지나 있으면 갱신이 필요합니다.",),
             "https://fal.ai/models/fal-ai/wan/v2.2-a14b/image-to-video/turbo", renderer="fal"),
    Provider("manual_video", "video", "직접 만들기 (앱에서 만들어 가져오기)", "manual", "Flow(Veo)·Grok Imagine 등 구독 앱에서 직접 만든 영상을 가져옵니다.", "manual",
             (), ("'구독 앱 작업지시서'가 샷마다 프롬프트를 정리해 줍니다.",), renderer="manual"),
    Provider("mock_video", "video", "테스트 (비용 없음)", "test", "정지 화면을 확대·이동해 타이밍만 확인합니다. 완성 영상이 아닙니다.", "renderer", (), (), renderer="mock"),
]
PROVIDERS = {p.id: p for p in _ALL}
del _ALL


def providers_for(stage):
    return [p for p in PROVIDERS.values() if p.stage == stage]


def get(provider_id):
    try:
        return PROVIDERS[provider_id]
    except KeyError:
        raise FilmError(f"Unknown provider: {provider_id}") from None


def values(provider):
    """Field defaults overridden by saved settings. Secrets are read separately."""
    saved = get_settings(provider.id)
    return {f.key: saved.get(f.key) or f.default for f in provider.fields if not f.secret}


def secret_names(provider):
    return [f.key for f in provider.fields if f.secret]


def missing(provider, ignore=()):
    """Labels of required fields that are still empty."""
    current = values(provider)
    out = []
    for f in provider.fields:
        if f.optional or f.key in ignore:
            continue
        if f.secret and not get_secret(f.key):
            out.append(f.label)
        elif not f.secret and not current.get(f.key):
            out.append(f.label)
    return out


def status(provider_id):
    p = get(provider_id)
    lacking = missing(p)
    return {"configured": not lacking, "missing": lacking,
            "keys": {name: secret_source(name) for name in secret_names(p)}}


# --- selection per project ----------------------------------------------------

def selection(project):
    chosen = read(Path(project) / "project.yaml").get("providers", {})
    return chosen if isinstance(chosen, dict) else {}


def select(project, stage, provider_id):
    if stage not in STAGES or get(provider_id).stage != stage:
        raise FilmError("That provider does not belong to this stage")
    p = Path(project)
    with project_mutex(p):
        config = read(p / "project.yaml")
        chosen = config.get("providers") if isinstance(config.get("providers"), dict) else {}
        chosen[stage] = provider_id
        config["providers"] = chosen
        write(p / "project.yaml", config)
    return chosen


def chosen(project, stage):
    provider_id = selection(project).get(stage)
    return get(provider_id) if provider_id in PROVIDERS and PROVIDERS[provider_id].stage == stage else None


# --- error text ---------------------------------------------------------------

def redact(text, provider=None):
    text = str(text)
    for name in (secret_names(provider) if provider else []):
        secret = get_secret(name)
        if secret:
            text = text.replace(secret, "[key]")
    return text


def describe_error(exc, provider=None):
    """One plain sentence for a failed call, with no key and no request body."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    detail = ""
    try:
        body = exc.read() if hasattr(exc, "read") else None
        if body:
            parsed = json.loads(body)
            error = parsed.get("error", parsed.get("errors", ""))
            if isinstance(error, list) and error:
                error = error[0]
            detail = error.get("message", "") if isinstance(error, dict) else str(error)
    except Exception:
        detail = ""
    hint = {401: "키가 거부되었습니다. 키를 다시 확인하세요.", 403: "키에 권한이 없거나 계정에서 막혀 있습니다.",
            404: "주소나 모델 이름이 맞는지 확인하세요.", 402: "결제 수단 또는 잔액이 필요합니다.",
            429: "사용 한도(분당 또는 하루 무료 한도)에 도달했습니다. 잠시 뒤 다시 시도하세요."}.get(code)
    if hint is None:
        hint = f"서비스가 오류를 돌려줬습니다({code})." if code else f"연결하지 못했습니다: {type(exc).__name__}"
    detail = redact(detail, provider)[:200]
    return f"{hint} {detail}".strip()


# --- text adapters ------------------------------------------------------------

class OpenAICompatText:
    """chat/completions on any OpenAI-compatible server."""

    def __init__(self, provider, base_url, model, transport):
        self.provider, self.model, self.http = provider, model, transport
        self.base_url = base_url

    def complete(self, system, user, max_tokens=8000):
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                self.provider.token_param: max_tokens}
        try:
            data = self.http.request("POST", "chat/completions", body, timeout=240)
        except FilmError:
            raise
        except Exception as exc:
            raise FilmError(describe_error(exc, self.provider)) from None
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise FilmError("The model returned an unexpected response") from None
        if choice.get("finish_reason") == "length":
            raise FilmError("응답이 길이 제한으로 잘렸습니다. 샷 수를 줄이거나 출력이 더 긴 모델을 고르세요.")
        if not isinstance(text, str) or not text.strip():
            raise FilmError("The model returned an empty answer")
        return text

    def models(self):
        data = self.http.request("GET", "models", timeout=30)
        ids = [str(m.get("id", "")) for m in data.get("data", []) if isinstance(m, dict)]
        return sorted({i.removeprefix("models/") for i in ids if i})


CLAUDE_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-1"}


class ClaudeText:
    """Claude through the official SDK. No automatic retries; refusals fall back server-side."""

    def __init__(self, provider, model, client=None):
        self.provider, self.model, self._client = provider, model, client

    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise FilmError("Claude를 쓰려면 설치가 필요합니다: pip install anthropic") from None
            key = get_secret("ANTHROPIC_API_KEY")
            if not key:
                raise FilmError("Anthropic API 키를 입력하세요")
            self._client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=240.0)
        return self._client

    def complete(self, system, user, max_tokens=16000):
        options = {}
        if self.model in CLAUDE_FALLBACK_MODELS:
            # A safety decline re-runs once on a fallback model instead of failing the whole draft.
            options.update(betas=["server-side-fallback-2026-07-01"], extra_body={"fallbacks": "default"},
                           output_config={"effort": "medium"})
        try:
            response = self.client().beta.messages.create(
                model=self.model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}], **options)
        except FilmError:
            raise
        except Exception as exc:
            raise FilmError(describe_error(exc, self.provider)) from None
        if response.stop_reason == "refusal":
            raise FilmError("Claude가 이 요청을 처리하지 않았습니다. 설명을 바꾸거나 다른 모델을 고르세요.")
        if response.stop_reason == "max_tokens":
            raise FilmError("응답이 길이 제한으로 잘렸습니다. 샷 수를 줄여 다시 시도하세요.")
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise FilmError("The model returned an empty answer")
        return text

    def models(self):
        return [m.id for m in itertools.islice(self.client().models.list(), 50)]


def build_text(provider_id, transport=None, client=None, require_model=True):
    p = get(provider_id)
    if p.stage != "text":
        raise FilmError("Not a text provider")
    if p.kind == "manual":
        raise FilmError("직접 붙여넣기 방식입니다. 'AI 감독' 화면의 안내에 따라 지시문을 복사하고 답을 붙여넣으세요.")
    lacking = missing(p, ignore=() if require_model else ("model",))
    if lacking:
        raise FilmError("먼저 입력하세요: " + ", ".join(lacking))
    current = values(p)
    if p.kind == "claude":
        return ClaudeText(p, current["model"], client)
    base = p.base_url.format(**current).rstrip("/")
    if p.id == "cloudflare_text" and not current["account_id"].isalnum():
        raise FilmError("Cloudflare 계정 ID는 영문과 숫자만 들어갑니다")
    key_name = next((name for name in secret_names(p)), None)

    def headers():
        key = get_secret(key_name) if key_name else None
        return {"Authorization": f"Bearer {key}"} if key else {}

    http = transport or SafeHTTP(base, headers, allow_local_http=host_of(base) in LOCAL_HOSTS)
    return OpenAICompatText(p, base, current["model"], http)


def check(provider_id, transport=None, client=None):
    """A no-cost connection test where the service offers one. Never raises."""
    try:
        p = get(provider_id)
        # Listing models needs no model name, so a text provider can be tested before one is chosen.
        lacking = missing(p, ignore=("model",) if p.stage == "text" else ())
        if lacking:
            return {"ok": False, "message": "먼저 입력하세요: " + ", ".join(lacking), "models": []}
        if p.stage == "text":
            adapter = build_text(provider_id, transport, client, require_model=False)
            models = adapter.models()
            return {"ok": True, "message": f"연결되었습니다. 사용할 수 있는 모델 {len(models)}개", "models": models}
        if p.kind == "gemini_image" or p.renderer == "gemini":
            from .gemini import GeminiHTTP
            GeminiHTTP().request("GET", "https://generativelanguage.googleapis.com/v1beta/models?pageSize=5")
            return {"ok": True, "message": "연결되었습니다.", "models": []}
        if p.kind == "cloudflare_flux":
            image = build_image(provider_id, transport)
            image.generate("a small red circle on a white background", [], "1:1")
            return {"ok": True, "message": "연결되었습니다. 시험 이미지 1장에 하루 무료 한도의 약 0.6%를 썼습니다.", "models": []}
        return {"ok": True, "message": "키가 입력되었습니다. 이 제공처는 무료 연결 테스트가 없어, 첫 생성 때 확인됩니다.", "models": []}
    except FilmError as exc:
        return {"ok": False, "message": str(exc), "models": []}
    except Exception as exc:
        return {"ok": False, "message": describe_error(exc, PROVIDERS.get(provider_id)), "models": []}


def build_image(provider_id, transport=None):
    p = get(provider_id)
    if p.stage != "image":
        raise FilmError("Not an image provider")
    if p.kind == "manual":
        raise FilmError("이 제공처는 직접 만들기입니다. '구독 앱 작업지시서'를 쓰세요.")
    lacking = missing(p)
    if lacking:
        raise FilmError("먼저 입력하세요: " + ", ".join(lacking))
    if p.kind == "gemini_image":
        from .gemini import GeminiImage
        return GeminiImage(p, transport)
    from .cloudflare import CloudflareFlux
    return CloudflareFlux(p, values(p)["account_id"], transport)


def selected_text(project, transport=None, client=None):
    choice = chosen(project, "text")
    if choice is None:
        raise FilmError("콘티 감독 모델을 먼저 고르세요 ('모델 선택' 탭 또는 use --stage text)")
    return build_text(choice.id, transport, client)


def selected_image(project, transport=None):
    choice = chosen(project, "image")
    if choice is None:
        raise FilmError("이미지 모델을 먼저 고르세요 ('모델 선택' 탭 또는 use --stage image)")
    return build_image(choice.id, transport)


def fit_warnings(project, provider_id):
    """Reasons this provider does not suit this project, in plain words."""
    p, out = get(provider_id), []
    try:
        aspect = read(Path(project) / "project.yaml")["format"].get("aspect_ratio", "")
    except (FilmError, KeyError, OSError):
        return out
    if p.id == "gemini_video" and aspect not in {"16:9", "9:16"}:
        out.append(f"Veo는 16:9와 9:16만 지원합니다. 이 프로젝트는 {aspect}입니다.")
    if p.id == "fal_video" and aspect not in {"16:9", "9:16", "auto"} and aspect:
        out.append("이 모델은 화면비를 입력 이미지에 맞춥니다. 결과가 프로젝트 비율과 다르면 여백이 생길 수 있습니다.")
    if p.id == "cloudflare_flux" and aspect != "1:1":
        out.append(f"이 모델은 정사각형으로 만들어 {aspect}에 맞게 잘라 씁니다. 해상도가 낮아집니다.")
    return out
