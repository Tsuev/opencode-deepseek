import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from providers.access import AccessGuard, ProviderRejected
from providers.common import ProviderUnavailable
from providers.tab_bridge import _annotate_bridge_failure
from providers.browser_protocol import claude_answer


class BrowserFailureSafetyTests(unittest.TestCase):
    def test_uncertain_dispatch_stays_pending_across_restart(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'guard.json'
            guard = AccessGuard(path, interval=0)
            with self.assertRaises(ProviderUnavailable):
                with guard.attempt('mistral', lambda: None) as attempt:
                    attempt.dispatch()
                    error = _annotate_bridge_failure(ProviderUnavailable('Completion stream observation stalled'))
                    # Arbitrary exception attributes must not authorize clearing.
                    error.clear_without_pause = True
                    raise error
            saved = path.read_bytes()
            self.assertTrue(json.loads(saved)['mistral']['pending'])
            with self.assertRaises(ProviderRejected):
                AccessGuard(path, interval=0).begin('mistral')
            self.assertEqual(path.read_bytes(), saved)

    def test_partial_claude_tool_call_never_becomes_an_answer(self):
        text = '```tool_calls\n[{"name":"write","arguments":{"file_path":"x"}}]\n```'
        delta = 'data: ' + json.dumps({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': text}}) + '\n'
        for suffix in ('', 'data: {broken}\ndata: {"type":"message_stop"}\n'):
            with self.subTest(suffix=suffix), self.assertRaises(ProviderUnavailable):
                claude_answer((delta + suffix).encode())
        self.assertEqual(claude_answer((delta + 'data: {"type":"message_stop"}\n').encode()), text)
        with self.assertRaises(ProviderUnavailable):
            claude_answer((delta + 'data: {"type":"message_stop"}\n' + delta).encode())

    def test_claude_truncation_is_not_success_even_with_terminal(self):
        for frame in (
            {'type': 'completion', 'completion': 'partial', 'stop_reason': 'max_tokens'},
            {'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens'}},
            {'choices': [{'delta': {'content': 'partial'}, 'finish_reason': 'length'}]},
        ):
            wire = 'data: ' + json.dumps(frame) + '\ndata: {"type":"message_stop"}\n'
            with self.subTest(frame=frame), self.assertRaises(ProviderUnavailable):
                claude_answer(wire.encode())

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_stale_dom_cannot_complete_a_new_request(self):
        run = subprocess.run(['node', str(Path(__file__).with_name('controller_stall_fixture.cjs'))],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
