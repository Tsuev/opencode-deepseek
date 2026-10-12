from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
import warnings
from unittest.mock import patch

import httpx
from ruamel.yaml.error import ReusedAnchorWarning, YAMLError

from integrations import harness


CATALOG = {"data": [
    {"id": "qwen3.8-omni-flash", "owned_by": "qwen"},
    {"id": "glm-web", "owned_by": "glm"},
]}
BASE = "http://127.0.0.1:8000/v1"


class HarnessSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.directory = self.home / "profiles" / "desktop"
        self.directory.mkdir(parents=True)
        self.path = self.directory / "cordis.patch.yml"
        self.path.write_text("# my settings\n[]\n")
        self.manifest = self.directory / "package.json"
        self.manifest.write_text(json.dumps({"dsh": {"profile": {"bundles": [
            "@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"]}}}))
        self.models = harness.catalog_models(CATALOG)

    def test_discovery_only_reads_catalog_without_proxy_or_redirects(self):
        requests = []
        real_client = httpx.Client

        def respond(request):
            requests.append((request.method, str(request.url)))
            return httpx.Response(200, json=CATALOG)

        def client(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return real_client(transport=httpx.MockTransport(respond), **kwargs)

        with patch.object(harness.httpx, "Client", side_effect=client):
            self.assertEqual(harness.discover(BASE), self.models)
        self.assertEqual(requests, [("GET", BASE + "/models")])

    def test_remote_credentialed_and_ambiguous_endpoints_are_rejected(self):
        for url in ("https://example.com/v1", "http://localhost:8000/v1", "http://127.0.0.1.evil/v1",
                    "http://user:secret@127.0.0.1/v1", BASE + "?key=x", BASE + "#fragment",
                    "http://127.0.0.1:0/v1", "http://127.0.0.1:bad/v1", "http://127.0.0.1/v2"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                harness.local_url(url)
        self.assertEqual(harness.local_url("http://[::1]:8000/v1/"), "http://[::1]:8000/v1")

    def test_catalog_never_invents_disabled_models_and_rejects_invalid_identities(self):
        self.assertEqual([model["id"] for model in self.models], ["qwen3.8-omni-flash", "glm-web"])
        for catalog in ({"data": []}, {"data": CATALOG["data"] * 2}, {"data": [None]},
                        {"data": [{"id": "x\n- injected", "owned_by": "qwen"}]},
                        {"data": [{"id": "x", "owned_by": "unexpected"}]}):
            with self.subTest(catalog=catalog), self.assertRaises(ValueError):
                harness.catalog_models(catalog)

    def test_without_browser_filters_all_providers_and_preserves_advertised_order(self):
        rows = [{"id": provider + "-test", "owned_by": provider}
                for provider in ("glm", "deepseek", "kimi", "qwen", "grok", "gemini", "mistral", "chatgpt", "claude")]
        catalog = {"data": rows}
        self.assertEqual([model["id"] for model in harness.catalog_models(catalog)],
                         [row["id"] for row in rows])
        self.assertEqual([model["id"] for model in harness.catalog_models(catalog, without_browser=True)],
                         ["deepseek-test", "qwen-test", "gemini-test"])
        self.assertEqual(harness.catalog_models(CATALOG, without_browser=True), self.models[:1])

    def test_without_browser_validates_excluded_rows_and_duplicates(self):
        valid = CATALOG["data"][0]
        excluded = CATALOG["data"][1]
        invalid_rows = [[None], [{"id": "invalid\nmodel", "owned_by": "glm"}],
                        [{"id": "unknown", "owned_by": "unknown"}],
                        [{"id": "invalid-owner", "owned_by": []}],
                        [excluded, excluded], [{"id": valid["id"], "owned_by": "glm"}]]
        for rows in invalid_rows:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                harness.catalog_models({"data": [valid] + rows}, without_browser=True)

    def test_without_browser_keeps_only_the_explicit_direct_kimi_models(self):
        rows = [{'id': 'kimi-web', 'owned_by': 'kimi'},
                {'id': 'kimi-instant', 'owned_by': 'kimi'},
                {'id': 'kimi-k3', 'owned_by': 'kimi'},
                {'id': 'kimi-k2.8', 'owned_by': 'kimi'},
                {'id': 'unknown-kimi', 'owned_by': 'kimi'}]
        models = harness.catalog_models({'data': rows}, without_browser=True)
        self.assertEqual([m['id'] for m in models], ['kimi-instant', 'kimi-k3', 'kimi-k2.8'])
        self.assertEqual(models[0]['name'], 'Kimi Instant · web')

    def test_catalog_accepts_matching_owner_prefixed_aliases(self):
        catalog = {"data": [
            {"id": "qwen3.8-omni-flash", "owned_by": "qwen"},
            {"id": "qwen/qwen3.8-omni-flash", "owned_by": "qwen"},
            {"id": "kimi/kimi-instant", "owned_by": "kimi"},
        ]}
        models = harness.catalog_models(catalog)
        self.assertEqual([model["id"] for model in models],
                         ["qwen3.8-omni-flash", "qwen/qwen3.8-omni-flash", "kimi/kimi-instant"])
        self.assertEqual(models[1]["name"], "Qwen3.8 Omni Flash · web")
        self.assertEqual(
            [model["id"] for model in harness.catalog_models(catalog, without_browser=True)],
            ["qwen3.8-omni-flash", "qwen/qwen3.8-omni-flash", "kimi/kimi-instant"],
        )
        with self.assertRaises(ValueError):
            harness.catalog_models({"data": [{"id": "evil/qwen3.8-omni-flash", "owned_by": "qwen"}]})

    def test_without_browser_excludes_web_aliases_too(self):
        rows = [{"id": "deepseek/deepseek-chat", "owned_by": "deepseek"}]
        for model in harness.BRIDGE_ONLY_MODELS:
            owner = model.removesuffix("-web")
            rows.extend({"id": value, "owned_by": owner}
                        for value in (model, owner + "/" + model))
        models = harness.catalog_models({"data": rows}, without_browser=True)
        self.assertEqual([row["id"] for row in models], ["deepseek/deepseek-chat"])

    def run_cli_catalog(self, catalog, *args):
        requests = []
        real_client = httpx.Client

        def respond(request):
            requests.append((request.method, str(request.url)))
            return httpx.Response(200, json=catalog)

        def client(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return real_client(transport=httpx.MockTransport(respond), **kwargs)

        output, errors = io.StringIO(), io.StringIO()
        try:
            with patch.object(harness.httpx, "Client", side_effect=client), \
                    redirect_stdout(output), redirect_stderr(errors):
                harness.main(["--install", "desktop", "--home", str(self.home), *args])
        finally:
            self.assertEqual(requests, [("GET", BASE + "/models")])

    def test_cli_without_browser_threads_discovery_and_leaves_default_cli_unchanged(self):
        self.run_cli_catalog(CATALOG)
        rows = harness.yaml_codec().load(self.path)
        route = next(row for row in rows if row["id"] == "llm-pi-ai")["config"]["providers"][harness.ROUTE]
        self.assertEqual(route["models"], self.models)
        self.run_cli_catalog(CATALOG, "--without-browser")
        rows = harness.yaml_codec().load(self.path)
        route = next(row for row in rows if row["id"] == "llm-pi-ai")["config"]["providers"][harness.ROUTE]
        self.assertEqual(route["models"], self.models[:1])
        self.assertNotIn("agent-default-model", [row["id"] for row in rows])

    def test_without_browser_empty_catalog_refuses_before_any_write(self):
        before = self.path.read_bytes()
        files = set(self.directory.iterdir())
        with patch.object(harness, "write_private", wraps=harness.write_private) as writer:
            with self.assertRaises(SystemExit) as caught:
                self.run_cli_catalog({"data": [CATALOG["data"][1]]}, "--without-browser")
            self.assertEqual(caught.exception.code, 1)
            writer.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(set(self.directory.iterdir()), files)
        with self.assertRaisesRegex(ValueError, "No browserless models"):
            harness.install(self.home, "desktop", BASE, [], without_browser=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_without_browser_removed_owned_default_requires_explicit_valid_selection(self):
        original = harness.render([{"id": "agent-default-model", "config": {
            "provider": harness.ROUTE, "model": "glm-web"}}])
        self.path.write_bytes(original)
        selected = harness.catalog_models(CATALOG, without_browser=True)
        for default, message in ((None, "--default-model"), ("glm-web", "advertised")):
            with self.subTest(default=default), \
                    patch.object(harness, "write_private", wraps=harness.write_private) as writer:
                with self.assertRaisesRegex(ValueError, message):
                    harness.install(self.home, "desktop", BASE, selected, default, without_browser=True)
                writer.assert_not_called()
            self.assertEqual(self.path.read_bytes(), original)
            self.assertFalse(list(self.directory.glob("*.bridge-backup-*")))
        path, backup = harness.install(self.home, "desktop", BASE, selected,
                                       "qwen3.8-omni-flash", without_browser=True)
        self.assertEqual(backup.read_bytes(), original)
        rows = harness.yaml_codec().load(path)
        self.assertEqual(rows[0]["config"], {"provider": harness.ROUTE, "model": "qwen3.8-omni-flash"})

    def test_cli_without_browser_checks_existing_default_before_write(self):
        original = harness.render([{"id": "agent-default-model", "config": {
            "provider": harness.ROUTE, "model": "glm-web"}}])
        self.path.write_bytes(original)
        with patch.object(harness, "write_private", wraps=harness.write_private) as writer:
            with self.assertRaises(SystemExit) as caught:
                self.run_cli_catalog(CATALOG, "--without-browser")
            self.assertEqual(caught.exception.code, 1)
            writer.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)
        self.run_cli_catalog(CATALOG, "--without-browser", "--default-model", "qwen3.8-omni-flash")
        self.assertEqual(harness.yaml_codec().load(self.path)[0]["config"]["model"], "qwen3.8-omni-flash")

    def test_without_browser_dynamic_selectors_refuse_without_write_and_allow_explicit_override(self):
        selected = harness.catalog_models(CATALOG, without_browser=True)
        configurations = [
            'provider: !!js \'"local-browser-bridge"\'\n    model: glm-web',
            'provider: []\n    model: glm-web',
            'provider: null\n    model: glm-web',
            'provider: false\n    model: glm-web',
            'provider: local-browser-bridge\n    model: !!js \'"qwen3.8-omni-flash"\'',
            'provider: local-browser-bridge\n    model: []',
            'provider: local-browser-bridge\n    model: null',
            'provider: local-browser-bridge\n    model: false',
        ]
        for configuration in configurations:
            with self.subTest(configuration=configuration):
                original = ('# Preserve on refusal\n- id: agent-default-model\n  config:\n    '
                            + configuration + '\n').encode()
                self.path.write_bytes(original)
                files = set(self.directory.iterdir())
                with patch.object(harness, "write_private", wraps=harness.write_private) as writer:
                    with self.assertRaisesRegex(ValueError, "Dynamic or non-string.*--default-model"):
                        harness.install(self.home, "desktop", BASE, selected, without_browser=True)
                    writer.assert_not_called()
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(set(self.directory.iterdir()), files)
                path, backup = harness.install(self.home, "desktop", BASE, selected,
                                               "qwen3.8-omni-flash", without_browser=True)
                self.assertEqual(backup.read_bytes(), original)
                self.assertEqual(harness.yaml_codec().load(path)[0]["config"],
                                 {"provider": harness.ROUTE, "model": "qwen3.8-omni-flash"})

    def test_without_browser_preserves_unrelated_routes_and_existing_defaults(self):
        selected = harness.catalog_models(CATALOG, without_browser=True)
        for provider, model in (("deepseek-account", "native"), (harness.ROUTE, "qwen3.8-omni-flash")):
            document = [{"id": "agent-default-model", "config": {
                "provider": provider, "model": model, "extra": "preserved"}},
                {"id": "llm-pi-ai", "config": {"providers": {
                    "other": {"models": [{"id": "other-model"}]}}}},
                {"id": "native-account", "disabled": False}]
            with self.subTest(provider=provider):
                self.path.write_bytes(harness.render(document))
                harness.install(self.home, "desktop", BASE, selected, without_browser=True)
                rows = harness.yaml_codec().load(self.path)
                self.assertEqual(rows[0], document[0])
                self.assertEqual(rows[1]["config"]["providers"]["other"],
                                 document[1]["config"]["providers"]["other"])
                self.assertEqual(rows[2], document[2])

    def test_install_preserves_unrelated_routes_comments_and_js_without_execution(self):
        original = """# Keep this comment
- id: llm-pi-ai
  config:
    providers:
      other:
        apiKeyEnv: EXISTING_KEY
        models: [{id: existing}]
- id: unrelated
  disabled: !!js 'process.env.SOME_FLAG'
"""
        self.path.write_text(original)
        before = harness.yaml_codec().load(original)
        path, backup = harness.install(self.home, "desktop", BASE, self.models, "glm-web")
        self.assertEqual(backup.read_text(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        after = harness.yaml_codec().load(path)
        self.assertEqual(after[0]["config"]["providers"]["other"], before[0]["config"]["providers"]["other"])
        self.assertIn("Keep this comment", path.read_text())
        self.assertIn("!!js", path.read_text())
        route = after[0]["config"]["providers"][harness.ROUTE]
        self.assertEqual(route["retryPolicy"]["maxRetries"], 0)
        self.assertEqual(route["models"], self.models)
        self.assertTrue(next(row for row in after if row["id"] == "session-title-llm")["disabled"])
        self.assertEqual(after[-1]["config"], {"provider": harness.ROUTE, "model": "glm-web"})
        repeated = path.read_bytes()
        self.assertIsNone(harness.install(self.home, "desktop", BASE, self.models, "glm-web")[1])
        self.assertEqual(path.read_bytes(), repeated)

    def test_no_implicit_default_or_account_state_changes(self):
        pause = self.home / "provider-pauses.json"
        pause.write_text('{"deepseek":"account_restricted"}')
        before = pause.read_bytes()
        harness.install(self.home, "desktop", BASE, self.models)
        rows = harness.yaml_codec().load(self.path)
        self.assertNotIn("agent-default-model", [row["id"] for row in rows])
        self.assertEqual(pause.read_bytes(), before)
        self.assertFalse((self.home / ".credentials.yaml").exists())

    def test_shared_yaml_anchors_do_not_change_other_rows(self):
        document = harness.yaml_codec().load('''
- id: other
  config: &shared
    providers:
      a: &route {apiKeyEnv: EXISTING_KEY}
      b: *route
- id: another
  config: *shared
- id: llm-pi-ai
  config: *shared
''')
        result = harness.patch_document(document, BASE, self.models)
        with warnings.catch_warnings():
            warnings.simplefilter("error", ReusedAnchorWarning)
            restored = harness.yaml_codec().load(harness.render(result))
        self.assertEqual(restored[0]["config"], document[0]["config"])
        self.assertEqual(restored[1]["config"], document[1]["config"])
        self.assertIn(harness.ROUTE, restored[2]["config"]["providers"])

    def test_custom_layers_are_refused_before_writing(self):
        original = self.path.read_bytes()
        for file in (self.home / "cordis.patch.yml", self.directory / "cordis.yml"):
            file.write_text("- id: llm-pi-ai\n  disabled: true\n")
            with self.assertRaises(ValueError):
                harness.install(self.home, "desktop", BASE, self.models)
            self.assertEqual(self.path.read_bytes(), original)
            file.unlink()
        self.manifest.write_text('{"dsh":{"profile":{"bundles":["custom"]}}}')
        with self.assertRaises(ValueError):
            harness.install(self.home, "desktop", BASE, self.models)
        self.assertEqual(self.path.read_bytes(), original)

    def test_ambiguous_or_dynamic_edits_and_unadvertised_defaults_are_refused(self):
        documents = [[{"insert": [{"id": "llm-pi-ai"}]}],
                     [{"id": "llm-pi-ai"}, {"id": "llm-pi-ai"}],
                     [{"id": "llm-pi-ai", "disabled": True}],
                     harness.yaml_codec().load('- id: llm-pi-ai\n  config: !!js "dynamic()"'),
                     [{"id": "llm-pi-ai", "config": {"providers": "dynamic"}}]]
        for document in documents:
            original = harness.render(document)
            with self.subTest(document=document), self.assertRaises(ValueError):
                harness.patch_document(document, BASE, self.models)
            self.assertEqual(harness.render(document), original)
        with self.assertRaises(ValueError):
            harness.patch_document([], BASE, self.models, "deepseek-chat")

    def test_duplicate_yaml_keys_and_symlinks_cannot_silently_replace_config(self):
        self.path.write_text('- id: llm-pi-ai\n  config: {}\n  config: {}\n')
        original = self.path.read_bytes()
        with self.assertRaises(YAMLError):
            harness.install(self.home, "desktop", BASE, self.models)
        self.assertEqual(self.path.read_bytes(), original)
        link = self.directory / "link.yml"
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            harness.write_private(link, b"[]\n", original)
        self.assertEqual(self.path.read_bytes(), original)

    def test_concurrent_edit_is_retained(self):
        before = self.path.read_bytes()
        self.path.write_text("# a newer edit\n[]\n")
        with self.assertRaises(ValueError):
            harness.write_private(self.path, b"[]\n", before)
        self.assertIn("newer edit", self.path.read_text())
