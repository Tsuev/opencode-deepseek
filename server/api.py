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
import os
import threading
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar

import anyio
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from deepseek.auth import SESSION_MAX_AGE, LoginRequired, get_session
from chat_protocol import Reply
from deepseek.client import DeepSeekClient
from qwen.auth import get_session as get_qwen_session
from qwen.client import QwenClient, _decode_cid as decode_qwen_cid
from providers.conversations import decode as decode_provider_cid, PROVIDERS
from providers.registry import build_client as build_provider_client
from providers.access import guard, ProviderRejected, rejection

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
from .browser_routes import router as browser_router
from .schemas import ChatCompletionRequest

# One shared client (and its signed-in session) built lazily on first use.
_client: DeepSeekClient | None = None
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


def _debug_toolcalls() -> bool:
    return os.getenv("DEBUG_TOOLCALLS", "").strip().lower() not in (
        "", "0", "false", "no", "off",
    )


def _build_client(force: bool = False, allow_interactive=None) -> DeepSeekClient:
    """Load a cached session only; legacy force/login arguments cannot browse."""
    session = get_session(max_age=SESSION_MAX_AGE, allow_interactive=False, allow_refresh=False)
    return DeepSeekClient(session=session, access_guard=None)  # server owns pause/pacing


def get_client(force_refresh: bool = False, rejected_client=None) -> DeepSeekClient:
    """Cache a client; an explicit reset reloads the session file without browsing."""
    global _client
    with _client_lock:
        if _client is None or _client.session.age >= SESSION_MAX_AGE or (force_refresh and (
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
    """Load a manually captured, usable Qwen session; never capture a browser."""
    global _qwen_client
    with _qwen_client_lock:
        if (_qwen_client is None or not _qwen_client.session.usable
                or (force_refresh and (rejected_client is None or _qwen_client is rejected_client))):
            session = get_qwen_session(force=force_refresh, allow_interactive=False, allow_refresh=False,
                                       channel=REFRESH_BROWSER_CHANNEL,
                                       fallback_channel=REFRESH_BROWSER_CHANNEL_FALLBACK)
            _qwen_client = QwenClient(session, access_guard=None)  # server owns pause/pacing
        return _qwen_client


def _request_client(req, force_refresh=False, rejected_client=None):
    provider = model_provider(req.model)
    guard.check(provider)
    if provider in PROVIDERS:
        # Browser routes run in another worker context. Bind the originating
        # request's Event now so they see cancellation before this worker wakes.
        signal = _worker_cancellation.get()
        return build_provider_client(provider, lambda: _check_cancellation(signal))
    factory = get_qwen_client if provider == "qwen" else get_client
    if force_refresh:
        return factory(True, rejected_client=rejected_client)
    return factory()


def _request_queue(req):
    provider = model_provider(req.model)
    if provider in PROVIDERS:
        return _provider_request_gates[provider]
    return _qwen_request_gate if provider == "qwen" else _request_gate


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
    client = None
    attempt = None
    try:
        with guard.attempt(provider, _cancellation_check()) as attempt:
            client = _request_client(req)
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
        except ProviderRejected as e:
            yield _sse_error(str(e), e.code)
            return
        except Exception:
            yield _sse_error("Provider request failed; automatic replay is disabled.")
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
        yield _sse_error(str(error), error.code)
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
            yield _sse_error(str(exc), exc.code)
            return
        except Exception:
            yield _sse_error("Provider request failed; automatic replay is disabled.")
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
install_rate_limit(app, RateLimiter(limit=RATE_LIMIT_PER_MINUTE, window=60.0))


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

    try:
        # A live request in this process may be queued behind; abandoned or
        # external attempts and real pauses must still fail before any factory.
        await _run_worker(guard.check, model_provider(req.model), None, True)
    except ProviderRejected as exc:
        return _error(str(exc), status=403, err_type=exc.code)

    if req.conversation_id:
        try:
            provider = model_provider(req.model)
            if provider in PROVIDERS:
                decode_provider_cid(req.conversation_id, provider)
            elif provider == "qwen":
                decode_qwen_cid(req.conversation_id)
            elif req.conversation_id.startswith(("qwen:", "web:")):
                raise ValueError("A provider conversation cannot be resumed through DeepSeek")
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
    # The provider worker latches upstream failures before exposing the result.
    if req.tools:
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
            return _error(str(e), status=403, err_type=e.code)
        except Exception:
            return _error("Provider request failed; automatic replay is disabled.", status=400)

        return completion_response(
            req.model, content, prompt, reply.conversation_id, tool_calls,
            finish_reason=reply.finish_reason,
        )

    # Plain chat: one incremental stream or one buffered completion.
    if req.stream:
        if model_provider(req.model) in PROVIDERS:
            return StreamingResponse(_buffered_plain_stream(req, prompt, model_type),
                                     media_type="text/event-stream")
        return StreamingResponse(_plain_stream(None, prompt, req, model_type),
                                 media_type="text/event-stream")

    try:
        reply = await _run_chat_with_retry(prompt, req, model_type)
    except LoginRequired as e:
        return _error(str(e), status=401, err_type="login_required")
    except ProviderRejected as e:
        return _error(str(e), status=403, err_type=e.code)
    except Exception:
        return _error("Provider request failed; automatic replay is disabled.", status=400)

    return completion_response(req.model, reply.text, prompt, reply.conversation_id,
                               finish_reason=reply.finish_reason)
