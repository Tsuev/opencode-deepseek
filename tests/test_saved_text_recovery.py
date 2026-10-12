import json
import tempfile
import unittest
from pathlib import Path

from chat_protocol import Reply
from deepseek.auth import write_private_json
from server.conversation_cache import ConversationCache, ConversationStateError
from server.openai_format import ToolCallError
from server.recovery_status import RECOVERY_STOP_TEXT
from server.saved_text_recovery import SavedTextRecovery
from server.schemas import ChatCompletionRequest, ChatMessage
from server.tool_protocol import validate_reply
from tests.test_tool_protocol import tool


class SavedTextRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = ConversationCache(self.root / 'history.sqlite3')
        self.raw = '```calls\n[{"name":"read","arguments":{"file_path":"a","content":"b"}}]\n```'
        self.original = ChatCompletionRequest(model='deepseek-chat', tools=[tool('read')],
            messages=[ChatMessage(role='user', content='original')])
        self.cache.remember(self.original, Reply(self.raw, 'saved:64'), self.raw, None, 5, scope='owned')
        self.req = self.original.model_copy(update={'messages': [*self.original.messages,
            ChatMessage(role='assistant', content=self.raw), ChatMessage(role='user', content='recover once')]})
        write_private_json(self.root / 'control.json', {'phase': 'ready', 'model': 'deepseek-chat',
            'reply': self.raw, 'trigger': 'recover once'})
        def validate(reply, req):
            content, calls = validate_reply(reply.text, req.tools, req.tool_choice)
            return reply, content, calls
        self.recovery = SavedTextRecovery(self.root, self.cache, 'owned', validate)

    def test_delivers_once_same_cid_and_count_then_stops_without_claiming_next_reply(self):
        reply, content, calls = self.recovery.run('', self.req, None)
        self.assertEqual(reply.conversation_id, 'saved:64')
        with self.assertRaises(ToolCallError):
            self.recovery.run('', self.req, None)
        history = [*self.req.messages, ChatMessage(role='assistant', content=content, tool_calls=calls),
                   ChatMessage(role='tool', tool_call_id=calls[0]['id'], content='read result')]
        req = self.req.model_copy(update={'messages': history})
        self.assertEqual(self.recovery.run('', req, None)[1], RECOVERY_STOP_TEXT)
        with self.cache.connection() as db:
            self.assertEqual(db.execute('select steps from links').fetchall(), [(6,), (6,)])
        req = req.model_copy(update={'messages': [*history, ChatMessage(role='assistant', content=RECOVERY_STOP_TEXT),
                                                       ChatMessage(role='user', content='continue')]})
        resumed, count = self.cache.prepare(req, scope='owned')
        self.assertEqual((resumed.conversation_id, count), ('saved:64', 6))

    def test_changed_history_and_trigger_never_deliver(self):
        for index in (0, 2):
            messages = list(self.req.messages)
            messages[index] = ChatMessage(role='user', content='changed')
            with self.assertRaises((ToolCallError, ConversationStateError)):
                self.recovery.run('', self.req.model_copy(update={'messages': messages}), None)
        with self.cache.connection() as db:
            self.assertEqual(db.execute('select used from links').fetchall(), [(0,)])

    def test_schema_failure_leaves_parent_unclaimed(self):
        req = self.req.model_copy(update={'tool_choice': 'none'})
        with self.assertRaises(ToolCallError):
            self.recovery.run('', req, None)
        with self.cache.connection() as db:
            self.assertEqual(db.execute('select used from links').fetchall(), [(0,)])
