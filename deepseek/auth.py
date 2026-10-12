"""
Authentication — Playwright login + session capture.

Mirrors the Windows-Copilot-API design: the browser is used ONLY to establish a
signed-in session (handling the AWS WAF / "verify you're human" check and the
email/password form). It does not chat. We then capture the bearer token from
`localStorage.userToken` plus the session cookies, and hand them to the
pure-HTTP client in `deepseek.client`.

A persistent Chromium profile can retain manual sign-in. Session loading uses
the local cache only by default; authentication cannot clear an account pause.

    from deepseek.auth import get_session
    session = get_session()          # cached only; use python -m deepseek.auth to sign in
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, asdict
from functools import wraps
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import ClassVar, Optional
from urllib.parse import urlsplit

from settings import ROOT

# Default Playwright browser channel. "chrome" uses the installed Google Chrome
# (needed to read a profile it created, e.g. cookies encrypted with Chrome's
# Keychain key). Pass channel=None to use Playwright's bundled Chromium, or
# "chromium-headless-shell" for the light headless shell.
DEFAULT_CHANNEL: Optional[str] = "chrome"

from playwright.sync_api import Error as PlaywrightError, sync_playwright

# Override with DEEPSEEK_PROFILE_DIR to reuse an existing signed-in Chrome profile.
DEFAULT_PROFILE_DIR = Path(os.getenv("DEEPSEEK_PROFILE_DIR", ROOT / "session" / "profile"))
DEFAULT_SESSION_FILE = ROOT / "session" / "session.json"

CHAT_URL = "https://chat.deepseek.com/"
SIGNIN_URL = "https://chat.deepseek.com/sign_in"

LAUNCH_ARGS = []
# No inferred server TTL. Explicit library callers can still request an age bound.
SESSION_MAX_AGE = None
_PROFILE_LOCK = threading.RLock()


def _serialize_profile(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with _PROFILE_LOCK:
            return function(*args, **kwargs)
    return locked


def write_private_json(path: Path, data: dict) -> None:
    """Atomically replace a private state file without exposing a partial write."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, indent=2, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _deepseek_cookie(cookie: dict) -> bool:
    domain = cookie.get("domain", "").lower().lstrip(".")
    return domain in ("deepseek.com", "chat.deepseek.com")


class LoginRequired(RuntimeError):
    """Raised when no usable session exists and interactive login is disallowed
    (e.g. inside the server, where we can't pop open a browser mid-request).
    The message tells the user how to log in."""

    before_dispatch = True

    DEFAULT = (
        "No DeepSeek session found. Log in first by running:\n"
        "    python -m deepseek.auth\n"
        "This opens a browser once so you can sign in and clear the human-check; "
        "afterwards the server reuses the saved session automatically."
    )

    def __init__(self, message: str = DEFAULT):
        super().__init__(message)


@dataclass
class Session:
    """A captured signed-in DeepSeek session."""

    COOKIE_DOMAINS: ClassVar[tuple[str, ...]] = ("deepseek.com", "chat.deepseek.com")
    token: str
    cookies: list[dict]
    user_agent: str
    captured_at: float
    # Set only by an explicit same-account local session transfer, never by
    # browser capture. Keeps already delivered history bound across rotation.
    continuation_scope: Optional[str] = None
    account_id: Optional[str] = None

    @property
    def age(self) -> float:
        return time.time() - self.captured_at

    def save(self, path: Path = DEFAULT_SESSION_FILE) -> None:
        write_private_json(path, asdict(self))

    def identity(self) -> str:
        value = asdict(self)
        if value.get('continuation_scope') is None:
            value.pop('continuation_scope', None)
        if value.get('account_id') is None:
            value.pop('account_id', None)
        return hashlib.sha256(json.dumps(value, sort_keys=True,
                                        separators=(',', ':'), allow_nan=False).encode()).hexdigest()

    def preserve_history_from(self, previous: "Session", *, same_account_confirmed=False):
        """Explicit local transfer only; caller must verify the same account.

        Does not save credentials, clear provider pauses, or unconsume history.
        Browser capture must never call this automatically.
        """
        if same_account_confirmed is not True:
            raise ValueError('Same-account confirmation required for history transfer')
        self.continuation_scope = previous.continuation_scope or previous.identity()

    def cookie_jar(self) -> CookieJar:
        """Keep domain, path, expiry and secure restrictions in HTTP requests."""
        if not isinstance(self.cookies, list):
            raise LoginRequired("Legacy session cookies have no domain information. "
                                "Run python -m deepseek.auth to capture a new session.")
        jar = CookieJar()
        for cookie in self.cookies:
            if cookie.get("domain", "").lower().lstrip(".") not in self.COOKIE_DOMAINS:
                continue
            domain = cookie["domain"]
            expires = cookie.get("expires", -1)
            expires = int(expires) if expires > 0 else None
            jar.set_cookie(Cookie(
                version=0, name=cookie["name"], value=cookie["value"],
                port=None, port_specified=False,
                domain=domain, domain_specified=domain.startswith("."),
                domain_initial_dot=domain.startswith("."),
                path=cookie.get("path", "/"), path_specified=True,
                secure=bool(cookie.get("secure", False)), expires=expires,
                discard=expires is None, comment=None, comment_url=None,
                rest={"HttpOnly": None} if cookie.get("httpOnly") else {},
            ))
        return jar

    @classmethod
    def load(cls, path: Path = DEFAULT_SESSION_FILE) -> Optional["Session"]:
        if not path.exists():
            return None
        try:
            path.parent.chmod(0o700)
            path.chmod(0o600)
            data = json.loads(path.read_text(encoding="utf-8"))
            # Old caches lost domain/path information; recapture instead of
            # forwarding potentially unrelated cookies from those caches.
            if not isinstance(data.get("cookies"), list):
                return None
            session = cls(**data)
            if session.account_id is not None and (not isinstance(session.account_id, str)
                    or not 1 <= len(session.account_id) <= 128
                    or any(ord(c) < 33 or ord(c) > 126 for c in session.account_id)):
                return None
            if session.continuation_scope is not None and (
                not isinstance(session.continuation_scope, str)
                or len(session.continuation_scope) != 64
                or any(c not in '0123456789abcdef' for c in session.continuation_scope)
            ):
                return None
            if not isinstance(session.token, str) or not session.token.strip() or any(c in session.token for c in "\r\n"):
                return None
            session.cookie_jar()
            return session
        except Exception:
            return None


# --- in-page helpers --------------------------------------------------------

# Reads the bearer token the web app stores after login. Shape:
#   localStorage.userToken = {"value":"<TOKEN>","__version":"0"}
_READ_TOKEN_JS = """
() => {
  if (window.location.origin !== 'https://chat.deepseek.com') return null;
  try {
    const raw = window.localStorage.getItem('userToken');
    if (!raw) return null;
    const o = JSON.parse(raw);
    return (o && typeof o.value === 'string' && o.value.trim()) ? o.value : null;
  } catch (e) { return null; }
}
"""


# Same field the Safari transfer reads; only used to bind history to an account.
_READ_ACCOUNT_JS = """
() => {
  if (window.location.origin !== 'https://chat.deepseek.com') return null;
  try {
    const id = JSON.parse(window.localStorage.getItem('__appKit_userInfo'))?.value?.id;
    return id === undefined || id === null ? null : String(id);
  } catch (e) { return null; }
}
"""


def _valid_account(value) -> Optional[str]:
    if (isinstance(value, str) and 1 <= len(value) <= 128
            and all(33 <= ord(c) <= 126 for c in value)):
        return value
    return None


class AccountBindingError(RuntimeError):
    """A capture would silently detach saved history from its verified account."""


def bind_capture(fresh: "Session", session_file: Path = DEFAULT_SESSION_FILE, *,
                 same_account=False, replace_account=False) -> "Session":
    """Carry the history pin only to a verified same-account capture.

    A previous Safari transfer pins continuation_scope and account_id. A new
    browser capture must neither drop that pin silently nor move it to another
    account. Never clears provider pauses or unconsumes history.
    """
    previous = Session.load(Path(session_file))
    if previous is None or replace_account:
        return fresh
    if previous.account_id is not None:
        if fresh.account_id is None:
            raise AccountBindingError(
                "Saved history is bound to a verified DeepSeek account, but this capture has no "
                "account id. Session not saved; use /deepseek-login (Safari) or --replace-account.")
        if fresh.account_id != previous.account_id:
            raise AccountBindingError(
                "This is a different DeepSeek account than the saved history. Session not saved; "
                "pass --replace-account only to deliberately start unrelated history.")
        fresh.preserve_history_from(previous, same_account_confirmed=True)
    elif same_account:
        fresh.preserve_history_from(previous, same_account_confirmed=True)
    else:
        # The previous session (pinned or not) scopes saved histories, and its
        # account is unknown. Never overwrite it silently.
        raise AccountBindingError(
            "The saved session has no verified account; overwriting it would detach saved "
            "Harness histories. Session not saved; rerun with --same-account (same account) "
            "or --replace-account (deliberately start unrelated history).")
    return fresh


def _safe_evaluate(page, js: str):
    """Run page.evaluate, swallowing the benign 'Execution context was destroyed'
    error that fires when a navigation (e.g. the post-login redirect) happens to
    land mid-evaluate. Returns None on any such transient failure instead of
    raising, so callers can just retry on the next poll."""
    try:
        return page.evaluate(js)
    except Exception as e:
        msg = str(e)
        if "Execution context was destroyed" in msg or "navigation" in msg.lower():
            return None
        raise


def _capture_from_context(context, page) -> Optional[Session]:
    """Read token + cookies + UA off a logged-in page, or None if not signed in."""
    try:
        url = urlsplit(page.url)
        if (url.scheme != 'https' or url.hostname != 'chat.deepseek.com'
                or url.port not in (None, 443) or url.username is not None or url.password is not None):
            return None
    except (ValueError, TypeError):
        return None
    token = _safe_evaluate(page, _READ_TOKEN_JS)
    if not isinstance(token, str) or not token.strip():
        return None
    cookies = [c for c in context.cookies([CHAT_URL]) if _deepseek_cookie(c)]
    ua = _safe_evaluate(page, "() => navigator.userAgent") or ""
    account = _valid_account(_safe_evaluate(page, _READ_ACCOUNT_JS))
    return Session(token=token, cookies=cookies, user_agent=ua, captured_at=time.time(),
                   account_id=account)


def _wait_for_token(page, timeout: float) -> Optional[str]:
    """Poll localStorage.userToken until it appears or we time out. Tolerates
    transient navigations (e.g. the Google OAuth redirect chain) that briefly
    destroy the page's JS execution context — those just count as "no token
    yet" rather than aborting the whole login."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        token = _safe_evaluate(page, _READ_TOKEN_JS)
        if token:
            return token
        page.wait_for_timeout(1000)
    return None


def _launch_context(p, profile_dir: Path, headless: bool, channel: Optional[str],
                    allow_fallback: bool = True):
    """Launch a persistent context for `profile_dir`, preferring `channel`.

    `channel="chrome"` reuses the installed Google Chrome so it can read the
    cookies it wrote (Chrome encrypts them with a Keychain key). A None channel
    uses Playwright's bundled Chromium. If the requested channel isn't available,
    fall back to the bundled browser rather than failing outright.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.chmod(0o700)
    kwargs: dict = {"headless": headless, "args": LAUNCH_ARGS}
    if channel:
        kwargs["channel"] = channel
    try:
        return p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
    except PlaywrightError:
        if not channel or not allow_fallback:
            raise
        kwargs.pop("channel", None)
        return p.chromium.launch_persistent_context(str(profile_dir), **kwargs)


def _safe_goto(page, url: str) -> None:
    """Navigate, tolerating the benign `net::ERR_ABORTED` that DeepSeek's SPA
    redirects and the AWS WAF check often raise mid-navigation. We wait only for
    the initial commit, not full `load`; the token-poll afterwards is what
    actually gates sign-in, so an aborted/partial load here is fine."""
    try:
        page.goto(url, wait_until="commit", timeout=60000)
    except Exception as e:
        print(f"[auth] navigation to {url} was interrupted ({type(e).__name__}); "
              "continuing — finish signing in in the window if needed.")
    # Give the SPA a moment to render its login UI before we touch the form.
    page.wait_for_timeout(2000)


@_serialize_profile
def login(
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    headless: bool = False,
    assume_logged_out: bool = False,
    channel: Optional[str] = DEFAULT_CHANNEL,
    session_file: Path = DEFAULT_SESSION_FILE,
    same_account: bool = False,
    replace_account: bool = False,
) -> Session:
    """Interactive login. Opens a visible window and waits for you to sign in by
    hand (and clear the AWS WAF human-check); once a token appears it captures
    and saves the session. Later `get_session()` calls read this private cache;
    they do not open a browser or refresh it by default.

    Navigate once, then let the user and website complete sign-in. A missing
    token can mean a pending human-check, not a reason to interrupt it with
    another navigation. `assume_logged_out=True` selects SIGNIN_URL as the
    initial destination instead of CHAT_URL.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = _launch_context(p, profile_dir, headless, channel)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            _safe_goto(page, SIGNIN_URL if assume_logged_out else CHAT_URL)
            existing = _safe_evaluate(page, _READ_TOKEN_JS)

            if not existing:
                print("[auth] Please sign in in this window (use the website's sign-in "
                      "button if needed). Complete any human-check manually; this "
                      "helper will not navigate again. Waiting for the session...")
                if not _wait_for_token(page, timeout=300):
                    raise RuntimeError("Login timed out — no token captured.")

            session = _capture_from_context(context, page)
        finally:
            context.close()
        if session is None:
            raise RuntimeError("Logged in but could not read the token.")
        bind_capture(session, session_file, same_account=same_account,
                     replace_account=replace_account).save(session_file)
        return session


@_serialize_profile
def _headless_refresh(
    profile_dir: Path, channel: Optional[str] = DEFAULT_CHANNEL
) -> Optional[Session]:
    """Try to capture a token headlessly from the persistent profile. Returns a
    Session if the profile is still signed in, else None. Never opens a
    visible window."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = _launch_context(p, profile_dir, True, channel)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            _safe_goto(page, CHAT_URL)
            session = _capture_from_context(context, page)
        finally:
            context.close()

    return session


@_serialize_profile
def get_session(
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    session_file: Path = DEFAULT_SESSION_FILE,
    max_age: Optional[int] = SESSION_MAX_AGE,
    allow_interactive: bool = False,
    channel: Optional[str] = DEFAULT_CHANNEL,
    fallback_channel: Optional[str] = None,
    allow_refresh: bool = False,
) -> Session:
    """Load a saved session without browser access or an inferred expiry.

    Explicit library callers may opt into allow_refresh; the API server never
    does. Use the auth CLI for manual sign-in. Never use sign-in to clear a
    provider pause or retry an account/security restriction.
    """
    cached = Session.load(session_file)
    if cached and (max_age is None or cached.age < max_age):
        return cached

    # Requests must not reopen a website, recapture a session, or fall back to
    # interactive login. The auth CLI is the explicit manual sign-in path.
    if not allow_refresh:
        raise LoginRequired()

    # Try a headless refresh from the (presumably logged-in) persistent profile.
    channels = [channel]
    if fallback_channel and fallback_channel != channel:
        channels.append(fallback_channel)
    last_error = None
    for browser_channel in channels:
        try:
            session = _headless_refresh(profile_dir, browser_channel)
        except (PlaywrightError, OSError, RuntimeError) as exc:
            last_error = exc
            print(f"[auth] headless capture failed ({type(exc).__name__}); "
                  "trying the next configured channel.")
            continue
        if session is not None:
            bind_capture(session, session_file).save(session_file)
            return session

    if not allow_interactive:
        raise LoginRequired() from last_error

    # Not logged in yet — open a visible window so the user can sign in (and
    # clear the human-check) by hand. Explicit refresh callers only. We confirmed
    # (above) there's no token, so go straight to the sign-in page.
    print("[auth] No valid session found — opening a browser window to log in...")
    return login(profile_dir=profile_dir, assume_logged_out=True, channel=channel,
                 session_file=session_file)


@_serialize_profile
def keep_open(profile_dir: Path = DEFAULT_PROFILE_DIR,
              session_file: Path = DEFAULT_SESSION_FILE,
              channel: Optional[str] = DEFAULT_CHANNEL,
              same_account: bool = False, replace_account: bool = False) -> None:
    """Explicit foreground inspection/capture; never used by the API server."""
    with sync_playwright() as p:
        context = _launch_context(p, profile_dir, False, channel, allow_fallback=False)
        page = None
        closed = False

        def on_close(*_):
            nonlocal closed
            closed = True

        context.on('close', on_close)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            print('[auth] Inspect this account in the website window. Ctrl+C or window close stops capture. '
                  'A captured session does not confirm account access or clear a pause.', flush=True)
            _safe_goto(page, CHAT_URL)
            previous = None
            while not closed and not page.is_closed():
                session = _capture_from_context(context, page)
                if session is not None:
                    state = (session.token, session.user_agent,
                             tuple(sorted(json.dumps(cookie, sort_keys=True) for cookie in session.cookies)))
                    if state != previous:
                        # Only the first save may deliberately replace an account.
                        bind_capture(session, session_file, same_account=same_account,
                                     replace_account=replace_account and previous is None).save(session_file)
                        previous = state
                        print('[auth] Session captured. Provider pauses are unchanged.', flush=True)
                page.wait_for_timeout(1000)
        except KeyboardInterrupt:
            print('[auth] Capture stopped.', flush=True)
        except PlaywrightError:
            if not closed and (page is None or not page.is_closed()):
                raise RuntimeError('Browser capture failed; stopped without retry.') from None
        finally:
            try:
                context.close()
            except PlaywrightError:
                pass


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Capture a DeepSeek session in a visible website window.')
    parser.add_argument('--keep-open', action='store_true',
                        help='Inspect the website and capture changed sessions until Ctrl+C or window close.')
    parser.add_argument('--channel', choices=('chrome', 'chromium'), default='chrome',
                        help='Browser for manual sign-in; chromium uses the bundled browser.')
    parser.add_argument('--same-account', action='store_true',
                        help='Confirm this is the account of the saved history (unverified captures only).')
    parser.add_argument('--replace-account', action='store_true',
                        help='Deliberately detach saved history and bind a different account.')
    args = parser.parse_args(argv)
    channel = None if args.channel == 'chromium' else args.channel
    binding = dict(same_account=args.same_account, replace_account=args.replace_account)
    try:
        if args.keep_open:
            keep_open(channel=channel, **binding)
        else:
            session = login(channel=channel, **binding)
            print(f'[auth] Session saved ({len(session.cookies)} cookies). Provider pauses are unchanged.', flush=True)
    except KeyboardInterrupt:
        print('[auth] Capture stopped.', flush=True)
    except AccountBindingError as exc:
        print('[auth] ' + str(exc), flush=True)
        raise SystemExit(1) from None
    except (PlaywrightError, OSError, RuntimeError):
        print('[auth] Sign-in capture failed; stopped without retry.', flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
