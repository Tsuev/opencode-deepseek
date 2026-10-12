"""Grok / Mistral / GLM / ChatGPT / Claude are browser-only (no transport env flag)."""

import json
import os
import subprocess
import sys
import unittest


class BrowserOnlyProviderTests(unittest.TestCase):
    def test_enabled_browser_providers_advertise_web_models(self):
        env = {
            **os.environ,
            "DEEPSEEK_ENABLED": "0",
            "QWEN_ENABLED": "0",
            "KIMI_ENABLED": "0",
            "GEMINI_ENABLED": "0",
            "BROWSER_BRIDGE_ENABLED": "1",
            "GROK_ENABLED": "1",
            "MISTRAL_ENABLED": "1",
            "GLM_ENABLED": "1",
            "CHATGPT_ENABLED": "1",
            "CLAUDE_ENABLED": "1",
        }
        script = (
            "import json;"
            "from server.config import MODEL_MAP, model_provider;"
            "from providers.registry import build_client;"
            "from providers.tab_bridge import TabClient;"
            "rows = {k: model_provider(k) for k in MODEL_MAP};"
            "assert rows == {"
            "'chatgpt-web': 'chatgpt', 'claude-web': 'claude', 'grok-web': 'grok', "
            "'mistral-web': 'mistral', 'glm-web': 'glm'};"
            "assert all(isinstance(build_client(p, lambda: None), TabClient) for p in rows.values());"
            "print(json.dumps(sorted(rows)))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True,
        )
        self.assertEqual(
            json.loads(result.stdout.strip()),
            ["chatgpt-web", "claude-web", "glm-web", "grok-web", "mistral-web"],
        )


if __name__ == "__main__":
    unittest.main()
