"""Human-initiated Safari session transfer. No upstream requests or goal resume."""
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from deepseek.auth import DEFAULT_SESSION_FILE, Session
from providers.access import ProviderRejected
from server.budgets import authorized

router = APIRouter()
ORIGIN = 'https://chat.deepseek.com'
TTL = 600
LIMIT = 32768


class TransferError(Exception):
    pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def eligible(record):
    if record.get('pause') not in (None, 'session_expired'):
        raise TransferError('Provider restriction requires manual review')
    if record.get('pending') and not record.get('recoverable_auth'):
        raise TransferError('Active or uncertain request; session transfer refused')


def capture_session(data):
    if not isinstance(data, dict) or set(data) != {'token', 'account_id', 'user_agent', 'cookie', 'same_account'}:
        raise TransferError('Invalid capture')
    token, account, ua, cookie = (data[k] for k in ('token', 'account_id', 'user_agent', 'cookie'))
    if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{16,4096}', token)
            or not isinstance(account, str) or not re.fullmatch(r'[!-~]{1,128}', account)
            or not isinstance(ua, str) or not re.fullmatch(r'[ -~]{1,512}', ua)
            or not isinstance(cookie, str) or len(cookie) > 16384
            or any(ord(c) < 32 or ord(c) > 126 for c in cookie)
            or not isinstance(data['same_account'], bool)):
        raise TransferError('Invalid capture')
    cookies = []
    for entry in cookie.split(';') if cookie else []:
        name, separator, value = entry.strip().partition('=')
        if not separator or not re.fullmatch(r'[!#$%&\x27*+.^_`|~0-9A-Za-z-]+', name):
            raise TransferError('Invalid cookie')
        cookies.append(dict(name=name, value=value, domain='chat.deepseek.com', path='/',
                            secure=True, httpOnly=False, expires=-1))
    return Session(token, cookies, ua, time.time(), account_id=account)


class SessionTransfer:
    def __init__(self, path=DEFAULT_SESSION_FILE, clock=time.monotonic):
        self.path, self.clock = Path(path), clock
        self.lock = threading.Lock()
        self.ticket = None
        self.result = 'idle'

    def status(self):
        with self.lock:
            if self.ticket and self.clock() >= self.ticket['expires']:
                self.ticket = None
                self.result = 'expired'
            return {'status': self.result, 'goal_resumed': False}

    def start(self, guard):
        with self.lock, guard._state() as state:
            if 'deepseek' in guard._failed:
                raise TransferError('Safety state requires manual repair')
            record = state.get('deepseek', {})
            eligible(record)
            old = Session.load(self.path)
            if old is None:
                raise TransferError('Existing session required to preserve history')
            nonce = secrets.token_urlsafe(32)
            self.ticket = dict(nonce=nonce, expires=self.clock()+TTL,
                               session_hash=hashlib.sha256(self.path.read_bytes()).hexdigest(),
                               safety_hash=fingerprint(record))
            self.result = 'waiting'
            return {'nonce': nonce, 'expires_in': TTL, 'account_bound': old.account_id is not None}

    def cancel(self):
        with self.lock:
            self.ticket = None
            self.result = 'cancelled'

    def capture(self, nonce, data, guard):
        with self.lock:
            ticket = self.ticket
            if (not ticket or self.clock() >= ticket['expires']
                    or not hmac.compare_digest(nonce.encode(), ticket['nonce'].encode())):
                raise TransferError('Transfer absent, expired or already consumed')
            fresh = capture_session(data)
            with guard._state() as state:
                if 'deepseek' in guard._failed:
                    raise TransferError('Safety state requires manual repair')
                if self.clock() >= ticket['expires']:
                    self.ticket = None
                    self.result = 'expired'
                    raise TransferError('Transfer expired while waiting for local state')
                record = state.get('deepseek', {})
                eligible(record)
                if (fingerprint(record) != ticket['safety_hash']
                        or hashlib.sha256(self.path.read_bytes()).hexdigest() != ticket['session_hash']):
                    self.ticket = None
                    self.result = 'state_changed'
                    raise TransferError('Local state changed; start a new transfer')
                old = Session.load(self.path)
                if old is None:
                    raise TransferError('Existing session unavailable')
                if old.account_id is not None and old.account_id != fresh.account_id:
                    raise TransferError('Different account; history was not transferred')
                if old.account_id is None and not data['same_account']:
                    return {'status': 'confirmation_required'}
                if old.token == fresh.token and record.get('pause') == 'session_expired':
                    return {'status': 'new_login_required'}
                fresh.preserve_history_from(old, same_account_confirmed=True)
                if self.clock() >= ticket['expires']:
                    self.ticket = None
                    self.result = 'expired'
                    raise TransferError('Transfer expired before saving')
                # Consume before mutation. A failed save must never replay capture.
                self.ticket = None
                self.result = 'failed'
                fresh.save(self.path)
                if record.get('pause') == 'session_expired':
                    for key in ('pause', 'pending', 'dispatched', 'recoverable_auth'):
                        record.pop(key, None)
                    try:
                        guard._save(state)
                    except OSError:
                        guard._failed.add('deepseek')
                        raise
                self.result = 'saved'
            return {'status': 'saved', 'goal_resumed': False}


transfer = SessionTransfer()


def response(data, code=200):
    return JSONResponse(data, status_code=code, headers={'Cache-Control': 'no-store'})


@router.post('/admin/session/deepseek/start')
async def start(request: Request):
    if not authorized(request):
        return response({'error': 'Unauthorized'}, 403)
    from server import api
    if api._request_gate.locked():
        return response({'error': 'DeepSeek request active'}, 409)
    async with api._request_gate:
        try:
            return response(transfer.start(api.guard))
        except (TransferError, ProviderRejected, OSError):
            return response({'error': 'Session transfer unavailable; inspect provider pause or active request'}, 409)


@router.get('/admin/session/deepseek/status')
async def status(request: Request):
    if not authorized(request):
        return response({'error': 'Unauthorized'}, 403)
    return response(transfer.status())


@router.post('/admin/session/deepseek/cancel')
async def cancel(request: Request):
    if not authorized(request):
        return response({'error': 'Unauthorized'}, 403)
    transfer.cancel()
    return response(transfer.status())


@router.post('/session/deepseek/capture')
async def capture(request: Request):
    origin = request.headers.get('origin')
    if (not request.client or request.client.host not in ('127.0.0.1', '::1')
            or origin is not None and origin != ORIGIN
            and not re.fullmatch(r'safari-web-extension://[a-fA-F0-9-]{36}', origin)
            or request.headers.get('content-type', '').split(';')[0] != 'application/json'):
        return response({'error': 'Unauthorized'}, 403)
    authorization = request.headers.get('authorization', '')
    if not authorization.startswith('Bearer '):
        return response({'error': 'Unauthorized'}, 403)
    data = b''
    async for chunk in request.stream():
        data += chunk
        if len(data) > LIMIT:
            return response({'error': 'Capture too large'}, 413)
    from server import api
    if api._request_gate.locked():
        return response({'error': 'DeepSeek request active'}, 409)
    async with api._request_gate:
        try:
            result = transfer.capture(authorization[7:], json.loads(data), api.guard)
            if result['status'] == 'saved':
                api.reset_client()
            return response(result)
        except (TransferError, ProviderRejected, OSError, ValueError, TypeError):
            return response({'error': 'Capture refused; inspect account and start a new transfer'}, 409)
