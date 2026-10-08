"""Pinned local ONNX weight registry.

Weights are never stored in git and never downloaded while the engine is
imported, while tests run, or while CI installs the ``test`` extra.
``fetch`` is the only network path, and only the CLI (or a test that
supplies its own fetcher) calls it.

The first pin, ``rife49``, is the Practical-RIFE v4.9 ONNX export.
Its sha256 and byte size were measured from a download of the source URL
and match the Hugging Face LFS oid of that file and of the mirror.

This module does not run inference. Capability evidence it builds is never
``QUALIFIED_FOR_SCOPE``. Product qualification stays UNQUALIFIED.
"""
from pathlib import Path
import json
import os
import re
import shutil
import tempfile
from urllib.parse import urljoin, urlparse

from .core import FilmError, digest

_CATALOG_FIELDS = {"id", "version", "source_url", "mirror_url", "sha256",
                   "bytes", "license", "license_url", "inputs", "outputs"}
_TENSOR_FIELDS = {"name", "dtype", "shape"}
_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
# capability_evidence schema-1 only admits encode drivers. This registry is
# not an encoder; QUALIFIED_SERVICE is the schema's non-hardware slot so the
# record validates. The registry_state below is never QUALIFIED_FOR_SCOPE.
_EVIDENCE_DRIVER = "QUALIFIED_SERVICE"
_MAX_REDIRECTS = 5
_FACETS = {"qualification_state": "UNQUALIFIED",
           "acceptance_state": "PENDING",
           "release_state": "NOT_AUTHORIZED"}


class _HashMismatch(FilmError):
    """The downloaded body did not match the pin. The body is already gone."""


def _check_id(model_id):
    if type(model_id) is not str or not _ID.fullmatch(model_id):
        raise FilmError("model id must be a single safe path segment")
    return model_id


def _check_https(url):
    if type(url) is not str or not url or any(c in url for c in " \t\r\n"):
        raise FilmError("model fetch allows https only")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise FilmError("model fetch allows https only")
    if parsed.username or parsed.password:
        raise FilmError("model URL must not carry credentials")
    return url


def _join_https(current, location):
    """Next hop of a download redirect. An http (or other) target is refused."""
    if type(location) is not str or not location:
        raise FilmError("model download redirect had no Location")
    return _check_https(urljoin(current, location))


def _check_dest(dest):
    path = Path(dest).expanduser()
    if any(part == ".." for part in path.parts):
        raise FilmError("model destination must not contain '..'")
    return path


def _tensor(item, where):
    if type(item) is not dict or set(item) != _TENSOR_FIELDS:
        raise FilmError(f"{where} must be {{name, dtype, shape}}")
    if type(item["name"]) is not str or not item["name"]:
        raise FilmError(f"{where}.name must be a non-empty string")
    if type(item["dtype"]) is not str or not item["dtype"]:
        raise FilmError(f"{where}.dtype must be a non-empty string")
    shape = item["shape"]
    if type(shape) is not list or not shape:
        raise FilmError(f"{where}.shape must be a non-empty list")
    for dim in shape:
        if type(dim) is bool or (
                type(dim) is not int and type(dim) is not str):
            raise FilmError(f"{where}.shape dims must be ints or names")
        if type(dim) is str and (not dim or "/" in dim or dim == ".."):
            raise FilmError(f"{where}.shape names must be plain tokens")
    return item


def load_catalog():
    """Read ``engine/models.json``. No network."""
    path = Path(__file__).with_name("models.json")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FilmError(f"model catalog is unreadable: {path}") from exc
    entries = document.get("models") if type(document) is dict else None
    if type(entries) is not list or not entries:
        raise FilmError("model catalog must list models")
    by_id = {}
    for entry in entries:
        if type(entry) is not dict or set(entry) != _CATALOG_FIELDS:
            raise FilmError("model entry must hold exactly the catalog fields")
        model_id = _check_id(entry["id"])
        if model_id in by_id:
            raise FilmError(f"duplicate model id: {model_id}")
        if type(entry["version"]) is not str or not entry["version"]:
            raise FilmError(f"{model_id}: version must be a non-empty string")
        _check_https(entry["source_url"])
        _check_https(entry["mirror_url"])
        if not _SHA.fullmatch(entry["sha256"]):
            raise FilmError(f"{model_id}: sha256 must be 64 lowercase hex chars")
        size = entry["bytes"]
        if type(size) is not int or size < 1:
            raise FilmError(f"{model_id}: bytes must be a positive integer")
        if type(entry["license"]) is not str or not entry["license"]:
            raise FilmError(f"{model_id}: license must be a non-empty string")
        _check_https(entry["license_url"])
        for key in ("inputs", "outputs"):
            tensors = entry[key]
            if type(tensors) is not list or not tensors:
                raise FilmError(f"{model_id}: {key} must be a non-empty list")
            for index, tensor in enumerate(tensors):
                _tensor(tensor, f"{model_id}.{key}[{index}]")
        by_id[model_id] = entry
    return by_id


def get_model(model_id):
    _check_id(model_id)
    catalog = load_catalog()
    if model_id not in catalog:
        raise FilmError(f"unknown model: {model_id}")
    return catalog[model_id]


def model_root():
    """``$FILMUNIT_MODEL_DIR`` or ``~/.cache/filmunit/models``."""
    raw = os.environ.get("FILMUNIT_MODEL_DIR")
    if raw:
        return _check_dest(raw)
    return Path.home() / ".cache" / "filmunit" / "models"


def model_path(model_id):
    """``<root>/<id>/<sha256>.onnx``. The file need not exist."""
    entry = get_model(model_id)
    root = model_root().resolve()
    path = root / model_id / f"{entry['sha256']}.onnx"
    if not path.is_relative_to(root):
        raise FilmError("model path escapes the cache directory")
    return path


def verify(model_id):
    """``PRESENT``, ``MISSING`` or ``MISMATCH``. Never deletes."""
    entry = get_model(model_id)
    path = model_path(model_id)
    pinned = entry["sha256"]
    result = {"state": "MISSING", "path": str(path), "sha256": pinned}
    if not path.is_file():
        return result
    if path.stat().st_size != entry["bytes"] or digest(path) != pinned:
        result["state"] = "MISMATCH"
        return result
    result["state"] = "PRESENT"
    return result


def _default_fetcher(url, dest_path, expected_bytes):
    """Stream an https response into ``dest_path``. Stops past the pin size."""
    import urllib.error
    import urllib.request

    from .safehttp import NoRedirect

    opener = urllib.request.build_opener(NoRedirect)
    current = _check_https(url)
    hops = 0
    while True:
        request = urllib.request.Request(
            current, headers={"User-Agent": "filmunit-model-registry"})
        try:
            response = opener.open(request, timeout=120)
            break
        except urllib.error.HTTPError as exc:
            code = exc.code
            location = exc.headers.get("Location") if exc.headers else None
            exc.close()
            if code not in {301, 302, 303, 307, 308}:
                raise FilmError(f"model download failed: HTTP {code}") from exc
            hops += 1
            if hops > _MAX_REDIRECTS:
                raise FilmError("model download followed too many redirects")
            current = _join_https(current, location)
        except urllib.error.URLError as exc:
            raise FilmError("model download failed") from exc
    remaining = expected_bytes + 1
    try:
        with open(dest_path, "wb") as handle:
            while remaining > 0:
                block = response.read(min(1024 * 1024, remaining))
                if not block:
                    break
                handle.write(block)
                remaining -= len(block)
    finally:
        response.close()


def fetch(model_id, dest=None, *, fetcher=None):
    """Download one pin over https into a fresh temp directory.

    The body is moved under ``dest/<id>/<sha256>.onnx`` only after the
    sha256 and the byte size both match. A mismatch is deleted and is not
    left in ``dest``. ``dest`` defaults to :func:`model_root`. A ``..``
    destination or a non-https catalog URL is refused before any download.

    ``fetcher(url, dest_path)`` replaces the https client in tests. The
    engine does not call this function on import.
    """
    _check_id(model_id)
    root = model_root() if dest is None else _check_dest(dest)
    entry = get_model(model_id)
    for url in (entry["source_url"], entry["mirror_url"]):
        _check_https(url)
    root = root.resolve()
    final = root / model_id / f"{entry['sha256']}.onnx"
    if not final.is_relative_to(root):
        raise FilmError("model destination escapes the cache directory")
    mismatch = None
    transport = None
    for url in (entry["source_url"], entry["mirror_url"]):
        tmp = Path(tempfile.mkdtemp(prefix=".filmunit-model-"))
        partial = tmp / "payload.onnx"
        try:
            if fetcher is None:
                _default_fetcher(url, partial, entry["bytes"])
            else:
                fetcher(url, partial)
            if not partial.is_file():
                raise FilmError("model download produced no file")
            if partial.stat().st_size != entry["bytes"] \
                    or digest(partial) != entry["sha256"]:
                partial.unlink(missing_ok=True)
                raise _HashMismatch(
                    f"refusing mismatched download for {model_id}: "
                    f"pinned sha256 {entry['sha256']} and "
                    f"{entry['bytes']} bytes; removed")
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(partial), str(final))
            return {"id": model_id, "state": "PRESENT", "path": str(final),
                    "sha256": entry["sha256"], "bytes": entry["bytes"],
                    "url": url}
        except _HashMismatch as exc:
            mismatch = exc
        except FilmError as exc:
            transport = exc
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if mismatch is not None:
        raise mismatch
    raise transport or FilmError(f"model download failed for {model_id}")


def _onnxruntime_version():
    """Installed onnxruntime version, or None when the import fails.

    Importing onnxruntime is deferred to evidence time. It is an optional
    extra and is not installed by CI.
    """
    try:
        import onnxruntime as ort
    except ImportError:
        return None
    return str(getattr(ort, "__version__", "unknown"))


def evidence(model_id, operation):
    """A ``capability_evidence`` schema-1 record for this pin.

    Missing onnxruntime is ``UNAVAILABLE`` / ``missing-runtime``. A missing
    or mismatched file is ``UNAVAILABLE`` / ``missing-model``. A present
    file with a runtime import is only ``DOCUMENTED_ONLY``: this module
    does not run a session and never emits ``QUALIFIED_FOR_SCOPE``.
    """
    if type(operation) is not str or not operation:
        raise FilmError("operation must name what this evidence is about")
    entry = get_model(model_id)
    checked = verify(model_id)
    runtime = _onnxruntime_version()
    if runtime is None:
        registry_state = "UNAVAILABLE"
        qualification = "missing-runtime"
        reason = ("onnxruntime is not installed (optional extra 'models'; "
                  "CI installs the test extra only)")
    elif checked["state"] != "PRESENT":
        registry_state = "UNAVAILABLE"
        qualification = "missing-model"
        reason = (f"pinned weights are {checked['state']}; "
                  "nothing was deleted")
    else:
        registry_state = "DOCUMENTED_ONLY"
        qualification = "present-unprobed"
        reason = ("weights match the pin and onnxruntime imports; "
                  "this module does not run inference and cannot qualify")
    if registry_state == "QUALIFIED_FOR_SCOPE":
        raise FilmError("model registry evidence is never QUALIFIED_FOR_SCOPE")
    import platform

    from .animation_schema import validate_capability_evidence
    from .encoder_backends import capability_evidence

    probe = {
        "registry_state": registry_state,
        "qualification": qualification,
        "reason": reason,
        "operation": operation,
        "environment": {
            "session": None,
            "device": "local",
            "os": platform.platform(),
            "driver": "model_registry",
            "onnxruntime": runtime or "absent",
            "model_id": model_id,
            "model_state": checked["state"],
            "network": "none",
        },
        "scope": {"model_id": model_id, "sha256": entry["sha256"],
                  "bytes": entry["bytes"], "verify": checked["state"],
                  "inference": "not-run"},
        "caps": {"onnxruntime": runtime or "absent", "inference": False},
        "fixture": None,
        "entitlement_basis": ("local pinned weights — no account, "
                              "no service, no credential"),
    }
    record = capability_evidence(_EVIDENCE_DRIVER, probe)
    if record["registry_state"] == "QUALIFIED_FOR_SCOPE":
        raise FilmError("model registry evidence is never QUALIFIED_FOR_SCOPE")
    return validate_capability_evidence(record)


def status(model_id=None, operation="inspect_pinned_weights"):
    """Verify state plus evidence. Facets stay unqualified."""
    catalog = load_catalog()
    if model_id is None:
        ids = sorted(catalog)
    else:
        _check_id(model_id)
        if model_id not in catalog:
            raise FilmError(f"unknown model: {model_id}")
        ids = [model_id]
    models = []
    for item_id in ids:
        models.append({"id": item_id,
                       "version": catalog[item_id]["version"],
                       "verify": verify(item_id),
                       "evidence": evidence(item_id, operation)})
    return {**_FACETS,
            "note": ("pinned weight registry only; no inference and "
                     "no product capability"),
            "models": models}
