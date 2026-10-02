"""Validate terminal replies produced by the sites' own frontend requests."""

import json
import struct

from chat_protocol import sse_events
from .common import ProviderUnavailable


def _object(payload):
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError):
        raise ProviderUnavailable("Unsupported browser response JSON") from None
    if not isinstance(value, dict) or value.get("error"):
        raise ProviderUnavailable("Browser provider rejected the completion")
    return value


def grok_answer(body):
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


def connect_completed(body):
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
            _object(payload)
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
