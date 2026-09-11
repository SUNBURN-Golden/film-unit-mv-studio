"""Cost-first routing over explicitly configured, priced provider profiles.

Amounts in different billing units are NEVER compared or added. Provider order is
an operator preference; within a provider, the lowest valid quote is tried first.
"""
from pathlib import Path
import copy
import math
import re
from .core import FilmError, object_hash, read, safe_path, write
from .renderers import VideoRenderer, get_renderer


def initialize(project):
    p = Path(project)
    path = p / "render/economy.json"
    if path.exists():
        raise FilmError("Economy policy exists; edit its profiles instead of overwriting it")
    profiles = []
    for provider in ("openart", "fal"):
        config = f"render/{provider}_config.json"
        if (p / config).exists():
            profiles.append({"id": provider, "provider": provider, "config_path": config})
    policy = {"version": 1, "provider_order": ["openart", "fal"], "profiles": profiles,
        "same_model_retries": 1, "shots": {}, "note": "Only verified configurations are candidates. No paid calls or approval performed."}
    write(path, policy)
    return policy


class SelectedRenderer(VideoRenderer):
    """Pins the chosen profile without mutating the locked shot manifest."""
    def __init__(self, base, profile):
        super().__init__(base.project, base.fmt)
        self.base, self.profile = base, profile
        self.name, self.billing_unit, self.quality = base.name, base.billing_unit, base.quality

    def pinned(self, shot):
        s = copy.deepcopy(shot)
        s["renderer"] = self.base.name
        return s

    def quote(self, shot, quality):
        return self.base.quote(self.pinned(shot), quality)

    def preflight(self):
        return self.base.preflight()

    def input_hash(self, shot, attempt, job_id):
        return self.base.input_hash(self.pinned(shot), attempt, job_id)

    def generation_config(self, shot):
        return self.base.generation_config(self.pinned(shot))

    def render(self, shot, target, attempt=0, correction="", job_id=""):
        return self.base.render(self.pinned(shot), target, attempt, correction, job_id)


class EconomyRenderer(VideoRenderer):
    name = "economy"
    billing_unit = "mixed"

    def __init__(self, project, fmt):
        super().__init__(project, fmt)
        self.policy = read(self.project / "render/economy.json", {})
        self.profiles, self.plans, self.exclusions = {}, {}, {}

    def load_profiles(self):
        if not self.policy or not self.policy.get("profiles"):
            raise FilmError("Configure at least one verified profile in render/economy.json")
        order = self.policy.get("provider_order", [])
        if len(order) != len(set(order)) or any(x not in {"fal", "openart"} for x in order):
            raise FilmError("provider_order must list unique fal/openart providers")
        for profile in self.policy["profiles"]:
            identity, provider = profile["id"], profile["provider"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity) or identity in self.profiles:
                raise FilmError("Profile IDs must be unique simple names")
            if provider not in order:
                raise FilmError("Each profile provider must be listed in provider_order")
            base = get_renderer(provider, self.project, self.fmt, self.quality)
            base.config = read(safe_path(self.project, profile["config_path"]))
            self.profiles[identity] = SelectedRenderer(base, identity)

    def schedule(self, shot, count):
        if not self.profiles:
            self.load_profiles()
        policy_shots = self.policy.get("shots", {})
        explicit = policy_shots.get(shot["id"])
        if explicit is not None and (not explicit or not isinstance(explicit, list)):
            raise FilmError("Shot profile override must be a nonempty list")
        allowed = explicit if explicit is not None else list(self.profiles)
        selected, errors = [], []
        requested = shot.get("renderer", "auto")
        for identity in allowed:
            if identity not in self.profiles:
                raise FilmError(f"Unknown profile {identity}")
            renderer = self.profiles[identity]
            # An explicit provider/model in the locked shot is a hard restriction.
            if requested not in {"auto", "economy", renderer.name}:
                if renderer.name == "fal":
                    model = renderer.base.config.get("endpoint")
                else:
                    model = renderer.base.config.get("shots", {}).get(shot["id"], {}).get(self.quality, {}).get("model")
                if requested != model:
                    continue
            try:
                amount = renderer.quote(shot, self.quality)
                if not math.isfinite(amount) or amount < 0:
                    raise FilmError("Quote must be finite and nonnegative")
                selected.append((identity, amount))
            except (FilmError, KeyError, ValueError, TypeError) as exc:
                if explicit is not None:
                    raise FilmError(f"Explicit profile {identity} is invalid: {exc}") from exc
                errors.append({"profile": identity, "reason": str(exc)})
        self.exclusions[shot["id"]] = errors
        if not selected:
            raise FilmError(f"{shot['id']}: no compatible verified quote; {errors}")
        if explicit is None:
            order = self.policy["provider_order"]
            selected.sort(key=lambda row: (order.index(self.profiles[row[0]].name), row[1], row[0]))
        repeat = self.policy.get("same_model_retries", 1)
        if type(repeat) is not int or not 0 <= repeat <= 10:
            raise FilmError("same_model_retries must be 0–10")
        sequence = [selected[0]] * (1 + repeat) + selected[1:]
        sequence += [sequence[-1]] * count
        return sequence[:count]

    def make_estimate(self, shots, fingerprint):
        config = read(self.project / "project.yaml")
        retry = config["budget"]["max_retry_per_shot"]
        if type(retry) is not int or not 0 <= retry <= 10:
            raise FilmError("max_retry_per_shot must be 0–10")
        pools = {unit: {"initial_amount": 0, "worst_case_amount": 0,
            "max_amount": config["budget"].get(cap, 0)} for unit, cap in [("USD", "max_usd"), ("credits", "max_credits")]}
        for pool in pools.values():
            cap = pool["max_amount"]
            if not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap < 0:
                raise FilmError("Budget must be finite and nonnegative")
        rows = []
        for shot in shots:
            attempts = []
            if shot["render_mode"] != "STATIC":
                if shot.get("renderer") == "mock":
                    raise FilmError("Economy animation cannot substitute a moving shot with Mock")
                schedule = self.schedule(shot, retry + 1)
                self.plans[shot["id"]] = [row[0] for row in schedule]
                for index, (identity, amount) in enumerate(schedule):
                    renderer = self.profiles[identity]
                    pools[renderer.billing_unit]["worst_case_amount"] += amount
                    if index == 0:
                        pools[renderer.billing_unit]["initial_amount"] += amount
                    attempts.append({"attempt": index, "profile": identity, "provider": renderer.name,
                        "billing_unit": renderer.billing_unit, "amount": amount,
                        "provider_config": renderer.base.config_hash()})
            rows.append({"shot": shot["id"], "in_ms": shot["in_ms"], "out_ms": shot["out_ms"], "attempts": attempts})
        for unit, pool in pools.items():
            for k in ["initial_amount", "worst_case_amount"]:
                pool[k] = round(pool[k], 6)
            pool["retry_reserve"] = round(pool["worst_case_amount"] - pool["initial_amount"], 6)
            if pool["worst_case_amount"] > pool["max_amount"]:
                raise FilmError(f"Worst-case {pool['worst_case_amount']} {unit} exceeds budget {pool['max_amount']} {unit}")
        spec = {"production": fingerprint, "renderer": self.name, "quality": self.quality,
            "billing_unit": "mixed", "pools": pools, "rows": rows, "max_retry_per_shot": retry,
            "policy": self.policy, "excluded_candidates": self.exclusions,
            "qc_policy": config.get("qc", {}), "cost_note": "Conservative bound. Cached paid takes incur no new generation charge. Provider pools are not currency conversions."}
        # Exclusion error text is diagnostic, not an input to a billed job.
        spec["estimate_id"] = object_hash({k: v for k, v in spec.items() if k != "excluded_candidates"})
        write(self.project / "render/estimate.json", spec)
        return spec

    def for_attempt(self, shot, attempt):
        return self.profiles[self.plans[shot["id"]][attempt]]

    def quote(self, shot, quality):
        raise FilmError("Economy uses separate USD and credit estimates")

    def render(self, *args, **kwargs):
        raise FilmError("Select the approved attempt profile first")
