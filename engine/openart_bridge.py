"""Helpers used by Work after authenticated OpenArt metadata/cost calls."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import shutil
import jsonschema
from .core import FilmError, digest, object_hash, project_mutex, read, safe_path, write
from .renderers import build_prompt


def register_quote(project, shot_id, form, reference, cost_result, quality="draft", extra_params=None, profile=None):
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
    params.update(prompt=build_prompt(p, shot), startFrame=reference, videoCount=1)
    for key, value in {"generateAudio": False, "generateSound": False, "aspectRatio": "4:3"}.items():
        if key in props:
            params[key] = value
    jsonschema.validate(params, schema)
    rows = cost_result["items"]
    matching = [r for r in rows if r["model"] == form["model"] and r["mode"] == form["mode"]]
    if len(matching) != 1:
        raise FilmError("Need one exact model/mode cost result")
    priced = matching[0]
    for key in ["duration", "resolution", "aspectRatio", "videoCount", "generateAudio", "generateSound"]:
        if priced["config"].get(key) != params.get(key):
            raise FilmError(f"Cost result does not cover exact {key}")
    if profile is not None:
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile):
            raise FilmError("Profile must be a simple name")
    path = p / (f"render/profiles/{profile}.json" if profile else "render/openart_config.json")
    config = read(path, {"models": {}, "shots": {}})
    config["models"][form["model"]] = form
    specific = config["shots"].setdefault(shot_id, {})
    specific["reference_sha256"] = digest(safe_path(p, shot["references"][0]))
    specific[quality] = {"model": form["model"], "mode": form["mode"], "params": params,
        "credits": priced["totalCredits"], "cost_evidence": cost_result,
        "priced_params_sha256": object_hash({k:v for k,v in params.items() if k != "prompt"}),
        "valid_until": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}
    write(path, config)
    return config


def import_completed(project, job_id, history_id, clip):
    p = Path(project)
    request_path = safe_path(p, f"render/requests/{job_id}.json")
    request = read(request_path)
    if request["job_id"] != job_id or not history_id or request.get("history_id") != history_id:
        raise FilmError("Job and recorded provider history ID must match before importing")
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
    if not isinstance(history_id, str) or not history_id.strip():
        raise FilmError("A nonempty provider history ID is required")
    if previous and previous != history_id:
        raise FilmError("This job already has a history ID. Do not submit it twice")
    if job_id.startswith("take_") and request["status"] not in {"SUBMITTING", "SUBMITTED"}:
        raise FilmError("Claim this Work job before submitting it")
    request.update(history_id=history_id, status="SUBMITTED")
    write(path, request)


def claim_submission(project, job_id):
    """Call immediately before the external tool. Ambiguous claims require reconciliation."""
    from .pipeline import prepare
    from .budget import require_approval
    p = Path(project)
    with project_mutex(p):
        saved = read(p / "render/estimate.json")
        seconds = max(row["out_ms"] for row in saved["rows"]) / 1000
        current = prepare(p, seconds, saved["renderer"], saved["quality"])[4]
        require_approval(p, current)
        report = read(p / "qc/report.json")
        if report["run_id"] != current["estimate_id"][:16] or not any(
            s.get("job_id") == job_id and s["status"] == "AWAITING_RENDER" for s in report["shots"]
        ):
            raise FilmError("Job is not waiting in the currently approved batch; resume compile first")
        path = safe_path(p, f"render/requests/{job_id}.json")
        request = read(path)
        if request["status"] != "READY_FOR_WORK_SUBMISSION" or request.get("history_id"):
            raise FilmError("Job already claimed or submitted; reconcile history instead of resubmitting")
        request["status"] = "SUBMITTING"
        write(path, request)
        return request


def record_failure(project, job_id, history_id, error):
    p = Path(project)
    request = read(safe_path(p, f"render/requests/{job_id}.json"))
    if not history_id or request.get("history_id") != history_id or not error.strip():
        raise FilmError("Confirmed provider failure requires matching history ID and error evidence")
    write(safe_path(p, f"render/responses/{job_id}.json"), {
        "job_id": job_id, "history_id": history_id, "status": "FAILED", "error": error})
