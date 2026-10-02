from tests.support import isolated_access
import asyncio
import base64
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx

from providers import tab_bridge
from providers.common import ProviderUnavailable
from server import api, config
from server.schemas import ChatCompletionRequest
from chat_protocol import Reply

OWNER = "browser-owner-123456789"
DOCUMENT = "document-instance-123456789"


class BrokerTests(unittest.TestCase):
    def test_only_the_claiming_tab_can_finish_and_replay_is_rejected(self):
        broker = tab_bridge.Broker()
        with ThreadPoolExecutor() as pool:
            future = pool.submit(broker.submit, "grok", "marker", None, lambda: None, 2)
            until = time.monotonic() + 1
            while not broker.pending and time.monotonic() < until:
                time.sleep(.005)
            job = broker.claim("grok", OWNER, DOCUMENT)
            self.assertIsNone(broker.claim("grok", "another-owner-123456789", DOCUMENT))
            self.assertIsNone(broker.claim("glm", OWNER, DOCUMENT))
            self.assertEqual(broker.claim("grok", OWNER, DOCUMENT)['lease'], job['lease'])
            for provider, owner, lease in (("glm", OWNER, job["lease"]),
                                           ("grok", "another-owner-123456789", job["lease"]),
                                           ("grok", OWNER, "wrong-lease")):
                with self.assertRaises(ValueError):
                    broker.finish(provider, job["id"], owner, lease, DOCUMENT, {"text":"wrong"})
            broker.begin("grok",job['id'],OWNER,job['lease'],DOCUMENT)
            broker.finish("grok", job["id"], OWNER, job["lease"], DOCUMENT, {"text":"answer"})
            self.assertEqual(future.result(), {"text":"answer"})
            with self.assertRaises(ValueError):
                broker.finish("grok", job["id"], OWNER, job["lease"], DOCUMENT, {})

    def test_cancelled_job_retires_lease_before_next_request(self):
        broker = tab_bridge.Broker()
        cancel = threading.Event()

        def check():
            if cancel.is_set():
                raise asyncio.CancelledError()

        with ThreadPoolExecutor() as pool:
            future = pool.submit(broker.submit, "glm", "marker", None, check, 2)
            while not broker.pending:
                time.sleep(.005)
            job = broker.claim("glm", OWNER, DOCUMENT)
            cancel.set()
            self.assertFalse(broker.active('glm',job['id'],OWNER,job['lease'],DOCUMENT))
            with self.assertRaises(ValueError):
                broker.begin('glm',job['id'],OWNER,job['lease'],DOCUMENT)
            with self.assertRaises(asyncio.CancelledError):
                future.result()
        self.assertEqual(broker.pending, {})
        with self.assertRaises(ValueError):
            broker.finish("glm", job["id"], OWNER, job["lease"], DOCUMENT, {})

    def test_timeout_does_not_leave_a_claimable_job(self):
        broker = tab_bridge.Broker()
        with self.assertRaises(ProviderUnavailable):
            broker.submit("kimi", "marker", None, lambda: None, .01)
        self.assertIsNone(broker.claim("kimi", OWNER, DOCUMENT))

    def test_duplicate_owner_cannot_submit_from_two_documents(self):
        broker = tab_bridge.Broker()
        with ThreadPoolExecutor() as pool:
            future = pool.submit(broker.submit,'glm','marker',None,lambda:None,10)
            while not broker.pending:
                time.sleep(.005)
            job = broker.claim('glm',OWNER,DOCUMENT)
            other = 'another-document-123456789'
            self.assertIsNone(broker.claim('glm',OWNER,other))
            for result in ({'error':'wrong document'}, {'text':'wrong document'}):
                with self.assertRaises(ValueError):
                    broker.finish('glm',job['id'],OWNER,job['lease'],other,result)
            broker.begin('glm',job['id'],OWNER,job['lease'],DOCUMENT)
            for document in (DOCUMENT,other):
                with self.assertRaises(ValueError):
                    broker.begin('glm',job['id'],OWNER,job['lease'],document)
            broker.finish('glm',job['id'],OWNER,job['lease'],DOCUMENT,{'text':'answer'})
            self.assertEqual(future.result(),{'text':'answer'})

    def test_only_explicit_navigation_can_transfer_an_unsubmitted_lease(self):
        broker = tab_bridge.Broker()
        with ThreadPoolExecutor() as pool:
            future = pool.submit(broker.submit,'glm','marker',None,lambda:None,10)
            while not broker.pending:
                time.sleep(.005)
            job = broker.claim('glm',OWNER,DOCUMENT)
            broker.navigate('glm',job['id'],OWNER,job['lease'],DOCUMENT)
            other = 'navigation-document-123456789'
            successor = broker.claim('glm',OWNER,other)
            self.assertEqual(successor['lease'],job['lease'])
            self.assertIsNone(broker.claim('glm',OWNER,DOCUMENT))
            broker.begin('glm',job['id'],OWNER,job['lease'],other)
            with self.assertRaises(ValueError):
                broker.navigate('glm',job['id'],OWNER,job['lease'],other)
            broker.finish('glm',job['id'],OWNER,job['lease'],other,{'text':'answer'})
            self.assertEqual(future.result(),{'text':'answer'})

    def test_result_cannot_change_origin_path_or_resumed_conversation(self):
        body = json.dumps({"result":{"response":{"modelResponse":{"message":"marker","partial":False}}}}).encode()
        result = {"status":200,"body":base64.b64encode(body).decode(),"path":"/c/abc"}
        self.assertEqual(tab_bridge.parse_browser_result("grok", result).text, "marker")
        for bad in ({**result,"path":"https://evil.test/c/abc"}, {**result,"status":403},
                    {**result,"body":"!"}, {**result,"error":"blocked"}):
            with self.assertRaises((ProviderUnavailable, ValueError)):
                tab_bridge.parse_browser_result("grok", bad)
        with self.assertRaises(ProviderUnavailable):
            tab_bridge.parse_browser_result("grok", result, "/c/another")

    def test_pairing_exports_are_private_and_do_not_replace_the_placeholder_guard(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tab_bridge, "token_path", return_value=Path(directory)/"token"):
            destination = tab_bridge.export_scripts()
            token = (Path(directory)/"token").read_text()
            text = (destination/"opencode-controller.user.js").read_text()
            self.assertEqual(text.count(token), 1)
            self.assertIn('TOKEN === "__BRIDGE_TOKEN__"', text)
            for file in destination.iterdir():
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)


class TabApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        isolated_access(self)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app,client=("127.0.0.1",123)),base_url="http://local.test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_jobs_are_disabled_without_pairing_and_reject_remote_clients(self):
        token = "x" * 43
        with patch.dict(os.environ, {"BROWSER_BRIDGE_ENABLED":"1"}), patch.object(tab_bridge,"bridge_token",return_value=token):
            for header in ({}, {"Authorization":"Bearer wrong"}):
                response = await self.client.get("/browser/jobs/grok?owner="+OWNER+"&document="+DOCUMENT,headers=header)
                self.assertEqual(response.status_code,403)
            headers = {"Authorization":"Bearer "+token}
            self.assertEqual((await self.client.get("/browser/jobs/grok?owner="+OWNER+"&document="+DOCUMENT,headers=headers)).status_code,200)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app,client=("203.0.113.7",123)),base_url="http://local.test") as remote:
                self.assertEqual((await remote.get("/browser/jobs/grok?owner="+OWNER+"&document="+DOCUMENT,headers=headers)).status_code,403)
            with patch.dict(os.environ, {"BROWSER_BRIDGE_ENABLED":"0"}):
                self.assertEqual((await self.client.get("/browser/jobs/grok?owner="+OWNER+"&document="+DOCUMENT,headers=headers)).status_code,403)

    async def test_cross_provider_conversation_rejected_before_any_session(self):
        from providers.conversations import encode
        token = encode("glm","default","/c/example")
        with patch.dict(config.MODEL_MAP,{"grok-web":"default"}), patch.dict(config.OPTIONAL_MODEL_PROVIDERS,{"grok-web":"grok"}), patch.object(api,"build_provider_client") as factory:
            result = await self.client.post("/v1/chat/completions",json={"model":"grok-web","conversation_id":token,"messages":[{"role":"user","content":"marker"}]})
        self.assertEqual(result.status_code,400)
        factory.assert_not_called()

    async def test_document_submit_permission_is_atomic_and_new_routes_require_pairing(self):
        broker = tab_bridge.Broker()
        headers = {'Authorization':'Bearer ' + 'x' * 43}
        with patch.dict(os.environ,{'BROWSER_BRIDGE_ENABLED':'1'}), patch.object(tab_bridge,'bridge_token',return_value='x' * 43), patch('server.browser_routes.broker',broker), ThreadPoolExecutor() as pool:
            future = pool.submit(broker.submit,'glm','marker',None,lambda:None,10)
            while not broker.pending:
                await asyncio.sleep(.005)
            response = await self.client.get('/browser/jobs/glm',params={'owner':OWNER,'document':DOCUMENT},headers=headers)
            job = response.json()['job']
            value = {'provider':'glm','id':job['id'],'owner':OWNER,'lease':job['lease'],'document':DOCUMENT}
            for path in ('/browser/check','/browser/submit','/browser/navigate'):
                self.assertEqual((await self.client.post(path,json=value)).status_code,403)
            self.assertTrue((await self.client.post('/browser/check',json=value,headers=headers)).json()['active'])
            responses = await asyncio.gather(*(self.client.post('/browser/submit',json=value,headers=headers) for _ in range(2)))
            self.assertEqual(sorted(response.status_code for response in responses),[200,409])
            self.assertEqual((await self.client.post('/browser/navigate',json=value,headers=headers)).status_code,409)
            self.assertEqual((await self.client.post('/browser/result',json={**value,'result':{'text':'answer'}},headers=headers)).status_code,200)
            self.assertEqual(future.result(),{'text':'answer'})
            self.assertFalse((await self.client.post('/browser/check',json=value,headers=headers)).json()['active'])

    async def test_access_rejection_is_not_retried(self):
        from unittest.mock import Mock
        client = Mock()
        client.chat.side_effect = ProviderUnavailable("region access denied")
        with patch.dict(config.MODEL_MAP,{"grok-web":"default"}), patch.dict(config.OPTIONAL_MODEL_PROVIDERS,{"grok-web":"grok"}), patch.object(api,"build_provider_client",return_value=client):
            response = await self.client.post("/v1/chat/completions",json={"model":"grok-web","messages":[{"role":"user","content":"marker"}]})
        self.assertEqual(response.status_code,403)
        self.assertEqual(client.chat.call_count,1)

    async def test_expiry_or_cancellation_during_pacing_never_authorizes_submission(self):
        from providers.access import AccessGuard
        headers = {'Authorization':'Bearer ' + 'x' * 43}
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                guard = AccessGuard(path=None, interval=2)
                guard.wait('glm')
                attempt = guard.begin('glm')
                signal = threading.Event()
                def check():
                    if signal.is_set():
                        raise asyncio.CancelledError()
                broker = tab_bridge.Broker()
                job = tab_bridge.Job('owned-job', 'glm', 'marker', None,
                    owner=OWNER, lease='owned-lease', document=DOCUMENT,
                    deadline=time.monotonic() + (10 if cancelled else .1),
                    check_cancelled=check, access_attempt=attempt)
                broker.pending['glm'] = job
                timer = threading.Timer(.1, signal.set)
                if cancelled:
                    timer.start()
                try:
                    with patch.dict(os.environ, {'BROWSER_BRIDGE_ENABLED':'1'}), patch.object(tab_bridge, 'bridge_token', return_value='x' * 43), patch('server.browser_routes.broker', broker):
                        response = await self.client.post('/browser/submit', json={
                            'provider':'glm', 'id':job.id, 'owner':OWNER,
                            'lease':job.lease, 'document':DOCUMENT}, headers=headers)
                    self.assertEqual(response.status_code, 409)
                    self.assertFalse(job.submitted)
                    self.assertFalse(attempt.dispatched)
                finally:
                    timer.cancel()
                    attempt.abort()
                guard.check('glm')

    async def test_browser_routes_see_cancelled_origin_before_its_worker_wakes(self):
        broker = tab_bridge.Broker()
        waiting, release = threading.Event(), threading.Event()
        signals = []

        def pause_worker(timeout):
            # Keep the original job pending while browser routes execute in
            # their own context, exposing the cancellation race deterministically.
            signals.append(api._worker_cancellation.get())
            broker.condition.release()
            waiting.set()
            try:
                if not release.wait(5):
                    raise TimeoutError("test did not release the origin worker")
            finally:
                broker.condition.acquire()

        async def until(predicate):
            async def poll():
                while not predicate():
                    await asyncio.sleep(.005)
            await asyncio.wait_for(poll(), 2)

        headers = {'Authorization':'Bearer ' + 'x' * 43}
        req = ChatCompletionRequest(model='glm-web',messages=[{'role':'user','content':'marker'}])
        with patch.dict(os.environ,{'BROWSER_BRIDGE_ENABLED':'1'}), patch.dict(config.MODEL_MAP,{'glm-web':'default'}), patch.dict(config.OPTIONAL_MODEL_PROVIDERS,{'glm-web':'glm'}), patch.dict(api._provider_request_gates,{'glm':asyncio.Lock()}), patch.object(tab_bridge,'bridge_token',return_value='x' * 43), patch.object(tab_bridge,'broker',broker), patch('server.browser_routes.broker',broker), patch.object(broker.condition,'wait',side_effect=pause_worker):
            task = asyncio.create_task(api._run_chat_with_retry('marker',req,'default'))
            try:
                await until(waiting.is_set)
                job = (await self.client.get('/browser/jobs/glm',params={'owner':OWNER,'document':DOCUMENT},headers=headers)).json()['job']
                value = {'provider':'glm','id':job['id'],'owner':OWNER,'lease':job['lease'],'document':DOCUMENT}
                pending = broker.pending['glm']
                task.cancel()
                await until(signals[0].is_set)
                self.assertIsNone(api._worker_cancellation.get())
                self.assertIn('glm',broker.pending)
                self.assertFalse((await self.client.post('/browser/check',json=value,headers=headers)).json()['active'])
                for path in ('/browser/submit','/browser/navigate'):
                    self.assertEqual((await self.client.post(path,json=value,headers=headers)).status_code,409)
                self.assertIsNone((await self.client.get('/browser/jobs/glm',params={'owner':OWNER,'document':DOCUMENT},headers=headers)).json()['job'])
                self.assertFalse(pending.submitted)
            finally:
                release.set()
                task.cancel()
                result = await asyncio.wait_for(asyncio.gather(task,return_exceptions=True),2)
        self.assertIsInstance(result[0],asyncio.CancelledError)
        self.assertEqual(broker.pending,{})

    async def test_kimi_only_bound_completed_wire_answers_can_become_tools(self):
        from tests.test_kimi_protocol import fixture, wire
        tools = [{"type":"function","function":{"name":"read","parameters":{"type":"object"}}}]
        text = '```tool_calls\n[{"name":"read","arguments":{"filePath":"fixture.txt"}}]\n```'
        for completed in (False, True):
            for stream in (False, True):
                api.guard.resume("kimi")  # each case is a distinct owned fixture
                def submit_result(provider, prompt, *args, **kwargs):
                    frames = fixture()
                    frames[2]['message']['blocks'][0]['text']['content'] = prompt
                    frames[7]['block']['text']['content'] = text
                    frames[8]['block']['text']['content'] = ''
                    if not completed:
                        frames = frames[:-2] + frames[-1:]
                    return {'status':200,'path':'/chat/owned-chat','body':base64.b64encode(wire(frames)).decode()}
                with patch.dict(os.environ,{'BROWSER_BRIDGE_ENABLED':'1'}), patch.dict(config.MODEL_MAP,{'kimi-web':'default'}), patch.dict(config.OPTIONAL_MODEL_PROVIDERS,{'kimi-web':'kimi'}), patch.object(api,'build_provider_client',return_value=tab_bridge.TabClient('kimi')), patch.object(tab_bridge.broker,'submit',side_effect=submit_result) as submit:
                    response = await self.client.post('/v1/chat/completions',json={'model':'kimi-web','tools':tools,'stream':stream,'messages':[{'role':'user','content':'marker'}]})
                submit.assert_called_once()
                if not completed:
                    self.assertNotIn('"tool_calls":',response.text)
                    self.assertIn('error',response.text)
                    continue
                self.assertEqual(response.status_code,200)
                if stream:
                    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ') and line != 'data: [DONE]']
                    self.assertTrue(any(frame['choices'][0]['delta'].get('tool_calls',[{}])[0].get('function',{}).get('name') == 'read' for frame in frames))
                    self.assertTrue(any(frame['choices'][0]['finish_reason'] == 'tool_calls' for frame in frames))
                    self.assertIn('data: [DONE]',response.text)
                else:
                    choice = response.json()['choices'][0]
                    self.assertEqual(choice['message']['tool_calls'][0]['function']['name'],'read')
                    self.assertEqual(choice['finish_reason'],'tool_calls')

    async def test_failed_grok_terminal_never_becomes_tool_calls(self):
        tools = [{"type":"function","function":{"name":"bash","parameters":{"type":"object"}}}]
        text = '```tool_calls\n[{"name":"bash","arguments":{"command":"echo must-not-run"}}]\n```'
        for error in ({'error':True}, {'streamErrors':[{'error':'fatal'}]}):
            body = json.dumps({'result':{'response':{'modelResponse':{'partial':False,'message':text,**error}}}}).encode()
            result = {'status':200,'path':'/c/example','body':base64.b64encode(body).decode()}
            for stream in (False,True):
                api.guard.resume("grok")
                api.guard.resume("kimi")
                with patch.dict(os.environ,{'BROWSER_BRIDGE_ENABLED':'1'}), patch.dict(config.MODEL_MAP,{'grok-web':'default'}), patch.dict(config.OPTIONAL_MODEL_PROVIDERS,{'grok-web':'grok'}), patch.object(api,'build_provider_client',return_value=tab_bridge.TabClient('grok')), patch.object(tab_bridge.broker,'submit',return_value=result) as submit:
                    response = await self.client.post('/v1/chat/completions',json={'model':'grok-web','tools':tools,'stream':stream,'messages':[{'role':'user','content':'marker'}]})
                submit.assert_called_once()
                self.assertNotIn('"tool_calls":',response.text)
                self.assertIn('error',response.text)
                if not stream:
                    self.assertGreaterEqual(response.status_code,400)

    async def test_buffered_plain_reply_emits_keepalive_and_never_executes_text(self):
        from unittest.mock import Mock
        client = Mock()
        text = '```tool_calls\n[{"name":"bash","arguments":{}}]\n```'
        def chat(*args):
            time.sleep(.05)
            return Reply(text,"web:grok:marker")
        client.chat.side_effect = chat
        with patch.dict(config.MODEL_MAP,{"grok-web":"default"}), patch.dict(config.OPTIONAL_MODEL_PROVIDERS,{"grok-web":"grok"}), patch.object(api,"build_provider_client",return_value=client), patch.object(api,"_KEEPALIVE_SECONDS",.01):
            response = await self.client.post("/v1/chat/completions",json={"model":"grok-web","stream":True,"messages":[{"role":"user","content":"marker"}]})
        self.assertIn(": keep-alive", response.text)
        data = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
        self.assertTrue(all("tool_calls" not in frame["choices"][0]["delta"] for frame in data))
        self.assertIn("tool_calls", "".join(frame["choices"][0]["delta"].get("content") or "" for frame in data))
