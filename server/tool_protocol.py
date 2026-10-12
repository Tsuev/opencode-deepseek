"""Pure reply contract shared by live transport and offline replay.

No clients, session storage, diagnostics or network operations belong here.
Only complete, schema-checked batches may cross this boundary as actions.
"""

from .openai_format import (
    ToolCallError, enforce_tool_choice, has_tool_call_markup, parse_tool_calls,
)


def validate_reply(text, tools, tool_choice=None, *, finish_reason="stop", provider="deepseek"):
    names = [t.get("function", t).get("name") for t in tools or []]
    finished = finish_reason == "stop"
    deepseek = finished and provider == "deepseek"
    content, calls = parse_tool_calls(
        text, allowed_names=names,
        allow_unclosed_fence=finished,
        allow_closing_artifacts=finished,
        allow_missing_array_closer=finished,
        # Incomplete replies get no normalization; their calls are rejected
        # below even if the JSON happens to be whole.
        flat_argument_tools=tools if finished else None,
        allow_outer_closer_transposition=deepseek,
        allow_missing_call_closer=deepseek,
        allow_tool_preamble=deepseek,
        allow_calls_fence=deepseek,
    )
    if (tool_choice != "none" and calls is None
            and has_tool_call_markup(text, names if provider == "deepseek" else None)):
        raise ToolCallError("Unsupported tool-call markup; no calls were returned")
    if not calls and not (content or "").strip():
        raise ToolCallError("Provider returned no answer text or tool calls; no actions were returned")
    enforce_tool_choice(tool_choice, calls)
    if calls and not finished:
        raise ToolCallError("The provider did not finish the tool reply normally; no calls were returned")
    return content, calls
