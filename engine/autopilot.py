"""One command that advances a project as far as current approvals allow.

Each run performs only already-approved work with the providers chosen in the
model picker, then stops at the next human decision: approve an image batch,
review frames and LOCK, approve the video batch, or review the Preview. It never
waits in a loop; run it again to continue.

FRAME_ANIMATION_V1 projects take the ANIM-007 path instead: PLAN/WAVE/FINAL
scope locks gate production, the initial wave (W00) produces only its declared
main-film cuts, and adoption stops at NEEDS_ROUTE_DECISION until a recorded
KEEP/CHANGE/MIX decision opens later waves.
"""
from pathlib import Path
import shlex

from .core import FilmError, production_profile, read, require_lock, write
from .schema import ANIMATION_PROFILE
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
    if production_profile(read(p / "project.yaml")) == ANIMATION_PROFILE:
        return animation_autopilot(p)
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


# ---------------------------------------------------------------------------
# FRAME_ANIMATION_V1: scope-gated W00/route-decision flow (ANIM-007, design 9)


def _gate(result):
    from .animation_locks import EVIDENCE_FACETS, EVIDENCE_NOTE
    result["facets"] = dict(EVIDENCE_FACETS)
    result["note"] = EVIDENCE_NOTE
    return result


def _matching_candidate(p, reviews):
    """A COMPLETE FINAL_CANDIDATE Build 2 that already binds these reviews."""
    from .builds import list_builds
    for record in list_builds(p):
        if (record.get("document_type") == "animation_build"
                and record.get("status") == "COMPLETE"
                and record.get("mode") == "FINAL_CANDIDATE"
                and record.get("edit_digest") == reviews["edit_digest"]
                and record.get("reviews", {}).get("cuts") == reviews["cuts"]
                and record.get("reviews", {}).get("transitions")
                == reviews["transitions"]):
            return record
    return None


def animation_autopilot(project, progress=None):
    """One bounded run for a FRAME_ANIMATION_V1 project (design 9.4/9.5).

    Executes only the currently-approved slice, then stops at the next human
    gate. Locks, cut adoptions and the W00 route decision are recorded only
    through their own explicit calls — this function never invents them. The
    returned stage names the exact unmet gate; run it again to continue.
    """
    from .animation_locks import (WAVES_PATH, _initial_wave,
                                  latest_route_decision, load_waves,
                                  lock_status, unresolved_wave_shots)
    from .animation_review import (film_review_status, require_current_reviews,
                                   review_status)
    from .animation_schema import (load_animation_timeline,
                                   require_animation_profile)
    target = shlex.quote(str(project))
    p = Path(project)
    config = require_animation_profile(p)
    initial = _initial_wave(config)
    timeline = load_animation_timeline(p)
    status = lock_status(p)
    if status["plan"]["state"] != "CURRENT":
        return _gate({"stage": "NEEDS_PLAN_LOCK", "lock": status["plan"],
                      "next": f"python -m engine.cli animation-lock {target} "
                              "--scope PLAN_LOCK --approver <name>"})
    waves = load_waves(p)
    if waves is None:
        return _gate({"stage": "NEEDS_PRODUCTION_INPUTS", "wave": initial,
                      "missing": [WAVES_PATH],
                      "next": f"declare the production order: python -m engine.cli "
                              f"animation-waves {target} --file <waves.json>"})
    decision = latest_route_decision(p, initial)
    reviews = review_status(p, strict=False)
    wave_by_shot = {s: w["wave"] for w in waves["waves"] for s in w["shots"]}
    for wave in waves["waves"]:
        wave_id = wave["wave"]
        if wave_id != initial:
            if decision is None:
                return _gate({"stage": "NEEDS_ROUTE_DECISION", "wave": initial,
                              "next": f"python -m engine.cli route-decision {target} {initial} "
                                      "--decision keep|change|mix --decider <name> "
                                      "--approver <name> --apply-scope <waves>"})
            if wave_id not in decision["apply_scope"]["waves"]:
                return _gate({"stage": "WAVE_SCOPE_CLOSED", "wave": wave_id,
                              "route": decision["decision_id"],
                              "next": f"{wave_id} is outside the apply scope recorded by "
                                      f"{decision['decision_id']}; a later decision or "
                                      "plan revision must open it"})
        missing = unresolved_wave_shots(p, timeline, waves, wave_id)
        if missing:
            return _gate({"stage": "NEEDS_PRODUCTION_INPUTS", "wave": wave_id,
                          "missing": missing,
                          "next": "import the wave's adopted frame sequences "
                                  "(animation-import), then lock the wave"})
        lock = status["waves"].get(wave_id, {"state": "UNLOCKED"})
        if lock["state"] != "CURRENT":
            return _gate({"stage": "NEEDS_WAVE_LOCK", "wave": wave_id,
                          "lock": lock,
                          "next": f"python -m engine.cli animation-lock {target} "
                                  f"--scope WAVE_LOCK --wave {wave_id} --approver <name>"})
        pending = [entry["instance_id"] for entry in timeline["entries"]
                   if wave_by_shot.get(entry["shot_id"]) == wave_id
                   and reviews["targets"].get(entry["instance_id"], {})
                   .get("state") != "CURRENT"]
        if pending:
            return _gate({"stage": "NEEDS_CUT_REVIEW", "wave": wave_id,
                          "pending": pending,
                          "next": "record cut reviews inside the locked scope "
                                  "(review-cut)"})
        if wave_id == initial and decision is None:
            adopted = {entry["instance_id"]:
                       reviews["targets"][entry["instance_id"]]["binding_sha256"]
                       for entry in timeline["entries"]
                       if wave_by_shot.get(entry["shot_id"]) == wave_id}
            return _gate({"stage": "NEEDS_ROUTE_DECISION", "wave": wave_id,
                          "adopted": adopted,
                          "next": f"initial wave adopted; the production lead records "
                                  f"the route decision: python -m engine.cli "
                                  f"route-decision {target} {wave_id} --decision "
                                  "keep|change|mix --decider <name> --approver <name>"})
    pending = [target_id for target_id, row in reviews["targets"].items()
               if row["state"] != "CURRENT"]
    if pending:
        return _gate({"stage": "NEEDS_CUT_REVIEW", "wave": None,
                      "pending": pending,
                      "next": "every cut and transition needs a current review "
                              "before the Final scope locks"})
    if status["final"]["state"] != "CURRENT":
        return _gate({"stage": "NEEDS_FINAL_LOCK", "lock": status["final"],
                      "next": f"python -m engine.cli animation-lock {target} "
                              "--scope FINAL_LOCK --approver <name>"})
    current = require_current_reviews(p)
    candidate = _matching_candidate(p, current)
    if candidate is None:
        from .animation_compiler import compile_final_candidate
        result = compile_final_candidate(p, progress=progress)
        return _gate({"stage": "FINAL_CANDIDATE_READY",
                      "build_id": result["build_id"],
                      "build_dir": result["build_dir"],
                      "next": "the sealed candidate still needs an explicit "
                              "final-film review (approve-film)"})
    film = film_review_status(p, candidate["build_id"])
    if film["state"] == "CURRENT":
        return _gate({"stage": "FINAL_APPROVED", "build_id": candidate["build_id"],
                      "review_id": film["review_id"],
                      "next": "fixture reviews only — real artwork acceptance, "
                              "qualification and release stay PENDING/UNQUALIFIED"})
    return _gate({"stage": "NEEDS_FINAL_REVIEW",
                  "build_id": candidate["build_id"], "film": film,
                  "next": f"python -m engine.cli approve-film {target} "
                          f"--build {candidate['build_id']} "
                          "--reviewer <name> --methods "
                          "FULL_SPEED_WHOLE_FILM,TECHNICAL_VALIDATION"})

