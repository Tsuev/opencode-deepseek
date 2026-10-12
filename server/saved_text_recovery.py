"""One-use local delivery of a saved answer previously returned as text."""
import json
import threading

from chat_protocol import Reply
from deepseek.auth import write_private_json
from .openai_format import ToolCallError, _text_of
from .recovery_status import recovery_stopped
from .schemas import ChatMessage


class SavedTextRecovery:
    def __init__(self, root, cache, scope, validate):
        self.root, self.cache, self.scope, self.validate = root, cache, scope, validate
        self.lock = threading.Lock()

    def run(self, prompt, req, model):
        with self.lock:
            state = json.loads((self.root / 'control.json').read_text())
            positions = [i for i, m in enumerate(req.messages) if m.role == 'assistant']
            if req.conversation_id or req.model != state['model'] or not positions:
                raise ToolCallError('Saved-text recovery history mismatch; no request sent.')
            end = positions[-1] + 1
            parent = req.messages[end - 1]
            key = self.cache.key(req, req.messages[:end], self.scope)
            if state['phase'] == 'delivered':
                tail = req.messages[end:]
                ids = [m.tool_call_id for m in tail if m.role == 'tool']
                expected = [c['id'] for c in parent.tool_calls or []]
                if (key != state['expected_key'] or len(ids) != len(set(ids))
                        or set(ids) != set(expected) or not ids
                        or any(m.role != 'tool' for m in tail)):
                    raise ToolCallError('Recovered tool results mismatch; no request sent.')
                return recovery_stopped()
            if (state['phase'] != 'ready' or parent.tool_calls
                    or _text_of(parent.content) != state['reply']
                    or len(req.messages[end:]) != 1
                    or req.messages[end].role != 'user'
                    or _text_of(req.messages[end].content) != state['trigger']):
                raise ToolCallError('Saved-text recovery is not authorized for this history.')
            # Full schema validation happens BEFORE claiming the existing link.
            result = self.validate(Reply(state['reply'], None, 'stop'), req)
            if not result[2]:
                raise ToolCallError('Saved reply contains no validated calls.')
            prepared, steps = self.cache.prepare(req, scope=self.scope)
            if not prepared.conversation_id or steps < 1:
                raise ToolCallError('Saved reply has no established continuation link.')
            reply, content, calls = result
            reply = Reply(reply.text, prepared.conversation_id, reply.finish_reason)
            state['phase'] = 'delivering'
            write_private_json(self.root / 'control.json', state)
            write_private_json(self.root / 'request.json', req.model_dump())
            # No new upstream answer: carry the same CID and accepted-reply count.
            self.cache.remember(req, reply, content, calls, steps - 1, scope=self.scope)
            state.update(phase='delivered', expected_key=self.cache.key(req,
                [*req.messages, ChatMessage(role='assistant', content=content, tool_calls=calls)], self.scope))
            write_private_json(self.root / 'control.json', state)
            write_private_json(self.root / 'result.json', {'cid': reply.conversation_id,
                'content': content, 'calls': calls, 'steps': steps})
            return reply, content, calls
