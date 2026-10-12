"""Synthetic offline coverage of remaining audit findings."""
import json
import unittest
from unittest.mock import Mock
import httpx
from providers.stream_limits import bounded_lines
from qwen.client import QwenClient, QwenStreamError, _parse_sse
from deepseek.client import _parse_sse as ds_parse
from tests.test_qwen import answer, events, session, CHAT, MESSAGE
from integrations import harness


class RemainingAuditTests(unittest.TestCase):
    def test_cancel_before_and_after_read_and_eof(self):
        for cancel_at in (1, 3, 5):
            with self.subTest(cancel_at=cancel_at):
                calls = []
                def check():
                    calls.append(1)
                    if len(calls) >= cancel_at:
                        raise RuntimeError('cancelled')
                response = Mock()
                response.iter_bytes.return_value = iter([b':heartbeat\n'])
                with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                    list(bounded_lines(response, Mock(check=check)))

    def test_deadline_and_byte_limits(self):
        response = Mock()
        response.iter_bytes.return_value = iter([b':heartbeat\n'] * 5)
        ticks = iter(range(100))
        with self.assertRaises(TimeoutError):
            list(bounded_lines(response, clock=lambda: next(ticks), max_seconds=4))
        for options, content in (({'max_bytes': 3}, b'1234'), ({'max_line_bytes': 3}, b'1234\n'), ({'max_line_bytes': 3}, b'1234')):
            response.iter_bytes.return_value = iter([content])
            with self.subTest(options=options), self.assertRaises(ValueError):
                list(bounded_lines(response, **options))

    def test_split_unicode_and_crlf(self):
        response = Mock()
        data = 'data: привет\r\n\r\n'.encode()
        response.iter_bytes.return_value = iter(bytes([b]) for b in data)
        self.assertEqual(list(bounded_lines(response)), ['data: привет', ''])

    def test_qwen_rejects_late_primary_content(self):
        with self.assertRaises(QwenStreamError):
            list(_parse_sse(events(answer('done', status='finished'), answer('late'), done=False), {}))

    def test_qwen_allows_terminal_metadata(self):
        self.assertEqual(''.join(_parse_sse(events(answer('done', status='finished'), answer('', status='finished')), {})), 'done')

    def test_qwen_stale_parent_closes_response(self):
        captured = []
        def respond(request):
            response = httpx.Response(200, text='\n'.join(events(answer('done', status='finished'))), headers={'content-type': 'text/event-stream'})
            captured.append(response)
            return response
        with QwenClient(session(), access_guard=None, transport=httpx.MockTransport(respond))._http as transport:
            client = QwenClient.__new__(QwenClient)
            import threading
            client._http = transport
            client._request_lock = threading.Lock()
            client.access_guard = None
            with self.assertRaises(QwenStreamError):
                client.chat('synthetic', f'qwen:qwen3.8-max:{CHAT}:{MESSAGE}')
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0].is_closed)

    def test_harness_missing_owned_default_rejected(self):
        models = [{'id': 'qwen3.8-omni-flash'}]
        row = {'id': 'agent-default-model', 'config': {'provider': harness.ROUTE, 'model': 'deepseek-expert'}}
        with self.assertRaises(ValueError):
            harness.patch_document([row], 'http://127.0.0.1:8000/v1', models)
        self.assertEqual(row['config']['model'], 'deepseek-expert')
        row['config']['provider'] = 'external'
        self.assertEqual(harness.patch_document([row], 'http://127.0.0.1:8000/v1', models)[0], row)

    def test_explicit_default_enables_row(self):
        row = {'id': 'agent-default-model', 'disabled': True}
        result = harness.patch_document([row], 'http://127.0.0.1:8000/v1', [{'id': 'qwen3.8-max'}], default_model='qwen3.8-max')
        self.assertIs(result[0]['disabled'], False)

    def test_deepseek_idempotent_final_snapshot(self):
        frame = {'v': {'response': {'message_id': 2, 'status': 'FINISHED', 'fragments': [{'type': 'RESPONSE', 'content': 'done'}]}}}
        self.assertEqual(''.join(ds_parse(events(frame, frame))), 'done')

    def test_both_clients_cancel_stream_close_and_pause_without_replay(self):
        import threading
        from deepseek.client import DeepSeekClient
        from providers.access import AccessGuard, ProviderRejected
        for provider, mime in ((p, m) for p in ('deepseek', 'qwen') for m in ('text/event-stream', 'application/json')):
            with self.subTest(provider=provider, mime=mime):
                state = {'cancel': False, 'closed': False, 'calls': 0}
                class Body(httpx.SyncByteStream):
                    def __iter__(self):
                        yield b':heartbeat\n\n'
                        state['cancel'] = True
                        yield b':heartbeat\n\n'
                        raise AssertionError('must not drain cancelled stream')
                    def close(self):
                        state['closed'] = True
                def handler(request):
                    state['calls'] += 1
                    return httpx.Response(200, stream=Body(), headers={'content-type': mime})
                def cancelled():
                    if state['cancel']:
                        raise RuntimeError('fixture cancellation')
                guard = AccessGuard(path=None, interval=0)
                client = (DeepSeekClient if provider == 'deepseek' else QwenClient).__new__(DeepSeekClient if provider == 'deepseek' else QwenClient)
                client.access_guard = guard
                client._request_lock = threading.Lock()
                client._http = httpx.Client(base_url='https://fixture.invalid', transport=httpx.MockTransport(handler))
                client._pow_header = Mock(return_value='synthetic')
                cid = 'fixture:1' if provider == 'deepseek' else f'qwen:qwen3.8-max:{CHAT}:{MESSAGE}'
                stream = client.stream('synthetic', cid)
                with guard.attempt(provider, cancelled):
                    with self.assertRaisesRegex(RuntimeError, 'fixture cancellation'):
                        list(stream)
                client.close()
                self.assertTrue(state['closed'])
                self.assertEqual(state['calls'], 1)
                with self.assertRaises(ProviderRejected):
                    guard.check(provider)

    def test_qwen_finished_length_in_same_frame(self):
        frame = answer('partial', status='finished')
        frame['choices'][0]['finish_reason'] = 'length'
        meta = {}
        self.assertEqual(''.join(_parse_sse(events(frame), meta)), 'partial')
        self.assertEqual(meta['finish_reason'], 'length')

    def test_cr_only_sse_lines(self):
        response = Mock()
        response.iter_bytes.return_value = iter([b"data: a\r", b"\rdata: b\r\r"])
        self.assertEqual(list(bounded_lines(response)), ['data: a', '', 'data: b', ''])

    def test_deepseek_terminal_status_cannot_change(self):
        from deepseek.client import DeepSeekStreamError
        first = {'v': {'response': {'message_id': 2, 'status': 'FINISHED', 'fragments': [{'type': 'RESPONSE', 'content': 'done'}]}}}
        for status in ('INCOMPLETE', 'RUNNING', None):
            with self.subTest(status=status), self.assertRaises(DeepSeekStreamError):
                list(ds_parse(events(first, {'p': 'response/status', 'v': status})))

    def test_deepseek_final_snapshot_after_incremental_terminal(self):
        first = {'v': {'response': {'message_id': 2, 'status': 'RUNNING', 'fragments': [{'type': 'RESPONSE', 'content': 'a'}]}}}
        final = {'v': {'response': {'message_id': 2, 'status': 'FINISHED', 'fragments': [{'type': 'RESPONSE', 'content': 'ab'}]}}}
        frames = events(first, {'p': 'response/fragments/0/content', 'o': 'APPEND', 'v': 'b'}, {'p': 'response/status', 'v': 'FINISHED'}, final)
        self.assertEqual(''.join(ds_parse(frames)), 'ab')
