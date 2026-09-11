from pathlib import Path
import copy
import math
from .core import FilmError, digest, frame_at, now, object_hash, probe, project_mutex, read, require_lock, validate_manifest, write
from .renderers import AwaitingRender, RenderBlocked, RenderResult, MockRenderer, get_renderer, normalize
from . import budget, takes
from .qc import inspect_clip
from .assemble import assemble, exports


def prepare(project, duration_seconds=None, mode="mock", quality="final"):
    p = Path(project)
    config, audio = read(p / "project.yaml"), read(p / "analysis/audio.json")
    threshold = config.get("qc", {}).get("threshold")
    if type(threshold) not in {int, float} or not math.isfinite(threshold) or not 0 <= threshold <= 100:
        raise FilmError("QC threshold must be a finite score from 0 to 100")
    lock = require_lock(p, mode)
    shots = read(p / "manifest/shots.json")
    validate_manifest(shots, audio["duration_ms"], config["format"]["fps"])
    duration_ms = min(audio["duration_ms"], round(duration_seconds*1000)) if duration_seconds is not None else audio["duration_ms"]
    if not 1000 <= duration_ms <= audio["duration_ms"]:
        raise FilmError("Pilot duration must be at least 1 second")
    selected = []
    for s in shots:
        if s["in_ms"] >= duration_ms:
            break
        s = copy.deepcopy(s)
        s["out_ms"] = min(s["out_ms"], duration_ms)
        s["duration_ms"] = s["out_ms"] - s["in_ms"]
        if frame_at(s["out_ms"], config["format"]["fps"]) > frame_at(s["in_ms"], config["format"]["fps"]):
            selected.append(s)
    fmt = dict(config["format"])
    if quality == "draft":
        fmt.update(width=960, height=720)
    elif quality != "final":
        raise FilmError("Quality must be draft or final")
    renderer = get_renderer(mode, p, fmt, quality)
    estimate = budget.make_estimate(p, selected, renderer, quality, lock["fingerprint"])
    return selected, duration_ms, fmt, renderer, estimate


def compile_project(project, duration_seconds=None, mode="mock", quality="final", output_name=None, progress=None):
    p = Path(project)
    with project_mutex(p):
        return _compile(p, duration_seconds, mode, quality, output_name, progress)


def _compile(p, seconds, mode, quality, output_name, progress):
    shots, duration_ms, fmt, renderer, estimate = prepare(p, seconds, mode, quality)
    budget.require_approval(p, estimate)
    output_name = output_name or ("ANIMATIC.mp4" if mode == "mock" else "MASTER.mp4")
    if Path(output_name).name != output_name or not output_name.endswith(".mp4"):
        raise FilmError("Output must be a simple .mp4 filename")
    run_id = estimate["estimate_id"][:16]
    directory = p / "render" / quality / run_id
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "state.json"
    state = read(state_path, {"shots": {}})
    shared_states = {}
    def save_state():
        write(state_path, state)
        for identity, path in shared_states.items():
            write(path, state["shots"][identity])
    report = {"run_id": run_id, "production_id": estimate["production"], "mode": mode, "status": "RENDERING", "shots": [], "started_at": now()}
    report_path = p / "qc/report.json"
    clips, pending = [], []
    local = MockRenderer(p, fmt)
    for index, shot in enumerate(shots):
        if progress:
            progress(index, len(shots), shot["id"])
        sstate = state["shots"].setdefault(shot["id"], {"attempt": 0, "correction": ""})
        max_retry = estimate["max_retry_per_shot"]
        if shot["render_mode"] != "STATIC" and shot.get("renderer") != "mock" and mode in {"economy", "fal", "openart"}:
            planned = [renderer.for_attempt(shot, i) if mode == "economy" else renderer for i in range(max_retry + 1)]
            identity = object_hash({"production": estimate["production"], "shot": shot["id"],
                "inputs": [takes.key_for(r, shot, i, "") for i, r in enumerate(planned)]})
            shared_states[shot["id"]] = p / f"render/progress/{identity}.json"
            # Re-export the chosen take instead of revisiting rejected attempts
            # when only the local output size or price evidence changes.
            sstate = read(shared_states[shot["id"]], sstate)
            state["shots"][shot["id"]] = sstate
        while sstate["attempt"] <= max_retry:
            attempt = sstate["attempt"]
            active = local if shot["render_mode"] == "STATIC" or shot.get("renderer") == "mock" else renderer
            if active.name == "economy":
                active = renderer.for_attempt(shot, attempt)
            job_id = f"{run_id}_{shot['id']}_a{attempt}"
            raw = directory / f"{shot['id']}_a{attempt}_raw.mp4"
            target = directory / f"{shot['id']}_a{attempt}.mp4"
            try:
                # Settings/auth/budget failures are not failed generations. Never
                # burn retries or switch to a more expensive provider for them.
                try:
                    credits = active.quote(shot, quality)
                    take_key = takes.key_for(active, shot, attempt, sstate["correction"])
                except FilmError as exc:
                    raise RenderBlocked(str(exc)) from exc
                if take_key:
                    job_id = "take_" + take_key
                reused = takes.restore(p, take_key, raw)
                source_in_ms = takes.edit_start(p, job_id, raw) if raw.exists() else 0
                cache = read(target.with_suffix(".cache.json"), {})
                def inputs():
                    return object_hash({"provider_input": active.input_hash(shot, attempt, job_id),
                                        "source_in_ms": source_in_ms, "format": fmt})
                input_hash = inputs()
                cached = target.exists() and cache.get("sha256") == digest(target) and cache.get("input_hash") == input_hash
                if not cached:
                    if reused:
                        result = RenderResult(raw, True, active.name, job_id)
                    else:
                        try:
                            active.preflight()
                            require_lock(p, mode)
                            if credits:
                                budget.reserve(p, job_id, credits, estimate, active.billing_unit)
                        except FilmError as exc:
                            raise RenderBlocked(str(exc)) from exc
                        result = active.render(shot, raw, attempt, sstate["correction"], job_id)
                    generated = result.generated
                    if generated:
                        if not reused:
                            takes.remember(p, take_key, raw, result.provider, job_id)
                        source_in_ms = takes.edit_start(p, job_id, raw)
                        info = probe(raw)
                        video = next(s for s in info["streams"] if s["codec_type"] == "video")
                        expected = frame_at(shot["out_ms"], fmt["fps"]) - frame_at(shot["in_ms"], fmt["fps"])
                        if float(video.get("duration", info["format"]["duration"])) - source_in_ms/1000 + 0.5/fmt["fps"] < expected/fmt["fps"]:
                            raise FilmError("Generated clip is too short; render the entire requested duration")
                        normalize(raw, target, expected, fmt, source_in_ms)
                    else:
                        raw.replace(target)
                    write(target.with_suffix(".cache.json"), {"sha256": digest(target), "generated": generated, "input_hash": inputs()})
                else:
                    generated = cache["generated"]
                qc = inspect_clip(target, shot, p, fmt, generated, estimate["production"])
                qc.update(attempt=attempt, job_id=job_id, provider=active.name,
                          profile=getattr(active, "profile", active.name), reused_take=reused,
                          raw_path=str(raw.relative_to(p)) if generated else None,
                          selected_duration_ms=shot["duration_ms"], source_in_ms=source_in_ms)
                if qc["status"] in {"PASS", "PASS_LOCAL"}:
                    clips.append(target)
                    report["shots"].append(qc)
                    sstate["status"] = qc["status"]
                    break
                if qc["status"] == "NEEDS_REVIEW":
                    report["shots"].append(qc)
                    pending.append(shot["id"])
                    break
                correction = "\n".join(qc["failures"])
            except AwaitingRender as e:
                report["shots"].append({"shot_id": shot["id"], "status": "AWAITING_RENDER", "attempt": attempt, "job_id": job_id, "provider": active.name, "profile": getattr(active, "profile", active.name), "message": str(e)})
                pending.append(shot["id"])
                break
            except RenderBlocked as exc:
                save_state()
                report["shots"].append({"shot_id": shot["id"], "status": "BLOCKED", "job_id": job_id, "message": str(exc)})
                report.update(status="BLOCKED", blocked_reason=str(exc))
                write(report_path, report)
                raise
            except (FilmError, StopIteration) as e:
                correction = str(e)
                qc = {"shot_id": shot["id"], "status": "FAIL", "attempt": attempt, "job_id": job_id, "failures": [correction]}
            sstate.setdefault("failures", []).append(qc)
            if attempt >= max_retry:
                report["shots"].append(qc)
                pending.append(shot["id"])
                sstate["status"] = "EXHAUSTED"
                break
            sstate["correction"] = correction
            sstate["attempt"] += 1
            save_state()
        save_state()
        write(report_path, report)
    report["retries"] = sum(s["attempt"] for s in state["shots"].values())
    report["pending_shots"] = pending
    if pending:
        report["status"] = "AWAITING_REVIEW_OR_RENDER"
        write(report_path, report)
        return {"status": report["status"], "pending": pending, "report": str(report_path)}
    # Recheck content lock after long external/local work and before export.
    require_lock(p, mode)
    kind = "MOCK TIMING ANIMATIC" if mode == "mock" else "REVIEWED MUSIC VIDEO"
    out = assemble(p, clips, duration_ms, fmt, kind, output_name)
    exports(p, out)
    report.update(status="COMPLETE", output=str(out), completed_at=now(), generated_shots=sum(bool(s.get("generated")) for s in report["shots"]))
    write(report_path, report)
    write(out.with_name(out.stem + "_QC.json"), report)
    if progress:
        progress(len(shots), len(shots), "complete")
    return {"status": "COMPLETE", "output": str(out), "shots": len(shots), "generated_shots": report["generated_shots"], "retries": report["retries"]}
