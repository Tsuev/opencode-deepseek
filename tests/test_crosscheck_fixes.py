"""Regressions for the 2026-10-12 Opus review and Codex cross-check findings."""
import asyncio
import base64
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from chat_protocol import Reply
from providers import access, antigravity, tab_bridge
from providers.access import AccessGuard, ProviderRejected, current_attempt
from providers.browser_protocol import qwen_answer, qwen_reply
from providers.common import ProviderUnavailable
from server import api, api_auth
from server.conversation_cache import ConversationCache
from server.schemas import ChatCompletionRequest
from server.tool_protocol import validate_reply
from server.openai_format import ToolCallError
from tests import NO_FIXTURE_AUTH, TEST_API_TOKEN

TOOLS = [{'type': 'function', 'function': {'name': 'read', 'parameters': {
    'type': 'object', 'required': ['file_path'], 'additionalProperties': False,
    'properties': {'file_path': {'type': 'string'}}}}}]
CALL = '```tool_calls\n' + json.dumps([{'name': 'read', 'arguments': {'file_path': 'fixture'}}]) + '\n```'


def jwt(identity, kind='email', exp=None):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    return part({'alg': 'none'}) + '.' + part({'id': identity, 'type': kind, 'exp': exp or time.time() + 3600}) + '.sig'


class ApiAuthBoundary(unittest.IsolatedAsyncioTestCase):
    """P1-1 / P2-1: /v1 requires the private token and a local Host."""

    async def asyncSetUp(self):
        self.reached = []

        async def handler(*args, **kwargs):
            self.reached.append(True)
            return Reply('ok', None), 'ok', None
        self.patches = [patch.object(api, '_run_tool_chat', handler),
                        patch.object(api, 'guard', AccessGuard(path=None, interval=0)),
                        patch.dict(api.MODEL_MAP, {'deepseek-chat': 'default'}, clear=True)]
        for item in self.patches:
            item.start()
        self.body = {'model': 'deepseek-chat', 'messages': [{'role': 'user', 'content': 'x'}]}

    async def asyncTearDown(self):
        for item in self.patches:
            item.stop()

    async def post(self, headers, client=('127.0.0.1', 1), base='http://127.0.0.1:8000'):
        transport = httpx.ASGITransport(app=api.app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url=base) as http:
            return await http.post('/v1/chat/completions', json=self.body, headers=headers)

    async def test_missing_wrong_and_browser_requests_never_reach_provider(self):
        for headers in ({NO_FIXTURE_AUTH: '1'}, {'Authorization': 'Bearer wrong'},
                        {NO_FIXTURE_AUTH: '1', 'Origin': 'https://evil.example', 'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(headers=headers):
                response = await self.post(headers)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn('x-ratelimit-remaining', response.headers)  # no limiter spend
                self.assertIn(str(api_auth.token_path()), response.json()['error']['message'])
        self.assertEqual(self.reached, [])

    async def test_foreign_host_is_refused_even_with_token(self):
        response = await self.post({'Authorization': 'Bearer ' + TEST_API_TOKEN}, base='http://attacker.example')
        self.assertEqual(response.status_code, 421)
        self.assertEqual(self.reached, [])

    async def test_valid_token_and_local_host_are_admitted(self):
        response = await self.post({'Authorization': 'Bearer ' + TEST_API_TOKEN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.reached, [True])

    async def test_legacy_mode_admits_only_tokenless_loopback_non_browser_requests(self):
        with patch.dict(os.environ, {'API_AUTH': 'legacy-loopback'}):
            self.assertEqual((await self.post({NO_FIXTURE_AUTH: '1'})).status_code, 200)
            for extra in ({'Origin': 'https://evil.example'}, {'Sec-Fetch-Site': 'cross-site'},
                          {'Sec-Fetch-Mode': 'no-cors'}):
                self.assertEqual((await self.post({NO_FIXTURE_AUTH: '1', **extra})).status_code, 401)
            self.assertEqual((await self.post({NO_FIXTURE_AUTH: '1'}, client=('192.0.2.5', 1))).status_code, 401)
        self.assertEqual(self.reached, [True])

    async def test_health_stays_open_and_invalid_mode_fails_closed(self):
        transport = httpx.ASGITransport(app=api.app, client=('127.0.0.1', 1))
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1') as http:
            self.assertEqual((await http.get('/healthz')).status_code, 200)
        with patch.dict(os.environ, {'API_AUTH': 'off'}), self.assertRaises(ValueError):
            api_auth.mode()

    def test_host_authority_is_parsed_strictly(self):
        """R1."""
        token = {'authorization': 'Bearer ' + TEST_API_TOKEN}
        for host in ('localhost', 'localhost:8000', '127.0.0.1:65535', '[::1]', '[::1]:8000', 'LOCALHOST:80'):
            with self.subTest(host=host):
                self.assertIsNone(api_auth.decide('/v1/models', '127.0.0.1', {**token, 'host': host}))
        for host in ('localhost:garbage', 'localhost:', '127.0.0.1:65536', 'localhost:0', '[::1]:bad:port',
                     '[::1]:', '[127.0.0.1]', 'a:b:c', 'x@localhost', '[::1]x', '', 'local host'):
            with self.subTest(host=host):
                self.assertEqual(api_auth.decide('/v1/models', '127.0.0.1', {**token, 'host': host})[0], 421)
        self.assertEqual(api_auth.decide('/v1/models', '127.0.0.1', token)[0], 421)

    async def test_oversized_port_is_a_controlled_refusal(self):
        """N4: no int() on unbounded digits before authorization."""
        transport = httpx.ASGITransport(app=api.app, client=('127.0.0.1', 1))
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1') as http:
            response = await http.get('/v1/models', headers={'Host': 'localhost:' + '9' * 4301, NO_FIXTURE_AUTH: '1'})
        self.assertEqual(response.status_code, 421)
        self.assertIsNone(api_auth._host_name('localhost:' + '1' * 6))

    async def test_duplicate_host_headers_are_refused(self):
        transport = httpx.ASGITransport(app=api.app, client=('127.0.0.1', 1))
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1') as http:
            request = http.build_request('GET', '/v1/models', headers={'Authorization': 'Bearer ' + TEST_API_TOKEN})
            request.headers = httpx.Headers([*request.headers.raw, (b'host', b'localhost')])
            self.assertEqual((await http.send(request)).status_code, 421)

    def test_legacy_mode_refuses_any_browser_provenance_header(self):
        """R2: presence, not value."""
        base = {'host': '127.0.0.1:8000'}
        with patch.dict(os.environ, {'API_AUTH': 'legacy-loopback'}):
            self.assertIsNone(api_auth.decide('/v1/models', '127.0.0.1', base))
            for extra in ({'origin': ''}, {'origin': 'null'}, {'sec-fetch-site': ''},
                          {'sec-fetch-dest': 'empty'}, {'Sec-Fetch-User': '?1'}):
                with self.subTest(extra=extra):
                    self.assertEqual(api_auth.decide('/v1/models', '127.0.0.1', {**base, **extra})[0], 401)

    def test_public_bind_requires_token_mode_and_explicit_opt_in(self):
        self.assertIsNone(api_auth.public_bind_refusal('127.0.0.1'))
        self.assertIsNone(api_auth.public_bind_refusal('localhost'))
        self.assertIn('API_ALLOW_PUBLIC_BIND', api_auth.public_bind_refusal('0.0.0.0'))
        with patch.dict(os.environ, {'API_ALLOW_PUBLIC_BIND': '1'}):
            self.assertIsNone(api_auth.public_bind_refusal('0.0.0.0'))
            with patch.dict(os.environ, {'API_AUTH': 'legacy-loopback'}):
                self.assertIn('legacy', api_auth.public_bind_refusal('0.0.0.0'))

    def test_token_file_is_private_and_created_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nested' / 'api-token'
            value = api_auth.ensure_token(path)
            self.assertEqual(api_auth.ensure_token(path), value)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                api_auth.ensure_token(link)

    def test_harness_installer_writes_bridge_token_not_placeholder(self):
        from integrations import harness
        config = harness.provider_config('http://127.0.0.1:8000/v1', [])
        self.assertEqual(config['headers']['Authorization'], 'Bearer ' + TEST_API_TOKEN)


class QwenRenewalInsideAttempt(unittest.IsolatedAsyncioTestCase):
    """P2-2: opted-in renewal runs under the durable attempt; scope survives rotation."""

    async def asyncSetUp(self):
        from qwen import auth as qwen_auth
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.session_file = root / 'qwen-session.json'
        self.old = qwen_auth.Session(jwt('account-1', exp=time.time() - 10), [], 'UA', 1.0, expires_at=time.time() - 10)
        self.old.save(self.session_file)
        self.fresh = qwen_auth.Session(jwt('account-1'), [], 'UA', 2.0, expires_at=time.time() + 900)
        self.guard = AccessGuard(path=root / 'pauses.json', interval=0)
        self.cache = ConversationCache(root / 'links.sqlite3', account_file=root / 'session.json')
        self.renewals, self.sent = [], []
        self.patches = [
            patch.object(api, 'guard', self.guard), patch.object(access, 'guard', self.guard),
            patch.object(api, 'conversation_cache', self.cache),
            patch.object(api, 'QWEN_RENEW_SESSION', True),
            patch.object(qwen_auth, 'DEFAULT_SESSION_FILE', self.session_file),
            patch.object(api, 'get_qwen_session', side_effect=self.cached_or_login),
            patch.object(api, 'renew_qwen_session', side_effect=self.renew),
            patch.object(api, 'QwenClient', side_effect=self.client),
            patch.object(api, '_qwen_client', None),
            patch.dict(api.MODEL_MAP, {'qwen3.8-max': 'qwen3.8-max'}, clear=True),
        ]
        for item in self.patches:
            item.start()
        self.current = None

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def cached_or_login(self, **kwargs):
        if self.current is None:
            raise api.QwenLoginRequired()
        return self.current

    def renew(self, expected_account=None):
        attempt = current_attempt('qwen')
        self.renewals.append(attempt is not None and not attempt.dispatched
                             and expected_account == api._qwen_account(self.old))
        attempt.dispatch()
        self.current = self.fresh
        return self.fresh

    def client(self, session, access_guard=None):
        fake = Mock(session=session)

        def chat(prompt, *args):
            current_attempt('qwen').dispatch(kind='generation')
            self.sent.append(prompt)
            return Reply('answer', 'qwen-chat:1')
        fake.chat.side_effect = chat
        return fake

    async def test_expired_session_renews_under_attempt_and_completes(self):
        req = ChatCompletionRequest(model='qwen3.8-max', messages=[{'role': 'user', 'content': 'hi'}])
        response = await api._chat_completions(req)
        self.assertEqual(response['choices'][0]['message']['content'], 'answer')
        self.assertEqual(self.renewals, [True])
        self.assertEqual(len(self.sent), 1)
        self.guard.check('qwen')  # no pause, no pending attempt

    def test_scope_is_bound_to_account_not_token(self):
        class Client:
            def __init__(self, session):
                self.session = session
        self.assertEqual(api._conversation_scope('qwen', Client(self.old)),
                         api._conversation_scope('qwen', Client(self.fresh)))

    async def test_account_switch_after_history_is_refused_before_any_request(self):
        """A manual capture of another account must not continue this history."""
        from qwen import auth as qwen_auth
        self.current = qwen_auth.Session(jwt('account-1'), [], 'UA', 2.0, expires_at=time.time() + 900)
        tools = TOOLS
        first = ChatCompletionRequest(model='qwen3.8-max', tools=tools, messages=[{'role': 'user', 'content': 'hi'}])
        calls = [{'id': 'call_1', 'type': 'function', 'function': {'name': 'read', 'arguments': '{"file_path":"f"}'}}]

        class Snapshot:
            session = self.current
        self.cache.remember(first, Reply('', 'qwen-chat:1'), '', calls, 0,
                            scope=api._conversation_scope('qwen', Snapshot()))
        nxt = ChatCompletionRequest(model='qwen3.8-max', tools=tools, messages=[
            {'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': None, 'tool_calls': calls},
            {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'data'}])
        self.current = qwen_auth.Session(jwt('account-2'), [], 'UA', 3.0, expires_at=time.time() + 900)
        response = await api._chat_completions(nxt)
        self.assertIn(response.status_code, (400, 401))
        self.assertEqual(self.sent, [])
        self.guard.check('qwen')  # refused before dispatch: no pause


class QwenRenewalAccountRace(unittest.IsolatedAsyncioTestCase):
    """R3: a session file replaced by another account between prepare and the
    attempt must be refused by renewal itself, before any dispatch."""

    async def test_foreign_account_renewal_is_refused_before_dispatch(self):
        from qwen import auth as qwen_auth
        from qwen import renewal
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_file = root / 'qwen.json'
            guard = AccessGuard(path=root / 'pauses.json', interval=0)
            cache = ConversationCache(root / 'links.sqlite3', account_file=root / 'session.json')
            account_a = qwen_auth.Session(jwt('account-A', exp=time.time() - 5), [], 'UA', 1.0, expires_at=time.time() - 5)
            account_a.save(session_file)
            calls = [{'id': 'call_1', 'type': 'function', 'function': {'name': 'read', 'arguments': '{"file_path":"f"}'}}]
            first = ChatCompletionRequest(model='qwen3.8-max', tools=TOOLS, messages=[{'role': 'user', 'content': 'hi'}])

            class Snapshot:
                session = account_a
            cache.remember(first, Reply('', 'qwen-chat:1'), '', calls, 0, scope=api._conversation_scope('qwen', Snapshot()))
            nxt = ChatCompletionRequest(model='qwen3.8-max', tools=TOOLS, messages=[
                {'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': None, 'tool_calls': calls},
                {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'data'}])
            original_prepare = cache.prepare

            def prepare_then_swap(*args, **kwargs):
                result = original_prepare(*args, **kwargs)
                refresh = [dict(name='refresh_token', value='r' * 32, domain='auth.qwen.ai', path='/',
                                secure=True, httpOnly=True, expires=-1)]  # renewable
                qwen_auth.Session(jwt('account-B', exp=time.time() - 5), refresh, 'UA', 2.0,
                                  expires_at=time.time() - 5).save(session_file)
                return result
            transport = httpx.MockTransport(lambda request: self.fail('renewal HTTP must not be sent'))

            def renew(**kwargs):
                return renewal.renew_session(session_file, transport=transport, **kwargs)
            with patch.object(api, 'guard', guard), patch.object(access, 'guard', guard), \
                    patch.object(api, 'conversation_cache', cache), patch.object(cache, 'prepare', prepare_then_swap), \
                    patch.object(api, 'QWEN_RENEW_SESSION', True), patch.object(api, '_qwen_client', None), \
                    patch.object(qwen_auth, 'DEFAULT_SESSION_FILE', session_file), \
                    patch.object(api, 'get_qwen_session', side_effect=api.QwenLoginRequired), \
                    patch.object(api, 'renew_qwen_session', side_effect=renew), \
                    patch.dict(api.MODEL_MAP, {'qwen3.8-max': 'qwen3.8-max'}, clear=True):
                response = await api._chat_completions(nxt)
            self.assertEqual(response.status_code, 401)
            guard.check('qwen')  # no dispatch happened, so no durable pause


class DeepSeekCaptureBinding(unittest.TestCase):
    """P2-3: a browser capture keeps or refuses the history/account binding."""

    def setUp(self):
        from deepseek import auth
        self.auth = auth
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'session.json'
        cookie = [dict(name='ds', value='1', domain='chat.deepseek.com', path='/', secure=True)]
        self.cookie = cookie
        pinned = auth.Session('a' * 20, cookie, 'UA', 1.0, account_id='42')
        pinned.preserve_history_from(auth.Session('z' * 20, cookie, 'UA', 0.5), same_account_confirmed=True)
        pinned.save(self.path)
        self.pin = pinned.continuation_scope

    def fresh(self, account):
        return self.auth.Session('b' * 20, self.cookie, 'UA', 2.0, account_id=account)

    def test_same_account_keeps_pin(self):
        self.auth.bind_capture(self.fresh('42'), self.path).save(self.path)
        self.assertEqual(self.auth.Session.load(self.path).continuation_scope, self.pin)

    def test_other_or_unknown_account_is_refused_and_file_kept(self):
        before = self.path.read_bytes()
        for account in ('43', None):
            with self.subTest(account=account), self.assertRaises(self.auth.AccountBindingError):
                self.auth.bind_capture(self.fresh(account), self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_explicit_replacement_starts_new_binding(self):
        bound = self.auth.bind_capture(self.fresh('43'), self.path, replace_account=True)
        self.assertIsNone(bound.continuation_scope)

    def test_unverified_previous_needs_explicit_choice_and_is_never_overwritten(self):
        plain = self.auth.Session('c' * 20, self.cookie, 'UA', 3.0)
        legacy_pin = self.auth.Session('d' * 20, self.cookie, 'UA', 4.0, continuation_scope='e' * 64)
        for previous in (plain, legacy_pin):  # R4: pinned or not, account unknown
            with self.subTest(pinned=previous.continuation_scope is not None):
                previous.save(self.path)
                before = self.path.read_bytes()
                with self.assertRaises(self.auth.AccountBindingError):
                    self.auth.bind_capture(self.fresh('42'), self.path)
                self.assertEqual(self.path.read_bytes(), before)
                kept = self.auth.bind_capture(self.fresh('42'), self.path, same_account=True)
                self.assertEqual(kept.continuation_scope, previous.continuation_scope or previous.identity())

    def test_login_path_uses_binding(self):
        page = Mock(url=self.auth.CHAT_URL)
        page.evaluate.side_effect = ['b' * 20, 'b' * 20, 'UA', '43']
        context = Mock(pages=[page])
        context.cookies.return_value = self.cookie
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value = context
        manager = Mock(__enter__=Mock(return_value=playwright), __exit__=Mock(return_value=False))
        before = self.path.read_bytes()
        with patch.object(self.auth, 'sync_playwright', return_value=manager), \
                patch.object(self.auth, '_safe_goto'), self.assertRaises(self.auth.AccountBindingError):
            self.auth.login(profile_dir=Path(self.temp.name) / 'profile', session_file=self.path)
        self.assertEqual(self.path.read_bytes(), before)


class LocalRefusalsAndJournal(unittest.IsolatedAsyncioTestCase):
    """P3-2 / P3-3."""

    async def test_local_limits_say_nothing_was_sent(self):
        guard = AccessGuard(path=None, interval=0)
        with patch.object(api, 'guard', guard), patch.dict(api.MODEL_MAP, {'deepseek-chat': 'default'}, clear=True), \
                patch.object(api, 'conversation_cache', ConversationCache(Path(tempfile.mkdtemp(dir=os.environ.get('TMPDIR'))) / 'c.sqlite3')):
            fake = Mock(session=Mock(continuation_scope=None, identity=Mock(return_value='x' * 64)))
            fake.chat.side_effect = AssertionError('must not send')
            with patch.object(api, 'get_client', return_value=fake):
                req = ChatCompletionRequest(model='deepseek-chat', messages=[{'role': 'user', 'content': 'x' * 140000}])
                response = await api._chat_completions(req)
        self.assertEqual(response.status_code, 413)
        message = json.loads(response.body)['error']['message']
        self.assertIn('No request was sent', message)
        self.assertNotIn('enabled a durable', message)
        error = ProviderRejected('local_budget_exceeded', source='local_limit')
        self.assertEqual(api._provider_rejected_response(error, 'deepseek-chat').status_code, 429)

    def test_stop_after_journal_retracts_the_unsent_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pauses.json'
            guard = AccessGuard(path=path, interval=0)
            stop = threading.Event()

            def cancelled():
                if stop.is_set():
                    raise asyncio.CancelledError()
            original = guard._save

            def save_then_stop(state):
                original(state)
                if state.get('deepseek', {}).get('dispatched'):
                    stop.set()
            with self.assertRaises(asyncio.CancelledError):
                with guard.attempt('deepseek', cancelled) as attempt:
                    guard._save = save_then_stop
                    attempt.dispatch(kind='chat')
            guard._save = original
            record = json.loads(path.read_text())['deepseek']
            self.assertEqual(record.get('usage'), [])
            self.assertNotIn('pause', record)
            self.assertNotIn('pending', record)
            AccessGuard(path=path, interval=0).check('deepseek')

    def test_stop_after_an_earlier_send_stays_conservative(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pauses.json'
            guard = AccessGuard(path=path, interval=0)
            stop = threading.Event()

            def cancelled():
                if stop.is_set():
                    raise asyncio.CancelledError()
            with self.assertRaises(asyncio.CancelledError):
                with guard.attempt('deepseek', cancelled) as attempt:
                    attempt.dispatch(kind='chat')  # sent
                    original = guard._save

                    def save_then_stop(state):
                        original(state)
                        stop.set()
                    guard._save = save_then_stop
                    attempt.dispatch(kind='generation')
            guard._save = original
            record = json.loads(path.read_text())['deepseek']
            self.assertEqual([kind for _, kind in record['usage']], ['chat'])
            self.assertEqual(record['pause'], 'upstream_failure')


class RolloverAndSchemas(unittest.TestCase):
    """P3-4 / P3-7."""

    def test_rollover_accepts_object_arguments(self):
        from server.context_rollover import checkpoint
        req = ChatCompletionRequest(model='deepseek-chat', messages=[
            {'role': 'user', 'content': 'goal'},
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'c1', 'type': 'function', 'function': {'name': 'bash', 'arguments': {'command': 'ls'}}}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'}])
        with tempfile.TemporaryDirectory() as directory:
            text, archive = checkpoint(req, 'chat:7', Path(directory) / 'ckpt')
        self.assertIn('{\\"command\\": \\"ls\\"}', text)

    def test_tool_without_parameters_is_callable_but_invalid_schemas_still_fail(self):
        tools = [{'type': 'function', 'function': {'name': 'now'}}]
        block = '```tool_calls\n[{"name":"now","arguments":{}}]\n```'
        self.assertEqual(len(validate_reply(block, tools)[1]), 1)
        for parameters in (False, None, {'type': 'nonsense'}):
            with self.subTest(parameters=parameters), self.assertRaises(ToolCallError):
                validate_reply(block, [{'type': 'function', 'function': {'name': 'now', 'parameters': parameters}}])


def qwen_sse(*frames):
    return ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames).encode() + b'data: [DONE]\n\n'


class BrowserQwenContract(unittest.TestCase):
    """N2 / N3: browser Qwen uses the HTTP candidate and terminal state machine."""

    def test_interleaved_candidates_never_mix(self):
        primary, secondary = str(uuid.uuid4()), str(uuid.uuid4())
        body = qwen_sse(
            {'response_id': primary, 'choices': [{'index': 0, 'delta': {'content': 'A'}}]},
            {'response_id': secondary, 'choices': [{'index': 0, 'delta': {'content': 'B'}}]},
            {'response_id': primary, 'choices': [{'index': 0, 'delta': {'content': '', 'status': 'finished'}}]})
        self.assertEqual(qwen_reply(body), ('A', 'stop'))

    def test_unnamed_frame_after_two_candidates_is_ambiguous(self):
        primary, secondary = str(uuid.uuid4()), str(uuid.uuid4())
        body = qwen_sse(
            {'response_id': primary, 'choices': [{'index': 0, 'delta': {'content': 'A'}}]},
            {'response_id': secondary, 'choices': [{'index': 0, 'delta': {'content': 'B'}}]},
            {'choices': [{'index': 0, 'delta': {'content': 'C'}, 'finish_reason': 'stop'}]})
        with self.assertRaises(ProviderUnavailable):
            qwen_reply(body)

    def test_output_limit_is_kept_and_blocks_tool_calls(self):
        body = qwen_sse({'choices': [{'index': 0, 'delta': {'content': CALL}, 'finish_reason': 'length'}]})
        text, finish = qwen_reply(body)
        self.assertEqual(finish, 'length')
        with self.assertRaises(ToolCallError):
            validate_reply(text, TOOLS, finish_reason=finish, provider='qwen')
        with self.assertRaises(ProviderUnavailable):
            qwen_answer(body)
        path = '/c/' + str(uuid.uuid4())
        result = {'status': 200, 'path': path, 'body': base64.b64encode(body).decode()}
        self.assertEqual(tab_bridge.parse_browser_result('qwen', result, path).finish_reason, 'length')


class ProcessGroupCleanup(unittest.TestCase):
    """macOS returns EPERM for a group whose only member is an unreaped zombie."""

    def test_zombie_only_group_is_reaped_without_error(self):
        process = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True,
                                   stdout=subprocess.PIPE)
        process.stdout.read()  # EOF: the child exited; it stays an unreaped zombie
        process.stdout.close()
        time.sleep(.2)
        antigravity.stop_process_group(process)
        self.assertIsNotNone(process.returncode)

    def test_other_denials_still_propagate(self):
        process = Mock(pid=999999, returncode=None)
        process.wait.side_effect = subprocess.TimeoutExpired('cli', 1)
        with patch.object(antigravity.os, 'killpg', side_effect=PermissionError), self.assertRaises(PermissionError):
            antigravity.stop_process_group(process)

    def test_no_group_signal_after_the_leader_is_reaped(self):
        """R5: a reaped leader frees the PGID; no later killpg may use it."""
        events = []
        process = Mock(pid=424242)
        process.wait.side_effect = lambda *a, **k: events.append('reap')
        process.poll.side_effect = lambda: events.append('reap')

        def killpg(pid, sig):
            events.append(sig)
            if sig == signal.SIGTERM:
                raise PermissionError  # exiting leader (macOS)
        with patch.object(antigravity.os, 'killpg', side_effect=killpg):
            antigravity.stop_process_group(process)
        self.assertEqual(events, [signal.SIGTERM, 'reap'])
        events.clear()

        def probe(pid, sig):
            events.append(sig)
            if sig == 0:
                raise ProcessLookupError
        with patch.object(antigravity.os, 'killpg', side_effect=probe):
            antigravity.stop_process_group(process)
        self.assertEqual(events, [signal.SIGTERM, 0, 'reap'])


class AntigravityLifecycleNeverSignalsReapedGroup(unittest.TestCase):
    """R5 (lifecycle): _chat must not reap the CLI before the last group signal."""

    def run_cli(self, body):
        from tests.support import isolated_access
        from tests.test_providers import cli_init
        isolated_access(self)
        events = []
        real_popen, real_killpg = subprocess.Popen, os.killpg

        class Recording(real_popen):
            def poll(self):
                events.append(('poll', self.returncode))
                return super().poll()

            def communicate(self, *args, **kwargs):
                events.append(('communicate',))
                return super().communicate(*args, **kwargs)
        created = []

        def popen(*args, **kwargs):
            created.append(Recording(*args, **kwargs))
            return created[-1]

        def killpg(pgid, sig):
            # Leader still unreaped: its PID (and this PGID) cannot be reused.
            events.append(('killpg', sig, created[0].returncode))
            return real_killpg(pgid, sig)
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'agy-lifecycle'
            script.write_text('#!' + sys.executable + '\nimport sys,json\n' + body.replace('INIT', repr(json.dumps(cli_init()))))
            script.chmod(0o700)
            with patch.dict(os.environ, {'ANTIGRAVITY_BIN': str(script)}), \
                    patch.object(antigravity.subprocess, 'Popen', side_effect=popen), \
                    patch.object(antigravity.os, 'killpg', side_effect=killpg):
                try:
                    antigravity.AntigravityClient().chat('marker', model='flash')
                except ProviderUnavailable:
                    pass
        self.assertIsNotNone(created[0].returncode)  # reaped in the end
        self.assertFalse([e for e in events if e[0] in ('poll', 'communicate')], events)
        signals = [e for e in events if e[0] == 'killpg']
        self.assertTrue(signals)
        self.assertTrue(all(e[2] is None for e in signals), events)

    def test_normal_completion(self):
        self.run_cli("print(INIT,flush=True)\np=json.loads(sys.stdin.read())['message']['content']\n"
                     "print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':p,"
                     "'conversation_id':'" + str(uuid.uuid4()) + "'}}))\n")

    def test_early_result_frame(self):
        self.run_cli("print(json.dumps({'event':'result','result':{'status':'ERROR'}}),flush=True)\n")

    def test_exit_before_init(self):
        self.run_cli("sys.exit(3)\n")


class ExportedScriptsUsePort(unittest.TestCase):
    """P3-5 / R6 / N5."""

    def test_budget_cli_default_reads_port_from_dotenv_in_a_fresh_process(self):
        """N5: a fresh process (no earlier settings import) must see .env PORT."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = Path(tab_bridge.ROOT)
            # Include the real transitive model catalog imported by harness.py.
            for name in ('settings.py', 'integrations', 'providers'):
                target = root / name
                if (source / name).is_dir():
                    import shutil
                    shutil.copytree(source / name, target)
                else:
                    target.write_bytes((source / name).read_bytes())
            (root / '.env').write_text('PORT=9876\n')
            code = ("import sys, integrations.harness_budget as b\n"
                    "b.install = lambda home, profile, token, base: print(base)\n"
                    "sys.argv = ['x', '--token-file', 't']\n"
                    "b.main()\n")
            env = {key: value for key, value in os.environ.items() if key != 'PORT'}
            env['PYTHONPATH'] = str(root)
            result = subprocess.run([sys.executable, '-c', code], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['http://127.0.0.1:9876', 'None'], result.stderr)

    def test_budget_cli_explicit_base_url_wins_over_broken_port(self):
        """N6: a broken PORT must not block a valid explicit --base-url or --help."""
        from integrations import harness_budget
        seen = []
        with patch.object(harness_budget, 'install', side_effect=lambda *args: seen.append(args[3])), \
                patch.dict(os.environ, {'PORT': 'bad'}), \
                patch('settings.load_environment'):
            harness_budget.main(['--token-file', 't', '--base-url', 'http://127.0.0.1:9000/'])
            self.assertEqual(seen, ['http://127.0.0.1:9000'])
            for argv in (['--token-file', 't'],                                   # broken PORT, no override
                         ['--token-file', 't', '--base-url', 'https://example.com'],
                         ['--token-file', 't', '--base-url', 'http://127.0.0.1:99999'],
                         ['--token-file', 't', '--base-url', 'http://127.0.0.1:9000/v1']):
                with self.subTest(argv=argv), self.assertRaises(SystemExit) as stopped, \
                        patch('sys.stderr', new_callable=io.StringIO):
                    harness_budget.main(argv)
                self.assertEqual(stopped.exception.code, 2)
            with self.assertRaises(SystemExit) as stopped, patch('sys.stdout', new_callable=io.StringIO):
                harness_budget.main(['--help'])
            self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(seen, ['http://127.0.0.1:9000'])

    def test_single_digit_port_is_kept_by_userscripts(self):
        node = '/Users/dmitrylabintcev/.local/node/bin/node'
        script = ("const PORT_TEMPLATE=process.argv[1];const validPort=v=>/^[1-9][0-9]{0,4}$/.test(v)&&Number(v)<=65535;"
                  "console.log(['9','8080','65535','0','65536','08'].map(validPort).join(','))")
        if Path(node).exists():
            self.assertEqual(subprocess.run([node, '-e', script, 'x'], capture_output=True, text=True).stdout.strip(),
                             'true,true,true,false,false,false')
        source = Path(tab_bridge.ROOT, 'browser', 'opencode-controller.user.js').read_text()
        self.assertIn('/^[1-9][0-9]{0,4}$/', source)
        self.assertEqual(source.count('__BRIDGE_PORT__'), 1)  # only the template constant is replaced

    def test_export_substitutes_port_in_all_bridge_scripts(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(tab_bridge, 'token_path', return_value=Path(directory) / 'token'), \
                patch.dict(os.environ, {'PORT': '8123'}):
            destination = tab_bridge.export_scripts()
            controller = (destination / 'opencode-controller.user.js').read_text()
            transfer = (destination / 'deepseek-session-transfer.user.js').read_text()
        for text in (controller, transfer):
            self.assertIn('"8123"' if text is controller else "'8123'", text)
            self.assertNotIn('__BRIDGE_PORT__', text)


if __name__ == '__main__':
    unittest.main()
