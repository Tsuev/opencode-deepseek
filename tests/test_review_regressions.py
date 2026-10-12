"""Regressions from the independent offline Astra review; synthetic HTTP only."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import httpx

from deepseek.auth import Session
from deepseek.client import DeepSeekClient
from providers import access
from providers.access import AccessGuard, ProviderRejected
from server import api
from server.conversation_cache import ConversationCache, digest
from server.openai_format import messages_to_prompt
from server.schemas import ChatCompletionRequest, ChatMessage
from tests.test_tools import block, FLAT_TOOLS


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.account = self.root / 'synthetic-session.json'
        self.account.write_text('synthetic A')
        self.cache = ConversationCache(self.root / 'cache.sqlite3', self.account)
        self.guard = AccessGuard(self.root / 'guard.json', interval=0)
        self.http_calls = []
        self.outputs = []
        self.generation = 0
        self.a = self.make_client('A')
        self.b = self.make_client('B')
        for target, name, value in ((api, 'guard', self.guard), (access, 'guard', self.guard),
                                     (api, 'conversation_cache', self.cache),
                                     (api, '_FORMAT_REPAIR', False), (api, '_MAX_CONTINUATIONS', 0)):
            patcher = patch.object(target, name, value); patcher.start(); self.addCleanup(patcher.stop)
        self.factory = patch.object(api, 'get_client', return_value=self.a).start()
        self.addCleanup(patch.stopall)
        self.req = ChatCompletionRequest(model='deepseek-chat', messages=[ChatMessage(role='user', content='fixture')])

    def make_client(self, name):
        client = DeepSeekClient.__new__(DeepSeekClient)
        client.session = Session('synthetic-' + name, [], 'fixture', 1000)
        client.access_guard = None
        client._request_lock = threading.Lock(); client._pow_lock = threading.Lock()
        client._pow = Mock(); client._pow.make_header.return_value = 'synthetic-proof'
        def handler(request):
            self.http_calls.append((name, request.url.path, json.loads(request.content)))
            if request.url.path.endswith('/create'):
                return httpx.Response(200, json={'code':0,'data':{'biz_data':{'id':'chat-' + name}}})
            if request.url.path.endswith('/create_pow_challenge'):
                return httpx.Response(200, json={'code':0,'data':{'biz_data':{'challenge':{}}}})
            self.generation += 1
            response = self.outputs.pop(0) if self.outputs else {'message_id':self.generation * 2,'status':'FINISHED','fragments':[{'type':'RESPONSE','content':'answer'}]}
            return httpx.Response(200, text='data: ' + json.dumps({'v':{'response':response}}) + '\n\n', headers={'content-type':'text/event-stream'})
        client._http = httpx.Client(base_url='https://offline.invalid', trust_env=False, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def run_turn(self, req):
        return api._conversation_tool_chat(messages_to_prompt(req.messages, req.tools), req, 'default')

    def follow(self, req, result, tail=None):
        return req.model_copy(update={'messages':[*req.messages, ChatMessage(role='assistant', content=result[1], tool_calls=result[2]),
            *(tail if tail is not None else [ChatMessage(role='user', content='next')])]})

    def test_session_swap_after_prepare_keeps_pinned_client_and_cache_scope(self):
        first = self.run_turn(self.req); following = self.follow(self.req, first)
        original = self.cache.prepare
        def swap(*args, **kwargs):
            result = original(*args, **kwargs)
            self.account.write_text('synthetic B')
            self.factory.return_value = self.b
            return result
        with patch.object(self.cache, 'prepare', side_effect=swap):
            second = self.run_turn(following)
        self.assertTrue(all(name == 'A' for name, _, _ in self.http_calls))
        self.assertEqual(self.factory.call_count, 2)
        before = len(self.http_calls)
        with self.assertRaises(api.ToolCallError):
            self.run_turn(self.follow(following, second))
        self.assertEqual(len(self.http_calls), before)
        self.factory.return_value = self.a
        self.run_turn(self.follow(following, second))

    def test_session_swap_at_remember_cannot_store_old_chat_under_new_scope(self):
        original = self.cache.remember
        def swap(*args, **kwargs):
            self.account.write_text('synthetic B')
            self.factory.return_value = self.b
            return original(*args, **kwargs)
        with patch.object(self.cache, 'remember', side_effect=swap):
            first = self.run_turn(self.req)
        before = len(self.http_calls)
        with self.assertRaises(api.ToolCallError):
            self.run_turn(self.follow(self.req, first))
        self.assertEqual(len(self.http_calls), before)
        self.factory.return_value = self.a
        self.run_turn(self.follow(self.req, first))

    def test_client_identity_and_credentials_use_the_same_loaded_snapshot(self):
        # Exercise the real function, not the fixture factory.
        with patch.object(api, '_client', None), patch.object(api, '_client_identity', None), \
             patch.object(api, 'get_session', return_value=self.a.session) as load, \
             patch.object(api, '_build_client', return_value=self.a) as build:
            self.assertIs(REAL_GET_CLIENT(), self.a)
            self.assertIs(build.call_args.kwargs['session'], self.a.session)
            self.assertEqual(api._client_identity, api._session_identity(self.a.session))
            load.assert_called_once()

    def test_out_of_order_results_include_exact_call_argument_mapping(self):
        text = block([{'name':'read','arguments':{'file_path':'A.txt'}}, {'name':'read','arguments':{'file_path':'B.txt'}}])
        self.outputs = [{'message_id':2,'status':'FINISHED','fragments':[{'type':'RESPONSE','content':text}]}]
        req = self.req.model_copy(update={'tools':FLAT_TOOLS})
        first = self.run_turn(req); calls = first[2]
        self.run_turn(self.follow(req, first, [ChatMessage(role='tool', tool_call_id=calls[1]['id'], content='B value'), ChatMessage(role='tool', tool_call_id=calls[0]['id'], content='A value')]))
        delta = self.http_calls[-1][2]['prompt']
        for call in calls:
            self.assertIn(call['id'], delta)
            self.assertIn(json.loads(call['function']['arguments'])['file_path'], delta)
        self.assertIn('B value', delta); self.assertIn('A value', delta)

    def test_text_parts_hash_matches_wire_text_and_rejects_changed_history(self):
        req = self.req.model_copy(update={'messages':[ChatMessage(role='user', content=[{'type':'text','text':'a'}, {'type':'text','text':'b'}])]})
        same = req.model_copy(update={'messages':[ChatMessage(role='user', content='a\nb')]})
        different = req.model_copy(update={'messages':[ChatMessage(role='user', content='ab')]})
        self.assertEqual(digest(req, req.messages), digest(same, same.messages))
        self.assertNotEqual(digest(req, req.messages), digest(different, different.messages))
        first = self.run_turn(req); before = len(self.http_calls)
        with self.assertRaises(api.ToolCallError):
            self.run_turn(self.follow(different, first))
        self.assertEqual(len(self.http_calls), before)
        self.run_turn(self.follow(same, first))

    def test_budget_rejection_releases_only_undispatched_reservation(self):
        first = self.run_turn(self.req); following = self.follow(self.req, first)
        for _ in range(11):
            with self.guard.attempt('deepseek') as attempt:
                attempt.dispatch(kind='generation')
        before = len(self.http_calls)
        with self.assertRaises(ProviderRejected) as error:
            self.run_turn(following)
        self.assertEqual(error.exception.code, 'local_budget_exceeded')
        self.assertEqual(len(self.http_calls), before)
        with patch('providers.access.time.time', return_value=time.time() + 3601):
            self.run_turn(following)
        with self.assertRaises(api.ToolCallError):
            self.run_turn(following)

    def test_cancellation_before_dispatch_preserves_continuation(self):
        first = self.run_turn(self.req); following = self.follow(self.req, first)
        before = len(self.http_calls)
        with patch.object(self.a, 'chat', side_effect=asyncio.CancelledError), self.assertRaises(asyncio.CancelledError):
            self.run_turn(following)
        self.assertEqual(len(self.http_calls), before)
        self.run_turn(following)

    def test_missing_or_stale_message_id_stops_without_cache_commit_or_replay(self):
        first = self.run_turn(self.req); following = self.follow(self.req, first)
        self.outputs = [{'status':'FINISHED','fragments':[{'type':'RESPONSE','content':'uncertain'}]}]
        with self.assertRaises(ProviderRejected):
            self.run_turn(following)
        before = len(self.http_calls)
        self.guard.resume('deepseek')  # Synthetic fixture only.
        with self.assertRaises(api.ToolCallError):
            self.run_turn(following)
        self.assertEqual(len(self.http_calls), before)

    def test_repeated_parent_message_id_is_rejected(self):
        first = self.run_turn(self.req)
        self.outputs = [{'message_id':2,'status':'FINISHED','fragments':[{'type':'RESPONSE','content':'stale'}]}]
        with self.assertRaises(ProviderRejected):
            self.run_turn(self.follow(self.req, first))
        self.assertEqual(self.guard.status()['deepseek'], 'upstream_failure')


REAL_GET_CLIENT = api.get_client
