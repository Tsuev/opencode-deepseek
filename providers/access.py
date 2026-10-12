"""Durable provider attempts and pauses; no upstream response data is stored."""
import argparse
import contextlib
from contextvars import ContextVar
import json
import hashlib
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

import httpx
from settings import ROOT

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

MAX_BUDGET = 2000

PROVIDERS = frozenset(('deepseek', 'qwen', 'grok', 'mistral', 'kimi', 'glm', 'gemini', 'chatgpt', 'claude'))
MESSAGES = {
    'local_budget_exceeded': 'Local request budget exhausted. No automatic retry; limits survive restart and resume.',
    'local_prompt_too_large': 'The prompt exceeds the local per-request size limit (128 KiB). Shorten the context; waiting or resetting budgets does not help.',
    'account_restricted': 'The provider restricted this account. Stop and inspect the normal website.',
    'quota_exceeded': 'The provider rejected the request because of a quota or usage limit.',
    'conversation_context_exceeded': 'DeepSeek conversation context limit reached. This is not a local budget or session expiry. No automatic new chat or replay; preserve the goal before manually migrating to a new conversation.',
    'capacity_unavailable': 'The provider has no capacity for this free account right now. Automatic retries are disabled.',
    'access_denied': 'The provider denied access. Inspect sign-in, security and regional restrictions manually.',
    'session_expired': 'The provider rejected the session. Sign in manually before resuming.',
    'upstream_failure': 'The provider request failed or its outcome is uncertain. Automatic replay is disabled.',
    'safety_state_unavailable': 'Cannot persist or read provider safety state. Requests are stopped until it is repaired.',
}
_active = ContextVar('provider_attempt', default=None)
_log = logging.getLogger(__name__)
# Peek at initiating HTTP bodies for shape only; never log message text or secrets.
_MAX_DIAGNOSTIC_BODY = 64 * 1024
_KNOWN_BUSINESS_CODES = frozenset({
    '401', '403', '429', 'muted', 'invalid_token', 'unauthorized', 'forbidden',
    'access_prohibited', 'account_pending', 'data_inspection_failed', 'bad_request',
})


# Fixed QwenStreamError messages only; never log free-form upstream text.
_PROTOCOL_TOKENS = {
    'Qwen stream ended before a terminal marker': 'stream_no_terminal',
    'Qwen did not return an SSE response': 'non_sse_response',
    'Qwen completion returned no response_id': 'missing_response_id',
    'Qwen returned an invalid response_id': 'invalid_response_id',
    'Qwen emitted an error event': 'sse_error_event',
    'Invalid JSON in Qwen SSE': 'sse_invalid_json',
    'Invalid Qwen SSE event shape': 'sse_invalid_shape',
    'Invalid Qwen response_id': 'invalid_response_id',
    'Invalid Qwen choices': 'invalid_choices',
    'Invalid Qwen choice': 'invalid_choice',
    'Invalid Qwen delta': 'invalid_delta',
    'Invalid Qwen response.created event': 'invalid_created',
    'Qwen response.created has no response_id': 'created_no_id',
    'Conflicting Qwen response_id values': 'conflicting_response_id',
    'Qwen returned a different chat_id': 'chat_id_mismatch',
    'Invalid Qwen response.stopped event': 'invalid_stopped',
    'Qwen response was stopped before completion': 'stopped_early',
    'Ambiguous Qwen response without response_id': 'ambiguous_response_id',
    'Qwen content is not text': 'non_text_content',
    'Qwen chat creation returned no id': 'chat_create_no_id',
    'Qwen returned an invalid chat id': 'chat_create_invalid_id',
}


def _protocol_token(exc):
    name = type(exc).__name__
    if name == 'CancelledError':
        return 'cancelled'
    attached = getattr(exc, 'protocol', None)
    # Only accept short tokens we attach ourselves; never free-form upstream text.
    if isinstance(attached, str) and 0 < len(attached) <= 40 and attached.replace('_', '').isalnum():
        return attached
    if name != 'QwenStreamError':
        return None
    message = str(exc)
    if message in _PROTOCOL_TOKENS:
        return _PROTOCOL_TOKENS[message]
    if message.startswith('Unsupported Qwen finish_reason:'):
        return 'unsupported_finish_reason'
    return 'qwen_protocol_error'


def _failure_kind(exc):
    # Never log exception text, upstream bodies, headers, or account values.
    if isinstance(exc, ProviderRejected):
        return exc.code
    if isinstance(exc, httpx.HTTPStatusError):
        return 'http_status'
    if isinstance(exc, httpx.TimeoutException):
        return 'transport_timeout'
    if isinstance(exc, httpx.RequestError):
        return 'transport_error'
    if type(exc).__name__ == 'CancelledError':
        return 'cancelled'
    if type(exc).__name__ == 'ProviderUnavailable':
        return 'provider_unavailable'
    if type(exc).__name__ == 'QwenStreamError' or isinstance(exc, (ValueError, KeyError, TypeError, json.JSONDecodeError)):
        return 'invalid_protocol'
    return 'unexpected_failure'


def _json_type(item):
    return {dict: 'object', list: 'array', str: 'string', bool: 'boolean',
            int: 'number', float: 'number', type(None): 'null'}.get(type(item), 'other')


def _business_code(value):
    """Allowlisted public codes only; never free-form message text."""
    if type(value) is int and value in (401, 403, 429):
        return value
    if isinstance(value, str):
        if value in ('401', '403', '429'):
            return int(value)
        key = value.lower()
        if key in _KNOWN_BUSINESS_CODES:
            return key
    return None


# Short public identifiers only (API enums / numeric codes). Never messages or URLs.
_SAFE_CODE = re.compile(r'^[A-Za-z0-9_.-]{1,40}$')


def _public_code_token(value):
    """Return a loggable code token, or None when the value may contain secrets/text."""
    known = _business_code(value)
    if known is not None:
        return known
    if type(value) is int and not isinstance(value, bool) and abs(value) < 10 ** 9:
        return value
    if isinstance(value, str) and _SAFE_CODE.fullmatch(value):
        return value
    return None


def _response_body(response):
    try:
        if hasattr(response, 'is_stream_consumed') and not response.is_stream_consumed:
            response.read()
        return response.content
    except Exception:
        try:
            return response.read()
        except Exception:
            return b''


def _response_diagnostics(response):
    """Fixed schema for pause logs: shape and allowlisted codes, never payload text."""
    content_type = (response.headers.get('content-type') or '').split(';', 1)[0].strip().lower() or None
    raw = _response_body(response)
    length = len(raw)
    truncated = length > _MAX_DIAGNOSTIC_BODY
    sample = raw[:_MAX_DIAGNOSTIC_BODY]
    response_type = 'empty'
    business_code = None
    success_true = None
    if not sample:
        return content_type, length, truncated, response_type, business_code, success_true
    looks_json = (content_type and 'json' in content_type) or sample[:1] in (b'{', b'[')
    if looks_json:
        try:
            value = json.loads(sample.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return content_type, length, truncated, 'invalid_json', None, None
        response_type = _json_type(value)
        if isinstance(value, dict):
            if 'success' in value:
                success_true = value.get('success') is True
            detail = value.get('data') if isinstance(value.get('data'), dict) else None
            if detail is None and isinstance(value.get('error'), dict):
                detail = value['error']
            if isinstance(detail, dict):
                business_code = _business_code(detail.get('code'))
            if business_code is None:
                business_code = _business_code(value.get('code'))
        return content_type, length, truncated, response_type, business_code, success_true
    if (content_type and 'html' in content_type) or sample.lstrip()[:1] == b'<':
        return content_type, length, truncated, 'html', None, None
    return content_type, length, truncated, 'other', None, None


def _log_new_pause(provider, code, exc):
    # Never log exception text, upstream bodies, credentials, prompts or free-form messages.
    http_status = getattr(exc, 'http_status', None)
    content_type = getattr(exc, 'content_type', None)
    body_length = body_truncated = None
    response_type = getattr(exc, 'response_type', None)
    business_code = getattr(exc, 'business_code', None)
    raw_code = getattr(exc, 'raw_code', None)
    success_true = getattr(exc, 'success_true', None)
    phase = getattr(exc, 'phase', None)
    if isinstance(exc, httpx.HTTPStatusError):
        http_status = exc.response.status_code
        (content_type, body_length, body_truncated, response_type,
         business_code, success_true) = _response_diagnostics(exc.response)
        phase = phase or 'http_error'
    _log.warning(
        'Provider pause set: provider=%s code=%s reason=%s exc_type=%s phase=%s protocol=%s '
        'http_status=%s content_type=%s body_length=%s body_truncated=%s response_type=%s '
        'business_code=%s raw_code=%s success_true=%s',
        provider, code, _failure_kind(exc), type(exc).__name__, phase, _protocol_token(exc),
        http_status, content_type, body_length, body_truncated, response_type,
        business_code, raw_code, success_true,
    )


class ProviderRejected(RuntimeError):
    """Allowlisted refusal. source=local_pause means providers.access already blocks dispatch."""

    def __init__(self, code='upstream_failure', *, source='upstream'):
        self.code = code if code in MESSAGES else 'upstream_failure'
        # local_limit: refused locally; nothing was sent unless `paused` is set.
        self.source = source if source in ('upstream', 'local_pause', 'local_limit') else 'upstream'
        self.paused = False
        super().__init__(MESSAGES[self.code])


def rejection(detail=None, status=None):
    text = str(detail).lower() if detail is not None else ''
    if 'muted' in text or any(word in text for word in ('banned', 'suspended', 'account blocked')):
        return ProviderRejected('account_restricted')
    if status == 429 or any(word in text for word in ('quota', 'rate_limit', 'rate limit', 'usage limit')):
        return ProviderRejected('quota_exceeded')
    if status == 403 or any(word in text for word in ('forbidden', 'region', 'security', 'access denied')):
        return ProviderRejected('access_denied')
    if status == 401 or any(word in text for word in ('invalid_token', 'token expired', 'unauthorized')):
        return ProviderRejected('session_expired')
    return ProviderRejected()


def current_attempt(provider):
    attempt = _active.get()
    return attempt if attempt is not None and attempt.provider == provider else None


def _file_lock(file, unlock=False):
    if os.name == 'nt':
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(file, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX)


def _configured_budgets():
    """Bounded rolling ceilings; MAX_BUDGET is also the persisted ledger size limit."""
    limits = []
    for name, category, defaults in (
        ('HTTP', None, (40, 200)), ('CHATS', 'chat', (3, 6)),
        ('GENERATIONS', 'generation', (12, 60)),
    ):
        values = []
        for suffix, default in zip(('HOUR', 'DAY'), defaults):
            key = f'DEEPSEEK_MAX_{name}_PER_{suffix}'
            raw = os.getenv(key, str(default))
            if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= MAX_BUDGET:
                raise ValueError(f'{key} must be an integer between 1 and {MAX_BUDGET}')
            values.append(int(raw))
        if values[0] > values[1]:
            raise ValueError(f'DEEPSEEK_MAX_{name}_PER_DAY must be >= PER_HOUR')
        limits.extend((window, category, value)
                      for window, value in zip((3600, 86400), values))
    return tuple(limits)


class AccessGuard:
    def __init__(self, path=ROOT / 'session' / 'provider-pauses.json', interval=None):
        self.path = Path(path) if path is not None else None
        self.interval = float(os.getenv('PROVIDER_MIN_INTERVAL', '30') if interval is None else interval)
        if not math.isfinite(self.interval) or not 0 <= self.interval <= 3600:
            raise ValueError('PROVIDER_MIN_INTERVAL must be between 0 and 3600 seconds')
        self.budget_limits = _configured_budgets()
        self._lock = threading.RLock()
        self._memory = {}
        self._failed = set()
        self._live = {}

    @staticmethod
    def _decode(state):
        if not isinstance(state, dict):
            raise ValueError('Invalid safety state')
        records = {}
        for provider, item in state.items():
            if provider not in PROVIDERS:
                raise ValueError('Unknown provider')
            # Upgrade the initial flat pause format without losing restrictions.
            if isinstance(item, str) and item in MESSAGES:
                item = {'pause': item}
            if not isinstance(item, dict) or set(item) - {'pause', 'pending', 'dispatched', 'next_at', 'next_http_at', 'usage', 'recoverable_auth'}:
                raise ValueError('Invalid provider record')
            if 'recoverable_auth' in item and (item['recoverable_auth'] is not True
                    or provider != 'deepseek' or item.get('pause') != 'session_expired'
                    or not item.get('pending') or item.get('dispatched') is not True):
                raise ValueError('Invalid auth recovery evidence')
            pause, pending, dispatched, next_at = item.get('pause'), item.get('pending'), item.get('dispatched', False), item.get('next_at', 0)
            if (pause is not None and (not isinstance(pause, str) or pause not in MESSAGES)
                    or pending is not None and (not isinstance(pending, str) or len(pending) != 32)
                    or not isinstance(dispatched, bool)
                    or isinstance(next_at, bool) or not isinstance(next_at, (int, float))
                    or not math.isfinite(next_at) or next_at < 0):
                raise ValueError('Invalid provider state')
            if dispatched and pending is None:
                raise ValueError('Dispatch record lacks an attempt owner')
            next_http_at = item.get('next_http_at', 0)
            if (isinstance(next_http_at, bool) or not isinstance(next_http_at, (int, float))
                    or not math.isfinite(next_http_at) or next_http_at < 0):
                raise ValueError('Invalid HTTP pacing state')
            usage = item.get('usage', [])
            if (not isinstance(usage, list) or len(usage) > MAX_BUDGET
                    or any(not isinstance(entry, list) or len(entry) != 2
                           or entry[1] not in ('http', 'chat', 'generation')
                           or isinstance(entry[0], bool) or not isinstance(entry[0], (int, float))
                           or not math.isfinite(entry[0]) or entry[0] < 0 for entry in usage)):
                raise ValueError('Invalid usage ledger')
            records[provider] = dict(item)
        return records

    @contextlib.contextmanager
    def _state(self):
        with self._lock:
            if self.path is None:
                yield self._memory
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with self.path.with_suffix('.lock').open('a+') as lock:
                    os.chmod(lock.name, 0o600)
                    _file_lock(lock)
                    try:
                        state = self._decode(json.loads(self.path.read_text())) if self.path.exists() else {}
                        yield state
                    finally:
                        _file_lock(lock, unlock=True)
            except (OSError, ValueError, TypeError) as exc:
                raise ProviderRejected('safety_state_unavailable') from exc

    def _save(self, state):
        if self.path is None:
            return
        fd, temporary = tempfile.mkstemp(prefix='.provider-pauses-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as output:
                if hasattr(os, 'fchmod'):
                    os.fchmod(output.fileno(), 0o600)
                json.dump(state, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            if os.name != 'nt':
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def check(self, provider, owner=None, queued=False):
        if provider not in PROVIDERS:
            raise ValueError('Unknown provider')
        if provider in self._failed:
            raise ProviderRejected('safety_state_unavailable')
        active = current_attempt(provider)
        owner = owner or (active.id if active is not None and active.guard is self else None)
        with self._state() as state:
            record = state.get(provider, {})
            if record.get('pause'):
                raise ProviderRejected(record['pause'], source='local_pause')
            if owner is not None and record.get('pending') != owner:
                raise ProviderRejected('upstream_failure')
            if (record.get('pending') and record['pending'] != owner
                    and not (queued and self._live.get(provider) == record['pending'])):
                raise ProviderRejected('upstream_failure')

    def begin(self, provider, cancelled=None):
        attempt = Attempt(self, provider, cancelled)
        self.check(provider)
        with self._state() as state:
            record = state.setdefault(provider, {})
            if record.get("pause") or record.get("pending"):
                raise ProviderRejected(
                    record.get("pause") or "upstream_failure",
                    source='local_pause' if record.get("pause") else 'upstream',
                )
            record.update(pending=attempt.id, dispatched=False)
            try:
                self._save(state)  # Write-ahead: failure here prohibits all dispatch.
                self._live[provider] = attempt.id
            except OSError as exc:
                self._failed.add(provider)
                raise ProviderRejected('safety_state_unavailable') from exc
        return attempt

    def retire_context_refusal(self, attempt):
        """Retire only this in-memory proven refusal; keep every budget entry."""
        if (attempt.guard is not self or attempt.provider != 'deepseek'
                or attempt.completed or not getattr(attempt, 'empty_context_refusal', False)
                or not attempt.generation_dispatched or 'deepseek' in self._failed):
            raise ProviderRejected('upstream_failure')
        with self._state() as state:
            record = state.get('deepseek', {})
            if (record.get('pending') != attempt.id
                    or record.get('pause') != 'conversation_context_exceeded'):
                raise ProviderRejected('upstream_failure')
            for key in ('pending', 'dispatched', 'pause'):
                record.pop(key, None)
            try:
                self._save(state)
            except OSError as exc:
                self._failed.add('deepseek')
                raise ProviderRejected('safety_state_unavailable') from exc
            attempt.completed = True

    @contextlib.contextmanager
    def attempt(self, provider, cancelled=None):
        attempt = self.begin(provider, cancelled)
        with attempt.bind():
            try:
                yield attempt
                attempt.complete()
            except BaseException as exc:
                attempt.abort(exc)
                raise

    def wait(self, provider, cancelled=None):
        # Compatibility helper; actual adapters use durable attempt ownership.
        with self.attempt(provider, cancelled) as attempt:
            attempt.dispatch()

    def reject(self, provider, exc, owner=None):
        error = exc if isinstance(exc, ProviderRejected) else rejection(
            exc, status=exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None)
        newly_paused = False
        try:
            with self._state() as state:
                if owner is not None and state.get(provider, {}).get('pending') != owner:
                    return ProviderRejected('upstream_failure')
                record = state.setdefault(provider, {})
                previous = record.get('pause')
                record.setdefault('pause', error.code)
                self._save(state)
                # First latch is upstream-driven; later hits are the durable local pause.
                local = (isinstance(exc, ProviderRejected) and exc.source == 'local_limit'
                         and previous is None)
                error = ProviderRejected(
                    record['pause'],
                    source='local_limit' if local else 'local_pause' if previous else 'upstream',
                )
                error.paused = True
                newly_paused = previous is None
        except (OSError, ProviderRejected):
            self._failed.add(provider)
            return ProviderRejected('safety_state_unavailable')
        if newly_paused:
            _log_new_pause(provider, error.code, exc)
        return error

    def resume(self, provider):
        if provider not in PROVIDERS:
            raise ValueError('Unknown provider')
        with self._state() as state:
            record = state.setdefault(provider, {})
            record.pop('pause', None)
            record.pop('pending', None)
            record.pop('dispatched', None)
            record.pop('recoverable_auth', None)
            self._save(state)
            self._failed.discard(provider)

    @staticmethod
    def _usage_revision(record):
        return hashlib.sha256(json.dumps(record.get('usage', []), separators=(',', ':')).encode()).hexdigest()

    def budget_status(self):
        now = time.time()
        with self._state() as state:
            record = state.get('deepseek', {})
            usage = record.get('usage', [])
            rows = []
            for window, category, limit in self.budget_limits:
                entries = sorted(t for t, kind in usage if t > now - window
                                 and (category is None or kind == category))
                needed = max(0, len(entries) + 1 - limit)
                rows.append({'category': category or 'http', 'window_seconds': window,
                             'used': len(entries), 'limit': limit,
                             'remaining': max(0, limit - len(entries)),
                             'next_available_at': entries[needed - 1] + window if needed else now})
            return {'provider': 'deepseek', 'at': now, 'budgets': rows,
                    'revision': self._usage_revision(record),
                    'paused': record.get('pause'), 'busy': bool(record.get('pending')),
                    'min_interval_seconds': self.interval}

    def reset_budget(self, revision):
        with self._state() as state:
            record = state.setdefault('deepseek', {})
            if record.get('pending'):
                raise RuntimeError('A request is active or unresolved; counters were not reset')
            if self._usage_revision(record) != revision:
                raise RuntimeError('Counters changed; inspect them again before resetting')
            # Only usage changes. Provider pauses and pacing survive this operation.
            record['usage'] = []
            self._save(state)

    def status(self):
        with self._state() as state:
            return {name: ('safety_state_unavailable' if name in self._failed else record.get('pause') or 'upstream_failure')
                    for name, record in state.items() if name in self._failed or record.get('pause') or record.get('pending')}


def _budget_usage(record, kind, now, http_cost=1, limits=None):
    usage = [entry for entry in record.get('usage', []) if entry[0] > now - 86400]
    # Local ceilings only, not provider-approved quotas.
    limits = _configured_budgets() if limits is None else limits
    for window, category, maximum in limits:
        if category is None or category == kind:
            count = sum(t > now - window and (category is None or k == category)
                        for t, k in usage)
            if count + (http_cost if category is None else 1) > maximum:
                error = ProviderRejected('local_budget_exceeded', source='local_limit')
                error.before_dispatch = True
                raise error
    return usage


class Attempt:
    def __init__(self, guard, provider, cancelled):
        self.guard, self.provider, self.cancelled = guard, provider, cancelled
        self.id = uuid.uuid4().hex
        self.completed = False
        self.dispatched = False
        self.generation_dispatched = False
        self.challenge_auth_rejected = False

    @contextlib.contextmanager
    def bind(self):
        token = _active.set(self)
        try:
            yield self
        finally:
            _active.reset(token)

    def check(self):
        if self.cancelled:
            self.cancelled()
        self.guard.check(self.provider, self.id)
        if self.cancelled:
            self.cancelled()

    def preflight_generation(self, new_chat):
        """Check the whole HTTP sequence before creating a chat or challenge."""
        self.check()
        if self.provider == 'deepseek':
            with self.guard._state() as state:
                record = state[self.provider]
                _budget_usage(record, 'generation', time.time(), 3 if new_chat else 2, self.guard.budget_limits)
                if new_chat:
                    _budget_usage(record, 'chat', time.time(), 3, self.guard.budget_limits)

    def dispatch(self, validate=None, kind="http"):
        if kind not in ('http', 'chat', 'generation'):
            raise ValueError('Unknown dispatch kind')
        while True:
            self.check()
            if validate is not None:
                validate()
            with self.guard._state() as state:
                record = state[self.provider]
                if record.get('pending') != self.id or record.get('pause'):
                    raise ProviderRejected(
                        record.get('pause') or 'upstream_failure',
                        source='local_pause' if record.get('pause') else 'upstream',
                    )
                delay = 0 if self.dispatched else record.get('next_at', 0) - time.time()
                if self.provider == 'deepseek':
                    delay = max(delay, record.get('next_http_at', 0) - time.time())
                if delay <= 0:
                    entry = None
                    first = not self.dispatched
                    restore = {key: record[key] for key in ('next_at', 'next_http_at') if key in record}
                    if self.provider == 'deepseek':
                        now = time.time()
                        usage = _budget_usage(record, kind, now, limits=self.guard.budget_limits)
                        entry = [now, kind]
                        record['usage'] = [*usage, entry]
                        record['next_http_at'] = now + min(self.guard.interval, 3)
                    if validate is not None:
                        validate()
                    if first:
                        record.update(dispatched=True, next_at=time.time() + self.guard.interval)
                    # Conservative even if persisting the dispatch later fails.
                    generation_before = self.generation_dispatched
                    if kind == 'generation':
                        self.generation_dispatched = True
                    self.guard._save(state)
                    self.dispatched = True
            if delay <= 0:
                # Saving/fsync and lock acquisition can block. Revoke the
                # authorization if cancellation or resume happened meanwhile.
                try:
                    self.check()
                    if validate is not None:
                        validate()
                except BaseException:
                    # Control never returned to the transport: this dispatch
                    # provably sent nothing. Retract only it; earlier sends of
                    # this attempt and crash recovery stay conservative.
                    self._retract_unsent(entry, first, restore, generation_before)
                    raise
                return
            time.sleep(min(delay, 0.1))

    def _retract_unsent(self, entry, first, restore, generation_before):
        try:
            with self.guard._state() as state:
                record = state.get(self.provider, {})
                if record.get('pending') != self.id:
                    return
                if entry is not None:
                    usage = record.get('usage', [])
                    for index in range(len(usage) - 1, -1, -1):
                        if usage[index] == entry:
                            del usage[index]
                            break
                    for key in ('next_at', 'next_http_at'):
                        if key in restore:
                            record[key] = restore[key]
                        elif key == 'next_http_at':
                            record.pop(key, None)
                if first:
                    record['dispatched'] = False
                    if 'next_at' in restore:
                        record['next_at'] = restore['next_at']
                    else:
                        record.pop('next_at', None)
                self.guard._save(state)
                if first:
                    self.dispatched = False
                self.generation_dispatched = generation_before
        except (OSError, ProviderRejected):
            pass  # Unproven retraction keeps the conservative journal.

    def complete(self):
        if self.completed:
            return
        with self.guard._state() as state:
            record = state[self.provider]
            if record.get('pending') != self.id:
                raise ProviderRejected('upstream_failure')
            record.pop('pending', None)
            record.pop('dispatched', None)
            self.guard._save(state)
            self.completed = True
            self.guard._live.pop(self.provider, None)

    def abort(self, exc=None):
        if self.completed:
            return
        if self.guard._live.get(self.provider) == self.id:
            self.guard._live.pop(self.provider, None)
        # Cancellation before dispatch is harmless; afterwards an unverified
        # outcome remains durably pending even if writing the pause fails.
        if self.dispatched or isinstance(exc, Exception) and not isinstance(exc, ValueError) and not getattr(exc, 'before_dispatch', False):
            self.guard.reject(self.provider, exc or ProviderRejected(), owner=self.id)
            if (self.provider == 'deepseek' and self.challenge_auth_rejected
                    and not self.generation_dispatched):
                with self.guard._state() as state:
                    record = state[self.provider]
                    if record.get('pending') == self.id and record.get('pause') == 'session_expired':
                        record['recoverable_auth'] = True
                        self.guard._save(state)
        else:
            try:
                self.complete()
            except OSError:
                self.guard._failed.add(self.provider)
            except ProviderRejected as error:
                if error.code == 'safety_state_unavailable':
                    self.guard._failed.add(self.provider)


guard = AccessGuard()


@contextlib.contextmanager
def provider_attempt(provider, cancelled=None):
    existing = current_attempt(provider)
    if existing is not None:
        existing.check()
        yield existing
    else:
        with guard.attempt(provider, cancelled) as attempt:
            yield attempt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Inspect pauses; resume only after manually verifying normal website access.')
    parser.add_argument('command', choices=('status', 'resume'))
    parser.add_argument('provider', nargs='?', choices=sorted(PROVIDERS))
    args = parser.parse_args()
    if args.command == 'resume':
        if not args.provider:
            parser.error('resume requires a provider')
        guard.resume(args.provider)
    print(json.dumps(guard.status(), sort_keys=True))
