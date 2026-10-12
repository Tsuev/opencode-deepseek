"""Authenticated, loopback-only budget controls for human Harness commands."""
import hmac
import json
import os
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from providers.access import ProviderRejected

router = APIRouter()


def authorized(request):
    if not request.client or request.client.host not in ('127.0.0.1', '::1'):
        return False
    if request.headers.get('origin') or request.headers.get('sec-fetch-site'):
        return False
    try:
        path = os.environ.get('BRIDGE_BUDGET_TOKEN_FILE')
        token = Path(path).read_text().strip() if path else ''
    except OSError:
        return False
    return len(token) >= 32 and hmac.compare_digest(
        request.headers.get('authorization', '').encode('utf-8'), ('Bearer ' + token).encode('utf-8'))


def status_with_conversation_limit(guard):
    from server.api import conversation_cache, SESSION_MAX_AGE
    from deepseek.stream_diagnostics import StreamCapture
    return {**guard.budget_status(), 'conversation': {
        'max_replies': conversation_cache.max_replies,
        'reset_by_budget_reset': False,
    }, 'session_policy': {'max_age_seconds': SESSION_MAX_AGE},
        'stream_diagnostics': {'enabled': StreamCapture().enabled}}


@router.get('/admin/budget')
async def budget_status(request: Request):
    if not authorized(request):
        return JSONResponse({'error': 'Unauthorized'}, status_code=403)
    from server.api import guard
    try:
        return status_with_conversation_limit(guard)
    except ProviderRejected:
        return JSONResponse({'error': 'Safety state unavailable'}, status_code=503)


@router.post('/admin/budget/reset')
async def budget_reset(request: Request):
    if not authorized(request):
        return JSONResponse({'error': 'Unauthorized'}, status_code=403)
    body = b''
    async for chunk in request.stream():
        body += chunk
        if len(body) > 2048:
            return JSONResponse({'error': 'Request too large'}, status_code=413)
    try:
        data = json.loads(body)
        if (not isinstance(data, dict) or set(data) != {'confirmation', 'revision'}
                or data['confirmation'] != 'RESET LOCAL BUDGET'
                or not isinstance(data['revision'], str) or len(data['revision']) != 64):
            raise ValueError()
    except (ValueError, TypeError):
        return JSONResponse({'error': 'Explicit confirmation and current revision required'}, status_code=400)
    from server.api import guard
    try:
        guard.reset_budget(data['revision'])
        return status_with_conversation_limit(guard)
    except RuntimeError as exc:
        if isinstance(exc, ProviderRejected):
            return JSONResponse({'error': 'Safety state unavailable'}, status_code=503)
        return JSONResponse({'error': str(exc)}, status_code=409)
