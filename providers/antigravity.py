"""Google account access through the official CLI, without extracting OAuth."""

from __future__ import annotations

import json
import os
import select
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


class _ExitWatch:
    """Observe the CLI leader's exit WITHOUT reaping it.

    An unreaped leader keeps its PID, and so the process-group ID, reserved;
    only then can stop_process_group signal the group without any risk of
    hitting a reused ID. Reaping happens in stop_process_group, after the last
    group signal. Linux uses waitid(WNOWAIT); macOS/BSD use a kqueue NOTE_EXIT.
    """

    @staticmethod
    def supported():
        return (hasattr(os, "waitid") and hasattr(os, "WNOWAIT")) or hasattr(select, "kqueue")

    def __init__(self, process):
        self.pid, self.done, self.queue = process.pid, False, None
        if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
            return
        self.queue = select.kqueue()
        event = select.kevent(self.pid, filter=select.KQ_FILTER_PROC,
                              flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)
        try:
            self.queue.control([event], 0, 0)
        except ProcessLookupError:
            self.done = True  # Already exited; still an unreaped zombie (not waited).

    def exited(self):
        if not self.done:
            if self.queue is None:
                self.done = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            else:
                self.done = bool(self.queue.control(None, 1, 0))
        return self.done

    def close(self):
        if self.queue is not None:
            self.queue.close()


def stop_process_group(process):
    """Retire the owned group even when its leader has already exited.

    The leader is never reaped before the last group signal: while it is an
    unreaped zombie its PID, and therefore this PGID, cannot be reused, so no
    signal can reach a foreign group. Reaping happens only once signalling ends.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            # No member accepted the signal. For a same-user group this means
            # only our exiting/zombie leader remains (macOS reports EPERM for
            # it). Stop signalling, then reap; a live denied leader is real.
            _reap_after_denied_signal(process)
            return
        if sig == signal.SIGTERM:
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    os.killpg(process.pid, 0)  # Probe only; the leader stays unreaped.
                except ProcessLookupError:
                    process.wait()
                    return
                except PermissionError:
                    # A denied probe does not prove retirement. Escalate to
                    # SIGKILL; the unreaped leader still reserves the PGID.
                    break
                time.sleep(.05)
    process.wait()


def _reap_after_denied_signal(process):
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        raise PermissionError("Owned Antigravity process group refused termination") from None


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
        if os.name != "posix" or not _ExitWatch.supported():
            error = ProviderUnavailable("Antigravity adapter requires POSIX process-group cleanup")
            error.before_dispatch = True
            raise error
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
                watch = None
                output, returncode = None, None
                try:
                    watch = _ExitWatch(process)
                    deadline = time.monotonic() + timeout
                    initial = b""
                    with open(Path(output_directory) / "stdout", "rb") as reader:
                        while b"\n" not in initial:
                            self.check_cancelled()
                            initial += reader.read(65536)
                            if len(initial) > 1024 * 1024:
                                raise ProviderUnavailable("Antigravity initialization exceeded the size limit")
                            if watch.exited():
                                output = initial
                                break
                            if time.monotonic() >= deadline:
                                raise ProviderUnavailable("Antigravity initialization timed out")
                            if b"\n" not in initial:
                                time.sleep(0.05)
                    if output is None:
                        try:
                            first = json.loads(initial.splitlines()[0])
                        except (ValueError, TypeError):
                            raise ProviderUnavailable("Antigravity returned invalid initialization") from None
                        if isinstance(first, dict) and first.get("event") == "result":
                            output, returncode = initial, 1
                    if output is None:
                        validate_init(first, model)
                        # Send no prompt until the CLI confirms its exact model,
                        # selected agent, and empty tool capability list.
                        attempt.check()
                        self._send_prompt(process, watch, prompt, stdout, stderr, deadline, attempt)
                        while not watch.exited():
                            self._check_running(stdout, stderr, deadline, attempt)
                            time.sleep(0.05)
                        stdout.seek(0)
                        output = stdout.read(8 * 1024 * 1024 + 1)
                        if len(output) > 8 * 1024 * 1024:
                            raise ProviderUnavailable("Antigravity response exceeded the size limit")
                finally:
                    try:
                        if process.stdin is not None:
                            process.stdin.close()
                    finally:
                        try:
                            # Signals the still-reserved group, then reaps the leader.
                            stop_process_group(process)
                        finally:
                            if watch is not None:
                                watch.close()
                return parse_stream(output, process.returncode if returncode is None else returncode,
                                    model, cid)

    def _check_running(self, stdout, stderr, deadline, attempt):
        self.check_cancelled()
        if stdout.tell() > 8 * 1024 * 1024 or stderr.tell() > 8 * 1024 * 1024:
            raise ProviderUnavailable("Antigravity output exceeded the size limit")
        if time.monotonic() >= deadline:
            raise ProviderUnavailable("Antigravity completion timed out")
        attempt.check()

    def _send_prompt(self, process, watch, prompt, stdout, stderr, deadline, attempt):
        """Write the prompt without blocking cancellation and without reaping."""
        pending = memoryview((json.dumps({"event": "user", "message": {"content": prompt}}) + "\n").encode())
        descriptor = process.stdin.fileno()
        os.set_blocking(descriptor, False)
        while pending and not watch.exited():
            self._check_running(stdout, stderr, deadline, attempt)
            try:
                pending = pending[os.write(descriptor, pending[:65536]):]
            except BlockingIOError:
                time.sleep(0.01)
            except BrokenPipeError:
                break
        process.stdin.close()  # EOF, as communicate() did after the input.

    def stream(self, *args, **kwargs):
        return BufferedStream(self.chat(*args, **kwargs))
