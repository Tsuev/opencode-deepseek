import base64
import json
import os
import subprocess
import sys
import unittest

from providers.browser_protocol import (
    gemini_answer, gemini_conversation_path, gemini_paths_compatible,
)
from providers.common import ProviderUnavailable
from providers.conversations import encode, decode
from providers.tab_bridge import BROWSER_PROVIDERS, parse_browser_result

CONV = "c_abc123def456"
MESSAGE = "00000000-0000-4000-8000-000000000002"


def stream_generate(text, conversation_id=CONV):
    payload = [
        None,
        [conversation_id, "r_response"],
        None,
        None,
        [[["cid"], [text]]],
    ]
    wrb = ["wrb.fr", None, json.dumps(payload)]
    body = ")]}'\n" + json.dumps([wrb]) + "\n" + '[["e",4,null,null]]\n'
    return body.encode()


class GeminiBridgeTests(unittest.TestCase):
    def test_gemini_is_a_browser_provider(self):
        self.assertIn("gemini", BROWSER_PROVIDERS)
        token = encode("gemini", "default", "/app/" + CONV)
        self.assertEqual(decode(token, "gemini"), ("default", "/app/" + CONV))
        cli = encode("gemini", "flash", MESSAGE)
        self.assertEqual(decode(cli, "gemini"), ("flash", MESSAGE))

    def test_gemini_answer_reads_final_wrb_text(self):
        self.assertEqual(gemini_answer(stream_generate("hello world")), "hello world")

    def test_gemini_answer_strips_internal_code_scaffolding(self):
        raw = (
            "```python?code_reference&code_event_index=1\nprint(1)\n```\n"
            "```text?code_stdout&code_event_index=1\n1\n```\n"
            "final answer"
        )
        self.assertEqual(gemini_answer(stream_generate(raw)), "final answer")

    def test_gemini_answer_rejects_bard_error(self):
        with self.assertRaisesRegex(ProviderUnavailable, "rejected"):
            gemini_answer(b")]}'\nBardErrorInfo [3]\n")

    def test_gemini_conversation_path_normalizes_account_prefix(self):
        body = stream_generate("ok")
        self.assertEqual(
            gemini_conversation_path(body, "/u/0/app/" + CONV),
            "/app/" + CONV,
        )

    def test_gemini_conversation_path_recovers_from_stream(self):
        body = stream_generate("ok", conversation_id=CONV)
        self.assertEqual(gemini_conversation_path(body, "/app"), "/app/" + CONV)

    def test_gemini_answer_reads_ai_mode_html_fragments(self):
        body = b"<div class='response'>Hello <b>AI Mode</b></div>"
        self.assertEqual(gemini_answer(body), "Hello **AI Mode**")

    def test_gemini_answer_reads_unicode_escaped_folif_html(self):
        body = (
            ")]}'\n"
            "\\u003cdiv class=\"n6owBd\"\\u003eHello escaped AI Mode\\u003c/div\\u003e"
        ).encode()
        self.assertEqual(gemini_answer(body), "Hello escaped AI Mode")

    def test_gemini_answer_reads_plain_dom_fallback_text(self):
        self.assertEqual(gemini_answer(b"Visible DOM answer"), "Visible DOM answer")

    def test_gemini_answer_strips_prompt_rewrite_from_ai_mode_html(self):
        prompt = (
            "System: rules\n\nYou can call functions (tools). Available tools, given as JSON Schema:\n"
            '[{"name":"read"}]\n\n```tool_calls\n[{"name":"read"}]\n```\n\nUser: hi'
        )
        body = (
            "<div class='q'>" + prompt + "</div>"
            "<div class='response'>Use the read tool on README.md</div>"
        ).encode()
        self.assertEqual(
            gemini_answer(body, prompt),
            "Use the read tool on README.md",
        )

    def test_gemini_conversation_path_uses_aimode_placeholder_on_search(self):
        body = b"<div class='response'>ok</div>"
        self.assertEqual(gemini_conversation_path(body, "/search"), "/app/aimode")

    def test_gemini_paths_compatible_equates_aimode_and_search(self):
        self.assertTrue(gemini_paths_compatible("/app/aimode", "/search"))
        self.assertTrue(gemini_paths_compatible("/search?udm=50", "/app/aimode"))
        self.assertFalse(gemini_paths_compatible("/app/aimode", "/app/" + CONV))

    def test_parse_browser_result_accepts_aimode_resume_with_search_path(self):
        body = b"Second turn answer with **bold**"
        result = {
            "status": 200,
            "path": "/search",
            "body": base64.b64encode(body).decode(),
        }
        reply = parse_browser_result("gemini", result, "/app/aimode", "follow up")
        self.assertEqual(reply.text, "Second turn answer with **bold**")
        self.assertEqual(decode(reply.conversation_id, "gemini"), ("default", "/app/aimode"))

    def test_gemini_answer_keeps_otqkpb_section_headings(self):
        body = (
            b'<div class="otQkpb" role="heading" aria-level="2">'
            b'<span aria-hidden="true">1.</span> Introduction</div>'
            b'<div class="n6owBd">Body text</div>'
        )
        text = gemini_answer(body)
        self.assertIn("Introduction", text)
        self.assertIn("Body text", text)
        self.assertTrue(
            text.startswith("## ") or "1." in text,
            msg=text,
        )

    def test_gemini_answer_keeps_zlzg0d_headings_and_code_blocks(self):
        body = (
            '<h3 class="MheKwc">rewrite of the user prompt</h3>'
            '<div data-container-id="main-col" class="mZJni">'
            '<div class="ZLzG0d" role="heading" aria-level="3">'
            '<span aria-hidden="true">1.</span> Способ первый</div>'
            '<div class="n6owBd">Шаг с цитатой'
            '<div class="DHPVt">1</div></div>'
            '<div class="aiModeUiCodeBlock__Container BpxUDf">'
            '<div class="GlO4G">python</div>'
            '<pre><code>print("hi")\n</code></pre></div>'
            '<div class="ZLzG0d" role="heading" aria-level="3">'
            '<span aria-hidden="true">2.</span> Способ второй</div>'
            '<div class="n6owBd">Ещё абзац</div>'
            '<div class="n6owBd">В ответах искусственного интеллекта могут быть ошибки. '
            'Подробнее</div></div>'
        ).encode("utf-8")
        text = gemini_answer(body)
        self.assertNotIn("rewrite of the user prompt", text)
        self.assertNotIn("В ответах искусственного интеллекта могут быть ошибки", text)
        self.assertIn("### 1. Способ первый", text)
        self.assertIn("### 2. Способ второй", text)
        self.assertIn("Шаг с цитатой", text)
        self.assertIn("```python", text)
        self.assertIn('print("hi")', text)
        self.assertIn("Ещё абзац", text)
        # Blank-line separation around headings and fenced code.
        self.assertRegex(text, r"### 1\. Способ первый\n\nШаг с цитатой")
        self.assertRegex(text, r"Шаг с цитатой\n\n```python\n")
        self.assertRegex(text, r"```\n\n### 2\. Способ второй")

    def test_gemini_dom_scrape_keeps_newlines_after_prompt_filter(self):
        body = (
            "### Heading\n\n"
            "Paragraph one.\n\n"
            "```python\nprint(1)\n```\n\n"
            "Paragraph two.\n\n"
            "В ответах искусственного интеллекта могут быть ошибки. Learn more"
        ).encode("utf-8")
        text = gemini_answer(body, "unrelated prompt")
        self.assertEqual(
            text,
            "### Heading\n\nParagraph one.\n\n```python\nprint(1)\n```\n\nParagraph two.",
        )

    def test_parse_browser_result_accepts_gemini_stream(self):
        body = stream_generate("ok")
        result = {
            "status": 200,
            "path": "/app/" + CONV,
            "body": base64.b64encode(body).decode(),
        }
        reply = parse_browser_result("gemini", result, None, "prompt")
        self.assertEqual(reply.text, "ok")
        self.assertEqual(decode(reply.conversation_id, "gemini"), ("default", "/app/" + CONV))

    def test_parse_browser_result_rejects_echoed_opencode_prompt(self):
        prompt = (
            "System: rules\n\nYou can call functions (tools). Available tools, given as JSON Schema:\n"
            '[{"name":"read"}]\n\n```tool_calls\n[{"name":"read"}]\n```\n\nUser: hi'
        )
        body = stream_generate(prompt)
        result = {
            "status": 200,
            "path": "/app/" + CONV,
            "body": base64.b64encode(body).decode(),
        }
        with self.assertRaisesRegex(ProviderUnavailable, "request prompt instead of an answer"):
            parse_browser_result("gemini", result, None, prompt)

    def test_browser_transport_advertises_gemini_web_only(self):
        env = {
            **os.environ,
            "DEEPSEEK_ENABLED": "0",
            "QWEN_ENABLED": "0",
            "GROK_ENABLED": "0",
            "MISTRAL_ENABLED": "0",
            "KIMI_ENABLED": "0",
            "GLM_ENABLED": "0",
            "CHATGPT_ENABLED": "0",
            "CLAUDE_ENABLED": "0",
            "GEMINI_ENABLED": "1",
            "GEMINI_TRANSPORT": "browser",
            "GEMINI_MODELS": "{}",
            "BROWSER_BRIDGE_ENABLED": "1",
        }
        script = (
            "from server.config import MODEL_MAP, model_provider, gemini_uses_browser; "
            "assert gemini_uses_browser(); "
            "assert MODEL_MAP == {'gemini-web': 'default'}; "
            "assert model_provider('gemini-web') == 'gemini'"
        )
        subprocess.run([sys.executable, "-c", script], env=env, check=True)

    def test_cli_transport_keeps_model_map(self):
        env = {
            **os.environ,
            "DEEPSEEK_ENABLED": "0",
            "QWEN_ENABLED": "0",
            "GROK_ENABLED": "0",
            "MISTRAL_ENABLED": "0",
            "KIMI_ENABLED": "0",
            "GLM_ENABLED": "0",
            "CHATGPT_ENABLED": "0",
            "CLAUDE_ENABLED": "0",
            "GEMINI_ENABLED": "1",
            "GEMINI_TRANSPORT": "cli",
            "GEMINI_MODELS": json.dumps({"gemini-flash": "flash"}),
            "BROWSER_BRIDGE_ENABLED": "0",
        }
        script = (
            "from server.config import MODEL_MAP, gemini_uses_browser; "
            "from providers.registry import build_client; "
            "assert not gemini_uses_browser(); "
            "assert MODEL_MAP == {'gemini-flash': 'flash'}; "
            "client = build_client('gemini', lambda: None); "
            "assert type(client).__name__ == 'AntigravityClient'"
        )
        subprocess.run([sys.executable, "-c", script], env=env, check=True)


if __name__ == "__main__":
    unittest.main()
