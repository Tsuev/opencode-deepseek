"""One guarded cookie renewal for an explicitly opted-in user request."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime
import json
import logging
import math
from pathlib import Path
import re
import time
import uuid

import httpx

from providers.access import ProviderRejected, current_attempt, rejection
from .auth import AUTH_REFRESH_URL, DEFAULT_SESSION_FILE, LoginRequired, Session

_AUTH_PATH = "/api/v2/auths/refresh"
_MAX_TOKEN = 32 * 1024
_MAX_JSON = 64 * 1024
_LOG = logging.getLogger(__name__)
# Public frontend business codes, plus Bad_Request observed without credentials.
_KNOWN_BUSINESS_CODES = {code.lower(): code for code in (
    "unauthorized", "invalid token", "forbidden", "access_prohibited",
    "ACCOUNT_PENDING", "data_inspection_failed", "Bad_Request")}


def _timezone_header():
    """Match the website's date string using the host's actual local offset."""
    return datetime.now().astimezone().strftime("%a %b %d %Y %H:%M:%S GMT%z")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Invalid expiry")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _jwt_metadata(token):
    """Read bounded metadata, without treating an unsigned decode as auth proof."""
    if not isinstance(token, str) or not 0 < len(token) <= _MAX_TOKEN:
        raise ValueError("Invalid token size")
    parts = token.split(".")
    if len(parts) != 3 or any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
        raise ValueError("Invalid JWT shape")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    data = json.loads(base64.b64decode(payload, altchars=b"-_", validate=True),
                      object_pairs_hook=_unique_object)
    if (not isinstance(data, dict) or not isinstance(data.get("id"), str)
            or not data["id"].strip() or len(data["id"]) > 256
            or not isinstance(data.get("type"), str) or not data["type"].strip()
            or len(data["type"]) > 64):
        raise ValueError("Invalid JWT metadata")
    return data["id"], data["type"], _number(data.get("exp"))


def _has_refresh_cookie(jar, now):
    for cookie in jar:
        domain = cookie.domain.lower()
        if domain not in ("auth.qwen.ai", ".auth.qwen.ai", ".qwen.ai"):
            continue
        path = cookie.path or "/"
        matches = (_AUTH_PATH == path or _AUTH_PATH.startswith(path)
                   and (path.endswith("/") or _AUTH_PATH[len(path):].startswith("/")))
        if (cookie.name == "refresh_token" and cookie.value and cookie.secure and matches
                and (cookie.expires is None or cookie.expires > now)):
            return True
    return False


def _cookie_snapshot(jar):
    return [{"name": c.name, "value": c.value, "domain": c.domain,
             "path": c.path, "secure": c.secure,
             "expires": c.expires if c.expires is not None else -1,
             "httpOnly": c.has_nonstandard_attr("HttpOnly")}
            for c in jar if c.domain.lower().lstrip(".") in Session.COOKIE_DOMAINS]


def _log_response_shape(value, reason):
    """Log a fixed schema, never response keys, text, credentials or headers."""
    def json_type(item):
        return {dict: "object", list: "array", str: "string", bool: "boolean",
                int: "number", float: "number", type(None): "null"}[type(item)]

    obj = value if isinstance(value, dict) else {}
    data = obj.get("data")
    code = data.get("code") if isinstance(data, dict) else None
    if isinstance(code, str) and code in ("401", "403", "429"):
        code = int(code)
    business_code = code if type(code) is int and code in (401, 403, 429) else None
    if isinstance(code, str):
        business_code = _KNOWN_BUSINESS_CODES.get(code.lower())
    _LOG.warning(
        "Qwen renewal response rejected: reason=%s response_type=%s "
        "success_present=%s success_type=%s success_true=%s "
        "data_present=%s data_type=%s business_code=%s",
        reason, json_type(value), "success" in obj, json_type(obj.get("success")),
        obj.get("success") is True, "data" in obj, json_type(data), business_code)


def _response_data(response, check):
    check()
    if not 200 <= response.status_code < 300:
        raise rejection(status=response.status_code)
    body = bytearray()
    for chunk in response.iter_bytes():
        check()
        if len(body) + len(chunk) > _MAX_JSON:
            raise ProviderRejected()
        body.extend(chunk)
    check()
    value = json.loads(body, object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or value.get("success") is not True:
        reason = ("missing_success" if not isinstance(value, dict) or "success" not in value
                  else "success_false" if value["success"] is False else "invalid_success")
        _log_response_shape(value, reason)
        detail = value.get("data") if isinstance(value, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        status = int(code) if str(code) in ("401", "403", "429") else None
        raise rejection(detail, status=status)
    data = value.get("data")
    if not isinstance(data, dict):
        _log_response_shape(value, "invalid_data")
        raise ProviderRejected()
    return data


def account_key(token):
    """Stable account identity from JWT id/type (metadata only, not auth proof)."""
    identity, kind, _ = _jwt_metadata(token)
    return hashlib.sha256(json.dumps([identity, kind]).encode()).hexdigest()


def _account_or_none(session):
    try:
        return account_key(session.token)
    except (ValueError, TypeError):
        return None


def renew_session(session_file: Path = DEFAULT_SESSION_FILE, transport=None,
                  expected_account=None) -> Session:
    """Reload local state, then make at most one fixed-origin guarded request.

    The caller owns explicit opt-in and the attempt's completion/abort lifecycle.
    This helper never starts, resumes or completes an AccessGuard attempt.
    """
    attempt = current_attempt("qwen")
    if attempt is None or attempt.dispatched:
        raise ProviderRejected()
    attempt.check()
    cached = Session.load(session_file)
    if cached is not None and cached.usable:
        if expected_account is not None and _account_or_none(cached) != expected_account:
            raise LoginRequired()
        return cached
    try:
        if cached is None:
            raise ValueError("Missing session")
        old_id, old_type, old_exp = _jwt_metadata(cached.token)
        if expected_account is not None and account_key(cached.token) != expected_account:
            # The history belongs to another account: refuse before dispatch.
            raise ValueError("Session belongs to another account")
        local_exp = _number(cached.expires_at)
        now = time.time()
        jar = cached.cookie_jar()
        if (max(old_exp, local_exp) > now + 60 or not _has_refresh_cookie(jar, now)
                or not isinstance(cached.user_agent, str) or not cached.user_agent.strip()):
            raise ValueError("Session cannot be renewed")
        client = httpx.Client(transport=transport, cookies=jar, trust_env=False,
                              follow_redirects=False, timeout=20,
                              headers={"User-Agent": cached.user_agent,
                                       "Origin": "https://chat.qwen.ai",
                                       "Referer": "https://chat.qwen.ai/",
                                       "Accept": "application/json",
                                       "Accept-Language": "en-US,en;q=0.9",
                                       "Content-Type": "application/json",
                                       "Timezone": _timezone_header(),
                                       "x-request-origin": "https://chat.qwen.ai",
                                       "X-Request-Id": str(uuid.uuid4()),
                                       "Version": "0.3.14", "source": "web"})
    except Exception:
        raise LoginRequired() from None

    def validate_current():
        if Session.load(session_file) != cached or not _has_refresh_cookie(jar, time.time()):
            raise ProviderRejected()

    with client:
        phase, status = "dispatch", None
        try:
            attempt.check()
            attempt.dispatch(validate=validate_current)
            deadline = time.monotonic() + 30

            def check_response():
                attempt.check()
                if time.monotonic() >= deadline:
                    raise ProviderRejected()

            with client.stream("GET", AUTH_REFRESH_URL) as response:
                phase, status = "response", response.status_code
                data = _response_data(response, check_response)
            phase = "token_metadata"
            new_token = data.get("access_token")
            new_id, new_type, jwt_exp = _jwt_metadata(new_token)
            phase = "expiry"
            # The website falls back to JWT expiry when this optional response
            # field is absent or null. JWT identity and expiry remain required.
            server_exp = _number(data["expires_at"]) if data.get("expires_at") is not None else jwt_exp
            expires_at = min(server_exp, jwt_exp)
            if ((new_id, new_type) != (old_id, old_type)
                    or expires_at <= max(time.time() + 60, old_exp, local_exp)):
                raise ProviderRejected()
            fresh = Session(new_token, _cookie_snapshot(client.cookies.jar),
                            cached.user_agent, time.time(), expires_at=expires_at)
            phase = "persist"
            attempt.check()
            if not fresh.save_if_current(cached, session_file, check=attempt.check):
                raise ProviderRejected()
            attempt.check()
            return fresh
        except ProviderRejected as error:
            _LOG.warning("Qwen renewal stopped: phase=%s http_status=%s code=%s", phase, status, error.code)
            raise
        except Exception as error:
            # Never publish exception text, response bodies, tokens, or cookies.
            _LOG.warning("Qwen renewal stopped: phase=%s http_status=%s exception=%s",
                         phase, status, type(error).__name__)
            raise ProviderRejected() from None
