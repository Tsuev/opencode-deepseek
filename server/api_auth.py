"""Access boundary for the OpenAI-compatible /v1 API.

Every /v1 request needs `Authorization: Bearer <token>` from a private local
file and a Host header naming this machine. Browsers cannot read the token, so
cross-site requests and DNS rebinding cannot drive account providers.

`API_AUTH=legacy-loopback` is a temporary migration mode for old keyless
profiles: a request without a valid token is admitted only from a loopback
peer, with a loopback Host, and without browser provenance headers.

    python -m server.api_auth path   # print the token file path, never the token
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import sys
import tempfile
from pathlib import Path

from settings import ROOT

PREFIX = "/v1"
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
MODES = ("token", "legacy-loopback")


def mode():
    value = os.getenv("API_AUTH", "token").strip().lower() or "token"
    if value not in MODES:
        raise ValueError("API_AUTH must be token or legacy-loopback")
    return value


def token_path() -> Path:
    configured = os.getenv("BRIDGE_API_TOKEN_FILE", "").strip()
    return Path(configured).expanduser() if configured else ROOT / "session" / "api-token"


def ensure_token(path: Path | None = None) -> str:
    """Create a 0600 token atomically on first use; never print it."""
    path = token_path() if path is None else Path(path)
    if path.is_symlink():
        raise ValueError("Refusing a symlinked API token file")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.exists():
        descriptor, temporary = tempfile.mkstemp(prefix=".api-token-", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "w") as output:
                output.write(secrets.token_urlsafe(32) + "\n")
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, path)  # Never replace a concurrently created token.
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    path.chmod(0o600)
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 32 or any(c.isspace() for c in value):
        raise ValueError("Invalid API token file")
    return value


def _valid_port(text):
    # Bound the length before int(): a huge digit string must be a 421, not a 500.
    return 1 <= len(text) <= 5 and text.isascii() and text.isdecimal() and 1 <= int(text) <= 65535


def _host_name(header):
    """Strict host[:port] / [ipv6][:port]; anything else is refused (None)."""
    if not isinstance(header, str) or not header or not header.isascii() or any(
            c.isspace() or c in "/\\@?#," for c in header):
        return None
    if header.startswith("["):
        end = header.find("]")
        if end < 0:
            return None
        host, rest = header[1:end], header[end + 1:]
        try:
            if ipaddress.ip_address(host).version != 6:
                return None
        except ValueError:
            return None
        if rest and not (rest.startswith(":") and _valid_port(rest[1:])):
            return None
        return host.lower()
    host, separator, port = header.partition(":")
    if not host or ":" in port or (separator and not _valid_port(port)):
        return None
    return host.lower()


def allowed_hosts():
    extra = {item.strip().lower() for item in os.getenv("API_ALLOWED_HOSTS", "").split(",") if item.strip()}
    return LOOPBACK_NAMES | extra


def _loopback_peer(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except (TypeError, ValueError):
        return False


def _items(headers):
    """Raw header pairs, keeping duplicates when the mapping supports it."""
    raw = getattr(headers, "raw", None)
    if raw is not None:
        return [(k.decode("latin-1"), v.decode("latin-1")) for k, v in raw]
    return list(headers.items())


def _browser_provenance(headers):
    """Presence, not value: any Origin or Sec-Fetch-* header marks a browser."""
    return any(name.lower() == "origin" or name.lower().startswith("sec-fetch-") for name, _ in _items(headers))


def decide(path, client_host, headers):
    """Return None to admit, else (status, message). Pure for offline tests."""
    if not (path == PREFIX or path.startswith(PREFIX + "/")):
        return None
    hosts = [value for name, value in _items(headers) if name.lower() == "host"]
    host = _host_name(hosts[0]) if len(hosts) == 1 else None
    if host is None or host not in allowed_hosts():
        return 421, "Request Host is not served by this bridge; no provider request was sent."
    supplied = headers.get("authorization", "")
    try:
        expected = "Bearer " + ensure_token()
    except (OSError, ValueError):
        return 503, "API token file is unavailable; no provider request was sent."
    if isinstance(supplied, str) and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        return None
    if (mode() == "legacy-loopback" and _loopback_peer(client_host) and host in LOOPBACK_NAMES
            and not _browser_provenance(headers)):
        return None
    return 401, ("Bridge API token required. Point the client at the private token file "
                 f"{token_path()} (OpenCode: \"apiKey\": \"{{file:{token_path()}}}\"; Harness: "
                 "rerun python -m integrations.harness --install <profile>). No provider request was sent.")


def install(app):
    from starlette.responses import JSONResponse
    mode()  # Fail at startup on an invalid setting.

    @app.middleware("http")
    async def _api_auth(request, call_next):
        refusal = decide(request.url.path, request.client.host if request.client else None, request.headers)
        if refusal is None:
            return await call_next(request)
        status, message = refusal
        kind = "authentication_error" if status == 401 else "invalid_request_error"
        return JSONResponse({"error": {"message": message, "type": kind, "code": kind, "retryable": False}},
                            status_code=status, headers={"x-should-retry": "false", "Cache-Control": "no-store"})


def public_bind_refusal(host):
    """Return an error text when HOST would expose the API unsafely."""
    if _loopback_peer(host) or host == "localhost":
        return None
    if mode() != "token":
        return "API_AUTH=legacy-loopback cannot listen on a non-loopback HOST."
    if os.getenv("API_ALLOW_PUBLIC_BIND", "0") != "1":
        return ("Refusing non-loopback HOST without API_ALLOW_PUBLIC_BIND=1. The /v1 API controls "
                "your provider accounts; also set API_ALLOWED_HOSTS and use a firewall.")
    return None


if __name__ == "__main__":
    if sys.argv[1:] != ["path"]:
        raise SystemExit("usage: python -m server.api_auth path")
    ensure_token()
    print(token_path())
