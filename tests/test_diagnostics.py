import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from chat_protocol import Reply
from server import diagnostics
from server.schemas import ChatCompletionRequest, ChatMessage


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'private' / 'last.json'
        self.reply = Reply('PRIVATE_INVALID_REPLY', 'PRIVATE_CONVERSATION')
        self.request = ChatCompletionRequest(model='deepseek-chat', messages=[
            ChatMessage(role='user', content='PRIVATE_PROMPT')])

    def test_capture_is_opt_in_private_deduplicated_and_bounded(self):
        with patch.object(diagnostics, 'FAILURE_FILE', self.path), patch.dict(os.environ, TOOLCALL_DIAGNOSTICS='0'):
            diagnostics.save_tool_failure(self.reply, self.request)
            self.assertFalse(self.path.exists())
        with patch.object(diagnostics, 'FAILURE_FILE', self.path), patch.dict(os.environ, TOOLCALL_DIAGNOSTICS='1'):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                diagnostics.save_tool_failure(self.reply, self.request)
            self.assertEqual(output.getvalue(), '')
            value = self.path.read_text()
            self.assertIn('PRIVATE_INVALID_REPLY', value)
            self.assertNotIn('PRIVATE_PROMPT', value)
            self.assertNotIn('PRIVATE_CONVERSATION', value)
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
            diagnostics.save_tool_failure(Reply('replacement', None), self.request)
            self.assertEqual(json.loads(self.path.read_text())['reply'], 'replacement')
            archive = self.path.parent / 'toolcall-failures'
            self.assertEqual(len(list(archive.iterdir())), 2)
            for entry in archive.iterdir():
                self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o600)
            diagnostics.save_tool_failure(Reply('replacement', None), self.request)
            self.assertEqual(len(list(archive.iterdir())), 2)
            with patch.object(diagnostics, 'MAX_ARCHIVED', 2):
                diagnostics.save_tool_failure(Reply('new failure', None), self.request)
            self.assertEqual(len(list(archive.iterdir())), 2)
            self.assertEqual(json.loads(self.path.read_text())['reply'], 'new failure')

    def test_capture_failure_never_leaks_payload_or_replaces_original_error(self):
        with patch.dict(os.environ, TOOLCALL_DIAGNOSTICS='1'), \
                patch.object(diagnostics, 'write_private_json', side_effect=OSError('PRIVATE_PATH')) as writer, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            diagnostics.save_tool_failure(self.reply, self.request)
        writer.assert_called_once()
        self.assertNotIn('PRIVATE', output.getvalue())
        with patch.dict(os.environ, TOOLCALL_DIAGNOSTICS='1'), \
                patch.object(diagnostics, 'MAX_BYTES', 1), \
                patch.object(diagnostics, 'write_private_json') as writer, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            diagnostics.save_tool_failure(self.reply, self.request)
        writer.assert_not_called()
        self.assertNotIn('PRIVATE', output.getvalue())
