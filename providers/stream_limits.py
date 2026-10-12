"""Bounded SSE input. No retries; callers retain ownership of response cleanup."""
import time

READ_TIMEOUT = 15.0
MAX_SECONDS = 300.0
MAX_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024


def bounded_lines(response, attempt=None, *, started=None, clock=time.monotonic,
                  max_seconds=MAX_SECONDS, max_bytes=MAX_BYTES,
                  max_line_bytes=MAX_LINE_BYTES):
    """Check cancellation before/after reads, including EOF and terminal markers.

    Transport read timeout bounds a silent blocking read. Total decoded bytes
    and unfinished lines are capped before passing data to the SSE parser.
    """
    start = clock() if started is None else started
    total = 0
    pending = b""

    def check():
        if attempt is not None:
            attempt.check()
        if clock() - start >= max_seconds:
            raise TimeoutError("Provider stream exceeded total deadline")

    check()
    chunks = iter(response.iter_bytes())
    while True:
        check()
        try:
            chunk = next(chunks)
        except StopIteration:
            check()
            break
        check()
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("Provider stream exceeded byte limit")
        pending += chunk
        while True:
            endings = [i for i in (pending.find(b"\n"), pending.find(b"\r")) if i >= 0]
            if not endings:
                break
            end = min(endings)
            if pending[end:] == b"\r":
                break  # CRLF may straddle transport chunks.
            width = 2 if pending[end:end + 2] == b"\r\n" else 1
            line, pending = pending[:end], pending[end + width:]
            if len(line) > max_line_bytes:
                raise ValueError("Provider SSE line exceeded byte limit")
            check()
            yield line.rstrip(b"\r").decode("utf-8", errors="strict")
        if len(pending.rstrip(b"\r")) > max_line_bytes:
            raise ValueError("Provider SSE line exceeded byte limit")
    if pending:
        yield pending.rstrip(b"\r").decode("utf-8", errors="strict")
    check()
