import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import httpx

from chat_protocol import Reply
from server import api
from server.config import QWEN_MODEL_MAP
from server.schemas import ChatCompletionRequest, ChatMessage
from qwen.client import QwenClient
from tests.test_qwen import CHAT, MESSAGE, answer, events, session
from tests.test_tools import block
from tests.support import isolated_access

CID = f"qwen:qwen3.8-max:{CHAT}:{MESSAGE}"


def qwen_with_sse(lines):
    def handler(request):
        if request.url.path == "/api/v2/chats/new":
            return httpx.Response(200, json={"success": True, "data": {"id": CHAT}})
        return httpx.Response(200, text="\n".join(lines), headers={"content-type": "text/event-stream"})
    return QwenClient(session(), access_guard=None, transport=httpx.MockTransport(handler))


class QwenApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        isolated_access(self)
        api._request_gate = asyncio.Lock()
        api._qwen_request_gate = asyncio.Lock()
        self.enabled = patch.dict(api.MODEL_MAP, QWEN_MODEL_MAP)
        self.enabled.start()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app, client=(self.id(), 123)),
                                      base_url="http://test.local")
        self.body = {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "test"}]}

    async def asyncTearDown(self):
        await self.http.aclose()
        self.enabled.stop()

    async def test_models_have_correct_provider_ownership(self):
        response = await self.http.get("/v1/models")
        models = {m["id"]: m["owned_by"] for m in response.json()["data"]}
        self.assertEqual(models["qwen3.8-omni-flash"], "qwen")
        self.assertEqual(models["qwen3.8-max"], "qwen")
        self.assertEqual(models["deepseek-chat"], "deepseek")

    async def test_qwen_requests_do_not_use_deepseek_credentials(self):
        fake = Mock()
        fake.chat.return_value = Reply("QWEN_OK", CID)
        with patch.object(api, "get_qwen_client", return_value=fake), patch.object(api, "get_client") as ds:
            response = await self.http.post("/v1/chat/completions", json=self.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "QWEN_OK")
        ds.assert_not_called()
        self.assertEqual(fake.chat.call_args.args[2], "qwen3.8-max")

    async def test_foreign_conversation_is_rejected_before_any_client_access(self):
        with patch.object(api, "get_qwen_client") as qwen, patch.object(api, "get_client") as ds:
            for model, cid in (("qwen3.8-max", "deepseek-thread:2"), ("deepseek-chat", CID)):
                response = await self.http.post("/v1/chat/completions", json={**self.body, "model": model, "conversation_id": cid})
                self.assertEqual(response.status_code, 400)
        qwen.assert_not_called()
        ds.assert_not_called()

    async def test_qwen_uses_the_same_strict_tool_protocol(self):
        fake = Mock()
        fake.chat.return_value = Reply(block([{"name": "read", "arguments": {"filePath": "fixture.txt"}}]), CID)
        tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
        with patch.object(api, "get_qwen_client", return_value=fake):
            response = await self.http.post("/v1/chat/completions", json={**self.body, "tools": tools, "tool_choice": "required"})
        self.assertEqual(response.status_code, 200)
        call = response.json()["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "read")

    async def test_qwen_auth_rejection_never_refreshes_or_replays(self):
        old, new = Mock(), Mock()
        request = httpx.Request("POST", "https://chat.qwen.ai/api/v2/chat/completions")
        old.chat.side_effect = httpx.HTTPStatusError("token expired", request=request, response=httpx.Response(401, request=request))
        new.chat.return_value = Reply("refreshed", CID)
        with patch.object(api, "get_qwen_client", side_effect=[old, new]) as qwen, patch.object(api, "get_client") as ds, contextlib.redirect_stdout(io.StringIO()):
            response = await self.http.post("/v1/chat/completions", json=self.body)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(qwen.call_count, 1)
        self.assertEqual(new.chat.call_count, 0)
        ds.assert_not_called()

    async def test_named_sse_rejections_are_terminal_in_both_response_modes(self):
        payloads = [
            {"error": {"code": "invalid_token", "message": "Access rejected"}},
            {"error": {"code": 401, "message": "Access rejected"}},
            {"error": {"code": "403", "message": "Access rejected"}},
            {"code": "invalid_token", "message": "Access rejected"},
            {"success": False, "data": {"code": 401, "message": "Access rejected"}},
        ]
        for payload in payloads:
            for streamed in (False, True):
                with self.subTest(payload=payload, stream=streamed):
                    api.guard.resume("qwen")
                    old = qwen_with_sse(["event: error", "data: " + json.dumps(payload), ""])
                    new = qwen_with_sse(events(answer("refreshed", status="finished")))
                    try:
                        with patch.object(api, "get_qwen_client", side_effect=[old, new]) as qwen, patch.object(api, "get_client") as ds, contextlib.redirect_stdout(io.StringIO()):
                            response = await self.http.post("/v1/chat/completions", json={**self.body, "stream": streamed})
                        if streamed:
                            self.assertEqual(response.status_code, 200)
                            chunks = [json.loads(line[6:]) for line in response.text.splitlines()
                                      if line.startswith("data: ") and line != "data: [DONE]"]
                            self.assertTrue(all("choices" not in chunk for chunk in chunks))
                            error = chunks[-1]["error"]
                        else:
                            self.assertEqual(response.status_code, 403)
                            error = response.json()["error"]
                        expected = "access_denied" if str(payload.get("error", {}).get("code")) == "403" else "session_expired"
                        self.assertEqual(error["code"], expected)
                        self.assertFalse(error["retryable"])
                        self.assertEqual(qwen.call_count, 1)
                        ds.assert_not_called()
                    finally:
                        old.close()
                        new.close()

    async def test_named_sse_non_auth_error_does_not_refresh(self):
        payload = {"error": {"code": "quota_exceeded", "message": "Capacity exceeded"}}
        old = qwen_with_sse(["event: error", "data: " + json.dumps(payload), ""])
        try:
            with patch.object(api, "get_qwen_client", return_value=old) as qwen:
                response = await self.http.post("/v1/chat/completions", json=self.body)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["error"]["code"], "quota_exceeded")
            self.assertEqual(qwen.call_count, 1)
        finally:
            old.close()

    async def test_waiting_qwen_request_does_not_block_deepseek(self):
        started, release = threading.Event(), threading.Event()
        qwen, ds = Mock(), Mock()
        def wait(*args):
            started.set()
            if not release.wait(3):
                raise RuntimeError("fixture wait timed out")
            return Reply("qwen", CID)
        qwen.chat.side_effect = wait
        ds.chat.return_value = Reply("deepseek", "ds:2")
        qwen_req = ChatCompletionRequest(model="qwen3.8-max", messages=[ChatMessage(role="user", content="test")])
        ds_req = qwen_req.model_copy(update={"model": "deepseek-chat"})
        with patch.object(api, "get_qwen_client", return_value=qwen), patch.object(api, "get_client", return_value=ds):
            task = asyncio.create_task(api._run_chat_with_retry("test", qwen_req, "qwen3.8-max"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                reply = await asyncio.wait_for(api._run_chat_with_retry("test", ds_req, "default"), 1)
                self.assertEqual(reply.text, "deepseek")
            finally:
                release.set()
                await task


class QwenClientCacheTests(unittest.TestCase):
    def test_qwen_model_registration_is_opt_in(self):
        for enabled in ("0", "1"):
            with self.subTest(enabled=enabled):
                result = subprocess.run(
                    [sys.executable, "-c", "import json; from server.config import MODEL_MAP; print(json.dumps(list(MODEL_MAP)))"],
                    env=dict(os.environ, QWEN_ENABLED=enabled, DEEPSEEK_ENABLED="0"), check=True, capture_output=True, text=True,
                )
                models = json.loads(result.stdout)
                self.assertNotIn("deepseek-chat", models)
                for model in QWEN_MODEL_MAP:
                    self.assertEqual(model in models, enabled == "1")

    def test_expiry_rebuilds_only_the_qwen_client(self):
        old, fresh = Mock(), Mock()
        old.session.usable = False
        fresh.session.usable = True
        with patch.object(api, "_qwen_client", old), patch.object(api, "get_qwen_session") as capture, patch.object(api, "QwenClient", return_value=fresh), patch.object(api, "get_client") as ds:
            self.assertIs(api.get_qwen_client(), fresh)
        self.assertFalse(capture.call_args.kwargs["force"])
        ds.assert_not_called()

    def test_rejection_of_an_already_replaced_client_reuses_the_refresh(self):
        old, current = Mock(), Mock()
        current.session.usable = True
        with patch.object(api, "_qwen_client", current), patch.object(api, "get_qwen_session") as capture:
            self.assertIs(api.get_qwen_client(True, rejected_client=old), current)
        capture.assert_not_called()
