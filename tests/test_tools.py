import json
import unittest

from server.openai_format import (
    ToolCallError, enforce_tool_choice, looks_truncated, messages_to_prompt,
    parse_tool_calls, validate_tool_choice,
)
from server.schemas import ChatMessage


def block(calls):
    return "```tool_calls\n" + json.dumps(calls) + "\n```"


class ToolProtocolTests(unittest.TestCase):
    def test_examples_and_native_markup_remain_text(self):
        samples = [
            'Do not execute `bash(command="echo audit")`.',
            '```json\n{"name":"bash","arguments":{"command":"echo audit"}}\n```',
            '{"name":"bash","arguments":{"command":"echo audit"}}',
            '<tool_call><function name="bash"><parameter name="command">echo audit</parameter></function></tool_call>',
            'function<|tool_sep|>bash {"command":"echo audit"}',
            'An example follows:\n' + block([{"name":"bash","arguments":{}}]),
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertEqual(parse_tool_calls(text, {"bash"}), (text, None))

    def test_complete_batch_preserves_order_and_validates_string_arguments(self):
        text = block([{"name":"write","arguments":{"filePath":"a","content":"hello"}},
                      {"name":"bash","arguments":json.dumps({"command":"echo audit"})}])
        content, calls = parse_tool_calls(text, {"write","bash"})
        self.assertEqual(content, "")
        self.assertEqual([c["function"]["name"] for c in calls], ["write","bash"])
        self.assertEqual(json.loads(calls[1]["function"]["arguments"]), {"command":"echo audit"})
        self.assertNotEqual(calls[0]["id"], calls[1]["id"])

    def test_partial_batch_never_returns_first_call(self):
        text = '```tool_calls\n[{"name":"write","arguments":{}},{"name":"bash","arguments":{"command":"echo'
        self.assertTrue(looks_truncated(text, {"write","bash"}))
        with self.assertRaises(ToolCallError):
            parse_tool_calls(text, {"write","bash"})

    def test_bad_member_rejects_entire_batch(self):
        bad_members = [
            {"name":"unknown","arguments":{}},
            {"name":"write","arguments":'{"filePath":"cut'},
            {"name":"write","arguments":[]},
            {"name":"write"},
            {"name":"write","arguments":{"value":float("nan")}},
            "not a call",
        ]
        for member in bad_members:
            with self.subTest(member=member), self.assertRaises(ToolCallError):
                parse_tool_calls(block([{"name":"write","arguments":{}}, member]), {"write"})

    def test_empty_allowlist_allows_no_actions(self):
        with self.assertRaises(ToolCallError):
            parse_tool_calls(block([{"name":"write","arguments":{}}]), set())

    def test_closed_invalid_protocol_does_not_trigger_continuation(self):
        for text in ['```tool_calls\n[broken]\n```', '```python\nprint("unfinished")',
                     'Ordinary prose with an unmatched [', '```tool_calls\n[]\n```\nextra']:
            self.assertFalse(looks_truncated(text))
        with self.assertRaises(ToolCallError):
            parse_tool_calls('```tool_calls\n[broken]\n```', {"write"})

    def test_markdown_inside_cut_arguments_does_not_hide_truncation(self):
        self.assertTrue(looks_truncated('```tool_calls\n[{"name":"write","arguments":{"content":"```code'))

    def test_tool_choice_is_enforced(self):
        _, bash = parse_tool_calls(block([{"name":"bash","arguments":{}}]), {"bash","write"})
        for choice, calls in [("none",bash),("required",None),
                              ({"type":"function","function":{"name":"write"}},bash)]:
            with self.subTest(choice=choice), self.assertRaises(ToolCallError):
                enforce_tool_choice(choice, calls)
        enforce_tool_choice("auto", bash)
        enforce_tool_choice("none", None)
        enforce_tool_choice({"type":"function","function":{"name":"bash"}}, bash)

    def test_request_policy_requires_advertised_tools(self):
        for choice, names in [("required",set()), ("unsupported",{"bash"}),
                              ({"type":"function","function":{"name":"write"}},{"bash"})]:
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                validate_tool_choice(choice, names)

    def test_out_of_order_tool_results_keep_call_identity(self):
        prompt = messages_to_prompt([
            ChatMessage(role="assistant", tool_calls=[
                {"id":"call_a","function":{"name":"read","arguments":'{"filePath":"a"}'}},
                {"id":"call_b","function":{"name":"read","arguments":'{"filePath":"b"}'}},
            ]),
            ChatMessage(role="tool",tool_call_id="call_b",content="second file"),
            ChatMessage(role="tool",tool_call_id="call_a",content="first file"),
        ])
        self.assertIn('read({"filePath":"a"}) [call_id=call_a]', prompt)
        self.assertIn('Tool (read, call_id=call_b): second file', prompt)
        self.assertIn('Tool (read, call_id=call_a): first file', prompt)
