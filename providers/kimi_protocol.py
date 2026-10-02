"""Bind Kimi's JSON Connect text blocks to a completed assistant turn."""

import struct

from .browser_protocol import _object, connect_completed
from .common import ProviderUnavailable


def kimi_answer(body, prompt, expected_chat):
    if not isinstance(prompt, str) or not isinstance(expected_chat, str):
        raise ProviderUnavailable("Kimi requires request and conversation binding")
    connect_completed(body)
    user = assistant = None
    blocks, texts = {}, {}
    completed = done = chat_seen = False
    offset = 0
    while offset < len(body):
        flags, length = body[offset], struct.unpack_from(">I", body, offset + 1)[0]
        offset += 5
        payload = body[offset:offset + length]
        offset += length
        if flags == 2:
            continue  # Framing and transport errors were checked above.
        frame = _object(payload)
        events = [key for key in ("chat", "message", "block", "heartbeat", "done", "ref", "usage", "notification") if key in frame]
        if len(events) != 1:
            raise ProviderUnavailable("Unsupported Kimi response event")
        kind = events[0]
        value = frame[kind]
        if not isinstance(value, dict):
            raise ProviderUnavailable("Invalid Kimi response event")
        if kind == "heartbeat":
            continue
        if done:
            raise ProviderUnavailable("Kimi sent events after completion")
        op, mask = frame.get("op"), frame.get("mask", "")
        if kind == "chat":
            if value.get("id") != expected_chat or op != "set" or mask not in ("", "chat.name", "chat.lastRequest"):
                raise ProviderUnavailable("Kimi response belongs to a different conversation")
            chat_seen = True
        elif kind == "message":
            identity = value.get("id")
            if not chat_seen or not isinstance(identity, str) or not identity or op != "set":
                raise ProviderUnavailable("Invalid Kimi message identity")
            role, status = value.get("role"), value.get("status")
            if mask == "message.status":
                if identity != assistant or completed or status != "MESSAGE_STATUS_COMPLETED":
                    raise ProviderUnavailable("Kimi assistant did not complete successfully")
                completed = True
            elif mask in ("", "message"):
                if role == "system" and user is None and identity == expected_chat and status == "MESSAGE_STATUS_COMPLETED":
                    continue
                if role == "user" and user is None and assistant is None:
                    parts = value.get("blocks")
                    if (status != "MESSAGE_STATUS_COMPLETED" or not isinstance(parts, list)
                            or any(not isinstance(p, dict) or not isinstance(p.get("text"), dict)
                                   or not isinstance(p["text"].get("content"), str) for p in parts)
                            or "".join(p["text"]["content"] for p in parts) != prompt):
                        raise ProviderUnavailable("Kimi user turn does not match this request")
                    user = identity
                elif role == "assistant" and user is not None and assistant is None:
                    if identity == user or value.get("parentId") != user or status != "MESSAGE_STATUS_GENERATING":
                        raise ProviderUnavailable("Kimi assistant is not bound to this user turn")
                    if "blocks" in value and value["blocks"] != []:
                        raise ProviderUnavailable("Unsupported Kimi inline assistant blocks")
                    assistant = identity
                else:
                    raise ProviderUnavailable("Kimi returned ambiguous message turns")
            else:
                raise ProviderUnavailable("Unsupported Kimi message patch")
        elif kind == "block":
            identity, parent = value.get("id"), value.get("parentId", "")
            if (assistant is None or completed or not isinstance(identity, str) or not identity
                    or value.get("messageId", "") not in ("", assistant)
                    or (parent and parent not in blocks)):
                raise ProviderUnavailable("Kimi block is outside the active assistant turn")
            if identity in blocks and blocks[identity][0] != parent:
                raise ProviderUnavailable("Kimi changed a block's parent")
            field = mask.removeprefix("block.") if mask.startswith("block.") else ""
            if field in ("multiStage", "stage", "think", "text") and op == "set":
                if not isinstance(value.get(field), dict):
                    raise ProviderUnavailable("Invalid Kimi block content")
                if identity in texts:
                    raise ProviderUnavailable("Kimi replaced an existing answer block")
                if identity in blocks and blocks[identity][1] != field:
                    raise ProviderUnavailable("Kimi changed a block's content type")
                blocks[identity] = (parent, field)
                if field == "text":
                    if parent:
                        raise ProviderUnavailable("Kimi answer text is not a root block")
                    text = value[field].get("content")
                    if not isinstance(text, str):
                        raise ProviderUnavailable("Invalid Kimi answer text")
                    texts[identity] = text
            elif field in ("text.content", "think.content") and op == "append":
                content = value.get(field.split(".")[0])
                if (identity not in blocks or blocks[identity][1] != field.split(".")[0]
                        or not isinstance(content, dict) or not isinstance(content.get("content"), str)):
                    raise ProviderUnavailable("Kimi appended to an unknown block")
                if field == "text.content":
                    if identity not in texts:
                        raise ProviderUnavailable("Kimi converted a non-answer block into text")
                    texts[identity] += content["content"]
                elif identity in texts:
                    raise ProviderUnavailable("Kimi mixed thinking and answer blocks")
            else:
                raise ProviderUnavailable("Unsupported Kimi block patch")
        elif kind in ("ref", "usage"):
            continue  # Metadata never becomes executable answer text.
        elif kind == "done":
            if not completed:
                raise ProviderUnavailable("Kimi ended before assistant completion")
            done = True
        else:
            raise ProviderUnavailable("Kimi returned an unsupported notification")
    text = "".join(texts.values())
    if not done or not completed or not text.strip():
        raise ProviderUnavailable("Kimi returned no completed assistant text")
    return text
