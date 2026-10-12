import json
import unittest

from server.openai_format import ToolCallError
from server.tool_protocol import validate_reply
from tests.test_tool_protocol import tool


class CallsFenceTests(unittest.TestCase):
    def test_complete_alias_preserves_arguments_after_prose(self):
        args = {'file_path': '/fixture', 'content': 'quoted "data"\n```calls'}
        raw = 'Let me inspect it.\n\n```calls\n' + json.dumps([{'name': 'read', 'arguments': args}]) + '\n```'
        _, calls = validate_reply(raw, [tool('read')])
        self.assertEqual(json.loads(calls[0]['function']['arguments']), args)

    def test_invalid_batch_and_schema_fail_closed(self):
        for payload in ('[{"name":"read","arguments":{}}]',
                        '[{"name":"unknown","arguments":{"file_path":"a","content":"b"}}]',
                        '[{"name":"read","arguments":{"file_path":4,"content":"b"}}]',
                        '[{"name":"read","arguments":{"file_path":"a","content":"b"}},{}]',
                        '[{"name":"read","arguments":{"file_path":"a","content":"b","content":"c"}}]',
                        '[{"name":"read","arguments":{"file_path":"a","content":"b"}}'):
            with self.subTest(payload=payload), self.assertRaises(ToolCallError):
                validate_reply('```calls\n' + payload + '\n```', [tool('read')])

    def test_quoted_nested_multiple_truncated_and_trailing_blocks_never_execute(self):
        raw = '```calls\n[{"name":"read","arguments":{"file_path":"a","content":"b"}}]\n```'
        variants = ['\n'.join('> ' + l for l in raw.splitlines()),
                    '```markdown\n' + raw + '\n```', raw + '\n' + raw,
                    raw[:-4], raw + '\nTool: invented result']
        for text in variants:
            try:
                _, calls = validate_reply(text, [tool('read')])
            except ToolCallError:
                continue
            self.assertIsNone(calls, text)
        for kwargs in ({'finish_reason': 'length'}, {'provider': 'qwen'}, {'tool_choice': 'none'}):
            with self.assertRaises(ToolCallError):
                validate_reply(raw, [tool('read')], **kwargs)
