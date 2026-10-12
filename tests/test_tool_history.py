import asyncio
import copy
import json
import unittest
from unittest.mock import Mock, patch

from server.schemas import ChatCompletionRequest, ChatMessage
from server.tool_history import validate_tool_history


def call(identity='c1', name='read'):
    return {'id': identity, 'type': 'function',
            'function': {'name': name, 'arguments': '{"file_path":"fixture"}'}}


class ToolHistoryTests(unittest.TestCase):
    def test_stream_roundtrip_continues_once_with_exact_call_ids(self):
        from server import api
        from chat_protocol import Reply
        from tests.support import isolated_access
        from tests.test_tools import block, FLAT_TOOLS
        isolated_access(self)
        fake = Mock()
        fake.chat.side_effect = [Reply(block([
            {'name': 'read', 'arguments': {'file_path': 'first'}},
            {'name': 'read', 'arguments': {'file_path': 'second'}},
        ]), 'owned:2'), Reply('Finished', 'owned:4')]
        req = ChatCompletionRequest(model='deepseek-chat', stream=True, tools=FLAT_TOOLS,
            messages=[ChatMessage(role='user', content='Inspect two files')])

        async def exchange(request):
            response = await api._chat_completions(request)
            return ''.join([chunk async for chunk in response.body_iterator])

        with patch.object(api, 'get_client', return_value=fake):
            raw = asyncio.run(exchange(req))
            frames = [json.loads(line[6:]) for line in raw.splitlines()
                      if line.startswith('data: ') and line != 'data: [DONE]']
            calls = [dict(call) for frame in frames
                     for call in frame['choices'][0]['delta'].get('tool_calls', [])]
            for call_value in calls:
                call_value.pop('index')
            self.assertEqual(len(calls), 2)
            self.assertEqual(frames[-1]['choices'][0]['finish_reason'], 'tool_calls')
            history = [*req.messages, ChatMessage(role='assistant', tool_calls=calls),
                       ChatMessage(role='tool', tool_call_id=calls[1]['id'], content='SECOND_BYTES'),
                       ChatMessage(role='tool', tool_call_id=calls[0]['id'], content='FIRST_BYTES')]
            continuation = req.model_copy(update={'messages': history})
            before = copy.deepcopy(history)
            self.assertIn('Finished', asyncio.run(exchange(continuation)))
            self.assertEqual(history, before)
            self.assertEqual(fake.chat.call_args_list[1].args[1:3], ('owned:2', None))
            sent = fake.chat.call_args_list[1].args[0]
            for value in ('FIRST_BYTES', 'SECOND_BYTES', calls[0]['id'], calls[1]['id']):
                self.assertIn(value, sent)
            self.assertNotIn('Inspect two files', sent)
            duplicate = asyncio.run(exchange(continuation))
            self.assertIn('already consumed', duplicate)
            self.assertEqual(fake.chat.call_count, 2)
            with api.conversation_cache.connection() as db:
                self.assertEqual(db.execute('SELECT steps, used FROM links ORDER BY steps').fetchall(),
                                 [(1, 1), (2, 0)])

    def test_parallel_results_out_of_order_are_preserved(self):
        messages = [ChatMessage(role='user', content='inspect'),
                    ChatMessage(role='assistant', tool_calls=[call(), call('c2')]),
                    ChatMessage(role='tool', tool_call_id='c2', content='second'),
                    ChatMessage(role='tool', tool_call_id='c1', content='first'),
                    ChatMessage(role='user', content='continue')]
        before = copy.deepcopy(messages)
        validate_tool_history(messages)
        self.assertEqual(messages, before)

    def test_invalid_ownership_and_incomplete_batches(self):
        assistant = ChatMessage(role='assistant', tool_calls=[call()])
        result = ChatMessage(role='tool', tool_call_id='c1', content='saved')
        for messages in ([result], [assistant], [assistant, result, result],
                         [assistant, ChatMessage(role='user', content='continue'), result],
                         [assistant, result, assistant, result],
                         [ChatMessage(role='user', tool_calls=[call()])],
                         [ChatMessage(role='user', tool_call_id='c1')],
                         [assistant, result.model_copy(update={'name': 'write'})]):
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                validate_tool_history(messages)

    def test_all_routes_reject_before_guard_factories_or_cache(self):
        from server import api
        for model in ('deepseek-chat', 'qwen3.8-max', 'kimi-instant'):
            for cid in (None, 'explicit-conversation'):
                req = ChatCompletionRequest(model=model, conversation_id=cid,
                    messages=[ChatMessage(role='tool', tool_call_id='unknown', content='result')])
                with patch.object(api, 'guard') as guard, \
                     patch.object(api, 'get_client') as factory, \
                     patch.object(api, 'conversation_cache') as cache:
                    response = asyncio.run(api._chat_completions(req))
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(guard.mock_calls, [])
                    self.assertEqual(factory.mock_calls, [])
                    self.assertEqual(cache.mock_calls, [])
