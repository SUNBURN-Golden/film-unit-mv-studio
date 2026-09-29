"""Gemini API adapters: Nano Banana first frames and Veo 3.1 Lite image-to-video.

Paid calls keep the fal guards: an explicitly approved estimate, a durable job
record written before each POST, no resubmission of an ambiguous request and no
background worker. GEMINI_API_KEY stays in the local environment and is sent
only to the Gemini API host.
"""
import base64
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, build_opener

from .core import FilmError, digest, now, object_hash, read, safe_path, write
from .fal_renderer import NoRedirect
from .imagegen import ImageProvider, ImageRejected
from .packets import MAX_REFERENCES, aspect_ratio
from .renderers import AwaitingRender, RenderBlocked, RenderResult, VideoRenderer
from .settings import get_secret

API = "https://generativelanguage.googleapis.com/v1beta/"
API_HOST = "generativelanguage.googleapis.com"
FRAME_MODEL = "gemini-3.1-flash-image"
VIDEO_MODEL = "veo-3.1-lite-generate-preview"
PRICE_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
# Verified 2026-09-29 on PRICE_SOURCE. Veo bills per generated second, audio included.
VIDEO_USD_PER_SECOND = {"720p": 0.05, "1080p": 0.08}
VIDEO_SECONDS = (4, 6, 8)
VIDEO_ASPECTS = {"16:9", "9:16"}
# Conservative bound per 1K frame: $0.067 image output plus prompt, up to 14
# reference images and thinking tokens. Actual charges are expected to be lower.
FRAME_USD = 0.09
FRAME_SIZE = "1K"
FRAME_ASPECTS = {"1:1", "3:2", "2:3", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}
# Written only when a project has no Gemini settings. Renew price_valid_until
# after checking PRICE_SOURCE; changed prices must be updated in this module.
DEFAULT_CONFIG = {"video": {"model": VIDEO_MODEL, "resolution": "720p", "usd_per_second": 0.05},
                  "frame": {"model": FRAME_MODEL, "image_size": FRAME_SIZE, "usd_per_frame": FRAME_USD},
                  "price_source": PRICE_SOURCE, "price_valid_until": "2026-10-31T23:59:59+00:00"}
OPERATION = re.compile(r"models/[A-Za-z0-9.-]+/operations/[A-Za-z0-9_-]+")
MAX_DOWNLOAD = 256 * 1024 * 1024
MAX_INLINE_BYTES = 14 * 1024 * 1024
REDIRECTS = {301, 302, 303, 307, 308}


def api_url(url):
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname != API_HOST or parsed.username or parsed.password
            or parsed.port not in {None, 443} or not parsed.path.startswith("/v1beta/")):
        raise RenderBlocked("Unexpected Gemini API URL; no credentials sent")
    return url


def storage_url(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}
            or not (host.endswith(".googleusercontent.com") or host == "storage.googleapis.com")):
        raise RenderBlocked("Unrecognized result storage host; inspect the Gemini receipt before importing")
    return url


class GeminiHTTP:
    """No retries and no authenticated redirects; storage redirects receive no key."""

    def key(self):
        key = get_secret("GEMINI_API_KEY")
        if not key:
            raise RenderBlocked("Gemini API 키가 없습니다. '모델 선택'에서 입력하거나 GEMINI_API_KEY를 설정하세요")
        return key

    def request(self, method, url, payload=None, timeout=60):
        api_url(url)
        headers = {"x-goog-api-key": self.key(), "Content-Type": "application/json"}
        req = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                      headers=headers, method=method)
        with build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            return json.load(response)

    def download(self, url, target):
        api_url(url)
        opener = build_opener(NoRedirect())
        try:
            source = opener.open(Request(url, headers={"x-goog-api-key": self.key()}), timeout=60)
        except HTTPError as exc:
            if exc.code not in REDIRECTS:
                raise
            source = opener.open(storage_url(exc.headers.get("Location", "")), timeout=60)
        temp = Path(target).with_suffix(".download")
        try:
            with source, temp.open("wb") as output:
                size = 0
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise RenderBlocked("Result exceeds the 256 MiB clip download limit")
                    output.write(chunk)
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)


def api_error(exc):
    try:
        return str(json.loads(exc.read()).get("error", {}).get("message", ""))[:300]
    except Exception:
        return ""


def image_part(path):
    from PIL import Image
    with Image.open(path) as im:
        mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(im.format)
    if not mime or path.stat().st_size > 10 * 1024 * 1024:
        raise RenderBlocked(f"Use PNG/JPEG/WebP images under 10 MiB: {path.name}")
    return mime, base64.b64encode(path.read_bytes()).decode()


def project_aspect(project):
    return aspect_ratio(read(Path(project) / "project.yaml")["format"])


def checked_config(project):
    """Price evidence shared by frames and video; renewing it is a human check."""
    config = read(Path(project) / "render/gemini_config.json", {})
    video, frame = config.get("video", {}), config.get("frame", {})
    if video.get("model") != VIDEO_MODEL or frame.get("model") != FRAME_MODEL or frame.get("image_size") != FRAME_SIZE:
        raise FilmError("Configure the verified Gemini models in render/gemini_config.json (templates/gemini_veo_lite.json)")
    resolution = video.get("resolution")
    if (resolution not in VIDEO_USD_PER_SECOND or video.get("usd_per_second") != VIDEO_USD_PER_SECOND[resolution]
            or frame.get("usd_per_frame") != FRAME_USD):
        raise FilmError("Gemini settings differ from the verified price snapshot; refresh the adapter price evidence")
    try:
        valid = datetime.fromisoformat(config["price_valid_until"]) > datetime.now(timezone.utc)
    except (KeyError, ValueError, TypeError):
        valid = False
    if not valid:
        raise FilmError(f"Gemini price evidence expired; verify {PRICE_SOURCE} before renewing price_valid_until")
    return config


# --- First frames (Nano Banana 2) -------------------------------------------

def output_image(interaction):
    if not isinstance(interaction, dict) or interaction.get("status") != "completed":
        return None
    blocks = [b for step in interaction.get("steps") or [] if step.get("type") == "model_output"
              for b in step.get("content") or []] + list(interaction.get("outputs") or [])
    return next((b for b in reversed(blocks) if b.get("type") == "image" and b.get("data")), None)


class GeminiImage(ImageProvider):
    id = "gemini_image"
    model = FRAME_MODEL
    unit_usd = FRAME_USD
    supports_references = True
    max_references = MAX_REFERENCES
    prompt_limit = None
    native_aspects = FRAME_ASPECTS

    def __init__(self, provider=None, transport=None):
        self.provider = provider
        self.transport = transport or GeminiHTTP()

    def preflight(self, project):
        if not (Path(project) / "render/gemini_config.json").exists():
            write(Path(project) / "render/gemini_config.json", DEFAULT_CONFIG)
        checked_config(project)

    def generate(self, prompt, references, aspect):
        parts = [{"type": "text", "text": prompt}]
        for reference in references:
            mime, data = image_part(reference)
            parts.append({"type": "image", "mime_type": mime, "data": data})
        body = {"model": FRAME_MODEL, "input": parts,
                "response_format": {"type": "image", "aspect_ratio": aspect, "image_size": FRAME_SIZE}}
        try:
            interaction = self.transport.request("POST", API + "interactions", body, timeout=180)
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                raise ImageRejected(f"Gemini rejected the request ({exc.code}) {api_error(exc)}") from None
            raise
        block = output_image(interaction)
        if block is None:
            raise ImageRejected(f"Gemini returned no image (status {(interaction or {}).get('status')})")
        return base64.b64decode(block["data"], validate=True)


# --- Video (Veo 3.1 Lite image-to-video) --------------------------------------

def video_prompt(project, shot):
    """Motion-only prompt: Veo accepts 1,024 prompt tokens, and the approved first
    frame already carries composition, style and identity."""
    rules = read(Path(project) / "bible/style_bible.yaml", {}).get("rules", [])
    return "\n".join([
        "Animate this approved first frame. Preserve its exact composition, style and character identities.",
        shot["description"], f"Motion: {shot.get('motion', {}).get('instruction', '')}",
        f"Camera: {shot['camera']}", *(["Style rules: " + "; ".join(map(str, rules))] if rules else []),
        "No new text. Do not introduce new scenes or camera movement.",
    ])


class GeminiVideoRenderer(VideoRenderer):
    name = "gemini"
    billing_unit = "USD"

    def __init__(self, project, fmt, transport=None):
        super().__init__(project, fmt)
        self.config = read(self.project / "render/gemini_config.json", {})
        self.transport = transport or GeminiHTTP()

    def preflight(self):
        if not get_secret("GEMINI_API_KEY"):
            raise RenderBlocked("Gemini is not connected: enter the API key in the model picker or set GEMINI_API_KEY")

    def config_hash(self):
        # Renewing unchanged price evidence must not produce another paid run.
        return object_hash({"adapter": 1, "config": {k: v for k, v in self.config.items() if k != "price_valid_until"}})

    def settings(self, shot):
        video = checked_config(self.project)["video"]
        if shot.get("renderer", "auto") not in {"auto", "gemini", VIDEO_MODEL}:
            raise FilmError("Shot requests another provider; select gemini explicitly before LOCK")
        aspect = project_aspect(self.project)
        if aspect not in VIDEO_ASPECTS:
            raise FilmError(f"Veo generates 16:9 or 9:16 only; this project is {aspect}")
        # 1080p is 8s only; otherwise the shortest clip that covers the shot.
        choices = (8,) if video["resolution"] == "1080p" else VIDEO_SECONDS
        seconds = next((s for s in choices if shot["duration_ms"] <= s * 1000), None)
        if seconds is None:
            raise FilmError("Veo clips are at most 8s; split longer shots and review them before LOCK")
        return {"aspectRatio": aspect, "durationSeconds": seconds, "resolution": video["resolution"],
                "personGeneration": "allow_adult"}, video["usd_per_second"]

    def quote(self, shot, quality):
        params, rate = self.settings(shot)
        return round(params["durationSeconds"] * rate, 6)

    def generation_config(self, shot):
        params, _ = self.settings(shot)
        return {"provider": self.name, "adapter": 1, "model": VIDEO_MODEL, "parameters": params}

    def input_hash(self, shot, attempt, job_id):
        receipt = read(safe_path(self.project, f"render/gemini_jobs/{job_id}.json"), {})
        clip = safe_path(self.project, receipt["clip_path"]) if receipt.get("clip_path") else None
        return object_hash({"config": self.config_hash(), "receipt": receipt,
                            "clip": digest(clip) if clip and clip.exists() else None})

    def render(self, shot, target, attempt=0, correction="", job_id=""):
        params, rate = self.settings(shot)
        self.preflight()
        if not job_id:
            raise RenderBlocked("A durable compiler job ID is required")
        path = safe_path(self.project, f"render/gemini_jobs/{job_id}.json")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = safe_path(self.project, shot["references"][0])
        prompt = video_prompt(self.project, shot)
        if correction:
            prompt += "\nPrevious attempt failed. Required corrections:\n" + correction
        identity = object_hash({"model": VIDEO_MODEL, "prompt": prompt, "parameters": params, "reference": digest(source)})
        receipt = read(path, {})
        if receipt and receipt.get("input_hash") != identity:
            raise RenderBlocked("Gemini job inputs changed; inspect the existing job before continuing")
        if not receipt:
            mime, data = image_part(source)
            receipt = {"job_id": job_id, "shot_id": shot["id"], "attempt": attempt, "model": VIDEO_MODEL,
                       "input_hash": identity, "status": "SUBMITTING",
                       "usd_reserved": round(params["durationSeconds"] * rate, 6), "submitted_at": now()}
            write(path, receipt)  # Written BEFORE POST, including timeout ambiguity.
            body = {"instances": [{"prompt": prompt, "image": {"inlineData": {"mimeType": mime, "data": data}}}],
                    "parameters": params}
            try:
                operation = self.transport.request("POST", f"{API}models/{VIDEO_MODEL}:predictLongRunning", body)
            except HTTPError as exc:
                if 400 <= exc.code < 500:
                    path.unlink()  # Definitive rejection: no generation was created.
                    raise RenderBlocked(f"Gemini rejected the video request ({exc.code}) {api_error(exc)}; "
                                        "fix it or wait for the rate limit, then RESUME") from None
                raise RenderBlocked("Gemini submission needs reconciliation; do not resubmit. Check render/gemini_jobs and AI Studio usage.") from None
            except Exception:
                raise RenderBlocked("Gemini submission needs reconciliation; do not resubmit. Check render/gemini_jobs and AI Studio usage.") from None
            name = operation.get("name") if isinstance(operation, dict) else None
            if not isinstance(name, str) or not OPERATION.fullmatch(name):
                receipt["response_excerpt"] = str(operation)[:500]
                write(path, receipt)
                raise RenderBlocked("Gemini returned no operation name; reconcile this job instead of submitting again")
            receipt.update(operation=name, status="SUBMITTED")
            write(path, receipt)
        if receipt.get("status") == "SUBMITTING":
            raise RenderBlocked("Gemini submission status is unknown; reconcile the existing job instead of submitting again")
        if receipt.get("status") == "FAILED":
            raise FilmError("Gemini confirmed this generation failed; apply the recorded correction on the next attempt")
        if receipt.get("status") != "COMPLETED":
            try:
                status = self.transport.request("GET", API + receipt["operation"])
            except Exception:
                raise RenderBlocked("Could not read the Gemini operation; RESUME checks the same job without a new submission") from None
            if not status.get("done"):
                raise AwaitingRender(f"Gemini {job_id}: still generating; RESUME in a few minutes checks the same job")
            if status.get("error"):
                error = status["error"]
                receipt.update(status="FAILED", error=str(error.get("message", error) if isinstance(error, dict) else error)[:300])
                write(path, receipt)
                raise FilmError("Gemini generation failed: " + receipt["error"])
            response = (status.get("response") or {}).get("generateVideoResponse") or {}
            samples = response.get("generatedSamples") or []
            uri = (samples[0].get("video") or {}).get("uri") if samples else None
            if not uri:
                reasons = response.get("raiMediaFilteredReasons") or ["no video returned"]
                receipt.update(status="FAILED", error="; ".join(map(str, reasons))[:300])
                write(path, receipt)
                raise FilmError("Gemini returned no video: " + receipt["error"])
            api_url(uri)
            receipt.update(status="COMPLETED", video_uri=uri, completed_at=now())
            write(path, receipt)
        saved = safe_path(self.project, f"render/gemini_jobs/{job_id}.mp4")
        if not saved.exists() or receipt.get("sha256") != digest(saved):
            try:
                self.transport.download(receipt["video_uri"], saved)
            except Exception:
                raise RenderBlocked("Could not download the Gemini clip; RESUME downloads the existing result without "
                                    "paying again. Google keeps generated videos for only 2 days") from None
            receipt.update(clip_path=str(saved.relative_to(self.project)), sha256=digest(saved))
            write(path, receipt)
        shutil.copyfile(saved, target)
        return RenderResult(target, True, VIDEO_MODEL, job_id)
