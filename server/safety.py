"""Account-safety guards for the DeepSeek web bridge.

The bridge drives a personal DeepSeek *web* account, not the official API.
DeepSeek's anti-automation system can therefore temporarily block an account
when it sees a burst of machine-generated requests. These guards make the
traffic look (and stay) far closer to a human session:

  * ``UpstreamGate``  — serialise upstream generations and space them out with
    a configurable minimum interval plus random jitter;
  * ``BudgetGuard``   — persisted rolling hourly/daily ceilings on new chats
    and generations, so a runaway agent loop stops before DeepSeek does;
  * ``Cooldown``      — latch a long pause as soon as DeepSeek refuses a
    request (403/429), instead of retrying and making things worse.

Everything is deliberately dependency-free and process-local: the server is a
single-user, single-process bridge.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple

import httpx

ROOT = Path(__file__).resolve().parent.parent

# Body/exception fragments that mean "we are being refused for risk reasons",
# not "the session token expired". Kept deliberately broad: a false positive
# only causes a local pause, while a false negative risks a longer ban.
_RISK_HINTS = (
    "risk", "security", "forbidden", "denied", "blocked",
    "abnormal", "automation", "automated", "bot activity", "bot detected",
    "violat", "banned", "suspicious", "unusual", "rate limit", "too many",
)
_AUTH_HINTS = (
    "auth", "unauthorized", "login", "token", "session", "credential",
    "expired", "not signed", "sign in",
)


class BudgetExceeded(RuntimeError):
    """Raised when a local hourly/daily ceiling would be exceeded."""

    def __init__(self, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = retry_after


class CooldownActive(RuntimeError):
    """Raised while the local safety cooldown is active."""

    def __init__(self, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = retry_after


def classify_error(exc: BaseException) -> str:
    """Classify an upstream failure as ``auth``, ``refusal``, ``rate_limit``
    or ``other``. Only ``auth`` is safe to retry with a refreshed session."""
    status: Optional[int] = None
    body = ""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            body = exc.response.text.lower()
        except Exception:
            body = ""
    text = f"{body} {exc}".lower()

    if status == 429:
        return "rate_limit"
    if status == 403:
        if any(h in text for h in _RISK_HINTS):
            return "refusal"
        if any(h in text for h in _AUTH_HINTS):
            return "auth"
        # An unexplained 403 is treated as a refusal: retrying an anti-fraud
        # rejection is exactly what we want to avoid.
        return "refusal"
    if status in (401,):
        return "auth"
    if any(h in text for h in _RISK_HINTS):
        return "refusal"
    if any(h in text for h in _AUTH_HINTS):
        return "auth"
    return "other"


class UpstreamGate:
    """Serialise upstream generations and enforce a minimum spacing.

    The lock is held for the whole generation, so parallel agents queue instead
    of hitting the account concurrently. ``jitter`` randomises the gap so the
    request cadence is not perfectly periodic.
    """

    def __init__(self, min_interval: float, jitter: float):
        self.min_interval = max(0.0, min_interval)
        self.jitter = max(0.0, jitter)
        self._lock = threading.Lock()
        self._last_finished = 0.0
        self._rng = random.Random()

    @contextmanager
    def turn(self) -> Iterator[None]:
        with self._lock:
            now = time.monotonic()
            delay = (
                self._last_finished
                + self.min_interval
                + self._rng.uniform(0.0, self.jitter)
                - now
            )
            if delay > 0:
                time.sleep(delay)
            try:
                yield
            finally:
                self._last_finished = time.monotonic()


def _load_json(path: Path, default: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


class BudgetGuard:
    """Persisted rolling-window ceilings for ``chat`` and ``generation`` events.

    ``limits`` maps an event kind to ``(per_hour, per_day)``. A new chat also
    records a generation, because creating the chat is followed by one.
    """

    def __init__(
        self,
        path: Path,
        limits: Dict[str, Tuple[int, int]],
        max_events: int = 10000,
    ):
        self.path = path
        self.limits = limits
        self.max_events = max_events
        self._lock = threading.Lock()

    def _prune(self, events: list, now: float) -> list:
        cutoff = now - 86400
        if not isinstance(events, list):
            events = []
        events = [e for e in events if isinstance(e, dict)
                  and isinstance(e.get("t"), (int, float))
                  and e["t"] > cutoff]
        return events[-self.max_events:]

    def reserve(self, *, new_chat: bool) -> None:
        """Validate both relevant ceilings and record the events atomically.

        Raises ``BudgetExceeded`` without recording anything when a ceiling is
        already reached.
        """
        now = time.time()
        wanted = ["generation"] + (["chat"] if new_chat else [])
        with self._lock:
            state = _load_json(self.path, {"events": []})
            events = self._prune(state.get("events", []), now)

            for kind in wanted:
                hourly, daily = self.limits[kind]
                in_hour = sum(1 for e in events
                              if e.get("k") == kind and e["t"] > now - 3600)
                in_day = sum(1 for e in events if e.get("k") == kind)
                if in_hour >= hourly:
                    raise BudgetExceeded(
                        f"Local safety budget reached: {in_hour} {kind} "
                        f"requests in the last hour (limit {hourly}). "
                        "This protects the DeepSeek account from being blocked. "
                        "Wait or raise the corresponding DEEPSEEK_MAX_* value.",
                        retry_after=3600.0,
                    )
                if in_day >= daily:
                    raise BudgetExceeded(
                        f"Local safety budget reached: {in_day} {kind} "
                        f"requests in the last 24 hours (limit {daily}). "
                        "This protects the DeepSeek account from being blocked. "
                        "Wait or raise the corresponding DEEPSEEK_MAX_* value.",
                        retry_after=86400.0,
                    )

            events.extend({"t": now, "k": kind} for kind in wanted)
            _save_json(self.path, {"events": self._prune(events, now)})


class Cooldown:
    """Persisted pause latched after DeepSeek refuses a request.

    A restart must not bypass the pause, otherwise a supervisor process could
    immediately start hammering a flagged account again.
    """

    def __init__(self, path: Path, seconds: float):
        self.path = path
        self.seconds = max(0.0, seconds)
        self._lock = threading.Lock()

    def ensure(self) -> None:
        if self.seconds <= 0:
            return
        with self._lock:
            state = _load_json(self.path, {})
        until = state.get("until")
        if isinstance(until, (int, float)) and until > time.time():
            remaining = until - time.time()
            reason = state.get("reason") or "upstream refusal"
            raise CooldownActive(
                "DeepSeek safety cooldown is active "
                f"({reason}). Retry in {int(remaining) + 1}s. The bridge is "
                "paused locally to avoid turning a temporary block into a longer one.",
                retry_after=remaining,
            )

    def latch(self, reason: str) -> None:
        if self.seconds <= 0:
            return
        until = time.time() + self.seconds
        with self._lock:
            _save_json(self.path, {"until": until, "reason": reason})

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
