"""Capture Qwen Chat's short-lived access token in a dedicated browser profile."""

from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Optional
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from deepseek.auth import LoginRequired as DeepSeekLoginRequired, Session as BrowserSession
from settings import ROOT

CHAT_URL = "https://chat.qwen.ai/"
AUTH_URL = "https://auth.qwen.ai/"
DEFAULT_PROFILE_DIR = Path(os.getenv("QWEN_PROFILE_DIR", ROOT / "session" / "qwen" / "profile"))
DEFAULT_SESSION_FILE = ROOT / "session" / "qwen" / "session.json"
_PROFILE_LOCK = threading.RLock()


class LoginRequired(DeepSeekLoginRequired):
    def __init__(self, message: str = "No valid Qwen Chat session. Run python -m qwen.auth to sign in."):
        super().__init__(message)


@dataclass
class Session(BrowserSession):
    COOKIE_DOMAINS: ClassVar[tuple[str, ...]] = ("qwen.ai", "chat.qwen.ai", "auth.qwen.ai")
    expires_at: float = 0

    @property
    def usable(self) -> bool:
        return (isinstance(self.token, str) and bool(self.token.strip())
                and isinstance(self.expires_at, (int, float)) and math.isfinite(self.expires_at)
                and self.expires_at > time.time() + 60)

    def save(self, path: Path = DEFAULT_SESSION_FILE) -> None:
        super().save(path)

    @classmethod
    def load(cls, path: Path = DEFAULT_SESSION_FILE) -> Optional["Session"]:
        return super().load(path)


_READ_SESSION_JS = """() => {
    if (location.origin !== 'https://chat.qwen.ai') return null;
    try {
        const state = JSON.parse(localStorage.getItem('qwen_access_token_state') || 'null');
        if (state && state.version === 1 && typeof state.token === 'string' && state.token.trim())
            return {token: state.token, expires_at: state.expiresAt / 1000};
        const token = localStorage.getItem('token');
        const expires = Number(localStorage.getItem('at_expire_time'));
        return token && token.trim() && expires > 0 ? {token, expires_at: expires / 1000} : null;
    } catch (_) { return null; }
}"""


def _capture(context, page) -> Optional[Session]:
    # Never inspect storage on an OAuth provider or a redirected third-party page.
    if urlparse(page.url).hostname != "chat.qwen.ai":
        return None
    try:
        data = page.evaluate(_READ_SESSION_JS)
        if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"].strip():
            return None
        expires = data.get("expires_at")
        if not isinstance(expires, (int, float)) or not math.isfinite(expires) or expires <= time.time() + 60:
            return None
        cookies = [c for c in context.cookies([CHAT_URL, AUTH_URL])
                   if c.get("domain", "").lower().lstrip(".") in Session.COOKIE_DOMAINS]
        return Session(data["token"], cookies, page.evaluate("() => navigator.userAgent"),
                       time.time(), expires)
    except PlaywrightError as exc:
        if "Execution context was destroyed" in str(exc) or "navigation" in str(exc).lower():
            return None
        raise


def _launch(p, profile_dir: Path, headless: bool, channel: Optional[str]):
    profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile_dir.chmod(0o700)
    options = {"headless": headless}
    if channel:
        options["channel"] = channel
    try:
        return p.chromium.launch_persistent_context(str(profile_dir), **options)
    except PlaywrightError:
        if not channel:
            raise
        options.pop("channel")
        return p.chromium.launch_persistent_context(str(profile_dir), **options)


def _capture_profile(profile_dir: Path, headless: bool, channel: Optional[str], timeout: float):
    with sync_playwright() as p:
        context = _launch(p, profile_dir, headless, channel)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(CHAT_URL if headless else CHAT_URL + "auth", wait_until="commit", timeout=60000)
            except PlaywrightError:
                if page.is_closed():
                    raise
            if not headless:
                print("[qwen-auth] Sign in to Qwen Chat in this window. Waiting for the session...", flush=True)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                session = _capture(context, page)
                if session:
                    return session
                page.wait_for_timeout(1000)
            return None
        finally:
            context.close()


def login(profile_dir: Path = DEFAULT_PROFILE_DIR, session_file: Path = DEFAULT_SESSION_FILE,
          channel: Optional[str] = "chrome") -> Session:
    with _PROFILE_LOCK:
        session = _capture_profile(profile_dir, False, channel, 1800)
        if session is None or not session.usable:
            raise LoginRequired("Qwen Chat sign-in timed out without a valid session.")
        session.save(session_file)
        return session


def get_session(profile_dir: Path = DEFAULT_PROFILE_DIR, session_file: Path = DEFAULT_SESSION_FILE,
                allow_interactive: bool = False, force: bool = False,
                channel: Optional[str] = "chromium-headless-shell",
                fallback_channel: Optional[str] = "chrome") -> Session:
    with _PROFILE_LOCK:
        cached = Session.load(session_file)
        if not force and cached and cached.usable:
            return cached
        channels = list(dict.fromkeys([channel] + ([fallback_channel] if fallback_channel else [])))
        for browser_channel in channels:
            try:
                session = _capture_profile(profile_dir, True, browser_channel, 30)
            except (PlaywrightError, OSError, RuntimeError):
                continue
            if session and session.usable:
                session.save(session_file)
                return session
        if allow_interactive:
            return login(profile_dir, session_file)
        raise LoginRequired()


if __name__ == "__main__":
    captured = login()
    print(f"[qwen-auth] Session saved to {DEFAULT_SESSION_FILE} ({len(captured.cookies)} cookies).")
