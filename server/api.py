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

Requests under /v1 are rate limited per client IP (default 30/min, set via
RATE_LIMIT_PER_MINUTE); /healthz is exempt.

Sessions expire. To survive that, this module:
  * rebuilds the shared client once and retries the request if DeepSeek rejects
    the token (auth error), and
  * runs a background refresher that re-captures the token from the persistent
    browser profile every SESSION_REFRESH_INTERVAL seconds.
If a refresh can't recover the session, the endpoint returns a clear 503 instead
of blocking on an interactive login window.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from deepseek.auth import SESSION_MAX_AGE, LoginRequired, get_session
from deepseek.client import DeepSeekClient

from .config import (
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
from .openai_format import (
    completion_response,
    messages_to_prompt,
    parse_tool_calls,
    sse_frames,
    stream_chunks,
)
from .ratelimit import RateLimiter, install_rate_limit
from .schemas import ChatCompletionRequest

load_dotenv()

# One shared client (and its signed-in session) built lazily on first use.
_client: DeepSeekClient | None = None
_client_lock = threading.Lock()
_stop_refresh = threading.Event()

_MISSING = object()

# Substrings that mark a DeepSeek rejection as an auth/session problem (worth a
# session refresh + one retry). Deliberately broad: we only retry once, so a
# false positive costs a single extra attempt.
_AUTH_HINTS = (
    "auth", "unauthorized", "forbidden", "login", "token",
    "session", "credential", "expired", "not signed",
)


def _is_auth_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (401, 403)
    msg = str(exc).lower()
    return any(h in msg for h in _AUTH_HINTS)


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
        return await run_in_threadpool(
            client.chat, prompt, req.conversation_id,
            model_type, req.thinking, req.search,
        )
    except Exception as e:
        if not _is_auth_error(e):
            raise
        print(f"[retry] DeepSeek rejected the session ({e}); refreshing...", flush=True)
        client = await run_in_threadpool(get_client, True)
        return await run_in_threadpool(
            client.chat, prompt, req.conversation_id,
            model_type, req.thinking, req.search,
        )


def _open_stream(client: DeepSeekClient, prompt: str, req: ChatCompletionRequest, model_type):
    return client.stream(
        prompt, conversation_id=req.conversation_id,
        model=model_type, thinking=req.thinking, search=req.search,
    )


def _sse_error(message: str, err_type: str = "server_error") -> str:
    obj = {"error": {"message": message, "type": err_type}}
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\ndata: [DONE]\n\n"


def _stream_with_retry(client: DeepSeekClient, prompt: str,
                       req: ChatCompletionRequest, model_type):
    """Stream a plain-text completion, refreshing the session once if the very
    first chunk fails with an auth error (before any bytes were sent)."""
    stream = _open_stream(client, prompt, req, model_type)
    it = iter(stream)
    try:
        first = next(it, _MISSING)
    except Exception as e:
        if not _is_auth_error(e):
            raise
        print(f"[retry] DeepSeek rejected the stream ({e}); refreshing...", flush=True)
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
            yield _sse_error(f"DeepSeek request failed: {e3}")
            return

    iterator = it if first is _MISSING else itertools.chain([first], it)
    yield from stream_chunks(req.model, stream, iterator=iterator)


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

    # A thread's model is fixed when it's created, so on resume we ignore `model`
    # (the OpenAI SDK always sends one) and let the existing thread's model stand.
    model_type = None if req.conversation_id else resolve_model_type(req.model)
    prompt = messages_to_prompt(req.messages, req.tools, req.tool_choice)

    # Tool calls are emulated by buffering the reply and parsing it, so we use the
    # non-streaming call and then emit SSE frames when the client asked to stream.
    # That also lets us retry on an auth error and still return a clean status.
    if req.tools:
        try:
            reply = await _run_chat_with_retry(prompt, req, model_type)
        except LoginRequired as e:
            return _error(str(e), status=503, err_type="login_required")
        except Exception as e:
            return _error(f"DeepSeek request failed: {e}")

        content, tool_calls = parse_tool_calls(reply.text, allowed_names=_tool_names(req.tools))
        if tool_calls is None and _debug_toolcalls():
            print(f"[toolcalls] no call parsed; raw reply:\n{reply.text}\n", flush=True)
        if req.stream:
            return StreamingResponse(
                sse_frames(req.model, content, tool_calls, reply.conversation_id),
                media_type="text/event-stream",
            )
        return completion_response(
            req.model, content, prompt, reply.conversation_id, tool_calls
        )

    # Plain chat: real incremental streaming, or a buffered retryable call.
    if req.stream:
        try:
            client = await run_in_threadpool(get_client)
        except LoginRequired as e:
            return _error(str(e), status=503, err_type="login_required")
        except Exception as e:
            return _error(f"Failed to initialise DeepSeek session: {e}")

        def gen():
            yield from _stream_with_retry(client, prompt, req, model_type)

        return StreamingResponse(gen(), media_type="text/event-stream")

    try:
        reply = await _run_chat_with_retry(prompt, req, model_type)
    except LoginRequired as e:
        return _error(str(e), status=503, err_type="login_required")
    except Exception as e:
        return _error(f"DeepSeek request failed: {e}")

    return completion_response(req.model, reply.text, prompt, reply.conversation_id)
