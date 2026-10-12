import json
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import httpx
import unittest
from unittest.mock import Mock, patch

from chat_protocol import Reply
from providers.access import AccessGuard, ProviderRejected
from server import api
from server.conversation_cache import ConversationCache, ConversationStateError
from server.schemas import ChatCompletionRequest, ChatMessage
from tests.support import isolated_access
from tests.test_tools import block, FLAT_TOOLS


class ConversationTests(unittest.TestCase):
    def test_local_stop_preserves_original_results_and_single_use_claim(self):
        from server.recovery_status import RECOVERY_STOP_TEXT
        self.cache.remember(self.req, self.reply, '', self.calls, 0)
        history = self.history() + [ChatMessage(role='assistant', content=RECOVERY_STOP_TEXT)]
        with self.assertRaises(ConversationStateError):
            self.cache.prepare(self.req.model_copy(update={'messages': history}))
        history.append(ChatMessage(role='user', content='Continue manually'))
        req = self.req.model_copy(update={'messages': history})
        continued, steps = self.cache.prepare(req)
        self.assertEqual(continued.conversation_id, self.reply.conversation_id)
        self.assertEqual(steps, 1)
        self.assertIn('fixture result', [m.content for m in continued.messages])
        with self.assertRaises(ConversationStateError):
            self.cache.prepare(req)
        with self.assertRaises(ConversationStateError):
            self.cache.prepare(self.req.model_copy(update={'messages': self.history()}))

    def test_local_stop_cannot_hide_changed_history_or_missing_results(self):
        from server.recovery_status import RECOVERY_STOP_TEXT
        self.cache.remember(self.req, self.reply, '', self.calls, 0)
        for history in (self.history()[:-1],
                        [ChatMessage(role='user', content='changed'), *self.history()[1:]]):
            history += [ChatMessage(role='assistant', content=RECOVERY_STOP_TEXT),
                        ChatMessage(role='user', content='Continue')]
            with self.assertRaises(ConversationStateError):
                self.cache.prepare(self.req.model_copy(update={'messages': history}))

    def setUp(self):
        isolated_access(self)
        self.cache = api.conversation_cache
        self.req = ChatCompletionRequest(model='deepseek-chat', tools=FLAT_TOOLS,
            messages=[ChatMessage(role='user', content='Explore owned fixture')])
        self.calls = [{'id': 'call_owned', 'type': 'function', 'function': {
            'name': 'read', 'arguments': '{"file_path":"fixture"}'}}]
        self.reply = Reply('unused', 'owned:2')

    def history(self):
        return [*self.req.messages, ChatMessage(role='assistant', tool_calls=self.calls),
                ChatMessage(role='tool', tool_call_id='call_owned', content='fixture result')]

    def test_restart_reuses_chat_and_only_new_messages_once(self):
        self.cache.remember(self.req, self.reply, '', self.calls, 0)
        req = self.req.model_copy(update={'messages': self.history()})
        restarted = ConversationCache(self.cache.path)
        continuation, steps = restarted.prepare(req)
        self.assertEqual(continuation.conversation_id, 'owned:2')
        self.assertEqual([m.role for m in continuation.messages], ['assistant', 'tool'])
        self.assertEqual(steps, 1)
        with self.assertRaises(ConversationStateError):
            restarted.prepare(req)
        self.assertNotIn('fixture result', self.cache.path.read_bytes().decode(errors='ignore'))
        self.assertEqual(self.cache.path.stat().st_mode & 0o777, 0o600)

    def test_owner_prefixed_model_alias_reuses_the_same_history(self):
        self.cache.remember(self.req, self.reply, '', self.calls, 0)
        req = self.req.model_copy(update={
            'model': 'deepseek/deepseek-chat', 'messages': self.history(),
        })
        continuation, steps = self.cache.prepare(req)
        self.assertEqual(continuation.conversation_id, 'owned:2')
        self.assertEqual(steps, 1)
        # A different bare model still must not share the link.
        with self.assertRaises(ConversationStateError):
            self.cache.prepare(self.req.model_copy(update={
                'model': 'deepseek/deepseek-expert', 'messages': self.history(),
            }))

    def test_unknown_changed_scope_and_edited_histories_never_start_chat(self):
        self.cache.remember(self.req, self.reply, '', self.calls, 0)
        for update in ({'model': 'deepseek-expert'}, {'user': 'other'}, {'tools': []},
                       {'thinking': True}, {'search': True}):
            req = self.req.model_copy(update={'messages': self.history(), **update})
            with self.assertRaises(ConversationStateError):
                self.cache.prepare(req)
        changed = self.history(); changed[0] = ChatMessage(role='user', content='other project')
        with self.assertRaises(ConversationStateError):
            self.cache.prepare(self.req.model_copy(update={'messages': changed}))

    def test_corrupt_state_fails_closed(self):
        self.cache.path.write_bytes(b'not sqlite')
        with self.assertRaises(ConversationStateError):
            self.cache.prepare(self.req.model_copy(update={'messages': self.history()}))

    def test_tool_round_trip_sends_delta_to_same_provider_chat(self):
        fake = Mock()
        fake.chat.side_effect = [Reply(block([{'name':'read','arguments':{'file_path':'fixture'}}]), 'owned:2'),
                                 Reply('Exploration complete', 'owned:4')]
        with patch.object(api, 'get_client', return_value=fake):
            first = api._conversation_tool_chat('initial fixture context', self.req, 'default')
            history = [*self.req.messages, ChatMessage(role='assistant', content=first[1], tool_calls=first[2]),
                       ChatMessage(role='tool', tool_call_id=first[2][0]['id'], content='fixture result')]
            req = self.req.model_copy(update={'messages': history})
            second = api._conversation_tool_chat('must not resend this history', req, 'default')
            self.assertEqual(second[1], 'Exploration complete')
            args = fake.chat.call_args_list[1].args
            self.assertEqual(args[1:3], ('owned:2', None))
            self.assertIn('fixture result', args[0])
            self.assertNotIn('Explore owned fixture', args[0])
            with self.assertRaises(api.ToolCallError):
                api._conversation_tool_chat('duplicate', req, 'default')
        self.assertEqual(fake.chat.call_count, 2)

    def test_conversation_step_cap(self):
        self.cache.remember(self.req, self.reply, '', self.calls, 19)
        with self.assertRaises(ConversationStateError):
            self.cache.prepare(self.req.model_copy(update={'messages': self.history()}))

    def test_raised_cap_resumes_existing_history_without_reset_or_replay(self):
        self.cache.remember(self.req, self.reply, '', self.calls, 19)
        req = self.req.model_copy(update={'messages': self.history()})
        with self.assertRaises(ConversationStateError):
            self.cache.prepare(req)
        with patch.dict('os.environ', {'CONVERSATION_MAX_REPLIES': '100'}):
            restarted = ConversationCache(self.cache.path)
            continuation, steps = restarted.prepare(req)
            self.assertEqual(continuation.conversation_id, 'owned:2')
            self.assertEqual(steps, 20)
            with self.assertRaises(ConversationStateError):
                restarted.prepare(req)

    def test_configured_cap_stops_before_claiming_history(self):
        with patch.dict('os.environ', {'CONVERSATION_MAX_REPLIES': '100'}):
            cache = ConversationCache(self.cache.path)
            cache.remember(self.req, self.reply, '', self.calls, 99)
            with self.assertRaisesRegex(ConversationStateError, '100 accepted replies'):
                cache.prepare(self.req.model_copy(update={'messages': self.history()}))
            with cache.connection() as db:
                self.assertEqual(db.execute('SELECT used, steps FROM links').fetchone(), (0, 100))

    def test_raise_100_to_200_preserves_history_and_stops_at_200(self):
        req = self.req.model_copy(update={'messages': self.history()})
        with patch.dict('os.environ', {'CONVERSATION_MAX_REPLIES': '100'}):
            old = ConversationCache(self.cache.path)
            old.remember(self.req, self.reply, '', self.calls, 99)
            with self.assertRaises(ConversationStateError):
                old.prepare(req)
        with patch.dict('os.environ', {'CONVERSATION_MAX_REPLIES': '200'}):
            restarted = ConversationCache(self.cache.path)
            continuation, steps = restarted.prepare(req)
            self.assertEqual((continuation.conversation_id, steps), ('owned:2', 100))
            with self.assertRaises(ConversationStateError):
                restarted.prepare(req)
            # A separate offline fixture exercises the new exact boundary.
            boundary = ConversationCache(self.cache.path.with_name('boundary.sqlite3'))
            boundary.remember(self.req, self.reply, '', self.calls, 199)
            with self.assertRaisesRegex(ConversationStateError, '200 accepted replies'):
                boundary.prepare(req)
            with boundary.connection() as db:
                self.assertEqual(db.execute('SELECT used, steps FROM links').fetchone(), (0, 200))

    def test_invalid_conversation_cap_fails_startup(self):
        for raw in ('0', '-1', '1001', 'nan', '1.5', '', '２０'):
            with self.subTest(raw=raw), patch.dict('os.environ', {'CONVERSATION_MAX_REPLIES': raw}):
                with self.assertRaises(ValueError):
                    ConversationCache(self.cache.path)

    def test_ambiguous_identical_histories_are_not_merged(self):
        self.cache.remember(self.req, self.reply, '', self.calls, 0)
        self.cache.remember(self.req, Reply('', 'another:2'), '', self.calls, 0)
        with self.assertRaises(ConversationStateError):
            self.cache.prepare(self.req.model_copy(update={'messages': self.history()}))

    def test_transport_creates_one_chat_for_two_agent_steps(self):
        from deepseek.client import DeepSeekClient
        from tests.test_stream import snapshot
        paths, bodies = [], []
        def handler(request):
            paths.append(request.url.path)
            if request.url.path.endswith('/create'):
                return httpx.Response(200, json={'code': 0, 'data': {'biz_code': 0, 'biz_data': {'id': 'owned'}}})
            if request.url.path.endswith('/create_pow_challenge'):
                return httpx.Response(200, json={'code': 0, 'data': {'biz_code': 0, 'biz_data': {'challenge': {}}}})
            bodies.append(json.loads(request.content))
            answer = block([{'name': 'read', 'arguments': {'file_path': 'fixture'}}]) if len(bodies) == 1 else 'Done'
            return httpx.Response(200, text=snapshot(answer, 'FINISHED').replace('\"message_id\": 2', '\"message_id\": ' + str(len(bodies) * 2)), headers={'content-type':'text/event-stream'})
        client = DeepSeekClient.__new__(DeepSeekClient)
        client.access_guard = None
        client._request_lock, client._pow_lock = threading.Lock(), threading.Lock()
        client._pow = Mock(); client._pow.make_header.return_value = 'owned-fixture'
        client._http = httpx.Client(base_url='https://offline.invalid', transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        with patch.object(api, 'get_client', return_value=client):
            first = api._conversation_tool_chat('initial context', self.req, 'default')
            req = self.req.model_copy(update={'messages': [*self.req.messages,
                ChatMessage(role='assistant', content=first[1], tool_calls=first[2]),
                ChatMessage(role='tool', tool_call_id=first[2][0]['id'], content='owned result')]})
            api._conversation_tool_chat('unused full history', req, 'default')
        self.assertEqual(sum(path.endswith('/create') for path in paths), 1)
        self.assertEqual(len(paths), 5)
        self.assertEqual(bodies[1]['chat_session_id'], bodies[0]['chat_session_id'])
        self.assertEqual(bodies[1]['parent_message_id'], 2)
        self.assertNotIn('initial context', bodies[1]['prompt'])
        self.assertEqual([k for _, k in api.guard._memory['deepseek']['usage']],
                         ['chat', 'http', 'generation', 'http', 'generation'])

    def test_simultaneous_continuations_cannot_claim_one_parent_twice(self):
        self.cache.remember(self.req, self.reply, '', self.calls, 0)
        req = self.req.model_copy(update={'messages': self.history()})
        def claim():
            try:
                return ConversationCache(self.cache.path).prepare(req)[0].conversation_id
            except ConversationStateError:
                return None
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: claim(), range(2)))
        self.assertEqual(results.count('owned:2'), 1)
        self.assertEqual(results.count(None), 1)

    def test_challenge_auth_recovery_is_pre_generation_only(self):
        from deepseek.client import DeepSeekClient
        from tests.test_stream import snapshot
        for mode in ('40002', '40003', 'string_code', 'timeout', 'http401',
                     'malformed', 'generic_auth', 'completion_auth', 'correction_auth'):
            with self.subTest(mode=mode):
                cache = ConversationCache(self.cache.path.with_name(mode + '.sqlite3'))
                cache.remember(self.req, self.reply, '', self.calls, 0)
                req = self.req.model_copy(update={'messages': self.history()})
                guard = AccessGuard(cache.path.with_suffix('.json'), interval=0)
                paths, bodies = [], []
                recovering = False
                def handler(request):
                    paths.append(request.url.path)
                    if request.url.path.endswith('create_pow_challenge'):
                        if recovering or mode == 'completion_auth' or mode == 'correction_auth' and not bodies:
                            return httpx.Response(200, json={'code':0,'data':{'biz_data':{'challenge':{}}}})
                        if mode == 'timeout':
                            raise httpx.ReadTimeout('offline', request=request)
                        if mode == 'http401':
                            return httpx.Response(401)
                        if mode == 'malformed':
                            return httpx.Response(200, text='invalid')
                        if mode == 'generic_auth':
                            return httpx.Response(200, json={'code':999,'msg':'invalid_token'})
                        return httpx.Response(200, json={'code':'40003' if mode == 'string_code' else 40002 if mode == '40002' else 40003})
                    self.assertTrue(request.url.path.endswith('/completion'))
                    bodies.append(json.loads(request.content))
                    if mode == 'completion_auth':
                        return httpx.Response(200, json={'code':40003})
                    answer = 'Recovered' if recovering else '```tool_calls\nnot-json\n```'
                    return httpx.Response(200, text=snapshot(answer, 'FINISHED').replace('"message_id": 2', '"message_id": 4'), headers={'content-type':'text/event-stream'})
                client = DeepSeekClient.__new__(DeepSeekClient)
                client.access_guard = None
                client._request_lock, client._pow_lock = threading.Lock(), threading.Lock()
                client._pow = Mock(); client._pow.make_header.return_value = 'offline'
                client._http = httpx.Client(base_url='https://offline.invalid', transport=httpx.MockTransport(handler))
                self.addCleanup(client.close)
                with patch.object(api, 'conversation_cache', cache), patch.object(api, 'guard', guard), patch.object(api, 'get_client', return_value=client), patch.object(api, '_FORMAT_REPAIR', True):
                    with self.assertRaises(Exception):
                        api._conversation_tool_chat('unused', req, None)
                    safe = mode in ('40002', '40003', 'string_code')
                    with cache.connection() as db:
                        self.assertEqual(db.execute('SELECT used,steps FROM links').fetchone(), (0 if safe else 1, 1))
                    charged = json.loads(guard.path.read_text())['deepseek']['usage']
                    count = len(paths)
                    with self.assertRaises(ProviderRejected):
                        api._conversation_tool_chat('unused', req, None)
                    self.assertEqual(len(paths), count)
                    if safe:
                        self.assertEqual([kind for _,kind in charged], ['http'])
                        # Simulate explicit manual recovery; no counters reset.
                        guard.resume('deepseek')
                        self.assertEqual(json.loads(guard.path.read_text())['deepseek']['usage'], charged)
                        recovering = True
                        api._conversation_tool_chat('unused', req, None)
                        self.assertEqual(len(bodies), 1)
                        self.assertEqual((bodies[0]['chat_session_id'], bodies[0]['parent_message_id']), ('owned', 2))

    def test_account_change_does_not_reuse_previous_chat(self):
        account = self.cache.path.parent / 'synthetic-account.json'
        account.write_text('synthetic account A')
        cache = ConversationCache(self.cache.path, account)
        cache.remember(self.req, self.reply, '', self.calls, 0)
        account.write_text('synthetic account B')
        with self.assertRaises(ConversationStateError):
            cache.prepare(self.req.model_copy(update={'messages': self.history()}))

    def test_confirmed_rotation_resumes_exact_history_without_reset(self):
        from deepseek.auth import Session
        old = Session('old', [], 'fixture', 1)
        fresh = Session('new', [], 'fixture', 2)
        cache = ConversationCache(self.cache.path, self.cache.path.parent/'account.json')
        cache.remember(self.req, self.reply, '', self.calls, 0,
                       scope='session-v2:' + old.identity())
        req = self.req.model_copy(update={'messages': self.history()})
        with self.assertRaises(ConversationStateError):
            cache.prepare(req, scope='session-v2:' + fresh.identity())
        fresh.preserve_history_from(old, same_account_confirmed=True)
        fake = Mock(); fake.session = fresh
        fake.chat.return_value = Reply('Done', 'owned:4')
        with patch.object(api, 'conversation_cache', cache), patch.object(api, 'get_client', return_value=fake):
            api._conversation_tool_chat('unused', req, None)
            self.assertEqual(fake.chat.call_args.args[1], 'owned:2')
            with self.assertRaises(api.ToolCallError):
                api._conversation_tool_chat('unused', req, None)
        self.assertEqual(fake.chat.call_count, 1)

    def test_oversized_input_does_not_build_client(self):
        with patch.object(api, 'get_client') as factory, self.assertRaises(ProviderRejected):
            api._chat_with_retry('x' * 131073, self.req, 'default')
        factory.assert_not_called()

    def test_qwen_browser_stateless_continuation_reuses_chat_path(self):
        from providers.conversations import encode
        chat = '00000000-0000-4000-8000-000000000099'
        cid = encode('qwen', 'default', '/c/' + chat)
        fake = Mock()
        fake.chat.side_effect = [Reply('привет!', cid), Reply('второй ответ', cid)]
        req = ChatCompletionRequest(
            model='qwen-web',
            messages=[
                ChatMessage(role='system', content='You are helpful.'),
                ChatMessage(role='user', content='привет'),
            ],
        )
        with patch.object(api, 'model_provider', return_value='qwen'), \
             patch.object(api, 'qwen_uses_browser', return_value=True), \
             patch.object(api, '_request_client', return_value=fake):
            first = api._conversation_tool_chat('unused full history', req, 'default')
            follow = req.model_copy(update={'messages': [
                *req.messages,
                ChatMessage(role='assistant', content=first[1]),
                ChatMessage(role='user', content='ещё вопрос'),
            ]})
            second = api._conversation_tool_chat('must not resend this history', follow, 'default')
        self.assertEqual(second[1], 'второй ответ')
        self.assertEqual(fake.chat.call_count, 2)
        prompt, conversation_id, model_type = fake.chat.call_args_list[1].args[:3]
        self.assertEqual(conversation_id, cid)
        self.assertIsNone(model_type)
        self.assertEqual(prompt, 'ещё вопрос')
        self.assertNotIn('You are helpful', prompt)
        self.assertNotIn('привет', prompt)

    def test_qwen_browser_edited_history_does_not_open_another_chat(self):
        from providers.conversations import encode
        chat = '00000000-0000-4000-8000-000000000098'
        cid = encode('qwen', 'default', '/c/' + chat)
        fake = Mock()
        fake.chat.return_value = Reply('ok', cid)
        req = ChatCompletionRequest(
            model='qwen-web',
            messages=[ChatMessage(role='user', content='first')],
        )
        with patch.object(api, 'model_provider', return_value='qwen'), \
             patch.object(api, 'qwen_uses_browser', return_value=True), \
             patch.object(api, '_request_client', return_value=fake):
            first = api._conversation_tool_chat('first', req, 'default')
            edited = req.model_copy(update={'messages': [
                ChatMessage(role='user', content='edited first'),
                ChatMessage(role='assistant', content=first[1]),
                ChatMessage(role='user', content='next'),
            ]})
            before = fake.chat.call_count
            with self.assertRaises(api.ToolCallError):
                api._conversation_tool_chat('must not dispatch', edited, 'default')
            self.assertEqual(fake.chat.call_count, before)



class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'guard.json'

    def charge(self, guard, kind):
        with guard.attempt('deepseek') as attempt:
            attempt.dispatch(kind=kind)

    def test_chat_budget_survives_restart_resume_and_expires(self):
        with patch('providers.access.time.time', return_value=100000):
            guard = AccessGuard(self.path, interval=0)
            for _ in range(3):
                self.charge(guard, 'chat')
            guard = AccessGuard(self.path, interval=0)
            guard.resume('deepseek')
            with self.assertRaises(ProviderRejected) as error:
                self.charge(guard, 'chat')
            self.assertEqual(error.exception.code, 'local_budget_exceeded')
            self.assertEqual(guard.status(), {})
        with patch('providers.access.time.time', return_value=103601):
            for _ in range(3):
                self.charge(guard, 'chat')
        with patch('providers.access.time.time', return_value=107202):
            with self.assertRaises(ProviderRejected):
                self.charge(guard, 'chat')
        with patch('providers.access.time.time', return_value=190000):
            self.charge(guard, 'chat')

    def test_generation_cap_and_all_http_dispatches_charged(self):
        guard = AccessGuard(self.path, interval=0)
        for _ in range(12):
            self.charge(guard, 'generation')
        with self.assertRaises(ProviderRejected) as error:
            self.charge(guard, 'generation')
        self.assertEqual(error.exception.code, 'local_budget_exceeded')
        with guard.attempt('deepseek') as attempt:
            attempt.dispatch(kind='chat')
            attempt.dispatch()
        self.assertEqual(len(json.loads(self.path.read_text())['deepseek']['usage']), 14)

    def test_failed_calls_are_not_refunded(self):
        guard = AccessGuard(self.path, interval=0)
        with self.assertRaises(RuntimeError):
            with guard.attempt('deepseek') as attempt:
                attempt.dispatch(kind='chat')
                raise RuntimeError('offline failure')
        guard.resume('deepseek')
        self.assertEqual(len(json.loads(self.path.read_text())['deepseek']['usage']), 1)

    def test_malformed_usage_fails_closed(self):
        self.path.write_text('{"deepseek":{"usage":[[0,"unknown"]]}}')
        with self.assertRaises(ProviderRejected):
            AccessGuard(self.path, interval=0).check('deepseek')

    def test_preflight_rejects_before_wasting_chat_or_challenge(self):
        guard = AccessGuard(self.path, interval=0)
        for _ in range(12):
            self.charge(guard, 'generation')
        with self.assertRaises(ProviderRejected):
            with guard.attempt('deepseek') as attempt:
                attempt.preflight_generation(True)
        self.assertEqual(len(json.loads(self.path.read_text())['deepseek']['usage']), 12)
        self.assertEqual(guard.status(), {})

    def test_http_budget_preflight_requires_room_for_whole_sequence(self):
        guard = AccessGuard(self.path, interval=0)
        for _ in range(39):
            self.charge(guard, 'http')
        with self.assertRaises(ProviderRejected):
            with guard.attempt('deepseek') as attempt:
                attempt.preflight_generation(False)
        self.assertEqual(len(json.loads(self.path.read_text())['deepseek']['usage']), 39)

    def test_configured_limit_preserves_usage_and_preflight(self):
        guard = AccessGuard(self.path, interval=0)
        for _ in range(12):
            self.charge(guard, 'generation')
        with patch.dict('os.environ', {'DEEPSEEK_MAX_GENERATIONS_PER_HOUR': '13'}):
            raised = AccessGuard(self.path, interval=0)
            with raised.attempt('deepseek') as attempt:
                attempt.preflight_generation(False)
                attempt.dispatch(kind='generation')
            with self.assertRaises(ProviderRejected):
                with raised.attempt('deepseek') as attempt:
                    attempt.preflight_generation(False)
        self.assertEqual(len(json.loads(self.path.read_text())['deepseek']['usage']), 13)
        with self.assertRaises(ProviderRejected):
            self.charge(AccessGuard(self.path, interval=0), 'generation')

    def test_invalid_budget_configuration_is_rejected(self):
        for value in ('0', '-1', '2001', '1.5', 'nan', '', '１２', '61'):
            with self.subTest(value=value), patch.dict('os.environ',
                    {'DEEPSEEK_MAX_GENERATIONS_PER_HOUR': value}):
                with self.assertRaises(ValueError):
                    AccessGuard(self.path, interval=0)

    def test_configured_http_preflight_reserves_whole_sequence(self):
        with patch.dict('os.environ', {'DEEPSEEK_MAX_HTTP_PER_HOUR': '2'}):
            guard = AccessGuard(self.path, interval=0)
            with self.assertRaises(ProviderRejected):
                with guard.attempt('deepseek') as attempt:
                    attempt.preflight_generation(True)
            self.assertEqual(json.loads(self.path.read_text())['deepseek'].get('usage', []), [])

    def test_http_pacing_is_persisted_inside_one_operation(self):
        guard = AccessGuard(self.path, interval=30)
        with patch('providers.access.time.time', return_value=100000):
            with guard.attempt('deepseek') as attempt:
                attempt.dispatch(kind='chat')
                record = json.loads(self.path.read_text())['deepseek']
                self.assertEqual(record['next_at'], 100030)
                self.assertEqual(record['next_http_at'], 100003)
                with patch('providers.access.time.time', return_value=100003):
                    attempt.dispatch()
                self.assertEqual(json.loads(self.path.read_text())['deepseek']['next_http_at'], 100006)
