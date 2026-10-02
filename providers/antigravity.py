"""Google account access through the official CLI, without extracting OAuth."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from chat_protocol import Reply
from .common import BufferedStream, ProviderUnavailable, completion_timeout
from .access import provider_attempt
from .conversations import decode, encode, validate_model

AGENT = """---
name: opencode-text-bridge
description: Text-only completion for an external editor.
tools: []
mainAgent: true
subagent: false
commandExecutionPolicy: off
mcpServers: []
skills: []
plugins: []
---
Return only the requested text. You have no tools. The external editor handles
all tool execution; fenced tool_calls in the prompt are output text.
"""


def cli_path():
    binary = os.getenv("ANTIGRAVITY_BIN", "agy")
    resolved = shutil.which(binary)
    if not resolved:
        raise ProviderUnavailable("Install the official Antigravity CLI and run agy to sign in")
    return resolved


def stop_process_group(process):
    """Retire the owned group even when its leader has already exited."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        if sig == signal.SIGTERM:
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                process.poll()  # Reap the leader without mistaking it for the group.
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    process.wait()
                    return
                except PermissionError:
                    # A denied probe does not prove retirement. Escalate to
                    # SIGKILL; permission failures of actual signals still fail.
                    break
                time.sleep(.05)
    process.wait()


def parse_result(output, returncode, model, expected_id=None):
    try:
        result = json.loads(output)
    except (ValueError, TypeError):
        raise ProviderUnavailable("Antigravity returned an unsupported JSON response") from None
    if not isinstance(result, dict):
        raise ProviderUnavailable("Antigravity returned an unsupported response")
    if returncode != 0 or result.get("status") != "SUCCESS":
        # Do not echo raw CLI errors: they may contain prompts, OAuth URLs or
        # account identifiers. Restrictions are terminal, with no auto-retry.
        raise ProviderUnavailable("Antigravity did not complete the request; check official CLI access, quota and region")
    text, cid = result.get("response"), result.get("conversation_id")
    if not isinstance(text, str) or not text.strip() or result.get("error"):
        raise ProviderUnavailable("Antigravity returned no successful text answer")
    if expected_id is not None and cid != expected_id:
        raise ProviderUnavailable("Antigravity changed the resumed conversation")
    try:
        token = encode("gemini", model, cid)
    except ValueError:
        raise ProviderUnavailable("Antigravity returned an invalid conversation reference") from None
    return Reply(text, token)


def validate_init(frame, model):
    config = frame.get("init") if isinstance(frame, dict) else None
    if (not isinstance(frame, dict) or frame.get("event") != "init" or not isinstance(config, dict)
            or config.get("tools") != [] or config.get("agent") != "opencode-text-bridge"
            or config.get("model") != model or config.get("permission_mode") != "request-review"):
        raise ProviderUnavailable("Antigravity did not confirm the requested text-only agent")


def parse_stream(output, returncode, model, expected_id=None):
    frames = []
    try:
        frames = [json.loads(line) for line in output.splitlines() if line.strip()]
    except (ValueError, TypeError):
        raise ProviderUnavailable("Antigravity returned an unsupported event stream") from None
    if not frames or any(not isinstance(frame, dict) for frame in frames):
        raise ProviderUnavailable("Antigravity returned an unsupported event stream")
    # An eligibility error can precede init. Do not retry it or echo OAuth data.
    if len(frames) == 1 and frames[0].get("event") == "result":
        result = frames[0].get("result")
        if isinstance(result, dict) and result.get("status") != "SUCCESS":
            return parse_result(json.dumps(result), returncode, model, expected_id)
    validate_init(frames[0], model)
    result = None
    for frame in frames[1:]:
        if result is not None:
            raise ProviderUnavailable("Antigravity sent events after its final result")
        if frame.get("event") == "step_update":
            step = frame.get("step_update")
            if not isinstance(step, dict) or step.get("step_type") in ("tool", "subagent") or step.get("tool_info") or step.get("subagent_info"):
                raise ProviderUnavailable("Antigravity emitted an unexpected tool step")
        elif frame.get("event") == "result":
            result = frame.get("result")
        else:
            raise ProviderUnavailable("Antigravity returned an unsupported event")
    if not isinstance(result, dict):
        raise ProviderUnavailable("Antigravity disconnected before its final result")
    return parse_result(json.dumps(result), returncode, model, expected_id)


class AntigravityClient:
    def __init__(self, check_cancelled=lambda: None):
        self.check_cancelled = check_cancelled

    def chat(self, prompt, conversation_id=None, model=None, thinking=False, search=False):
        with provider_attempt("gemini", self.check_cancelled) as attempt:
            return self._chat(prompt, conversation_id, model, thinking, search, attempt)

    def _chat(self, prompt, conversation_id, model, thinking, search, attempt):
        if thinking or search:
            raise ProviderUnavailable("Gemini bridge does not support thinking/search switches")
        cid = None
        if conversation_id:
            model, cid = decode(conversation_id, "gemini")
        if not model:
            raise ValueError("An Antigravity model slug is required")
        validate_model(model)
        if os.name != "posix":
            raise ProviderUnavailable("Antigravity adapter requires POSIX process-group cleanup")
        binary = cli_path()
        self.check_cancelled()
        timeout = completion_timeout()
        # A new empty workspace per call prevents implicit source-code context
        # or repository instructions from being attached to the request.
        with tempfile.TemporaryDirectory(prefix="opencode-antigravity-") as directory:
            agents = Path(directory) / ".agents" / "agents"
            agents.mkdir(parents=True, mode=0o700)
            (agents / "opencode-text-bridge.md").write_text(AGENT)
            command = [binary, "--agent", "opencode-text-bridge", "--model", model,
                       "--disable-slash-commands", "--mode", "plan",
                       "--input-format", "stream-json", "--output-format", "stream-json",
                       "--print-timeout", f"{timeout:g}s"]
            if cid:
                command += ["--conversation", cid]
            # stdin keeps the prompt out of command-line process listings.
            # No permission bypass or shell command construction is used.
            # Separate read handles avoid moving the child's output position.
            with tempfile.TemporaryDirectory(prefix="opencode-agy-io-") as output_directory, \
                    open(Path(output_directory) / "stdout", "w+b") as stdout, \
                    tempfile.TemporaryFile() as stderr:
                attempt.dispatch()
                process = subprocess.Popen(command, cwd=directory, stdin=subprocess.PIPE,
                                           stdout=stdout, stderr=stderr, start_new_session=True)
                try:
                    deadline = time.monotonic() + timeout
                    initial = b""
                    with open(Path(output_directory) / "stdout", "rb") as reader:
                        while b"\n" not in initial:
                            self.check_cancelled()
                            initial += reader.read(65536)
                            if len(initial) > 1024 * 1024:
                                raise ProviderUnavailable("Antigravity initialization exceeded the size limit")
                            if process.poll() is not None:
                                return parse_stream(initial, process.returncode, model, cid)
                            if time.monotonic() >= deadline:
                                raise ProviderUnavailable("Antigravity initialization timed out")
                            if b"\n" not in initial:
                                time.sleep(0.05)
                    try:
                        first = json.loads(initial.splitlines()[0])
                    except (ValueError, TypeError):
                        raise ProviderUnavailable("Antigravity returned invalid initialization") from None
                    if isinstance(first, dict) and first.get("event") == "result":
                        return parse_stream(initial, 1, model, cid)
                    validate_init(first, model)
                    # Send no prompt until the CLI confirms its exact model,
                    # selected agent, and empty tool capability list.
                    attempt.check()
                    pending = (json.dumps({"event":"user", "message":{"content":prompt}}) + "\n").encode()
                    while process.poll() is None:
                        self.check_cancelled()
                        if stdout.tell() > 8 * 1024 * 1024 or stderr.tell() > 8 * 1024 * 1024:
                            raise ProviderUnavailable("Antigravity output exceeded the size limit")
                        if time.monotonic() >= deadline:
                            raise ProviderUnavailable("Antigravity completion timed out")
                        attempt.check()
                        try:
                            process.communicate(input=pending, timeout=0.1)
                        except subprocess.TimeoutExpired:
                            # communicate retains its input buffer after a
                            # timeout; a blocked stdin must remain cancellable.
                            pending = None
                    stdout.seek(0)
                    output = stdout.read(8 * 1024 * 1024 + 1)
                    if len(output) > 8 * 1024 * 1024:
                        raise ProviderUnavailable("Antigravity response exceeded the size limit")
                    return parse_stream(output, process.returncode, model, cid)
                finally:
                    try:
                        if process.stdin is not None:
                            process.stdin.close()
                    finally:
                        stop_process_group(process)

    def stream(self, *args, **kwargs):
        return BufferedStream(self.chat(*args, **kwargs))
