"""Factories for optional providers; authentication is an explicit local step."""

from .antigravity import AntigravityClient
from .tab_bridge import TabClient


def build_client(provider, check_cancelled):
    if provider == "gemini":
        return AntigravityClient(check_cancelled)
    return TabClient(provider, check_cancelled)
