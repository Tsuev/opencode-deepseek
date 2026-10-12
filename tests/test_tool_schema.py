import json
import unittest
from unittest.mock import patch

from server.openai_format import parse_tool_calls, ToolCallError
from tests.test_tools import FLAT_TOOLS, block


SUFFIX = '</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>'
SAVED_REPLY = ('I\'ll start by orienting myself in the workspace to see what\'s there '
               'and what "продолжи" refers to.\n\n```tool_calls\n'
               '[{"name": "bash", "command": "pwd && ls -la", '
               '"description": "Show working directory and list files"}]' + SUFFIX)


def parse(text, tools=FLAT_TOOLS):
    names = {t.get('function', t)['name'] for t in tools}
    return parse_tool_calls(text, names, allow_unclosed_fence=True,
                            allow_closing_artifacts=True, flat_argument_tools=tools,
                            allow_tool_preamble=True)


class SchemaProtocolTests(unittest.TestCase):
    def test_multiple_missing_envelope_closers_preserve_all_arguments(self):
        items = [{'name': 'read', 'arguments': {'file_path': '/fixture/} , { "\\n'}}] * 3
        for mask in range(1, 8):
            parts = [json.dumps(item)[:-1] if mask & (1 << i) else json.dumps(item)
                     for i, item in enumerate(items)]
            text = '```tool_calls\n[' + ', '.join(parts) + ']\n```'
            _, calls = parse_tool_calls(text, {'read'}, flat_argument_tools=FLAT_TOOLS,
                                        allow_missing_call_closer=True)
            self.assertEqual([json.loads(c['function']['arguments']) for c in calls],
                             [i['arguments'] for i in items])

    def test_multi_envelope_recovery_rejects_bad_member_and_is_bounded(self):
        good = json.dumps({'name': 'read', 'arguments': {'file_path': '/fixture'}})
        for last in ('{"name":"read","arguments":{"file_path":7}',
                     '{"name":"read","arguments":{"file_path":"x","file_path":"y"}',
                     '{"name":"read","arguments":{"file_path":"unterminated}',
                     '{"name":"read","arguments":{"file_path":"x"',
                     '{"name":"read","arguments":{"file_path":"x"},"extra":1'):
            with self.subTest(last=last), self.assertRaises(ToolCallError):
                parse_tool_calls('```tool_calls\n[' + good[:-1] + ',' + last + ']\n```',
                                 {'read'}, flat_argument_tools=FLAT_TOOLS,
                                 allow_missing_call_closer=True)
        with self.assertRaises(ToolCallError):
            parse_tool_calls('```tool_calls\n[' + ','.join([good[:-1]] * 33) + ']\n```',
                             {'read'}, flat_argument_tools=FLAT_TOOLS,
                             allow_missing_call_closer=True)

    def test_missing_call_closer_preserves_complete_nested_arguments(self):
        tools = [{'name': 'plan', 'parameters': {'type': 'object', 'properties': {
            'items': {'type': 'array'}}, 'required': ['items'], 'additionalProperties': False}},
            *FLAT_TOOLS]
        first = {'name': 'plan', 'arguments': {'items': [{'text': '}, { \\"nested"', 'status': 'pending'}]}}
        second = {'name': 'read', 'arguments': {'file_path': '/fixture'}}
        payload = '[' + json.dumps(first)[:-1] + ', ' + json.dumps(second) + ']'
        text = '```tool_calls\n' + payload + '\n```'
        with self.assertRaises(ToolCallError):
            parse(text, tools)
        _, calls = parse_tool_calls(text, {'plan', 'read'}, flat_argument_tools=tools,
                                    allow_missing_call_closer=True)
        self.assertEqual([json.loads(c['function']['arguments']) for c in calls],
                         [first['arguments'], second['arguments']])

    def test_missing_call_closer_never_repairs_arguments_or_other_errors(self):
        first = json.dumps({'name': 'read', 'arguments': {'file_path': '/fixture'}})
        second = json.dumps({'name': 'read', 'arguments': {'file_path': '/other'}})
        valid = '[' + first[:-1] + ', ' + second + ']'
        for payload in ('[' + first[:-2] + ', ' + second + ']',
                        valid.replace('"/other"', '7'),
                        valid.replace('"/other"', '"/other", "file_path": "duplicate"'),
                        valid.replace('"name": "read"', '"name": "unknown"'),
                        valid + ' trailing',
                        '[' + first[:-1] + ', "extra": 1, ' + second + ']'):
            with self.subTest(payload=payload), self.assertRaises(ToolCallError):
                parse_tool_calls('```tool_calls\n' + payload + '\n```', {'read'},
                                 flat_argument_tools=FLAT_TOOLS, allow_missing_call_closer=True)
        for tools, ending in ((None, '\n```'), (FLAT_TOOLS, '')):
            with self.assertRaises(ToolCallError):
                parse_tool_calls('```tool_calls\n' + valid + ending, {'read'},
                                 flat_argument_tools=tools, allow_unclosed_fence=True,
                                 allow_missing_call_closer=True)

    def test_outer_closer_transposition_preserves_values_and_batch(self):
        arguments = {'command': 'printf "]} \\\" [ { \\n"', 'description': 'Run tests'}
        batch = [{'name': 'read', 'arguments': {'file_path': '/fixture'}},
                 {'name': 'bash', 'arguments': arguments}]
        payload = json.dumps(batch, ensure_ascii=False)
        broken = payload[:-2] + ']}'
        text = '```tool_calls\n' + broken + '\n```'
        with self.assertRaises(ToolCallError):
            parse(text)
        for tail in (']}', '] \n }  '):
            _, calls = parse_tool_calls('```tool_calls\n' + payload[:-2] + tail + '\n```',
                                       {'read', 'bash'}, flat_argument_tools=FLAT_TOOLS,
                                       allow_outer_closer_transposition=True)
            self.assertEqual([json.loads(c['function']['arguments']) for c in calls],
                             [c['arguments'] for c in batch])

    def test_transposition_never_repairs_arguments_or_partial_data(self):
        good = '[{"name":"bash","arguments":{"command":"pwd","description":"test"}}]'
        for payload in (
            good[:-2] + ']} trailing', good[:-2] + ']',
            good.replace('"test"}}]', '"test"]}}'),
            good.replace('"test"}}]', '"test"}]}'),  # valid normalization, invalid schema below
            good.replace('"command":"pwd"', '"command":7')[:-2] + ']}',
            good.replace('"command":"pwd"', '"command":"pwd","command":"ls"')[:-2] + ']}',
            good.replace('"test"', '"unterminated')[:-2] + ']}',
            good[:-2] + ']}\n[]',
        ):
            # The ordinary valid normalization is tested with an incompatible
            # schema to prove that normalization cannot skip schema validation.
            tools = FLAT_TOOLS
            if payload == good[:-2] + ']}':
                tools = [{'name': 'bash', 'parameters': {'type': 'object', 'required': ['missing']}}]
            with self.subTest(payload=payload), self.assertRaises(ToolCallError):
                parse_tool_calls('```tool_calls\n' + payload + '\n```', {'bash'},
                                 flat_argument_tools=tools, allow_outer_closer_transposition=True)
        for schemas, fence in ((None, '\n```'), (FLAT_TOOLS, '')):
            with self.assertRaises(ToolCallError):
                parse_tool_calls('```tool_calls\n' + good[:-2] + ']}' + fence, {'bash'},
                                 flat_argument_tools=schemas, allow_unclosed_fence=True,
                                 allow_outer_closer_transposition=True)

    def test_saved_reply_values_preserved(self):
        _, calls = parse(SAVED_REPLY)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]['function']['arguments']), {
            'command': 'pwd && ls -la', 'description': 'Show working directory and list files'})
        self.assertIsNone(parse_tool_calls(SAVED_REPLY, {'bash'})[1])
        self.assertIsNone(parse_tool_calls(SAVED_REPLY, {'bash'}, allow_tool_preamble=True)[1])

    def test_general_batch_and_empty_arguments(self):
        tools = FLAT_TOOLS + [{'name': 'arbitrary_reader', 'parameters': {
            'type': 'object', 'properties': {}, 'additionalProperties': False}}]
        items = [{'name': 'arbitrary_reader'}, {'name': 'read', 'file_path': '/fixture'},
                 {'name': 'bash', 'arguments': {'command': 'printf "hello"', 'description': 'test'}}]
        for text in (block(items), 'Next steps.\n' + block(items),
                     'Next steps.\n```tool_calls\n' + json.dumps(items) + SUFFIX):
            _, calls = parse(text, tools)
            self.assertEqual([c['function']['name'] for c in calls], ['arbitrary_reader', 'read', 'bash'])
            self.assertEqual(json.loads(calls[0]['function']['arguments']), {})

    def test_invalid_batch_never_returns_partial_calls(self):
        valid = {'name': 'read', 'arguments': {'file_path': '/fixture'}}
        for bad in ({'name': 'bash'}, {'name': 'read', 'file_path': 7},
                    {'name': 'read', 'arguments': {'file_path': False}},
                    {'name': 'read', 'file_path': '/fixture', 'unknown': 1},
                    {'name': 'read', 'arguments': {}, 'file_path': '/fixture'},
                    {'name': 'unknown', 'arguments': {}}):
            with self.subTest(bad=bad), self.assertRaises(ToolCallError):
                parse('Next steps.\n' + block([valid, bad]))

    def test_ambiguous_framing_and_suffix_are_not_executed(self):
        for text in ('```text\n' + SAVED_REPLY, '> Quoted example\n' + block([{'name': 'read', 'file_path': '/fixture'}]),
                     '<pre>\n' + SAVED_REPLY,
                     SAVED_REPLY + '\n' + block([{'name': 'read', 'file_path': '/fixture'}]),
                     SAVED_REPLY.replace('```tool_calls', '```json')):
            self.assertIsNone(parse(text)[1])
        for text in (SAVED_REPLY + ' trailing text', SAVED_REPLY.replace('}]', '}'),
                     SAVED_REPLY.replace('"name": "bash"', '"name": "bash", "name": "read"'),
                     SAVED_REPLY.replace('</｜｜DSML｜｜ parameter>', '<｜｜DSML｜｜ parameter>')):
            with self.subTest(text=text), self.assertRaises(ToolCallError):
                parse(text)

    def test_prose_quotes_before_top_level_calls_preserve_arguments(self):
        args = {'file_path': '/fixture/cli.py', 'offset': 1170, 'limit': 40}
        tools = [{'name': 'read', 'parameters': {
            'type': 'object', 'properties': {'file_path': {'type': 'string'},
            'offset': {'type': 'integer'}, 'limit': {'type': 'integer'}},
            'required': ['file_path'], 'additionalProperties': False}}]
        action = block([{'name': 'read', 'arguments': args}])
        for prefix in ('The docs say:\n\n> Read-only inputs.\n\nNow inspect.\n\n',
                       '> Quote with `inline code`.\n\n',
                       '> Outer\n>> Nested prose\n\n',
                       '> Quote\r\n \t\r\n'):
            with self.subTest(prefix=prefix):
                _, calls = parse(prefix + action, tools)
                self.assertEqual(len(calls), 1)
                self.assertEqual(json.loads(calls[0]['function']['arguments']), args)
                self.assertIsNone(parse_tool_calls(prefix + action, {'read'})[1])

    def test_quoted_nested_and_ambiguous_code_stays_text(self):
        action = block([{'name': 'read', 'file_path': '/fixture'}])
        for text in (
            '\n'.join('> ' + line for line in action.splitlines()),
            '\n'.join('>> ' + line for line in action.splitlines()),
            '> Quote\n' + action,
            '> ```tool_calls\n' + action + '\n> ```',
            '> ```text\n> example\n> ```\n\n' + action,
            '> ~~~text\n> example\n> ~~~\n\n' + action,
            '````text\n> quote\n\n' + action + '\n````',
            '> Quote\n\n    ' + action.replace('\n', '\n    '),
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse(text)[1])

    def test_prose_quote_does_not_bypass_batch_validation(self):
        for calls in ([{'name': 'read', 'file_path': 7}],
                      [{'name': 'read', 'file_path': '/fixture'}, {'name': 'unknown'}]):
            with self.assertRaises(ToolCallError):
                parse('> Ordinary quotation\n\n' + block(calls))

    def test_nested_schema_constraints_and_local_refs(self):
        schema = {'type': 'object', 'properties': {'items': {'type': 'array', 'minItems': 1,
                  'items': {'$ref': '#/$defs/entry'}}}, 'required': ['items'],
                  'additionalProperties': False, '$defs': {'entry': {'type': 'object',
                  'properties': {'mode': {'enum': ['read']}, 'limit': {'type': 'integer', 'minimum': 1}},
                  'required': ['mode', 'limit'], 'additionalProperties': False}}}
        tools = [{'name': 'inspect', 'parameters': schema}]
        good = {'items': [{'mode': 'read', 'limit': 2}]}
        self.assertIsNotNone(parse(block([{'name': 'inspect', 'arguments': good}]), tools)[1])
        for args in ({}, {'items': []}, {'items': [{'mode': 'write', 'limit': 2}]},
                     {'items': [{'mode': 'read', 'limit': True}]},
                     {'items': [{'mode': 'read', 'limit': 0}]}, {**good, 'extra': 1}):
            with self.subTest(args=args), self.assertRaises(ToolCallError):
                parse(block([{'name': 'inspect', 'arguments': args}]), tools)

    def test_remote_invalid_and_duplicate_schemas_fail_closed(self):
        for schema in ({'$ref': 'https://example.invalid/schema'}, {'type': 'invalid'},
                       {'$schema': 'https://example.invalid/dialect'}, False):
            with self.subTest(schema=schema), patch('urllib.request.urlopen', side_effect=AssertionError('network')) as network:
                with self.assertRaises(ToolCallError):
                    parse(block([{'name': 'inspect', 'arguments': {}}]), [{'name': 'inspect', 'parameters': schema}])
                network.assert_not_called()
        with self.assertRaises(ToolCallError):
            parse(block([{'name': 'read', 'arguments': {'file_path': '/fixture'}}]), FLAT_TOOLS * 2)
