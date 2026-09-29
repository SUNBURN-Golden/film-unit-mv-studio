"""One command that advances a Gemini project as far as current approvals allow.

Each run performs only already-approved work, then stops at the next human
decision: approve frames, review frames and LOCK, approve the video batch, or
review the Preview. It never waits in a loop; run it again to continue.
"""
from pathlib import Path
import shlex

from .core import FilmError, read, require_lock, write
from .gemini import DEFAULT_CONFIG, approved_frames, checked_config, frames_estimate, generate_frames, reserved_usd


def placeholders(p):
    return [s["id"] for s in read(p / "manifest/shots.json") if s.get("storyboard_kind") == "placeholder"]


def autopilot(project, transport=None):
    p = Path(project)
    target = shlex.quote(str(project))
    if not (p / "render/gemini_config.json").exists():
        write(p / "render/gemini_config.json", DEFAULT_CONFIG)
    checked_config(p)

    # 1. First frames: generate only under a matching, approved estimate.
    waiting = placeholders(p)
    if waiting:
        estimate = approved_frames(p)
        frames = None
        if estimate and set(waiting) <= {row["shot"] for row in estimate["rows"]}:
            frames = generate_frames(p, transport)
            waiting = placeholders(p)
        if not waiting:
            return {"stage": "FRAMES_READY_FOR_REVIEW", "frames": frames,
                    "next": f"Review storyboard/storyboard.html. Replace any frame you dislike, then: "
                            f"python -m engine.cli lock {target} --reviewer <name>"}
        estimate = frames_estimate(p)
        return {"stage": "NEEDS_FRAME_APPROVAL", "frames": frames, "shots": len(estimate["rows"]),
                "initial_usd": estimate["initial_amount"], "worst_case_usd": estimate["worst_case_amount"],
                "estimate_id": estimate["estimate_id"],
                "note": "Failed frames used every approved attempt; consider editing those shots first." if frames else None,
                "next": f"python -m engine.cli approve-frames {target} --estimate-id {estimate['estimate_id']}"}

    # 2. LOCK is always a human review of the complete storyboard.
    try:
        require_lock(p, "gemini")
    except FilmError:
        return {"stage": "NEEDS_LOCK",
                "next": f"Review storyboard/storyboard.html, then: python -m engine.cli lock {target} --reviewer <name>"}

    # 3. Video batch: an exact estimate the User approves once.
    from .pipeline import compile_project, prepare
    estimate = prepare(p, None, "gemini", "final")[4]
    if read(p / "render/approval.json", {}).get("estimate_id") != estimate["estimate_id"]:
        spent = reserved_usd(p)
        if spent + estimate["worst_case_amount"] > estimate["max_amount"]:
            raise FilmError(f"Worst-case {estimate['worst_case_amount']} USD plus {spent} USD already reserved "
                            f"exceeds budget {estimate['max_amount']} USD; raise budget.max_usd or lower max_retry_per_shot")
        return {"stage": "NEEDS_VIDEO_APPROVAL", "initial_usd": estimate["initial_amount"],
                "worst_case_usd": estimate["worst_case_amount"], "estimate_id": estimate["estimate_id"],
                "next": f"python -m engine.cli approve {target} --estimate-id {estimate['estimate_id']}"}
    compile_project(p, None, "gemini", "final")
    shots = read(p / "qc/report.json", {}).get("shots", [])
    pending = [s["shot_id"] for s in shots if s["status"] == "AWAITING_RENDER"]
    if pending:
        return {"stage": "GENERATING", "pending": pending,
                "next": "Veo is still generating. Run autopilot again in a few minutes; it checks the same jobs and never pays twice."}

    # 4. Preview with every available take; Final still needs per-shot review.
    from .compiler import compile_preview
    build = compile_preview(p)
    return {"stage": "PREVIEW_READY", "build": build.get("build_dir"),
            "needs_review": [s["shot_id"] for s in shots if s["status"] == "NEEDS_REVIEW"],
            "failed": [s["shot_id"] for s in shots if s["status"] == "FAIL"],
            "next": "Watch the Preview. Review each generated shot (control panel 07 · RENDER), then compile-final."}
