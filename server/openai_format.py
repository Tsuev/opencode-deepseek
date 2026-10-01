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

# Only the complete, explicitly requested protocol may produce actions.
_TOOL_BLOCK_RE = re.compile(r"\A```tool_calls[ \t]*\r?\n(.*?)\r?\n```[ \t]*\Z", re.DOTALL)
_TOOL_START_RE = re.compile(r"\A```tool_calls[ \t]*\r?\n")
_CLOSING_TOOL_FENCE_RE = re.compile(r"(?m)^```[ \t]*\r?$")


class ToolCallError(RuntimeError):
    """The model emitted an invalid or disallowed explicit tool reply."""


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
    example = specs[0]["name"]
    instructions = (
        "You can call functions (tools). Available tools, given as JSON Schema:\n\n"
        f"{rendered}\n\n"
        "To call one or more tools, your ENTIRE reply must be a single fenced code "
        "block in exactly this format and nothing else:\n\n"
        "```tool_calls\n"
        '[{"name": "<tool_name>", "arguments": {<arguments>}}]\n'
        "```\n\n"
        "Hard rules:\n"
        "- The fenced `tool_calls` block must be the WHOLE reply. Never write any "
        "text, plan, explanation, or reasoning before or after it.\n"
        "- Never describe the action you are about to take — perform it by emitting "
        "the block.\n"
        "- Use the exact tool name and an `arguments` object matching that tool's "
        "schema. To call several tools, put multiple objects in the array.\n"
        "- Never use XML/DSML tags such as <tool_call> or <|tool_calls|>.\n"
        "- Only if no tool is needed, answer normally in plain text and do NOT emit "
        "a tool_calls block.\n\n"
        f"Example — to use the `{example}` tool, reply with exactly:\n\n"
        "```tool_calls\n"
        f'[{{"name": "{example}", "arguments": {{}}}}]\n'
        "```"
    )
    if _forced_tool_name(tool_choice) is not None:
        instructions += f"\n\nYou MUST call the tool `{_forced_tool_name(tool_choice)}` now."
        instructions += " Reply with ONLY the fenced tool_calls block."
    elif tool_choice == "required":
        instructions += "\n\nYou MUST call one of the tools now. Reply with ONLY the fenced tool_calls block."
    elif tool_choice == "none":
        instructions += "\n\nDo NOT call any tools; answer in plain text."
    # Keep the protocol reminder next to the model's turn.
    if tool_choice != "none":
        instructions += (
            "\n\nREMINDER: if you are about to perform any action, respond with the "
            "fenced ```tool_calls block now, as your entire reply — no other text."
        )
    return instructions


def _render_message(m: ChatMessage, call_names: dict) -> str:
    content = _text_of(m.content)
    if m.role == "assistant" and m.tool_calls:
        calls = []
        for tc in m.tool_calls:
            fn = (tc or {}).get("function") or {}
            name = fn.get("name", "")
            args = fn.get("arguments")
            args_s = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
            calls.append(f"{name}({args_s}) [call_id={tc.get('id', 'unknown')}]")
        call_txt = "[called tools: " + ", ".join(calls) + "]"
        content = f"{content} {call_txt}".strip() if content else call_txt
        return f"Assistant: {content}"
    if m.role == "tool":
        label = m.name or call_names.get(m.tool_call_id, "tool")
        return f"Tool ({label}, call_id={m.tool_call_id or 'unknown'}): {content}"
    label = _ROLE_LABELS.get(m.role, m.role.capitalize())
    return f"{label}: {content}"


def messages_to_prompt(
    messages: List[ChatMessage], tools: List[dict] = None, tool_choice=None
) -> str:
    """Flatten a chat history into a single prompt DeepSeek can answer.

    A lone user message (no system prompt, no tools) is sent verbatim. Otherwise
    the history is serialised with role labels and a trailing 'Assistant:' cue.
    When `tools` are given, their spec and the required reply format are injected
    as the LAST block, immediately before the 'Assistant:' cue: DeepSeek's web
    chat follows the instruction closest to its own turn far more reliably.
    """
    system_parts: List[str] = []
    convo: List[ChatMessage] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(_text_of(m.content))
        else:
            convo.append(m)

    tool_instr = _tool_instructions(tools, tool_choice) if tools else ""

    if not system_parts and not tool_instr and len(convo) == 1 and convo[0].role == "user":
        return _text_of(convo[0].content)

    lines = []
    preamble = "\n\n".join(p for p in system_parts if p)
    if preamble:
        lines.append(f"System: {preamble}")
    call_names = {}
    for m in convo:
        for call in m.tool_calls or []:
            fn = call.get("function") or {}
            if isinstance(fn, dict) and call.get("id"):
                call_names[call["id"]] = fn.get("name", "tool")
    lines.extend(_render_message(m, call_names) for m in convo)
    if tool_instr:
        lines.append(tool_instr)
    lines.append("Assistant:")
    return "\n\n".join(lines)


def validate_tool_choice(tool_choice, allowed_names: Iterable[str]) -> None:
    """Reject invalid request policies before making an upstream request."""
    names = set(allowed_names)
    if tool_choice is None or tool_choice in ("auto", "none"):
        return
    if tool_choice == "required":
        if not names:
            raise ValueError("tool_choice='required' needs at least one tool")
        return
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        name = _forced_tool_name(tool_choice)
        if isinstance(name, str) and name in names:
            return
    raise ValueError("tool_choice must be auto, none, required, or an advertised function")


def enforce_tool_choice(tool_choice, tool_calls: Optional[List[dict]]) -> None:
    """Treat the caller's tool policy as a contract, not a model suggestion."""
    if tool_choice == "none" and tool_calls:
        raise ToolCallError("The model called a tool while tool_choice='none'")
    forced = _forced_tool_name(tool_choice)
    if (tool_choice == "required" or forced) and not tool_calls:
        raise ToolCallError("The model did not emit the required tool call")
    if forced and any(c["function"]["name"] != forced for c in tool_calls or []):
        raise ToolCallError(f"The model called a tool other than the required '{forced}'")


def parse_tool_calls(
    text: str, allowed_names: Optional[Iterable[str]] = None
) -> Tuple[str, Optional[List[dict]]]:
    """Accept one whole fenced tool_calls array, never examples embedded in prose.

    All calls and their arguments must validate before any call is returned.
    Ordinary text, JSON examples, XML/DSML and pseudo-calls remain visible text.
    Malformed explicit protocol blocks fail closed instead of executing a prefix.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("```tool_calls"):
        return text, None
    match = _TOOL_BLOCK_RE.fullmatch(stripped)
    if not match:
        raise ToolCallError("Expected one complete fenced tool_calls block")
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ToolCallError("The tool_calls block contains invalid JSON") from exc
    if not isinstance(data, list) or not data:
        raise ToolCallError("tool_calls must be a non-empty JSON array")
    names = set(allowed_names) if allowed_names is not None else None
    calls = []
    for item in data:
        if not isinstance(item, dict):
            raise ToolCallError("Each tool call must be an object")
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        name = fn.get("name")
        if not isinstance(name, str) or not name or (names is not None and name not in names):
            raise ToolCallError("The model called an unadvertised tool")
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ToolCallError("Tool arguments contain invalid JSON") from exc
        if not isinstance(args, dict):
            raise ToolCallError("Tool arguments must be a JSON object")
        try:
            arguments = json.dumps(args, ensure_ascii=False, allow_nan=False)
        except ValueError as exc:
            raise ToolCallError("Tool arguments contain non-finite numbers") from exc
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    return "", calls


def looks_truncated(text: str, allowed_names: Optional[Iterable[str]] = None) -> bool:
    """Continue only an explicitly started tool block missing its closing fence."""
    stripped = (text or "").strip()
    return bool(_TOOL_START_RE.match(stripped)) and not _CLOSING_TOOL_FENCE_RE.search(stripped)


# --- OpenAI response shapes -----------------------------------------------


def _now() -> int:
    return int(time.time())


def _id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def _est_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) — DeepSeek's web API gives us no count."""
    return max(1, len(text) // 4)


def completion_response(model: str, content: str, prompt: str,
                        conversation_id: str = None, tool_calls: List[dict] = None,
                        finish_reason: str = "stop") -> dict:
    """A full (non-streaming) OpenAI chat.completion object.

    `conversation_id` is an extra top-level field (outside OpenAI's schema) you
    send back to resume the conversation.
    """
    pt, ct = _est_tokens(prompt), _est_tokens(content or "")
    message: dict = {"role": "assistant", "content": content or ""}
    finish = finish_reason
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
               conversation_id: str = None, cid: str = None, created: int = None,
               finish_reason: str = "stop") -> Iterable[str]:
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
        yield frame({}, finish=finish_reason, extra={"conversation_id": conversation_id})
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
        yield from sse_frames(model, content, calls, conversation_id, cid=cid, created=created,
                              finish_reason=getattr(stream, "finish_reason", "stop"))
        return

    # First frame announces the assistant role.
    yield frame({"role": "assistant", "content": ""})
    for d in it:
        if d:
            yield frame({"content": d})
    conversation_id = getattr(stream, "conversation_id", None)
    yield frame({}, finish=getattr(stream, "finish_reason", "stop"),
                extra={"conversation_id": conversation_id})
    yield "data: [DONE]\n\n"
