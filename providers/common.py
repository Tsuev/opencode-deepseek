"""Buffered streams close browser/CLI resources before crossing worker threads."""

import math
import os


def completion_timeout():
    value = float(os.getenv("WEB_PROVIDER_TIMEOUT", "180"))
    if not math.isfinite(value) or not 1 <= value <= 1800:
        raise ValueError("WEB_PROVIDER_TIMEOUT must be between 1 and 1800 seconds")
    return value

class ProviderUnavailable(RuntimeError):
    """An access restriction or unsupported protocol must not trigger a retry."""


class BufferedStream:
    def __init__(self, reply):
        self.conversation_id = reply.conversation_id
        self.finish_reason = reply.finish_reason
        self._text = reply.text

    def __iter__(self):
        # These providers finish and validate an answer before exposing text.
        # In particular, a sync Playwright context cannot survive across the
        # server's generator advances, which may run on different threads.
        for offset in range(0, len(self._text), 1024):
            yield self._text[offset:offset + 1024]
