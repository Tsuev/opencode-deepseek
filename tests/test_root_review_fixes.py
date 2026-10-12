from tests import TEST_API_TOKEN
"""Offline regressions for the October10 independent review; no real accounts."""
import asyncio
import base64
import json
import tempfile
import shutil
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from deepseek.auth import write_private_json
from deepseek.client import _decode_cid, _parse_sse, DeepSeekStreamError
from providers.access import AccessGuard
from providers.common import ProviderUnavailable
from providers.tab_bridge import Broker, Job, parse_browser_result
from server import api
from server.schemas import ChatCompletionRequest, ChatMessage
from server.openai_format import parse_tool_calls, ToolCallError
from server.conversation_cache import ConversationCache, ConversationStateError
from chat_protocol import Reply

class ProtocolFixes(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required for observer VM fixture')
    def test_actual_observer_request_binding(self):
        subprocess.run([shutil.which('node'), str(Path(__file__).with_name('observer_binding_fixture.cjs'))],
                       check=True, capture_output=True, text=True, timeout=10)

    def test_invalid_resume_ids_fail_closed(self):
        for value in (':','chat:not-an-id','chat:2:4','chat:0','chat:-1','chat:２','chat','/chat:2'):
            with self.subTest(value=value), self.assertRaises(ValueError): _decode_cid(value)
        self.assertEqual(_decode_cid('owned-chat:2'),('owned-chat',2))
        self.assertEqual(_decode_cid(None),(None,None))

    def test_error_done_is_error(self):
        with self.assertRaises(DeepSeekStreamError):
            list(_parse_sse(['event: error','data: [DONE]','']))

    def test_no_text_or_snapshot_after_terminal(self):
        first={'v':{'response':{'message_id':2,'status':'FINISHED','fragments':[{'type':'RESPONSE','content':'safe'}]}}}
        for later in ({'p':'response/fragments/0/content','o':'APPEND','v':'-late'},{'p':'response/fragments','o':'APPEND','v':[{'type':'RESPONSE','content':'late'}]}):
            with self.subTest(later=later),self.assertRaises(DeepSeekStreamError):
                list(_parse_sse(['data: '+json.dumps(first),'','data: '+json.dumps(later),'','data: [DONE]','']))

    def test_duplicate_keys_reject_whole_tool_batch(self):
        for arguments in ('{"path":"A","path":"B"}','{"nested":{"path":"A","path":"B"}}',json.dumps('{"path":"A","path":"B"}')):
            with self.subTest(arguments=arguments),self.assertRaises(ToolCallError):
                parse_tool_calls('```tool_calls\n[{"name":"write","arguments":{}},{"name":"write","arguments":'+arguments+'}]\n```', ['write'])

    def test_private_write_preserves_existing_parent_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory)/'shared';parent.mkdir(mode=0o755);parent.chmod(0o755)
            target=parent/'state.json';write_private_json(target,{'synthetic':True})
            self.assertEqual(parent.stat().st_mode&0o777,0o755)
            self.assertEqual(target.stat().st_mode&0o777,0o600)

    def test_partial_results_do_not_consume_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            cache=ConversationCache(Path(directory)/'state.sqlite3')
            req=ChatCompletionRequest(model='deepseek-chat',messages=[ChatMessage(role='user',content='two calls')])
            calls=[{'id':i,'type':'function','function':{'name':'read','arguments':'{}'}} for i in ('A','B')]
            cache.remember(req,Reply('','chat:2'),'',calls,0)
            history=[*req.messages,ChatMessage(role='assistant',content='',tool_calls=calls)]
            partial=req.model_copy(update={'messages':[*history,ChatMessage(role='tool',tool_call_id='A',content='A result')]})
            with self.assertRaises(ConversationStateError):cache.prepare(partial)
            complete=partial.model_copy(update={'messages':[*partial.messages,ChatMessage(role='tool',tool_call_id='B',content='B result')]})
            ready,_=cache.prepare(complete)
            self.assertEqual(ready.conversation_id,'chat:2')

    def test_browser_exact_binding_required(self):
        body=json.dumps({'result':{'response':{'modelResponse':{'message':'answer','partial':False}}}}).encode()
        result={'status':200,'path':'/c/expected','body':base64.b64encode(body).decode()}
        for turn in (None,{'chat_id':'other','prompt':'owned','endpoint':'/rest/app-chat/conversations/other/responses'}, {'chat_id':'expected','prompt':'neighbor','endpoint':'/rest/app-chat/conversations/expected/responses'}):
            with self.subTest(turn=turn),self.assertRaises(ProviderUnavailable):
                parse_browser_result('grok',{**result,'request_turn':turn},'/c/expected','owned')
        turn={'chat_id':'expected','prompt':'owned','endpoint':'/rest/app-chat/conversations/expected/responses'}
        self.assertEqual(parse_browser_result('grok',{**result,'request_turn':turn},'/c/expected','owned').text,'answer')

    def test_response_identity_cannot_override_bound_request(self):
        body=json.dumps({'result':{'response':{'conversationId':'other', 'modelResponse':{'message':'answer','partial':False}}}}).encode()
        result={'status':200,'path':'/c/expected','body':base64.b64encode(body).decode(),
                'request_turn':{'chat_id':'expected','prompt':'owned','endpoint':'/rest/app-chat/conversations/expected/responses'}}
        with self.assertRaises(ProviderUnavailable):parse_browser_result('grok',result,'/c/expected','owned')

    def test_pacing_releases_broker_lock_and_rejects_duplicate_submit(self):
        broker=Broker();entered=threading.Event();release=threading.Event();errors=[]
        class Attempt:
            def check(self):pass
            def dispatch(self,validate):entered.set();release.wait(2);validate()
        broker.pending['glm']=Job('job','glm','prompt',None,owner='owner',lease='lease',document='document',deadline=time.monotonic()+5,access_attempt=Attempt())
        broker.pending['grok']=Job('other','grok','prompt',None,deadline=time.monotonic()+5)
        def run():
            try:broker.begin('glm','job','owner','lease','document')
            except BaseException as exc:errors.append(exc)
        thread=threading.Thread(target=run);thread.start()
        try:
            self.assertTrue(entered.wait(1))
            start=time.monotonic()
            self.assertIsNotNone(broker.claim('grok','owner_123456789012','document_12345678'))
            self.assertLess(time.monotonic()-start,.5)
            with self.assertRaises(ValueError):broker.begin('glm','job','owner','lease','document')
            with self.assertRaises(ValueError):broker.navigate('glm','job','owner','lease','document')
        finally:release.set();thread.join(3)
        self.assertFalse(thread.is_alive());self.assertFalse(errors)
        self.assertTrue(broker.pending['glm'].submitted)

class ApiFixes(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_history_and_resume_rejected_before_provider(self):
        async def forbidden(*args,**kwargs):raise AssertionError('must not reach provider')
        with patch.object(api,'guard',AccessGuard(path=None,interval=0)),patch.dict(api.MODEL_MAP,{'deepseek-chat':'default'},clear=True),patch.object(api,'_run_tool_chat',forbidden):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://fixture.local') as client:
                r=await client.post('/v1/chat/completions',json={'model':'deepseek-chat','messages':[{'role':'assistant','tool_calls':[{'id':['bad'],'function':{'name':'read','arguments':'{}'}}]}]})
                self.assertEqual(r.status_code,422)
                r=await client.post('/v1/chat/completions',json={'model':'deepseek-chat','messages':[{'role':'user','content':'hello'}],'conversation_id':':'})
                self.assertEqual(r.status_code,400)

    async def test_disconnect_cancels_work_before_dispatch(self):
        disconnected=asyncio.Event();cancelled=asyncio.Event();dispatched=[]
        class Request:
            async def receive(self):
                await disconnected.wait()
                return {"type":"http.disconnect"}
        async def queued(req):
            try:
                disconnected.set()
                await asyncio.sleep(.5)
                dispatched.append(True)
            except asyncio.CancelledError:cancelled.set();raise
        req=ChatCompletionRequest(messages=[ChatMessage(role='user',content='synthetic')])
        with patch.object(api,'_chat_completions',queued):
            response=await asyncio.wait_for(api.chat_completions(req,Request()),2)
        self.assertEqual(response.status_code,499);self.assertTrue(cancelled.is_set());self.assertFalse(dispatched)

    async def test_actual_asgi_disconnect_through_rate_limit_middleware(self):
        body=json.dumps({'model':'deepseek-chat','messages':[{'role':'user','content':'synthetic'}]}).encode()
        received=0
        dispatched=[]
        async def receive():
            nonlocal received
            received+=1
            if received==1:return {'type':'http.request','body':body,'more_body':False}
            return {'type':'http.disconnect'}
        async def send(event):pass
        async def queued(*args,**kwargs):
            await asyncio.sleep(.15)
            dispatched.append(True)
            return Reply('synthetic','fake:2'),'synthetic',[]
        scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'http_version':'1.1','method':'POST',
               'scheme':'http','path':'/v1/chat/completions','raw_path':b'/v1/chat/completions','query_string':b'',
               'headers':[(b'host',b'localhost'),(b'authorization',('Bearer '+TEST_API_TOKEN).encode()),
                          (b'content-type',b'application/json'),(b'content-length',str(len(body)).encode())],
               'client':('disconnect-fixture',123),'server':('localhost',80),'root_path':''}
        with patch.object(api,'guard',AccessGuard(path=None,interval=0)),patch.dict(api.MODEL_MAP,{'deepseek-chat':'default'},clear=True),patch.object(api,'_run_tool_chat',queued):
            await asyncio.wait_for(api.app(scope,receive,send),2)
        self.assertGreaterEqual(received,2)
        self.assertFalse(dispatched)
