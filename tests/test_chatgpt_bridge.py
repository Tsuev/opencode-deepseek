import base64
import json
import os
import subprocess
import sys
import unittest

from providers.browser_protocol import chatgpt_answer, chatgpt_conversation_path
from providers.common import ProviderUnavailable
from providers.conversations import encode, decode
from providers.tab_bridge import BROWSER_PROVIDERS, parse_browser_result

CHAT = "00000000-0000-4000-8000-0000000000aa"
MESSAGE = "00000000-0000-4000-8000-0000000000bb"


def sse(*objects, done=True):
    lines = []
    for obj in objects:
        lines += ["data: " + json.dumps(obj), ""]
    if done:
        lines += ["data: [DONE]", ""]
    return "\n".join(lines).encode()


def assistant(text, status="finished_successfully", msg_id=MESSAGE):
    return {
        "conversation_id": CHAT,
        "message": {
            "id": msg_id,
            "author": {"role": "assistant"},
            "recipient": "all",
            "status": status,
            "content": {"content_type": "text", "parts": [text]},
        },
    }


class ChatGPTBridgeTests(unittest.TestCase):
    def test_chatgpt_is_a_browser_provider(self):
        self.assertIn("chatgpt", BROWSER_PROVIDERS)
        token = encode("chatgpt", "default", "/c/" + CHAT)
        self.assertEqual(decode(token, "chatgpt"), ("default", "/c/" + CHAT))

    def test_chatgpt_answer_reads_finished_message_frames(self):
        body = sse(
            assistant("hel", status="in_progress"),
            assistant("hello"),
        )
        self.assertEqual(chatgpt_answer(body), "hello")

    def test_chatgpt_answer_keeps_patch_appends_after_the_first_flush(self):
        body = sse(
            {"v": "В 1С массив можно отс"},
            {"o": "patch", "v": [
                {"p": "/message/content/parts/0", "o": "append", "v": "ортировать"},
                {"p": "/message/status", "o": "replace", "v": "finished_successfully"},
            ]},
        )
        self.assertEqual(chatgpt_answer(body), "В 1С массив можно отсортировать")

    def test_chatgpt_answer_applies_a_bare_patch_list(self):
        body = sse(
            {"v": "В 1С у `СписокЗначений"},
            {"v": [
                {"p": "/message/content/parts/0", "o": "append", "v": ".Сортировать()`"},
            ]},
            {"p": "/message/status", "o": "replace", "v": "finished_successfully"},
        )
        self.assertEqual(chatgpt_answer(body), "В 1С у `СписокЗначений.Сортировать()`")

    def test_chatgpt_answer_does_not_let_a_short_snapshot_replace_the_tail(self):
        body = sse(
            {"v": "начало"},
            {"p": "/message/content/parts/0", "o": "append", "v": " и хвост"},
            {"v": {"message": {
                "id": MESSAGE,
                "author": {"role": "assistant"},
                "recipient": "all",
                "status": "finished_successfully",
                "content": {"content_type": "text", "parts": ["начало"]},
            }}},
        )
        self.assertEqual(chatgpt_answer(body), "начало и хвост")

    def test_chatgpt_answer_reads_v_patch_string_deltas(self):
        body = sse(
            {"conversation_id": CHAT, "v": "hello"},
            {"v": " world"},
            {
                "v": {
                    "message": {
                        "id": MESSAGE,
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "status": "finished_successfully",
                        "content": {"content_type": "text", "parts": ["hello world"]},
                    }
                }
            },
        )
        self.assertEqual(chatgpt_answer(body), "hello world")

    def test_chatgpt_answer_rejects_json_error_envelope(self):
        body = json.dumps({"detail": "Something went wrong"}).encode()
        with self.assertRaisesRegex(ProviderUnavailable, "rejected"):
            chatgpt_answer(body)

    def test_chatgpt_conversation_path_recovers_from_sse(self):
        body = sse(assistant("ok"))
        self.assertEqual(chatgpt_conversation_path(body, "/"), "/c/" + CHAT)
        self.assertEqual(chatgpt_conversation_path(body, "/c/" + CHAT), "/c/" + CHAT)

    def test_parse_browser_result_accepts_chatgpt_sse(self):
        body = sse(assistant("ok"))
        result = {
            "status": 200,
            "path": "/",
            "body": base64.b64encode(body).decode(),
        }
        reply = parse_browser_result("chatgpt", result, None, "prompt")
        self.assertEqual(reply.text, "ok")
        self.assertEqual(decode(reply.conversation_id, "chatgpt"), ("default", "/c/" + CHAT))

    def test_browser_only_advertises_chatgpt_web(self):
        env = {
            **os.environ,
            "DEEPSEEK_ENABLED": "0",
            "QWEN_ENABLED": "0",
            "GROK_ENABLED": "0",
            "MISTRAL_ENABLED": "0",
            "KIMI_ENABLED": "0",
            "GLM_ENABLED": "0",
            "GEMINI_ENABLED": "0",
            "CLAUDE_ENABLED": "0",
            "CHATGPT_ENABLED": "1",
            "BROWSER_BRIDGE_ENABLED": "1",
        }
        script = (
            "from server.config import MODEL_MAP, model_provider, provider_uses_browser; "
            "assert MODEL_MAP == {'chatgpt-web': 'default'}; "
            "assert model_provider('chatgpt-web') == 'chatgpt'; "
            "assert provider_uses_browser('chatgpt')"
        )
        subprocess.run([sys.executable, "-c", script], env=env, check=True)


if __name__ == "__main__":
    unittest.main()
