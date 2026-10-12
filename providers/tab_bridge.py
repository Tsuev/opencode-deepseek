"""Authenticated, loopback-only jobs for an already signed-in browser tab."""

from __future__ import annotations

import base64
import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from chat_protocol import Reply
from settings import ROOT
from .browser_protocol import (
    chatgpt_answer, chatgpt_conversation_path, claude_answer, claude_conversation_path,
    gemini_answer, gemini_conversation_path, gemini_paths_compatible,
    glm_answer, grok_answer, mistral_answer, qwen_reply, qwen_conversation_path,
)
from .kimi_protocol import kimi_answer
from .common import BufferedStream, ProviderUnavailable, completion_timeout
from .access import provider_attempt, current_attempt, ProviderRejected
from .conversations import decode, encode

BROWSER_PROVIDERS = frozenset(("grok", "mistral", "kimi", "glm", "qwen", "gemini", "chatgpt", "claude"))
_log = logging.getLogger(__name__)
_SAFE_BRIDGE_DETAIL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,:;!?_+/'\-]{0,159}$")
_BRIDGE_PROTOCOL = (
    ("browser verification", "browser_verify"),
    ("did not return an SSE", "browser_json"),
    ("no conversation path", "browser_path"),
    ("empty completion", "browser_empty"),
    ("disconnected before", "browser_incomplete"),
    ("send control is unavailable", "browser_send"),
    ("did not retain", "browser_editor"),
    ("could not be observed", "browser_observe"),
    ("was not observed", "browser_observe"),
    ("not observed over REST", "browser_observe"),
    ("observation stalled", "browser_observe"),
    ("observation failed", "browser_observe"),
    ("unsent draft", "browser_draft"),
    ("Tab unavailable", "browser_tab"),
    ("rejected access or quota", "browser_status"),
    ("changed the resumed conversation", "browser_route"),
    ("did not complete the request", "browser_timeout"),
)


def _bridge_detail(value):
    """Log/raise only short fixed bridge messages; never free-form page text."""
    if isinstance(value, str) and _SAFE_BRIDGE_DETAIL.fullmatch(value):
        return value
    return None


def _annotate_bridge_failure(exc, detail=None):
    """Attach diagnostic categories without changing the attempt safety policy."""
    if not isinstance(exc, ProviderUnavailable):
        return exc
    if not getattr(exc, "phase", None):
        exc.phase = "browser_result"
    text = detail or str(exc)
    if not getattr(exc, "protocol", None):
        for needle, token in _BRIDGE_PROTOCOL:
            if needle in text:
                exc.protocol = token
                break
        else:
            exc.protocol = "browser_bridge"
    # Unknown post-dispatch outcomes must retain the durable pause.
    return exc


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
    submitting: bool = False
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
            if job.submitted or job.submitting:
                raise ValueError("The browser prompt has already been authorized")
            job.submitting = True
        def validate():
            # Never acquire condition here: dispatch also invokes this callback
            # while holding the durable guard lock. Final ownership validation
            # under condition below closes the race after pacing.
            if (self.pending.get(provider) is not job or job.document != document
                    or time.monotonic() >= job.deadline or job.result is not None):
                raise ValueError("The browser lease expired before submission")
            try:
                if job.check_cancelled is not None:
                    job.check_cancelled()
            except asyncio.CancelledError:
                raise ValueError("The browser lease was cancelled") from None
        try:
            if job.access_attempt is not None:
                try:
                    job.access_attempt.dispatch(validate)
                    job.access_attempt.check()
                except (asyncio.CancelledError, ProviderRejected):
                    raise ValueError("The browser lease was revoked before submission") from None
            with self.condition:
                if self._owned(provider, job_id, owner, lease, document) is not job:
                    raise ValueError("The browser job changed before submission")
                validate()
                job.submitted = True
        finally:
            with self.condition:
                job.submitting = False

    def navigate(self, provider, job_id, owner, lease, document):
        with self.condition:
            job = self._owned(provider, job_id, owner, lease, document)
            if job.submitted or job.submitting:
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
    if not isinstance(result, dict):
        raise ProviderUnavailable(f"{provider} browser did not complete the request")
    if result.get("error"):
        detail = _bridge_detail(result.get("error"))
        raise ProviderUnavailable(
            f"{provider} browser did not complete the request"
            + (f": {detail}" if detail else "")
        )
    if result.get("status") != 200:
        raise ProviderUnavailable(f"{provider} rejected access or quota; inspect its normal browser tab")
    reference = result.get("path")
    if path is not None and reference != path:
        # Gemini AI Mode: first turn stores /app/aimode while the tab still
        # reports /search — same thread, not a route change.
        if provider != "gemini" or not gemini_paths_compatible(path, reference):
            raise ProviderUnavailable("Browser changed the resumed conversation")
    encoded = result.get("body")
    if not isinstance(encoded, str) or len(encoded) > 12 * 1024 * 1024:
        raise ProviderUnavailable("Invalid browser response size")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, UnicodeError):
        raise ProviderUnavailable("Invalid browser response encoding") from None
    if len(body) > 8 * 1024 * 1024:
        raise ProviderUnavailable("Browser response exceeded the size limit")
    if provider == "qwen":
        # SPA may still show "/" when the stream already carries chat_id.
        reference = qwen_conversation_path(body, reference)
    elif provider == "chatgpt":
        # SPA may still show "/" until the stream carries conversation_id.
        reference = chatgpt_conversation_path(body, reference)
    elif provider == "claude":
        # New chats may still show "/" until the SPA/API exposes /chat/<id>.
        reference = claude_conversation_path(body, reference)
    elif provider == "gemini":
        # SPA often stays on /app until the stream carries the conversation id.
        reference = gemini_conversation_path(body, reference)
    if provider in ("glm", "grok"):
        turn = result.get("request_turn")
        if (not isinstance(turn, dict) or set(turn) != {"chat_id", "prompt", "endpoint"}
                or not isinstance(prompt, str) or turn["prompt"] != prompt):
            raise ProviderUnavailable("Browser response lacks exact request binding")
        chat_id = turn["chat_id"]
        if chat_id is not None and (not isinstance(chat_id, str) or reference != "/c/" + chat_id):
            raise ProviderUnavailable("Browser request belongs to a different conversation")
        if path is not None and chat_id is None:
            raise ProviderUnavailable("Resumed browser request lacks conversation binding")
        if provider == "grok":
            endpoint = ("/rest/app-chat/conversations/new" if chat_id is None else
                        "/rest/app-chat/conversations/" + chat_id + "/responses")
            if turn["endpoint"] != endpoint:
                raise ProviderUnavailable("Unexpected Grok request endpoint")
        elif turn["endpoint"] not in ("/api/chat/completions", "/api/v2/chat/completions"):
            raise ProviderUnavailable("Unexpected GLM request endpoint")
    token = encode(provider, "default", reference)
    finish_reason = "stop"
    if provider == "glm":
        text = glm_answer(body)
    elif provider == "grok":
        text = grok_answer(body, reference.rsplit("/", 1)[-1])
    elif provider == "qwen":
        # Keep finish_reason: an output-limit stop must not authorize tool calls.
        text, finish_reason = qwen_reply(body, reference.rsplit("/", 1)[-1])
    elif provider == "gemini":
        text = gemini_answer(body, prompt)
    elif provider == "chatgpt":
        text = chatgpt_answer(body)
    elif provider == "claude":
        text = claude_answer(body, prompt)
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
    if provider in ("gemini", "claude") and _browser_text_echoes_prompt(text, prompt):
        # DOM scrapes can echo the pasted OpenCode prompt before a real answer.
        label = "Gemini" if provider == "gemini" else "Claude"
        raise ProviderUnavailable(f"{label} browser returned the request prompt instead of an answer")
    return Reply(text, token, finish_reason)


def _browser_text_echoes_prompt(text, prompt):
    if not isinstance(text, str) or not isinstance(prompt, str) or not text.strip():
        return False

    def norm(value):
        return " ".join(value.replace("\u200b", "").replace("\ufeff", "").split())

    got, wanted = norm(text), norm(prompt)
    if not got or not wanted:
        return False
    if got == wanted:
        return True
    if wanted.startswith(got) and len(got) >= 64:
        return True
    if got.startswith(wanted) and len(got) <= len(wanted) + 64:
        return True
    head = wanted[:120]
    if len(head) >= 48 and head in got and len(got) <= len(wanted) + 96:
        return True
    if "Available tools, given as JSON Schema" in got:
        return True
    if "```tool_calls" in got and "You can call functions" in got:
        return True
    return False


class TabClient:
    def __init__(self, provider, check_cancelled=lambda: None):
        if provider not in BROWSER_PROVIDERS:
            raise ValueError("Unsupported browser provider")
        self.provider, self.check_cancelled = provider, check_cancelled

    def chat(self, prompt, conversation_id=None, model=None, thinking=False, search=False):
        with provider_attempt(self.provider, self.check_cancelled):
            try:
                return self._chat(prompt, conversation_id, model, thinking, search)
            except ProviderUnavailable as exc:
                # Diagnostic annotations never grant permission to repeat a request.
                raise _annotate_bridge_failure(exc) from exc

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
        try:
            result = broker.submit(self.provider, prompt, path, self.check_cancelled, completion_timeout())
        except ProviderUnavailable as exc:
            # Timeouts never reach parse_browser_result; log them the same way.
            detail = _bridge_detail(str(exc)) or type(exc).__name__
            _log.warning(
                "Browser result rejected: provider=%s detail=%s status=%s path=%s body_kind=%s",
                self.provider, detail, None, path, "none",
            )
            raise _annotate_bridge_failure(exc, detail) from exc
        try:
            return parse_browser_result(self.provider, result, path, prompt)
        except (ProviderUnavailable, ValueError) as exc:
            # Fixed bridge/parser messages only; never log response bodies.
            detail = _bridge_detail(str(exc)) or type(exc).__name__
            body = result.get("body") if isinstance(result, dict) else None
            kind = "none"
            body_len = 0
            shape = ""
            if isinstance(body, str) and body:
                try:
                    raw = base64.b64decode(body, validate=True)
                    body_len = len(raw)
                    kind = "json" if raw[:1] in (b"{", b"[") else "sse_or_other"
                    if self.provider == "gemini" and raw:
                        probe = raw[:65536].decode("utf-8", errors="replace")
                        shape = ",".join(flag for flag, ok in (
                            ("lt", "<" in probe),
                            ("u003c", "\\u003c" in probe),
                            ("aim", "n6owBd" in probe or "data-subtree" in probe),
                            ("tools", "Available tools, given as JSON Schema" in probe),
                            ("wrb", "wrb.fr" in probe),
                        ) if ok) or "empty_shape"
                except (ValueError, UnicodeError):
                    kind = "invalid_b64"
            _log.warning(
                "Browser result rejected: provider=%s detail=%s status=%s path=%s "
                "body_kind=%s body_len=%s shape=%s",
                self.provider, detail, result.get("status") if isinstance(result, dict) else None,
                result.get("path") if isinstance(result, dict) else None, kind, body_len, shape,
            )
            if isinstance(exc, ValueError):
                raise _annotate_bridge_failure(
                    ProviderUnavailable(f"{self.provider} browser returned an invalid conversation route"),
                    "invalid conversation route",
                ) from exc
            raise _annotate_bridge_failure(exc, detail)

    def stream(self, *args, **kwargs):
        return BufferedStream(self.chat(*args, **kwargs))


def export_scripts():
    """Write pairing material only to ignored, private files; never stdout."""
    destination = token_path().parent / "userscripts"
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.chmod(0o700)
    token = bridge_token()
    port = os.getenv("PORT", "8000").strip()
    if not (1 <= len(port) <= 5 and port.isdecimal() and 1 <= int(port) <= 65535):
        raise ValueError("PORT must be a TCP port number")
    for name in ("opencode-observer.user.js", "opencode-controller.user.js",
                 "deepseek-session-transfer.user.js"):
        source = ROOT / "browser" / name
        target = destination / name
        value = (source.read_text(encoding="utf-8").replace("__BRIDGE_TOKEN__", token, 1)
                 .replace("__BRIDGE_PORT__", str(int(port)), 1))
        target.write_text(value, encoding="utf-8")
        target.chmod(0o600)
    return destination


if __name__ == "__main__":
    print("Private userscripts written to:", export_scripts())
