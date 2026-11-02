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
import os
import re
import threading
from dataclasses import dataclass
from typing import Iterator, Optional

import httpx

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


@dataclass
class Reply:
    """A completed chat reply plus the id to resume the conversation."""

    text: str
    conversation_id: str

    def __str__(self) -> str:  # so print(reply) shows the text
        return self.text


def _biz(data: dict) -> dict:
    """Unwrap DeepSeek's `data.biz_data` envelope, raising on API-level errors."""
    if data.get("code") != 0:
        raise RuntimeError(f"DeepSeek API error: {data.get('msg') or data}")
    biz = data.get("data", {}).get("biz_data")
    if biz is None:
        raise RuntimeError(f"Unexpected response shape: {data}")
    return biz


class DeepSeekClient:
    def __init__(
        self,
        session: Optional[Session] = None,
        allow_interactive: bool = True,
    ):
        # `allow_interactive=False` makes session resolution non-blocking: it
        # uses a cached/headless session and raises LoginRequired instead of
        # opening a browser window. The server passes False (see server/api.py).
        self.session = session or get_session(allow_interactive=allow_interactive)
        self._pow = DeepSeekPow()
        # The wasmtime Store behind the PoW solver is not reentrant; serialise
        # access so concurrent server requests don't corrupt it.
        self._pow_lock = threading.Lock()
        self._http = httpx.Client(
            base_url=BASE,
            headers=self._base_headers(),
            cookies=self.session.cookies,
            timeout=httpx.Timeout(120.0, read=300.0),
        )

    def _base_headers(self) -> dict:
        app_version = os.getenv("DEEPSEEK_APP_VERSION", "2.0.0")
        headers = {
            "authorization": f"Bearer {self.session.token}",
            "accept": "*/*",
            "content-type": "application/json",
            "user-agent": self.session.user_agent,
            "origin": BASE,
            "referer": f"{BASE}/",
            "x-app-version": app_version,
            "x-client-version": app_version,
            "x-client-platform": "web",
            "x-client-bundle-id": "com.deepseek.chat",
        }
        # Only send client hints we actually captured from the signed-in browser.
        # Sending a hardcoded locale/timezone makes every bridge user share the
        # same (and usually wrong) fingerprint.
        if self.session.locale:
            headers["x-client-locale"] = self.session.locale
            headers["accept-language"] = self.session.locale.replace("_", "-")
        if self.session.timezone_offset is not None:
            headers["x-client-timezone-offset"] = str(
                int(self.session.timezone_offset)
            )
        return headers

    # --- protocol steps -----------------------------------------------------

    def create_chat_session(self) -> str:
        r = self._http.post("/api/v0/chat_session/create", json={})
        r.raise_for_status()
        return _biz(r.json())["chat_session"]["id"]

    def _pow_header(self, target_path: str = COMPLETION_PATH) -> str:
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
            # New thread: select the model (default when unspecified).
            session_id = self.create_chat_session()
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
        return Reply(text=text, conversation_id=s.conversation_id)

    def close(self) -> None:
        self._http.close()


class _Stream:
    """Iterable of reply-text chunks. After it's consumed, `.conversation_id`
    holds the token for resuming the conversation."""

    def __init__(self, client: "DeepSeekClient", prompt: str, session_id: str,
                 parent_id: Optional[int], model: str,
                 thinking: bool, search: bool):
        self._client = client
        self._prompt = prompt
        self._session_id = session_id
        self._parent_id = parent_id
        self._model = model
        self._thinking = thinking
        self._search = search
        self._message_id: Optional[int] = None

    def __iter__(self) -> Iterator[str]:
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
        headers = {"x-ds-pow-response": self._client._pow_header()}
        meta: dict = {}
        with self._client._http.stream(
            "POST", COMPLETION_PATH, json=body, headers=headers
        ) as resp:
            resp.raise_for_status()
            yield from _parse_sse(resp.iter_lines(), meta)
        if meta.get("message_id") is not None:
            self._message_id = meta["message_id"]

    @property
    def conversation_id(self) -> str:
        return _encode_cid(self._session_id, self._message_id)


_CONTENT_PATH = re.compile(r"^response/fragments/(-?\d+)/content$")


def _parse_sse(lines, meta: Optional[dict] = None) -> Iterator[str]:
    """Turn DeepSeek's SSE completion stream into reply-text deltas.

    The stream sends an initial snapshot frame whose `v` is the full response
    object (with `fragments[]`), then a series of append frames:
      * {"p":"response/fragments/-1/content","o":"APPEND","v":" what"}  (sets path)
      * {"v":"'s"}                                                       (appends to it)
    We track the fragment list and emit only RESPONSE-fragment text.

    Two details matter. Fragments arrive out of band: the snapshot carries
    whatever exists so far (with DeepThink on, that is the THINK fragment), and
    later `{"p":"response/fragments","o":"APPEND","v":[{...RESPONSE...}]}` adds
    the answer. And the paths address the NEWEST fragment as `-1` — which is the
    THINK fragment for as long as reasoning streams. So we resolve `-1` against
    the fragments we have actually seen and emit text only for RESPONSE ones,
    which keeps DeepThink reasoning out of the reply.

    If `meta` is given, the assistant's `message_id` is recorded into it (used to
    build the resumable conversation_id). The exact field location can vary, so
    we look in a few plausible spots defensively.
    """
    types: list[Optional[str]] = []   # fragment type per index, in arrival order
    active: Optional[int] = None      # index of the fragment being appended to
    emitted: dict[int, int] = {}      # fragment index -> chars already emitted

    def resolve(idx: int) -> int:
        """Map DeepSeek's -1 ("newest fragment") onto a concrete index."""
        return len(types) - 1 if idx == -1 else idx

    def is_response(idx: Optional[int]) -> bool:
        return idx is not None and 0 <= idx < len(types) and types[idx] == "RESPONSE"

    def claim(idx: int, frag: dict) -> Iterator[str]:
        """Yield whatever a fragment object adds beyond what we already emitted.

        Snapshots and `response/fragments` frames carry whole fragment contents
        that partly repeat what the content appends already streamed.
        """
        content = frag.get("content") or ""
        seen = emitted.get(idx, 0)
        if len(content) > seen:
            emitted[idx] = len(content)
            return iter(content[seen:])
        return iter(())

    for line in lines:
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue

        v = obj.get("v")

        # Snapshot frame: the full response object, replacing what we knew.
        if isinstance(v, dict) and "response" in v:
            if meta is not None:
                _capture_message_id(meta, v)
            frags = [f for f in v["response"].get("fragments", [])
                     if isinstance(f, dict)]
            types = [f.get("type") for f in frags]
            for idx, frag in enumerate(frags):
                if frag.get("type") != "RESPONSE":
                    continue
                active = idx
                yield from claim(idx, frag)
            continue

        p = obj.get("p")

        # A new fragment object was appended to the list — note its type so we
        # can tell the answer apart from DeepSeek reasoning. The answer often
        # arrives whole inside this frame, with no content appends after it.
        if p == "response/fragments" and obj.get("o") == "APPEND":
            for frag in _parse_fragment_list(v):
                idx = len(types)
                types.append(frag.get("type"))
                if frag.get("type") != "RESPONSE":
                    continue
                active = idx
                yield from claim(idx, frag)
            continue

        # Path-setting append frame.
        if "p" in obj:
            m = _CONTENT_PATH.match(p) if isinstance(p, str) else None
            if m:
                active = resolve(int(m.group(1)))
            if meta is not None and isinstance(p, str) and p.endswith("message_id") \
                    and isinstance(v, int):
                meta["message_id"] = v
            if obj.get("o") == "APPEND" and isinstance(v, str) and m \
                    and is_response(active):
                emitted[active] = emitted.get(active, 0) + len(v)
                yield v
            continue

        # Bare append to the current path.
        if isinstance(v, str) and is_response(active):
            emitted[active] = emitted.get(active, 0) + len(v)
            yield v


def _parse_fragment_list(v) -> list:
    """Read the fragment descriptors out of a `response/fragments` APPEND frame.

    The payload is a JSON array of one fragment object, but tolerate a bare
    object (or something unreadable) rather than dropping the stream.
    """
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            return []
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list):
        return [f for f in v if isinstance(f, dict)]
    return []


def _capture_message_id(meta: dict, snapshot: dict) -> None:
    """Best-effort: pull the assistant message_id out of a snapshot frame.

    DeepSeek nests the assistant message under `response`; we check there first,
    then the snapshot root, accepting `message_id` or `id`.
    """
    for container in (snapshot.get("response"), snapshot):
        if isinstance(container, dict):
            mid = container.get("message_id", container.get("id"))
            if isinstance(mid, int):
                meta["message_id"] = mid
                return
