import base64
import json
import os
import subprocess
import sys
import unittest

from providers.browser_protocol import claude_answer, claude_conversation_path
from providers.common import ProviderUnavailable
from providers.conversations import encode, decode
from providers.tab_bridge import BROWSER_PROVIDERS, parse_browser_result

CHAT = "00000000-0000-4000-8000-0000000000aa"


def sse(*objects, done=True):
    lines = []
    for obj in objects:
        lines += ["data: " + json.dumps(obj), ""]
    if done:
        lines += ["data: [DONE]", ""]
    return "\n".join(lines).encode()


class ClaudeBridgeTests(unittest.TestCase):
    def test_claude_is_a_browser_provider(self):
        self.assertIn("claude", BROWSER_PROVIDERS)
        token = encode("claude", "default", "/chat/" + CHAT)
        self.assertEqual(decode(token, "claude"), ("default", "/chat/" + CHAT))

    def test_claude_answer_reads_conversation_json_leaf(self):
        body = json.dumps({
            "uuid": CHAT,
            "current_leaf_message_uuid": "asst-1",
            "chat_messages": [
                {
                    "uuid": "user-1",
                    "sender": "human",
                    "text": "hello",
                    "parent_message_uuid": None,
                },
                {
                    "uuid": "asst-1",
                    "sender": "assistant",
                    "text": "hi there",
                    "parent_message_uuid": "user-1",
                    "content": [{"type": "text", "text": "hi there"}],
                },
            ],
        }).encode()
        self.assertEqual(claude_answer(body), "hi there")
        self.assertEqual(claude_conversation_path(body, "/"), "/chat/" + CHAT)

    def test_claude_answer_reads_dom_synthesized_sse(self):
        body = sse(
            {"type": "message.delta", "text": "from dom"},
            {"type": "message.completed"},
        )
        self.assertEqual(claude_answer(body), "from dom")

    def test_claude_answer_reads_consecutive_data_lines_without_blank_records(self):
        # Claude.ai often omits the blank line between SSE records; sse_events
        # would join those JSON objects and fail to parse.
        body = (
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hel"}}\n'
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"lo"}}\n'
            'data: {"type":"message_stop"}\n'
        ).encode()
        self.assertEqual(claude_answer(body), "Hello")

    def test_claude_answer_reads_anthropic_event_named_sse(self):
        body = (
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n'
            "\n"
            "event: message_stop\n"
            'data: {"type":"message_stop"}\n'
            "\n"
        ).encode()
        self.assertEqual(claude_answer(body), "ok")

    def test_claude_answer_rejects_error_envelope(self):
        body = json.dumps({"error": {"message": "denied"}}).encode()
        with self.assertRaisesRegex(ProviderUnavailable, "rejected"):
            claude_answer(body)

    def test_parse_browser_result_accepts_claude_conversation_json(self):
        body = json.dumps({
            "uuid": CHAT,
            "current_leaf_message_uuid": "asst-1",
            "chat_messages": [
                {"uuid": "asst-1", "sender": "assistant", "text": "ok",
                 "content": [{"type": "text", "text": "ok"}]},
            ],
        }).encode()
        result = {
            "status": 200,
            "path": "/chat/" + CHAT,
            "body": base64.b64encode(body).decode(),
        }
        reply = parse_browser_result("claude", result, None, "prompt")
        self.assertEqual(reply.text, "ok")
        self.assertEqual(decode(reply.conversation_id, "claude"), ("default", "/chat/" + CHAT))

    def test_browser_only_advertises_claude_web(self):
        env = {
            **os.environ,
            "DEEPSEEK_ENABLED": "0",
            "QWEN_ENABLED": "0",
            "GROK_ENABLED": "0",
            "MISTRAL_ENABLED": "0",
            "KIMI_ENABLED": "0",
            "GLM_ENABLED": "0",
            "GEMINI_ENABLED": "0",
            "CHATGPT_ENABLED": "0",
            "CLAUDE_ENABLED": "1",
            "BROWSER_BRIDGE_ENABLED": "1",
        }
        script = (
            "from server.config import MODEL_MAP, model_provider, provider_uses_browser; "
            "assert MODEL_MAP == {'claude-web': 'default'}; "
            "assert model_provider('claude-web') == 'claude'; "
            "assert provider_uses_browser('claude')"
        )
        subprocess.run([sys.executable, "-c", script], env=env, check=True)


if __name__ == "__main__":
    unittest.main()
