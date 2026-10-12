"""Replay private diagnostics against reviewed expectations without account I/O.

Usage: python -m server.offline_corpus session/protocol-corpus.json
The manifest pins source hashes and expected calls; it is private, not a fixture
to publish. Output contains counts only, never source, commands or arguments.
"""
import hashlib
import json
from pathlib import Path
import sys

from .openai_format import ToolCallError
from .tool_protocol import validate_reply


def replay(manifest_path):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    failures = []
    for index, item in enumerate(manifest):
        raw = (manifest_path.parent / item["file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            failures.append(index)
            continue
        data = json.loads(raw)
        try:
            content, calls = validate_reply(
                data["reply"], data["tools"], data.get("tool_choice"),
                finish_reason=data["finish_reason"], provider=item["provider"],
            )
            actual = {"kind": "calls", "calls": [
                {"name": c["function"]["name"],
                 "arguments": json.loads(c["function"]["arguments"])} for c in calls
            ]} if calls else {"kind": "text", "text": content}
        except ToolCallError:
            actual = {"kind": "reject"}
        if actual != item["expected"]:
            failures.append(index)
    return {"cases": len(manifest), "failed_indices": failures}


if __name__ == "__main__":
    report = replay(sys.argv[1])
    print(json.dumps(report))
    sys.exit(bool(report["failed_indices"]))
