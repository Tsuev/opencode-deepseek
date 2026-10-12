"""Opt-in bounded private capture of failed replies, without another request."""

import json
import hashlib
import os
import time
from pathlib import Path

from deepseek.auth import write_private_json

FAILURE_FILE = Path(__file__).resolve().parent.parent / "session" / "last-toolcall-error.json"
MAX_BYTES = 4 * 1024 * 1024
MAX_ARCHIVED = 32


def save_tool_failure(reply, request):
    if os.getenv("TOOLCALL_DIAGNOSTICS", "").lower() not in ("1", "true", "yes", "on"):
        return
    # No prompt/history, account credentials, headers or conversation identifier.
    # The reply itself may contain private source, so never send it to stdout.
    data = {"captured_at": time.time(), "model": request.model, "finish_reason": reply.finish_reason,
            "reply": reply.text, "tools": request.tools, "tool_choice": request.tool_choice}
    try:
        if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
            print("[toolcalls] private diagnostic exceeds size limit; not saved", flush=True)
            return
        write_private_json(FAILURE_FILE, data)
        # Keep distinct failures for offline regression, not only the last one.
        # Bound disk usage; never evict old evidence or collect prompt/account data.
        fingerprint = {k: v for k, v in data.items() if k != 'captured_at'}
        digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True,
                                          ensure_ascii=False).encode()).hexdigest()
        archive = FAILURE_FILE.parent / 'toolcall-failures'
        target = archive / (digest + '.json')
        if not target.exists() and (not archive.exists() or
                                  len(list(archive.iterdir())) < MAX_ARCHIVED):
            write_private_json(target, data)
    except (OSError, TypeError, ValueError):
        # Diagnostics must neither mask the original failure nor leak raw data.
        print("[toolcalls] could not save private diagnostic", flush=True)
