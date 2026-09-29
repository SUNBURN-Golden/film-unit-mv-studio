"""Cloudflare Workers AI: FLUX.1 schnell text-to-image, free up to 10,000 neurons a day."""
import base64
import re
from urllib.error import HTTPError

from .imagegen import ImageProvider, ImageRejected
from .providers import describe_error
from .safehttp import SafeHTTP
from .settings import get_secret

API = "https://api.cloudflare.com/client/v4"
MODEL = "@cf/black-forest-labs/flux-1-schnell"
ACCOUNT = re.compile(r"[A-Za-z0-9]{8,64}")


class CloudflareFlux(ImageProvider):
    id = "cloudflare_flux"
    model = MODEL
    unit_usd = 0.0
    supports_references = False   # schnell takes only a prompt
    prompt_limit = 2048
    native_aspects = None         # 1024x1024 only; the pipeline crops to the project ratio

    def __init__(self, provider, account_id, transport=None):
        if not ACCOUNT.fullmatch(account_id or ""):
            raise ImageRejected("Cloudflare 계정 ID를 확인하세요 (영문과 숫자만)")
        self.provider, self.account_id = provider, account_id
        self.http = transport or SafeHTTP(API, lambda: {"Authorization": f"Bearer {get_secret('CLOUDFLARE_API_TOKEN')}"})

    def generate(self, prompt, references, aspect):
        try:
            data = self.http.request("POST", f"accounts/{self.account_id}/ai/run/{MODEL}",
                                     {"prompt": prompt[:self.prompt_limit], "steps": 4}, timeout=120)
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                raise ImageRejected(describe_error(exc, self.provider)) from None
            raise
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(data, dict) or not data.get("success") or not isinstance(result, dict) or not result.get("image"):
            errors = data.get("errors") if isinstance(data, dict) else None
            message = errors[0].get("message", "") if errors and isinstance(errors[0], dict) else ""
            raise ImageRejected(f"Cloudflare가 이미지를 돌려주지 않았습니다 {message}"[:240])
        return base64.b64decode(result["image"], validate=True)
