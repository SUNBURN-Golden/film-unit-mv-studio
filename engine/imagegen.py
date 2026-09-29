"""Reference images and first frames through any image provider.

Same safety model for every provider: an estimate the User approves when money
is involved, a durable job record before each request, no resubmission of an
ambiguous paid request, and provider-independent import of the result.
Free providers (unit cost 0) need no approval and may simply be retried.
"""
import io
from pathlib import Path

from PIL import Image

from .core import FilmError, atomic_text, digest, now, object_hash, project_mutex, read, safe_path, write
from .packets import aspect_ratio, compact_prompt, frame_prompt, frame_references, reference_prompt
from .renderers import RenderBlocked
from . import budget

KINDS = ("references", "frames")
PLACEHOLDER = "Director review required"


class NothingToDo(FilmError):
    """No image of this kind is missing."""

    def __init__(self, message, warnings=()):
        super().__init__(message)
        self.warnings = list(warnings)


class ImageRejected(FilmError):
    """The service refused the request (HTTP 4xx): no image was created or billed."""


class ImageProvider:
    """Adapter contract. generate() returns encoded image bytes."""
    id = ""
    model = ""
    unit_usd = 0.0              # conservative reservation per image; 0 for free providers
    supports_references = False
    max_references = 0
    prompt_limit = None         # characters, when the service caps the prompt
    native_aspects = None       # None: square only

    def preflight(self, project):
        """Provider-specific checks before any estimate or request (e.g. price evidence)."""

    def generate(self, prompt, references, aspect):
        raise NotImplementedError


def reserved_usd(project):
    ledger = read(Path(project) / "render/ledger.json", {"jobs": {}})
    return round(sum(j.get("reserved_amount", 0) for j in ledger["jobs"].values()
                     if j.get("billing_unit") == "USD"), 6)


def fit_aspect(data, aspect, native=None):
    """Return PNG bytes at the requested aspect ratio, center-cropping when the provider cannot."""
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        w, h = im.size
        tw, th = (int(x) for x in aspect.split(":"))
        if not (native and aspect in native) and abs(w / h - tw / th) > 0.02:
            if w / h > tw / th:
                width = round(h * tw / th)
                box = ((w - width) // 2, 0, (w - width) // 2 + width, h)
            else:
                height = round(w * th / tw)
                box = (0, (h - height) // 2, w, (h - height) // 2 + height)
            im = im.crop(box)
        out = io.BytesIO()
        im.save(out, format="PNG")
        return out.getvalue()


def _entries(p, name, key):
    return read(p / "bible" / name, {}).get(key, [])


def _has_description(entry):
    text = " ".join(str(entry.get(k, "")) for k in ("look", "description", "behavior"))
    return bool(text.strip()) and PLACEHOLDER not in text


def _items(p, kind, provider, shot_ids):
    aspect = aspect_ratio(read(p / "project.yaml")["format"])
    items, warnings = [], []
    limit = provider.prompt_limit
    if kind == "frames":
        shots = read(p / "manifest/shots.json")
        if shot_ids:
            unknown = set(shot_ids) - {s["id"] for s in shots}
            if unknown:
                raise FilmError("Unknown shot ID: " + ", ".join(sorted(unknown)))
            shots = [s for s in shots if s["id"] in shot_ids]
        else:
            shots = [s for s in shots if s.get("storyboard_kind") == "placeholder"]
        for shot in shots:
            references, missing = frame_references(p, shot)
            if missing:
                raise FilmError(f"{shot['id']}: reference image not found: {missing[0]}")
            references = references[:provider.max_references] if provider.supports_references else []
            if sum(safe_path(p, r).stat().st_size for r in references) > 14 * 1024 * 1024:
                raise FilmError(f"{shot['id']}: reference images exceed one request's size limit; use smaller files")
            prompt = frame_prompt(p, shot, aspect)
            if limit and len(prompt) > limit:
                prompt = compact_prompt(p, shot, aspect, limit)
            items.append({"key": shot["id"], "prompt": prompt, "references": references, "aspect": aspect})
        return items, warnings, aspect
    for folder, name, listkey in (("characters", "characters.yaml", "characters"), ("locations", "locations.yaml", "locations")):
        for entry in _entries(p, name, listkey):
            existing = [r for r in entry.get("reference_images", []) if safe_path(p, r).is_file()]
            if existing or (shot_ids and entry["id"] not in shot_ids):
                continue
            if not _has_description(entry):
                warnings.append(f"{entry['id']}: 설명이 비어 있어 건너뜁니다. 먼저 인물·장소 설명을 채우세요.")
                continue
            prompt = reference_prompt(p, entry, "character" if folder == "characters" else "location", limit)
            items.append({"key": entry["id"], "prompt": prompt, "references": [], "aspect": "1:1", "folder": folder})
    return items, warnings, "1:1"


def _inputs(p, item, provider):
    refs = {r: digest(safe_path(p, r)) for r in item["references"]}
    return object_hash({"provider": provider.id, "model": provider.model, "prompt": item["prompt"],
                        "aspect": item["aspect"], "references": refs})


def estimate(project, kind, provider, shot_ids=None):
    """Quote the images that are missing. No LOCK is needed; approval binds exact inputs."""
    if kind not in KINDS:
        raise FilmError("kind must be references or frames")
    p = Path(project)
    provider.preflight(p)
    config = read(p / "project.yaml")
    retry, cap = config["budget"]["max_retry_per_shot"], config["budget"].get("max_usd", 0)
    if type(retry) is not int or not 0 <= retry <= 10:
        raise FilmError("max_retry_per_shot must be 0–10")
    items, warnings, aspect = _items(p, kind, provider, shot_ids)
    if provider.native_aspects is not None and aspect not in provider.native_aspects:
        raise FilmError(f"이 이미지 모델은 {aspect} 화면비를 만들 수 없습니다")
    if not items:
        raise NothingToDo("만들 이미지가 없습니다" + (": " + " ".join(warnings) if warnings else ""), warnings)
    rows = [{"key": i["key"], "inputs": _inputs(p, i, provider)} for i in items]
    initial = round(provider.unit_usd * len(rows), 6)
    failed = sorted(f.stem for f in (p / "render/image_jobs").glob(f"img_{kind}_*.json")
                    if read(f).get("status") == "FAILED" and read(f).get("key") in {r["key"] for r in rows})
    spec = {"kind": kind, "provider": provider.id, "model": provider.model, "billing_unit": "USD",
            "unit_usd": provider.unit_usd, "aspect_ratio": aspect, "rows": rows, "initial_amount": initial,
            "retry_reserve": round(initial * retry, 6), "worst_case_amount": round(initial * (1 + retry), 6),
            "max_retry_per_shot": retry, "max_amount": cap, "after_failed_jobs": failed,
            "cost_note": "Conservative bound per image; the service bills its own actual price." if provider.unit_usd else "무료 한도 안에서 실행됩니다."}
    spent = reserved_usd(p)
    if spec["worst_case_amount"] and spent + spec["worst_case_amount"] > cap:
        raise FilmError(f"Worst-case {spec['worst_case_amount']} USD plus {spent} USD already reserved "
                        f"exceeds budget {cap} USD; raise budget.max_usd or lower max_retry_per_shot")
    spec["estimate_id"] = object_hash(spec)
    write(p / f"render/images_{kind}_estimate.json", spec)
    return {**spec, "warnings": warnings}


def approve(project, kind, estimate_id):
    p = Path(project)
    current = read(p / f"render/images_{kind}_estimate.json", {})
    if not current or current.get("estimate_id") != estimate_id:
        raise FilmError("Estimate ID does not match the current image estimate")
    write(p / f"render/images_{kind}_approval.json", {"estimate_id": estimate_id, "approved_at": now()})
    return {"approved": estimate_id}


def approved(project, kind, provider):
    """The estimate that may run now, or None when unapproved or its inputs changed."""
    p = Path(project)
    current = read(p / f"render/images_{kind}_estimate.json", {})
    if not current or current.get("provider") != provider.id or current.get("model") != provider.model:
        return None
    if current["worst_case_amount"] and read(p / f"render/images_{kind}_approval.json", {}).get("estimate_id") != current["estimate_id"]:
        return None
    try:
        items, _, _ = _items(p, kind, provider, [r["key"] for r in current["rows"]] if kind == "frames" else None)
    except FilmError:
        return None
    by_key = {i["key"]: i for i in items}
    for row in current["rows"]:
        item = by_key.get(row["key"])
        if item is None or _inputs(p, item, provider) != row["inputs"]:
            return None
    return current


def _import(p, kind, item, image):
    if kind == "frames":
        from .production import import_frame
        import_frame(p, item["key"], image)
        return
    with project_mutex(p):
        relative = f"{item['folder']}/{item['key']}.png"
        target = safe_path(p, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(image.read_bytes())
        name = "characters.yaml" if item["folder"] == "characters" else "locations.yaml"
        listkey = item["folder"]
        document = read(p / "bible" / name)
        for entry in document[listkey]:
            if entry["id"] == item["key"] and relative not in entry.setdefault("reference_images", []):
                entry["reference_images"].append(relative)
        write(p / "bible" / name, document)


def generate(project, kind, provider):
    """Make every approved image once each; import the results."""
    p = Path(project)
    provider.preflight(p)
    plan = approved(p, kind, provider)
    if plan is None:
        raise FilmError("현재 견적을 승인하세요. 이미지 입력이 견적 이후 바뀌었을 수도 있습니다.")
    free = provider.unit_usd == 0
    items, _, _ = _items(p, kind, provider, [r["key"] for r in plan["rows"]] if kind == "frames" else None)
    by_key = {i["key"]: i for i in items}
    result = {"generated": [], "failed": [], "already_done": []}
    for row in plan["rows"]:
        item = by_key[row["key"]]
        for attempt in range(plan["max_retry_per_shot"] + 1):
            job_id = f"img_{kind}_{plan['estimate_id'][:12]}_{item['key']}_a{attempt}"
            path = safe_path(p, f"render/image_jobs/{job_id}.json")
            receipt = read(path, {})
            status = receipt.get("status")
            if status == "FAILED" or (status == "SUBMITTING" and free):
                continue
            if status == "IMPORTED":
                result["already_done"].append(item["key"])
                break
            if status == "SUBMITTING":
                raise RenderBlocked(f"Image job {job_id} has an unknown outcome; check the provider's usage page before "
                                    "continuing. It is never resubmitted automatically")
            if status != "COMPLETED":
                with project_mutex(p):
                    if not free:
                        budget.reserve(p, job_id, provider.unit_usd, plan, "USD")
                    receipt = {"job_id": job_id, "key": item["key"], "kind": kind, "attempt": attempt,
                               "provider": provider.id, "model": provider.model, "input_hash": row["inputs"],
                               "status": "SUBMITTING", "usd_reserved": provider.unit_usd, "submitted_at": now()}
                    write(path, receipt)  # Written BEFORE the request, including timeout ambiguity.
                    try:
                        data = provider.generate(item["prompt"], [safe_path(p, r) for r in item["references"]], item["aspect"])
                    except ImageRejected as exc:
                        path.unlink()  # Definitive rejection: nothing was created or billed.
                        raise RenderBlocked(f"{exc}; 해결한 뒤 다시 실행하세요") from None
                    except Exception:
                        if free:
                            receipt.update(status="FAILED", error="연결 실패 또는 시간 초과")
                            write(path, receipt)
                            continue
                        raise RenderBlocked(f"Image job {job_id} needs reconciliation; do not resubmit") from None
                    try:
                        png = fit_aspect(data, item["aspect"], provider.native_aspects)
                    except Exception:
                        receipt.update(status="FAILED", error="unreadable image")
                        write(path, receipt)
                        continue
                    image = safe_path(p, f"render/image_jobs/{job_id}.img")
                    image.write_bytes(png)
                    receipt.update(status="COMPLETED", image_path=str(image.relative_to(p)), sha256=digest(image), completed_at=now())
                    write(path, receipt)
            image = safe_path(p, receipt["image_path"])
            if digest(image) != receipt["sha256"]:
                raise RenderBlocked(f"Saved image for {job_id} changed; recover it before spending again")
            _import(p, kind, item, image)
            receipt.update(status="IMPORTED", imported_at=now())
            write(path, receipt)
            result["generated"].append(item["key"])
            break
        else:
            result["failed"].append(item["key"])
    return result
