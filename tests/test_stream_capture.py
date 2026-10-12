import base64
import contextlib
import hashlib
import json
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import httpx

from deepseek import stream_diagnostics as diagnostics
from deepseek.client import DeepSeekStreamError
from providers.access import AccessGuard, ProviderRejected
from tests.test_stream import event, snapshot, mock_client


class CaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'captures'
        for patcher in (patch.object(diagnostics, 'DIRECTORY', self.root),
                        patch.dict(os.environ, DEEPSEEK_STREAM_DIAGNOSTICS='1')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def capture(self, raw, **changes):
        obj = diagnostics.StreamCapture()
        class Response:
            def iter_bytes(self):
                yield raw
        list(obj.wrap(Response()).iter_bytes())
        obj.save(reason='stream_missing_terminal', session_id='fixture', parent_id=1)
        path = next(self.root.glob('*.json'))
        if changes:
            data = json.loads(path.read_text())
            data.update(changes)
            path.write_text(json.dumps(data))
        return path

    def test_real_client_failure_saves_bytes_without_prompt_and_blocks_replay(self):
        raw = snapshot('PRIVATE_RESPONSE')
        client = mock_client(raw)
        self.addCleanup(client.close)
        client.access_guard = AccessGuard(path=None, interval=0)
        with patch.object(client._http, 'send', wraps=client._http.send) as send:
            with self.assertLogs(level='WARNING') as logs, self.assertRaises(DeepSeekStreamError):
                client.chat('PRIVATE_PROMPT', conversation_id='fixture:1')
            with self.assertRaises(ProviderRejected):
                client.chat('DO_NOT_SEND', conversation_id='fixture:1')
            self.assertEqual(send.call_count, 1)
        path = next(self.root.glob('*.json'))
        data = json.loads(path.read_text())
        self.assertEqual(base64.b64decode(data['body_base64']).decode(), raw)
        self.assertTrue(data['eof'])
        self.assertFalse(data['truncated'])
        self.assertNotIn('PRIVATE_PROMPT', path.read_text())
        self.assertNotIn('PRIVATE_', '\n'.join(logs.output))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        before = path.read_bytes()
        with self.assertRaises(DeepSeekStreamError):
            diagnostics.reconstruct(path)
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(client.access_guard.status(), {'deepseek': 'upstream_failure'})

    def test_success_and_disabled_capture_never_write(self):
        for enabled, payload in [('1', snapshot(status='FINISHED')), ('0', snapshot())]:
            with patch.dict(os.environ, DEEPSEEK_STREAM_DIAGNOSTICS=enabled):
                client = mock_client(payload)
                try:
                    if enabled == '1':
                        client.chat('fixture')
                    else:
                        with self.assertRaises(DeepSeekStreamError), self.assertLogs(level='WARNING'):
                            client.chat('fixture')
                finally:
                    client.close()
                self.assertFalse(self.root.exists())

    def test_transport_failure_keeps_prefix_and_refuses_recovery(self):
        obj = diagnostics.StreamCapture()
        class Broken:
            def iter_bytes(self):
                yield snapshot(status='FINISHED').encode()
                raise httpx.ReadError('PRIVATE_ERROR')
        with self.assertRaises(httpx.ReadError):
            list(obj.wrap(Broken()).iter_bytes())
        obj.save(reason='transport_error', session_id='fixture', parent_id=1)
        path = next(self.root.glob('*.json'))
        self.assertFalse(json.loads(path.read_text())['eof'])
        with self.assertRaises(ValueError):
            diagnostics.reconstruct(path)

    def test_capture_and_disk_caps_do_not_delete_evidence(self):
        with patch.object(diagnostics, 'MAX_BYTES', 3):
            path = self.capture(b'abcdef')
        data = json.loads(path.read_text())
        self.assertTrue(data['truncated'])
        self.assertEqual(base64.b64decode(data['body_base64']), b'abc')
        with self.assertRaises(ValueError):
            diagnostics.reconstruct(path)
        before = path.read_bytes()
        with patch.object(diagnostics, 'MAX_FILES', 1), self.assertLogs(level='WARNING'):
            self.capture(b'other')
        self.assertEqual(before, path.read_bytes())

    def test_write_failure_does_not_replace_original_failure(self):
        client = mock_client(snapshot())
        self.addCleanup(client.close)
        with patch.object(diagnostics, 'write_private_json', side_effect=OSError('PRIVATE_PATH')):
            with self.assertLogs(level='WARNING') as logs, self.assertRaises(DeepSeekStreamError):
                client.chat('fixture')
        self.assertNotIn('PRIVATE_PATH', '\n'.join(logs.output))

    def test_complete_evidence_reconstructs_without_network_or_state_writes(self):
        path = self.capture(snapshot('fixture reply', 'FINISHED').encode())
        before = path.read_bytes()
        with patch('httpx.Client.send', side_effect=AssertionError('network forbidden')), \
                patch.object(diagnostics, 'write_private_json', side_effect=AssertionError('write forbidden')):
            result = diagnostics.reconstruct(path)
        self.assertEqual(result['text'], 'fixture reply')
        self.assertEqual(result['conversation_id'], 'fixture:2')
        self.assertEqual(result['status'], 'review_only')
        self.assertFalse(result['goal_resumed'])
        self.assertEqual(before, path.read_bytes())

    def test_corrupt_incomplete_stale_or_failed_evidence_cannot_recover(self):
        raw = snapshot(status='FINISHED').encode()
        path = self.capture(raw)
        original = json.loads(path.read_text())
        for changes in ({'eof': False}, {'truncated': True}, {'sha256': 'wrong'},
                        {'parent_id': 2}, {'session_id': None}, {'version': 4}):
            with self.subTest(changes=changes):
                path.write_text(json.dumps({**original, **changes}))
                with self.assertRaises(ValueError):
                    diagnostics.reconstruct(path)
        for payload in (snapshot(), snapshot(status='FAILED'),
                        snapshot(status='OUTPUT_LIMIT'),
                        snapshot() + 'data: [DONE]\n\n' + event({'error': 'denied'}),
                        'data: {broken\n\n'):
            path.write_text(json.dumps({**original,
                'body_base64': base64.b64encode(payload.encode()).decode(),
                'sha256': hashlib.sha256(payload.encode()).hexdigest()}))
            with self.subTest(payload=payload), self.assertRaises((ValueError, DeepSeekStreamError, ProviderRejected)):
                diagnostics.reconstruct(path)

    def test_oversize_file_refused_before_json_parse(self):
        path = self.capture(b'fixture')
        with patch.object(diagnostics, 'MAX_FILE_BYTES', 10), self.assertRaises(ValueError):
            diagnostics.reconstruct(path)

    def test_cli_exports_private_review_only_and_never_overwrites(self):
        path = self.capture(snapshot('PRIVATE_REPLY', 'FINISHED').encode())
        with patch('sys.argv', ['stream_diagnostics', str(path)]), \
                patch('httpx.Client.send', side_effect=AssertionError('network forbidden')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(diagnostics.main(), 0)
            target = path.with_name(path.name + '.review.json')
            before = target.read_bytes()
            self.assertEqual(diagnostics.main(), 1)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertNotIn('PRIVATE_REPLY', output.getvalue())
        self.assertEqual(json.loads(before)['status'], 'review_only')
