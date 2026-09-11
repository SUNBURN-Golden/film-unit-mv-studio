"""Actionable Work handoff. This is not a background agent or a vision model."""
from pathlib import Path
from .core import read
from .qc import SEMANTIC_ITEMS


def status(project):
    p = Path(project)
    report = read(p / "qc/report.json", {})
    manifest = {s["id"]: s for s in read(p / "manifest/shots.json", [])}
    tasks = []
    for record in report.get("shots", []):
        shot_id, state = record["shot_id"], record["status"]
        if state in {"PASS", "PASS_LOCAL"}:
            continue
        task = {"shot": shot_id, "job_id": record.get("job_id"), "status": state}
        if state == "NEEDS_REVIEW":
            task.update(action="REVIEW_ANIMATION", clip=record["clip_path"],
                raw_clip=record.get("raw_path"), samples=record.get("frame_samples", []),
                references=manifest[shot_id]["references"], shot_direction=manifest[shot_id],
                bibles=["bible/style_bible.yaml", "bible/characters.yaml", "bible/locations.yaml"],
                criteria=SEMANTIC_ITEMS,
                instruction="Inspect the actual motion plus frame samples. Record only observed evidence. Try a clean local window before failing a usable take; otherwise save failure notes and resume the approved retry plan.")
        elif state == "AWAITING_RENDER":
            job = record["job_id"]
            request = read(p / f"render/requests/{job}.json", {})
            receipt = read(p / f"render/responses/{job}.json", {})
            if receipt:
                task["action"] = "RESUME_COMPILE"
            elif request:
                request_status = request["status"]
                task.update(action={"READY_FOR_WORK_SUBMISSION": "CLAIM_THEN_SUBMIT_OPENART_ONCE",
                    "SUBMITTING": "RECONCILE_DO_NOT_RESUBMIT", "SUBMITTED": "IMPORT_EXISTING_RESULT"}.get(request_status, "INSPECT_PROVIDER_STATUS"),
                    request=f"render/requests/{job}.json", history_id=request.get("history_id"))
            elif record.get("provider") == "fal":
                task.update(action="RESUME_EXISTING_FAL_JOB", receipt=f"render/fal_jobs/{job}.json")
            else:
                task.update(action="IMPORT_MANUAL_TAKE", message=record.get("message"))
        elif state == "BLOCKED":
            task.update(action="RESOLVE_BLOCK_BEFORE_NEW_GENERATION", message=record.get("message"))
        else:
            task.update(action="REVIEW_EXHAUSTED_SHOT", failures=record.get("failures", []))
        tasks.append(task)
    ledger = read(p / "render/ledger.json", {"jobs": {}})
    reserved = {unit: round(sum(j.get("reserved_amount", j.get("reserved_credits", 0))
        for j in ledger["jobs"].values() if j.get("billing_unit", "credits") == unit), 6)
        for unit in ("USD", "credits")}
    return {"status": report.get("status", "NOT_STARTED"), "tasks": tasks,
        "reserved_in_this_project": reserved, "ledger_jobs": len(ledger["jobs"]),
        "cost_scope": "Conservative reservations, not a provider invoice or account-wide monthly spending.",
        "automation": "Work operator handles tool calls and evidence-backed visual review. Local compiler resumes and assembles; no autonomous background vision service is connected."}
