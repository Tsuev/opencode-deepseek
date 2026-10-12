import base64
import json
import os
import subprocess
import sys
import unittest

from providers.browser_protocol import qwen_answer
from providers.common import ProviderUnavailable
from providers.conversations import encode, decode
from providers.tab_bridge import BROWSER_PROVIDERS, parse_browser_result

MESSAGE = "00000000-0000-4000-8000-000000000002"


def sse(*objects, done=True):
    lines = []
    for obj in objects:
        lines += ["data: " + json.dumps(obj), ""]
    if done:
        lines += ["data: [DONE]", ""]
    return "\n".join(lines).encode()


def answer(content, **extra):
    return {"response_id": MESSAGE, "choices": [{
        "index": 0, "delta": {"phase": "answer", "content": content, **extra},
    }]}


class QwenBridgeTests(unittest.TestCase):
    def test_qwen_is_a_browser_provider(self):
        self.assertIn("qwen", BROWSER_PROVIDERS)
        token = encode("qwen", "default", "/c/" + MESSAGE)
        self.assertEqual(decode(token, "qwen"), ("default", "/c/" + MESSAGE))

    def test_qwen_answer_reads_finished_sse_answer_phase(self):
        body = sse(
            {"response.created": {"response_id": MESSAGE}},
            answer("hello"),
            answer(" world", status="finished"),
        )
        self.assertEqual(qwen_answer(body), "hello world")

    def test_qwen_answer_rejects_captcha_json_envelope(self):
        body = json.dumps({"data": {"url": "https://example.test/verify"}, "ret": ["FAIL_SYS_USER_VALIDATE"]}).encode()
        with self.assertRaisesRegex(ProviderUnavailable, "browser verification"):
            qwen_answer(body)

    def test_parse_browser_result_accepts_qwen_sse(self):
        body = sse(answer("ok", status="finished"))
        result = {
            "status": 200,
            "path": "/c/" + MESSAGE,
            "body": base64.b64encode(body).decode(),
        }
        reply = parse_browser_result("qwen", result, None, "prompt")
        self.assertEqual(reply.text, "ok")
        self.assertTrue(reply.conversation_id.startswith("web:qwen:"))

    def test_parse_browser_result_recovers_chat_path_from_sse_when_url_still_home(self):
        body = sse(
            {"response.created": {"response_id": MESSAGE, "chat_id": MESSAGE}},
            answer("ok", status="finished"),
        )
        result = {
            "status": 200,
            "path": "/",
            "body": base64.b64encode(body).decode(),
        }
        reply = parse_browser_result("qwen", result, None, "prompt")
        self.assertEqual(reply.text, "ok")
        self.assertEqual(decode(reply.conversation_id, "qwen"), ("default", "/c/" + MESSAGE))

    def test_parse_browser_result_accepts_path_from_request_when_sse_omits_chat_id(self):
        body = sse(answer("ok", status="finished"))
        result = {
            "status": 200,
            "path": "/c/" + MESSAGE,
            "body": base64.b64encode(body).decode(),
        }
        reply = parse_browser_result("qwen", result, None, "prompt")
        self.assertEqual(reply.text, "ok")
        self.assertEqual(decode(reply.conversation_id, "qwen"), ("default", "/c/" + MESSAGE))

    def test_parse_browser_result_surfaces_safe_tab_error(self):
        with self.assertRaisesRegex(
            ProviderUnavailable,
            r"qwen browser did not complete the request: Provider response could not be observed",
        ):
            parse_browser_result("qwen", {"error": "Provider response could not be observed"})

    def test_browser_transport_advertises_qwen_web_only(self):
        env = {
            **os.environ,
            "DEEPSEEK_ENABLED": "0",
            "QWEN_ENABLED": "1",
            "QWEN_TRANSPORT": "browser",
            "GROK_ENABLED": "0",
            "MISTRAL_ENABLED": "0",
            "KIMI_ENABLED": "0",
            "GLM_ENABLED": "0",
            "GEMINI_ENABLED": "0",
            "CHATGPT_ENABLED": "0",
            "CLAUDE_ENABLED": "0",
            "BROWSER_BRIDGE_ENABLED": "1",
        }
        script = (
            "from server.config import MODEL_MAP, model_provider, qwen_uses_browser; "
            "assert qwen_uses_browser(); "
            "assert MODEL_MAP == {'qwen-web': 'default'}; "
            "assert model_provider('qwen-web') == 'qwen'"
        )
        subprocess.run([sys.executable, "-c", script], env=env, check=True)


if __name__ == "__main__":
    unittest.main()
