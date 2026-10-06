from pathlib import Path
import math
from .core import FilmError, now, object_hash, read, require_lock, require_legacy_profile, write


def make_estimate(p, shots, renderer, quality, fingerprint):
    if renderer.name == "economy":
        return renderer.make_estimate(shots, fingerprint)
    p = Path(p)
    config = read(p / "project.yaml")
    retry = int(config["budget"]["max_retry_per_shot"])
    if not 0 <= retry <= 10:
        raise FilmError("max_retry_per_shot must be 0–10")
    unit = getattr(renderer, "billing_unit", "credits")
    if unit not in {"credits", "USD"}:
        raise FilmError("Unknown billing unit")
    cap = config["budget"].get("max_usd" if unit == "USD" else "max_credits", 0)
    if not math.isfinite(cap) or cap < 0:
        raise FilmError("Budget must be a finite nonnegative amount")
    rows = []
    for shot in shots:
        credits = renderer.quote(shot, quality) if shot["render_mode"] != "STATIC" and shot.get("renderer") != "mock" else 0
        if not math.isfinite(credits) or credits < 0:
            raise FilmError("Negative or unknown quote")
        row = {"shot": shot["id"], "in_ms": shot["in_ms"], "out_ms": shot["out_ms"], "amount": credits}
        if unit == "credits":
            row["credits"] = credits  # Legacy OpenArt readers only; never USD.
        rows.append(row)
    initial = round(sum(r["amount"] for r in rows), 6)
    spec = {"production": fingerprint, "renderer": renderer.name, "quality": quality,
        "billing_unit": unit, "rows": rows, "initial_amount": initial, "retry_reserve": round(initial * retry, 6),
        "worst_case_amount": round(initial * (1+retry), 6), "max_retry_per_shot": retry,
        "max_amount": cap, "provider_config": renderer.config_hash(), "qc_policy": config.get("qc", {})}
    if unit == "credits":
        spec.update(initial_credits=initial, worst_case_credits=spec["worst_case_amount"], max_credits=cap)
    if spec["worst_case_amount"] > cap:
        raise FilmError(f"Worst-case {spec['worst_case_amount']} {unit} exceeds budget {cap} {unit}")
    spec["estimate_id"] = object_hash(spec)
    write(p / "render/estimate.json", spec)
    return spec


def approve(p, estimate):
    require_legacy_profile(p)
    require_lock(p, estimate["renderer"])
    write(Path(p) / "render/approval.json", {"estimate_id": estimate["estimate_id"], "approved_at": now()})


def require_approval(p, estimate):
    if estimate.get("billing_unit") == "mixed" and all(pool["worst_case_amount"] == 0 for pool in estimate["pools"].values()):
        return
    if estimate.get("worst_case_amount", estimate.get("worst_case_credits")) == 0:
        return
    approval = read(Path(p) / "render/approval.json", {})
    if approval.get("estimate_id") != estimate["estimate_id"]:
        raise FilmError("Review and approve this exact batch estimate before paid rendering")


def reserve(p, job_id, credits, estimate, billing_unit=None):
    """Caller holds project_mutex. Pending/failed jobs remain reserved conservatively."""
    path = Path(p) / "render/ledger.json"
    ledger = read(path, {"jobs": {}})
    unit = billing_unit or estimate.get("billing_unit", "credits")
    if unit not in {"USD", "credits"} or (estimate.get("billing_unit") != "mixed" and unit != estimate.get("billing_unit", "credits")):
        raise FilmError("Invalid billing unit for this estimate")
    limits = estimate["pools"][unit] if estimate.get("billing_unit") == "mixed" else estimate
    if not math.isfinite(credits) or credits < 0:
        raise FilmError("Invalid reservation amount")
    previous = ledger["jobs"].get(job_id)
    if previous:
        if previous.get("billing_unit", "credits") != unit or previous.get("reserved_amount", previous.get("reserved_credits")) != credits or (not job_id.startswith("take_") and previous["estimate_id"] != estimate["estimate_id"]):
            raise FilmError("Existing reservation differs from this job")
        return
    jobs = [j for j in ledger["jobs"].values() if j.get("billing_unit", "credits") == unit]
    amount = lambda j: j.get("reserved_amount", j.get("reserved_credits", 0))
    total = round(sum(amount(j) for j in jobs) + credits, 6)
    batch = round(sum(amount(j) for j in jobs if j["estimate_id"] == estimate["estimate_id"]) + credits, 6)
    if total > limits.get("max_amount", limits.get("max_credits")) or batch > limits.get("worst_case_amount", limits.get("worst_case_credits")):
        raise FilmError(f"{unit} cap reached; submission stopped before a new job")
    entry = {"billing_unit": unit, "reserved_amount": credits, "estimate_id": estimate["estimate_id"], "created_at": now()}
    if unit == "credits":
        entry["reserved_credits"] = credits
    ledger["jobs"][job_id] = entry
    write(path, ledger)
