"""Provider-scoped conversation tokens, validated before opening any session."""

import base64
import json
import re
from uuid import UUID

PROVIDERS = frozenset(("gemini", "grok", "mistral", "kimi", "glm"))
_MODEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")
_PATHS = {
    "grok": re.compile(r"/c/[a-zA-Z0-9_-]{1,128}\Z"),
    "mistral": re.compile(r"/(?:chat|work)/[a-zA-Z0-9_-]{1,128}\Z"),
    "kimi": re.compile(r"/chat/[a-zA-Z0-9_-]{1,128}\Z"),
    "glm": re.compile(r"/c/[a-zA-Z0-9_-]{1,128}\Z"),
}


def validate_model(model):
    if not isinstance(model, str) or not _MODEL.fullmatch(model):
        raise ValueError("Invalid provider conversation model")


def validate_reference(provider, model, reference):
    if provider not in PROVIDERS:
        raise ValueError("Invalid conversation provider")
    validate_model(model)
    if not isinstance(reference, str):
        raise ValueError("Invalid provider conversation reference")
    if provider == "gemini":
        try:
            if str(UUID(reference)) != reference:
                raise ValueError()
        except ValueError:
            raise ValueError("Invalid Gemini conversation reference") from None
    elif not _PATHS[provider].fullmatch(reference):
        raise ValueError("Invalid browser conversation path")


def encode(provider, model, reference):
    validate_reference(provider, model, reference)
    data = json.dumps([model, reference], separators=(",", ":")).encode()
    return "web:" + provider + ":" + base64.urlsafe_b64encode(data).decode().rstrip("=")


def decode(value, provider):
    if not isinstance(value, str) or len(value) > 1024 or not value.startswith("web:" + provider + ":"):
        raise ValueError("The conversation_id belongs to a different provider or is invalid")
    try:
        encoded = value.split(":", 2)[2]
        data = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        payload = json.loads(data)
        if not isinstance(payload, list) or len(payload) != 2:
            raise ValueError()
        model, reference = payload
        validate_reference(provider, model, reference)
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("Invalid provider conversation_id") from None
    return model, reference
