"""Translate between OpenAI's chat-completions shapes and our DeepSeek client.

DeepSeek's protocol has no system/role channel — just a single `prompt` string.
So we flatten the OpenAI `messages` array into one prompt, and wrap DeepSeek's
text output back into OpenAI response/stream objects.

Tool calling is EMULATED: DeepSeek's web chat has no function-calling channel,
so we inject the tool specs into the prompt, ask the model to answer with a
fenced `tool_calls` JSON block when it wants a tool, then parse that block back
into OpenAI `tool_calls`.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Iterable, List, Optional, Tuple

from .schemas import ChatMessage

_ROLE_LABELS = {"system": "System", "user": "User", "assistant": "Assistant"}

# Only the complete, explicitly requested protocol may produce actions.
_TOOL_BLOCK_RE = re.compile(r"\A```tool_calls[ \t]*\r?\n(.*?)\r?\n```[ \t]*\Z", re.DOTALL)
_TOOL_START_RE = re.compile(r"\A```tool_calls[ \t]*\r?\n")
_CLOSING_TOOL_FENCE_RE = re.compile(r"(?m)^```[ \t]*\r?$")
_TOOL_MARKUP_RE = re.compile(
    r"(?m)^[ \t]*```(?:tool_calls|calls)[ \t]*\r?$"
    r"|^[ \t]*<｜｜DSML｜｜ calls>(?=\s*<｜｜DSML｜｜ invoke\b)"
)
# Observed web-model terminators after an otherwise complete JSON array. Only
# closing syntax is allowed; no opening tag, content, or second call can match.
_DSML_CLOSERS = r"</｜｜DSML｜｜ parameter>\s*</｜｜DSML｜｜ invoke>\s*</｜｜DSML｜｜ calls>"
_CLOSING_ARTIFACTS_RE = re.compile(r"\A(?:\}|</｜｜DSML｜｜tool_calls>|(?:\}\s*)?" + _DSML_CLOSERS + r")\Z")


class ToolCallError(RuntimeError):
    """The model emitted an invalid or disallowed explicit tool reply."""

    def __init__(self, message, *, repair_hint=None):
        super().__init__(message)
        self.repair_hint = repair_hint


def has_tool_call_markup(text: str, allowed_names=None) -> bool:
    """Recognize call framing, not inline protocol names in a final report.

    Detection alone never authorizes execution: callers may request one fresh
    formatted response, which must pass the full parser and tool-choice policy.
    """
    if _TOOL_MARKUP_RE.search(text):
        return True
    if not allowed_names:
        return False
    # A generic fenced call array is only a signal to ask for a fresh,
    # explicit protocol response. It must never authorize execution itself.
    for match in re.finditer(r"(?m)^```(?:json)?[ \t]*\r?\n(.*?)\r?\n```[ \t]*$",
                             text, re.DOTALL):
        try:
            data = _tool_json(match.group(1))
        except (ValueError, TypeError):
            continue
        if (isinstance(data, list) and data and all(
                isinstance(item, dict) and set(item) == {"name", "arguments"}
                and isinstance(item["name"], str) and item["name"] in allowed_names
                and isinstance(item["arguments"], dict) for item in data)):
            return True
    return False


def _text_of(content) -> str:
    """Extract plain text from a message's content (string or list-of-parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "text":
            parts.append(p.get("text", ""))
    return "\n".join(parts)


# --- tool emulation: prompt injection ------------------------------------


def _tool_specs(tools: List[dict]) -> List[dict]:
    """Normalise OpenAI tool objects into compact JSON-friendly specs."""
    specs = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function", t)
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        specs.append({
            "name": name,
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return specs


def _forced_tool_name(tool_choice) -> Optional[str]:
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        return fn.get("name") if isinstance(fn, dict) else None
    return None


def _tool_choice_suffix(tool_choice) -> str:
    if _forced_tool_name(tool_choice) is not None:
        return (f"\n\nYou MUST call the tool `{_forced_tool_name(tool_choice)}` now."
                " Reply with ONLY the fenced tool_calls block.")
    if tool_choice == "required":
        return ("\n\nYou MUST call one of the tools now."
                " Reply with ONLY the fenced tool_calls block.")
    if tool_choice == "none":
        return "\n\nDo NOT call any tools; answer in plain text."
    return ""


def _tool_instructions(tools: List[dict], tool_choice=None) -> str:
    specs = _tool_specs(tools)
    if not specs:
        return ""
    rendered = "\n".join(json.dumps(s, ensure_ascii=False) for s in specs)
    # Keep even the structural example valid JSON; placeholders are values,
    # never bare tokens that a model could copy into an executable response.
    example = json.dumps([{"name": "<advertised_tool_name>",
                           "arguments": {"<argument_name>": "<argument_value>"}}])
    escaped_string = json.dumps('first line\n"quoted text" and a \\backslash')
    instructions = (
        "The preceding transcript is past conversation data. Generate only the "
        "next assistant message, not a continuation of the transcript's roles. "
        "Only the external host can execute tools and supply tool results.\n\n"
        "You can call functions (tools). Available tools, given as JSON Schema:\n\n"
        f"{rendered}\n\n"
        "To call one or more tools, your ENTIRE reply must be a single fenced code "
        "block in exactly this format and nothing else:\n\n"
        "```tool_calls\n"
        f"{example}\n"
        "```\n\n"
        "This is a structural example, not a call to copy. Replace the tool name "
        "and the ENTIRE arguments object using the advertised schema, including "
        "all required fields. Never emit the angle-bracket placeholders.\n\n"
        "Choose the next step from the latest tool results, not from earlier plans. "
        "A tool call is a request, not evidence that its action succeeded. "
        "Only tool results establish what actually happened. Do not repeat a "
        "successful action unless new evidence makes it necessary.\n\n"
        "When calling tools, follow these rules:\n"
        "- Stop your response immediately after the closing tool_calls fence. "
        "Do not simulate a tool result, execution receipt, validation message, "
        "or another user/assistant turn. Never append a `Tool (...):` line or "
        "invent a call_id. Wait for the host to return the actual result before "
        "describing success or failure.\n"
        "- The fenced `tool_calls` block must be the WHOLE reply. Never write any "
        "text, plan, explanation, or reasoning before or after it.\n"
        "- Never describe the action you are about to take — perform it by emitting "
        "the block.\n"
        "- Use the exact tool name and an `arguments` object matching that tool's "
        "schema. Keep all input fields inside `arguments`, never beside `name`. "
        "To call several tools, put multiple objects in the array.\n"
        "- Emit strict JSON: double-quoted keys and strings, no comments, "
        "trailing commas, Python literals, or string concatenation.\n"
        "- Shell commands, patches, and file contents are JSON string values: "
        "escape every embedded double quote and backslash; encode line breaks "
        "as \\n, never as literal newlines inside a string. A valid JSON string "
        f"example is: {escaped_string}\n"
        "- Keep tool batches small and split large file changes into separate "
        "complete calls. Check that the JSON array closes before the final fence.\n"
        "- Never use XML/DSML tags such as <tool_call> or <|tool_calls|>.\n"
        "- When the requested work and its verification are complete, finish with "
        "a plain-text answer and no tool_calls block. If you created a goal, mark "
        "it complete once the objective is achieved, then give the final answer; "
        "do not keep checking a completed goal."
    )
    required = []
    for spec in specs:
        schema = spec['parameters']
        fields = schema.get('required') if isinstance(schema, dict) else None
        if isinstance(fields, list) and fields and all(isinstance(f, str) for f in fields):
            required.append({'name': spec['name'], 'required_argument_fields': fields})
    instructions += (
        '\n\nBefore emitting each call, check its complete arguments against the '
        'advertised schema. Required top-level argument fields (not a replacement '
        'for nested or conditional schema rules):\n'
        + '\n'.join(json.dumps(item, ensure_ascii=False) for item in required)
        + '\nChoose required targets and identifiers from established task context '
        'before composing large content. Include every required field in arguments; '
        'never omit a target or expect it to be inferred from content or another call. '
        'If a required value is unknown, obtain it with an appropriate read-only '
        'tool or ask the user; do not guess. '
        'Do not batch a mutation with commands that depend on its success. '
        'Wait for the mutation tool result before running its verifier.'
    )
    instructions += (
        '\n\nVerification reuse contract:\n'
        '- Before proposing a test run, inspect the established execution results '
        'and what changed since the most recent applicable successful run. Reuse '
        'that result when the tested behavior, test code, dependencies, configuration '
        'and execution environment have not materially changed.\n'
        '- Repeat or broaden a check only for a concrete reason: a relevant code '
        'or test change, a relevant dependency/configuration/environment change, '
        'a new failure or unresolved concern that this check can investigate, '
        'or an explicit user request for a fresh run. Record the concrete reason '
        'in the tool description when that field exists; never invent a reason.\n'
        '- A README-only wording change does not invalidate application/parser '
        'tests unless those tests actually consume that documentation. Final '
        'verification, confidence, a new turn, recovery, resume, a restart alone, '
        'or preparing a summary is not a reason to rerun unchanged passing tests.\n'
        '- Do not hide a repeated run inside a compound shell command with git '
        'status, git diff, tail, or another check. Inspect status/diffs on their '
        'own when the tests have already passed. A larger suite containing the '
        'same tests also needs a concrete new justification. Prefer the smallest '
        'check that resolves the actual change or uncertainty.\n'
        '- Report reused evidence as previously observed, not freshly rerun. '
        'Skipped tests are not passes. Unknown, failed, interrupted or ambiguous '
        'results are not successful evidence; resolve their status without '
        'automatically repeating actions. Stop-on-error and no-retry instructions '
        'take precedence over any reason to test again.'
        '\n\nShell search contract:\n'
        '- A search with no matches is not evidence that its input producer succeeded. '
        'Keep errexit and pipefail enabled. Never use || true or normalize exit code 1 '
        'on an entire pipeline: that can hide a failed gh, curl, or other producer.\n'
        '- Choose search semantics BEFORE writing the command. Discovery searches '
        '(locate a section, check whether a branch contains a fix, inspect optional '
        'markers, or compare feature presence) are OPTIONAL: absence is useful data, '
        'even when you do not know beforehand whether a match exists. Use '
        'bridge_grep_optional when the Harness shell-safety plugin is confirmed '
        'active; otherwise use the local status-handling stage below. Do not use '
        'plain grep for such discovery. Plain grep is for an explicit assertion '
        'that a required match MUST exist. State that assertion in the description.\n'
        '- For alternative marker names prefer grep -E with a quoted pattern '
        '("marker_a|marker_b"), or separate -e arguments for literal names; avoid '
        'double-escaped alternation. Example when the plugin is active: '
        'git show REF:path | bridge_grep_optional -n -E '
        '\'requires_bytes_plan|pytestmark|CAPABILITY_UNAVAILABLE\' | sed -n \'1,20p\'. '
        'Here a missing marker is acceptable, but a failed git show must stop.\n'
        '- When no matches is expected, handle only grep status 1 inside its own '
        'pipeline stage: { rc=0; grep PATTERN FILE || rc=$?; '
        'if [ "$rc" -eq 1 ]; then rc=0; fi; (exit "$rc"); }. '
        'Preserve grep status 2 and every other failure. The Harness shell-safety '
        'plugin also supplies bridge_grep_optional with precisely this behavior; '
        'use it only when that plugin is known to be active. Plain grep keeps its '
        'normal status for assertions that require a match.\n'
        '- Never use grep -q/--quiet/--silent for these checks: a match makes grep exit 0 even '
        'when another input failed, and bridge_grep_optional refuses quiet mode. Use -c or -n '
        'output instead. Avoid -m, -l and other early-exit options after a large producer: '
        'the producer then exits 141 (SIGPIPE) and the pipeline fails without hiding anything.\n'
        '- For bounded display of a stream, prefer sed -n \'1,40p\' over head: '
        'it drains the input and avoids an artificial producer SIGPIPE. '
        'Inspect already saved logs locally instead of downloading them again.'
    )
    file_tools = {spec['name'] for spec in specs
                  if isinstance(spec['parameters'], dict)
                  and isinstance(spec['parameters'].get('properties'), dict)
                  and 'file_path' in spec['parameters'].get('properties', {})}
    if {'read', 'edit'} <= file_tools:
        instructions += (
            '\n\nFile-edit prerequisite enforced by the host: before calling `edit`, '
            'successfully call the advertised `read` tool on that same `file_path` '
            'in this session and wait for its actual result. Reading through `bash` '
            '(cat, sed, grep, git diff, or any shell command) does NOT satisfy this '
            'prerequisite. A file excerpt in a user message or previous summary '
            'does not establish a successful `read` either. Do not batch the '
            'prerequisite `read` and dependent `edit` in one reply. If a successful '
            '`read` of the same file is already established, do not repeat it '
            'unless the host requires a fresh read or the target content changed. '
            'Never work around a read-before-edit rejection with bash, write, or '
            'another mutation tool. If instructed to stop on errors, stop and '
            'report the rejection instead of reading and retrying automatically.'
        )
    instructions += _tool_choice_suffix(tool_choice)
    # Keep the protocol reminder next to the model's turn.
    if tool_choice is None or tool_choice == "auto":
        instructions += (
            "\n\nNext reply: either a single fenced tool_calls block to perform a "
            "necessary action, or plain text to answer the user when no action "
            "remains. Never combine commentary with a tool_calls block."
        )
    return instructions


def _tool_reminder(tools: List[dict], tool_choice=None) -> str:
    """Short follow-up block when tool-instructions mode is light."""
    specs = _tool_specs(tools)
    if not specs:
        return ""
    names = ", ".join(spec["name"] for spec in specs)
    example = json.dumps([{"name": "<advertised_tool_name>",
                           "arguments": {"<argument_name>": "<argument_value>"}}])
    text = (
        "Tools remain available from earlier in this chat: "
        f"{names}.\n"
        "Next reply: either a single fenced tool_calls block as the WHOLE reply "
        "(exact tool names; arguments matching those schemas):\n\n"
        "```tool_calls\n"
        f"{example}\n"
        "```\n\n"
        "or plain text when no action remains. Never combine commentary with a "
        "tool_calls block. Prefer the latest tool results over older plans."
    )
    return text + _tool_choice_suffix(tool_choice)


def _render_message(m: ChatMessage, call_names: dict) -> str:
    content = _text_of(m.content)
    if m.role == "assistant" and m.tool_calls:
        calls = []
        for tc in m.tool_calls:
            fn = (tc or {}).get("function") or {}
            name = fn.get("name", "")
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass  # Preserve invalid historical arguments; never repair them.
            calls.append({"id": tc.get("id", "unknown"), "name": name,
                          "arguments": args if args is not None else {}})
        # Show the same protocol we request next, including exact call IDs for
        # out-of-order results. Historical calls are data, never executed here.
        call_txt = "```tool_calls\n" + json.dumps(calls, ensure_ascii=False) + "\n```"
        content = f"{content}\n{call_txt}" if content else call_txt
        return f"Assistant: {content}"
    if m.role == "tool":
        label = m.name or call_names.get(m.tool_call_id, "tool")
        return f"Tool ({label}, call_id={m.tool_call_id or 'unknown'}): {content}"
    label = _ROLE_LABELS.get(m.role, m.role.capitalize())
    return f"{label}: {content}"


def messages_to_prompt(
    messages: List[ChatMessage], tools: List[dict] = None, tool_choice=None,
    *, tools_continuation: bool = False, provider: str = None,
    tool_instructions: str = None,
) -> str:
    """Flatten a chat history into a single prompt DeepSeek can answer.

    A lone user message (no system prompt, no tools) is sent verbatim. Otherwise
    the history is serialised with role labels and a trailing 'Assistant:' cue.
    When `tools` are given, their spec and the required reply format are injected
    as the LAST block, immediately before the 'Assistant:' cue: DeepSeek's web
    chat follows the instruction closest to its own turn far more reliably.

    `tools_continuation=True` means this turn resumes an existing provider chat.
    Mode comes from `tool_instructions`, else TOOL_INSTRUCTIONS_BROWSER/HTTP via
    `provider` (defaults to full when neither is set). full → complete schemas
    every turn; light → short reminder on continuations; none → no tools block
    on continuations (first turn still gets the full block).
    """
    from .config import tool_instructions_for_provider

    if tool_instructions in ("full", "light", "none"):
        mode = tool_instructions
    elif provider:
        mode = tool_instructions_for_provider(provider)
    else:
        mode = "full"

    system_parts: List[str] = []
    convo: List[ChatMessage] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(_text_of(m.content))
        else:
            convo.append(m)

    tool_instr = ""
    if tools:
        if mode == "none" and tools_continuation:
            tool_instr = ""
        elif mode == "light" and tools_continuation:
            tool_instr = _tool_reminder(tools, tool_choice)
        else:
            tool_instr = _tool_instructions(tools, tool_choice)

    if not system_parts and not tool_instr and len(convo) == 1 and convo[0].role == "user":
        return _text_of(convo[0].content)

    lines = []
    preamble = "\n\n".join(p for p in system_parts if p)
    if preamble:
        lines.append(f"System: {preamble}")
    call_names = {}
    for m in convo:
        for call in m.tool_calls or []:
            fn = call.get("function") or {}
            if isinstance(fn, dict) and call.get("id"):
                call_names[call["id"]] = fn.get("name", "tool")
    lines.extend(_render_message(m, call_names) for m in convo)
    if tool_instr:
        lines.append(tool_instr)
    lines.append("Assistant:")
    return "\n\n".join(lines)


def validate_tool_choice(tool_choice, allowed_names: Iterable[str]) -> None:
    """Reject invalid request policies before making an upstream request."""
    names = set(allowed_names)
    if tool_choice is None or tool_choice in ("auto", "none"):
        return
    if tool_choice == "required":
        if not names:
            raise ValueError("tool_choice='required' needs at least one tool")
        return
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        name = _forced_tool_name(tool_choice)
        if isinstance(name, str) and name in names:
            return
    raise ValueError("tool_choice must be auto, none, required, or an advertised function")


def enforce_tool_choice(tool_choice, tool_calls: Optional[List[dict]]) -> None:
    """Treat the caller's tool policy as a contract, not a model suggestion."""
    if tool_choice == "none" and tool_calls:
        raise ToolCallError("The model called a tool while tool_choice='none'")
    forced = _forced_tool_name(tool_choice)
    if (tool_choice == "required" or forced) and not tool_calls:
        raise ToolCallError("The model did not emit the required tool call")
    if forced and any(c["function"]["name"] != forced for c in tool_calls or []):
        raise ToolCallError(f"The model called a tool other than the required '{forced}'")


def _flat_tool_arguments(item: dict, tools: Optional[List[dict]]) -> Optional[dict]:
    """Move explicitly declared input fields into a missing arguments wrapper.

    This changes only the envelope, never values or field names. The executor
    remains responsible for validating values against its full input schema.
    Ambiguous schemas and undeclared fields cannot authorize normalization.
    """
    if any(key in item for key in ("arguments", "function", "type", "id")):
        return None
    specs = [spec for spec in _tool_specs(tools) if spec["name"] == item.get("name")]
    if len(specs) != 1:
        return None
    schema = specs[0]["parameters"]
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return None
    properties, required = schema.get("properties"), schema.get("required", [])
    if (not isinstance(properties, dict) or not isinstance(required, list)
            or not all(isinstance(key, str) for key in required)
            or set(properties) & {"name", "arguments", "function", "type", "id"}):
        return None
    arguments = {key: value for key, value in item.items() if key != "name"}
    if not set(arguments) <= properties.keys() or not set(required) <= arguments.keys():
        return None
    return arguments


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _tool_json(payload):
    return json.loads(payload, object_pairs_hook=_unique_json_object)


def _tool_block_after_preamble(text, *, allow_calls_fence=False):
    """Accept one explicit terminal protocol block after plain commentary.

    This option defines tool_calls as an action protocol, not a documentation
    fence. Generic JSON, quoted blocks and blocks inside other fences stay text.
    The entire suffix still goes through the strict batch parser below.
    """
    label = r'(?:tool_calls|calls)' if allow_calls_fence else 'tool_calls'
    starts = list(re.finditer(r"(?m)^```" + label + r"[ \t]*\r?$", text))
    if len(starts) != 1:
        return text
    prefix = text[:starts[0].start()]
    if '```' in prefix or '~~~' in prefix or '<' in prefix:
        return text
    # A prose blockquote is not a quoted action. Require an explicit blank
    # line before the top-level protocol fence when quotes appear in prose;
    # never strip quote markers or extract code from a quoted/nested fence.
    if (re.search(r'(?m)^\s*>', prefix)
            and not re.search(r'\r?\n[ \t]*\r?\n\Z', prefix)):
        return text
    return text[starts[0].start():]


def _transpose_outer_closers(payload, error):
    """Swap only the last call/array closers after complete arguments.

    The JSON decoder must fail at the terminal ]}; a string-aware stack must
    show that only the call object and outer batch remain open. Never repair
    an argument container, add a delimiter or change a value.
    """
    tail = payload[error.pos:]
    if error.msg != "Expecting ',' delimiter" or not re.fullmatch(r'\]\s*\}\s*', tail):
        return None
    stack, quoted, escaped = [], False, False
    for char in payload[:error.pos]:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            stack.append(char)
        elif char in ']}':
            if not stack or stack.pop() != {']': '[', '}': '{'}[char]:
                return None
    if quoted or stack != ['[', '{'] or not payload[:error.pos].rstrip().endswith('}'):
        return None
    # Replace the two existing bytes in place; retain all other bytes.
    corrected = payload[:error.pos] + '}' + tail[1:].replace('}', ']', 1)
    try:
        data = _tool_json(corrected)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list) or not data or not isinstance(data[-1], dict):
        return None
    if not isinstance(data[-1].get('arguments'), dict):
        return None
    return data


def _close_call_before_next(payload, error):
    """Close action envelopes only after independently complete arguments.

    Each insertion must produce a strictly parsed batch prefix ending in exactly
    name/arguments. Never close arguments or recover other syntax. At most 32
    insertions bound the work; no partial batch escapes final schema validation.
    """
    for _ in range(32):
        if (error.msg == 'Expecting property name enclosed in double quotes'
                and payload[error.pos:error.pos + 1] == '{'):
            prefix = payload[:error.pos].rstrip()
            if not prefix.endswith(','):
                return None
            boundary = len(prefix) - 1
        elif (error.msg == "Expecting ',' delimiter"
              and payload[error.pos:].strip() == ']'):
            boundary = error.pos
        else:
            return None
        try:
            preceding = _tool_json(payload[:boundary] + '}]')
        except (ValueError, TypeError):
            return None
        if not isinstance(preceding, list) or not preceding:
            return None
        item = preceding[-1]
        if (not isinstance(item, dict) or set(item) != {'name', 'arguments'}
                or not isinstance(item['name'], str)
                or not isinstance(item['arguments'], dict)):
            return None
        payload = payload[:boundary] + '}' + payload[boundary:]
        try:
            repaired = _tool_json(payload)
        except json.JSONDecodeError as next_error:
            if next_error.pos <= boundary:
                return None
            error = next_error
            continue
        except (ValueError, TypeError):
            return None
        if (not isinstance(repaired, list)
                or repaired[:len(preceding)] != preceding):
            return None
        return repaired
    return None


def parse_tool_calls(
    text: str, allowed_names: Optional[Iterable[str]] = None, *,
    allow_unclosed_fence: bool = False,
    allow_closing_artifacts: bool = False,
    allow_missing_array_closer: bool = False,
    flat_argument_tools: Optional[List[dict]] = None,
    allow_tool_preamble: bool = False,
    allow_outer_closer_transposition: bool = False,
    allow_missing_call_closer: bool = False,
    allow_calls_fence: bool = False,
) -> Tuple[str, Optional[List[dict]]]:
    """Accept one whole fenced tool_calls array, with explicit narrow tolerances.

    All calls and their arguments must validate before any call is returned.
    Ordinary text, JSON examples, XML/DSML and pseudo-calls remain visible text.
    Optional normal-stop tolerances cover an absent final Markdown fence and
    known closing-only artifacts after a complete array, independently.
    A caller may permit a missing outer array closer only after all objects
    have closed; no nested delimiter or argument is repaired.
    Invalid JSON values and partial batches fail closed.
    Normal-stop callers may also supply schemas for an unambiguous flat input
    envelope. No input values or missing fields are inferred.
    With explicit opt-in, plain commentary may precede one terminal protocol
    block. Advertised schemas validate every call before the batch is returned.
    """
    stripped = (text or "").strip()
    if allow_tool_preamble and flat_argument_tools is not None:
        stripped = _tool_block_after_preamble(stripped, allow_calls_fence=allow_calls_fence)
    if allow_calls_fence and flat_argument_tools is not None and re.match(r'\A```calls[ \t]*\r?\n', stripped):
        # Observed label alias only. No JSON/closing repair is permitted here;
        # every argument must still satisfy the advertised schema below.
        stripped = '```tool_calls' + stripped[len('```calls'):]
        allow_unclosed_fence = allow_closing_artifacts = allow_missing_array_closer = False
        allow_outer_closer_transposition = allow_missing_call_closer = False
    if not stripped.startswith("```tool_calls"):
        return text, None
    match = _TOOL_BLOCK_RE.fullmatch(stripped)
    start = _TOOL_START_RE.match(stripped)
    if match:
        payload = match.group(1)
    elif allow_unclosed_fence and start and not _CLOSING_TOOL_FENCE_RE.search(stripped):
        # Only the Markdown wrapper may be missing after a confirmed normal
        # stop. json.loads still requires the entire array and rejects trailing
        # text, a second block, and every partial/ambiguous argument unchanged.
        payload = stripped[start.end():]
    else:
        raise ToolCallError("Expected one complete fenced tool_calls block")
    try:
        data = _tool_json(payload)
    except json.JSONDecodeError as exc:
        transposed = (_transpose_outer_closers(payload, exc)
                      if allow_outer_closer_transposition and match
                      and flat_argument_tools is not None else None)
        if transposed is not None:
            data = transposed
        elif (allow_missing_call_closer and match and flat_argument_tools is not None
              and (closed := _close_call_before_next(payload, exc)) is not None):
            data = closed
        elif (allow_missing_array_closer and exc.msg == "Expecting ',' delimiter"
                and exc.pos == len(payload) and payload.lstrip().startswith("[")
                and payload.rstrip().endswith("}")):
            # A normal-stop reply can omit exactly the outer array closer.
            # JSON must then parse in full: never close a string, argument
            # object, nested array, trailing comma, or partial batch member.
            try:
                data = _tool_json(payload.rstrip() + "]")
            except (ValueError, TypeError):
                raise _json_tool_error("The tool_calls block contains invalid JSON", exc) from exc
        elif (allow_closing_artifacts
                and exc.msg == "Extra data"
                and _CLOSING_ARTIFACTS_RE.fullmatch(payload[exc.pos:].strip())):
            # Observed FINISHED replies: a complete array followed by a stray
            # object closer or closing-only DSML. JSON was fully consumed.
            # Never repair strings, add missing delimiters, drop array members,
            # or tolerate any other suffix. Markdown framing is independent.
            data = _tool_json(payload[:exc.pos])
        else:
            raise _json_tool_error("The tool_calls block contains invalid JSON", exc) from exc
    except ValueError as exc:
        raise ToolCallError("The tool_calls block contains invalid JSON") from exc
    if not isinstance(data, list) or not data:
        raise ToolCallError("tool_calls must be a non-empty JSON array")
    names = set(allowed_names) if allowed_names is not None else None
    calls = []
    for item in data:
        if not isinstance(item, dict):
            raise ToolCallError("Each tool call must be an object")
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        if fn is not item:
            if set(item) - {"id", "type", "function"} or set(fn) - {"name", "arguments"}:
                raise ToolCallError("Tool call contains ambiguous envelope fields")
        elif "arguments" in item and set(item) - {"id", "type", "name", "arguments"}:
            raise ToolCallError("Tool call contains ambiguous envelope fields")
        name = fn.get("name")
        if not isinstance(name, str) or not name or (names is not None and name not in names):
            raise ToolCallError("The model called an unadvertised tool")
        args = fn.get("arguments")
        if "arguments" not in fn and fn is item:
            args = _flat_tool_arguments(item, flat_argument_tools)
        if isinstance(args, str):
            try:
                args = _tool_json(args)
            except json.JSONDecodeError as exc:
                raise _json_tool_error("Tool arguments contain invalid JSON", exc) from exc
            except ValueError as exc:
                raise ToolCallError("Tool arguments contain invalid JSON") from exc
        if not isinstance(args, dict):
            raise ToolCallError("Tool arguments must be a JSON object")
        try:
            arguments = json.dumps(args, ensure_ascii=False, allow_nan=False)
        except ValueError as exc:
            raise ToolCallError("Tool arguments contain non-finite numbers") from exc
        if flat_argument_tools is not None:
            from .tool_schema import validate_arguments
            try:
                validate_arguments(name, args, flat_argument_tools)
            except ValueError as exc:
                raise ToolCallError(str(exc)) from None
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    return "", calls


def _json_tool_error(message: str, exc: json.JSONDecodeError) -> ToolCallError:
    # Only fixed categories and numeric positions leave the parser. Decoder
    # messages/documents can contain command text or secrets; never echo them.
    if exc.msg.startswith("Invalid control character"):
        reason = "unescaped_control_character"
    elif exc.msg.startswith("Invalid \\escape"):
        reason = "invalid_escape"
    elif exc.msg.startswith("Unterminated string"):
        reason = "unterminated_string"
    else:
        reason = "invalid_syntax"
    return ToolCallError(
        f"{message} ({reason}; line {exc.lineno}, column {exc.colno}). "
        "No tools from this reply were returned; automatic replay is disabled.",
        repair_hint=(f"JSON validation failed: {reason}, line {exc.lineno}, column {exc.colno} "
                     "within the JSON payload (not the Markdown fence). "
                     "Regenerate the whole valid block, not a patch or suffix. "
                     "Check commas, quoted keys, and escaping of quotes and backslashes inside strings.")
    )


def looks_truncated(text: str, allowed_names: Optional[Iterable[str]] = None) -> bool:
    """Continue only an explicitly started tool block missing its closing fence."""
    stripped = (text or "").strip()
    return bool(_TOOL_START_RE.match(stripped)) and not _CLOSING_TOOL_FENCE_RE.search(stripped)


# --- OpenAI response shapes -----------------------------------------------


def _now() -> int:
    return int(time.time())


def _id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def _est_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) — DeepSeek's web API gives us no count."""
    return max(1, len(text) // 4)


def completion_response(model: str, content: str, prompt: str,
                        conversation_id: str = None, tool_calls: List[dict] = None,
                        finish_reason: str = "stop") -> dict:
    """A full (non-streaming) OpenAI chat.completion object.

    `conversation_id` is an extra top-level field (outside OpenAI's schema) you
    send back to resume the conversation.
    """
    pt, ct = _est_tokens(prompt), _est_tokens(content or "")
    message: dict = {"role": "assistant", "content": content or ""}
    finish = finish_reason
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = content or None
        finish = "tool_calls"
    return {
        "id": _id(),
        "object": "chat.completion",
        "created": _now(),
        "model": model,
        "conversation_id": conversation_id,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish,
            }
        ],
        "usage": {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
        },
    }


def sse_frames(model: str, content: str, tool_calls: List[dict] = None,
               conversation_id: str = None, cid: str = None, created: int = None,
               finish_reason: str = "stop") -> Iterable[str]:
    """Yield OpenAI SSE frames for an already-computed reply.

    Used when the caller has a full result (e.g. it ran a non-streaming request
    so tool calls can be validated) but the client asked for a stream.
    """
    cid = cid or _id()
    created = created if created is not None else _now()

    def frame(delta: dict, finish=None, extra: dict = None) -> str:
        obj = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if extra:
            obj.update(extra)
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    yield frame({"role": "assistant", "content": None})
    if tool_calls:
        for i, call in enumerate(tool_calls):
            yield frame({"tool_calls": [{
                "index": i,
                "id": call["id"],
                "type": "function",
                "function": call["function"],
            }]})
        yield frame({}, finish="tool_calls", extra={"conversation_id": conversation_id})
    else:
        if content:
            yield frame({"content": content})
        yield frame({}, finish=finish_reason, extra={"conversation_id": conversation_id})
    yield "data: [DONE]\n\n"


def stream_chunks(model: str, stream: Iterable[str], tool_supported: bool = False,
                  iterator: Iterable[str] = None) -> Iterable[str]:
    """Yield OpenAI SSE lines (`data: {...}\\n\\n`) for a streamed completion.

    `stream` is the client's stream object; after it's consumed we read its
    `.conversation_id` and attach it to the final chunk. When the caller has
    already started consuming it, it passes the remaining `iterator` (e.g. a
    chain of the first chunk plus the rest) — `stream` is still used only to
    read `.conversation_id` afterwards.

    When `tool_supported` is set we must buffer the whole reply before deciding
    whether it is plain text or a tool call, so the raw call block never leaks
    to the client as visible content.
    """
    cid, created = _id(), _now()
    it = iterator if iterator is not None else stream

    def frame(delta: dict, finish=None, extra: dict = None) -> str:
        obj = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if extra:
            obj.update(extra)
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    if tool_supported:
        text = "".join(d for d in it if d)
        conversation_id = getattr(stream, "conversation_id", None)
        content, calls = parse_tool_calls(text)
        yield from sse_frames(model, content, calls, conversation_id, cid=cid, created=created,
                              finish_reason=getattr(stream, "finish_reason", "stop"))
        return

    # First frame announces the assistant role.
    yield frame({"role": "assistant", "content": ""})
    for d in it:
        if d:
            yield frame({"content": d})
    conversation_id = getattr(stream, "conversation_id", None)
    yield frame({}, finish=getattr(stream, "finish_reason", "stop"),
                extra={"conversation_id": conversation_id})
    yield "data: [DONE]\n\n"
