"""Private Kimi credential rotation using its normal account refresh service."""

import base64
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import time

import httpx

from deepseek.auth import LoginRequired as BaseLoginRequired, write_private_json
from settings import ROOT
from .access import _file_lock, rejection

ORIGINS = ('https://www.kimi.ai', 'https://www.kimi.com')
SESSION_FILE = ROOT / 'session/kimi/session.json'
REFRESH_URL = 'https://auth.kimi.ai/api/account.gateway.v1.AuthService/RefreshToken'
LIMIT = 65536


class LoginRequired(BaseLoginRequired):
    before_dispatch = True

    def __init__(self):
        super().__init__('Kimi needs a manual session capture. Automatic sign-in and replay are disabled.')


def rotated_credentials(result):
    """Protobuf JSON permits snake_case and lowerCamelCase field spellings."""
    if not isinstance(result, dict):
        raise ValueError('Invalid Kimi refresh response')
    pair = {}
    for snake, camel in (('access_token', 'accessToken'), ('refresh_token', 'refreshToken')):
        if snake in result and camel in result and result[snake] != result[camel]:
            raise ValueError('Ambiguous Kimi refresh response')
        pair[snake] = result[snake] if snake in result else result[camel]
    return pair


def access_claims(token):
    try:
        if (not isinstance(token, str) or len(token) > 16384
                or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token)):
            raise ValueError()
        part = token.split('.')[1]
        claims = json.loads(base64.b64decode(part + '=' * (-len(part) % 4), altchars=b'-_', validate=True))
        expiry, subject = claims['exp'], claims['sub']
        if (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                or not math.isfinite(expiry) or not isinstance(subject, str)
                or not 1 <= len(subject) <= 1024):
            raise ValueError()
        return claims
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        raise ValueError('Invalid Kimi access token') from None


def validate_session(value, now=None, require_fresh=True):
    now = time.time() if now is None else now
    if not isinstance(value, dict) or set(value) != {'origin', 'token', 'refresh_token', 'user_agent'}:
        raise ValueError('Invalid Kimi session')
    if value['origin'] not in ORIGINS:
        raise ValueError('Unsupported Kimi origin')
    refresh, agent = value['refresh_token'], value['user_agent']
    if (not isinstance(refresh, str) or not re.fullmatch(r'[\x21-\x7e]{1,16384}', refresh)
            or not isinstance(agent, str) or not re.fullmatch(r'[\x20-\x7e]{1,1024}', agent)):
        raise ValueError('Invalid Kimi session')
    claims = access_claims(value['token'])
    if require_fresh and claims['exp'] <= now + 60:
        raise ValueError('Kimi session is expired or nearly expired')
    # Local expiry/account binding only; this does not verify a JWT signature.
    return {**value, 'expires_at': claims['exp'], 'captured_at': now}


@contextmanager
def session_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix('.lock').open('a+b') as lock:
        os.chmod(lock.name, 0o600)
        if os.name == 'nt' and lock.seek(0, 2) == 0:
            lock.write(b'\0')
            lock.flush()
        _file_lock(lock)
        try:
            yield
        finally:
            _file_lock(lock, unlock=True)


def _persist(path, value):
    write_private_json(path, value)
    if os.name != 'nt':
        directory = os.open(Path(path).parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def save_capture(path, value):
    with session_lock(path):
        _persist(path, value)


def request_headers(session):
    return {'Content-Type': 'application/json', 'Connect-Protocol-Version': '1',
            'x-msh-version': '2.3.0', 'x-msh-platform': 'web',
            'User-Agent': session['user_agent'], 'Origin': session['origin']}


def usable_session(attempt, path=SESSION_FILE, renew=False, client_factory=httpx.Client,
                   *, force_refresh=False):
    """Refresh at most once before dispatching a new model request.

    The same file lock serializes explicit captures and refreshes. Never refresh
    in response to a failed model request. A failed/uncertain exchange leaves
    the owning durable provider attempt paused, even if the old token is valid.
    """
    with session_lock(path):
        attempt.check()
        try:
            with Path(path).open('rb') as source:
                raw = source.read(LIMIT + 1)
            if len(raw) > LIMIT:
                raise ValueError()
            value = json.loads(raw)
            fields = {key: value[key] for key in ('origin', 'token', 'refresh_token', 'user_agent')}
            session = validate_session(fields, require_fresh=False)
        except (OSError, ValueError, KeyError, TypeError):
            raise LoginRequired() from None
        # force_refresh is for an explicit local diagnostic, never a model retry.
        if not force_refresh and session['expires_at'] > time.time() + 60:
            return session
        if not renew:
            raise LoginRequired()
        with client_factory(trust_env=False, follow_redirects=False, timeout=20) as client:
            attempt.dispatch()
            phase = 'exchange'
            try:
                deadline = time.monotonic() + 20
                with client.stream('POST', REFRESH_URL, headers=request_headers(session),
                                   json={'refresh_token': session['refresh_token']}) as response:
                    if response.status_code != 200:
                        raise rejection(status=response.status_code)
                    if response.headers.get('content-type', '').split(';')[0] != 'application/json':
                        raise rejection()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() >= deadline or len(body) + len(chunk) > LIMIT:
                            raise rejection()
                        body.extend(chunk)
                phase = 'decode'
                result = rotated_credentials(json.loads(body))
                updated = validate_session({**fields, 'token': result['access_token'],
                                            'refresh_token': result['refresh_token']})
                phase = 'account_binding'
                before, after = access_claims(session['token']), access_claims(updated['token'])
                if any(before.get(key) != after.get(key) for key in ('sub', 'iss', 'app_id')):
                    raise ValueError()
                # Persist a successfully rotated pair before checking cancellation:
                # the vendor may already have invalidated the old refresh token.
                phase = 'persist'
                _persist(path, updated)
            except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
                error = rejection()
                error.phase = 'kimi_refresh_' + phase
                raise error from None
        attempt.check()
        return updated
