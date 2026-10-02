"""
Pure-HTTP DeepSeek chat client.

Speaks chat.deepseek.com's internal API directly using a captured signed-in
session (see `deepseek.auth`). For each message it:

    1. creates a chat session   (POST /api/v0/chat_session/create)
    2. fetches a PoW challenge   (POST /api/v0/chat/create_pow_challenge)
    3. solves it via the WASM    (deepseek.pow.DeepSeekPow)
    4. POSTs the completion       with the x-ds-pow-response header
    5. parses the SSE stream      into text

    from deepseek.auth import get_session
    from deepseek.client import DeepSeekClient

    client = DeepSeekClient(get_session())
    print(client.chat("Hello!"))                 # full reply
    for chunk in client.stream("Tell a joke"):   # streamed
        print(chunk, end="", flush=True)
"""

from __future__ import annotations

import json
import re
import threading
from typing import Iterator, Optional

import httpx

from chat_protocol import Reply, sse_events as _sse_events
from providers.access import ProviderRejected, rejection, guard, current_attempt

from .auth import Session, get_session
from .pow import DeepSeekPow

BASE = "https://chat.deepseek.com"
COMPLETION_PATH = "/api/v0/chat/completion"

# DeepSeek's mode pill, sent as `model_type` in the completion body. "default" is
# Instant (the fast model); "expert" is the stronger, slower model. Omitting the
# field lets the backend pick, so we always send one explicitly.
DEFAULT_MODEL_TYPE = "default"

# A conversation_id is an opaque "<chat_session_id>:<last_message_id>" token. It
# carries everything needed to resume a thread, so the client stays stateless.
_CID_SEP = ":"


def _encode_cid(session_id: str, message_id: Optional[int]) -> str:
    if message_id is None:
        return session_id
    return f"{session_id}{_CID_SEP}{message_id}"


def _decode_cid(conversation_id: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    """Split a conversation_id back into (chat_session_id, parent_message_id)."""
    if not conversation_id:
        return None, None
    session_id, _, msg = conversation_id.partition(_CID_SEP)
    parent = int(msg) if msg.isdigit() else None
    return (session_id or None), parent


def _biz(data: dict) -> dict:
    """Unwrap DeepSeek's `data.biz_data` envelope, raising on API-level errors."""
    if not isinstance(data, dict):
        raise ProviderRejected()
    envelope = data.get("data")
    if data.get("code") != 0:
        raise rejection(data)
    if not isinstance(envelope, dict):
        raise ProviderRejected()
    if envelope.get("biz_code", 0) != 0:
        if envelope.get("biz_code") == 5:
            raise ProviderRejected("account_restricted")
        raise rejection(envelope)
    biz = envelope.get("biz_data")
    if not isinstance(biz, dict):
        raise ProviderRejected()
    return biz


class DeepSeekClient:
    def __init__(
        self,
        session: Optional[Session] = None,
        allow_interactive: bool = False,
        access_guard=guard,
    ):
        self.access_guard = access_guard
        if access_guard is not None:
            access_guard.check("deepseek")
        # `allow_interactive=False` makes session resolution non-blocking: it
        # uses a cached/headless session and raises LoginRequired instead of
        # opening a browser window. The server passes False (see server/api.py).
        self.session = session or get_session(allow_interactive=allow_interactive)
        self._pow = DeepSeekPow()
        # The wasmtime Store behind the PoW solver is not reentrant; serialise
        # access so concurrent server requests don't corrupt it.
        self._pow_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._http = httpx.Client(
            base_url=BASE,
            headers=self._base_headers(),
            cookies=self.session.cookie_jar(),
            timeout=httpx.Timeout(120.0, read=300.0),
        )

    def _base_headers(self) -> dict:
        return {
            "authorization": f"Bearer {self.session.token}",
            "accept": "*/*",
            "content-type": "application/json",
            "user-agent": self.session.user_agent,
            "origin": BASE,
            "referer": f"{BASE}/",
        }

    # --- protocol steps -----------------------------------------------------

    def create_chat_session(self, access_attempt=None) -> str:
        if access_attempt is None and getattr(self, "access_guard", None) is not None:
            with self.access_guard.attempt("deepseek") as attempt:
                return self.create_chat_session(attempt)
        if access_attempt is not None:
            access_attempt.dispatch()
        r = self._http.post("/api/v0/chat_session/create", json={})
        r.raise_for_status()
        return _biz(r.json())["chat_session"]["id"]

    def _pow_header(self, target_path: str = COMPLETION_PATH, access_attempt=None) -> str:
        if access_attempt is None and getattr(self, "access_guard", None) is not None:
            with self.access_guard.attempt("deepseek") as attempt:
                return self._pow_header(target_path, attempt)
        if access_attempt is not None:
            access_attempt.dispatch()
        r = self._http.post(
            "/api/v0/chat/create_pow_challenge", json={"target_path": target_path}
        )
        r.raise_for_status()
        challenge = _biz(r.json())["challenge"]
        with self._pow_lock:
            return self._pow.make_header(challenge)

    # --- public API ---------------------------------------------------------

    def stream(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        model: Optional[str] = None,
        thinking: bool = False,
        search: bool = False,
    ) -> "_Stream":
        """Stream a reply. Iterate it for text chunks; read `.conversation_id`
        afterwards to resume the thread. Pass an existing `conversation_id` to
        continue a previous conversation.

        `model` is DeepSeek's model_type wire value: "default" (Instant) or
        "expert"; it defaults to "default" on a NEW thread. It cannot be combined
        with `conversation_id` — a thread's model is fixed when it's created, so
        resuming keeps the original model. `thinking` enables DeepThink reasoning
        and `search` enables web search; both are independent of the model.
        """
        if conversation_id and model is not None:
            raise ValueError(
                "`model` cannot be set together with `conversation_id`; a thread's "
                "model is fixed when it is created. Pass `model` only on the first turn."
            )
        session_id, parent_id = _decode_cid(conversation_id)
        if session_id is None:
            # Create lazily while holding the full-request lock during iteration.
            model_type: Optional[str] = model or DEFAULT_MODEL_TYPE
        else:
            # Resuming: let the existing thread's model stand (send no model_type).
            model_type = None
        return _Stream(self, prompt, session_id, parent_id, model_type, thinking, search)

    def chat(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        model: Optional[str] = None,
        thinking: bool = False,
        search: bool = False,
    ) -> Reply:
        """Return the complete reply (`.text`) plus its `.conversation_id`."""
        s = self.stream(prompt, conversation_id=conversation_id,
                        model=model, thinking=thinking, search=search)
        text = "".join(s)
        return Reply(text=text, conversation_id=s.conversation_id,
                     finish_reason=s.finish_reason)

    def close(self) -> None:
        self._http.close()


class _Stream:
    """Iterable of reply-text chunks. After it's consumed, `.conversation_id`
    holds the token for resuming the conversation."""

    def __init__(self, client: "DeepSeekClient", prompt: str, session_id: Optional[str],
                 parent_id: Optional[int], model: Optional[str],
                 thinking: bool, search: bool):
        self._client = client
        self._prompt = prompt
        self._session_id = session_id
        self._parent_id = parent_id
        self._model = model
        self._thinking = thinking
        self._search = search
        self._message_id: Optional[int] = parent_id
        self.finish_reason = "stop"

    def __iter__(self):
        with self._client._request_lock:
            access = getattr(self._client, "access_guard", None)
            attempt = getattr(self, "access_attempt", None) or current_attempt("deepseek")
            owned = attempt is None and access is not None
            if owned:
                attempt = access.begin("deepseek")
            self.access_attempt = attempt
            try:
                yield from self._generate()
                if owned:
                    attempt.complete()
            except BaseException as exc:
                if attempt is not None:
                    attempt.abort(exc)
                raise

    def _dispatch(self):
        if self.access_attempt is not None:
            self.access_attempt.dispatch()

    def _generate(self) -> Iterator[str]:
        if self._session_id is None:
            self._session_id = self._client.create_chat_session(access_attempt=self.access_attempt)
        body = {
            "chat_session_id": self._session_id,
            "parent_message_id": self._parent_id,
            "prompt": self._prompt,
            "ref_file_ids": [],
            "thinking_enabled": self._thinking,
            "search_enabled": self._search,
            "action": None,
            "preempt": False,
        }
        # Only select a model on a new thread; on resume the thread keeps its own.
        if self._model is not None:
            body["model_type"] = self._model
        # PoW challenges are short-lived, so solve right before the request.
        headers = {"x-ds-pow-response": self._client._pow_header(access_attempt=self.access_attempt)}
        meta: dict = {}
        self._dispatch()
        with self._client._http.stream(
            "POST", COMPLETION_PATH, json=body, headers=headers
        ) as resp:
            resp.raise_for_status()
            if "text/event-stream" not in resp.headers.get("content-type", "").lower():
                resp.read()
                try:
                    _biz(resp.json())
                except ValueError as exc:
                    raise ProviderRejected() from exc
                raise ProviderRejected()
            yield from _parse_sse(resp.iter_lines(), meta)
        if meta.get("message_id") is not None:
            self._message_id = meta["message_id"]
        self.finish_reason = meta["finish_reason"]

    @property
    def conversation_id(self) -> Optional[str]:
        if self._session_id is None:
            return None
        return _encode_cid(self._session_id, self._message_id)


_CONTENT_PATH = re.compile(r"^response/fragments/(-?\d+)/content$")


class DeepSeekStreamError(RuntimeError):
    """An upstream stream failed or ended without a terminal marker."""


def _parse_sse(lines, meta: Optional[dict] = None) -> Iterator[str]:
    """Emit RESPONSE text while retaining the current patch path and operation.

    EOF succeeds only after FINISHED, INCOMPLETE, a finish event, or [DONE].
    INCOMPLETE is a deliberate output limit, exposed as finish_reason=length;
    transport truncation and error events never masquerade as a successful stop.
    """
    meta = meta if meta is not None else {}
    types: list[Optional[str]] = []
    emitted: dict[int, str] = {}
    last_emitted_index = -1
    current_path, current_operation = None, None
    terminal = False

    def status(value):
        nonlocal terminal
        if value in ("FINISHED", "INCOMPLETE"):
            terminal = True
            meta["finish_reason"] = "length" if value == "INCOMPLETE" else "stop"
        elif value in ("ERROR", "FAILED", "CANCELLED"):
            raise DeepSeekStreamError(f"DeepSeek response status: {value}")

    def claim(index, fragment):
        nonlocal last_emitted_index
        content = fragment.get("content", "")
        if not isinstance(content, str):
            raise DeepSeekStreamError("DeepSeek fragment content is not text")
        previous = emitted.get(index, "")
        if not content.startswith(previous):
            raise DeepSeekStreamError("DeepSeek rewrote an already emitted response prefix")
        if len(content) > len(previous):
            if index < last_emitted_index:
                raise DeepSeekStreamError("DeepSeek extended a response fragment after later text was emitted")
            last_emitted_index = index
            emitted[index] = content
            yield content[len(previous):]

    for event, payload in _sse_events(lines):
        if payload == "[DONE]":
            terminal = True
            meta.setdefault("finish_reason", "stop")
            break
        if not payload:
            if event == "finish":
                terminal = True
                meta.setdefault("finish_reason", "stop")
            elif event == "error":
                raise DeepSeekStreamError("DeepSeek emitted an error event")
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise DeepSeekStreamError("Invalid JSON in DeepSeek SSE event") from exc
        if not isinstance(obj, dict):
            raise DeepSeekStreamError("Unexpected DeepSeek SSE event shape")
        envelope = obj
        if isinstance(obj.get("v"), dict) and ("error" in obj["v"] or "code" in obj["v"]):
            envelope = obj["v"]
        if "data" in envelope and isinstance(envelope["data"], dict) and envelope["data"].get("biz_code", 0) != 0:
            _biz(envelope)
        if event == "error" or "error" in envelope or envelope.get("code", 0) != 0:
            detail = envelope.get("error") or envelope
            raise rejection(detail)
        if event == "finish":
            terminal = True
            meta.setdefault("finish_reason", "stop")

        value = obj.get("v")
        if isinstance(value, dict) and "response" in value:
            response = value["response"]
            if not isinstance(response, dict):
                raise DeepSeekStreamError("Unexpected DeepSeek response snapshot")
            _capture_message_id(meta, value)
            status(response.get("status"))
            fragments = response.get("fragments", [])
            if not isinstance(fragments, list):
                raise DeepSeekStreamError("Invalid DeepSeek snapshot fragments")
            fragments = _parse_fragment_list(fragments)
            types = [f.get("type") for f in fragments]
            if any(index >= len(types) or types[index] != "RESPONSE" for index in emitted):
                raise DeepSeekStreamError("DeepSeek replaced an already emitted response fragment")
            current_path, current_operation = None, None
            for index, fragment in enumerate(fragments):
                if types[index] == "RESPONSE":
                    yield from claim(index, fragment)
            continue

        if "p" in obj:
            current_path = obj["p"]
        if "o" in obj:
            current_operation = obj["o"]
        if current_path == "response/status":
            status(value)
            continue
        if current_path in ("response/message_id", "response/id"):
            _set_message_id(meta, value)
            continue
        if current_path == "response/fragments":
            if current_operation != "APPEND":
                raise DeepSeekStreamError("Unsupported DeepSeek fragment list patch")
            for fragment in _parse_fragment_list(value):
                index = len(types)
                types.append(fragment.get("type"))
                if types[index] == "RESPONSE":
                    yield from claim(index, fragment)
            continue
        match = _CONTENT_PATH.fullmatch(current_path) if isinstance(current_path, str) else None
        if not match:
            continue
        if current_operation != "APPEND" or not isinstance(value, str):
            raise DeepSeekStreamError("Unsupported DeepSeek content patch")
        index = int(match.group(1))
        if index == -1:
            index = len(types) - 1
        if 0 <= index < len(types) and types[index] == "RESPONSE":
            yield from claim(index, {"content": emitted.get(index, "") + value})
        elif not 0 <= index < len(types):
            raise DeepSeekStreamError("DeepSeek content patch references an unknown fragment")

    if not terminal:
        raise DeepSeekStreamError("DeepSeek stream ended before a terminal marker")


def _parse_fragment_list(v) -> list:
    """Read the fragment descriptors out of a `response/fragments` APPEND frame.

    Accept a JSON array or a bare descriptor; reject damaged payloads atomically.
    """
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError as exc:
            raise DeepSeekStreamError("Invalid DeepSeek fragment JSON") from exc
    if isinstance(v, dict):
        v = [v]
    if not isinstance(v, list) or any(not isinstance(f, dict) for f in v):
        raise DeepSeekStreamError("Invalid DeepSeek fragment descriptors")
    for fragment in v:
        if not isinstance(fragment.get("type"), str) or not fragment["type"]:
            raise DeepSeekStreamError("Invalid DeepSeek fragment type")
        if "content" in fragment and not isinstance(fragment["content"], str):
            raise DeepSeekStreamError("DeepSeek fragment content is not text")
    return v


def _set_message_id(meta, value):
    if not isinstance(value, int) or isinstance(value, bool):
        raise DeepSeekStreamError("Invalid DeepSeek response message_id")
    if "message_id" in meta and meta["message_id"] != value:
        raise DeepSeekStreamError("DeepSeek changed response message_id within one stream")
    meta["message_id"] = value


def _capture_message_id(meta: dict, snapshot: dict) -> None:
    """Pull the assistant message_id out of a snapshot without changing ownership.

    DeepSeek nests the assistant message under `response`; we check there first,
    then the snapshot root, accepting `message_id` or `id`.
    """
    for container in (snapshot.get("response"), snapshot):
        if isinstance(container, dict):
            mid = container.get("message_id", container.get("id"))
            if mid is not None:
                _set_message_id(meta, mid)
                return
