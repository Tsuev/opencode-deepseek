import base64
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import subprocess
import sys
from unittest.mock import patch

import httpx

from providers.access import AccessGuard, ProviderRejected
from providers.conversations import encode, decode
from providers.kimi_auth import save_capture, validate_session
from providers.kimi_http import KimiClient, MODEL, CHAT_PATH
from tests.test_kimi_auth import session
from tests.test_kimi_protocol import fixture, wire, envelope
from providers.kimi_models import MODELS, PUBLIC_MODELS


def model_fixture(model=MODEL):
    frames = fixture()
    for frame in frames:
        if frame.get('mask') == 'message':
            frame['message']['scenario'] = MODELS[model]['scenario']
    frames.insert(2, {'op': 'set', 'mask': 'chat.lastRequest', 'chat': {
        'id': 'owned-chat', 'lastRequest': {'scenario': MODELS[model]['scenario'],
        'options': {'model': model, 'enablePlugin': False}}}})
    return frames


class KimiHttpTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.path = Path(root.name) / 'session.json'
        save_capture(self.path, validate_session(session()))
        self.guard = AccessGuard(None, interval=0)
        self.requests = []

    def client(self, handler, renew=True):
        def factory(**kwargs):
            self.assertIs(kwargs['trust_env'], False)
            self.assertIs(kwargs['follow_redirects'], False)
            def checked(request):
                self.requests.append(request)
                return handler(request)
            return httpx.Client(transport=httpx.MockTransport(checked), **kwargs)
        return KimiClient(session_file=self.path, client_factory=factory, renew=renew)

    def run_chat(self, client, **kwargs):
        with self.guard.attempt('kimi') as attempt, attempt.bind():
            return client.chat('owned prompt', model=kwargs.pop('model', MODEL), **kwargs)

    def test_direct_request_is_bound_and_completed_before_exposing_text(self):
        def handler(request):
            self.assertEqual(str(request.url), 'https://www.kimi.ai' + CHAT_PATH)
            body = json.loads(request.content[5:])
            self.assertEqual(body['tools'], [])
            self.assertEqual(body['options']['model'], MODEL)
            self.assertEqual(body['options']['reasoning_effort'], 'REASONING_EFFORT_NONE')
            self.assertEqual(body['chat_id'], '')
            return httpx.Response(200, content=wire(model_fixture()), headers={'Content-Type': 'application/connect+json'})
        result = self.run_chat(self.client(handler))
        self.assertEqual(result.text, 'answer')
        self.assertEqual(decode(result.conversation_id, 'kimi'), (MODEL, '/chat/owned-chat/parent/assistant'))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.guard.status(), {})

    def test_each_model_preserves_selection_and_disables_optional_cloud_tools(self):
        for model, spec in MODELS.items():
            with self.subTest(model=model):
                self.guard = AccessGuard(None, interval=0)
                def handler(request):
                    body = json.loads(request.content[5:])
                    self.assertEqual(body['options']['model'], model)
                    self.assertEqual(body['scenario'], spec['scenario'])
                    self.assertEqual(body['message']['scenario'], spec['scenario'])
                    self.assertEqual(body['options']['reasoning_effort'], spec['effort'])
                    self.assertIs(body['options']['enable_plugin'], False)
                    self.assertEqual(body['tools'], [])
                    self.assertNotIn('SWARM', request.content.decode(errors='replace'))
                    if model != MODEL:
                        self.assertEqual(body['kimiplus_id'], 'ok-computer')
                        self.assertEqual(body['options']['context_length'], 'CONTEXT_LENGTH_L')
                    return httpx.Response(200, content=wire(model_fixture(model)),
                                          headers={'Content-Type': 'application/connect+json'})
                reply = self.run_chat(self.client(handler), model=model)
                self.assertEqual(decode(reply.conversation_id, 'kimi')[0], model)
                self.assertEqual(reply.text, 'answer')

    def test_missing_or_changed_model_echo_cannot_silently_fallback(self):
        base = model_fixture('k3-agent')
        missing = copy.deepcopy(base); del missing[2]
        changed = copy.deepcopy(base); changed[2]['chat']['lastRequest']['options']['model'] = MODEL
        plugins = copy.deepcopy(base); plugins[2]['chat']['lastRequest']['options']['enablePlugin'] = True
        scenario = copy.deepcopy(base); scenario[3]['message']['scenario'] = 'SCENARIO_CHAT'
        duplicate = copy.deepcopy(base); duplicate.insert(3, copy.deepcopy(duplicate[2]))
        for frames in (missing, changed, plugins, scenario, duplicate):
            with self.subTest(frames=frames):
                self.guard = AccessGuard(None, interval=0)
                before = len(self.requests)
                with self.assertRaises(RuntimeError):
                    self.run_chat(self.client(lambda request: httpx.Response(200,
                        content=wire(frames), headers={'Content-Type': 'application/connect+json'})), model='k3-agent')
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(self.guard.status()['kimi'], 'upstream_failure')

    def test_capacity_refusal_is_durable_without_replay_or_account_details(self):
        error = {'code': 'resource_exhausted', 'message': 'private account detail', 'details': [{
            'type': 'common.error.v1.ErrorDetail', 'value': base64.b64encode(bytes([8, 117])).decode()}]}
        self.guard = AccessGuard(self.path.parent / 'pauses.json', interval=0)
        client = self.client(lambda request: httpx.Response(200, content=envelope({'error': error}, 2),
            headers={'Content-Type': 'application/connect+json'}))
        with self.assertRaises(ProviderRejected) as failure:
            self.run_chat(client, model='k3-agent')
        self.assertEqual(failure.exception.code, 'capacity_unavailable')
        self.assertNotIn('private account', str(failure.exception))
        self.guard = AccessGuard(self.path.parent / 'pauses.json', interval=0)
        with self.assertRaises(ProviderRejected):
            self.run_chat(client, model='k28-agent-preview')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.guard.status()['kimi'], 'capacity_unavailable')

    def test_resumed_parent_must_match_and_browser_token_cannot_cross_transport(self):
        token = encode('kimi', MODEL, '/chat/owned-chat/parent/previous')
        frames = model_fixture()
        frames[3]['message']['parentId'] = 'previous'
        def handler(request):
            body = json.loads(request.content[5:])
            self.assertEqual(body['chat_id'], 'owned-chat')
            self.assertEqual(body['message']['parent_id'], 'previous')
            return httpx.Response(200, content=wire(frames), headers={'Content-Type': 'application/connect+json'})
        client = self.client(handler)
        self.assertEqual(self.run_chat(client, conversation_id=token).text, 'answer')
        with self.assertRaises(ValueError):
            self.run_chat(client, conversation_id=encode('kimi', 'default', '/chat/owned-chat'))
        self.assertEqual(len(self.requests), 1)
        frames[3]['message']['parentId'] = 'other'
        with self.assertRaises(RuntimeError):
            self.run_chat(client, conversation_id=token)
        self.assertEqual(self.guard.status()['kimi'], 'upstream_failure')

    def test_refresh_happens_once_before_chat_and_never_after_chat_refusal(self):
        save_capture(self.path, validate_session(session(time.time() - 1), require_fresh=False))
        new = session(refresh='new-refresh')
        def handler(request):
            if request.url.host == 'auth.kimi.ai':
                return httpx.Response(200, json={'accessToken': new['token'], 'refreshToken': new['refresh_token']})
            self.assertEqual(request.headers['Authorization'], 'Bearer ' + new['token'])
            return httpx.Response(429)
        client = self.client(handler)
        with self.assertRaises(ProviderRejected):
            self.run_chat(client)
        self.assertEqual([r.url.host for r in self.requests], ['auth.kimi.ai', 'www.kimi.ai'])
        self.assertEqual(self.guard.status()['kimi'], 'quota_exceeded')
        with self.assertRaises(ProviderRejected):
            self.run_chat(client)
        self.assertEqual(len(self.requests), 2)

    def test_refresh_failure_cannot_send_prompt(self):
        save_capture(self.path, validate_session(session(time.time() - 1), require_fresh=False))
        with self.assertRaises(ProviderRejected):
            self.run_chat(self.client(lambda request: httpx.Response(401)))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.host, 'auth.kimi.ai')
        self.assertNotIn(b'owned prompt', self.requests[0].content)

    def test_partial_disconnect_and_redirect_never_return_answer_or_retry(self):
        for response in (httpx.Response(302, headers={'Location': 'https://evil.invalid'}),
                         httpx.Response(200, content=wire(model_fixture())[:-1], headers={'Content-Type': 'application/connect+json'}),
                         httpx.Response(200, content=b'answer', headers={'Content-Type': 'text/html'})):
            self.guard = AccessGuard(None, interval=0)
            count = len(self.requests)
            with self.assertRaises(RuntimeError):
                self.run_chat(self.client(lambda request: response))
            self.assertEqual(len(self.requests), count + 1)
            self.assertEqual(self.guard.status()['kimi'], 'upstream_failure')

    def test_direct_catalog_and_factory_do_not_require_browser_bridge(self):
        env = {**os.environ, 'KIMI_ENABLED': '1', 'KIMI_TRANSPORT': 'direct', 'BROWSER_BRIDGE_ENABLED': '0'}
        result = subprocess.run([sys.executable, '-c',
            'import json; from server.config import MODEL_MAP, model_provider; '
            'print(json.dumps({k:[v,model_provider(k)] for k,v in MODEL_MAP.items() if k.startswith("kimi-")}))'],
            env=env, capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(json.loads(result.stdout), {public: [model, 'kimi'] for public, model in PUBLIC_MODELS.items()})
        from providers.registry import build_client
        with patch.dict(os.environ, env):
            self.assertIsInstance(build_client('kimi', lambda: None), KimiClient)


if __name__ == '__main__':
    unittest.main()
