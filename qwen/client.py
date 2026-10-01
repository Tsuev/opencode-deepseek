"""Qwen Chat's web API v2, using an independently captured browser session."""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Iterator, Optional

import httpx

from chat_protocol import Reply, sse_events
from .auth import Session, get_session

BASE = "https://chat.qwen.ai"
MODEL_IDS = frozenset(("qwen3.8-omni-flash", "qwen3.8-max"))
DEFAULT_MODEL = "qwen3.8-omni-flash"


class QwenStreamError(RuntimeError):
    """A failed, unsupported or prematurely disconnected Qwen response."""


def _upstream_error(detail):
    if isinstance(detail, dict):
        code = detail.get("code", "unknown_error")
        message = detail.get("details") or detail.get("message") or "Request rejected"
    else:
        code, message = "unknown_error", str(detail) if detail else "Request rejected"
    # The server recognises auth failures by their message, including web API
    # errors delivered inside HTTP 200 SSE responses rather than HTTP 401/403.
    kind = "authorization error" if code in (401, 403, "401", "403") else "error"
    return QwenStreamError(f"Qwen {kind} ({code}): {message}")


def _business(data):
    if not isinstance(data, dict) or data.get("success") is not True:
        detail = data.get("data", {}) if isinstance(data, dict) else {}
        if not isinstance(detail, dict):
            detail = {}
        raise _upstream_error(detail)
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
                if failure.get("success") is False:
                    _business(failure)
                failure = failure.get("error", failure)
            raise _upstream_error(failure)
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
            raise _upstream_error(obj["error"])

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
            delta = choice.get("delta", {})
            phase = delta.get("phase")
            if phase in (None, "answer", "final"):
                content = delta.get("content")
                if content is not None and not isinstance(content, str):
                    raise QwenStreamError("Qwen content is not text")
                if content:
                    yield content
                if delta.get("status") == "finished":
                    terminal = True
                    meta.setdefault("finish_reason", "stop")
            finish = choice.get("finish_reason")
            if finish is not None:
                if finish not in ("stop", "length"):
                    raise QwenStreamError(f"Unsupported Qwen finish_reason: {finish}")
                terminal = True
                meta["finish_reason"] = finish
    if not terminal:
        raise QwenStreamError("Qwen stream ended before a terminal marker")


class QwenClient:
    def __init__(self, session: Optional[Session] = None, *, transport=None):
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

    def create_chat(self, model: str) -> str:
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

    def __iter__(self):
        with self.client._request_lock:
            yield from self._generate()

    def _generate(self):
        if self.chat_id is None:
            self.chat_id = self.client.create_chat(self.model)
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
        with self.client._http.stream("POST", "/api/v2/chat/completions", params={"chat_id": self.chat_id},
                                     json=body, headers={"X-Request-Id": str(uuid.uuid4()),
                                                         "X-Accel-Buffering": "no"}) as response:
            response.raise_for_status()
            if "text/event-stream" not in response.headers.get("content-type", ""):
                response.read()
                _business(response.json())
                raise QwenStreamError("Qwen did not return an SSE response")
            yield from _parse_sse(response.iter_lines(), meta)
        self.message_id = meta.get("message_id")
        if not self.message_id:
            raise QwenStreamError("Qwen completion returned no response_id")
        try:
            uuid.UUID(self.message_id)
        except ValueError as exc:
            raise QwenStreamError("Qwen returned an invalid response_id") from exc
        self.finish_reason = meta["finish_reason"]

    @property
    def conversation_id(self):
        if not self.chat_id or not self.message_id:
            return None
        return f"qwen:{self.model}:{self.chat_id}:{self.message_id}"
