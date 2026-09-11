from pathlib import Path
from .core import FilmError, now, object_hash, read, require_lock, write


def make_estimate(p, shots, renderer, quality, fingerprint):
    p = Path(p)
    config = read(p / "project.yaml")
    retry = int(config["budget"]["max_retry_per_shot"])
    if not 0 <= retry <= 10:
        raise FilmError("max_retry_per_shot must be 0–10")
    rows = []
    for shot in shots:
        credits = renderer.quote(shot, quality) if shot["render_mode"] != "STATIC" else 0
        if credits < 0:
            raise FilmError("Negative or unknown quote")
        rows.append({"shot": shot["id"], "in_ms": shot["in_ms"], "out_ms": shot["out_ms"], "credits": credits})
    initial = sum(r["credits"] for r in rows)
    spec = {"production": fingerprint, "renderer": renderer.name, "quality": quality,
        "rows": rows, "initial_credits": initial, "retry_reserve": initial * retry,
        "worst_case_credits": initial * (1+retry), "max_retry_per_shot": retry,
        "max_credits": config["budget"]["max_credits"], "provider_config": renderer.config_hash()}
    if spec["worst_case_credits"] > spec["max_credits"]:
        raise FilmError(f"Worst-case {spec['worst_case_credits']} exceeds budget {spec['max_credits']}")
    spec["estimate_id"] = object_hash(spec)
    write(p / "render/estimate.json", spec)
    return spec


def approve(p, estimate):
    require_lock(p, estimate["renderer"])
    write(Path(p) / "render/approval.json", {"estimate_id": estimate["estimate_id"], "approved_at": now()})


def require_approval(p, estimate):
    if estimate["worst_case_credits"] == 0:
        return
    approval = read(Path(p) / "render/approval.json", {})
    if approval.get("estimate_id") != estimate["estimate_id"]:
        raise FilmError("Review and approve this exact batch estimate before paid rendering")


def reserve(p, job_id, credits, estimate):
    """Caller holds project_mutex. Pending/failed jobs remain reserved conservatively."""
    path = Path(p) / "render/ledger.json"
    ledger = read(path, {"jobs": {}})
    if job_id in ledger["jobs"]:
        return
    total = sum(j["reserved_credits"] for j in ledger["jobs"].values())
    batch = sum(j["reserved_credits"] for j in ledger["jobs"].values() if j["estimate_id"] == estimate["estimate_id"])
    if total + credits > estimate["max_credits"] or batch + credits > estimate["worst_case_credits"]:
        raise FilmError("Credit cap reached; submission stopped before a new job")
    ledger["jobs"][job_id] = {"reserved_credits": credits, "estimate_id": estimate["estimate_id"], "created_at": now()}
    write(path, ledger)
