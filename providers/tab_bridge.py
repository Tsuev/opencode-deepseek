"""Authenticated, loopback-only jobs for an already signed-in browser tab."""

from __future__ import annotations

import base64
import asyncio
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from chat_protocol import Reply
from settings import ROOT
from .browser_protocol import glm_answer, grok_answer, mistral_answer
from .kimi_protocol import kimi_answer
from .common import BufferedStream, ProviderUnavailable, completion_timeout
from .access import provider_attempt, current_attempt, ProviderRejected
from .conversations import decode, encode

BROWSER_PROVIDERS = frozenset(("grok", "mistral", "kimi", "glm"))


def bridge_enabled():
    return os.getenv("BROWSER_BRIDGE_ENABLED", "0").lower() not in ("", "0", "false", "no", "off")


def token_path():
    return ROOT / "session" / "browser-bridge" / "token"


def bridge_token():
    path = token_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    if not path.exists():
        # Publish a complete key atomically. Concurrent first requests must
        # never observe the empty file between creation and the first write.
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(secrets.token_urlsafe(32))
            file.flush()
            os.fsync(file.fileno())
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink()
    path.chmod(0o600)
    value = path.read_text().strip()
    if len(value) != 43:
        raise ProviderUnavailable("Invalid browser bridge pairing file")
    return value


def authorized(client_host, authorization):
    if not bridge_enabled() or client_host not in ("127.0.0.1", "::1"):
        return False
    expected = "Bearer " + bridge_token()
    return isinstance(authorization, str) and hmac.compare_digest(expected, authorization)


@dataclass
class Job:
    id: str
    provider: str
    prompt: str
    path: str | None
    owner: str | None = None
    lease: str | None = None
    result: dict | None = None
    document: str | None = None
    submitted: bool = False
    deadline: float = 0
    check_cancelled: Callable[[], None] | None = field(default=None, repr=False)
    access_attempt: object = field(default=None, repr=False)


class Broker:
    def __init__(self):
        self.condition = threading.Condition()
        self.pending = {}

    def submit(self, provider, prompt, path, check_cancelled, timeout):
        if provider not in BROWSER_PROVIDERS:
            raise ValueError("Unsupported browser provider")
        job = Job(secrets.token_urlsafe(24), provider, prompt, path,
                  deadline=time.monotonic() + timeout, check_cancelled=check_cancelled,
                  access_attempt=current_attempt(provider))
        with self.condition:
            if provider in self.pending:
                raise ProviderUnavailable("The provider already has an active browser job")
            self.pending[provider] = job
            try:
                while job.result is None:
                    check_cancelled()
                    if job.access_attempt is not None:
                        job.access_attempt.check()
                    remaining = job.deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProviderUnavailable(f"{provider} browser tab did not complete the request; connect it and keep it open")
                    self.condition.wait(min(remaining, 0.1))
                check_cancelled()
                return job.result
            finally:
                # Retire before releasing the provider queue: late/replayed
                # browser results cannot satisfy the next completion.
                self.pending.pop(provider, None)

    @staticmethod
    def _live(job):
        if job is None or job.result is not None or time.monotonic() >= job.deadline:
            return False
        try:
            if job.access_attempt is not None:
                job.access_attempt.check()
            if job.check_cancelled is not None:
                job.check_cancelled()
        except (asyncio.CancelledError, ProviderRejected):
            return False
        return True

    def claim(self, provider, owner, document):
        if (provider not in BROWSER_PROVIDERS or not isinstance(owner, str) or not 16 <= len(owner) <= 128
                or not isinstance(document, str) or not 16 <= len(document) <= 128):
            raise ValueError("Invalid browser tab identity")
        with self.condition:
            job = self.pending.get(provider)
            if not self._live(job) or job.owner not in (None, owner) or job.document not in (None, document):
                return None
            if job.owner is None:
                job.owner, job.lease = owner, secrets.token_urlsafe(24)
            job.document = document
            return {"id":job.id, "provider":provider, "prompt":job.prompt,
                    "path":job.path, "lease":job.lease, "submitted":job.submitted,
                    "expires_in":max(0, job.deadline - time.monotonic())}

    def _owned(self, provider, job_id, owner, lease, document):
        job = self.pending.get(provider)
        if (not self._live(job) or job.id != job_id or job.owner != owner or job.document != document
                or not isinstance(lease, str) or not hmac.compare_digest(job.lease or "", lease)):
            raise ValueError("The browser job is stale or belongs to another document")
        return job

    def active(self, provider, job_id, owner, lease, document):
        with self.condition:
            try:
                self._owned(provider, job_id, owner, lease, document)
                return True
            except ValueError:
                return False

    def begin(self, provider, job_id, owner, lease, document):
        with self.condition:
            job = self._owned(provider, job_id, owner, lease, document)
            if job.submitted:
                raise ValueError("The browser prompt has already been authorized")
            def validate():
                if time.monotonic() >= job.deadline or job.result is not None:
                    raise ValueError("The browser lease expired before submission")
                try:
                    if job.check_cancelled is not None:
                        job.check_cancelled()
                except asyncio.CancelledError:
                    raise ValueError("The browser lease was cancelled") from None
            if job.access_attempt is not None:
                try:
                    job.access_attempt.dispatch(validate)
                    job.access_attempt.check()
                except (asyncio.CancelledError, ProviderRejected):
                    raise ValueError("The browser lease was revoked before submission") from None
            validate()
            job.submitted = True

    def navigate(self, provider, job_id, owner, lease, document):
        with self.condition:
            job = self._owned(provider, job_id, owner, lease, document)
            if job.submitted:
                raise ValueError("A submitted job cannot transfer documents")
            # Only an explicit navigation releases the old document. The first
            # successor claim binds a fresh instance; other copies stay blocked.
            job.document = None

    def finish(self, provider, job_id, owner, lease, document, result):
        with self.condition:
            job = self._owned(provider, job_id, owner, lease, document)
            if not job.submitted and not result.get("error"):
                raise ValueError("The browser prompt was not authorized")
            job.result = result
            self.condition.notify_all()


broker = Broker()


def parse_browser_result(provider, result, path=None, prompt=None):
    if not isinstance(result, dict) or result.get("error"):
        raise ProviderUnavailable(f"{provider} browser did not complete the request")
    if result.get("status") != 200:
        raise ProviderUnavailable(f"{provider} rejected access or quota; inspect its normal browser tab")
    reference = result.get("path")
    if path is not None and reference != path:
        raise ProviderUnavailable("Browser changed the resumed conversation")
    token = encode(provider, "default", reference)
    encoded = result.get("body")
    if not isinstance(encoded, str) or len(encoded) > 12 * 1024 * 1024:
        raise ProviderUnavailable("Invalid browser response size")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, UnicodeError):
        raise ProviderUnavailable("Invalid browser response encoding") from None
    if len(body) > 8 * 1024 * 1024:
        raise ProviderUnavailable("Browser response exceeded the size limit")
    if provider == "glm":
        text = glm_answer(body)
    elif provider == "grok":
        text = grok_answer(body)
    elif provider == "mistral":
        if not body.lstrip().startswith(b"15:"):
            raise ProviderUnavailable("Mistral browser response lacks verifiable turn binding")
        request_turn = result.get("request_turn")
        if path is not None and (not isinstance(request_turn, dict)
                                 or request_turn.get("chat_id") != reference.rsplit("/", 1)[-1]):
            raise ProviderUnavailable("Mistral resumed request does not match its conversation")
        text = mistral_answer(body, prompt, reference.rsplit("/", 1)[-1], request_turn)
    else:
        text = kimi_answer(body, prompt, reference.rsplit("/", 1)[-1])
    return Reply(text, token)


class TabClient:
    def __init__(self, provider, check_cancelled=lambda: None):
        if provider not in BROWSER_PROVIDERS:
            raise ValueError("Unsupported browser provider")
        self.provider, self.check_cancelled = provider, check_cancelled

    def chat(self, prompt, conversation_id=None, model=None, thinking=False, search=False):
        with provider_attempt(self.provider, self.check_cancelled):
            return self._chat(prompt, conversation_id, model, thinking, search)

    def _chat(self, prompt, conversation_id, model, thinking, search):
        if not bridge_enabled():
            raise ProviderUnavailable("Enable BROWSER_BRIDGE_ENABLED and pair your normal browser tab first")
        if thinking or search:
            raise ProviderUnavailable("Browser tab bridge does not support thinking/search switches")
        path = None
        if conversation_id:
            model, path = decode(conversation_id, self.provider)
        if model not in (None, "default"):
            raise ValueError("Browser providers use the account's selected web model")
        self.check_cancelled()
        result = broker.submit(self.provider, prompt, path, self.check_cancelled, completion_timeout())
        return parse_browser_result(self.provider, result, path, prompt)

    def stream(self, *args, **kwargs):
        return BufferedStream(self.chat(*args, **kwargs))


def export_scripts():
    """Write pairing material only to ignored, private files; never stdout."""
    destination = token_path().parent / "userscripts"
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.chmod(0o700)
    token = bridge_token()
    for name in ("opencode-observer.user.js", "opencode-controller.user.js"):
        source = ROOT / "browser" / name
        target = destination / name
        value = source.read_text().replace("__BRIDGE_TOKEN__", token, 1)
        target.write_text(value)
        target.chmod(0o600)
    return destination


if __name__ == "__main__":
    print("Private userscripts written to:", export_scripts())
