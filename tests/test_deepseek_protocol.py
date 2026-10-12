import threading
import unittest
from unittest.mock import Mock, patch

import httpx

from deepseek.client import DeepSeekClient, DeepSeekStreamError, _failure_kind
from providers.access import AccessGuard, ProviderRejected
from tests.test_stream import event, snapshot, mock_client


class StreamDiagnosticsTests(unittest.TestCase):
    def test_context_limit_hint_is_a_durable_refusal_not_missing_terminal(self):
        payload = event({'request_message_id':301,'response_message_id':302,'model_type':'default'}, 'ready')
        payload += event({'type':'error','content':'PRIVATE_PROVIDER_DETAIL',
                         'clear_response':True,'finish_reason':'context_length_exceeded'}, 'hint')
        payload += event({'click_behavior':'none','auto_resume':False}, 'close')
        client = mock_client(payload)
        self.addCleanup(client.close)
        client.access_guard = AccessGuard(path=None, interval=0)
        with patch.object(client._http,'send',wraps=client._http.send) as send:
            with self.assertLogs('deepseek.client',level='WARNING') as logs, self.assertRaises(ProviderRejected) as error:
                client.chat('fixture',conversation_id='existing:298')
            self.assertEqual(error.exception.code,'conversation_context_exceeded')
            self.assertNotIn('PRIVATE_PROVIDER_DETAIL','\n'.join(logs.output))
            self.assertEqual(client.access_guard.status(),{'deepseek':'conversation_context_exceeded'})
            with self.assertRaises(ProviderRejected):
                client.chat('must not replay',conversation_id='existing:298')
            self.assertEqual(send.call_count,1)

    def test_error_hint_cannot_be_hidden_by_terminal_status_or_unknown_reason(self):
        from deepseek.client import _parse_sse
        for prefix in ('', snapshot(status='FINISHED')):
            for reason in ('context_length_exceeded','unknown'):
                payload=prefix+event({'type':'error','finish_reason':reason},'hint')+event({},'finish')
                with self.subTest(prefix=bool(prefix),reason=reason), self.assertRaises(ProviderRejected):
                    list(_parse_sse(payload.splitlines()))

    def test_late_status_shape_is_useful_without_logging_upstream_data(self):
        completed = snapshot('PRIVATE_REPLY') + event({'p': 'response/status', 'v': 'FINISHED'})
        cases = [
            (event({'p': 'response/status'}, 'finish'),
             'event=finish source=patch value=missing explicit_path=True'),
            (event({'v': None}), 'event=default source=patch value=null'),
            (event({'p': 'response/status', 'v': 'PRIVATE_STATUS'}, 'PRIVATE_EVENT'),
             'event=other source=patch value=string explicit_path=True'),
            (snapshot('PRIVATE_REPLY'), 'event=default source=snapshot value=missing'),
        ]
        for tail, expected in cases:
            with self.subTest(expected=expected):
                client = mock_client(completed + tail)
                self.addCleanup(client.close)
                client.access_guard = AccessGuard(path=None, interval=0)
                with self.assertLogs('deepseek.client', level='WARNING') as log:
                    with self.assertRaises(DeepSeekStreamError):
                        client.chat('PRIVATE_PROMPT')
                output = '\n'.join(log.output)
                self.assertIn(expected, output)
                self.assertIn('terminal_before_event=True', output)
                self.assertIn('reason=stream_status_after_completion', output)
                self.assertNotIn('PRIVATE_', output)
                self.assertEqual(client.access_guard.status(), {'deepseek': 'upstream_failure'})

    def test_protocol_failures_have_safe_codes_and_do_not_replay(self):
        cases = [
            (snapshot('PRIVATE_REPLY') + 'data: {PRIVATE_TOKEN\n\n', 'stream_invalid_json', 'existing:4'),
            (snapshot('PRIVATE_REPLY'), 'stream_missing_terminal', 'existing:4'),
            (snapshot('PRIVATE_REPLY') + event({'p': 'response/fragments/0/content', 'o': 'SET', 'v': 'PRIVATE_TOKEN'}),
             'stream_unsupported_content_patch', 'existing:4'),
            (snapshot('PRIVATE_REPLY', 'FINISHED'), 'stream_missing_or_stale_message_id', 'existing:2'),
        ]
        for payload, code, cid in cases:
            with self.subTest(code=code):
                client = mock_client(payload)
                self.addCleanup(client.close)
                client.access_guard = AccessGuard(path=None, interval=0)
                with patch.object(client._http, 'send', wraps=client._http.send) as send:
                    with self.assertLogs('deepseek.client', level='WARNING') as log, self.assertRaises(DeepSeekStreamError):
                        client.chat('PRIVATE_PROMPT', conversation_id=cid)
                    self.assertIn('phase=parse_stream http_status=200 reason=' + code, log.output[0])
                    self.assertNotIn('PRIVATE_', '\n'.join(log.output))
                    self.assertEqual(client.access_guard.status(), {'deepseek': 'upstream_failure'})
                    with self.assertRaises(ProviderRejected):
                        client.chat('must not retry', conversation_id=cid)
                    self.assertEqual(send.call_count, 1)

    def test_unknown_exception_text_never_becomes_a_diagnostic_field(self):
        self.assertEqual(_failure_kind(DeepSeekStreamError('PRIVATE_TOKEN')), 'stream_error')
        self.assertEqual(_failure_kind(RuntimeError('PRIVATE_TOKEN')), 'unexpected_failure')

    def test_empty_finish_allows_two_mocked_turns_without_pausing(self):
        import json
        client = mock_client('')
        client._http.close()
        client.access_guard = AccessGuard(path=None, interval=0)
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            message_id = 2 * len(requests)
            payload = event({'v': {'response': {'message_id': message_id, 'fragments': [
                {'type': 'RESPONSE', 'content': 'fixture answer'}]}}})
            payload += event({'p': 'response/status', 'o': 'SET', 'v': 'FINISHED'})
            payload += event({}, 'finish')
            return httpx.Response(200, text=payload, headers={'content-type': 'text/event-stream'})

        client._http = httpx.Client(base_url='https://offline.invalid', transport=httpx.MockTransport(respond))
        self.addCleanup(client.close)
        first = client.chat('first')
        second = client.chat('second', conversation_id=first.conversation_id)
        self.assertEqual((first.text, second.text), ('fixture answer', 'fixture answer'))
        self.assertEqual((first.conversation_id, second.conversation_id), ('fake-session:2', 'fake-session:4'))
        self.assertEqual([r['parent_message_id'] for r in requests], [None, 2])
        self.assertEqual(client.access_guard.status(), {})


class ChatCreationTests(unittest.TestCase):
    def client(self, data):
        calls = []

        def respond(request):
            calls.append(request.url.path)
            return httpx.Response(200, json=data)

        client = DeepSeekClient.__new__(DeepSeekClient)
        client._request_lock = threading.Lock()
        client.access_guard = AccessGuard(path=None, interval=0)
        client._pow_header = Mock(side_effect=AssertionError('must not reach challenge'))
        client._http = httpx.Client(base_url='https://chat.deepseek.com',
                                   transport=httpx.MockTransport(respond))
        self.addCleanup(client.close)
        return client, calls

    @staticmethod
    def envelope(biz):
        return {'code': 0, 'data': {'biz_code': 0, 'biz_data': biz}}

    def test_accepts_flat_and_nested_chat_identity(self):
        for biz in ({'id': 'fixture-session'},
                    {'chat_session': {'id': 'fixture-session'}},
                    {'id': 'fixture-session', 'chat_session': {'id': 'fixture-session'}}):
            with self.subTest(biz=biz):
                client, calls = self.client(self.envelope(biz))
                self.assertEqual(client.create_chat_session(), 'fixture-session')
                self.assertEqual(calls, ['/api/v0/chat_session/create'])

    def test_invalid_identity_stops_before_challenge_and_blocks_replay(self):
        for biz in ({}, {'id': ''}, {'id': '  '}, {'id': 4}, {'id': True},
                    {'id': 'fixture:2'}, {'id': ' padded '},
                    {'chat_session': None}, {'chat_session': []},
                    {'chat_session': {}}, {'chat_session': {'id': None}},
                    {'id': 'other', 'chat_session': {'id': 'fixture'}},
                    {'id': 'fixture', 'chat_session': None}):
            with self.subTest(biz=biz):
                client, calls = self.client(self.envelope(biz))
                with self.assertRaises(ValueError), self.assertLogs('deepseek.client', level='WARNING'):
                    client.chat('fixture')
                self.assertEqual(client.access_guard.status(), {'deepseek': 'upstream_failure'})
                with self.assertRaises(ProviderRejected):
                    client.chat('must not send')
                self.assertEqual(calls, ['/api/v0/chat_session/create'])
                client._pow_header.assert_not_called()

    def test_business_error_wins_over_any_chat_identity(self):
        for code in (5, 19):
            envelope = self.envelope({'id': 'fixture'})
            envelope['data']['biz_code'] = code
            client, calls = self.client(envelope)
            with self.assertRaises(ProviderRejected) as failure:
                client.create_chat_session()
            self.assertEqual(failure.exception.code,
                             'account_restricted' if code == 5 else 'upstream_failure')
            self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
