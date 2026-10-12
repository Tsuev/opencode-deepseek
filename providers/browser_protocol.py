"""Validate terminal replies produced by the sites' own frontend requests."""

import html
import json
import re
import struct

from chat_protocol import sse_events
from .common import ProviderUnavailable

_GEMINI_APP_PATH = re.compile(r"/app/[a-zA-Z0-9_-]{1,128}\Z")
_GEMINI_SCAFFOLD = re.compile(
    r"```(?:python|javascript|text)\?code_(?:reference|stdout)[^\n]*\n.*?```\n?",
    re.DOTALL,
)
_GEMINI_CONV = re.compile(r"^[a-zA-Z0-9_-]{6,128}$")


def _object(payload, on_error=None):
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError):
        raise ProviderUnavailable("Unsupported browser response JSON") from None
    if isinstance(value, dict) and value.get('error') and on_error is not None:
        raise on_error(value['error'])
    if not isinstance(value, dict) or value.get("error"):
        raise ProviderUnavailable("Browser provider rejected the completion")
    return value


def grok_answer(body, expected_chat=None):
    """Only a complete modelResponse, never an accumulated partial token tail."""
    final = None
    for line in body.splitlines():
        if not line.strip():
            continue
        frame = _object(line)
        result = frame.get("result")
        if not isinstance(result, dict) or result.get("error"):
            raise ProviderUnavailable("Unsupported Grok response frame")
        # New conversations wrap response; resumed conversations return it
        # directly. Both forms have the same terminal/error requirements.
        response = result.get("response", result)
        if not isinstance(response, dict) or response.get("error"):
            raise ProviderUnavailable("Grok rejected the completion")
        if expected_chat is not None:
            # Some frames omit identity; validate every explicit identity that
            # is present rather than trusting the controller's visible URL.
            objects = [result, response, result.get("conversation"), response.get("modelResponse")]
            for obj in objects:
                if isinstance(obj, dict):
                    for key in ("conversationId", "conversation_id"):
                        if key in obj and obj[key] != expected_chat:
                            raise ProviderUnavailable("Grok response belongs to a different conversation")
        model = response.get("modelResponse")
        if model is not None:
            if (not isinstance(model, dict) or model.get("partial") is True
                    or model.get("error") or model.get("streamErrors")):
                raise ProviderUnavailable("Grok returned an incomplete answer")
            text = model.get("message")
            if not isinstance(text, str) or not text.strip():
                raise ProviderUnavailable("Grok returned no text answer")
            if final is not None:
                raise ProviderUnavailable("Grok returned ambiguous final answers")
            final = text
    if final is None:
        raise ProviderUnavailable("Grok disconnected before a complete modelResponse")
    return final


def glm_answer(body):
    text, done = "", False
    for event, payload in sse_events(body.decode("utf-8").splitlines()):
        if not payload or payload == "[DONE]":
            continue
        frame = _object(payload)
        if event == "error" or frame.get("type") == "error":
            raise ProviderUnavailable("GLM rejected the completion")
        if frame.get("type") != "chat:completion":
            continue
        data = frame.get("data")
        if not isinstance(data, dict) or data.get("error"):
            raise ProviderUnavailable("Invalid GLM completion frame")
        if data.get("scope", "legacy") != "legacy":
            continue
        # The v2 frontend uses a separate global terminal phase. It confirms
        # the accumulated answer, but must never import reasoning snapshots.
        if data.get("phase") == "done" and data.get("done") is True:
            if not text.strip():
                raise ProviderUnavailable("GLM completed without an answer phase")
            done = True
            continue
        # Reasoning/tool phases must not become executable tool-call text.
        if data.get("phase") in (None, "answer", "final"):
            if done and ("content" in data or "delta_content" in data):
                raise ProviderUnavailable("GLM sent answer content after completion")
            if "content" in data:
                if not isinstance(data["content"], str):
                    raise ProviderUnavailable("Invalid GLM answer content")
                text = data["content"]
            elif "delta_content" in data:
                if not isinstance(data["delta_content"], str):
                    raise ProviderUnavailable("Invalid GLM answer delta")
                text += data["delta_content"]
            if data.get("done") is True:
                done = True
    if not done or not text.strip():
        raise ProviderUnavailable("GLM disconnected before a complete text answer")
    return text


def _gemini_normalize_path(path):
    if not isinstance(path, str):
        return None
    match = re.fullmatch(r"/u/\d+(/app/[a-zA-Z0-9_-]{1,128})", path)
    if match:
        path = match.group(1)
    return path if _GEMINI_APP_PATH.fullmatch(path) else None


def _gemini_payloads(body):
    """Yield decoded wrb.fr payloads from a Gemini batchexecute StreamGenerate body."""
    if not body:
        return
    text = body.decode("utf-8", errors="strict")
    if "BardErrorInfo" in text:
        raise ProviderUnavailable("Gemini rejected the completion")
    if text.startswith(")]}'"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("[") or "wrb.fr" not in line:
            continue
        try:
            chunks = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(chunks, list):
            continue
        for chunk in chunks:
            if not (isinstance(chunk, list) and chunk and chunk[0] == "wrb.fr"
                    and len(chunk) > 2 and isinstance(chunk[2], str) and chunk[2]):
                continue
            try:
                payload = json.loads(chunk[2])
            except (ValueError, UnicodeError):
                continue
            if isinstance(payload, list):
                yield payload


def _gemini_decode_body(body):
    """Decode a Gemini/AI Mode capture; tolerate XSSI prefixes and \\u003c HTML."""
    if not body:
        return ""
    try:
        raw = body.decode("utf-8", errors="strict")
    except UnicodeError:
        raw = body.decode("utf-8", errors="replace")
    if raw.startswith(")]}'"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[4:]
    # Google often ships HTML tags as JSON-unicode escapes inside async bodies.
    if "\\u003c" in raw or "\\u003e" in raw:
        raw = (raw.replace("\\u003c", "<").replace("\\u003e", ">")
               .replace("\\u003C", "<").replace("\\u003E", ">")
               .replace("\\u0026", "&").replace("\\/", "/"))
    return raw


def _gemini_answer_region(html):
    """Prefer AI Mode answer column / blocks over the full folif dump."""
    # Prefer main answer column when present (skips left-rail / query chrome later).
    col = re.search(
        r'data-container-id="main-col"|class="[^"]*\bmZJni\b[^"]*"',
        html,
    )
    matches = list(re.finditer(
        r'class="(?:n6owBd|otQkpb|ZLzG0d|T286Pc|Z1qcYe|aiModeUiCodeBlock__Container)[^"]*"'
        r'|data-subtree="aim(?:c|fl)"|jsname="ap3N9d"',
        html,
    ))
    if not matches and not col:
        return html
    start = col.start() if col else matches[0].start()
    if matches:
        start = min(start, matches[0].start())
    for cls in ("ZLzG0d", "otQkpb"):
        heading = html.rfind(f'class="{cls}', max(0, start - 8000), start + 1)
        # class may be prefixed: class="foo ZLzG0d
        if heading == -1:
            heading = html.rfind(cls, max(0, start - 8000), start + 1)
            if heading != -1:
                heading = html.rfind("class=", max(0, heading - 80), heading + 1)
        if heading != -1:
            start = min(start, heading)
            break
    lt = html.rfind("<", max(0, start - 300), start + 1)
    if lt != -1:
        start = lt
    end = len(html)
    for marker in ("vIWmYe", 'aria-label="Copy"', "Related questions", "related questions"):
        idx = html.find(marker, start + 200)
        if idx != -1:
            end = min(end, idx)
    return html[start:end]


def _gemini_heading_md(inner, level=2):
    text = re.sub(r"<[^>]+>", " ", inner)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    level = 2 if level < 1 else 6 if level > 6 else level
    return f"\n\n{'#' * level} {text}\n\n"


def _gemini_extract_balanced_div(markup, start):
    """Return (inner_html, end_index) for a <div...> opened at start, or None."""
    open_m = re.match(r"(?is)<div\b[^>]*>", markup[start:])
    if not open_m:
        return None
    inner_start = start + open_m.end()
    pos = inner_start
    depth = 1
    last_close_len = 0
    while pos < len(markup) and depth:
        nxt = re.search(r"(?is)<div\b[^>]*>|</div\s*>", markup[pos:])
        if not nxt:
            return None
        token = nxt.group(0)
        pos = pos + nxt.end()
        if token.lower().startswith("<div"):
            depth += 1
        else:
            depth -= 1
            last_close_len = len(token)
    if depth:
        return None
    return markup[inner_start: pos - last_close_len], pos


def _gemini_replace_code_blocks(markup):
    """Turn AI Mode code containers into fenced markdown (nested div-safe)."""
    out = []
    pos = 0
    marker = re.compile(
        r'(?is)<div\b[^>]*class="[^"]*\b(?:aiModeUiCodeBlock__Container|BpxUDf)\b[^"]*"[^>]*>',
    )
    while True:
        match = marker.search(markup, pos)
        if not match:
            out.append(markup[pos:])
            break
        out.append(markup[pos:match.start()])
        extracted = _gemini_extract_balanced_div(markup, match.start())
        if not extracted:
            out.append(match.group(0))
            pos = match.end()
            continue
        block, end = extracted
        lang_m = re.search(r'(?is)class="[^"]*\bGlO4G\b[^"]*"[^>]*>(.*?)</(?:div|span)>', block)
        lang = re.sub(r"<[^>]+>", "", lang_m.group(1)).strip() if lang_m else ""
        code_m = re.search(r"(?is)<code\b[^>]*>(.*?)</code>", block) or re.search(
            r"(?is)<pre\b[^>]*>(.*?)</pre>", block,
        )
        body = re.sub(r"<[^>]+>", "", code_m.group(1) if code_m else "")
        body = html.unescape(body).rstrip("\n")
        out.append(f"\n\n```{lang}\n{body}\n```\n\n" if body.strip() else " ")
        pos = end
    return "".join(out)


def _gemini_html_text(text):
    """Best-effort Markdown from Google AI Mode /async/folif HTML fragments."""
    region = _gemini_answer_region(text)
    # Drop query rewrite / citation chips before structure conversion.
    cleaned = re.sub(r'(?is)<h3\b[^>]*class="[^"]*\bMheKwc\b[^"]*"[^>]*>.*?</h3>', " ", region)
    cleaned = re.sub(
        r'(?is)<(?:div|span|button)\b[^>]*class="[^"]*\b(?:DHPVt|NMq1me|iMHp6c|vIWmYe|ofHStc)\b[^"]*"[^>]*>.*?</(?:div|span|button)>',
        " ",
        cleaned,
    )
    cleaned = re.sub(r"(?is)<(script|style|svg)\b[^>]*>.*?</\1>", " ", cleaned)
    cleaned = _gemini_replace_code_blocks(cleaned)
    cleaned = re.sub(r'(?is)<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"[\2](\1)", cleaned)
    cleaned = re.sub(r"(?is)<(strong|b)\b[^>]*>(.*?)</\1>", r"**\2**", cleaned)
    cleaned = re.sub(r"(?is)<(em|i)\b[^>]*>(.*?)</\1>", r"*\2*", cleaned)
    cleaned = re.sub(r"(?is)<code\b[^>]*>(.*?)</code>", r"`\1`", cleaned)
    cleaned = re.sub(
        r"(?is)<pre\b[^>]*>(.*?)</pre>",
        lambda m: "\n```\n" + re.sub(r"<[^>]+>", "", m.group(1)) + "\n```\n",
        cleaned,
    )
    cleaned = re.sub(r"(?is)<h1\b[^>]*>(.*?)</h1>", r"\n# \1\n", cleaned)
    cleaned = re.sub(r"(?is)<h2\b[^>]*>(.*?)</h2>", r"\n## \1\n", cleaned)
    cleaned = re.sub(r"(?is)<h3\b[^>]*>(.*?)</h3>", r"\n### \1\n", cleaned)

    def _role_heading(match):
        attrs, inner = match.group(2), match.group(3)
        level_m = re.search(r'aria-level="(\d+)"', attrs)
        level = int(level_m.group(1)) if level_m else 2
        if re.search(r'\b(?:otQkpb|ZLzG0d)\b', attrs) or 'role="heading"' in attrs:
            return _gemini_heading_md(inner, level)
        return match.group(0)

    # Same closing tag as open — otherwise </span> inside ZLzG0d truncates the title.
    cleaned = re.sub(
        r'(?is)<(div|span)\b([^>]*(?:class="[^"]*\b(?:otQkpb|ZLzG0d)\b[^"]*"|role="heading")[^>]*)>(.*?)</\1>',
        _role_heading,
        cleaned,
    )
    # Answer paragraphs: keep blank-line separation after n6owBd blocks.
    cleaned = re.sub(r'(?is)<div\b[^>]*class="[^"]*\bn6owBd\b[^"]*"[^>]*>', "\n\n", cleaned)
    cleaned = re.sub(r"(?is)<li\b[^>]*>", "\n- ", cleaned)
    cleaned = re.sub(r"(?is)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?is)</p\s*>", "\n\n", cleaned)
    cleaned = re.sub(r"(?is)</div\s*>", "\n", cleaned)
    cleaned = re.sub(r"(?is)</li\s*>", "\n", cleaned)
    cleaned = re.sub(r"(?is)<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    # Collapse runs of spaces/tabs inside a line, but keep newlines.
    cleaned = re.sub(r"[^\S\n]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _gemini_norm_ws(text):
    return " ".join((text or "").replace("\u200b", "").replace("\ufeff", "").split())


_GEMINI_DISCLAIMER = re.compile(
    r"(?is)(?:\n|\s)*"
    r"(?:В ответах искусственного интеллекта могут быть ошибки\."
    r"|AI responses may (?:include|contain) mistakes\."
    r"|Las respuestas de (?:la )?IA pueden contener errores\."
    r"|Les réponses de l['’]IA peuvent contenir des erreurs\.)"
    r".*\Z",
)


def _gemini_strip_disclaimer(text):
    """Drop Google AI Mode footer disclaimer (and anything after it)."""
    if not text:
        return text
    return _GEMINI_DISCLAIMER.sub("", text).rstrip()


def _gemini_drop_prompt_echo(text, prompt):
    """Remove a restated OpenCode prompt from AI Mode HTML / wrb text.

    Preserve newlines and blank lines — callers may already have Markdown.
    """
    if not text:
        return text
    got = text.replace("\u200b", "").replace("\ufeff", "")
    wanted = _gemini_norm_ws(prompt) if isinstance(prompt, str) else ""
    normalized = _gemini_norm_ws(got)
    if wanted and normalized == wanted:
        return ""
    if wanted and normalized.startswith(wanted):
        prefix = r"\s*" + r"\s+".join(re.escape(word) for word in wanted.split())
        got = re.sub(r"^" + prefix + r"\s*", "", got, count=1)
    elif wanted and wanted in normalized:
        # Embedded prompt text has no trustworthy boundary; do not guess.
        return ""
    if "Available tools, given as JSON Schema" in got or (
            "```tool_calls" in got and "You can call functions" in got):
        got = re.sub(
            r"You can call functions \(tools\)\. Available tools, given as JSON Schema:"
            r"[\s\S]*?(?=```tool_calls|$)",
            "",
            got,
        )
        got = re.sub(r"```tool_calls[\s\S]*?```", "", got)
    got = re.sub(r"[ \t]+\n", "\n", got)
    got = re.sub(r"\n{3,}", "\n\n", got)
    return got.strip()


def _gemini_aimode_path(path):
    """True for the synthetic AI Mode conversation that lives on /search?udm=50."""
    return isinstance(path, str) and (
        path == "/app/aimode" or path == "/search" or path.startswith("/search?")
    )


def gemini_paths_compatible(expected, reported):
    """Resume tokens: /app/aimode and /search are the same AI Mode thread."""
    if expected == reported:
        return True
    if _gemini_aimode_path(expected) and _gemini_aimode_path(reported):
        return True
    left = _gemini_normalize_path(expected)
    right = _gemini_normalize_path(reported)
    return bool(left and right and left == right)


def gemini_conversation_path(body, reported_path=None):
    """Prefer /app/<id> from the SPA URL; recover from StreamGenerate when still on /app."""
    normalized = _gemini_normalize_path(reported_path)
    if normalized:
        return normalized
    for payload in _gemini_payloads(body):
        identity = payload[1] if len(payload) > 1 else None
        if isinstance(identity, list) and identity:
            candidate = identity[0]
            if isinstance(candidate, str) and _GEMINI_CONV.fullmatch(candidate):
                return "/app/" + candidate
        if isinstance(identity, str) and _GEMINI_CONV.fullmatch(identity):
            return "/app/" + identity
    try:
        text = body.decode("utf-8", errors="strict") if body else ""
    except UnicodeError:
        text = ""
    match = re.search(r"/app/([a-zA-Z0-9_-]{6,128})", text)
    if match and _GEMINI_CONV.fullmatch(match.group(1)):
        return "/app/" + match.group(1)
    match = re.search(r"\b(c_[a-zA-Z0-9_-]{6,128})\b", text)
    if match:
        return "/app/" + match.group(1)
    # AI Mode empty chats stay on /search?udm=50 until Google assigns /app/<id>.
    if _gemini_aimode_path(reported_path):
        return "/app/aimode"
    raise ProviderUnavailable("Gemini browser response has no conversation path")


# Gemini browser answer capture — fork vs other tab providers
# -----------------------------------------------------------
# Only request-bound wire captures reach this decoder. Gemini may use
# StreamGenerate frames or folif HTML; visible DOM is not a completion source.
# Prompt echoes without a separately identifiable answer remain failures.


def gemini_answer(body, prompt=None):
    """Extract the final Gemini browser answer (wire text or folif HTML)."""
    if not body:
        raise ProviderUnavailable("Gemini browser returned an empty completion")
    texts = []
    for payload in _gemini_payloads(body):
        parts = payload[4] if len(payload) > 4 else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not (isinstance(part, list) and len(part) > 1 and isinstance(part[1], list)):
                continue
            for item in part[1]:
                if isinstance(item, str) and item.strip():
                    texts.append(item)
    text = ""
    prompt_only = False
    for candidate in reversed(texts):
        cleaned = _GEMINI_SCAFFOLD.sub("", candidate).strip()
        if not cleaned:
            continue
        stripped = _gemini_drop_prompt_echo(cleaned, prompt)
        if stripped:
            text = stripped
            break
        prompt_only = True
    if not text:
        # Decode captured HTML or a plain-text wire body.
        raw = _gemini_decode_body(body)
        if "<" in raw:
            cleaned = _GEMINI_SCAFFOLD.sub("", _gemini_html_text(raw)).strip()
            text = _gemini_drop_prompt_echo(cleaned, prompt)
            if cleaned and not text:
                prompt_only = True
        elif (raw.strip() and not texts and "wrb.fr" not in raw
              and not raw.lstrip().startswith("[[") and "BardErrorInfo" not in raw):
            # Plain captured text, not a failed wrb unwrap.
            cleaned = _GEMINI_SCAFFOLD.sub("", raw).strip()
            text = _gemini_drop_prompt_echo(cleaned, prompt)
            if cleaned and not text:
                prompt_only = True
    if not text:
        if prompt_only:
            raise ProviderUnavailable(
                "Gemini browser returned the request prompt instead of an answer"
            )
        raise ProviderUnavailable("Gemini disconnected before a complete text answer")
    return _gemini_strip_disclaimer(text)


_CONV_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
_C_PATH = re.compile(r"/c/[a-zA-Z0-9_-]{1,128}\Z")


def _sse_conversation_path(body, reported_path, label, extract_id):
    """Prefer a reported /c/<id>. Otherwise scan SSE JSON frames for one."""
    if isinstance(reported_path, str) and _C_PATH.fullmatch(reported_path):
        return reported_path
    if not body or body.lstrip()[:1] in (b"{", b"["):
        raise ProviderUnavailable(f"{label} browser response has no conversation path")
    for _event, payload in sse_events(body.decode("utf-8").splitlines()):
        if not payload or payload == "[DONE]":
            continue
        try:
            frame = json.loads(payload)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(frame, dict):
            continue
        candidate = extract_id(frame)
        if isinstance(candidate, str) and _CONV_ID.fullmatch(candidate):
            return "/c/" + candidate
    raise ProviderUnavailable(f"{label} browser response has no conversation path")


def _qwen_chat_id(frame):
    created = frame.get("response.created")
    candidates = [frame.get("chat_id")]
    if isinstance(created, dict):
        candidates.append(created.get("chat_id"))
    for candidate in candidates:
        if isinstance(candidate, str) and _CONV_ID.fullmatch(candidate):
            return candidate
    return None


def qwen_conversation_path(body, reported_path=None):
    """Prefer /c/<chat_id> from SSE; the SPA often updates the URL after the stream."""
    return _sse_conversation_path(body, reported_path, "Qwen", _qwen_chat_id)


def qwen_reply(body, chat_id=None):
    """Return (answer, finish_reason) from Qwen Chat's website SSE.

    Uses the same candidate/terminal state machine as the HTTP client: one
    primary response_id contributes text, ambiguous unnamed frames are
    rejected, and an output-limit stop stays `length` for the tool contract.
    """
    if not body:
        raise ProviderUnavailable("Qwen browser returned an empty completion")
    # Anti-bot / captcha envelopes are JSON, not SSE.
    stripped = body.lstrip()
    if stripped[:1] in (b"{", b"["):
        try:
            envelope = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ProviderUnavailable("Unsupported Qwen browser response") from None
        if isinstance(envelope, dict) and (
                isinstance(envelope.get("ret"), list)
                or (isinstance(envelope.get("data"), dict) and "url" in envelope["data"])):
            raise ProviderUnavailable(
                "Qwen requested browser verification; complete it in the paired tab")
        raise ProviderUnavailable("Qwen browser did not return an SSE completion")
    from qwen.client import QwenStreamError, _parse_sse
    meta = {"chat_id": chat_id}
    try:
        text = "".join(_parse_sse(body.decode("utf-8").splitlines(), meta))
    except QwenStreamError:
        raise ProviderUnavailable("Qwen disconnected before a complete text answer") from None
    except UnicodeError:
        raise ProviderUnavailable("Invalid Qwen answer content") from None
    if not text.strip():
        raise ProviderUnavailable("Qwen disconnected before a complete text answer")
    return text, meta.get("finish_reason", "stop")


def qwen_answer(body, chat_id=None):
    """Compatibility wrapper: answer text only. Output-limit stops are refused
    here because a bare string cannot carry finish_reason=length."""
    text, finish = qwen_reply(body, chat_id)
    if finish != "stop":
        raise ProviderUnavailable("Qwen stopped at its output limit")
    return text


def chatgpt_conversation_path(body, reported_path=None):
    """Prefer /c/<id> from the SPA; recover conversation_id from the SSE body."""
    return _sse_conversation_path(
        body, reported_path, "ChatGPT", lambda frame: frame.get("conversation_id"))


_CHATGPT_PART = re.compile(r"/message/content/parts/\d+\Z")


def _chatgpt_assistant_text(message):
    if not isinstance(message, dict):
        return None
    author = message.get("author")
    if not isinstance(author, dict) or author.get("role") != "assistant":
        return None
    if message.get("recipient") not in (None, "all"):
        return None
    content = message.get("content")
    if not isinstance(content, dict):
        return None
    if content.get("content_type") not in ("text", "multimodal_text"):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    chunks = [part for part in parts if isinstance(part, str)]
    if not chunks:
        return None
    return "".join(chunks)


def _chatgpt_prefer(text, current_id, part, msg_id):
    """Keep a longer stream if a later snapshot is only the first flush."""
    if msg_id and msg_id != current_id:
        if (current_id or text.startswith(part)) and len(part) < len(text):
            return text, current_id or msg_id
        return part, msg_id
    if text.startswith(part) and len(part) < len(text):
        return text, current_id or msg_id
    return part, msg_id or current_id


def _chatgpt_apply_op(text, finished, op):
    """Apply one frontend patch. Appends carry the tail after the first flush."""
    if not isinstance(op, dict):
        return text, finished
    path = op.get("p") if isinstance(op.get("p"), str) else ""
    operation = op.get("o") if isinstance(op.get("o"), str) else ""
    value = op.get("v")
    if operation == "patch" and isinstance(value, list):
        for item in value:
            text, finished = _chatgpt_apply_op(text, finished, item)
        return text, finished
    if path == "/message/status" or path.endswith("/status"):
        if value == "finished_successfully":
            finished = True
        return text, finished
    if isinstance(value, str) and (path in ("", "/") or _CHATGPT_PART.fullmatch(path)):
        if operation == "replace":
            text = value
        elif value:
            text += value
        return text, finished
    return text, finished


def _chatgpt_note_message(text, finished, current_id, message):
    if not isinstance(message, dict):
        return text, finished, current_id
    part = _chatgpt_assistant_text(message)
    if part is not None:
        text, current_id = _chatgpt_prefer(text, current_id, part, message.get("id"))
    if message.get("status") == "finished_successfully" and part is not None:
        finished = True
    return text, finished, current_id


def chatgpt_answer(body):
    """Extract the finished assistant text from ChatGPT's website SSE stream."""
    if not body:
        raise ProviderUnavailable("ChatGPT browser returned an empty completion")
    stripped = body.lstrip()
    if stripped[:1] in (b"{", b"["):
        try:
            envelope = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ProviderUnavailable("Unsupported ChatGPT browser response") from None
        if isinstance(envelope, dict) and (envelope.get("error") or envelope.get("detail")):
            raise ProviderUnavailable("ChatGPT rejected the completion")
        raise ProviderUnavailable("ChatGPT browser did not return an SSE completion")
    text, done, finished = "", False, False
    current_id = None
    for _event, payload in sse_events(body.decode("utf-8").splitlines()):
        if not payload:
            continue
        if payload == "[DONE]":
            done = True
            break
        try:
            frame = json.loads(payload)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(frame, dict):
            continue
        if frame.get("error") or frame.get("type") in ("error", "conversation_error"):
            raise ProviderUnavailable("ChatGPT rejected the completion")
        value = frame.get("v")
        # Patch list: the tail after the first flush, including status.
        if isinstance(value, list) or frame.get("o") == "patch" or isinstance(frame.get("p"), str):
            if isinstance(value, list) and frame.get("o") != "patch":
                for item in value:
                    text, finished = _chatgpt_apply_op(text, finished, item)
            else:
                text, finished = _chatgpt_apply_op(text, finished, frame)
            continue
        if isinstance(value, str):
            text, finished = _chatgpt_apply_op(text, finished, frame)
            continue
        if isinstance(value, dict):
            message = value.get("message") if value.get("message") is not None else value
            text, finished, current_id = _chatgpt_note_message(text, finished, current_id, message)
            continue
        message = frame.get("message")
        if isinstance(message, dict):
            text, finished, current_id = _chatgpt_note_message(text, finished, current_id, message)
    if not (done or finished) or not text.strip():
        raise ProviderUnavailable("ChatGPT disconnected before a complete text answer")
    return text


def connect_completed(body, on_error=None):
    """Connect RPC EOF alone is not success: require its end-stream envelope."""
    offset, messages, ended = 0, 0, False
    while offset < len(body):
        if len(body) - offset < 5 or ended:
            raise ProviderUnavailable("Invalid Connect response framing")
        flags, length = body[offset], struct.unpack_from(">I", body, offset + 1)[0]
        offset += 5
        if length > 8 * 1024 * 1024 or offset + length > len(body):
            raise ProviderUnavailable("Incomplete Connect response envelope")
        payload = body[offset:offset + length]
        offset += length
        if flags == 2:
            _object(payload, on_error=on_error)
            ended = True
        elif flags == 0:
            messages += 1
        else:
            raise ProviderUnavailable("Unsupported Connect response compression or flags")
    if not ended or not messages:
        raise ProviderUnavailable("Kimi disconnected before Connect completion")


def _mistral_text_chunk(value):
    if not isinstance(value, dict) or value.get("type") != "text" or not isinstance(value.get("text"), str):
        raise ProviderUnavailable("Unsupported Mistral answer chunk")
    context = value.get("_context")
    if "_context" in value and (not isinstance(context, dict) or context.get("type") != "reasoning"):
        raise ProviderUnavailable("Unsupported Mistral text context")
    return {"text": value["text"], "reasoning": "_context" in value}


def _mistral_patch_answer(body, prompt, expected_chat, request_turn):
    """Vibe's data stream: bind text patches to its completed assistant turn."""
    user = None
    chat_id = expected_chat
    if request_turn is not None:
        if (not isinstance(request_turn, dict) or not isinstance(request_turn.get("user_id"), str)
                or not request_turn["user_id"] or type(request_turn.get("version")) is not int
                or request_turn["version"] != 0
                or request_turn.get("chat_id") not in (None, expected_chat)):
            raise ProviderUnavailable("Invalid Mistral request turn binding")
        user = {"id": request_turn["user_id"], "version": request_turn["version"]}
    assistant = None
    chunks, content = None, ""
    done, ended = False, False
    bootstrap_seen = False
    for line in body.decode("utf-8").splitlines():
        if not line.strip():
            continue
        code, separator, payload = line.partition(":")
        if not separator:
            raise ProviderUnavailable("Invalid Mistral data-stream record")
        if code == "8":
            if payload != "null" or ended or not done:
                raise ProviderUnavailable("Invalid Mistral stream ending")
            ended = True
            continue
        if code != "15":
            raise ProviderUnavailable("Unsupported Mistral data-stream record")
        envelope = _object(payload)
        frame = envelope.get("json")
        if not isinstance(frame, dict) or frame.get("error"):
            raise ProviderUnavailable("Invalid Mistral message event")
        kind = frame.get("type")
        if kind == "chat":
            # Title updates can follow the end marker; they are not answer text.
            patches = frame.get("patches")
            if not isinstance(patches, list) or any(
                not isinstance(p, dict) or p.get("path") not in ("/generatedTitle", "/title", "/updatedAt")
                for p in patches
            ):
                raise ProviderUnavailable("Unsupported Mistral chat update")
            continue
        if kind == "bootstrap":
            if bootstrap_seen or assistant is not None or done or ended:
                raise ProviderUnavailable("Duplicate Mistral bootstrap")
            messages, chat = frame.get("messages"), frame.get("chat")
            if not isinstance(messages, list) or not isinstance(chat, dict) or not isinstance(chat.get("id"), str):
                raise ProviderUnavailable("Invalid Mistral conversation bootstrap")
            users = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
            current = users[-1] if users else None
            if (not current or not isinstance(current.get("id"), str)
                    or type(current.get("version")) is not int
                    or (prompt is not None and current.get("content") != prompt)
                    or (user is not None and (current["id"], current["version"]) != (user["id"], user["version"]))):
                raise ProviderUnavailable("Mistral bootstrap does not identify this user turn")
            chat_id = chat["id"]
            if expected_chat is not None and chat_id != expected_chat:
                raise ProviderUnavailable("Mistral response belongs to a different conversation")
            user, bootstrap_seen = current, True
            continue
        if kind != "message" or user is None or ended:
            raise ProviderUnavailable("Unsupported Mistral message event")
        message_id, version, patches = frame.get("messageId"), frame.get("messageVersion"), frame.get("patches")
        if not isinstance(message_id, str) or type(version) is not int or not isinstance(patches, list):
            raise ProviderUnavailable("Invalid Mistral message patches")
        if message_id == user["id"]:
            if version != user["version"] or any(
                not isinstance(p, dict) or p.get("path") != "/moderationCategory" for p in patches
            ):
                raise ProviderUnavailable("Unsupported Mistral user update")
            continue
        for patch in patches:
            if not isinstance(patch, dict):
                raise ProviderUnavailable("Invalid Mistral patch")
            op, path, value = patch.get("op"), patch.get("path"), patch.get("value")
            if path == "/" and op == "replace":
                if (assistant is not None or not isinstance(value, dict) or value.get("role") != "assistant"
                        or value.get("id") != message_id or value.get("version") != version
                        or value.get("parentId") != user["id"] or value.get("parentVersion") != user["version"]
                        or value.get("chatId") != chat_id or value.get("generationStatus") != "in-progress"):
                    raise ProviderUnavailable("Mistral assistant is not bound to this user turn")
                assistant = (message_id, version)
                content, chunks = value.get("content", ""), value.get("contentChunks")
                if not isinstance(content, str) or chunks is not None:
                    raise ProviderUnavailable("Unsupported Mistral initial assistant content")
                continue
            if assistant != (message_id, version) or done:
                raise ProviderUnavailable("Mistral sent patches outside the active assistant turn")
            if path == "/contentChunks" and op == "replace":
                if not isinstance(value, list):
                    raise ProviderUnavailable("Unsupported Mistral answer chunks")
                chunks = [_mistral_text_chunk(c) for c in value]
            elif isinstance(path, str) and path.startswith("/contentChunks/"):
                index, separator, field = path[len("/contentChunks/"):].partition("/")
                if not 1 <= len(index) <= 6 or not index.isascii() or not index.isdecimal() or chunks is None:
                    raise ProviderUnavailable("Invalid Mistral chunk index")
                index = int(index)
                if not separator and op == "add" and index <= len(chunks):
                    chunks.insert(index, _mistral_text_chunk(value))
                elif index >= len(chunks):
                    raise ProviderUnavailable("Mistral patched a missing chunk")
                elif not separator and op == "remove":
                    del chunks[index]
                elif field == "text" and op in ("append", "replace") and isinstance(value, str):
                    chunks[index]["text"] = value if op == "replace" else chunks[index]["text"] + value
                elif field == "_context" and op == "remove" and chunks[index]["reasoning"]:
                    chunks[index]["reasoning"] = False
                elif field == "_context/endTime" and op == "replace" and chunks[index]["reasoning"] and type(value) in (int, float):
                    continue  # Presentation timing does not change answer text.
                else:
                    raise ProviderUnavailable("Unsupported Mistral chunk patch")
            elif path == "/content" and op in ("replace", "append") and isinstance(value, str):
                content = value if op == "replace" else content + value
            elif path == "/generationStatus" and op == "replace":
                if value != "success":
                    raise ProviderUnavailable("Mistral assistant did not complete successfully")
                done = True
            else:
                raise ProviderUnavailable("Unsupported Mistral assistant patch")
    text = "".join(c["text"] for c in chunks if not c["reasoning"]) if chunks is not None else content
    if not done or not ended or not text.strip() or (content and chunks is not None and content != text):
        raise ProviderUnavailable("Mistral disconnected before a complete assistant answer")
    return text


def mistral_answer(body, prompt=None, expected_chat=None, request_turn=None):
    if body.lstrip().startswith(b"15:"):
        return _mistral_patch_answer(body, prompt, expected_chat, request_turn)
    text, done, ended = "", False, False
    for event, payload in sse_events(body.decode("utf-8").splitlines()):
        if payload == "[DONE]":
            if ended:
                raise ProviderUnavailable("Mistral returned duplicate stream endings")
            done = True
            ended = True
            continue
        if not payload:
            continue
        frame = _object(payload)
        kind = frame.get("type", event)
        if event == "error" or kind in ("error", "message.error"):
            raise ProviderUnavailable("Mistral rejected the completion")
        if kind in ("message.delta", "append-text"):
            if done:
                raise ProviderUnavailable("Mistral sent answer content after completion")
            value = frame.get("text", frame.get("content"))
            if not isinstance(value, str):
                raise ProviderUnavailable("Unsupported Mistral text delta")
            text += value
        elif kind in ("message.completed", "message.end", "complete"):
            if done:
                raise ProviderUnavailable("Mistral returned multiple completions")
            done = True
        elif "choices" in frame:
            choices = frame["choices"]
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise ProviderUnavailable("Unsupported Mistral choices")
            choice = choices[0]
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise ProviderUnavailable("Invalid Mistral delta")
            value = delta.get("content", "")
            if not isinstance(value, str):
                raise ProviderUnavailable("Invalid Mistral text")
            if done and (value or choice.get("finish_reason") is not None):
                raise ProviderUnavailable("Mistral sent answer content after completion")
            text += value
            if choice.get("finish_reason") == "stop":
                done = True
            elif choice.get("finish_reason") is not None:
                raise ProviderUnavailable("Mistral answer was not completed")
    if not done or not text.strip():
        raise ProviderUnavailable("Unsupported or incomplete Mistral answer")
    return text


_CLAUDE_CHAT_PATH = re.compile(r"/chat/[a-zA-Z0-9_-]{1,128}\Z")
_CLAUDE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _claude_message_text(message):
    """Prefer raw markdown from content[] text blocks; fall back to .text."""
    if not isinstance(message, dict):
        return None
    sender = message.get("sender")
    role = message.get("role")
    if sender in ("human", "user", "Human"):
        return None
    if sender not in ("assistant", "Assistant", "claude") and role != "assistant":
        return None
    parts = []
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind in (None, "text"):
                value = block.get("text")
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            elif kind == "thinking":
                continue
    if parts:
        return "\n\n".join(parts).strip()
    value = message.get("text")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _claude_leaf_assistant(payload):
    """Walk current_leaf → root and return the newest assistant text."""
    messages = payload.get("chat_messages")
    if not isinstance(messages, list) or not messages:
        return None
    by_id = {}
    for item in messages:
        if isinstance(item, dict) and isinstance(item.get("uuid"), str):
            by_id[item["uuid"]] = item
    leaf = payload.get("current_leaf_message_uuid")
    order = []
    seen = set()
    while isinstance(leaf, str) and leaf in by_id and leaf not in seen:
        seen.add(leaf)
        message = by_id[leaf]
        order.append(message)
        leaf = message.get("parent_message_uuid")
    if not order:
        order = [item for item in messages if isinstance(item, dict)]
    for message in order:
        text = _claude_message_text(message)
        if text:
            return text
    return None


def claude_conversation_path(body, reported_path=None):
    """Prefer /chat/<id> from the SPA; recover uuid from conversation JSON/SSE."""
    if isinstance(reported_path, str) and _CLAUDE_CHAT_PATH.fullmatch(reported_path):
        return reported_path
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return reported_path
    sample = text.lstrip()
    if sample.startswith("{"):
        try:
            payload = json.loads(sample)
        except (ValueError, UnicodeError):
            payload = None
        if isinstance(payload, dict):
            chat_id = payload.get("uuid") or payload.get("conversation_id") or payload.get("chat_id")
            if isinstance(chat_id, str) and _CLAUDE_UUID.fullmatch(chat_id):
                return "/chat/" + chat_id
    for event, payload in sse_events(text.splitlines()):
        if not payload or payload == "[DONE]":
            continue
        try:
            frame = json.loads(payload)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(frame, dict):
            continue
        for key in ("conversation_id", "chat_id", "uuid"):
            value = frame.get(key)
            if isinstance(value, str) and _CLAUDE_UUID.fullmatch(value):
                return "/chat/" + value
        message = frame.get("message")
        if isinstance(message, dict):
            chat_id = message.get("chat_id") or message.get("conversation_id")
            if isinstance(chat_id, str) and _CLAUDE_UUID.fullmatch(chat_id):
                return "/chat/" + chat_id
    return reported_path


def _claude_delta_text(frame):
    """Extract assistant text from one Claude.ai / Anthropic stream frame."""
    if not isinstance(frame, dict):
        return None, False, False
    kind = frame.get("type")
    if kind in ("error", "message_error") or frame.get("error"):
        raise ProviderUnavailable("Claude rejected the completion")
    reason = frame.get("stop_reason")
    if kind == "message_delta" and isinstance(frame.get("delta"), dict):
        reason = frame["delta"].get("stop_reason")
    if reason not in (None, "", "end_turn", "stop_sequence", "tool_use"):
        raise ProviderUnavailable("Claude completion did not finish normally")
    if kind in ("message_stop", "message.completed", "message.end"):
        return None, True, False
    if kind == "content_block_delta":
        delta = frame.get("delta")
        if isinstance(delta, dict):
            # Official Anthropic: delta.type == text_delta; web may omit type.
            if delta.get("type") in (None, "text_delta") and isinstance(delta.get("text"), str):
                return delta["text"], False, True
            if isinstance(delta.get("completion"), str):
                return delta["completion"], False, True
        return None, False, True
    if kind == "completion":
        value = frame.get("completion")
        if isinstance(value, str):
            stopped = frame.get("stop_reason") not in (None, "")
            return value, stopped, True
    if kind == "message.delta" or kind == "append-text":
        value = frame.get("text", frame.get("content"))
        if isinstance(value, str):
            return value, False, True
    if "choices" in frame:
        choices = frame["choices"]
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
            delta = choice.get("delta", {})
            chunk = ""
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                chunk = delta["content"]
            finish = choice.get("finish_reason")
            if finish not in (None, "stop", "tool_calls"):
                raise ProviderUnavailable("Claude completion did not finish normally")
            return chunk, finish is not None, True
    return None, False, False


def _claude_stream_text(body):
    """Line-oriented Claude SSE/NDJSON. Blank-line SSE records are optional.

    Claude.ai often emits consecutive `data: {...}` lines; requiring blank-line
    record boundaries (sse_events) drops those frames as invalid joined JSON.
    """
    text, saw_frame, done = "", False, False
    for raw in body.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("event:"):
            continue
        payload = line[5:].lstrip() if line.startswith("data:") else line
        if payload == "[DONE]":
            done = True
            continue
        if not payload.startswith("{"):
            continue
        try:
            frame = json.loads(payload)
        except (ValueError, UnicodeError):
            raise ProviderUnavailable("Invalid JSON in Claude stream") from None
        chunk, stopped, known = _claude_delta_text(frame)
        if done and chunk:
            raise ProviderUnavailable("Claude content after terminal marker")
        if known:
            saw_frame = True
        if isinstance(chunk, str) and chunk:
            text += chunk
        if stopped:
            done = True
    return text, saw_frame, done


def claude_answer(body, prompt=None):
    """Claude.ai capture: conversation JSON or terminal SSE/NDJSON.

    Prefer chat_messages leaf text when the body is a conversation dump.
    Streaming replies require a terminal marker.
    """
    sample = body.lstrip()
    if sample.startswith(b"{"):
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError):
            payload = None
        if isinstance(payload, dict):
            if payload.get("error") or payload.get("type") == "error":
                raise ProviderUnavailable("Claude rejected the completion")
            text = _claude_leaf_assistant(payload)
            if text:
                if prompt and text.strip() == str(prompt).strip():
                    raise ProviderUnavailable("Claude browser returned the request prompt instead of an answer")
                return text
            for key in ("completion", "completion_message", "message", "delta"):
                nested = payload.get(key)
                if isinstance(nested, dict):
                    text = _claude_message_text(nested) or (
                        nested.get("text") if isinstance(nested.get("text"), str) else None
                    )
                    if isinstance(text, str) and text.strip():
                        return text.strip()
                elif isinstance(nested, str) and nested.strip():
                    return nested.strip()
    text, saw_frame, done = _claude_stream_text(body)
    if text.strip() and done:
        return text.strip()
    # Generic terminal SSE uses message.delta + message.completed.
    try:
        return mistral_answer(body, prompt)
    except ProviderUnavailable:
        pass
    raise ProviderUnavailable("Unsupported or incomplete Claude answer")
