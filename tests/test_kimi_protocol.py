import base64
import copy
import json
import struct
import unittest

from providers.common import ProviderUnavailable
from providers.kimi_protocol import kimi_answer
from providers.tab_bridge import parse_browser_result


def envelope(value, flags=0):
    payload = json.dumps(value).encode()
    return bytes([flags]) + struct.pack(">I", len(payload)) + payload


def fixture():
    return [
        {"heartbeat":{}},
        {"op":"set","chat":{"id":"owned-chat"}},
        {"op":"set","mask":"message","message":{"id":"user","role":"user",
            "status":"MESSAGE_STATUS_COMPLETED","blocks":[{"text":{"content":"owned prompt"}}]}},
        {"op":"set","mask":"message","message":{"id":"assistant","role":"assistant",
            "parentId":"user","status":"MESSAGE_STATUS_GENERATING"}},
        {"op":"set","mask":"block.stage","block":{"id":"stage","messageId":"assistant",
            "parentId":"","stage":{"status":"STAGE_STATUS_START"}}},
        {"op":"set","mask":"block.think","block":{"id":"thinking","parentId":"stage",
            "think":{"content":"owned private reasoning"}}},
        {"op":"append","mask":"block.think.content","block":{"id":"thinking","parentId":"stage",
            "think":{"content":" must never become answer text"}}},
        {"op":"set","mask":"block.text","block":{"id":"text","parentId":"","text":{"content":"ans"}}},
        {"op":"append","mask":"block.text.content","block":{"id":"text","parentId":"","text":{"content":"wer"}}},
        {"op":"set","mask":"message.status","message":{"id":"assistant","status":"MESSAGE_STATUS_COMPLETED"}},
        {"done":{}},
    ]


def wire(frames):
    return b"".join(envelope(frame) for frame in frames) + envelope({},2)


class KimiProtocolTests(unittest.TestCase):
    def answer(self, frames):
        return kimi_answer(wire(frames),'owned prompt','owned-chat')

    def test_completed_wire_answer_excludes_thinking_and_does_not_use_dom_text(self):
        body = wire(fixture())
        self.assertEqual(kimi_answer(body,'owned prompt','owned-chat'),'answer')
        result = {'status':200,'path':'/chat/owned-chat','body':base64.b64encode(body).decode(),'text':'untrusted DOM text'}
        self.assertEqual(parse_browser_result('kimi',result,None,'owned prompt').text,'answer')

    def test_success_requires_message_completion_done_and_connect_end(self):
        frames = fixture()
        for body in (wire(frames[:-2]),wire(frames[:-1]),wire(frames[:-2]+frames[-1:]),
                     b''.join(envelope(f) for f in frames),wire(frames)[:-1]):
            with self.subTest(body=body), self.assertRaises(ProviderUnavailable):
                kimi_answer(body,'owned prompt','owned-chat')

    def test_conversation_prompt_assistant_parent_and_block_owner_must_match(self):
        changes = ((1,'chat','id','other-chat'),(2,'message','blocks',[{'text':{'content':'other prompt'}}]),
                   (3,'message','parentId','other-user'),(7,'block','messageId','other-assistant'),
                   (7,'block','parentId','unknown-parent'),(9,'message','id','other-assistant'))
        for index,group,key,value in changes:
            frames = copy.deepcopy(fixture());frames[index][group][key] = value
            with self.subTest(change=(index,key)), self.assertRaises(ProviderUnavailable):
                self.answer(frames)

    def test_failed_or_duplicate_completion_and_late_text_are_rejected(self):
        frames = fixture()
        failed = copy.deepcopy(frames);failed[-2]['message']['status'] = 'MESSAGE_STATUS_FAILED'
        for value in (failed,frames[:-1]+[frames[-2]]+frames[-1:],
                      frames[:-1]+[frames[8]]+frames[-1:],frames+[frames[8]],frames+[frames[-1]]):
            with self.subTest(frames=value), self.assertRaises(ProviderUnavailable):
                self.answer(value)

    def test_reasoning_block_cannot_be_converted_or_appended_as_answer_text(self):
        frames = fixture()
        replace = copy.deepcopy(frames);replace[7]['block']['id'] = 'thinking';replace[7]['block']['parentId'] = 'stage'
        append = copy.deepcopy(frames);append[8]['block']['id'] = 'thinking';append[8]['block']['parentId'] = 'stage'
        nested = copy.deepcopy(frames);nested[7]['block']['parentId'] = 'stage'
        for value in (replace,append,nested):
            with self.subTest(frames=value), self.assertRaises(ProviderUnavailable):
                self.answer(value)

    def test_second_assistant_or_answer_snapshot_is_ambiguous(self):
        frames = fixture()
        for value in (frames[:4]+[frames[3]]+frames[4:],frames[:8]+[frames[7]]+frames[8:]):
            with self.assertRaises(ProviderUnavailable):
                self.answer(value)

    def test_inline_assistant_blocks_cannot_lose_thinking_provenance(self):
        frames = copy.deepcopy(fixture())
        frames[3]['message']['blocks'] = [{'id':'text','parentId':'',
            'think':{'content':'owned seed thought'}}]
        with self.assertRaises(ProviderUnavailable):
            self.answer(frames)
        frames[3]['message']['blocks'] = []
        self.assertEqual(self.answer(frames),'answer')

    def test_assistant_identity_cannot_equal_its_user_parent(self):
        frames = copy.deepcopy(fixture())
        frames[3]['message']['id'] = 'user'
        frames[4]['block']['messageId'] = 'user'
        frames[9]['message']['id'] = 'user'
        with self.assertRaises(ProviderUnavailable):
            self.answer(frames)

    def test_error_or_notification_cannot_complete_a_partial_answer(self):
        for event in ({'error':{'code':'quota'}},{'notification':{'type':'quota'}},{'unknown':{}}):
            with self.subTest(event=event), self.assertRaises(ProviderUnavailable):
                self.answer(fixture()[:-1]+[event]+fixture()[-1:])

    def test_scope_and_prompt_binding_are_required(self):
        body = wire(fixture())
        for prompt,chat in ((None,'owned-chat'),('owned prompt',None),('other prompt','owned-chat'),('owned prompt','other-chat')):
            with self.subTest(prompt=prompt,chat=chat), self.assertRaises(ProviderUnavailable):
                kimi_answer(body,prompt,chat)

    def test_binary_or_compressed_protocol_is_rejected_without_dom_fallback(self):
        for body in (b'\0\0\0\0\2\x08\x01'+envelope({},2),envelope({},1)+envelope({},2)):
            result = {'status':200,'path':'/chat/owned-chat','body':base64.b64encode(body).decode(),'text':'fake answer'}
            with self.assertRaises(ProviderUnavailable):
                parse_browser_result('kimi',result,None,'owned prompt')
