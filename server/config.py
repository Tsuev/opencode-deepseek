"""Server configuration: the OpenAI-facing model names and what they map to."""

import os

# Requests per minute allowed per client IP (override with RATE_LIMIT_PER_MINUTE).
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))

# When the server has no session, should it pop a visible browser window for
# interactive sign-in (the first request then blocks until you finish logging
# in)? On by default for local single-user use. Set to "0"/"false" for headless
# deployments, where it instead returns a 503 telling the caller to run
# `python -m deepseek.auth`.
SERVER_INTERACTIVE_LOGIN = os.getenv("SERVER_INTERACTIVE_LOGIN", "1").lower() not in (
    "0", "false", "no", "off",
)

# Background session refresher: periodically re-captures the token from the
# persistent browser profile so a request never hits an expired token. Runs in a
# daemon thread and never opens a visible window (allow_interactive=False).
SESSION_REFRESH_ENABLED = os.getenv("SESSION_REFRESH_ENABLED", "1").lower() not in (
    "0", "false", "no", "off",
)
# Refresh well before SESSION_MAX_AGE (6h in deepseek.auth) so the cached token
# stays fresh. Default 5h.
SESSION_REFRESH_INTERVAL = int(os.getenv("SESSION_REFRESH_INTERVAL", str(5 * 60 * 60)))

# Playwright browser channel for the (background) headless refresh. The light
# bundled "chromium-headless-shell" uses far less RAM than full Chrome, but may
# not decrypt cookies written by Chrome's Keychain-bound profile — so callers
# fall back to "chrome" when a headless capture comes back empty.
REFRESH_BROWSER_CHANNEL = os.getenv("REFRESH_BROWSER_CHANNEL", "chromium-headless-shell")

# Fallback channel used when REFRESH_BROWSER_CHANNEL yields no session. Empty
# string disables the fallback.
REFRESH_BROWSER_CHANNEL_FALLBACK = os.getenv("REFRESH_BROWSER_CHANNEL_FALLBACK", "chrome")

# Public model ids the server advertises (via /v1/models) and accepts, mapped to
# DeepSeek's `model_type` wire value. This is the MODEL axis ONLY — it picks
# which model answers. DeepThink and web Search are orthogonal tools requested
# per call via `tool_names` (see deepseek.client.KNOWN_TOOLS), never encoded in
# the model name.
#
# "vision" is deferred: it only does anything with an image attached, which needs
# ref_file_ids / file-upload plumbing we don't have yet.
MODEL_MAP = {
    "deepseek-chat":   "default",   # Instant — the fast default model
    "deepseek-expert": "expert",    # Expert  — the stronger, slower model
}

DEFAULT_MODEL = "deepseek-chat"


def is_known_model(name: str) -> bool:
    """Whether `name` is a model id we accept (used to 404 unknown models)."""
    return name in MODEL_MAP


def resolve_model_type(name: str) -> str:
    """Translate a public model id to DeepSeek's `model_type` wire value.

    Caller must check `is_known_model` first; this raises KeyError otherwise.
    """
    return MODEL_MAP[name]
