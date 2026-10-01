"""
OpenAI-compatible FastAPI server for DeepSeek and optional Qwen Chat.

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
  * rebuilds the provider's client once and retries if it rejects
    the token (auth error), and
  * refreshes DeepSeek in the background every SESSION_REFRESH_INTERVAL seconds;
    Qwen's short-lived token is checked and refreshed before each request.
If a refresh can't recover the session, the endpoint returns a clear 503 instead
of blocking on an interactive login window.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar

import anyio
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from deepseek.auth import SESSION_MAX_AGE, LoginRequired, get_session
from chat_protocol import Reply
from deepseek.client import DeepSeekClient
from qwen.auth import get_session as get_qwen_session
from qwen.client import QwenClient, _decode_cid as decode_qwen_cid

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
    model_provider,
)
from .openai_format import (
    completion_response,
    looks_truncated,
    messages_to_prompt,
    parse_tool_calls,
    sse_frames,
    stream_chunks,
    ToolCallError,
    enforce_tool_choice,
    validate_tool_choice,
)
from .ratelimit import RateLimiter, install_rate_limit
from .schemas import ChatCompletionRequest

# One shared client (and its signed-in session) built lazily on first use.
_client: DeepSeekClient | None = None
_client_lock = threading.Lock()
_stop_refresh = threading.Event()
_request_gate = asyncio.Lock()
_qwen_request_gate = asyncio.Lock()
_qwen_client: QwenClient | None = None
_qwen_client_lock = threading.Lock()

_MISSING = object()
_worker_cancellation = ContextVar("worker_cancellation", default=None)


def _check_cancelled():
    signal = _worker_cancellation.get()
    if signal is not None and signal.is_set():
        raise asyncio.CancelledError()

# Substrings that mark an upstream rejection as an auth/session problem (worth a
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
        if isinstance(fn, dict) and isinstance(fn.get("name"), str) and fn["name"]:
            names.add(fn["name"])
    return names


def _debug_toolcalls() -> bool:
    return os.getenv("DEBUG_TOOLCALLS", "").strip().lower() not in (
        "", "0", "false", "no", "off",
    )


def _build_client(force: bool = False, allow_interactive=None) -> DeepSeekClient:
    """Resolve a session (cached → headless refresh) and wrap it in a client.

    `force=True` ignores the cached session file and re-captures from the browser
    profile — used after DeepSeek rejects the current token, since the file can
    look "fresh" (age < SESSION_MAX_AGE) while the token is already dead."""
    session = get_session(
        max_age=0 if force else SESSION_MAX_AGE,
        allow_interactive=SERVER_INTERACTIVE_LOGIN if allow_interactive is None else allow_interactive,
        channel=REFRESH_BROWSER_CHANNEL,
        fallback_channel=REFRESH_BROWSER_CHANNEL_FALLBACK,
    )
    return DeepSeekClient(session=session)


def get_client(force_refresh: bool = False, rejected_client=None) -> DeepSeekClient:
    """Build (once) the shared client and its signed-in session.

    Session resolution: cached file → headless capture off the persistent
    profile. If neither works and SERVER_INTERACTIVE_LOGIN is on, it opens a
    visible browser window so you can sign in — the triggering request blocks
    until you finish. If interactive login is off, it raises `LoginRequired`,
    which the endpoint turns into an actionable 503.

    `force_refresh=True` re-captures the token. If `rejected_client` was already
    replaced by another refresh, reuse that replacement instead of capturing again.

    This touches Playwright's sync API, so callers must invoke it OFF the event
    loop (via run_in_threadpool); calling it inside the asyncio loop raises
    "Playwright Sync API inside the asyncio loop"."""
    global _client
    with _client_lock:
        if _client is None or (force_refresh and (
            rejected_client is None or _client is rejected_client
        )):
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


def get_qwen_client(force_refresh=False, rejected_client=None) -> QwenClient:
    """Refresh Qwen before expiry, or once after rejection, off the event loop.

    Retain a replaced pool while any in-flight request still references it,
    matching get_client()'s ownership rule.
    """
    global _qwen_client
    with _qwen_client_lock:
        if (_qwen_client is None or not _qwen_client.session.usable
                or (force_refresh and (rejected_client is None or _qwen_client is rejected_client))):
            session = get_qwen_session(force=force_refresh, allow_interactive=SERVER_INTERACTIVE_LOGIN,
                                       channel=REFRESH_BROWSER_CHANNEL,
                                       fallback_channel=REFRESH_BROWSER_CHANNEL_FALLBACK)
            _qwen_client = QwenClient(session)
        return _qwen_client


def _request_client(req, force_refresh=False, rejected_client=None):
    factory = get_qwen_client if model_provider(req.model) == "qwen" else get_client
    if force_refresh:
        return factory(True, rejected_client=rejected_client)
    return factory()


def _request_queue(req):
    return _qwen_request_gate if model_provider(req.model) == "qwen" else _request_gate


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
    """Run a queued completion with one retry after an auth rejection."""
    return await _run_queued(_chat_with_retry, prompt, req, model_type, gate=_request_queue(req))


def _chat_with_retry(prompt: str, req: ChatCompletionRequest, model_type):
    _check_cancelled()
    client = _request_client(req)
    try:
        _check_cancelled()
        return client.chat(prompt, req.conversation_id, model_type, req.thinking, req.search)
    except Exception as e:
        _check_cancelled()
        if not _is_auth_error(e):
            raise
        print(f"[retry] {model_provider(req.model)} rejected the session ({type(e).__name__}); refreshing...", flush=True)
        client = _request_client(req, True, rejected_client=client)
        _check_cancelled()
        return client.chat(prompt, req.conversation_id, model_type, req.thinking, req.search)


# A truncated tool reply is continued this many times before giving up. Each
# round-trip resumes the same provider conversation and appends the next slice.
_MAX_CONTINUATIONS = int(os.getenv("TOOLCALL_MAX_CONTINUATIONS", "3"))
if _MAX_CONTINUATIONS < 0:
    raise ValueError("TOOLCALL_MAX_CONTINUATIONS must be non-negative")

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
    return await _run_queued(_tool_chat_with_retry, prompt, req, model_type, gate=_request_queue(req))


def _tool_chat_with_retry(prompt: str, req: ChatCompletionRequest, model_type):
    """Full completion for a tool request, transparently continuing truncations.

    A web chat's output limit can cut a large `write`/`edit` tool call off
    mid-JSON. Rather than hand opencode a `finish_reason: "stop"` — which ends
    the agent's turn mid-task — we detect the cut and ask the model to continue
    from where it stopped, concatenating slices until the call parses. Returns
    `(reply, content, tool_calls)`."""
    names = _tool_names(req.tools)
    reply = _chat_with_retry(prompt, req, model_type)

    for attempt in range(_MAX_CONTINUATIONS + 1):
        _check_cancelled()
        if not looks_truncated(reply.text, names):
            try:
                content, tool_calls = parse_tool_calls(reply.text, allowed_names=names)
                enforce_tool_choice(req.tool_choice, tool_calls)
            except ToolCallError:
                if _debug_toolcalls():
                    print(f"[toolcalls] invalid explicit reply:\n{reply.text}\n", flush=True)
                raise
            if tool_calls is None and _debug_toolcalls():
                print(f"[toolcalls] no call parsed; raw reply:\n{reply.text}\n", flush=True)
            return reply, content, tool_calls
        if req.tool_choice == "none":
            raise ToolCallError("The model started a tool call while tool_choice='none'")
        if attempt == _MAX_CONTINUATIONS:
            raise ToolCallError("The tool reply is still incomplete after the continuation limit")
        print(
            f"[toolcalls] reply truncated (attempt {attempt + 1}/"
            f"{_MAX_CONTINUATIONS}); requesting continuation...",
            flush=True,
        )
        try:
            nxt = _continue_reply(reply, req)
        except LoginRequired:
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
    obj = {"error": {"message": message, "type": err_type}}
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\ndata: [DONE]\n\n"


# How often to emit an SSE keep-alive comment while a buffered tool reply is
# still generating. Must stay below the client's idle read timeout.
_KEEPALIVE_SECONDS = 10.0


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
        except Exception as e:
            yield _sse_error(f"{model_provider(req.model)} request failed: {e}")
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
    """Stream a plain-text completion, refreshing the session once if the very
    first chunk fails with an auth error (before any bytes were sent)."""
    for attempt in range(2):
        iterator = None
        try:
            _check_cancelled()
            stream = _open_stream(client, prompt, req, model_type)
            iterator = iter(stream)
            first = next(iterator, _MISSING)
        except Exception as exc:
            if iterator is not None and hasattr(iterator, "close"):
                iterator.close()
            _check_cancelled()
            if attempt == 0 and _is_auth_error(exc):
                try:
                    client = _request_client(req, True, rejected_client=client)
                except LoginRequired as refresh_error:
                    yield _sse_error(str(refresh_error), "login_required")
                    return
                except Exception as refresh_error:
                    yield _sse_error(f"Session refresh failed: {refresh_error}")
                    return
                continue
            error_type = "login_required" if isinstance(exc, LoginRequired) else "server_error"
            yield _sse_error(f"{model_provider(req.model)} request failed: {exc}", error_type)
            return

        def remaining():
            if first is not _MISSING:
                yield first
            yield from iterator

        try:
            yield from stream_chunks(req.model, stream, iterator=remaining())
        except Exception as exc:
            # Once content has been emitted, retrying would duplicate it.
            yield _sse_error(f"{model_provider(req.model)} request failed: {exc}")
        finally:
            if hasattr(iterator, "close"):
                iterator.close()
        return


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


def _refresh_loop(stop_event=None) -> None:
    """Daemon loop: re-capture the token from the browser profile every interval.

    Uses max_age=0 to force a real (headless) capture each cycle rather than
    returning the cached file, and allow_interactive=False so it never opens a
    window."""
    global _client
    stop_event = stop_event if stop_event is not None else _stop_refresh
    while not stop_event.wait(SESSION_REFRESH_INTERVAL):
        try:
            with _client_lock:
                _client = _build_client(force=True, allow_interactive=False)
            print(f"[refresh] session refreshed at {time.strftime('%H:%M:%S')}", flush=True)
        except LoginRequired:
            print("[refresh] refresh failed: login required (cookies expired?)", flush=True)
        except Exception as e:
            print(f"[refresh] refresh error: {e}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _request_gate, _qwen_request_gate, _stop_refresh
    _request_gate = asyncio.Lock()
    _qwen_request_gate = asyncio.Lock()
    _stop_refresh = threading.Event()
    stop_event = _stop_refresh
    thread = None
    if SESSION_REFRESH_ENABLED:
        thread = threading.Thread(target=_refresh_loop, args=(stop_event,),
                                  name="session-refresher", daemon=True)
        thread.start()
        print(f"[refresh] background refresher started (every {SESSION_REFRESH_INTERVAL}s, "
              f"channel={REFRESH_BROWSER_CHANNEL})", flush=True)
    try:
        yield
    finally:
        stop_event.set()
        if thread is not None:
            thread.join(timeout=5)


app = FastAPI(title="DeepSeek and Qwen OpenAI-compatible API", version="0.2.0", lifespan=lifespan)
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
            {"id": name, "object": "model", "created": created, "owned_by": model_provider(name)}
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

    if req.conversation_id:
        try:
            if model_provider(req.model) == "qwen":
                decode_qwen_cid(req.conversation_id)
            elif req.conversation_id.startswith("qwen:"):
                raise ValueError("A Qwen conversation cannot be resumed through DeepSeek")
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
    prompt = messages_to_prompt(req.messages, req.tools, req.tool_choice)

    # Tool calls are emulated by buffering the reply and parsing it, so we use the
    # non-streaming call and then emit SSE frames when the client asked to stream.
    # That also lets us retry on an auth error and still return a clean status.
    if req.tools:
        if req.stream:
            return StreamingResponse(
                _tool_stream(req, prompt, model_type),
                media_type="text/event-stream",
            )
        try:
            reply, content, tool_calls = await _run_tool_chat(prompt, req, model_type)
        except LoginRequired as e:
            return _error(str(e), status=503, err_type="login_required")
        except ToolCallError as e:
            return _error(str(e), status=502, err_type="invalid_tool_response")
        except Exception as e:
            return _error(f"{model_provider(req.model)} request failed: {e}")

        return completion_response(
            req.model, content, prompt, reply.conversation_id, tool_calls,
            finish_reason=reply.finish_reason,
        )

    # Plain chat: real incremental streaming, or a buffered retryable call.
    if req.stream:
        try:
            client = await _run_queued(_request_client, req, gate=_request_queue(req))
        except LoginRequired as e:
            return _error(str(e), status=503, err_type="login_required")
        except Exception as e:
            return _error(f"Failed to initialise {model_provider(req.model)} session: {e}")

        return StreamingResponse(_plain_stream(client, prompt, req, model_type),
                                 media_type="text/event-stream")

    try:
        reply = await _run_chat_with_retry(prompt, req, model_type)
    except LoginRequired as e:
        return _error(str(e), status=503, err_type="login_required")
    except Exception as e:
        return _error(f"{model_provider(req.model)} request failed: {e}")

    return completion_response(req.model, reply.text, prompt, reply.conversation_id,
                               finish_reason=reply.finish_reason)
