"""
OpenAI-compatible FastAPI server for DeepSeek and opt-in account providers.

Point any OpenAI client at http://localhost:8000/v1 :

    from openai import OpenAI
    client = OpenAI(base_url="http://localhost:8000/v1", api_key="not-needed")
    r = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": "Hello!"}],
    )

Endpoints:
    GET  /v1/models
    POST /v1/chat/completions   (stream=true supported)
    GET  /healthz

Requests under /v1 are rate limited per client IP (default 30/min, set via
RATE_LIMIT_PER_MINUTE); /healthz is exempt.

All providers are opt-in. Sessions are loaded from cache only. Requests are
paced and attempted once; upstream failures persist a provider pause. Missing
sessions return 401; paused providers return 403 before any account access.
Streaming failures emit a terminal error and stop subsequent reconnects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
import hashlib
from contextvars import ContextVar

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from deepseek.auth import SESSION_MAX_AGE, LoginRequired, get_session
from chat_protocol import Reply
from deepseek.client import DeepSeekClient, _decode_cid as decode_deepseek_cid
from qwen.auth import get_session as get_qwen_session, LoginRequired as QwenLoginRequired
from qwen.renewal import renew_session as renew_qwen_session
from qwen.client import QwenClient, _decode_cid as decode_qwen_cid
from providers.conversations import decode as decode_provider_cid, PROVIDERS
from providers.registry import build_client as build_provider_client
from providers.access import guard, ProviderRejected, rejection
from providers.common import ProviderUnavailable
from .tool_protocol import validate_reply
from .tool_history import validate_tool_history
from .diagnostics import save_tool_failure
from .conversation_cache import conversation_cache, ConversationStateError

from .config import (
    MODEL_MAP,
    RATE_LIMIT_PER_MINUTE,
    QWEN_RENEW_SESSION,
    REFRESH_BROWSER_CHANNEL,
    REFRESH_BROWSER_CHANNEL_FALLBACK,
    SERVER_INTERACTIVE_LOGIN,
    SESSION_REFRESH_ENABLED,
    SESSION_REFRESH_INTERVAL,
    advertised_model_ids,
    is_known_model,
    resolve_model_type,
    model_provider,
    qwen_uses_browser,
)
from .openai_format import (
    completion_response,
    looks_truncated,
    messages_to_prompt,
    sse_frames,
    stream_chunks,
    ToolCallError,
    validate_tool_choice,
)
from .ratelimit import RateLimiter, install_rate_limit
from .browser_routes import router as browser_router
from .schemas import ChatCompletionRequest, ChatMessage

_log = logging.getLogger(__name__)

# One shared client (and its signed-in session) built lazily on first use.
_client: DeepSeekClient | None = None
_client_identity = None
_bound_deepseek = ContextVar("bound_deepseek", default=None)
# Non-DeepSeek conversation_cache turns track attempt.dispatch for one-use release.
_bound_conversation = ContextVar("bound_conversation", default=None)
_client_lock = threading.Lock()
_stop_refresh = threading.Event()
_request_gate = asyncio.Lock()
_qwen_request_gate = asyncio.Lock()
_qwen_client: QwenClient | None = None
_qwen_client_lock = threading.Lock()
_provider_request_gates = {name: asyncio.Lock() for name in PROVIDERS}

_MISSING = object()
_worker_cancellation = ContextVar("worker_cancellation", default=None)


def _check_cancellation(signal):
    if signal is not None and signal.is_set():
        raise asyncio.CancelledError()


def _check_cancelled():
    _check_cancellation(_worker_cancellation.get())


def _cancellation_check():
    # Browser lease routes run in another worker; retain the origin's Event.
    signal = _worker_cancellation.get()
    return lambda: _check_cancellation(signal)

def _tool_names(tools) -> set:
    """Extract requested tool names from OpenAI tool objects, defensively."""
    names = set()
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function", t)
        if isinstance(fn, dict) and isinstance(fn.get("name"), str) and fn["name"]:
            names.add(fn["name"])
    return names


def _toolcall_debug() -> bool:
    return os.getenv("TOOLCALL_DEBUG", "").strip().lower() not in (
        "", "0", "false", "no", "off",
    )


def _build_client(force: bool = False, allow_interactive=None, session=None) -> DeepSeekClient:
    """Load a cached session only; legacy force/login arguments cannot browse."""
    if session is None:
        session = get_session(max_age=SESSION_MAX_AGE, allow_interactive=False, allow_refresh=False)
    return DeepSeekClient(session=session, access_guard=None)  # server owns pause/pacing


def get_client(force_refresh: bool = False, rejected_client=None) -> DeepSeekClient:
    """Cache a client; an explicit reset reloads the session file without browsing."""
    global _client, _client_identity
    # One parsed snapshot supplies both credentials and identity. Never hash
    # the current file separately from the Session used to build the client.
    session = get_session(max_age=SESSION_MAX_AGE, allow_interactive=False, allow_refresh=False)
    identity = _session_identity(session)
    with _client_lock:
        if _client_identity != identity or _client is None or (force_refresh and (
            rejected_client is None or _client is rejected_client
        )):
            _client = _build_client(force=force_refresh, session=session)
            _client_identity = identity
        return _client


def _session_identity(session):
    return session.identity()


def reset_client() -> None:
    """Drop the cached client so the next get_client() rebuilds the session.

    The old client is intentionally not closed synchronously: an in-flight
    request may still hold a reference to it. Leaking one httpx pool is
    negligible; closing it early would break that request."""
    global _client
    with _client_lock:
        _client = None


def get_qwen_client(force_refresh=False, rejected_client=None, allow_renewal=True,
                    expected_account=None) -> QwenClient:
    """Load local Qwen credentials, with separately opted-in pre-dispatch renewal.

    Renewal is an account request: it runs only inside the request's durable
    attempt. Callers outside an attempt pass allow_renewal=False."""
    global _qwen_client
    with _qwen_client_lock:
        # A visible auth window can save a rotated token before the previous
        # token expires. Read the atomic local snapshot on every new request;
        # never keep using an older in-memory credential after that handoff.
        try:
            session = get_qwen_session(allow_interactive=False, allow_refresh=False,
                                       channel=REFRESH_BROWSER_CHANNEL,
                                       fallback_channel=REFRESH_BROWSER_CHANNEL_FALLBACK)
        except LoginRequired:
            if not QWEN_RENEW_SESSION or not allow_renewal:
                raise
            session = (renew_qwen_session() if expected_account is None
                       else renew_qwen_session(expected_account=expected_account))
        if (_qwen_client is None or _qwen_client.session != session
                or (force_refresh and (rejected_client is None or _qwen_client is rejected_client))):
            # Existing requests retain their own client; do not close it here.
            _qwen_client = QwenClient(session, access_guard=None)  # server owns pause/pacing
        return _qwen_client


def _request_client(req, force_refresh=False, rejected_client=None, expected_account=None):
    provider = model_provider(req.model)
    guard.check(provider)
    # Direct Qwen HTTP keeps its own client; browser transport shares TabClient.
    if provider == "qwen" and not qwen_uses_browser():
        if force_refresh:
            return get_qwen_client(True, rejected_client=rejected_client)
        if expected_account is not None:
            return get_qwen_client(expected_account=expected_account)
        return get_qwen_client()
    if provider in PROVIDERS:
        # Browser routes run in another worker context. Bind the originating
        # request's Event now so they see cancellation before this worker wakes.
        signal = _worker_cancellation.get()
        return build_provider_client(provider, lambda: _check_cancellation(signal))
    if force_refresh:
        return get_client(True, rejected_client=rejected_client)
    return get_client()


def _request_queue(req):
    provider = model_provider(req.model)
    if provider == "qwen" and not qwen_uses_browser():
        return _qwen_request_gate
    if provider in PROVIDERS:
        return _provider_request_gates[provider]
    return _request_gate


async def _run_worker(function, *args):
    """Cancellation waits for sync work rather than abandoning a live request."""
    signal = threading.Event()

    def run():
        token = _worker_cancellation.set(signal)
        try:
            _check_cancelled()
            return function(*args)
        finally:
            _worker_cancellation.reset(token)

    worker = asyncio.create_task(run_in_threadpool(run))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        signal.set()
        # Also shield against Starlette's AnyIO disconnect cancellation scope.
        with anyio.CancelScope(shield=True):
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                worker.exception()
        raise


async def _run_queued(function, *args, gate=None):
    """Keep one account request, including retries/continuations, in the queue."""
    queue = gate if gate is not None else _request_gate
    async with queue:
        return await _run_worker(function, *args)


async def _run_chat_with_retry(prompt: str, req: ChatCompletionRequest, model_type):
    """Run one paced completion inside the provider queue."""
    return await _run_queued(_chat_with_retry, prompt, req, model_type, gate=_request_queue(req))


def _invalidate_client(provider, client=None):
    global _client, _qwen_client
    lock = _qwen_client_lock if provider == "qwen" else _client_lock
    if provider in ("deepseek", "qwen"):
        with lock:
            if provider == "deepseek" and (client is None or _client is client):
                _client = None
            elif provider == "qwen" and (client is None or _qwen_client is client):
                _qwen_client = None


def _chat_with_retry(prompt: str, req: ChatCompletionRequest, model_type):
    """One durable attempt; cancellation cannot erase an uncertain dispatch."""
    provider = model_provider(req.model)
    if provider == 'deepseek' and len(prompt.encode('utf-8')) > 131072:
        raise ProviderRejected('local_prompt_too_large', source='local_limit')
    client = None
    attempt = None
    try:
        with guard.attempt(provider, _cancellation_check()) as attempt:
            binding = (_bound_deepseek.get() if provider == "deepseek"
                       else _bound_conversation.get())
            if binding is not None:
                binding['attempts'].append(attempt)
            if binding is not None and 'client' in binding:
                client = binding['client']
            elif binding is not None and 'account' in binding:
                # Renewal compares this account on its own snapshot before dispatch.
                client = _request_client(req, expected_account=binding['account'])
            else:
                client = _request_client(req)
            if binding is not None and 'account' in binding and (
                    provider != 'qwen' or _qwen_account(client.session) != binding['account']):
                # The history scope belongs to another account; refuse before dispatch.
                raise QwenLoginRequired('The Qwen session belongs to a different account than this history; '
                                        'no request was sent.')
            attempt.check()  # Recheck a pause that happened while building a client.
            return client.chat(prompt, req.conversation_id, model_type, req.thinking, req.search)
    except LoginRequired:
        _invalidate_client(provider, client)
        raise
    except Exception as exc:
        # A contender that never owned an attempt cannot mutate its peer state.
        if attempt is not None:
            _invalidate_client(provider, client)
        error = exc if isinstance(exc, ProviderRejected) else rejection(exc)
        raise error from exc


# A truncated tool reply is continued this many times before giving up. Each
# round-trip resumes the same provider conversation and appends the next slice.
_MAX_CONTINUATIONS = int(os.getenv("TOOLCALL_MAX_CONTINUATIONS", "0"))
if _MAX_CONTINUATIONS < 0:
    raise ValueError("TOOLCALL_MAX_CONTINUATIONS must be non-negative")

# One opt-in format correction in the same DeepSeek conversation. This is
# separate from transport retries and output-limit continuation; neither a
# rejection nor an uncertain/truncated generation qualifies.
_FORMAT_REPAIR = os.getenv("TOOLCALL_FORMAT_REPAIR", "0") == "1"
_FORMAT_REPAIR_PROMPT = (
    "No tools were executed from your previous reply: it was empty or its tool-call format was invalid. "
    "Choose the next response using the task and tool schemas already provided. "
    "If a tool is necessary, return ONLY one fenced tool_calls block containing a "
    "complete valid JSON array. Each entry must have name and arguments; ALL input "
    "fields belong inside the arguments object. Escape string values correctly. "
    "No commentary before or after the block, and no XML/DSML tags. "
    "If you intended only an explanation or the work is complete, give plain text "
    "without a tool_calls block. Do not claim an unexecuted action succeeded."
)

# Sent to make the model finish an answer its output limit cut off.
_CONTINUE_PROMPT = (
    "Your previous message was cut off by the output limit before it finished. "
    "Continue EXACTLY from the character where it stopped. Output only the "
    "remaining characters — no preamble, no apology, no repetition, and no "
    "opening code fence (only the missing closing one, if that is what was cut)."
)


def _continue_reply(prev: Reply, req: ChatCompletionRequest):
    """Resume the same provider conversation and return the next slice.

    On resume the thread keeps its own model, so `model` is passed as None —
    matching both clients' resume rules."""
    if not prev.conversation_id:
        raise ToolCallError("Cannot continue a tool reply without a conversation_id")
    continuation = req.model_copy(update={"conversation_id": prev.conversation_id})
    return _chat_with_retry(_CONTINUE_PROMPT, continuation, None)


async def _run_tool_chat(prompt: str, req: ChatCompletionRequest, model_type):
    return await _run_queued(_conversation_tool_chat, prompt, req, model_type, gate=_request_queue(req))


def _plain_conversation_reply(prompt, req, model_type):
    reply = _chat_with_retry(prompt, req, model_type)
    return reply, reply.text, None


def _conversation_scope(provider, client=None):
    """Namespace one-use history links per account/transport snapshot."""
    if conversation_cache.account_file is None:
        return "isolated"
    if provider == "deepseek":
        # Prefer pinned continuation_scope after Safari/session transfer so
        # history links survive identity rotation without resetting threads.
        return "session-v2:" + (
            client.session.continuation_scope or _session_identity(client.session)
        )
    if provider == "qwen" and not qwen_uses_browser():
        # Token rotation must not orphan a goal: bind to the JWT account.
        account = _qwen_account(client.session)
        return "qwen-v2:" + account if account else "qwen-v1:" + _session_identity(client.session)
    # Browser tab bridge: one local machine / provider, not DeepSeek session.json.
    return "web:" + provider


def _qwen_account(session):
    """Stable account key from the Qwen JWT id/type, or None if unreadable."""
    from qwen.renewal import account_key
    try:
        return account_key(session.token)
    except (ValueError, TypeError, AttributeError):
        return None


def _legacy_scopes(provider, client=None):
    """Pre-upgrade link namespaces that may still hold unconsumed histories."""
    if (provider == "qwen" and not qwen_uses_browser() and client is not None
            and conversation_cache.account_file is not None and _qwen_account(client.session)):
        return ("qwen-v1:" + _session_identity(client.session),)
    return ()


def _conversation_tool_chat(prompt, req, model_type):
    """Resume provider threads for DeepSeek and web providers via conversation_cache.

    Stateless OpenAI clients (OpenCode) resend full messages[]; the cache maps an
    exact prior history to conversation_id and forwards only the new tail so the
    browser/HTTP thread continues instead of opening another chat.
    """
    provider = model_provider(req.model)
    original = req
    claim = {}
    binding = {"attempts": []}
    deepseek_token = None
    conversation_token = None
    succeeded = False
    legacy = ()
    try:
        guard.check(provider)
        _check_cancelled()
        if provider == "deepseek":
            client = get_client()
            # Test caches deliberately have no account file. Production scopes
            # derive only from this pinned client's Session, never a later file.
            scope = _conversation_scope(provider, client)
            binding["client"] = client
            deepseek_token = _bound_deepseek.set(binding)
        elif provider == "qwen" and not qwen_uses_browser():
            try:
                # Never renew here: renewal is an account request and must be
                # owned by the durable attempt opened in _chat_with_retry.
                client = get_qwen_client(allow_renewal=False)
                binding["client"] = client
                scope_client = client
                legacy = _legacy_scopes(provider, client)
            except LoginRequired:
                from qwen.auth import Session as QwenSession, DEFAULT_SESSION_FILE as QWEN_SESSION_FILE
                cached = QwenSession.load(QWEN_SESSION_FILE) if QWEN_RENEW_SESSION else None
                if cached is None or _qwen_account(cached) is None:
                    raise
                scope_client = type("ScopeSnapshot", (), {"session": cached})()
                legacy = _legacy_scopes(provider, scope_client)
            scope = _conversation_scope(provider, scope_client)
            expected_account = _qwen_account(scope_client.session)
            if expected_account:
                binding["account"] = expected_account
            conversation_token = _bound_conversation.set(binding)
        else:
            scope = _conversation_scope(provider)
            conversation_token = _bound_conversation.set(binding)
        req, steps = conversation_cache.prepare(req, scope=scope, claim=claim,
                                                legacy_scopes=legacy)
        if req is not original:
            # Continuation of a saved provider chat: browser light mode may shorten tools.
            prompt = messages_to_prompt(
                req.messages, req.tools, req.tool_choice,
                tools_continuation=True, provider=provider)
            model_type = None
        try:
            result = (_tool_chat_with_retry(prompt, req, model_type) if req.tools else
                      _plain_conversation_reply(prompt, req, model_type))
        except ProviderRejected as exc:
            # One opt-in migration, never a transport retry or a format repair.
            # Existing pauses cannot enter here: only this request's first,
            # proven empty context refusal may retire its own durable attempt.
            if (os.getenv('DEEPSEEK_CONTEXT_ROLLOVER', '0') != '1'
                    or provider != 'deepseek' or original.conversation_id or not claim
                    or exc.code != 'conversation_context_exceeded'
                    or len(binding['attempts']) != 1
                    or not getattr(binding['attempts'][0], 'empty_context_refusal', False)):
                raise
            from .context_rollover import checkpoint
            _check_cancelled()
            handoff, archive = checkpoint(original, req.conversation_id,
                                          conversation_cache.path.parent / 'context-checkpoints')
            migrated = original.model_copy(update={'conversation_id': None,
                'messages': [ChatMessage(role='user', content=handoff)]})
            migration_prompt = messages_to_prompt(migrated.messages, migrated.tools,
                migrated.tool_choice, tools_continuation=False, provider=provider)
            if len(migration_prompt.encode('utf-8')) > 131072:
                raise ConversationStateError('Migration prompt exceeds local limit; no new chat.')
            conversation_cache.claim_rollover(claim, req.conversation_id, archive)
            _check_cancelled()
            guard.retire_context_refusal(binding['attempts'][0])
            # No format-repair or output continuation on the migration request.
            # Any new failure stops with the new attempt still consumed.
            migrated_reply = _chat_with_retry(migration_prompt, migrated, resolve_model_type(original.model))
            result = (_validate_tool_reply(migrated_reply, migrated, allow_continuation=False) if migrated.tools else
                      (migrated_reply, migrated_reply.text, None))
        conversation_cache.remember(original, *result, steps, scope=scope)
        succeeded = True
        return result
    except ConversationStateError as exc:
        raise ToolCallError(str(exc)) from exc
    finally:
        if deepseek_token is not None:
            _bound_deepseek.reset(deepseek_token)
        if conversation_token is not None:
            _bound_conversation.reset(conversation_token)
        # Pre-dispatch failures may release. DeepSeek challenge-auth rejection
        # before generation is also pre-generation; an earlier generation
        # (e.g. format correction) keeps the claim used. Browser/HTTP dispatch
        # without that flag still consumes the reservation.
        if not succeeded and all(
            not a.dispatched or (a.challenge_auth_rejected and not a.generation_dispatched)
            for a in binding["attempts"]
        ):
            conversation_cache.release(claim)


def _tool_chat_with_retry(prompt: str, req: ChatCompletionRequest, model_type):
    reply = _chat_with_retry(prompt, req, model_type)
    try:
        return _validate_tool_reply(reply, req)
    except ToolCallError as initial_error:
        if not (_FORMAT_REPAIR and model_provider(req.model) == "deepseek"
                and req.tool_choice != "none" and reply.finish_reason == "stop"
                and reply.conversation_id):
            print('[toolcalls] format_correction=not_eligible', flush=True)
            raise
        _check_cancelled()
        correction = req.model_copy(update={"conversation_id": reply.conversation_id})
        print('[toolcalls] format_correction=requested', flush=True)
        correction_prompt = _FORMAT_REPAIR_PROMPT
        if initial_error.repair_hint:
            correction_prompt += "\n" + initial_error.repair_hint
        corrected = _chat_with_retry(correction_prompt, correction, None)
        if corrected.finish_reason != "stop":
            print('[toolcalls] format_correction=incomplete', flush=True)
            save_tool_failure(corrected, req)
            raise ToolCallError("The format correction did not finish normally; no calls were returned")
        # No recursion: a second invalid answer is terminal. The original
        # batch never escaped validation and cannot be executed twice.
        try:
            result = _validate_tool_reply(corrected, req)
        except ToolCallError as exc:
            print('[toolcalls] format_correction=invalid', flush=True)
            raise ToolCallError(
                'The single format correction also failed; no further correction was requested. '
                + str(exc)
            ) from exc
        print('[toolcalls] format_correction=validated', flush=True)
        return result


def _validate_tool_reply(reply: Reply, req: ChatCompletionRequest, *, allow_continuation=True):
    """Validate the whole reply; continuation requires opt-in and an output limit.

    A missing Markdown fence alone is not evidence of an output limit. A
    normally completed response may omit the fence or exactly the outer array
    closer only when all call objects and their arguments validate in full.
    """
    names = _tool_names(req.tools)

    maximum = _MAX_CONTINUATIONS if allow_continuation else 0
    for attempt in range(maximum + 1):
        _check_cancelled()
        can_continue = (reply.finish_reason == "length"
                        and looks_truncated(reply.text, names))
        if not can_continue:
            try:
                content, tool_calls = validate_reply(
                    reply.text, req.tools, req.tool_choice,
                    finish_reason=reply.finish_reason,
                    provider=model_provider(req.model),
                )
            except ToolCallError:
                save_tool_failure(reply, req)
                if _toolcall_debug():
                    print(f"[toolcalls] invalid explicit reply:\n{reply.text}\n", flush=True)
                raise
            if tool_calls is None and _toolcall_debug():
                print(f"[toolcalls] no call parsed; raw reply:\n{reply.text}\n", flush=True)
            return reply, content, tool_calls
        if req.tool_choice == "none":
            raise ToolCallError("The model started a tool call while tool_choice='none'")
        if attempt == maximum:
            raise ToolCallError("The provider reached its output limit before completing the tool reply; "
                                "no calls were returned and no further continuation is allowed")
        print(
            f"[toolcalls] reply truncated (attempt {attempt + 1}/"
            f"{_MAX_CONTINUATIONS}); requesting continuation...",
            flush=True,
        )
        try:
            nxt = _continue_reply(reply, req)
        except (LoginRequired, ProviderRejected):
            raise
        except Exception as e:
            raise ToolCallError("Could not finish the truncated tool reply") from e
        reply = Reply(text=reply.text + nxt.text, conversation_id=nxt.conversation_id,
                      finish_reason=nxt.finish_reason)


def _open_stream(client: DeepSeekClient, prompt: str, req: ChatCompletionRequest, model_type):
    return client.stream(
        prompt, conversation_id=req.conversation_id,
        model=model_type, thinking=req.thinking, search=req.search,
    )


def _sse_error(message: str, err_type: str = "server_error") -> str:
    obj = {"error": {"message": message, "type": err_type, "code": err_type, "retryable": False}}
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\ndata: [DONE]\n\n"


def _provider_pause_message(exc: ProviderRejected, model: str) -> str:
    """Client-facing pause text. Never includes upstream bodies or secrets."""
    provider = model_provider(model)
    resume = f"python -m providers.access resume {provider}"
    if getattr(exc, "source", "upstream") == "local_limit":
        if getattr(exc, "paused", False):
            return (f"Local limit stopped `{provider}` after earlier requests of this step "
                    f"(code={exc.code}): {exc} The outcome of the step is uncertain, so a durable "
                    f"providers.access pause was set. Inspect the website, then: `{resume}`")
        return (f"Local limit refused this `{provider}` request (code={exc.code}): {exc} "
                f"No request was sent to the provider and no pause was set.")
    if getattr(exc, "source", "upstream") == "local_pause":
        # Durable local pause — no upstream call; agents must clear it manually.
        return (
            f"Manual providers.access pause is blocking `{provider}` "
            f"(pause={exc.code}): {exc} "
            f"No request was sent to the upstream provider. "
            f"Inspect the normal website, then clear the pause with: `{resume}`"
        )
    # Fresh allowlisted upstream refusal that just latched a durable pause.
    return (
        f"Upstream provider `{provider}` refused this request "
        f"(mapped={exc.code}): {exc} "
        f"This bridge enabled a durable providers.access pause for `{provider}`. "
        f"Further requests stay blocked until you clear it with: `{resume}`"
    )


def _provider_rejected_response(exc: ProviderRejected, model: str):
    # Local pause uses 400 so agents show our message instead of a generic
    # "invalid API key" wrapper that many clients attach to HTTP 403.
    source = getattr(exc, "source", "upstream")
    if source == "local_limit" and not getattr(exc, "paused", False):
        status = 413 if exc.code == "local_prompt_too_large" else 429
    else:
        status = 400 if source in ("local_pause", "local_limit") else 403
    return _error(_provider_pause_message(exc, model), status=status, err_type=exc.code)


# How often to emit an SSE keep-alive comment while a buffered tool reply is
# still generating. Must stay below the client's idle read timeout.
_KEEPALIVE_SECONDS = 10.0


def _completion_failure_message(exc: BaseException) -> str:
    """Return a client-visible failure; log the real exception (was previously silent)."""
    if isinstance(exc, ProviderUnavailable):
        detail = str(exc).strip()
        _log.warning("Completion failed without durable pause: %s", detail or type(exc).__name__)
        if detail and len(detail) <= 300:
            return detail
    _log.exception("Completion failed; automatic replay is disabled")
    return "Provider request failed; automatic replay is disabled."


async def _tool_stream(req: ChatCompletionRequest, prompt: str, model_type):
    """Stream a buffered tool-call reply without tripping the client's timeout.

    Tool calls are emulated by buffering the whole reply before parsing, which
    can take minutes for a large write/edit. We run that work as a task and emit
    SSE keep-alive comments while it runs so the client's idle timer never fires.
    """
    task = asyncio.ensure_future(_run_tool_chat(prompt, req, model_type))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=_KEEPALIVE_SECONDS)
            if task in done:
                break
            yield ": keep-alive\n\n"

        try:
            reply, content, tool_calls = task.result()
        except LoginRequired as e:
            yield _sse_error(str(e), "login_required")
            return
        except ToolCallError as e:
            yield _sse_error(str(e), "invalid_tool_response")
            return
        except ProviderRejected as e:
            yield _sse_error(_provider_pause_message(e, req.model), e.code)
            return
        except Exception as e:
            yield _sse_error(_completion_failure_message(e))
            return

        for frame in sse_frames(req.model, content, tool_calls, reply.conversation_id,
                                finish_reason=reply.finish_reason):
            yield frame
    finally:
        if not task.done():
            task.cancel()
        # A cancelled buffered request retains its worker until HTTP finishes.
        # Retrieve any eventual exception even after the SSE consumer leaves.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())


def _stream_with_retry(client: DeepSeekClient, prompt: str,
                       req: ChatCompletionRequest, model_type):
    iterator = None
    attempt = None
    try:
        attempt = guard.begin(model_provider(req.model), _cancellation_check())
        if client is None:
            with attempt.bind():
                client = _request_client(req)
        attempt.check()
        stream = _open_stream(client, prompt, req, model_type)
        stream.access_attempt = attempt
        iterator = iter(stream)
        first = next(iterator, _MISSING)

        def remaining():
            if first is not _MISSING:
                yield first
            yield from iterator
            # Verify and clear the journal before yielding final SSE frames.
            attempt.complete()

        yield from stream_chunks(req.model, stream, iterator=remaining())
    except LoginRequired as exc:
        _invalidate_client(model_provider(req.model), client)
        yield _sse_error(str(exc), "login_required")
    except Exception as exc:
        if attempt is not None:
            attempt.abort(exc)
            _invalidate_client(model_provider(req.model), client)
        error = exc if isinstance(exc, ProviderRejected) else rejection(exc)
        yield _sse_error(_provider_pause_message(error, req.model), error.code)
    finally:
        if iterator is not None and hasattr(iterator, "close"):
            iterator.close()
        if attempt is not None:
            attempt.abort()


async def _plain_stream(client, prompt, req, model_type):
    # Queue asynchronously so waiting requests cannot exhaust worker threads
    # needed to advance an already-open stream.
    async with _request_queue(req):
        generator = _stream_with_retry(client, prompt, req, model_type)
        try:
            while True:
                frame = await _run_worker(next, generator, _MISSING)
                if frame is _MISSING:
                    break
                yield frame
        finally:
            with anyio.CancelScope(shield=True):
                await _run_worker(generator.close)


async def _buffered_plain_stream(req, prompt, model_type):
    """Keep the connection alive while optional transports validate an answer."""
    task = asyncio.ensure_future(_run_chat_with_retry(prompt, req, model_type))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=_KEEPALIVE_SECONDS)
            if task in done:
                break
            yield ": keep-alive\n\n"
        try:
            reply = task.result()
        except LoginRequired as exc:
            yield _sse_error(str(exc), "login_required")
            return
        except ProviderRejected as exc:
            yield _sse_error(_provider_pause_message(exc, req.model), exc.code)
            return
        except Exception as exc:
            yield _sse_error(_completion_failure_message(exc))
            return
        for frame in sse_frames(req.model, reply.text, None, reply.conversation_id,
                                finish_reason=reply.finish_reason):
            yield frame
    finally:
        if not task.done():
            task.cancel()
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _request_gate, _qwen_request_gate, _stop_refresh, _provider_request_gates
    _request_gate = asyncio.Lock()
    _qwen_request_gate = asyncio.Lock()
    _provider_request_gates = {name: asyncio.Lock() for name in PROVIDERS}
    _stop_refresh = threading.Event()
    stop_event = _stop_refresh
    # Background browser recapture is retired, including existing .env opt-ins.
    try:
        yield
    finally:
        stop_event.set()


app = FastAPI(title="Account providers OpenAI-compatible API", version="0.2.0", lifespan=lifespan)
app.include_router(browser_router)
from server.budgets import router as budget_router
app.include_router(budget_router)
from server.session_transfer import router as session_transfer_router
app.include_router(session_transfer_router)
install_rate_limit(app, RateLimiter(limit=RATE_LIMIT_PER_MINUTE, window=60.0))
# Added last, so it runs first: unauthenticated callers cannot spend the rate limit.
from server.api_auth import install as install_api_auth
install_api_auth(app)


def _error(message: str, status: int = 500, err_type: str = "server_error"):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "code": err_type, "retryable": False}},
        headers={"x-should-retry": "false"},
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/v1/models")
def list_models():
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": created, "owned_by": model_provider(name)}
            for name in advertised_model_ids()
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request = None):
    # StreamingResponse owns disconnect monitoring after headers. Direct Python
    # callers do not have an ASGI transport to monitor.
    if request is None or req.stream:
        return await _chat_completions(req)
    stopping = asyncio.Event()
    async def disconnected():
        # The body has already been parsed by FastAPI. Await the next ASGI
        # event instead of polling is_disconnected: its immediately-cancelled
        # scope can lose disconnects behind BaseHTTPMiddleware task groups.
        while not stopping.is_set():
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return
    work = asyncio.create_task(_chat_completions(req))
    watch = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait({work, watch}, return_when=asyncio.FIRST_COMPLETED)
        if watch in done:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            return _error("Client disconnected", status=499, err_type="client_disconnected")
        return await work
    finally:
        # Stop and join both tasks, including through the middleware's
        # cancellation scopes. No orphaned receive or provider worker remains.
        stopping.set()
        watch.cancel()
        if not work.done():
            work.cancel()
        await asyncio.gather(work, watch, return_exceptions=True)


async def _chat_completions(req: ChatCompletionRequest):
    if not req.messages:
        return _error("`messages` must not be empty", status=400, err_type="invalid_request_error")

    try:
        validate_tool_history(req.messages)
    except ValueError as exc:
        return _error(str(exc), status=400, err_type="invalid_request_error")

    if not is_known_model(req.model):
        return _error(
            f"The model `{req.model}` does not exist. Available models: "
            f"{', '.join(advertised_model_ids())}",
            status=404, err_type="model_not_found",
        )

    try:
        # A live request in this process may be queued behind; abandoned or
        # external attempts and real pauses must still fail before any factory.
        await _run_worker(guard.check, model_provider(req.model), None, True)
    except ProviderRejected as exc:
        return _provider_rejected_response(exc, req.model)

    if req.conversation_id:
        try:
            provider = model_provider(req.model)
            if provider == "qwen" and not qwen_uses_browser():
                decode_qwen_cid(req.conversation_id)
            elif provider in PROVIDERS:
                decode_provider_cid(req.conversation_id, provider)
            else:
                decode_deepseek_cid(req.conversation_id)
        except ValueError as exc:
            return _error(str(exc), status=400, err_type="invalid_request_error")

    try:
        names = _tool_names(req.tools)
        if req.tools and len(names) != len(req.tools):
            raise ValueError("Tools must have distinct, non-empty function names")
        validate_tool_choice(req.tool_choice, names)
    except ValueError as exc:
        return _error(str(exc), status=400, err_type="invalid_request_error")

    # A thread's model is fixed when it's created, so on resume we ignore `model`
    # (the OpenAI SDK always sends one) and let the existing thread's model stand.
    model_type = None if req.conversation_id else resolve_model_type(req.model)
    prompt = messages_to_prompt(
        req.messages, req.tools, req.tool_choice,
        tools_continuation=bool(req.conversation_id),
        provider=model_provider(req.model),
    )

    # Tool calls and all account providers use conversation_cache so stateless
    # OpenAI clients resume the same upstream/browser thread. Buffer the reply
    # (tools and web providers validate before exposing text), then emit SSE.
    # The provider worker latches upstream failures before exposing the result.
    if req.stream:
        return StreamingResponse(
            _tool_stream(req, prompt, model_type),
            media_type="text/event-stream",
        )
    try:
        reply, content, tool_calls = await _run_tool_chat(prompt, req, model_type)
    except LoginRequired as e:
        return _error(str(e), status=401, err_type="login_required")
    except ToolCallError as e:
        return _error(str(e), status=400, err_type="invalid_tool_response")
    except ProviderRejected as e:
        return _provider_rejected_response(e, req.model)
    except Exception as e:
        return _error(_completion_failure_message(e), status=400)

    return completion_response(
        req.model, content, prompt, reply.conversation_id, tool_calls,
        finish_reason=reply.finish_reason,
    )
