"""One command that advances a project as far as current approvals allow.

Each run performs only already-approved work with the providers chosen in the
model picker, then stops at the next human decision: approve an image batch,
review frames and LOCK, approve the video batch, or review the Preview. It never
waits in a loop; run it again to continue.
"""
from pathlib import Path
import shlex

from .core import FilmError, read, require_lock, write
from . import imagegen, providers
from .gemini import DEFAULT_CONFIG
from .imagegen import reserved_usd


def placeholders(p):
    return [s["id"] for s in read(p / "manifest/shots.json") if s.get("storyboard_kind") == "placeholder"]


def _packets(p):
    from .packets import export_packets
    return export_packets(p)["packets"]


def _image_kind(p, kind, adapter):
    """Run one image kind until nothing is missing. Returns a stop dict (with 'stage') or a summary."""
    made, warnings = [], []
    for _ in range(2):  # generate, then re-check what is still missing
        try:
            estimate = imagegen.estimate(p, kind, adapter)
        except imagegen.NothingToDo as done:
            return {"made": made, "warnings": done.warnings or warnings}
        plan = imagegen.approved(p, kind, adapter)
        if plan is None:
            return {"stage": "NEEDS_IMAGE_APPROVAL", "kind": kind, "count": len(estimate["rows"]),
                    "initial_usd": estimate["initial_amount"], "worst_case_usd": estimate["worst_case_amount"],
                    "estimate_id": estimate["estimate_id"], "provider": adapter.id, "made": made,
                    "warnings": estimate["warnings"]}
        result = imagegen.generate(p, kind, adapter)
        made += result["generated"]
        warnings = estimate["warnings"]
        if result["failed"]:
            return {"stage": "IMAGES_FAILED", "kind": kind, "failed": result["failed"], "made": made,
                    "next": "실패한 이미지는 정해 둔 재시도 횟수를 모두 썼습니다. 설명을 고치거나 다른 모델을 골라 다시 실행하세요."}
    return {"made": made, "warnings": warnings}


def autopilot(project, image=None):
    p = Path(project)
    target = shlex.quote(str(project))
    image_choice, video_choice = providers.chosen(p, "image"), providers.chosen(p, "video")
    lacking = [label for label, choice in (("참조 이미지 · 첫 프레임", image_choice), ("영상", video_choice)) if choice is None]
    if lacking:
        return {"stage": "NEEDS_MODEL_CHOICE", "missing": lacking, "next": "'모델 선택' 탭에서 고르세요."}

    # 1. Images. A hand-made stage stops with a work order instead of calling anything.
    if image_choice.kind == "manual":
        if placeholders(p):
            return {"stage": "NEEDS_MANUAL_FRAMES", "packets": _packets(p), "shots": placeholders(p),
                    "next": "작업지시서로 첫 프레임을 만들어 각 샷에 넣은 뒤 다시 실행하세요."}
    else:
        adapter = image or providers.build_image(image_choice.id)
        made, warnings = [], []
        for kind in imagegen.KINDS:
            if kind == "references" and not adapter.supports_references:
                continue  # A service that cannot see reference images gains nothing from them.
            step = _image_kind(p, kind, adapter)
            if "stage" in step:
                return step
            made += step["made"]
            warnings += step["warnings"]
        if placeholders(p):
            raise FilmError("첫 프레임이 아직 남아 있습니다: " + ", ".join(placeholders(p)))
        if made:
            return {"stage": "FRAMES_READY_FOR_REVIEW", "made": made, "warnings": warnings,
                    "next": f"Review storyboard/storyboard.html. Replace any frame you dislike, then: "
                            f"python -m engine.cli lock {target} --reviewer <name>"}

    # 2. LOCK is always a human review of the complete storyboard.
    try:
        require_lock(p, video_choice.renderer or "gemini")
    except FilmError:
        return {"stage": "NEEDS_LOCK",
                "next": f"Review storyboard/storyboard.html, then: python -m engine.cli lock {target} --reviewer <name>"}

    # 3. Video.
    renderer = video_choice.renderer
    if renderer == "manual":
        return {"stage": "NEEDS_MANUAL_VIDEO", "packets": _packets(p),
                "next": "작업지시서로 샷 영상을 만들어 06 · COMPILE에서 가져온 뒤 Preview를 만드세요."}
    if renderer == "mock":
        from .compiler import compile_preview
        build = compile_preview(p)
        return {"stage": "PREVIEW_READY", "build": build.get("build_dir"), "needs_review": [], "failed": [],
                "next": "테스트 Preview입니다. 실제 영상 모델을 고르면 움직이는 영상이 됩니다."}
    if renderer == "gemini" and not (p / "render/gemini_config.json").exists():
        write(p / "render/gemini_config.json", DEFAULT_CONFIG)
    if renderer == "fal" and not (p / "render/fal_config.json").exists():
        write(p / "render/fal_config.json", read(Path(__file__).resolve().parents[1] / "templates/fal_wan_turbo.json"))
    from .pipeline import compile_project, prepare
    estimate = prepare(p, None, renderer, "final")[4]
    if read(p / "render/approval.json", {}).get("estimate_id") != estimate["estimate_id"]:
        spent = reserved_usd(p)
        if spent + estimate["worst_case_amount"] > estimate["max_amount"]:
            raise FilmError(f"Worst-case {estimate['worst_case_amount']} USD plus {spent} USD already reserved "
                            f"exceeds budget {estimate['max_amount']} USD; raise budget.max_usd or lower max_retry_per_shot")
        return {"stage": "NEEDS_VIDEO_APPROVAL", "initial_usd": estimate["initial_amount"],
                "worst_case_usd": estimate["worst_case_amount"], "estimate_id": estimate["estimate_id"],
                "next": f"python -m engine.cli approve {target} --estimate-id {estimate['estimate_id']}"}
    compile_project(p, None, renderer, "final")
    shots = read(p / "qc/report.json", {}).get("shots", [])
    pending = [s["shot_id"] for s in shots if s["status"] == "AWAITING_RENDER"]
    if pending:
        return {"stage": "GENERATING", "pending": pending,
                "next": "영상이 아직 만들어지는 중입니다. 몇 분 뒤 다시 실행하세요. 같은 작업을 확인할 뿐 다시 결제하지 않습니다."}

    # 4. Preview with every available take; Final still needs per-shot review.
    from .compiler import compile_preview
    build = compile_preview(p)
    return {"stage": "PREVIEW_READY", "build": build.get("build_dir"),
            "needs_review": [s["shot_id"] for s in shots if s["status"] == "NEEDS_REVIEW"],
            "failed": [s["shot_id"] for s in shots if s["status"] == "FAIL"],
            "next": "Watch the Preview. Review each generated shot (control panel 07 · RENDER), then compile-final."}

