"""Qwen Chat's web API v2, using an independently captured browser session."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from typing import Iterator, Optional

import httpx
from providers.stream_limits import bounded_lines, READ_TIMEOUT, MAX_SECONDS

from chat_protocol import Reply, sse_events
from providers.access import (
    ProviderRejected, rejection, guard, current_attempt,
    _business_code, _failure_kind, _json_type, _protocol_token, _public_code_token,
)
from server.config import QWEN_MODEL_MAP
from .auth import Session, get_session

BASE = "https://chat.qwen.ai"
# Wire ids from QWEN_MODELS (public id → website model).
MODEL_IDS = frozenset(QWEN_MODEL_MAP.values())
DEFAULT_MODEL = (
    "qwen3.8-omni-flash" if "qwen3.8-omni-flash" in MODEL_IDS
    else next(iter(MODEL_IDS), "qwen3.8-omni-flash")
)
_LOG = logging.getLogger(__name__)
# Presence flags only; values are never logged except via _public_code_token.
_TOP_KEYS = ('success', 'data', 'error', 'code', 'status', 'message', 'request_id', 'requestId')
_NESTED_KEYS = ('code', 'status', 'message', 'msg', 'type', 'name', 'id',
                'request_id', 'requestId', 'biz_code', 'biz_msg')
# Key names only (API field identifiers). Values stay out of the log.
_SAFE_KEY = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,39}$')
_MAX_FIELD_KEYS = 24


class QwenStreamError(RuntimeError):
    """A failed, unsupported or prematurely disconnected Qwen response."""


def _key_flags(obj, names):
    if not isinstance(obj, dict):
        return None
    present = [name for name in names if name in obj]
    return ','.join(present) if present else '-'


def _field_map(obj):
    """Safe key names with JSON value types; never value contents."""
    if not isinstance(obj, dict):
        return None, None
    names = sorted(key for key in obj if isinstance(key, str) and _SAFE_KEY.fullmatch(key))
    count = len(obj)
    if not names:
        return ('empty' if count == 0 else 'unsanitized'), count
    clipped = names[:_MAX_FIELD_KEYS]
    rendered = ','.join(f'{name}:{_json_type(obj[name])}' for name in clipped)
    if len(names) > _MAX_FIELD_KEYS:
        rendered += ',...'
    return rendered, count


def _code_fields(obj, data, err):
    raw = None
    if isinstance(data, dict) and 'code' in data:
        raw = data.get('code')
    elif isinstance(err, dict) and 'code' in err:
        raw = err.get('code')
    elif isinstance(obj, dict) and 'code' in obj:
        raw = obj.get('code')
    status_raw = None
    if isinstance(data, dict) and 'status' in data:
        status_raw = data.get('status')
    elif isinstance(err, dict) and 'status' in err:
        status_raw = err.get('status')
    elif isinstance(obj, dict) and 'status' in obj:
        status_raw = obj.get('status')
    return raw, status_raw


def _annotate(error, *, phase, detail=None, http_status=None, protocol=None,
              content_type=None, raw_code=None):
    """Attach allowlisted diagnostics only; never message text or raw payloads."""
    error.phase = phase
    error.protocol = protocol or getattr(error, 'protocol', None) or phase
    if http_status is not None:
        error.http_status = http_status
    if content_type is not None:
        error.content_type = content_type
    if raw_code is not None:
        error.raw_code = raw_code
    if isinstance(detail, dict):
        error.response_type = _json_type(detail)
        if 'success' in detail:
            error.success_true = detail.get('success') is True
        nested = detail.get('data') if isinstance(detail.get('data'), dict) else None
        if nested is None and isinstance(detail.get('error'), dict):
            nested = detail['error']
        code = None
        if isinstance(nested, dict):
            code = _business_code(nested.get('code'))
            if raw_code is None:
                error.raw_code = _public_code_token(nested.get('code'))
        if code is None:
            code = _business_code(detail.get('code'))
            if getattr(error, 'raw_code', None) is None:
                error.raw_code = _public_code_token(detail.get('code'))
        error.business_code = code
    return error


def _log_envelope(value, phase, content_type=None):
    # Fixed schema; never free-form message text, headers, or credentials.
    obj = value if isinstance(value, dict) else {}
    data_value = obj.get('data') if 'data' in obj else None
    data = data_value if isinstance(data_value, dict) else None
    err = obj.get('error') if isinstance(obj.get('error'), dict) else None
    raw, status_raw = _code_fields(obj, data, err)
    business_code = _business_code(raw)
    raw_code = _public_code_token(raw)
    raw_status = _public_code_token(status_raw)
    data_fields, data_key_count = _field_map(data_value if 'data' in obj else None)
    error_fields, error_key_count = _field_map(err)
    top_fields, top_key_count = _field_map(obj)
    _LOG.warning(
        'Qwen envelope rejected: phase=%s content_type=%s response_type=%s '
        'success_present=%s success_true=%s data_present=%s data_type=%s data_keys=%s '
        'data_fields=%s data_key_count=%s error_present=%s error_keys=%s error_fields=%s '
        'error_key_count=%s top_keys=%s top_fields=%s top_key_count=%s '
        'business_code=%s code_type=%s raw_code=%s status_type=%s raw_status=%s',
        phase, content_type, _json_type(value),
        'success' in obj, obj.get('success') is True if 'success' in obj else None,
        'data' in obj, _json_type(data_value) if 'data' in obj else None, _key_flags(data, _NESTED_KEYS),
        data_fields, data_key_count, 'error' in obj, _key_flags(err, _NESTED_KEYS), error_fields,
        error_key_count, _key_flags(obj, _TOP_KEYS), top_fields, top_key_count,
        business_code, _json_type(raw) if raw is not None else None, raw_code,
        _json_type(status_raw) if status_raw is not None else None, raw_status,
    )
    return business_code, raw_code


def _upstream_error(detail, *, phase='business_error', envelope=None, content_type=None):
    code = detail.get("code") if isinstance(detail, dict) else None
    status = int(code) if str(code) in ("401", "403", "429") else None
    if envelope is not None:
        business_code, raw_code = _log_envelope(envelope, phase, content_type=content_type)
    elif isinstance(detail, dict):
        wrapped = detail if 'success' in detail or 'error' in detail else {'data': detail}
        business_code, raw_code = _log_envelope(wrapped, phase, content_type=content_type)
    else:
        business_code, raw_code = None, None
    error = rejection(detail, status=status)
    return _annotate(error, phase=phase, detail=envelope if isinstance(envelope, dict) else detail,
                     protocol='business_error', content_type=content_type, raw_code=raw_code)


def _business(data, *, phase='business_error', content_type=None):
    if not isinstance(data, dict) or data.get("success") is not True:
        detail = data.get("data", {}) if isinstance(data, dict) else {}
        if not isinstance(detail, dict):
            detail = {}
        raise _upstream_error(detail, phase=phase, envelope=data if isinstance(data, dict) else None,
                              content_type=content_type)
    return data.get("data")


def _decode_cid(value: str):
    parts = value.split(":")
    if len(parts) != 4 or parts[0] != "qwen" or parts[1] not in MODEL_IDS:
        raise ValueError("Invalid Qwen conversation_id")
    try:
        uuid.UUID(parts[2])
        uuid.UUID(parts[3])
    except ValueError as exc:
        raise ValueError("Invalid Qwen conversation_id") from exc
    return parts[1], parts[2], parts[3]


def _response_id(obj):
    """Distinguish an absent ID from an explicitly malformed routing key."""
    if "response_id" not in obj:
        return None
    value = obj["response_id"]
    if not isinstance(value, str):
        raise QwenStreamError("Invalid Qwen response_id")
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise QwenStreamError("Invalid Qwen response_id") from exc
    return value


def _validate_choices(choices):
    if not isinstance(choices, list):
        raise QwenStreamError("Invalid Qwen choices")
    for choice in choices:
        if not isinstance(choice, dict):
            raise QwenStreamError("Invalid Qwen choice")
        if not isinstance(choice.get("delta", {}), dict):
            raise QwenStreamError("Invalid Qwen delta")


def _parse_sse(lines, meta: dict) -> Iterator[str]:
    # Qwen can return its quota notice as a normal, successful answer delta.
    # Hold only a possible notice prefix; ordinary answers still stream.
    notices = tuple(
        f"you've reached today's {model} limit. upgrade your membership or switch "
        "to another model above to continue." for model in MODEL_IDS
    )
    pending, checking = "", True
    for content in _parse_sse_events(lines, meta):
        if not checking:
            yield content
            continue
        pending += content
        normalized = " ".join(pending.replace("’", "'").split()).casefold()
        if len(pending) <= 1024 and any(notice.startswith(normalized) for notice in notices):
            continue
        yield pending
        pending, checking = "", False
    if checking and pending:
        if " ".join(pending.replace("’", "'").split()).casefold() in notices:
            raise ProviderRejected("quota_exceeded")
        yield pending


def _parse_sse_events(lines, meta: dict) -> Iterator[str]:
    terminal = False
    primary_id = None
    active_id = None
    known_ids, ignored_ids = set(), set()
    for event, payload in sse_events(lines):
        if event == "error":
            try:
                failure = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise QwenStreamError("Qwen emitted an error event") from exc
            if isinstance(failure, dict):
                envelope = failure
                if failure.get("success") is False:
                    _business(failure, phase='sse_error')
                failure = failure.get("error", failure)
                raise _upstream_error(failure, phase='sse_error', envelope=envelope)
            raise _upstream_error(failure, phase='sse_error')
        if payload == "[DONE]":
            terminal = True
            meta.setdefault("finish_reason", "stop")
            break
        if not payload:
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise QwenStreamError("Invalid JSON in Qwen SSE") from exc
        if not isinstance(obj, dict):
            raise QwenStreamError("Invalid Qwen SSE event shape")
        if obj.get("success") is False:
            _business(obj)
        if obj.get("error"):
            raise _upstream_error(obj["error"], phase='sse_error', envelope=obj)

        # Validate the whole frame before changing ownership or discarding a
        # secondary candidate. Malformed routing keys must not inherit an ID.
        response_id = _response_id(obj)
        created = obj.get("response.created")
        created_id = None
        if "response.created" in obj:
            if not isinstance(created, dict):
                raise QwenStreamError("Invalid Qwen response.created event")
            created_id = _response_id(created)
            if created_id is None:
                raise QwenStreamError("Qwen response.created has no response_id")
            if response_id is not None and response_id != created_id:
                raise QwenStreamError("Conflicting Qwen response_id values")
            if created.get("chat_id") not in (None, meta.get("chat_id")):
                raise QwenStreamError("Qwen returned a different chat_id")
        choices = obj.get("choices", [])
        _validate_choices(choices)
        if "response.stopped" in obj:
            stopped = obj["response.stopped"]
            if not isinstance(stopped, dict):
                raise QwenStreamError("Invalid Qwen response.stopped event")
            stopped_id = _response_id(stopped)
            if stopped_id is not None and (
                (response_id is not None and stopped_id != response_id)
                or (created_id is not None and stopped_id != created_id)
            ):
                raise QwenStreamError("Conflicting Qwen response_id values")
            stopped_id = stopped_id or response_id
            if stopped_id in ignored_ids and stopped_id != primary_id:
                continue
            raise QwenStreamError("Qwen response was stopped before completion")
        if created_id is not None:
            active_id = created_id
            known_ids.add(active_id)
            if created.get("response_index") not in (None, 0, "0"):
                ignored_ids.add(active_id)
            elif primary_id is None:
                primary_id = active_id
        if response_id is not None:
            active_id = response_id
            known_ids.add(active_id)
        elif created_id is None and choices and len(known_ids) > 1:
            raise QwenStreamError("Ambiguous Qwen response without response_id")
        # The web service can stream parallel candidates, all with choice index 0.
        # Only candidate 0 contributes text, completion status and the resume id.
        if active_id in ignored_ids:
            continue
        if primary_id is None and active_id is not None:
            primary_id = active_id
        if active_id is not None and active_id != primary_id:
            continue
        if primary_id is not None:
            meta["message_id"] = primary_id
        for choice in choices:
            if choice.get("index", 0) != 0:
                continue
            was_terminal = terminal
            delta = choice.get("delta", {})
            phase = delta.get("phase")
            if phase in (None, "answer", "final"):
                content = delta.get("content")
                if content is not None and not isinstance(content, str):
                    raise QwenStreamError("Qwen content is not text")
                if terminal and (content or delta.get("status") not in (None, "finished")):
                    raise QwenStreamError("Qwen changed an answer after completion")
                if content:
                    yield content
                if delta.get("status") == "finished":
                    terminal = True
                    meta.setdefault("finish_reason", "stop")
            finish = choice.get("finish_reason")
            if finish is not None:
                if finish not in ("stop", "length"):
                    raise QwenStreamError(f"Unsupported Qwen finish_reason: {finish}")
                if was_terminal and meta.get("finish_reason") != finish:
                    raise QwenStreamError("Qwen changed completion status")
                terminal = True
                meta["finish_reason"] = finish
    if not terminal:
        raise QwenStreamError("Qwen stream ended before a terminal marker")


class QwenClient:
    def __init__(self, session: Optional[Session] = None, *, transport=None, access_guard=guard):
        self.access_guard = access_guard
        if access_guard is not None:
            access_guard.check("qwen")
        self.session = session or get_session()
        self._request_lock = threading.Lock()
        self._http = httpx.Client(
            base_url=BASE, transport=transport, cookies=self.session.cookie_jar(),
            headers={"Authorization": f"Bearer {self.session.token}",
                     "User-Agent": self.session.user_agent, "Content-Type": "application/json",
                     "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9",
                     "Origin": BASE, "Referer": BASE + "/", "source": "web", "Version": "0.3.12"},
            timeout=httpx.Timeout(120, read=300),
        )

    def create_chat(self, model: str, access_attempt=None) -> str:
        if access_attempt is None and self.access_guard is not None:
            with self.access_guard.attempt("qwen") as attempt:
                return self.create_chat(model, attempt)
        if access_attempt is not None:
            access_attempt.dispatch()
        response = self._http.post("/api/v2/chats/new", json={
            "chatId": "", "models": [model], "timestamp": int(time.time() * 1000),
            "chat_type": "t2t", "chat_mode": "normal",
        }, headers={"X-Request-Id": str(uuid.uuid4())})
        response.raise_for_status()
        data = _business(response.json())
        value = data.get("id") if isinstance(data, dict) else None
        if not isinstance(value, str):
            raise QwenStreamError("Qwen chat creation returned no id")
        try:
            uuid.UUID(value)
        except ValueError as exc:
            raise QwenStreamError("Qwen returned an invalid chat id") from exc
        return value

    def stream(self, prompt: str, conversation_id: Optional[str] = None,
               model: Optional[str] = None, thinking: bool = False, search: bool = False):
        if conversation_id:
            if model is not None:
                raise ValueError("A resumed Qwen conversation keeps its original model")
            selected, chat_id, parent_id = _decode_cid(conversation_id)
        else:
            selected, chat_id, parent_id = model or DEFAULT_MODEL, None, None
        if selected not in MODEL_IDS:
            raise ValueError("Unsupported Qwen model")
        return _Stream(self, prompt, selected, chat_id, parent_id, thinking, search)

    def chat(self, prompt: str, conversation_id: Optional[str] = None,
             model: Optional[str] = None, thinking: bool = False, search: bool = False) -> Reply:
        stream = self.stream(prompt, conversation_id, model, thinking, search)
        text = "".join(stream)
        return Reply(text, stream.conversation_id, stream.finish_reason)

    def close(self):
        self._http.close()


class _Stream:
    def __init__(self, client, prompt, model, chat_id, parent_id, thinking, search):
        self.client, self.prompt, self.model = client, prompt, model
        self.chat_id, self.parent_id = chat_id, parent_id
        self.thinking, self.search = thinking, search
        self.message_id = None
        self.finish_reason = "stop"
        self._phase = "before_request"
        self._http_status = None

    def __iter__(self):
        with self.client._request_lock:
            access = getattr(self.client, "access_guard", None)
            attempt = getattr(self, "access_attempt", None) or current_attempt("qwen")
            owned = attempt is None and access is not None
            if owned:
                attempt = access.begin("qwen")
            self.access_attempt = attempt
            try:
                yield from self._generate()
                if owned:
                    attempt.complete()
            except BaseException as exc:
                if not getattr(exc, 'phase', None):
                    _annotate(exc, phase=self._phase, http_status=self._http_status,
                              protocol=_protocol_token(exc))
                _LOG.warning(
                    "Qwen request stopped: phase=%s http_status=%s reason=%s "
                    "exc_type=%s protocol=%s business_code=%s",
                    getattr(exc, 'phase', self._phase),
                    getattr(exc, 'http_status', self._http_status),
                    _failure_kind(exc), type(exc).__name__,
                    _protocol_token(exc), getattr(exc, 'business_code', None),
                )
                if attempt is not None:
                    attempt.abort(exc)
                raise

    def _dispatch(self):
        if self.access_attempt is not None:
            self.access_attempt.dispatch()

    def _generate(self):
        if self.chat_id is None:
            self._phase = "create_chat"
            self.chat_id = self.client.create_chat(self.model, self.access_attempt)
        timestamp = int(time.time())
        chat_type = "search" if self.search else "t2t"
        feature_config = {"thinking_enabled": self.thinking, "auto_thinking": False,
                          "auto_search": self.search, "output_schema": "phase", "research_mode": "normal"}
        body = {
            "stream": True, "version": "2.1", "incremental_output": True,
            "chat_id": self.chat_id, "chatId": self.chat_id, "chat_mode": "normal",
            "model": self.model, "parent_id": self.parent_id, "parentId": self.parent_id or "",
            "timestamp": timestamp,
            "messages": [{"fid": str(uuid.uuid4()), "role": "user", "content": self.prompt,
                          "parentId": self.parent_id, "parent_id": self.parent_id,
                          "childrenIds": [], "models": [self.model], "model": "",
                          "chat_type": chat_type, "sub_chat_type": chat_type,
                          "timestamp": timestamp, "user_action": "chat", "feature_config": feature_config}],
        }
        meta = {"chat_id": self.chat_id}
        self._phase = "completion_dispatch"
        self._dispatch()
        self._phase = "completion_http"
        stream_started = time.monotonic()
        with self.client._http.stream(
            "POST", "/api/v2/chat/completions",
            timeout=httpx.Timeout(READ_TIMEOUT),
            params={"chat_id": self.chat_id},
            json=body,
            headers={"X-Request-Id": str(uuid.uuid4()), "X-Accel-Buffering": "no"},
        ) as response:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                self._http_status = exc.response.status_code
                _annotate(exc, phase='completion_http', http_status=exc.response.status_code,
                          protocol='http_status')
                raise
            self._http_status = response.status_code
            content_type = response.headers.get("content-type", "")
            media_type = content_type.split(";", 1)[0].strip().lower() or None
            if "text/event-stream" not in content_type:
                self._phase = "completion_non_sse"
                error_body = "\n".join(bounded_lines(response, self.access_attempt, started=stream_started))
                try:
                    envelope = json.loads(error_body)
                except json.JSONDecodeError as exc:
                    raise QwenStreamError("Qwen did not return an SSE response") from exc
                _business(envelope, phase='completion_non_sse', content_type=media_type)
                raise QwenStreamError("Qwen did not return an SSE response")
            self._phase = "completion_sse"
            yield from _parse_sse(bounded_lines(response, self.access_attempt, started=stream_started), meta)
            if self.access_attempt is not None:
                self.access_attempt.check()
            if time.monotonic() - stream_started >= MAX_SECONDS:
                raise TimeoutError("Provider stream exceeded total deadline")
        self._phase = "completion_finalize"
        identity = meta.get("message_id")
        try:
            parsed_id = uuid.UUID(identity) if isinstance(identity, str) else None
            if parsed_id is None or (self.parent_id and parsed_id == uuid.UUID(self.parent_id)):
                raise ValueError("Missing or stale response identity")
        except ValueError as exc:
            raise QwenStreamError("Qwen returned a missing, invalid or stale response_id") from exc
        self.message_id = identity
        self.finish_reason = meta["finish_reason"]

    @property
    def conversation_id(self):
        if not self.chat_id or not self.message_id:
            return None
        return f"qwen:{self.model}:{self.chat_id}:{self.message_id}"
