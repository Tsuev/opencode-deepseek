import asyncio
import contextlib
import io
import json
import runpy
import threading
import time
import unittest
from unittest.mock import Mock, patch

import httpx
import anyio

from deepseek.auth import LoginRequired
from deepseek.client import Reply
from server import api
from server.schemas import ChatCompletionRequest, ChatMessage
from tests.test_stream import event, mock_client, snapshot
from tests.test_tools import block

TOOLS = [{"type":"function","function":{"name":name,"parameters":{"type":"object","properties":{}}}}
         for name in ("write","bash")]


def auth_error():
    request = httpx.Request("POST","https://chat.deepseek.com/api/v0/chat_session/create")
    response = httpx.Response(401,request=request)
    return httpx.HTTPStatusError("token expired",request=request,response=response)


class TextStream:
    conversation_id = "fake:2"
    finish_reason = "stop"
    def __iter__(self):
        yield "answer"


def sse_objects(text):
    return [json.loads(line[6:]) for line in text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        api._request_gate = asyncio.Lock()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.app,client=(self.id(),123)),base_url="http://audit.local")
        self.request = {"messages":[{"role":"user","content":"audit"}]}

    async def asyncTearDown(self):
        await self.client.aclose()

    async def post(self, **extra):
        return await self.client.post("/v1/chat/completions",json={**self.request,**extra})

    async def test_tool_choice_violations_never_return_executable_calls(self):
        fake = Mock()
        fake.chat.return_value = Reply(block([{"name":"bash","arguments":{"command":"echo audit"}}]),"fake:2")
        with patch.object(api,"get_client",return_value=fake):
            for choice in ["none",{"type":"function","function":{"name":"write"}}]:
                response = await self.post(tools=TOOLS,tool_choice=choice)
                self.assertEqual(response.status_code,502)
                self.assertEqual(response.json()["error"]["type"],"invalid_tool_response")
                self.assertNotIn("choices",response.json())

    async def test_streamed_policy_violation_is_an_error_not_an_action(self):
        fake = Mock()
        fake.chat.return_value = Reply(block([{"name":"bash","arguments":{}}]),"fake:2")
        with patch.object(api,"get_client",return_value=fake):
            response = await self.post(tools=TOOLS,tool_choice="none",stream=True)
        objects = sse_objects(response.text)
        self.assertEqual(objects[0]["error"]["type"],"invalid_tool_response")
        self.assertTrue(all("choices" not in obj for obj in objects))

    async def test_required_call_cannot_silently_become_plain_text(self):
        fake = Mock()
        fake.chat.return_value = Reply("I will write a file.","fake:2")
        with patch.object(api,"get_client",return_value=fake):
            response = await self.post(tools=TOOLS,tool_choice="required")
        self.assertEqual(response.status_code,502)

    async def test_invalid_request_policy_fails_before_upstream(self):
        with patch.object(api,"get_client") as get_client:
            for extra in [{"tool_choice":"required"},
                          {"tools":TOOLS,"tool_choice":{"type":"function","function":{"name":"unknown"}}},
                          {"tools":[{"function":{"name":[]}}]},
                          {"tools":[TOOLS[0],TOOLS[0]]}]:
                self.assertEqual((await self.post(**extra)).status_code,400)
            get_client.assert_not_called()

    async def test_invalid_nested_messages_are_rejected_before_upstream(self):
        with patch.object(api, "get_client") as factory:
            for message in ({"role": "user", "content": [{"type": "text", "text": 42}]},
                            {"role": "assistant", "tool_calls": [{"function": "bad-shape"}]}):
                response = await self.client.post("/v1/chat/completions", json={"messages": [message]})
                self.assertEqual(response.status_code, 422)
            factory.assert_not_called()

    async def test_quoted_examples_stay_visible_and_have_no_actions(self):
        fake = Mock()
        text = 'Do not execute this example: `bash(command="echo audit")`.'
        fake.chat.return_value = Reply(text,"fake:2")
        with patch.object(api,"get_client",return_value=fake):
            response = await self.post(tools=TOOLS)
        choice = response.json()["choices"][0]
        self.assertEqual(choice["finish_reason"],"stop")
        self.assertEqual(choice["message"]["content"],text)
        self.assertNotIn("tool_calls",choice["message"])

    async def test_partial_batch_is_continued_before_any_calls_are_returned(self):
        fake = Mock()
        fake.chat.side_effect = [
            Reply('```tool_calls\n[{"name":"write","arguments":{}},{"name":"bash","arguments":{"command":"echo',"fake:2","length"),
            Reply(' audit"}}]\n```',"fake:4"),
        ]
        with patch.object(api,"get_client",return_value=fake), contextlib.redirect_stdout(io.StringIO()):
            response = await self.post(tools=TOOLS)
        self.assertEqual(response.status_code,200)
        body = response.json()
        self.assertEqual(body["conversation_id"],"fake:4")
        calls = body["choices"][0]["message"]["tool_calls"]
        self.assertEqual([call["function"]["name"] for call in calls],["write","bash"])
        self.assertEqual(json.loads(calls[1]["function"]["arguments"]),{"command":"echo audit"})
        self.assertEqual(fake.chat.call_count,2)
        self.assertEqual(fake.chat.call_args_list[1].args[1:3],("fake:2",None))

    async def test_exhausted_continuation_returns_error_instead_of_partial_write(self):
        fake = Mock()
        fake.chat.return_value = Reply('```tool_calls\n[{"name":"write","arguments":{}},{',"fake:2")
        with patch.object(api,"get_client",return_value=fake), patch.object(api,"_MAX_CONTINUATIONS",0):
            response = await self.post(tools=TOOLS)
        self.assertEqual(response.status_code,502)
        self.assertEqual(fake.chat.call_count,1)
        self.assertNotIn("choices",response.json())

    async def test_failed_continuation_never_returns_partial_calls(self):
        fake = Mock()
        fake.chat.side_effect = [Reply('```tool_calls\n[{"name":"write","arguments":{}},{',"fake:2"),
                                 RuntimeError("network down")]
        with patch.object(api,"get_client",return_value=fake), contextlib.redirect_stdout(io.StringIO()):
            response = await self.post(tools=TOOLS)
        self.assertEqual(response.status_code,502)

    async def test_auth_refresh_on_buffered_call_is_retried_once(self):
        old, new = Mock(),Mock()
        old.chat.side_effect = auth_error()
        new.chat.return_value = Reply("answer","fake:2")
        with patch.object(api,"get_client",side_effect=[old,new]) as get_client, contextlib.redirect_stdout(io.StringIO()):
            response = await self.post()
        self.assertEqual(response.json()["choices"][0]["message"]["content"],"answer")
        self.assertEqual(old.chat.call_count,1)
        self.assertEqual(new.chat.call_count,1)
        self.assertEqual(get_client.call_args.kwargs,{"rejected_client":old})

    async def test_auth_retry_covers_stream_creation_and_first_iteration(self):
        for before_iterator in [True,False]:
            with self.subTest(before_iterator=before_iterator):
                old, new = Mock(),Mock()
                if before_iterator:
                    old.stream.side_effect = auth_error()
                else:
                    class BrokenStream:
                        def __iter__(self):
                            raise auth_error()
                            yield
                    old.stream.return_value = BrokenStream()
                new.stream.return_value = TextStream()
                with patch.object(api,"get_client",side_effect=[old,new]) as get_client:
                    response = await self.post(stream=True)
                objects = sse_objects(response.text)
                self.assertTrue(any(obj.get("choices",[{}])[0].get("delta",{}).get("content") == "answer" for obj in objects))
                self.assertFalse(any("error" in obj for obj in objects))
                self.assertEqual(get_client.call_count,2)

    async def test_midstream_error_does_not_retry_or_emit_successful_stop(self):
        class BrokenStream:
            conversation_id = "fake:2"
            def __iter__(self):
                yield "partial"
                raise auth_error()
        fake = Mock()
        fake.stream.return_value = BrokenStream()
        with patch.object(api,"get_client",return_value=fake) as get_client:
            response = await self.post(stream=True)
        objects = sse_objects(response.text)
        self.assertTrue(any("error" in obj for obj in objects))
        self.assertFalse(any(obj.get("choices",[{}])[0].get("finish_reason") == "stop" for obj in objects))
        self.assertEqual(get_client.call_count,1)

    async def test_login_required_remains_actionable_503(self):
        with patch.object(api,"get_client",side_effect=LoginRequired()):
            response = await self.post()
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json()["error"]["type"],"login_required")

    async def test_upstream_eof_cannot_be_reported_as_success(self):
        fake = mock_client(snapshot("partial"))
        try:
            with patch.object(api,"get_client",return_value=fake):
                response = await self.post()
            self.assertEqual(response.status_code,500)
            self.assertNotIn("choices",response.json())
        finally:
            fake.close()

    async def test_different_responses_cannot_be_spliced_into_a_tool_call(self):
        prefix = '```tool_calls\n[{"name":"write","arguments":{"filePath":"offline.txt","content":"'
        suffix = 'synthetic content"}}]\n```'
        second = event({"v": {"response": {"message_id": 4, "fragments": [{"type": "RESPONSE", "content": "X" * len(prefix) + suffix}], "status": "FINISHED"}}})
        fake = mock_client(snapshot(prefix) + second)
        try:
            with patch.object(api, "get_client", return_value=fake):
                response = await self.post(tools=TOOLS)
            self.assertEqual(response.status_code, 500)
            self.assertNotIn("choices", response.json())
            self.assertIn("message_id", response.json()["error"]["message"])
        finally:
            fake.close()

    async def test_length_finish_reason_survives_both_response_modes(self):
        fake = mock_client(snapshot("cut","INCOMPLETE"))
        try:
            with patch.object(api,"get_client",return_value=fake):
                for streamed in [False,True]:
                    response = await self.post(stream=streamed)
                    if streamed:
                        reason = sse_objects(response.text)[-1]["choices"][0]["finish_reason"]
                    else:
                        reason = response.json()["choices"][0]["finish_reason"]
                    self.assertEqual(reason,"length")
        finally:
            fake.close()

    async def test_account_queue_serializes_streams_and_buffered_requests(self):
        state = {"active":0,"maximum":0}
        lock = threading.Lock()
        def enter():
            with lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"],state["active"])
        def leave():
            with lock:
                state["active"] -= 1
        class Client:
            def chat(self,*args):
                enter()
                try:
                    time.sleep(0.02)
                    return Reply("answer","fake:2")
                finally:
                    leave()
            def stream(self,*args,**kwargs):
                class Stream(TextStream):
                    def __iter__(self):
                        enter()
                        try:
                            yield "answer"
                            time.sleep(0.02)
                        finally:
                            leave()
                return Stream()
        with patch.object(api,"get_client",return_value=Client()):
            responses = await asyncio.gather(self.post(stream=True),self.post(),self.post())
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(state,{"active":0,"maximum":1})

    async def test_cancelled_worker_keeps_gate_until_sync_http_finishes(self):
        entered, release = threading.Event(),threading.Event()
        calls = []
        def chat(*args):
            calls.append(args[0])
            if args[0] == "first":
                entered.set()
                release.wait(timeout=2)
            return Reply("answer","fake:2")
        fake = Mock()
        fake.chat.side_effect = chat
        req = ChatCompletionRequest(messages=[ChatMessage(role="user",content="audit")])
        with patch.object(api,"get_client",return_value=fake):
            first = asyncio.create_task(api._run_chat_with_retry("first",req,"default"))
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            first.cancel()
            second = asyncio.create_task(api._run_chat_with_retry("second",req,"default"))
            try:
                await asyncio.sleep(0.02)
                self.assertEqual(calls,["first"])
            finally:
                release.set()
            results = await asyncio.gather(first,second,return_exceptions=True)
        self.assertIsInstance(results[0],asyncio.CancelledError)
        self.assertEqual(results[1].text,"answer")
        self.assertEqual(calls,["first","second"])

    async def test_cancelled_tool_request_starts_no_continuation_or_auth_retry(self):
        for auth_failure in (False, True):
            with self.subTest(auth_failure=auth_failure):
                entered, release = threading.Event(), threading.Event()
                calls = []
                def chat(prompt, *args):
                    calls.append(prompt)
                    if prompt == "first":
                        entered.set()
                        release.wait(timeout=2)
                        if auth_failure:
                            raise auth_error()
                        return Reply('```tool_calls\n[{"name":"write","arguments":{', "fake:2", "length")
                    return Reply("answer", "fake:4")
                fake = Mock()
                fake.chat.side_effect = chat
                req = ChatCompletionRequest(messages=[ChatMessage(role="user", content="audit")], tools=TOOLS)
                with patch.object(api, "get_client", return_value=fake) as factory:
                    task = asyncio.create_task(api._run_tool_chat("first", req, "default"))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    task.cancel()
                    await asyncio.sleep(.01)
                    self.assertTrue(api._request_gate.locked())
                    release.set()
                    result = (await asyncio.gather(task, return_exceptions=True))[0]
                    self.assertIsInstance(result, asyncio.CancelledError)
                    self.assertEqual(calls, ["first"])
                    self.assertEqual(factory.call_count, 1)
                    reply = await api._run_chat_with_retry("second", req, "default")
                    self.assertEqual(reply.text, "answer")
                    self.assertEqual(calls, ["first", "second"])

    async def test_stream_initialization_waiters_do_not_exhaust_the_worker_pool(self):
        release = threading.Event()
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_limit = limiter.total_tokens
        limiter.total_tokens = 2
        class Client:
            def stream(self, *args, **kwargs):
                class Stream(TextStream):
                    def __iter__(self):
                        yield "first"
                        yield "second"
                return Stream()
        fake = Client()
        def refresh():
            if not release.wait(timeout=2):
                raise RuntimeError("fixture refresh timed out")
            return fake
        req = ChatCompletionRequest(messages=[ChatMessage(role="user", content="audit")], stream=True)
        generator = api._plain_stream(fake, "audit", req, "default")
        waiters = []
        try:
            await generator.__anext__()
            self.assertIn("first", await generator.__anext__())
            with patch.object(api, "get_client", side_effect=refresh):
                waiters = [asyncio.create_task(api.chat_completions(req)) for _ in range(4)]
                await asyncio.sleep(.03)
                frame = await asyncio.wait_for(generator.__anext__(), .5)
                self.assertIn("second", frame)
                release.set()
                await generator.aclose()
                await asyncio.gather(*waiters)
        finally:
            release.set()
            await generator.aclose()
            await asyncio.gather(*waiters, return_exceptions=True)
            limiter.total_tokens = original_limit

    async def test_cancelled_stream_waits_for_active_next_before_closing(self):
        entered, release = threading.Event(), threading.Event()
        closed = threading.Event()
        class Stream(TextStream):
            def __iter__(self):
                try:
                    entered.set()
                    release.wait(timeout=2)
                    yield "answer"
                finally:
                    closed.set()
        fake = Mock()
        fake.stream.return_value = Stream()
        req = ChatCompletionRequest(messages=[ChatMessage(role="user",content="audit")])
        generator = api._plain_stream(fake,"first",req,"default")
        first = asyncio.create_task(generator.__anext__())
        self.assertTrue(await asyncio.to_thread(entered.wait,1))
        first.cancel()
        try:
            await asyncio.sleep(0.02)
            self.assertTrue(api._request_gate.locked())
        finally:
            release.set()
        result = (await asyncio.gather(first,return_exceptions=True))[0]
        self.assertIsInstance(result,asyncio.CancelledError)
        self.assertTrue(closed.is_set())
        self.assertFalse(api._request_gate.locked())

    async def test_tool_continuations_keep_the_same_queue_slot(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def chat(prompt,*args):
            calls.append(prompt)
            if prompt == "first":
                entered.set()
                release.wait(timeout=2)
                return Reply('```tool_calls\n[{"name":"write","arguments":{',"fake:2","length")
            if prompt == api._CONTINUE_PROMPT:
                return Reply('}}]\n```',"fake:4")
            return Reply("answer","other:2")
        fake = Mock()
        fake.chat.side_effect = chat
        req = ChatCompletionRequest(messages=[ChatMessage(role="user",content="audit")],tools=TOOLS)
        with patch.object(api,"get_client",return_value=fake), contextlib.redirect_stdout(io.StringIO()):
            first = asyncio.create_task(api._run_tool_chat("first",req,"default"))
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            second = asyncio.create_task(api._run_chat_with_retry("second",req,"default"))
            try:
                await asyncio.sleep(0.02)
            finally:
                release.set()
            result, _ = await asyncio.gather(first,second)
        self.assertEqual(calls,["first",api._CONTINUE_PROMPT,"second"])
        self.assertEqual(result[0].conversation_id,"fake:4")

    async def test_repeated_cancellation_preserves_queue_when_worker_fails(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def chat(prompt,*args):
            calls.append(prompt)
            if prompt == "first":
                entered.set()
                release.wait(timeout=2)
                raise RuntimeError("network down after cancellation")
            return Reply("answer","fake:2")
        fake = Mock()
        fake.chat.side_effect = chat
        req = ChatCompletionRequest(messages=[ChatMessage(role="user",content="audit")])
        with patch.object(api,"get_client",return_value=fake):
            first = asyncio.create_task(api._run_chat_with_retry("first",req,"default"))
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            first.cancel()
            await asyncio.sleep(0.01)
            first.cancel()
            second = asyncio.create_task(api._run_chat_with_retry("second",req,"default"))
            try:
                await asyncio.sleep(0.01)
                self.assertEqual(calls,["first"])
            finally:
                release.set()
            results = await asyncio.gather(first,second,return_exceptions=True)
        self.assertIsInstance(results[0],asyncio.CancelledError)
        self.assertEqual(results[1].text,"answer")


class RefreshAndStartupTests(unittest.TestCase):
    def test_background_and_request_refresh_share_client_lock(self):
        state = {"active":0,"maximum":0}
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        failures = []
        def build(*args,**kwargs):
            with lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"],state["active"])
            time.sleep(0.02)
            with lock:
                state["active"] -= 1
            return Mock()
        class Stop:
            waits = 0
            def wait(self,*args):
                self.waits += 1
                return self.waits > 1
        def run(callback):
            try:
                barrier.wait(timeout=2)
                callback()
            except Exception as exc:
                failures.append(exc)
        with patch.object(api,"_build_client",side_effect=build) as builder, patch.object(api,"_client",None):
            with contextlib.redirect_stdout(io.StringIO()):
                threads = [threading.Thread(target=run,args=(lambda:api._refresh_loop(Stop()),)),
                           threading.Thread(target=run,args=(lambda:api.get_client(True),))]
                for thread in threads:
                    thread.start()
                barrier.wait(timeout=2)
                for thread in threads:
                    thread.join(timeout=3)
            self.assertTrue(any(call.kwargs.get("allow_interactive") is False for call in builder.call_args_list))
        self.assertFalse(failures)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(state["maximum"],1)

    def test_rejected_stale_client_reuses_already_refreshed_client(self):
        old, current = Mock(),Mock()
        with patch.object(api,"_client",current), patch.object(api,"_build_client") as build:
            self.assertIs(api.get_client(True,rejected_client=old),current)
            build.assert_not_called()

    def test_default_launcher_does_not_trust_forwarded_headers(self):
        with patch("uvicorn.run") as run:
            runpy.run_path(str(api.__file__).replace("server/api.py","app.py"),run_name="__main__")
        self.assertIs(run.call_args.kwargs["proxy_headers"],False)
