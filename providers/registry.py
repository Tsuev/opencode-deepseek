"""Factories for optional providers; authentication is an explicit local step."""

from .antigravity import AntigravityClient
from .tab_bridge import TabClient
from .kimi_http import KimiClient, direct_enabled


def build_client(provider, check_cancelled):
    if provider == "gemini":
        # Local import: server.config loads the full catalog at import time.
        from server.config import gemini_uses_browser
        if gemini_uses_browser():
            return TabClient(provider, check_cancelled)
        return AntigravityClient(check_cancelled)
    if provider == 'kimi' and direct_enabled():
        return KimiClient(check_cancelled)
    return TabClient(provider, check_cancelled)
