"""FRAME_ANIMATION_V1 AI subscription execution binding (ANIM-016,
exec/storage §7, schema §15/§17).

An AI subscription (ChatGPT code runtime, hosted notebook, ...) is an
execution environment the *user* drives inside the service's own UI — it
is not an API. This module implements:

- `subscription_entitlement` records: a secret-free account binding, a
  credential epoch, and allowance buckets that stay separate forever
  (subscription units / compute units / API credits / USD / human
  hand-off minutes are never summed into one currency);
- probe → `capability_evidence` records for a subscription runtime:
  observed environment, I/O limits, network (none), GPU (none), Drive
  reach, scratch cap, cancel/complete method and a fixture result;
- the routing gates: AUTOMATIC vs MANUAL labelling (a consumer site with
  no official execution API is MANUAL only — no UI automation, no
  standing daemon, no polling), extra-charge refusal, exhaustion stop,
  and the substitute account/provider fence;
- `SubscriptionWorker`, the MANUAL-only WorkerProtocol surface, and
  `FakeSubscriptionRuntime`, the test double that actually executes the
  shipped worker script in a subprocess.

Nothing here contacts a real service, stores a credential, or infers
qualification. Evidence from the fake runtime is marked FAKE and every
route reports qualification_state UNQUALIFIED — real qualification needs
real subscription access that does not exist in this repository.
"""
from pathlib import Path
import hashlib
import os
import re
import subprocess
import sys

from ..animation_schema import (canon_bytes, check_document, read_canon,
                                write_canon)
from ..core import FilmError, now
from ..storage_backends import ConnectionDropped
from . import Worker, expected_frame_digest

ENTITLEMENT_TYPE = "subscription_entitlement"
ENTITLEMENT_VERSION = 1

# Allowance buckets are tracked separately and never summed — a
# subscription minute is not a dollar (exec/storage §7.2, §11).
ALLOWANCE_UNITS = ("subscription_units", "compute_units", "api_credits",
                   "usd", "handoff_minutes")
UNIT_MARKERS = {"UNKNOWN", "UNLIMITED", "NOT_APPLICABLE"}
# Units that would charge money beyond the subscription itself.
PAID_UNITS = {"api_credits", "usd"}

USAGE_PATHS = {"CODE_RUNTIME_FILE_PACKET", "HOSTED_NOTEBOOK_UI"}
INCLUSION_STATES = {"CONFIRMED", "UNKNOWN", "NO"}
ROUTE_LABELS = {"AUTOMATIC", "MANUAL"}
VERDICTS = {"COVERED", "EXTRA_CHARGE_APPROVED", "ADDITIONAL_CHARGE_REFUSED",
            "ALLOWANCE_EXHAUSTED", "INCLUSION_UNKNOWN"}

ENTITLEMENT_FIELDS = {"document_type", "schema_version", "entitlement_id",
                      "service", "usage_path", "account_binding",
                      "credential_epoch", "official_execution_api",
                      "allowance", "used", "inclusion", "prices",
                      "service_caps", "observed_at", "expires_at_ms",
                      "notes"}
CAP_FIELDS = {"max_input_bytes", "max_output_bytes", "max_file_count",
              "max_frames_per_packet", "max_scratch_bytes", "network",
              "gpu", "drive_read", "drive_write", "runtimes",
              "cancel_method", "complete_method"}

_SHA_RE = re.compile(r"[0-9a-f]{64}")

# The shipped worker script may only ever use a pure-stdlib, offline
# subset; anything else means the artifact is not what the app ships.
FORBIDDEN_SCRIPT_TOKENS = (
    "import socket", "import urllib", "from urllib", "import http",
    "from http", "import requests", "import subprocess",
    "from subprocess", "import ctypes", "from ctypes", "import ssl",
    "import ftplib", "import smtplib", "import asyncio",
    "import socketserver", "import websocket", "import telnetlib",
    "import aiohttp", "__import__", "eval(", "exec(", "os.system",
    "os.popen",
)
WORKER_SCRIPT_MODULE = "engine.subscription_worker"


def _sha(value, what, nullable=False):
    if value is None and nullable:
        return value
    if type(value) is not str or not _SHA_RE.fullmatch(value):
        raise FilmError(f"{what} must be a lowercase SHA-256")
    return value


def _int(value, what, minimum=0):
    if type(value) is not int or value < minimum:
        raise FilmError(f"{what} must be an integer >= {minimum}")
    return value


def _str(value, what):
    if type(value) is not str or not value:
        raise FilmError(f"{what} must be a non-empty string")
    return value


# -- worker script ------------------------------------------------------------

def worker_script_source():
    """The exact bytes of the worker script the app ships."""
    return Path(__file__).resolve().parent.parent.joinpath(
        "subscription_worker.py").read_bytes()


def worker_script_sha256():
    return hashlib.sha256(worker_script_source()).hexdigest()


def worker_script_version():
    from .. import subscription_worker
    return subscription_worker.WORKER_SCRIPT_VERSION


def audit_worker_source(source=None):
    """Static audit of the shipped worker: no network, no subprocess, no
    dynamic execution. A packet never downloads or runs arbitrary code —
    it carries this audited file and pins its hash."""
    source = source if source is not None else worker_script_source()
    text = source.decode("utf-8")
    for token in FORBIDDEN_SCRIPT_TOKENS:
        if token in text:
            raise FilmError(
                f"WORKER_SCRIPT_AUDIT: the shipped worker contains "
                f"{token!r}; the runtime contract is no network, no "
                f"subprocess, no dynamic execution")
    return True


# -- entitlement records --------------------------------------------------------

def _unit_value(value, what):
    if type(value) is int and value >= 0:
        return value
    if value in UNIT_MARKERS:
        return value
    raise FilmError(
        f"{what} must be a non-negative integer or one of "
        f"{sorted(UNIT_MARKERS)}; allowance units stay separate and are "
        "never summed")


def validate_entitlement(document):
    check_document(document, ENTITLEMENT_TYPE)
    if set(document.keys()) != ENTITLEMENT_FIELDS:
        raise FilmError(f"subscription_entitlement must hold exactly "
                        f"{sorted(ENTITLEMENT_FIELDS)}")
    _str(document["entitlement_id"], "entitlement_id")
    _str(document["service"], "entitlement.service")
    if document["usage_path"] not in USAGE_PATHS:
        raise FilmError(f"usage_path must be one of {sorted(USAGE_PATHS)}")
    # Secret-free binding only: a digest, never an email or a token.
    _sha(document["account_binding"], "entitlement.account_binding")
    _int(document["credential_epoch"], "credential_epoch", 1)
    if type(document["official_execution_api"]) is not bool:
        raise FilmError("official_execution_api must be a boolean — a "
                        "consumer site without an official execution API "
                        "is MANUAL only")
    allowance = document["allowance"]
    if type(allowance) is not dict or set(allowance.keys()) != set(
            ALLOWANCE_UNITS):
        raise FilmError("allowance must carry every unit separately: "
                        + ", ".join(ALLOWANCE_UNITS))
    for unit, value in allowance.items():
        _unit_value(value, f"allowance.{unit}")
    used = document["used"]
    if type(used) is not dict or set(used.keys()) != set(ALLOWANCE_UNITS):
        raise FilmError("used must carry every unit separately")
    for unit, value in used.items():
        _int(value, f"used.{unit}")
    inclusion = document["inclusion"]
    if type(inclusion) is not dict \
            or inclusion.get("execution_in_subscription") \
            not in INCLUSION_STATES:
        raise FilmError("inclusion.execution_in_subscription must be "
                        "CONFIRMED, UNKNOWN or NO")
    if inclusion["execution_in_subscription"] == "UNKNOWN" \
            and not inclusion.get("note"):
        raise FilmError("UNKNOWN inclusion must carry a note — it is "
                        "never shown as 'no extra charge'")
    prices = document["prices"]
    if prices is not None and (type(prices) is not dict or any(
            type(k) is not str or type(v) is not int or v < 0
            for k, v in prices.items())):
        raise FilmError("prices must be null or a map of unit -> integer "
                        "micro-USD (CANON_JSON_V1 has no floats)")
    caps = document["service_caps"]
    if type(caps) is not dict or set(caps.keys()) - CAP_FIELDS:
        raise FilmError("service_caps holds only the declared limit fields")
    for field in ("max_input_bytes", "max_output_bytes", "max_file_count",
                  "max_frames_per_packet", "max_scratch_bytes"):
        if caps.get(field) is not None:
            _int(caps[field], f"service_caps.{field}", 1)
    runtimes = caps.get("runtimes")
    if runtimes is not None and (type(runtimes) is not list
                                 or any(type(r) is not str for r in runtimes)):
        raise FilmError("service_caps.runtimes must be a list of probed "
                        "runtime contract ids")
    if type(document["observed_at"]) is not str \
            or not document["observed_at"]:
        raise FilmError("observed_at is required")
    if document["expires_at_ms"] is not None:
        _int(document["expires_at_ms"], "expires_at_ms", 1)
    if type(document["notes"]) is not str:
        raise FilmError("notes must be a string")
    return document


def make_entitlement(service, *, usage_path, account_binding,
                     credential_epoch=1, official_execution_api=False,
                     allowance=None, inclusion=None, prices=None,
                     service_caps=None, expires_at_ms=None, notes="",
                     entitlement_id=None, observed_at=None):
    """Assemble and validate a `subscription_entitlement` record."""
    allowance = dict(allowance or {})
    for unit in ALLOWANCE_UNITS:
        allowance.setdefault(unit, "UNKNOWN")
    doc = {"document_type": ENTITLEMENT_TYPE,
           "schema_version": ENTITLEMENT_VERSION,
           "entitlement_id": entitlement_id or
           "ENT-" + hashlib.sha256(canon_bytes(
               {"service": service,
                "account_binding": account_binding})).hexdigest()[:12].upper(),
           "service": service, "usage_path": usage_path,
           "account_binding": account_binding,
           "credential_epoch": credential_epoch,
           "official_execution_api": official_execution_api,
           "allowance": allowance,
           "used": {unit: 0 for unit in ALLOWANCE_UNITS},
           "inclusion": inclusion or {
               "execution_in_subscription": "UNKNOWN",
               "note": "the subscription's inclusion of this runtime was "
                       "never confirmed — not reported as free"},
           "prices": prices,
           "service_caps": dict(service_caps or {}),
           "observed_at": observed_at or now(),
           "expires_at_ms": expires_at_ms,
           "notes": notes}
    return validate_entitlement(doc)


def entitlement_sha(document):
    return hashlib.sha256(canon_bytes(document)).hexdigest()


def quote_digest(entitlement):
    """Digest over the charge-relevant terms: allowance, inclusion and
    prices. Any change makes a previously issued packet's quote stale —
    the import refuses it and a new packet needs a fresh export."""
    return hashlib.sha256(canon_bytes({
        "allowance": entitlement["allowance"],
        "inclusion": entitlement["inclusion"],
        "prices": entitlement["prices"],
        "official_execution_api": entitlement["official_execution_api"],
    })).hexdigest()


def subscriptions_dir(state_dir):
    return Path(state_dir) / "subscriptions"


def entitlement_path(state_dir, service):
    return subscriptions_dir(state_dir) / f"{service}.json"


def save_entitlement(state_dir, document):
    path = entitlement_path(state_dir, document["service"])
    write_canon(path, document)
    return path


def load_entitlement(state_dir, service):
    return validate_entitlement(
        read_canon(entitlement_path(state_dir, service)))


def rebind_entitlement(state_dir, service):
    """Re-login / account switch / session expiry: the credential epoch
    increments and every grant/packet bound to the old epoch is void —
    existing jobs are never re-identified (schema §17)."""
    doc = load_entitlement(state_dir, service)
    doc["credential_epoch"] += 1
    doc["observed_at"] = now()
    save_entitlement(state_dir, doc)
    return doc


def remaining(entitlement, unit):
    allowance = entitlement["allowance"][unit]
    if type(allowance) is int:
        return allowance - entitlement["used"][unit]
    return allowance


def charge_check(entitlement, cost, *, allow_additional_charges=False,
                 additional_charges_approved=False):
    """Whether `cost` ({unit: amount}) can run on this entitlement.

    Verdicts are explicit: INCLUSION_UNKNOWN is never reported as "no
    extra charge", a paid unit is refused while the plan does not allow
    additional charges (and even then only with a separate approval),
    and exhaustion just stops — nothing here reroutes to another account
    or provider.
    """
    detail = {}
    needs_paid = False
    for unit, amount in sorted(cost.items()):
        if unit not in ALLOWANCE_UNITS:
            raise FilmError(f"Unknown allowance unit: {unit}")
        amount = _int(amount, f"cost.{unit}")
        if unit in PAID_UNITS and amount:
            needs_paid = True
            detail[unit] = "REQUIRES_EXTRA_CHARGE"
            continue
        allowance = entitlement["allowance"][unit]
        if allowance == "UNKNOWN" and amount:
            return {"verdict": "INCLUSION_UNKNOWN", "detail": detail,
                    "reason": f"{unit} inclusion is UNKNOWN — never "
                              "presented as 'no extra charge'"}
        if allowance == "NOT_APPLICABLE" and amount:
            return {"verdict": "ALLOWANCE_EXHAUSTED", "detail": detail,
                    "reason": f"{unit} is NOT_APPLICABLE on this "
                              "subscription"}
        if type(allowance) is int and amount:
            left = allowance - entitlement["used"][unit]
            if left < amount:
                return {"verdict": "ALLOWANCE_EXHAUSTED", "detail": detail,
                        "reason": f"{unit}: {amount} needed, {left} left "
                                  "— execution stops; no reroute to "
                                  "another account, provider or paid API"}
            detail[unit] = f"{amount} of {left} remaining"
    if needs_paid and not allow_additional_charges:
        return {"verdict": "ADDITIONAL_CHARGE_REFUSED", "detail": detail,
                "reason": "the plan keeps allow_additional_charges=false"}
    if needs_paid and not additional_charges_approved:
        return {"verdict": "ADDITIONAL_CHARGE_REFUSED", "detail": detail,
                "reason": "allow_additional_charges=true still needs a "
                          "separate explicit approval"}
    verdict = "EXTRA_CHARGE_APPROVED" if needs_paid else "COVERED"
    return {"verdict": verdict, "detail": detail, "reason": ""}


def consume(entitlement, cost):
    """Book the cost against `used`. Reserving is not refunding — a
    failed or cancelled run's allowance is not invented back."""
    verdict = charge_check(entitlement, cost,
                           allow_additional_charges=True,
                           additional_charges_approved=True)
    if verdict["verdict"] not in {"COVERED", "EXTRA_CHARGE_APPROVED"}:
        raise FilmError(f"{verdict['verdict']}: {verdict['reason']}")
    for unit, amount in cost.items():
        if type(entitlement["allowance"][unit]) is int:
            entitlement["used"][unit] += amount
    return entitlement


# -- probe -> capability evidence ------------------------------------------------

def _adapter_digest(service):
    """Digest of the provider/adapter pair actually probed — the worker
    script's own bytes pin the adapter, never a name."""
    return hashlib.sha256(canon_bytes({
        "driver": "QUALIFIED_SERVICE", "service": service,
        "worker_script_sha256": worker_script_sha256(),
        "worker_script_version": worker_script_version()})).hexdigest()


def capability_dir(state_dir):
    return Path(state_dir) / "capability"


def probe_subscription(service, *, entitlement=None, runtime=None,
                       work_dir=None, observed_at=None):
    """Probe one subscription runtime into a `capability_evidence` 1 doc.

    `runtime=None` is the honest state of a real service from this
    repository: there is no access, nothing was executed, and the record
    stays DOCUMENTED_ONLY — a service name or its public docs never make
    QUALIFIED_FOR_SCOPE. A FakeSubscriptionRuntime actually executes the
    shipped worker script on a tiny fixture and reports what it observed;
    that evidence is marked FAKE and still DOCUMENTED_ONLY for the real
    service. LLM reasoning can describe a plan; it can never produce this
    evidence — only a real probe run writes `fixture`/`environment`.
    """
    probe_result = runtime.probe_fixture(work_dir) if runtime is not None \
        else None
    observed = (probe_result or {}).get("environment") or {
        "service": service, "runtime": "UNOBSERVED",
        "session_id": None, "device": None, "os": None, "driver": None,
        "network": "UNKNOWN", "gpu": "UNKNOWN",
        "drive_read": "UNKNOWN", "drive_write": "UNKNOWN",
        "fake_runtime": False}
    document = {
        "document_type": "capability_evidence",
        "schema_version": 1,
        "evidence_id": "",
        "driver": "QUALIFIED_SERVICE",
        "adapter_digest": _adapter_digest(service),
        "account_binding": entitlement["account_binding"]
        if entitlement else None,
        "credential_epoch": entitlement["credential_epoch"]
        if entitlement else 0,
        "environment": observed,
        "operation": "compose_frame_range",
        "scope": (probe_result or {}).get("scope"),
        "caps": (probe_result or {}).get("caps")
        or (entitlement or {}).get("service_caps") or {},
        "route": "MANUAL_PACKET",
        "transport": "manual-file-handoff",
        "fixture": (probe_result or {}).get("fixture"),
        "observed_at": observed_at or now(),
        "entitlement_basis": (
            "subscription account binding "
            f"{entitlement['account_binding'][:12]}… — secret-free"
            if entitlement else "no account bound"),
        "allowance": dict(entitlement["allowance"]) if entitlement else {},
        "expiry": "STALE on any session, device, OS, driver, account, "
                  "credential-epoch, route or limit change, or at "
                  "session expiry — re-probe or wait for the user",
        "registry_state": "DOCUMENTED_ONLY",
        "qualification": ("FAKE — test double executed the shipped worker "
                          "script; proves the packet protocol, never the "
                          "real service" if probe_result else
                          "unprobed real service — no subscription access "
                          "exists in this repository"),
        "reason": ((probe_result or {}).get("reason")
                   or "documented protocol only; nothing observed"),
    }
    document["evidence_id"] = "CE-" + hashlib.sha256(
        canon_bytes(document)).hexdigest()[:16].upper()
    from ..animation_schema import validate_capability_evidence
    return validate_capability_evidence(document)


def save_evidence(state_dir, evidence):
    path = capability_dir(state_dir) / f"{evidence['evidence_id']}.json"
    write_canon(path, evidence)
    return path


def list_evidence(state_dir):
    folder = capability_dir(state_dir)
    if not folder.is_dir():
        return []
    return [read_canon(p) for p in sorted(folder.glob("CE-*.json"))]


def staleness_reasons(evidence, observation=None, *, now_ms=None):
    """Why a stored probe no longer applies (schema §15 / §7.2.1).

    `observation` is a fresh probe-shaped dict (environment/caps/
    account_binding/credential_epoch). Any change in session, device,
    driver, account, route or limits — or an expired session — makes the
    evidence STALE; the remedy is re-probe or an explicit user wait,
    never silent reuse.
    """
    reasons = []
    env = evidence.get("environment") or {}
    if now_ms is not None and env.get("session_expires_at_ms") is not None \
            and now_ms >= env["session_expires_at_ms"]:
        reasons.append("session_expired")
    if observation is None:
        return reasons
    other = observation.get("environment") or {}
    for field in ("session_id", "device", "os", "runtime", "driver"):
        if env.get(field) != other.get(field):
            reasons.append(f"environment.{field} changed")
    if evidence.get("account_binding") != observation.get("account_binding"):
        reasons.append("account_binding changed")
    if evidence.get("credential_epoch") != observation.get("credential_epoch"):
        reasons.append("credential_epoch changed")
    if evidence.get("caps") != observation.get("caps"):
        reasons.append("limits changed")
    if evidence.get("route") != observation.get("route") \
            or evidence.get("transport") != observation.get("transport"):
        reasons.append("route changed")
    return reasons


def evidence_registry_state(evidence, observation=None, *, now_ms=None):
    """DOCUMENTED_ONLY / QUALIFIED_FOR_SCOPE / STALE / UNAVAILABLE with
    reasons — never a program facet enum, never inferred."""
    reasons = staleness_reasons(evidence, observation, now_ms=now_ms)
    if reasons:
        return "STALE", reasons
    return evidence["registry_state"], []


SCOPE_EXACT = ("service", "usage_path", "operation", "runtime_contract",
               "pixel_format", "network", "gpu", "route")
SCOPE_BOUNDS = ("frames", "max_input_bytes")


def subscription_scope_covered(evidence_scope, requested_scope):
    """Whether `requested_scope` stays inside a scope a probe actually
    ran — same service/usage/operation/runtime contract/pixel format/
    network/gpu/route, and frame/byte counts no larger than probed. A
    CPU compose probe never covers a GPU encode; a 2-frame fixture never
    covers a 5,760-frame job (no superset inference)."""
    if not evidence_scope or not requested_scope:
        return False
    for key in SCOPE_EXACT:
        if evidence_scope.get(key) != requested_scope.get(key):
            return False
    for key in SCOPE_BOUNDS:
        have = evidence_scope.get(key)
        want = requested_scope.get(key)
        if want is None:
            continue
        if type(want) is not int or type(have) is not int or want > have \
                or want < 0:
            return False
    return True


def route_label(entitlement):
    """AUTOMATIC only for a service with a real official execution API;
    a consumer site is MANUAL — the user runs the shipped script inside
    the service UI. No UI automation, no standing daemon, no polling."""
    return "AUTOMATIC" if entitlement["official_execution_api"] else "MANUAL"


def subscription_worker_id(service, account_binding):
    _sha(account_binding, "account_binding")
    return f"subscription:{service}:{account_binding[:12]}"


def assert_same_binding(entitlement, worker):
    """The substitute-submission fence: the packet may only go to the
    worker bound to this exact entitlement — same service, same
    secret-free account binding, same credential epoch. Exhaustion,
    UNKNOWN acceptance or a resource refusal never auto-reroutes to
    another account, another provider or a paid API."""
    if worker.service != entitlement["service"]:
        raise FilmError("SUBSTITUTE_PROVIDER_FENCED: the packet targets "
                        f"{entitlement['service']}, not {worker.service}")
    if worker.account_binding != entitlement["account_binding"]:
        raise FilmError("SUBSTITUTE_ACCOUNT_FENCED: a different account "
                        "may not take this job")
    if worker.credential_epoch != entitlement["credential_epoch"]:
        raise FilmError("CREDENTIAL_EPOCH_STALE: the session changed; "
                        "re-probe or re-bind instead of submitting "
                        "against a voided epoch")
    return True


class SubscriptionWorker(Worker):
    """MANUAL-only WorkerProtocol surface for a subscription runtime.

    There is no official execution API: `submit`/`preflight`/
    `attach_resume` refuse — the packet is run by the user inside the
    service UI and the result comes back through the explicit import.
    `status` reports AWAITING_USER, which the coordinator does not treat
    as an answer: an UNKNOWN manual job settles only when the user
    imports a result or confirms abandonment.
    """

    kind = "SUBSCRIPTION_CODE_RUNTIME"
    evidence_class = "SUBSCRIPTION_PACKET"
    qualification_state = "UNQUALIFIED"

    def __init__(self, entitlement, **kwargs):
        self.entitlement = dict(entitlement)
        self.service = entitlement["service"]
        self.account_binding = entitlement["account_binding"]
        worker_id = kwargs.pop("worker_id", None) or subscription_worker_id(
            self.service, self.account_binding)
        super().__init__(worker_id,
                         endpoint=f"manual-packet://{self.service}",
                         credential_epoch=entitlement["credential_epoch"],
                         **kwargs)

    def _manual_only(self, *args, **kwargs):
        raise FilmError(
            "MANUAL_ONLY: this subscription has no official execution "
            "API; the user runs the shipped packet inside the service UI "
            "and the app imports the result — never UI automation, never "
            "a standing daemon, never polling")

    preflight = _manual_only
    submit = _manual_only
    attach_resume = _manual_only

    def status(self, request_id):
        # Not an answer the coordinator can act on — only the user's
        # explicit import settles a manual run.
        return {"state": "AWAITING_USER", "receipts": []}

    def collect_receipts(self, request_id):
        return []

    def cancel(self, request_id):
        # The app cannot signal the service UI; from the coordinator's
        # view nothing was ever accepted.
        return {"outcome": "NOT_FOUND", "receipts": []}

    def verify_artifact(self, request_id):
        raise FilmError("MANUAL_ONLY: artifacts are verified through "
                        "imported outputs, not a service query")

    def release_workspace(self, job_key):
        return True

    def probe(self):
        return {"worker_id": self.worker_id, "kind": self.kind,
                "service": self.service,
                "route_label": route_label(self.entitlement),
                "qualification_state": self.qualification_state,
                "evidence_class": self.evidence_class,
                "official_execution_api":
                    self.entitlement["official_execution_api"]}


class FakeSubscriptionRuntime(SubscriptionWorker):
    """FAKE subscription runtime for tests.

    It actually executes the shipped worker script — the exact bytes the
    packet carries — in a subprocess inside a sandboxed temp dir, on a
    tiny frame range, and returns real outputs + receipts. No network is
    needed and none is used (the script is audited for it). Fault
    injection: `expire_session()` voids the session mid-run, `dead` drops
    the runtime, `remaining_units` bounds the subscription allowance.
    Every result is FAKE/UNQUALIFIED — it proves the packet protocol,
    never the real service.
    """

    evidence_class = "FAKE_SUBSCRIPTION"

    def __init__(self, entitlement, *, python=None, session_epoch=None,
                 remaining_units=None, scratch_limit_bytes=None, **kwargs):
        super().__init__(entitlement, **kwargs)
        self.python = python or sys.executable
        self.session_epoch = session_epoch if session_epoch is not None \
            else entitlement["credential_epoch"]
        self.remaining_units = remaining_units
        self.scratch_limit_bytes = scratch_limit_bytes
        self.dead = False
        self._runs = {}

    def _live(self):
        if self.dead:
            raise ConnectionDropped("fake subscription runtime is "
                                    "unreachable")
        if self.session_epoch != self.entitlement["credential_epoch"]:
            raise FilmError("SESSION_EXPIRED: the runtime session changed; "
                            "re-attach explicitly — a run is never "
                            "resubmitted under a new identity")

    def execute_packet(self, packet_dir, result_dir, *, count_usage=True,
                       timeout=120):
        """Run the packet's shipped script for real, in a subprocess."""
        self._live()
        packet = read_canon(Path(packet_dir) / "packet.json")
        start, end = packet["execution_unit"]["frame_range"]
        frames = end - start
        if count_usage and self.remaining_units is not None:
            if self.remaining_units < frames:
                raise FilmError(
                    "ALLOWANCE_EXHAUSTED: the subscription allowance is "
                    "used up — the run stops; no other account, provider "
                    "or paid API is tried")
            self.remaining_units -= frames
        script = Path(packet_dir) / packet["worker"]["script"]
        if not script.is_file():
            raise FilmError("packet is missing its worker script")
        if hashlib.sha256(script.read_bytes()).hexdigest() \
                != packet["worker"]["sha256"]:
            raise FilmError("packet worker script hash mismatch — never "
                            "runs substituted code")
        proc = subprocess.run(
            [self.python, str(script), str(packet_dir), str(result_dir)],
            capture_output=True, timeout=timeout, cwd=str(packet_dir),
            env={"PATH": os.environ.get("PATH", ""),
                 "PYTHONIOENCODING": "utf-8",
                 "PYTHONDONTWRITEBYTECODE": "1"})
        if proc.returncode != 0:
            raise FilmError(
                "worker script failed: "
                + proc.stderr.decode(errors="replace")[-2000:])
        self._runs[packet["execution_unit"]["request_id"]] = {
            "state": "COMPLETE", "packet_dir": str(packet_dir),
            "result_dir": str(result_dir),
            "session_epoch": self.session_epoch}
        return read_canon(Path(result_dir) / "result_manifest.json")

    def expire_session(self):
        """Mid-run session expiry: the runtime forgets every tracked run
        and the credential epoch no longer matches the entitlement."""
        self.session_epoch += 1
        self._runs.clear()

    def probe_fixture(self, work_dir):
        """Actually run the shipped script on a tiny synthetic packet and
        report what was observed — the only fixture evidence that exists."""
        import tempfile
        from ..execution_packets import fixture_packet_dir
        work = Path(work_dir) if work_dir else Path(
            tempfile.mkdtemp(prefix=".subprobe-"))
        work.mkdir(parents=True, exist_ok=True)
        packet_dir = fixture_packet_dir(work / "fixture_packet")
        result_dir = work / "fixture_result"
        manifest = self.execute_packet(packet_dir, result_dir,
                                       count_usage=False)
        packet = read_canon(packet_dir / "packet.json")
        contract = packet["frame_contract"]
        start, end = packet["execution_unit"]["frame_range"]
        ran = all(
            m["pixel_sha256"] == expected_frame_digest(
                m["frame_index"], contract,
                packet["plan"]["snapshot_digest"],
                packet["plan"]["recipe_digest"])
            for m in manifest["outputs"])
        manifest_bytes = (result_dir / "result_manifest.json").read_bytes()
        return {"environment": {
                    "service": self.service, "usage_path":
                    self.entitlement["usage_path"],
                    "session_id": hashlib.sha256(
                        f"session:{self.service}:{self.session_epoch}"
                        .encode()).hexdigest(),
                    "session_epoch": self.session_epoch,
                    "device": manifest["environment"]["device"],
                    "os": manifest["environment"]["os"],
                    "runtime": manifest["environment"]["runtime"],
                    "driver": "python-stdlib",
                    "network": "NONE", "gpu": "NONE",
                    "drive_read": "NONE", "drive_write": "NONE",
                    "fake_runtime": True},
                "caps": {"network": "NONE", "gpu": "NONE",
                         "drive_read": "NONE", "drive_write": "NONE",
                         "runtimes": ["python-deterministic-v1"],
                         "cancel_method": "user abandons or removes the "
                                          "run inside the service UI",
                         "complete_method": "user downloads the result "
                                            "dir; the app imports it"},
                "scope": {"service": self.service,
                          "usage_path": self.entitlement["usage_path"],
                          "operation": "compose_frame_range",
                          "runtime_contract": "python-deterministic-v1",
                          "pixel_format": contract["pixel_format"],
                          "network": "NONE", "gpu": "NONE",
                          "route": "MANUAL_PACKET",
                          "frames": end - start,
                          "max_input_bytes": 1 << 20},
                "fixture": {"ran": True,
                            "input_sha256": hashlib.sha256(
                                (packet_dir / "packet.json").read_bytes()
                                ).hexdigest(),
                            "output_sha256": hashlib.sha256(
                                manifest_bytes).hexdigest(),
                            "frames": end - start,
                            "verified": ran},
                "reason": ("the fake runtime executed the shipped worker "
                           "script on a synthetic packet; the digests "
                           "match the coordinator's re-derivation")}

    def status(self, request_id):
        self._live()
        run = self._runs.get(request_id)
        if run is None:
            return {"state": "NOT_FOUND", "receipts": []}
        return {"state": run["state"], "receipts": []}

    def cancel(self, request_id):
        self._live()
        run = self._runs.pop(request_id, None)
        if run is None:
            return {"outcome": "NOT_FOUND", "receipts": []}
        return {"outcome": "CANCELLED", "receipts": []}


def subscription_facet_report():
    """Honest program facets: fake/manual evidence never qualifies a
    real subscription runtime."""
    return {"node_state": "IN_PROGRESS",
            "qualification_state": "UNQUALIFIED",
            "acceptance_state": "PENDING",
            "release_state": "NOT_AUTHORIZED"}
