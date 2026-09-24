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

# DeepSeek sometimes leaks its native tool markup instead of the fenced JSON we
# ask for. Cover both the XML-ish form and the DSML token form.
_XML_BLOCK_RE = re.compile(
    r"<(tool_calls?|function_calls?)\b[^>]*>(.*?)</\1>",
    re.DOTALL | re.IGNORECASE,
)
_FUNC_TAG_RE = re.compile(
    r"<function\b[^>]*\bname\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</function>",
    re.DOTALL | re.IGNORECASE,
)
_PARAM_TAG_RE = re.compile(
    r"<parameter\b[^>]*\bname\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</parameter>",
    re.DOTALL | re.IGNORECASE,
)
_DSML_SEP_RE = re.compile(
    r"function\s*<\|tool[_\u2581]?sep\|>\s*([A-Za-z_][\w\-.]*)",
    re.IGNORECASE,
)
_DSML_FENCE_RE = re.compile(r"<\|[^|]*\|>")


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
    # Recency reminder: the instruction block sits at the TOP of the prompt, so a
    # trailing nudge (closest to the model's turn) markedly improves compliance.
    if tool_choice != "none":
        instructions += (
            "\n\nREMINDER: if you are about to perform any action, respond with the "
            "fenced ```tool_calls block now, as your entire reply — no other text."
        )
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
    lines.extend(_render_message(m) for m in convo)
    if tool_instr:
        lines.append(tool_instr)
    lines.append("Assistant:")
    return "\n\n".join(lines)


def parse_tool_calls(
    text: str, allowed_names: Optional[Iterable[str]] = None
) -> Tuple[str, Optional[List[dict]]]:
    """Split a reply into (visible_content, tool_calls).

    DeepSeek's web chat has no function-calling channel, so the model is told to
    emit a fenced ``tool_calls`` JSON block. In practice it wraps that block in
    prose, drops the fence, or leaks its native XML/DSML markup — so we accept
    all of these and only fall back to plain text when nothing parses.

    `allowed_names` (the tool names from the request) filters out stray JSON that
    merely looks like a call. `tool_calls` is None when the reply is plain text.
    """
    if not text:
        return text, None

    names = {n for n in (allowed_names or ()) if n}

    # 1. Fenced blocks — the last one usually holds the call (reasoning first).
    blocks = _FENCE_RE.findall(text)
    for block in reversed(blocks):
        calls = _coerce_calls(block, names)
        if calls:
            return _FENCE_RE.sub("", text).strip(), calls

    # 2. Native XML-ish tool markup (<tool_call>/<function_call>...</...>).
    for _, inner in _XML_BLOCK_RE.findall(text):
        calls = _parse_xml_calls(inner, names)
        if calls:
            return _XML_BLOCK_RE.sub("", text).strip(), calls

    # 3. DeepSeek DSML token form: `function<|tool_sep|>NAME` + JSON body.
    dsml = _parse_dsml(text, names)
    if dsml:
        calls, start, end = dsml
        cleaned = _DSML_FENCE_RE.sub("", text[:start] + text[end:]).strip()
        return cleaned, calls

    # 4. Any balanced JSON block embedded in prose.
    for block in _iter_json_blocks(text):
        calls = _coerce_calls(block, names)
        if calls:
            return text.replace(block, "").strip(), calls

    # 5. Pseudo-call syntax the model prints instead of JSON: e.g.
    #    `bash(command="ls")`, `write(filePath="/x", content="hi")`.
    pseudo = _parse_pseudo_calls(text, names)
    if pseudo:
        calls, spans = pseudo
        cleaned = text
        for start, end in sorted(spans, reverse=True):
            cleaned = cleaned[:start] + cleaned[end:]
        return cleaned.strip(), calls

    # 6. The whole reply is just the JSON.
    calls = _coerce_calls(text.strip(), names)
    if calls:
        return "", calls

    return text, None


def _coerce_calls(raw: str, allowed_names: Optional[Iterable[str]] = None) -> Optional[List[dict]]:
    """Turn a JSON blob (possibly with surrounding prose) into tool_call objects."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for candidate in _json_candidates(raw):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        calls = _calls_from_data(data, allowed_names)
        if calls:
            return calls
    return None


def _calls_from_data(data, allowed_names=None) -> Optional[List[dict]]:
    """Normalise a decoded JSON value into OpenAI tool_call objects."""
    if isinstance(data, dict):
        for key in ("tool_calls", "tool_call", "function_call", "calls"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        return None

    calls: List[dict] = []
    for item in data:
        call = _call_from_item(item, allowed_names)
        if call:
            calls.append(call)
    return calls or None


def _call_from_item(item, allowed_names=None) -> Optional[dict]:
    """Build one tool_call from a permissive set of field aliases, or None."""
    if not isinstance(item, dict):
        return None
    fn = item.get("function") if isinstance(item.get("function"), dict) else {}
    name = (
        item.get("name")
        or item.get("tool")
        or item.get("tool_name")
        or item.get("recipient_name")
        or fn.get("name")
    )
    if not name or not isinstance(name, str):
        return None
    if allowed_names and name not in allowed_names:
        return None

    args = item.get("arguments")
    if args is None:
        args = fn.get("arguments")
    if args is None:
        args = item.get("parameters")
    if args is None:
        args = item.get("args")
    if args is None:
        args = item.get("input")
    args_s = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {"name": name, "arguments": args_s},
    }


def _parse_xml_calls(inner: str, allowed_names=None) -> Optional[List[dict]]:
    """Parse a <function name="...">...</function> block (or JSON inside it)."""
    calls = []
    for name, body in _FUNC_TAG_RE.findall(inner or ""):
        params = {k: v.strip() for k, v in _PARAM_TAG_RE.findall(body)}
        if params:
            calls.append({"name": name, "arguments": params})
        else:
            block = _first_json(body)
            calls.append({"name": name, "arguments": block if block is not None else {}})
    if calls:
        return _calls_from_data(calls, allowed_names)
    return _coerce_calls(inner, allowed_names)


def _parse_dsml(text: str, allowed_names=None):
    """Parse DeepSeek's `function<|tool_sep|>NAME` token form + JSON body.

    Returns `(tool_calls, span_start, span_end)` so the caller can strip the
    leaked markup from the visible content, or None if this isn't a DSML call.
    """
    m = _DSML_SEP_RE.search(text or "")
    if not m:
        return None
    name = m.group(1)
    args, span_end = {}, m.end()
    brace = next((i for i in range(m.end(), len(text)) if text[i] in "{["), None)
    if brace is not None:
        close = _match_bracket(text, brace)
        if close is not None:
            try:
                args = json.loads(text[brace : close + 1])
                span_end = close + 1
            except (json.JSONDecodeError, ValueError):
                args = {}
    calls = _calls_from_data([{"name": name, "arguments": args}], allowed_names)
    if not calls:
        return None
    return calls, m.start(), span_end


def _parse_pseudo_calls(text: str, allowed_names=None):
    """Parse `name(arg=val, ...)` pseudo-calls the model prints as prose.

    Returns `(tool_calls, spans)` where each span is the (start, end) of a
    matched call so the caller can strip it from the visible content, or None.
    Only names in `allowed_names` are considered, which keeps prose that merely
    contains an identifier-paren pair from being mistaken for a call.
    """
    names = {n for n in (allowed_names or ()) if n}
    if not names or not text:
        return None
    # Longest name first so `write_file` wins over `write` at the same position.
    calls: List[dict] = []
    spans: List[Tuple[int, int]] = []
    for name in sorted(names, key=len, reverse=True):
        for m in re.finditer(r"(?<![\w.])" + re.escape(name) + r"\s*\(", text):
            open_idx = m.end() - 1
            if any(s <= open_idx < e for s, e in spans):
                continue
            close = _match_bracket(text, open_idx)
            if close is None:
                continue
            args = _parse_pseudo_args(text[open_idx + 1 : close])
            if args is None:
                continue
            calls.append({
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            })
            spans.append((m.start(), close + 1))
    if not calls:
        return None
    return calls, spans


def _parse_pseudo_args(inner: str) -> Optional[dict]:
    """Decode the inside of a `name(...)` call into an arguments dict, else None."""
    inner = (inner or "").strip()
    if not inner:
        return {}
    if inner[0] in "{[":
        try:
            parsed = json.loads(inner)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        if parsed is not None:
            return {"value": parsed}
    args: dict = {}
    for part in _split_top_level(inner, ","):
        if "=" not in part:
            return None
        key, _, value = part.partition("=")
        key = key.strip().strip("'\"")
        if not key:
            return None
        args[key] = _parse_scalar(value.strip())
    return args


def _parse_scalar(value: str):
    """Best-effort decode of a single `key=value` right-hand side."""
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        pass
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _split_top_level(text: str, sep: str) -> List[str]:
    """Split on `sep`, ignoring separators inside quotes or nested brackets."""
    parts: List[str] = []
    buf: List[str] = []
    depth, quote, esc = 0, None, False
    for c in text:
        if quote:
            buf.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                quote = None
            continue
        if c in "'\"":
            quote = c
            buf.append(c)
        elif c in "{[(":
            depth += 1
            buf.append(c)
        elif c in "}])":
            depth -= 1
            buf.append(c)
        elif c == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
    parts.append("".join(buf))
    return parts


def _first_json(text: str):
    """Decode the first balanced JSON object/array in `text`, else None."""
    for block in _iter_json_blocks(text or ""):
        try:
            return json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
    stripped = (text or "").strip()
    if stripped:
        try:
            return json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _json_candidates(raw: str):
    """Yield progressively: the trimmed text, then every balanced JSON block."""
    seen = set()
    stripped = raw.strip()
    if stripped:
        seen.add(stripped)
        yield stripped
    for block in _iter_json_blocks(raw):
        if block not in seen:
            seen.add(block)
            yield block


def _iter_json_blocks(text: str):
    """Yield every balanced {...} / [...] substring, respecting strings/escapes."""
    i, n = 0, len(text)
    while i < n:
        if text[i] in "{[":
            end = _match_bracket(text, i)
            if end is not None:
                yield text[i : end + 1]
                i = end + 1
                continue
        i += 1


def _match_bracket(text: str, start: int) -> Optional[int]:
    """Return the index of the bracket matching text[start], or None."""
    opener = text[start]
    closer = {"{": "}", "[": "]", "(": ")"}.get(opener)
    if closer is None:
        return None
    depth = 0
    quote = None
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if quote:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                quote = None
            continue
        if c in "'\"":
            quote = c
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i
    return None


# --- truncation detection (continuation support) ----------------------------

# Signals that the model was composing a tool call rather than a plain answer.
_TOOL_INTENT_RE = re.compile(
    r'("arguments"|"tool_calls"|"name"\s*:|```|<tool_call|<function_call|<\|tool|function\s*<\|)',
    re.IGNORECASE,
)


def _bracket_state(text: str) -> Tuple[int, bool]:
    """Return (nesting depth, ended-inside-a-string?) for brackets in `text`.

    Brackets inside strings/escapes are ignored so trailing JSON is measured
    rather than the prose around it.
    """
    depth, quote, esc = 0, None, False
    for c in text:
        if quote:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                quote = None
            continue
        if c in "'\"":
            quote = c
        elif c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
    return depth, quote is not None


def looks_truncated(text: str, allowed_names: Optional[Iterable[str]] = None) -> bool:
    """Heuristic: did DeepSeek cut this reply off mid tool call?

    Only consulted after `parse_tool_calls` has already failed, so any positive
    signal means the model *intended* a call but never finished emitting it. We
    accept the cost of a false positive (one extra continuation round-trip) in
    exchange for not silently ending the agent's turn on a truncated call.
    """
    if not text:
        return False
    names = {n for n in (allowed_names or ()) if n}

    # 1. Unclosed markdown fence: ```tool_calls ... with no trailing ```.
    if text.count("```") % 2 == 1:
        return True

    # 2. Native XML block opened but never closed.
    for tag in ("tool_call", "function_call"):
        opens = len(re.findall(rf"<{tag}\b", text, re.IGNORECASE))
        closes = len(re.findall(rf"</{tag}>", text, re.IGNORECASE))
        if opens > closes:
            return True

    # 3. JSON array/object left open — only when the model showed call intent.
    depth, _ = _bracket_state(text)
    if depth > 0 and (_TOOL_INTENT_RE.search(text) or any(n in text for n in names)):
        return True

    return False


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
