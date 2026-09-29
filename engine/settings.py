"""User-level settings and API keys for the model picker.

Keys live in the user's own settings folder, never in a project or a build, so
sharing or archiving a project cannot leak them. An environment variable always
wins over a saved key, so existing shell setups keep working.
"""
import json
import os
from pathlib import Path
import re
import tempfile

from .core import FilmError

NAME = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
SETTING = re.compile(r"[a-z][a-z0-9_]{0,63}")
PROVIDER_ID = re.compile(r"[a-z][a-z0-9_]{1,63}")


def home():
    """FILM_UNIT_HOME, else ~/.film_unit. The desktop app uses the same variable."""
    root = os.environ.get("FILM_UNIT_HOME")
    path = Path(root) if root else Path.home() / ".film_unit"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load(name):
    path = home() / name
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise FilmError(f"Settings file {name} is unreadable; fix or delete it: {exc}") from None
    if not isinstance(value, dict):
        raise FilmError(f"Settings file {name} must contain an object")
    return value


def _save(name, value, private=False):
    directory = home()
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
        if private:
            os.chmod(temp, 0o600)  # No effect on Windows, whose profile folder is already per-user.
        os.replace(temp, directory / name)
    finally:
        Path(temp).unlink(missing_ok=True)


def get_secret(name):
    """Environment first, then the saved key. Returns None when unset."""
    if not NAME.fullmatch(name):
        raise FilmError("Invalid key name")
    return os.environ.get(name) or _load("secrets.json").get(name) or None


def secret_source(name):
    """'env', 'saved' or None. Never reveals the value."""
    if os.environ.get(name):
        return "env"
    return "saved" if _load("secrets.json").get(name) else None


def set_secret(name, value):
    if not NAME.fullmatch(name):
        raise FilmError("Invalid key name")
    value = (value or "").strip()
    # A key is a single token; anything else could smuggle extra HTTP header lines.
    if not value or len(value) > 512 or not value.isprintable() or re.search(r"\s", value):
        raise FilmError("The key must be a single token without spaces or line breaks")
    secrets = _load("secrets.json")
    secrets[name] = value
    _save("secrets.json", secrets, private=True)


def delete_secret(name):
    secrets = _load("secrets.json")
    if secrets.pop(name, None) is not None:
        _save("secrets.json", secrets, private=True)


def get_settings(provider_id):
    """Non-secret provider settings such as model name, account ID or base URL."""
    if not PROVIDER_ID.fullmatch(provider_id):
        raise FilmError("Invalid provider ID")
    value = _load("providers.json").get(provider_id, {})
    return value if isinstance(value, dict) else {}


def set_settings(provider_id, values):
    if not PROVIDER_ID.fullmatch(provider_id):
        raise FilmError("Invalid provider ID")
    clean = {}
    for key, value in values.items():
        if not SETTING.fullmatch(key) or not isinstance(value, str) or len(value) > 512 or "\n" in value:
            raise FilmError(f"Invalid setting {key!r}")
        if value.strip():
            clean[key] = value.strip()
    everything = _load("providers.json")
    everything[provider_id] = clean
    _save("providers.json", everything)
