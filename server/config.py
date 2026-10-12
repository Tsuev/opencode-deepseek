"""Server configuration: the OpenAI-facing model names and what they map to."""

import os
import json
import re

from settings import load_environment
from providers.kimi_http import direct_enabled
from providers.kimi_models import PUBLIC_MODELS as KIMI_MODELS

load_environment()

def _enabled(name, default="0"):
    return os.getenv(name, default).lower() not in ("", "0", "false", "no", "off")


def _transport(name, default, allowed):
    value = os.getenv(name, default).strip().lower() or default
    if value not in allowed:
        raise ValueError(f"{name} must be {' or '.join(allowed)}")
    return value


def _tool_instructions_mode(name, default):
    value = os.getenv(name, default).strip().lower() or default
    if value not in ("full", "light", "none"):
        raise ValueError(f"{name} must be full, light, or none")
    return value


# How OpenAI `tools` are flattened into provider prompts.
# full  — complete schemas + rules on every turn.
# light — full block on the first provider-chat turn; short reminder afterward
#         (recommended for browser tabs).
# none  — like light on the first turn; no tools block on continuations.
# HTTP (DeepSeek / Qwen API / Kimi direct / Gemini CLI) defaults to full.
# Browser tab bridge defaults to light (smaller follow-up prompts).
TOOL_INSTRUCTIONS_HTTP = _tool_instructions_mode("TOOL_INSTRUCTIONS_HTTP", "full")
TOOL_INSTRUCTIONS_BROWSER = _tool_instructions_mode("TOOL_INSTRUCTIONS_BROWSER", "light")

# Requests per minute allowed per client IP (override with RATE_LIMIT_PER_MINUTE).
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))

# Deprecated compatibility settings. The server ignores interactive/automatic
# browser capture, including old .env opt-ins. Use the provider auth CLI manually.
SERVER_INTERACTIVE_LOGIN = False
SESSION_REFRESH_ENABLED = False
SESSION_REFRESH_INTERVAL = 18000
REFRESH_BROWSER_CHANNEL = os.getenv("REFRESH_BROWSER_CHANNEL", "chromium-headless-shell")
REFRESH_BROWSER_CHANNEL_FALLBACK = os.getenv("REFRESH_BROWSER_CHANNEL_FALLBACK", "chrome")

# Public model ids the server advertises (via /v1/models) and accepts, mapped to
# DeepSeek's `model_type` wire value. This is the MODEL axis ONLY — it picks
# which model answers. DeepThink and web Search are orthogonal tools requested
# per call via the `thinking` and `search` booleans, never encoded in the model name.
#
# "vision" is deferred: it only does anything with an image attached, which needs
# ref_file_ids / file-upload plumbing we don't have yet.
DEEPSEEK_MODEL_MAP = {
    "deepseek-chat":   "default",   # Instant — the fast default model
    "deepseek-expert": "expert",    # Expert  — the stronger, slower model
}
DEEPSEEK_ENABLED = _enabled("DEEPSEEK_ENABLED")
MODEL_MAP = dict(DEEPSEEK_MODEL_MAP) if DEEPSEEK_ENABLED else {}

# Dual-mode providers. `browser` uses the signed-in tab and advertises
# `<provider>-web` (the model is selected in the site UI). The other value
# keeps that provider's own catalog: Qwen HTTP (`python -m qwen.auth`) or
# Gemini's Antigravity CLI (`GEMINI_MODELS` public-id → `agy` slug).
_DUAL_TRANSPORT = {
    "qwen": ("http", ("http", "browser")),
    "gemini": ("cli", ("cli", "browser")),
}
_TRANSPORT = {
    provider: _transport(provider.upper() + "_TRANSPORT", default, allowed)
    for provider, (default, allowed) in _DUAL_TRANSPORT.items()
}
QWEN_TRANSPORT = _TRANSPORT["qwen"]
GEMINI_TRANSPORT = _TRANSPORT["gemini"]

# Qwen Chat is opt-in and uses a separate account/profile from DeepSeek.
# HTTP catalog: public OpenAI id → website wire model. Override with QWEN_MODELS.
_DEFAULT_QWEN_MODELS = (
    '{"qwen3.8-omni-flash":"qwen3.8-omni-flash","qwen3.8-max":"qwen3.8-max",'
    '"qwen3.7-plus":"qwen3.7-plus"}'
)


def _load_qwen_models():
    models = json.loads(os.getenv("QWEN_MODELS", _DEFAULT_QWEN_MODELS))
    if not isinstance(models, dict) or len(models) > 50:
        raise ValueError("QWEN_MODELS must be a JSON object with at most 50 models")
    for name, slug in models.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", name)
                or not isinstance(slug, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", slug)):
            raise ValueError("Invalid QWEN_MODELS model name or wire id")
    return models


QWEN_MODEL_MAP = _load_qwen_models()
QWEN_ENABLED = _enabled("QWEN_ENABLED")
GEMINI_ENABLED = _enabled("GEMINI_ENABLED")
# Separate opt-in: one normal cookie renewal only before a new user request,
# under the same durable guard. Never renew in response to an upstream refusal.
QWEN_RENEW_SESSION = _enabled("QWEN_RENEW_SESSION")


def _dual_uses_browser(provider, enabled):
    return enabled and _TRANSPORT[provider] == "browser"


def qwen_uses_browser():
    return _dual_uses_browser("qwen", QWEN_ENABLED)


def gemini_uses_browser():
    return _dual_uses_browser("gemini", GEMINI_ENABLED)


def _advertise_web(provider):
    name = provider + "-web"
    MODEL_MAP[name] = "default"
    OPTIONAL_MODEL_PROVIDERS[name] = provider


OPTIONAL_MODEL_PROVIDERS = {}
if QWEN_ENABLED:
    if qwen_uses_browser():
        _advertise_web("qwen")
    else:
        MODEL_MAP.update(QWEN_MODEL_MAP)

# Grok / Mistral / GLM / ChatGPT / Claude / Kimi: opt-in browser tabs (or Kimi direct Instant).
for provider in ("grok", "mistral", "kimi", "glm", "chatgpt", "claude"):
    if not _enabled(provider.upper() + "_ENABLED"):
        continue
    if provider == "kimi" and direct_enabled():
        MODEL_MAP.update(KIMI_MODELS)
        OPTIONAL_MODEL_PROVIDERS.update({key: provider for key in KIMI_MODELS})
        continue
    _advertise_web(provider)

if GEMINI_ENABLED:
    if gemini_uses_browser():
        _advertise_web("gemini")
    else:
        # Model availability differs by Google account and region. Require slugs
        # from `agy models`, rather than advertising an invented substitute.
        google_models = json.loads(os.getenv("GEMINI_MODELS", "{}"))
        if not isinstance(google_models, dict) or len(google_models) > 50:
            raise ValueError("GEMINI_MODELS must be a JSON object with at most 50 models")
        for name, slug in google_models.items():
            if (not isinstance(name, str) or not re.fullmatch(r"gemini-[a-zA-Z0-9._-]{1,100}", name)
                    or not isinstance(slug, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", slug)):
                raise ValueError("Invalid GEMINI_MODELS model name or CLI slug")
            MODEL_MAP[name] = slug
            OPTIONAL_MODEL_PROVIDERS[name] = "gemini"


def _bare_owner(name: str) -> str:
    return OPTIONAL_MODEL_PROVIDERS.get(name, "qwen" if name in QWEN_MODEL_MAP else "deepseek")


def provider_uses_browser(provider: str) -> bool:
    """Whether completions for this provider go through the signed-in tab bridge."""
    if provider == "deepseek":
        return False
    if provider == "qwen":
        return qwen_uses_browser()
    if provider == "gemini":
        return gemini_uses_browser()
    if provider == "kimi":
        return not direct_enabled()
    return provider in ("grok", "mistral", "glm", "chatgpt", "claude")


def tool_instructions_for_provider(provider: str) -> str:
    return TOOL_INSTRUCTIONS_BROWSER if provider_uses_browser(provider) else TOOL_INSTRUCTIONS_HTTP


def _default_qwen_http_model():
    if "qwen3.8-omni-flash" in QWEN_MODEL_MAP:
        return "qwen3.8-omni-flash"
    return next(iter(QWEN_MODEL_MAP), "qwen3.8-omni-flash")


DEFAULT_MODEL = "qwen-web" if qwen_uses_browser() else _default_qwen_http_model()


def canonical_model(name: str):
    """Bare public id for a known model, or None.

    Accepts both `qwen3.8-omni-flash` and `qwen/qwen3.8-omni-flash` when the
    author segment matches the model's provider.
    """
    if name in MODEL_MAP:
        return name
    if "/" not in name:
        return None
    owner, _, bare = name.partition("/")
    if "/" in bare or bare not in MODEL_MAP or _bare_owner(bare) != owner:
        return None
    return bare


def model_provider(name: str) -> str:
    bare = canonical_model(name)
    return _bare_owner(bare if bare is not None else name)


def is_known_model(name: str) -> bool:
    """Whether `name` is a model id we accept (used to 404 unknown models)."""
    return canonical_model(name) is not None


def resolve_model_type(name: str) -> str:
    """Translate a public model id to the provider's wire model value.

    Caller must check `is_known_model` first; this raises KeyError otherwise.
    """
    bare = canonical_model(name)
    if bare is None:
        raise KeyError(name)
    return MODEL_MAP[bare]


def advertised_model_ids():
    """Bare ids plus matching `owner/id` aliases for OpenAI-compatible clients."""
    for name in MODEL_MAP:
        yield name
        yield f"{_bare_owner(name)}/{name}"
