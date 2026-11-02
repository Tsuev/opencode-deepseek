"""
OpenAI-compatible FastAPI server for DeepSeek.

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

Requests under /v1 are rate limited per client IP (default 10/min, set via
RATE_LIMIT_PER_MINUTE); /healthz is exempt. Upstream traffic is additionally
paced, budgeted and paused on refusal by `server.safety`, and OpenAI clients
reuse their DeepSeek conversation via `server.conversations`.

Sessions expire. To survive that, this module:
  * rebuilds the shared client once and retries the request if DeepSeek rejects
    the token (auth error), and
  * runs a background refresher that re-captures the token from the persistent
    browser profile every SESSION_REFRESH_INTERVAL seconds.
If a refresh can't recover the session, the endpoint returns a clear 503 instead
of blocking on an interactive login window.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import threading
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from deepseek.auth import SESSION_MAX_AGE, LoginRequired, get_session
from deepseek.client import DeepSeekClient, Reply

from .config import (
    CONVERSATION_MAX_ENTRIES,
    CONVERSATION_REUSE,
    CONVERSATION_TTL,
    DEEPSEEK_COOLDOWN_SECONDS,
    DEEPSEEK_JITTER,
    DEEPSEEK_MAX_GENERATIONS_PER_DAY,
    DEEPSEEK_MAX_GENERATIONS_PER_HOUR,
    DEEPSEEK_MAX_NEW_CHATS_PER_DAY,
    DEEPSEEK_MAX_NEW_CHATS_PER_HOUR,
    DEEPSEEK_MIN_INTERVAL,
    MODEL_MAP,
    RATE_LIMIT_PER_MINUTE,
    REFRESH_BROWSER_CHANNEL,
    REFRESH_BROWSER_CHANNEL_FALLBACK,
    SERVER_INTERACTIVE_LOGIN,
    SESSION_REFRESH_ENABLED,
    SESSION_REFRESH_INTERVAL,
    is_known_model,
    resolve_model_type,
)
from .conversations import ConversationStore
from .openai_format import (
    completion_response,
    looks_truncated,
    messages_to_prompt,
    parse_tool_calls,
    sse_frames,
    stream_chunks,
)
from .ratelimit import RateLimiter, install_rate_limit
from .safety import (
    ROOT as SAFETY_ROOT,
    BudgetExceeded,
    BudgetGuard,
    Cooldown,
    CooldownActive,
    UpstreamGate,
    classify_error,
)
from .schemas import ChatCompletionRequest

load_dotenv()

# One shared client (and its signed-in session) built lazily on first use.
_client: DeepSeekClient | None = None
_client_lock = threading.Lock()
_stop_refresh = threading.Event()

_MISSING = object()

# Account-safety guards (see server/safety.py). They are process-wide because
# the bridge shares one DeepSeek account and one session.
_gate = UpstreamGate(DEEPSEEK_MIN_INTERVAL, DEEPSEEK_JITTER)
_budget = BudgetGuard(
    SAFETY_ROOT / "session" / "usage.json",
    {
        "chat": (DEEPSEEK_MAX_NEW_CHATS_PER_HOUR, DEEPSEEK_MAX_NEW_CHATS_PER_DAY),
        "generation": (
            DEEPSEEK_MAX_GENERATIONS_PER_HOUR,
            DEEPSEEK_MAX_GENERATIONS_PER_DAY,
        ),
    },
)
_cooldown = Cooldown(
    SAFETY_ROOT / "session" / "cooldown.json", DEEPSEEK_COOLDOWN_SECONDS
)
_conversations = ConversationStore(
    SAFETY_ROOT / "session" / "conversations.json",
    enabled=CONVERSATION_REUSE,
    ttl=CONVERSATION_TTL,
    max_entries=CONVERSATION_MAX_ENTRIES,
)


def _is_auth_error(exc: Exception) -> bool:
    """Only auth failures are worth retrying after a session refresh.

    A 403 is often DeepSeek's anti-automation refusal, not an expired token.
    Retrying those is exactly what turns a temporary block into a longer one,
    so classify_error() only reports ``auth`` for genuine token problems.
    """
    return classify_error(exc) == "auth"


def _raise_if_refused(exc: Exception) -> None:
    """Latch a local cooldown when DeepSeek refuses us for risk/rate reasons."""
    kind = classify_error(exc)
    if kind not in ("refusal", "rate_limit"):
        return
    reason = (
        "rate limit" if kind == "rate_limit" else "possible automation check"
    )
    _cooldown.latch(reason)
    if DEEPSEEK_COOLDOWN_SECONDS > 0:
        detail = (
            f"DeepSeek refused the request ({reason}). The bridge is paused "
            f"locally for {DEEPSEEK_COOLDOWN_SECONDS}s to avoid turning a "
            "temporary block into a longer one."
        )
    else:
        detail = f"DeepSeek refused the request ({reason})."
    raise CooldownActive(detail, retry_after=float(DEEPSEEK_COOLDOWN_SECONDS))


def _gated_chat(client: DeepSeekClient, prompt: str,
                req: ChatCompletionRequest, model_type):
    """Run one generation behind the pacing gate and the cooldown check."""
    with _gate.turn():
        _cooldown.ensure()
        return client.chat(
            prompt, req.conversation_id, model_type, req.thinking, req.search
        )


def _remember(req: ChatCompletionRequest, conversation_id) -> None:
    _conversations.remember(req, conversation_id)


def _tool_names(tools) -> set:
    """Extract requested tool names from OpenAI tool objects, defensively."""
    names = set()
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function", t)
        if isinstance(fn, dict) and fn.get("name"):
            names.add(fn["name"])
    return names


def _debug_toolcalls() -> bool:
    return os.getenv("DEBUG_TOOLCALLS", "").strip().lower() not in (
        "", "0", "false", "no", "off",
    )


def _build_client(force: bool = False) -> DeepSeekClient:
    """Resolve a session (cached → headless refresh) and wrap it in a client.

    `force=True` ignores the cached session file and re-captures from the browser
    profile — used after DeepSeek rejects the current token, since the file can
    look "fresh" (age < SESSION_MAX_AGE) while the token is already dead."""
    session = get_session(
        max_age=0 if force else SESSION_MAX_AGE,
        allow_interactive=SERVER_INTERACTIVE_LOGIN,
        channel=REFRESH_BROWSER_CHANNEL,
        fallback_channel=REFRESH_BROWSER_CHANNEL_FALLBACK,
    )
    return DeepSeekClient(session=session)


def get_client(force_refresh: bool = False) -> DeepSeekClient:
    """Build (once) the shared client and its signed-in session.

    Session resolution: cached file → headless capture off the persistent
    profile. If neither works and SERVER_INTERACTIVE_LOGIN is on, it opens a
    visible browser window so you can sign in — the triggering request blocks
    until you finish. If interactive login is off, it raises `LoginRequired`,
    which the endpoint turns into an actionable 503.

    `force_refresh=True` rebuilds even if a client is cached, re-capturing the
    token from the browser profile.

    This touches Playwright's sync API, so callers must invoke it OFF the event
    loop (via run_in_threadpool); calling it inside the asyncio loop raises
    "Playwright Sync API inside the asyncio loop"."""
    global _client
    if _client is None or force_refresh:
        with _client_lock:
            if _client is None or force_refresh:
                _client = _build_client(force=force_refresh)
    return _client


def reset_client() -> None:
    """Drop the cached client so the next get_client() rebuilds the session.

    The old client is intentionally not closed synchronously: an in-flight
    request may still hold a reference to it. Leaking one httpx pool is
    negligible; closing it early would break that request."""
    global _client
    with _client_lock:
        _client = None


async def _run_chat_with_retry(prompt: str, req: ChatCompletionRequest, model_type):
    """Run a full (non-streaming) completion, refreshing the session once on an
    auth error. Raises `LoginRequired` if the rebuilt session can't be resolved."""
    client = await run_in_threadpool(get_client)
    try:
        return await run_in_threadpool(_gated_chat, client, prompt, req, model_type)
    except Exception as e:
        _raise_if_refused(e)
        if not _is_auth_error(e):
            raise
        print(f"[retry] DeepSeek rejected the session ({e}); refreshing...", flush=True)
        client = await run_in_threadpool(get_client, True)
        return await run_in_threadpool(_gated_chat, client, prompt, req, model_type)


# A truncated tool reply is continued this many times before giving up. Each
# round-trip resumes the same DeepSeek thread and appends the next slice.
_MAX_CONTINUATIONS = int(os.getenv("TOOLCALL_MAX_CONTINUATIONS", "3"))

# Sent to make DeepSeek finish an answer its output limit cut off.
_CONTINUE_PROMPT = (
    "Your previous message was cut off by the output limit before it finished. "
    "Continue EXACTLY from the character where it stopped. Output only the "
    "remaining characters — no preamble, no apology, no repetition, and no "
    "opening code fence (only the missing closing one, if that is what was cut)."
)


async def _continue_reply(prev: Reply, req: ChatCompletionRequest, model_type):
    """Resume the same DeepSeek thread and return the next slice of the reply.

    On resume the thread keeps its own model, so `model` is passed as None —
    matching the resume rule in `deepseek.client.stream`."""
    cont_req = req.model_copy(update={"conversation_id": prev.conversation_id})
    client = await run_in_threadpool(get_client)
    try:
        return await run_in_threadpool(
            _gated_chat, client, _CONTINUE_PROMPT, cont_req, None
        )
    except Exception as e:
        _raise_if_refused(e)
        if not _is_auth_error(e):
            raise
        client = await run_in_threadpool(get_client, True)
        return await run_in_threadpool(
            _gated_chat, client, _CONTINUE_PROMPT, cont_req, None
        )


async def _run_tool_chat(prompt: str, req: ChatCompletionRequest, model_type):
    """Full completion for a tool request, transparently continuing truncations.

    DeepSeek's web output limit can cut a large `write`/`edit` tool call off
    mid-JSON. Rather than hand opencode a `finish_reason: "stop"` — which ends
    the agent's turn mid-task — we detect the cut and ask DeepSeek to continue
    from where it stopped, concatenating slices until the call parses. Returns
    `(reply, content, tool_calls)`."""
    names = _tool_names(req.tools)
    reply = await _run_chat_with_retry(prompt, req, model_type)

    for attempt in range(_MAX_CONTINUATIONS):
        content, tool_calls = parse_tool_calls(reply.text, allowed_names=names)
        if tool_calls or not looks_truncated(reply.text, names):
            return reply, content, tool_calls
        print(
            f"[toolcalls] reply truncated (attempt {attempt + 1}/"
            f"{_MAX_CONTINUATIONS}); requesting continuation...",
            flush=True,
        )
        try:
            _budget.reserve(new_chat=False)
        except BudgetExceeded as e:
            print(f"[toolcalls] continuation skipped: {e}", flush=True)
            break
        try:
            nxt = await _continue_reply(reply, req, model_type)
        except Exception as e:
            print(f"[toolcalls] continuation failed: {e}", flush=True)
            break
        reply = Reply(text=reply.text + nxt.text, conversation_id=nxt.conversation_id)

    content, tool_calls = parse_tool_calls(reply.text, allowed_names=names)
    if tool_calls is None and _debug_toolcalls():
        print(f"[toolcalls] no call parsed; raw reply:\n{reply.text}\n", flush=True)
    return reply, content, tool_calls


def _open_stream(client: DeepSeekClient, prompt: str, req: ChatCompletionRequest, model_type):
    return client.stream(
        prompt, conversation_id=req.conversation_id,
        model=model_type, thinking=req.thinking, search=req.search,
    )


def _sse_error(message: str, err_type: str = "server_error") -> str:
    obj = {"error": {"message": message, "type": err_type}}
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\ndata: [DONE]\n\n"


# How often to emit an SSE keep-alive comment while a buffered tool reply is
# still generating. Must stay below the client's idle read timeout.
_KEEPALIVE_SECONDS = 10.0


async def _tool_stream(req: ChatCompletionRequest, prompt: str, model_type,
                       cache_req: ChatCompletionRequest = None):
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
        except (CooldownActive, BudgetExceeded) as e:
            yield _sse_error(str(e), "rate_limit_error")
            return
        except Exception as e:
            yield _sse_error(f"DeepSeek request failed: {e}")
            return

        _remember(cache_req or req, reply.conversation_id)
        for frame in sse_frames(req.model, content, tool_calls, reply.conversation_id):
            yield frame
    finally:
        if not task.done():
            task.cancel()


def _stream_with_retry(client: DeepSeekClient, prompt: str,
                       req: ChatCompletionRequest, model_type,
                       cache_req: ChatCompletionRequest = None):
    """Stream a plain-text completion behind the safety gate.

    Refreshes the session once if the very first chunk fails with an auth error
    (before any bytes were sent). The gate stays held for the whole stream, so
    parallel requests cannot hammer the account.
    """
    try:
        with _gate.turn():
            _cooldown.ensure()
            stream = _open_stream(client, prompt, req, model_type)
            it = iter(stream)
            try:
                first = next(it, _MISSING)
            except Exception as e:
                _raise_if_refused(e)
                if not _is_auth_error(e):
                    raise
                print(f"[retry] DeepSeek rejected the stream ({e}); refreshing...",
                      flush=True)
                try:
                    client = get_client(force_refresh=True)
                except Exception as e2:
                    yield _sse_error(f"Session refresh failed: {e2}", "login_required")
                    return
                stream = _open_stream(client, prompt, req, model_type)
                it = iter(stream)
                try:
                    first = next(it, _MISSING)
                except Exception as e3:
                    _raise_if_refused(e3)
                    yield _sse_error(f"DeepSeek request failed: {e3}")
                    return

            iterator = it if first is _MISSING else itertools.chain([first], it)
            yield from stream_chunks(req.model, stream, iterator=iterator)
            _remember(cache_req or req, getattr(stream, "conversation_id", None))
    except (CooldownActive, BudgetExceeded) as e:
        yield _sse_error(str(e), "rate_limit_error")
    except Exception as e:
        kind = classify_error(e)
        if kind in ("refusal", "rate_limit"):
            reason = "rate limit" if kind == "rate_limit" else "possible automation check"
            _cooldown.latch(reason)
            yield _sse_error(
                f"DeepSeek refused the request ({reason}). The bridge is paused "
                "locally to avoid a longer block.",
                "rate_limit_error",
            )
        else:
            yield _sse_error(f"DeepSeek request failed: {e}")


def _refresh_loop() -> None:
    """Daemon loop: re-capture the token from the browser profile every interval.

    Uses max_age=0 to force a real (headless) capture each cycle rather than
    returning the cached file, and allow_interactive=False so it never opens a
    window."""
    while not _stop_refresh.wait(SESSION_REFRESH_INTERVAL):
        try:
            get_session(
                max_age=0,
                allow_interactive=False,
                channel=REFRESH_BROWSER_CHANNEL,
                fallback_channel=REFRESH_BROWSER_CHANNEL_FALLBACK,
            )
            reset_client()
            print(f"[refresh] session refreshed at {time.strftime('%H:%M:%S')}", flush=True)
        except LoginRequired:
            print("[refresh] refresh failed: login required (cookies expired?)", flush=True)
        except Exception as e:
            print(f"[refresh] refresh error: {e}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    thread = None
    if SESSION_REFRESH_ENABLED:
        thread = threading.Thread(target=_refresh_loop, name="session-refresher", daemon=True)
        thread.start()
        print(f"[refresh] background refresher started (every {SESSION_REFRESH_INTERVAL}s, "
              f"channel={REFRESH_BROWSER_CHANNEL})", flush=True)
    try:
        yield
    finally:
        _stop_refresh.set()
        if thread is not None:
            thread.join(timeout=5)


app = FastAPI(title="DeepSeek OpenAI-compatible API", version="0.1.0", lifespan=lifespan)
install_rate_limit(app, RateLimiter(limit=RATE_LIMIT_PER_MINUTE, window=60.0))


def _error(message: str, status: int = 500, err_type: str = "server_error"):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type}},
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
            {"id": name, "object": "model", "created": created, "owned_by": "deepseek"}
            for name in MODEL_MAP
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    if not req.messages:
        return _error("`messages` must not be empty", status=400, err_type="invalid_request_error")

    if not is_known_model(req.model):
        return _error(
            f"The model `{req.model}` does not exist. Available models: "
            f"{', '.join(MODEL_MAP)}",
            status=404, err_type="model_not_found",
        )

    # OpenAI clients cannot send our custom conversation_id, so recover it from
    # the resent history. On a match, send only the new tail to DeepSeek instead
    # of creating a new chat and re-pasting the whole transcript.
    cache_req = req
    effective_req = req
    if CONVERSATION_REUSE and not req.conversation_id:
        match = _conversations.match(req)
        if match is not None:
            recovered_id, tail = match
            # Keep the current system prompt (it carries agent instructions and
            # the tool-discipline plugin) but drop the already-known history.
            system = [m for m in req.messages if m.role == "system"]
            effective_req = req.model_copy(
                update={
                    "conversation_id": recovered_id,
                    "messages": [*system, *tail],
                }
            )
            print(
                f"[conversations] resumed {recovered_id.split(':')[0]} "
                f"({len(tail)} new message(s))",
                flush=True,
            )

    is_new_chat = not effective_req.conversation_id
    # A thread's model is fixed when it's created, so on resume we ignore `model`
    # (the OpenAI SDK always sends one) and let the existing thread's model stand.
    model_type = resolve_model_type(req.model) if is_new_chat else None
    prompt = messages_to_prompt(
        effective_req.messages, effective_req.tools, effective_req.tool_choice
    )

    # Stop before touching the account when paused or over a local budget.
    try:
        _cooldown.ensure()
        _budget.reserve(new_chat=is_new_chat)
    except (CooldownActive, BudgetExceeded) as e:
        return _error(str(e), status=429, err_type="rate_limit_error")

    # Tool calls are emulated by buffering the reply and parsing it, so we use the
    # non-streaming call and then emit SSE frames when the client asked to stream.
    # That also lets us retry on an auth error and still return a clean status.
    if effective_req.tools:
        if effective_req.stream:
            return StreamingResponse(
                _tool_stream(effective_req, prompt, model_type, cache_req),
                media_type="text/event-stream",
            )
        try:
            reply, content, tool_calls = await _run_tool_chat(
                prompt, effective_req, model_type
            )
        except LoginRequired as e:
            return _error(str(e), status=503, err_type="login_required")
        except (CooldownActive, BudgetExceeded) as e:
            return _error(str(e), status=429, err_type="rate_limit_error")
        except Exception as e:
            return _error(f"DeepSeek request failed: {e}")

        _remember(cache_req, reply.conversation_id)
        return completion_response(
            effective_req.model, content, prompt, reply.conversation_id, tool_calls
        )

    # Plain chat: real incremental streaming, or a buffered retryable call.
    if effective_req.stream:
        try:
            client = await run_in_threadpool(get_client)
        except LoginRequired as e:
            return _error(str(e), status=503, err_type="login_required")
        except Exception as e:
            return _error(f"Failed to initialise DeepSeek session: {e}")

        def gen():
            yield from _stream_with_retry(
                client, prompt, effective_req, model_type, cache_req
            )

        return StreamingResponse(gen(), media_type="text/event-stream")

    try:
        reply = await _run_chat_with_retry(prompt, effective_req, model_type)
    except LoginRequired as e:
        return _error(str(e), status=503, err_type="login_required")
    except (CooldownActive, BudgetExceeded) as e:
        return _error(str(e), status=429, err_type="rate_limit_error")
    except Exception as e:
        return _error(f"DeepSeek request failed: {e}")

    _remember(cache_req, reply.conversation_id)
    return completion_response(
        effective_req.model, reply.text, prompt, reply.conversation_id
    )
