import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import chat
from server.ratelimit import RateLimiter, install_rate_limit


class RateLimitTests(unittest.TestCase):
    def test_forwarded_header_cannot_reset_peer_limit(self):
        app = FastAPI()
        install_rate_limit(app,RateLimiter(1))
        @app.get("/v1/models")
        def models():
            return {"object":"list","data":[]}
        with TestClient(app) as client:
            self.assertEqual(client.get("/v1/models").status_code,200)
            for forwarded in [None,"fake-a","fake-b","1.2.3.4, 127.0.0.1"]:
                headers = {"X-Forwarded-For":forwarded} if forwarded else {}
                response = client.get("/v1/models",headers=headers)
                self.assertEqual(response.status_code,429)
                self.assertIn("Retry-After",response.headers)

    def test_expired_identity_is_cleaned_without_returning(self):
        limiter = RateLimiter(2,window=60)
        limiter.hit("old",0)
        limiter.hit("recent",30)
        limiter.hit("new",61)
        self.assertNotIn("old",limiter._hits)
        self.assertEqual(set(limiter._hits),{"recent","new"})

    def test_capacity_is_bounded_without_evicting_active_limits(self):
        limiter = RateLimiter(1,window=60,max_keys=2)
        self.assertTrue(limiter.hit("a",0)[0])
        self.assertTrue(limiter.hit("b",1)[0])
        self.assertFalse(limiter.hit("c",2)[0])
        self.assertFalse(limiter.hit("a",3)[0])
        self.assertEqual(len(limiter._hits),2)
        self.assertTrue(limiter.hit("c",61)[0])

    def test_invalid_configuration_fails_at_startup(self):
        for kwargs in [{"limit":0},{"limit":1,"window":0},{"limit":1,"max_keys":0}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RateLimiter(**kwargs)


class CliTests(unittest.TestCase):
    def test_model_override_starts_new_thread_only_if_model_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)/"state.json"
            state.write_text(json.dumps({"conversation_id":"existing:4","model":"default"}))
            for selected, expected in [("expert",None),("default","existing:4")]:
                with self.subTest(selected=selected), patch.object(chat,"STATE_FILE",state):
                    with patch.object(chat,"Chat") as factory, patch.object(sys,"argv",["chat.py","--model",selected]):
                        factory.return_value.status.return_value = "audit"
                        with patch("builtins.input",side_effect=EOFError), contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(chat.main(),0)
                        self.assertEqual(factory.call_args.kwargs["conversation_id"],expected)

    def test_interruption_and_transport_errors_preserve_saved_parent(self):
        for error in [KeyboardInterrupt(),RuntimeError("transport failed")]:
            with self.subTest(error=error):
                instance = chat.Chat.__new__(chat.Chat)
                instance.model, instance.thinking, instance.search = "default",False,False
                instance.cid = "existing:4"
                instance.save = Mock()
                class BrokenStream:
                    conversation_id = "existing"
                    def __iter__(self):
                        yield "partial"
                        raise error
                instance.client = Mock()
                instance.client.stream.return_value = BrokenStream()
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(type(error)):
                    instance.ask("audit")
                self.assertEqual(instance.cid,"existing:4")
                instance.save.assert_not_called()

    def test_successful_turn_updates_and_saves_cid(self):
        instance = chat.Chat.__new__(chat.Chat)
        instance.model, instance.thinking, instance.search = "default",False,False
        instance.cid = "existing:4"
        instance.save = Mock()
        class CompletedStream:
            conversation_id = "existing:6"
            def __iter__(self):
                yield "answer"
        instance.client = Mock()
        instance.client.stream.return_value = CompletedStream()
        with contextlib.redirect_stdout(io.StringIO()):
            instance.ask("audit")
        self.assertEqual(instance.cid,"existing:6")
        instance.save.assert_called_once()

    @unittest.skipUnless(shutil.which("bash"),"bash is required for the launcher")
    def test_launcher_resolves_relative_symlink_chains_and_spaces(self):
        with tempfile.TemporaryDirectory(prefix="deepseek launcher ") as directory:
            root = Path(directory)
            project = root/"project with spaces"
            (project/"bin").mkdir(parents=True)
            shutil.copy2(chat.ROOT/"bin"/"ds-chat",project/"bin"/"ds-chat")
            target = project/"ds"
            target.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
            target.chmod(0o755)
            (root/"bin").mkdir()
            (root/"bin"/"middle").symlink_to("../project with spaces/bin/ds-chat")
            (root/"bin"/"ds-chat").symlink_to("middle")
            output = subprocess.check_output(["bash",str(root/"bin"/"ds-chat"),"--model","expert","arg with spaces"],cwd=root,text=True)
            self.assertEqual(output.splitlines(),["--model","expert","arg with spaces"])

    def test_sdk_example_starts_its_own_conversation(self):
        import ast
        tree = ast.parse((chat.ROOT/"examples"/"06_server_openai_sdk.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.keyword) and node.arg == "extra_body":
                self.assertNotIn("conversation_id",ast.literal_eval(node.value))
