"""Pydantic models for the OpenAI-compatible request/response shapes we support."""

from __future__ import annotations

from typing import List, Optional, Union

from pydantic import BaseModel, field_validator

from .config import DEFAULT_MODEL


class ChatMessage(BaseModel):
    role: str
    # content is a plain string, or a list of parts (OpenAI vision-style). We only
    # read text parts; non-text parts are ignored.
    content: Union[str, List[dict], None] = None
    # Tool-calling fields. The web protocols have no OpenAI tool channel, so these
    # are accepted for OpenAI compatibility and emulated via prompt injection
    # (see server.openai_format).
    tool_calls: Optional[List[dict]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

    @field_validator("content")
    @classmethod
    def validate_text_parts(cls, value):
        if isinstance(value, list):
            for part in value:
                if part.get("type") == "text" and not isinstance(part.get("text", ""), str):
                    raise ValueError("Text content parts must contain a string")
        return value

    @field_validator("tool_calls")
    @classmethod
    def validate_historical_calls(cls, value):
        for call in value or []:
            if "function" in call and not isinstance(call["function"], dict):
                raise ValueError("Historical tool call function must be an object")
        return value


class ChatCompletionRequest(BaseModel):
    model: str = DEFAULT_MODEL
    messages: List[ChatMessage]
    stream: bool = False
    # Pass a conversation_id from a previous response to resume that thread.
    conversation_id: Optional[str] = None
    # Tools to enable for this request, independent of the model. OpenAI clients
    # pass these via extra_body: `thinking` (DeepThink), `search` (web).
    thinking: bool = False
    search: bool = False
    # Function/tool calling (OpenAI format). These web chats have no OpenAI
    # tool channel, so we emulate it by injecting the tool specs into the prompt
    # and parsing the model's reply for a tool-call block.
    tools: Optional[List[dict]] = None
    tool_choice: Optional[Union[str, dict]] = None
    # Accepted for compatibility but not all are forwarded to the provider.
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    user: Optional[str] = None
