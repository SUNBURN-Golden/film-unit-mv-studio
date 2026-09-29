"""Wan 2.2 Turbo via fal's documented queue. No background submissions."""
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener
from .core import FilmError, digest, object_hash, read, safe_path, write
from .settings import get_secret
from .renderers import AwaitingRender, RenderBlocked, RenderResult, VideoRenderer, build_prompt

ENDPOINT = "fal-ai/wan/v2.2-a14b/image-to-video/turbo"
QUEUE = "https://queue.fal.run/"
PRICING_URL = "https://fal.ai/models/" + ENDPOINT
PRICES = {"480p": 0.05, "580p": 0.075, "720p": 0.10}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def queue_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "queue.fal.run" or not parsed.path.startswith("/fal-ai/wan/"):
        raise RenderBlocked("Unexpected fal queue URL; no credentials sent")
    return url


class FalHTTP:
    def request(self, method, url, payload=None):
        queue_url(url)
        key = get_secret("FAL_KEY")
        if not key:
            raise RenderBlocked("fal API 키가 없습니다. '모델 선택'에서 입력하거나 FAL_KEY를 설정하세요")
        headers = {"Authorization": "Key " + key, "Content-Type": "application/json", "X-Fal-No-Retry": "1"}
        req = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                      headers=headers, method=method)
        # Never follow an authenticated redirect or automatically retry a POST.
        with build_opener(NoRedirect()).open(req, timeout=30) as response:
            return json.load(response)

    def download(self, url, target):
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443} or not (
            host == "fal.media" or host.endswith(".fal.media") or host == "storage.googleapis.com"
        ):
            raise RenderBlocked("Unrecognized result storage host; inspect the provider receipt before importing")
        temp = target.with_suffix(".download")
        try:
            # Result storage receives no API key. Redirects require explicit inspection.
            with build_opener(NoRedirect()).open(url, timeout=30) as source, temp.open("wb") as output:
                size = 0
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > 256 * 1024 * 1024:
                        raise RenderBlocked("Result exceeds the 256 MiB clip download limit")
                    output.write(chunk)
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)


class FalRenderer(VideoRenderer):
    name = "fal"
    billing_unit = "USD"

    def __init__(self, project, fmt, transport=None):
        super().__init__(project, fmt)
        self.config = read(self.project / "render/fal_config.json", {})
        self.transport = transport or FalHTTP()

    def preflight(self):
        if not get_secret("FAL_KEY"):
            raise RenderBlocked("fal is not connected: enter the API key in the model picker or set FAL_KEY")

    def config_hash(self):
        # Renewing unchanged price evidence must not produce another paid run.
        return object_hash({"adapter": 1, "config": {k: v for k, v in self.config.items() if k != "price_valid_until"}})

    def generation_config(self, shot):
        self.quote(shot, self.quality)
        return {"provider": self.name, "adapter": 1, "endpoint": ENDPOINT,
                "resolution": self.config["resolution"]}

    def quote(self, shot, quality):
        config = self.config
        if config.get("endpoint") != ENDPOINT:
            raise FilmError("Configure the verified Wan 2.2 Turbo endpoint in render/fal_config.json")
        resolution = config.get("resolution")
        if resolution not in PRICES or config.get("usd_per_video") != PRICES[resolution]:
            raise FilmError("fal settings differ from the verified price snapshot; refresh the adapter price evidence")
        try:
            expiry = datetime.fromisoformat(config["price_valid_until"])
            valid = expiry > datetime.now(timezone.utc)
        except (KeyError, ValueError, TypeError):
            valid = False
        if not valid:
            raise FilmError("fal price evidence expired; verify the official model page before renewing it")
        if shot.get("renderer", "auto") not in {"auto", "fal", ENDPOINT}:
            raise FilmError("Shot requests another provider; select fal explicitly before LOCK")
        if not 0 < shot["duration_ms"] <= 5000:
            raise FilmError("Wan Turbo adapter supports shots up to 5s; split and review longer shots before LOCK")
        return config["usd_per_video"]

    def input_hash(self, shot, attempt, job_id):
        path = safe_path(self.project, f"render/fal_jobs/{job_id}.json")
        receipt = read(path, {})
        clip = safe_path(self.project, receipt["clip_path"]) if receipt.get("clip_path") else None
        return object_hash({"config": self.config_hash(), "receipt": receipt,
                            "clip": digest(clip) if clip and clip.exists() else None})

    def render(self, shot, target, attempt=0, correction="", job_id=""):
        self.quote(shot, self.quality)
        self.preflight()
        if not job_id:
            raise RenderBlocked("A durable compiler job ID is required")
        path = safe_path(self.project, f"render/fal_jobs/{job_id}.json")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = safe_path(self.project, shot["references"][0])
        prompt = build_prompt(self.project, shot)
        if correction:
            prompt += "\nPrevious attempt failed. Required corrections:\n" + correction
        params = {"prompt": prompt, "resolution": self.config["resolution"], "aspect_ratio": "auto",
                  "enable_safety_checker": True, "enable_output_safety_checker": True,
                  "enable_prompt_expansion": False, "acceleration": "regular",
                  "video_quality": "high", "video_write_mode": "balanced"}
        identity = object_hash({"params": params, "reference": digest(source)})
        receipt = read(path, {})
        if receipt and receipt.get("input_hash") != identity:
            raise RenderBlocked("fal job inputs changed; inspect the existing job before continuing")
        if not receipt:
            from PIL import Image
            with Image.open(source) as im:
                mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(im.format)
            if not mime or source.stat().st_size > 10 * 1024 * 1024:
                raise RenderBlocked("Use a PNG/JPEG/WebP first frame under 10 MiB")
            params["image_url"] = f"data:{mime};base64," + base64.b64encode(source.read_bytes()).decode()
            receipt = {"job_id": job_id, "shot_id": shot["id"], "attempt": attempt,
                       "input_hash": identity, "status": "SUBMITTING", "endpoint": ENDPOINT,
                       "usd_reserved": self.config["usd_per_video"]}
            write(path, receipt)  # Write BEFORE POST, including crash/timeout ambiguity.
            try:
                submitted = self.transport.request("POST", QUEUE + ENDPOINT, params)
                request_id = submitted["request_id"]
                if not isinstance(request_id, str) or not request_id:
                    raise ValueError("Missing request ID")
                receipt.update(request_id=request_id, status_url=submitted["status_url"],
                               response_url=submitted["response_url"], status="SUBMITTED")
                write(path, receipt)  # Preserve returned IDs before further validation.
                queue_url(receipt["status_url"])
                queue_url(receipt["response_url"])
            except Exception as exc:
                raise RenderBlocked("fal submission needs reconciliation; do not resubmit. Check render/fal_jobs and provider history.") from None
        if not receipt.get("request_id"):
            raise RenderBlocked("fal submission status is unknown; reconcile the existing job instead of submitting again")
        if receipt.get("status") == "FAILED":
            raise FilmError("fal confirmed this generation failed; apply the recorded correction on the next attempt")
        if receipt.get("status") != "COMPLETED":
            try:
                status = self.transport.request("GET", receipt["status_url"])
                if status.get("status") != "COMPLETED":
                    raise AwaitingRender(f"fal {job_id}: {status.get('status', 'pending')}; RESUME polls the same request")
                if status.get("error"):
                    receipt.update(status="FAILED", error=str(status["error"]))
                    write(path, receipt)
                    raise FilmError("fal generation failed: " + receipt["error"])
                result = self.transport.request("GET", receipt["response_url"])
                receipt.update(status="COMPLETED", video_url=result["video"]["url"])
                write(path, receipt)
            except (AwaitingRender, FilmError):
                raise
            except Exception:
                raise RenderBlocked("Could not read the fal result; RESUME retrieves the same request without a new submission") from None
        saved = safe_path(self.project, f"render/fal_jobs/{job_id}.mp4")
        if not saved.exists() or receipt.get("sha256") != digest(saved):
            try:
                self.transport.download(receipt["video_url"], saved)
            except Exception:
                raise RenderBlocked("Could not download the fal clip; RESUME downloads the existing result without paying again") from None
            receipt.update(clip_path=str(saved.relative_to(self.project)), sha256=digest(saved))
            write(path, receipt)
        shutil.copyfile(saved, target)
        return RenderResult(target, True, ENDPOINT, job_id)
