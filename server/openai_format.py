"""Translate between OpenAI's chat-completions shapes and our DeepSeek client.

DeepSeek's protocol has no system/role channel — just a single `prompt` string.
So we flatten the OpenAI `messages` array into one prompt, and wrap DeepSeek's
text output back into OpenAI response/stream objects.

Tool calling is EMULATED: DeepSeek's web chat has no function-calling channel,
so we inject the tool specs into the prompt, ask the model to answer with a
fenced `tool_calls` JSON block when it wants a tool, then parse that block back
into OpenAI `tool_calls`.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Iterable, List, Optional, Tuple

from .schemas import ChatMessage

_ROLE_LABELS = {"system": "System", "user": "User", "assistant": "Assistant"}

# Fenced blocks the model may use: ```tool_calls, ```tool_call, ```json or a bare fence.
_FENCE_RE = re.compile(r"```[a-zA-Z_]*\s*\n?(.*?)```", re.DOTALL)


def _text_of(content) -> str:
    """Extract plain text from a message's content (string or list-of-parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "text":
            parts.append(p.get("text", ""))
    return "\n".join(parts)


# --- tool emulation: prompt injection ------------------------------------


def _tool_specs(tools: List[dict]) -> List[dict]:
    """Normalise OpenAI tool objects into compact JSON-friendly specs."""
    specs = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function", t)
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        specs.append({
            "name": name,
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return specs


def _forced_tool_name(tool_choice) -> Optional[str]:
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        return fn.get("name") if isinstance(fn, dict) else None
    return None


def _tool_instructions(tools: List[dict], tool_choice=None) -> str:
    specs = _tool_specs(tools)
    if not specs:
        return ""
    rendered = "\n".join(json.dumps(s, ensure_ascii=False) for s in specs)
    instructions = (
        "You can call functions (tools). Available tools, given as JSON Schema:\n\n"
        f"{rendered}\n\n"
        "When you need to call one or more tools, reply with ONLY a single fenced "
        "code block in exactly this format and nothing else:\n\n"
        "```tool_calls\n"
        '[{"name": "<tool_name>", "arguments": {<arguments>}}]\n'
        "```\n\n"
        "Use the exact tool name and an `arguments` object matching that tool's "
        "schema. To call several tools, put multiple objects in the array. "
        "If no tool is needed, answer the user normally in plain text and do NOT "
        "emit a tool_calls block."
    )
    if _forced_tool_name(tool_choice) is not None:
        instructions += f"\n\nYou MUST call the tool `{_forced_tool_name(tool_choice)}` now."
    elif tool_choice == "required":
        instructions += "\n\nYou MUST call one of the tools now."
    elif tool_choice == "none":
        instructions += "\n\nDo NOT call any tools; answer in plain text."
    return instructions


def _render_message(m: ChatMessage) -> str:
    content = _text_of(m.content)
    if m.role == "assistant" and m.tool_calls:
        calls = []
        for tc in m.tool_calls:
            fn = (tc or {}).get("function") or {}
            name = fn.get("name", "")
            args = fn.get("arguments")
            args_s = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
            calls.append(f"{name}({args_s})")
        call_txt = "[called tools: " + ", ".join(calls) + "]"
        content = f"{content} {call_txt}".strip() if content else call_txt
        return f"Assistant: {content}"
    if m.role == "tool":
        label = m.name or "tool"
        return f"Tool ({label}): {content}"
    label = _ROLE_LABELS.get(m.role, m.role.capitalize())
    return f"{label}: {content}"


def messages_to_prompt(
    messages: List[ChatMessage], tools: List[dict] = None, tool_choice=None
) -> str:
    """Flatten a chat history into a single prompt DeepSeek can answer.

    A lone user message (no system prompt, no tools) is sent verbatim. Otherwise
    the history is serialised with role labels and a trailing 'Assistant:' cue.
    When `tools` are given, their spec and the required reply format are injected
    as a leading System block.
    """
    system_parts: List[str] = []
    convo: List[ChatMessage] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(_text_of(m.content))
        else:
            convo.append(m)

    if tools:
        instr = _tool_instructions(tools, tool_choice)
        if instr:
            system_parts.append(instr)

    if not system_parts and len(convo) == 1 and convo[0].role == "user":
        return _text_of(convo[0].content)

    lines = []
    preamble = "\n\n".join(p for p in system_parts if p)
    if preamble:
        lines.append(f"System: {preamble}")
    lines.extend(_render_message(m) for m in convo)
    lines.append("Assistant:")
    return "\n\n".join(lines)


def parse_tool_calls(text: str) -> Tuple[str, Optional[List[dict]]]:
    """Split a reply into (visible_content, tool_calls).

    Looks for the last fenced block containing a tool call; falls back to the
    whole reply being raw JSON. `tool_calls` is None when the reply is plain text.
    """
    if not text:
        return text, None

    blocks = _FENCE_RE.findall(text)
    for block in reversed(blocks):
        calls = _coerce_calls(block)
        if calls:
            return _FENCE_RE.sub("", text).strip(), calls

    calls = _coerce_calls(text.strip())
    if calls:
        return "", calls

    return text, None


def _coerce_calls(raw: str) -> Optional[List[dict]]:
    """Turn a JSON blob into OpenAI tool_call objects, or None if it isn't one."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None

    if isinstance(data, dict):
        if isinstance(data.get("tool_calls"), list):
            data = data["tool_calls"]
        elif data.get("name"):
            data = [data]
        else:
            return None
    if not isinstance(data, list):
        return None

    calls: List[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = item.get("name") or fn.get("name")
        if not name:
            continue
        args = item.get("arguments")
        if args is None:
            args = fn.get("arguments")
        if args is None:
            args = item.get("parameters")
        args_s = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {"name": name, "arguments": args_s},
        })
    return calls or None


# --- OpenAI response shapes -----------------------------------------------


def _now() -> int:
    return int(time.time())


def _id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def _est_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) — DeepSeek's web API gives us no count."""
    return max(1, len(text) // 4)


def completion_response(model: str, content: str, prompt: str,
                        conversation_id: str = None, tool_calls: List[dict] = None) -> dict:
    """A full (non-streaming) OpenAI chat.completion object.

    `conversation_id` is an extra top-level field (outside OpenAI's schema) you
    send back to resume the conversation.
    """
    pt, ct = _est_tokens(prompt), _est_tokens(content or "")
    message: dict = {"role": "assistant", "content": content or ""}
    finish = "stop"
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = content or None
        finish = "tool_calls"
    return {
        "id": _id(),
        "object": "chat.completion",
        "created": _now(),
        "model": model,
        "conversation_id": conversation_id,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish,
            }
        ],
        "usage": {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
        },
    }


def sse_frames(model: str, content: str, tool_calls: List[dict] = None,
               conversation_id: str = None, cid: str = None, created: int = None) -> Iterable[str]:
    """Yield OpenAI SSE frames for an already-computed reply.

    Used when the caller has a full result (e.g. it ran a non-streaming request
    so it could retry on an auth error) but the client asked for a stream.
    """
    cid = cid or _id()
    created = created if created is not None else _now()

    def frame(delta: dict, finish=None, extra: dict = None) -> str:
        obj = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if extra:
            obj.update(extra)
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    yield frame({"role": "assistant", "content": None})
    if tool_calls:
        for i, call in enumerate(tool_calls):
            yield frame({"tool_calls": [{
                "index": i,
                "id": call["id"],
                "type": "function",
                "function": call["function"],
            }]})
        yield frame({}, finish="tool_calls", extra={"conversation_id": conversation_id})
    else:
        if content:
            yield frame({"content": content})
        yield frame({}, finish="stop", extra={"conversation_id": conversation_id})
    yield "data: [DONE]\n\n"


def stream_chunks(model: str, stream: Iterable[str], tool_supported: bool = False,
                  iterator: Iterable[str] = None) -> Iterable[str]:
    """Yield OpenAI SSE lines (`data: {...}\\n\\n`) for a streamed completion.

    `stream` is the client's stream object; after it's consumed we read its
    `.conversation_id` and attach it to the final chunk. When the caller has
    already started consuming it, it passes the remaining `iterator` (e.g. a
    chain of the first chunk plus the rest) — `stream` is still used only to
    read `.conversation_id` afterwards.

    When `tool_supported` is set we must buffer the whole reply before deciding
    whether it is plain text or a tool call, so the raw call block never leaks
    to the client as visible content.
    """
    cid, created = _id(), _now()
    it = iterator if iterator is not None else stream

    def frame(delta: dict, finish=None, extra: dict = None) -> str:
        obj = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if extra:
            obj.update(extra)
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    if tool_supported:
        text = "".join(d for d in it if d)
        conversation_id = getattr(stream, "conversation_id", None)
        content, calls = parse_tool_calls(text)
        yield from sse_frames(model, content, calls, conversation_id, cid=cid, created=created)
        return

    # First frame announces the assistant role.
    yield frame({"role": "assistant", "content": ""})
    for d in it:
        if d:
            yield frame({"content": d})
    conversation_id = getattr(stream, "conversation_id", None)
    yield frame({}, finish="stop", extra={"conversation_id": conversation_id})
    yield "data: [DONE]\n\n"
