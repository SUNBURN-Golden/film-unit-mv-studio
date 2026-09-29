from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
import math
import shutil
import copy
from datetime import datetime, timezone
import jsonschema
from .core import FilmError, digest, ffmpeg, frame_at, object_hash, read, safe_path, write
from .renderer_router import route


class AwaitingRender(FilmError):
    pass


class RenderBlocked(FilmError):
    """Stop the batch without consuming another attempt or submitting more jobs."""


@dataclass
class RenderResult:
    path: Path
    generated: bool
    provider: str
    job_id: str | None = None


def normalize(source, target, frames, fmt, source_in_ms=0):
    w, h, fps = fmt["width"], fmt["height"], fmt["fps"]
    ffmpeg(["-ss", f"{source_in_ms / 1000:.6f}", "-i", source, "-map", "0:v:0", "-an", "-vf",
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0xECEAE4,setsar=1,fps={fps},trim=end_frame={frames},setpts=PTS-STARTPTS",
        "-frames:v", frames, "-c:v", "libx264", "-preset", "veryfast", "-crf", fmt["crf"], "-pix_fmt", "yuv420p", "-threads", "2", target])


class VideoRenderer(ABC):
    name = "abstract"
    billing_unit = "credits"

    def __init__(self, project, fmt):
        self.project, self.fmt = Path(project), fmt

    @abstractmethod
    def render(self, shot, target, attempt=0, correction="", job_id="") -> RenderResult:
        ...

    def quote(self, shot, quality):
        return 0

    def preflight(self):
        pass

    def config_hash(self):
        return object_hash({"name": self.name, "format": self.fmt})

    def input_hash(self, shot, attempt, job_id):
        return self.config_hash()

    def generation_config(self, shot):
        """Stable paid input settings, excluding local export size and price evidence."""
        return None


class MockRenderer(VideoRenderer):
    name = "mock"

    def render(self, shot, target, attempt=0, correction="", job_id=""):
        w, h, fps = self.fmt["width"], self.fmt["height"], self.fmt["fps"]
        frames = frame_at(shot["out_ms"], fps) - frame_at(shot["in_ms"], fps)
        source = safe_path(self.project, shot["references"][0])
        effect = shot["motion"].get("local_effect", "hold")
        if effect not in {"hold", "zoom", "pan"}:
            raise FilmError("Supported local effects: hold, zoom, pan")
        if effect != "hold" and shot["camera"].get("movement") == "none":
            raise FilmError("Pan/zoom conflicts with this shot's locked camera")
        vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0xECEAE4,setsar=1"
        if effect in {"zoom", "pan"}:
            zoom = f"1+0.035*on/{frames}" if effect == "zoom" else "1.04"
            x = "iw/2-iw/zoom/2" if effect == "zoom" else f"(iw-iw/zoom)*on/{frames}"
            vf += f",zoompan=z='{zoom}':x='{x}':y='ih/2-ih/zoom/2':d=1:s={w}x{h}:fps={fps}"
        ffmpeg(["-loop", "1", "-framerate", fps, "-i", source, "-an", "-vf", vf,
            "-frames:v", frames, "-r", fps, "-c:v", "libx264", "-preset", "veryfast", "-crf", self.fmt["crf"], "-pix_fmt", "yuv420p", "-threads", "2", target])
        return RenderResult(Path(target), False, "local", job_id)


class ManualRenderer(VideoRenderer):
    name = "manual"

    def input_hash(self, shot, attempt, job_id):
        source = self.project / f"render/manual/{shot['id']}_a{attempt}.mp4"
        return digest(source) if source.exists() else None

    def render(self, shot, target, attempt=0, correction="", job_id=""):
        source = self.project / f"render/manual/{shot['id']}_a{attempt}.mp4"
        if not source.exists():
            raise AwaitingRender(f"Upload clip {source.name} for {shot['id']}. Correction: {correction}")
        shutil.copyfile(source, target)
        return RenderResult(Path(target), True, "manual_import", job_id)


class OpenArtRenderer(VideoRenderer):
    """Work MCP bridge. Does not invent a public REST API or copy connector tokens.

    Writes a stable outbox request; Work submits it via the authenticated connector,
    saves historyId, then imports the completed file and response. Resuming never
    creates a new job ID for the same attempt. See docs/OPENART_BRIDGE.md.
    """
    name = "openart"

    def __init__(self, project, fmt):
        super().__init__(project, fmt)
        self.config = read(self.project / "render/openart_config.json", {"models": {}, "shots": {}})

    def config_hash(self):
        config = copy.deepcopy(self.config)
        for specific in config.get("shots", {}).values():
            for quality in ("draft", "final"):
                for key in ("valid_until", "cost_evidence"):
                    specific.get(quality, {}).pop(key, None)
        return object_hash(config)

    def generation_config(self, shot):
        model, params, _ = self.prepared(shot, self.quality)
        return {"provider": self.name, "model": model, "mode": "image2video", "params": params}

    def input_hash(self, shot, attempt, job_id):
        response = self.project / f"render/responses/{job_id}.json"
        if not response.exists():
            return None
        receipt = read(response)
        request = self.project / f"render/requests/{job_id}.json"
        if receipt.get("history_id") != read(request).get("history_id"):
            raise RenderBlocked("OpenArt receipt does not match the recorded provider history ID")
        clip = safe_path(self.project, receipt["clip_path"]) if receipt.get("clip_path") else None
        return object_hash({"receipt": receipt, "clip": digest(clip) if clip and clip.exists() else None})

    def prepared(self, shot, quality):
        specific = self.config.get("shots", {}).get(shot["id"])
        if not specific:
            raise FilmError(f"{shot['id']}: configure a verified OpenArt first-frame reference and exact quote")
        available = self.config.get("models", {})
        # Route only within this shot's priced model, never another shot's catalog.
        model = route(shot, [specific[quality]["model"]], quality)
        spec = available[model]
        schema = spec["jsonSchema"]
        params = dict(specific[quality]["params"])
        # Mutable direction is rebuilt from locked bible + shot; no model-directed story.
        params["prompt"] = build_prompt(self.project, shot)
        for key in ("generateAudio", "generateSound"):
            if key in schema["properties"]:
                params[key] = False
        params["videoCount"] = 1
        if params.get("duration", 0) * 1000 < shot["duration_ms"]:
            raise FilmError(f"{shot['id']}: quoted generation is shorter than the shot")
        if specific["reference_sha256"] != digest(safe_path(self.project, shot["references"][0])):
            raise FilmError("Uploaded reference no longer matches locked first frame")
        jsonschema.validate(params, schema)
        quote = specific[quality]
        expiry = datetime.fromisoformat(quote["valid_until"])
        if expiry <= datetime.now(timezone.utc):
            raise FilmError("OpenArt quote expired; refresh exact form/cost before approval")
        if quote["model"] != model or quote["mode"] != "image2video":
            raise FilmError("Router choice differs from quoted model/mode")
        price_params = {k: v for k, v in params.items() if k != "prompt"}
        if quote["priced_params_sha256"] != object_hash(price_params):
            raise FilmError("Render settings differ from the exact priced settings")
        return model, params, float(quote["credits"])

    def quote(self, shot, quality):
        return self.prepared(shot, quality)[2]

    def render(self, shot, target, attempt=0, correction="", job_id=""):
        model, params, credits = self.prepared(shot, self.quality)
        if correction:
            params["prompt"] += "\nPrevious attempt failed. Required corrections:\n" + correction
        job = {"job_id": job_id, "shot_id": shot["id"], "attempt": attempt,
            "credits": credits, "status": "READY_FOR_WORK_SUBMISSION", "tool": "openart_generate_video",
            "arguments": {"model": model, "mode": "image2video", "params": params},
            "reference_sha256": digest(safe_path(self.project, shot["references"][0]))}
        request = self.project / f"render/requests/{job_id}.json"
        response = self.project / f"render/responses/{job_id}.json"
        if not request.exists():
            write(request, job)
        if not response.exists():
            raise AwaitingRender(f"OpenArt job {job_id} is queued; submit once in Work and import the result")
        receipt = read(response)
        if receipt.get("history_id") != read(request).get("history_id"):
            raise RenderBlocked("OpenArt receipt does not match the recorded provider history ID")
        if receipt.get("job_id") == job_id and receipt.get("status") in {"FAILED", "CANCELLED"}:
            raise FilmError(f"Provider job failed: {receipt.get('error', receipt['status'])}")
        if receipt.get("job_id") != job_id or receipt.get("status") != "COMPLETED" or not receipt.get("history_id"):
            raise AwaitingRender(f"OpenArt job {job_id} is not completed; do not resubmit")
        clip = safe_path(self.project, receipt["clip_path"])
        if receipt["sha256"] != digest(clip):
            raise RenderBlocked("Imported clip hash does not match its OpenArt receipt; recover this result")
        shutil.copyfile(clip, target)
        return RenderResult(Path(target), True, model, job_id)


def build_prompt(project, shot):
    style = read(Path(project) / "bible/style_bible.yaml")
    chars = read(Path(project) / "bible/characters.yaml")
    return "\n".join([
        "Animate this approved first frame. Preserve its exact composition and character identities.",
        shot["description"], f"Composition: {shot['composition']}",
        f"Camera: {shot['camera']}", f"Motion only: {shot['motion']}",
        f"Style: {style}", f"Character invariants: {chars}",
        "No audio. No new text. Do not introduce new scenes or camera movement.",
    ])


def get_renderer(name, project, fmt, quality):
    from .fal_renderer import FalRenderer
    from .economy import EconomyRenderer
    from .gemini import GeminiVideoRenderer
    cls = {"mock": MockRenderer, "manual": ManualRenderer, "openart": OpenArtRenderer, "fal": FalRenderer,
           "economy": EconomyRenderer, "gemini": GeminiVideoRenderer}.get(name)
    if not cls:
        raise FilmError("Unknown renderer")
    renderer = cls(project, fmt)
    renderer.quality = quality
    return renderer
