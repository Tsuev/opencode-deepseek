"""Reuse DeepSeek conversations across stateless OpenAI requests.

OpenAI clients (including opencode) resend the whole chat history on every
turn, but they cannot send our custom top-level ``conversation_id``. Without
help, the bridge would create a brand-new DeepSeek chat for every agent step —
one of the clearest automation signals an account can emit.

This store maps an exact request history to the DeepSeek conversation that
answered it. On the next turn the client sends that history plus the assistant
reply and new tool results, so the stored history is a prefix of the new
messages. We find the longest matching prefix, resume that conversation, and
send only the *new* tail instead of re-pasting the entire transcript.

Only hashes of request histories are stored, never prompt text.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

# Upper bound on stored histories. Old entries are evicted first.
DEFAULT_MAX_ENTRIES = 500


def _normalise_message(message) -> dict:
    """Canonicalise a message so equivalent requests hash the same.

    Pydantic messages become plain dicts; tool-call argument strings are parsed
    into objects so a JSON string and an object hash identically.
    """
    if hasattr(message, "model_dump"):
        data = message.model_dump(exclude_none=True)
    else:
        data = dict(message)
    for call in data.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
            try:
                fn["arguments"] = json.loads(fn["arguments"])
            except ValueError:
                pass
    return data


class ConversationStore:
    """Persisted prefix match: request history -> DeepSeek conversation id."""

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = True,
        ttl: float = 24 * 60 * 60,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ):
        self.path = path
        self.enabled = enabled
        self.ttl = max(0.0, ttl)
        self.max_entries = max(1, max_entries)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            entries = data.get("entries") if isinstance(data, dict) else None
            return entries if isinstance(entries, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, entries: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps({"entries": entries}, separators=(",", ":")),
                       encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    @staticmethod
    def _key(req, messages) -> str:
        payload = {
            "model": req.model,
            "thinking": bool(req.thinking),
            "search": bool(req.search),
            "tools": req.tools,
            "messages": [
                _normalise_message(m) for m in messages if m.role != "system"
            ],
        }
        blob = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def match(self, req) -> Optional[Tuple[str, List]]:
        """Return ``(conversation_id, tail_messages)`` for a known history.

        ``tail_messages`` is what the client added since the matched turn; the
        caller sends only that to DeepSeek. Returns ``None`` for a new chat.
        """
        if not self.enabled or req.conversation_id:
            return None

        # System prompts are re-rendered by the client each turn and are not
        # part of DeepSeek's conversation state, so ignore them when matching.
        messages = [m for m in req.messages if m.role != "system"]
        assistant_positions = [
            i for i, m in enumerate(messages) if m.role == "assistant"
        ]
        if not assistant_positions:
            return None

        now = time.time()
        with self._lock:
            entries = self._load()

        for pos in reversed(assistant_positions):
            prefix = messages[:pos]
            if not prefix:
                continue
            entry = entries.get(self._key(req, prefix))
            if not isinstance(entry, dict):
                continue
            cid = entry.get("cid")
            stored_at = entry.get("ts")
            if not isinstance(cid, str) or not cid:
                continue
            if not isinstance(stored_at, (int, float)) or now - stored_at > self.ttl:
                continue
            tail = messages[pos + 1:]
            if not tail or tail[0].role not in ("tool", "user"):
                continue
            return cid, tail
        return None

    def remember(self, req, conversation_id: Optional[str]) -> None:
        """Store ``request history -> conversation id`` after a successful turn."""
        if not self.enabled or not conversation_id:
            return
        now = time.time()
        with self._lock:
            entries = {
                key: entry for key, entry in self._load().items()
                if isinstance(entry, dict)
                and isinstance(entry.get("ts"), (int, float))
                and now - entry["ts"] <= self.ttl
            }
            entries[self._key(req, req.messages)] = {
                "cid": conversation_id,
                "ts": now,
            }
            if len(entries) > self.max_entries:
                newest = sorted(
                    entries.items(), key=lambda item: item[1]["ts"], reverse=True
                )[: self.max_entries]
                entries = dict(newest)
            self._save(entries)
