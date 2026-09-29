"""JSON over HTTPS to one configured host: no redirects, no retries, no stray keys.

A provider's key is attached by the transport and only for its own host. Local
model servers (Ollama, LM Studio) may use plain HTTP on the same machine.
"""
import json
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .core import FilmError

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def host_of(url):
    return (urlparse(url).hostname or "").lower()


def check_url(url, allowed_host, allow_local_http=False):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.username or parsed.password or host != allowed_host.lower():
        raise FilmError("Unexpected host; no credentials were sent")
    if parsed.scheme == "https" and parsed.port in {None, 443}:
        return url
    if allow_local_http and parsed.scheme == "http" and host in LOCAL_HOSTS:
        return url
    raise FilmError("Use https (plain http is allowed only for a model server on this computer)")


class SafeHTTP:
    """headers: mapping, or a callable returning one, evaluated at request time."""

    def __init__(self, base_url, headers=None, allow_local_http=False):
        self.base = base_url.rstrip("/")
        self.host = host_of(self.base)
        self.headers = headers or {}
        self.allow_local_http = allow_local_http
        check_url(self.base + "/", self.host, allow_local_http)

    def request(self, method, path, payload=None, timeout=60):
        url = path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"
        check_url(url, self.host, self.allow_local_http)
        headers = self.headers() if callable(self.headers) else dict(self.headers)
        headers["Content-Type"] = "application/json"
        req = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                      headers=headers, method=method)
        with build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            return json.load(response)
