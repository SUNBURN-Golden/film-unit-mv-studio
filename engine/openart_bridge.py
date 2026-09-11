"""Helpers used by Work after authenticated OpenArt metadata/cost calls."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import shutil
import jsonschema
from .core import FilmError, digest, object_hash, read, safe_path, write
from .renderers import build_prompt


def register_quote(project, shot_id, form, reference, cost_result, quality="draft", extra_params=None):
    """Accept structuredContent from model_form_get / upload_metadata_get / model_cost.

    This helper only registers evidence. It neither uploads nor submits a job.
    Obtain a fresh exact-config cost result with this reference and settings first.
    """
    p = Path(project)
    shot = next(s for s in read(p / "manifest/shots.json") if s["id"] == shot_id)
    if form["mode"] != "image2video":
        raise FilmError("Only first-frame image2video is supported by this v0.1 bridge")
    schema = form["jsonSchema"]
    props = schema["properties"]
    params = {k: v["default"] for k, v in props.items() if "default" in v}
    params.update({k: v["const"] for k, v in props.items() if "const" in v})
    params.update(extra_params or {})
    params.update(prompt=build_prompt(p, shot), startFrame=reference, generateAudio=False, videoCount=1, aspectRatio="4:3")
    jsonschema.validate(params, schema)
    rows = cost_result["items"]
    matching = [r for r in rows if r["model"] == form["model"] and r["mode"] == form["mode"]]
    if len(matching) != 1:
        raise FilmError("Need one exact model/mode cost result")
    priced = matching[0]
    for key in ["duration", "resolution", "aspectRatio", "videoCount", "generateAudio"]:
        if priced["config"].get(key) != params.get(key):
            raise FilmError(f"Cost result does not cover exact {key}")
    config = read(p / "render/openart_config.json", {"models": {}, "shots": {}})
    config["models"][form["model"]] = form
    specific = config["shots"].setdefault(shot_id, {})
    specific["reference_sha256"] = digest(safe_path(p, shot["references"][0]))
    specific[quality] = {"model": form["model"], "mode": form["mode"], "params": params,
        "credits": priced["totalCredits"], "cost_evidence": cost_result,
        "priced_params_sha256": object_hash({k:v for k,v in params.items() if k != "prompt"}),
        "valid_until": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}
    write(p / "render/openart_config.json", config)
    return config


def import_completed(project, job_id, history_id, clip):
    p = Path(project)
    request_path = safe_path(p, f"render/requests/{job_id}.json")
    request = read(request_path)
    if request["job_id"] != job_id or not history_id:
        raise FilmError("Job ID and provider history ID are required")
    dest = safe_path(p, f"render/responses/{job_id}.mp4")
    if Path(clip).resolve() != dest.resolve():
        shutil.copyfile(clip, dest)
    write(safe_path(p, f"render/responses/{job_id}.json"), {"job_id": job_id, "history_id": history_id,
        "status": "COMPLETED", "clip_path": str(dest.relative_to(p)), "sha256": digest(dest)})


def record_submission(project, job_id, history_id):
    p = Path(project)
    path = safe_path(p, f"render/requests/{job_id}.json")
    request = read(path)
    previous = request.get("history_id")
    if previous and previous != history_id:
        raise FilmError("This job already has a history ID. Do not submit it twice")
    request.update(history_id=history_id, status="SUBMITTED")
    write(path, request)
