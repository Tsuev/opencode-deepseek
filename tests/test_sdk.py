import json
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from openai import APIError, InternalServerError, OpenAI

from deepseek.client import Reply
from server import api
from tests.test_api import TOOLS
from tests.test_stream import mock_client, snapshot
from tests.test_tools import block


class OpenAISdkTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(api, "SESSION_REFRESH_ENABLED", False).start()
        self.transport = TestClient(api.app)
        self.transport.__enter__()
        self.addCleanup(self.transport.__exit__, None, None, None)
        self.sdk = OpenAI(api_key="offline", base_url="http://testserver/v1",
                          http_client=self.transport, max_retries=0)
        self.addCleanup(self.sdk.close)
        self.messages = [{"role": "user", "content": "audit"}]

    def test_buffered_reply_and_resume_use_sdk_extra_fields(self):
        fake = Mock()
        fake.chat.return_value = Reply("answer", "fake:2")
        with patch.object(api, "get_client", return_value=fake):
            first = self.sdk.chat.completions.create(model="deepseek-expert", messages=self.messages)
            second = self.sdk.chat.completions.create(
                model="deepseek-chat", messages=self.messages,
                extra_body={"conversation_id": first.conversation_id, "thinking": True, "search": True})
        self.assertEqual(second.choices[0].message.content, "answer")
        self.assertEqual(fake.chat.call_args_list[0].args[1:3], (None, "expert"))
        self.assertEqual(fake.chat.call_args_list[1].args[1:], ("fake:2", None, True, True))

    def test_stream_preserves_text_cid_and_length(self):
        fake = mock_client(snapshot("cut", "INCOMPLETE"))
        self.addCleanup(fake.close)
        with patch.object(api, "get_client", return_value=fake):
            with self.sdk.chat.completions.create(model="deepseek-chat", messages=self.messages,
                                                  stream=True) as stream:
                chunks = list(stream)
        self.assertEqual("".join(chunk.choices[0].delta.content or "" for chunk in chunks), "cut")
        self.assertEqual(chunks[-1].choices[0].finish_reason, "length")
        self.assertEqual(chunks[-1].conversation_id, "fake-session:2")

    def test_complete_tools_work_in_buffered_and_streamed_sdk_calls(self):
        fake = Mock()
        fake.chat.return_value = Reply(block([{"name": "write", "arguments": {"content": "audit"}}]), "fake:2")
        with patch.object(api, "get_client", return_value=fake):
            reply = self.sdk.chat.completions.create(model="deepseek-chat", messages=self.messages,
                                                     tools=TOOLS, tool_choice="required")
            with self.sdk.chat.completions.create(model="deepseek-chat", messages=self.messages,
                                                  tools=TOOLS, tool_choice="required", stream=True) as stream:
                chunks = list(stream)
        call = reply.choices[0].message.tool_calls[0]
        self.assertEqual((call.function.name, json.loads(call.function.arguments)),
                         ("write", {"content": "audit"}))
        calls = [call for chunk in chunks for call in chunk.choices[0].delta.tool_calls or []]
        self.assertEqual(calls[0].function.name, "write")
        self.assertEqual(chunks[-1].choices[0].finish_reason, "tool_calls")

    def test_upstream_eof_raises_sdk_error_after_partial_text(self):
        fake = mock_client(snapshot("partial"))
        self.addCleanup(fake.close)
        seen = []
        with patch.object(api, "get_client", return_value=fake):
            with self.sdk.chat.completions.create(model="deepseek-chat", messages=self.messages,
                                                  stream=True) as stream:
                with self.assertRaises(APIError):
                    for chunk in stream:
                        seen.append(chunk)
        self.assertEqual("".join(chunk.choices[0].delta.content or "" for chunk in seen), "partial")
        self.assertFalse(any(chunk.choices[0].finish_reason for chunk in seen))

    def test_invalid_tool_policy_is_an_sdk_http_error(self):
        fake = Mock()
        fake.chat.return_value = Reply(block([{"name": "bash", "arguments": {}}]), "fake:2")
        with patch.object(api, "get_client", return_value=fake), self.assertRaises(InternalServerError) as error:
            self.sdk.chat.completions.create(model="deepseek-chat", messages=self.messages,
                                             tools=TOOLS, tool_choice="none")
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(error.exception.body["type"], "invalid_tool_response")
