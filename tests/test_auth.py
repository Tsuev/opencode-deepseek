import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import httpx

from deepseek import auth


def session():
    return auth.Session("fake-token", [], "audit-agent", time.time())


class SessionTests(unittest.TestCase):
    def test_only_scoped_deepseek_cookies_are_captured_and_sent(self):
        page = Mock()
        page.evaluate.side_effect = ["fake-token","audit-agent"]
        context = Mock()
        context.cookies.return_value = [
            {"name":"ds","value":"fake-ds","domain":".deepseek.com","path":"/","secure":True},
            {"name":"foreign","value":"fake-foreign","domain":".unrelated.invalid","path":"/"},
            {"name":"restricted","value":"fake-path","domain":"chat.deepseek.com","path":"/account"},
            {"name":"expired","value":"fake-old","domain":"chat.deepseek.com","path":"/","expires":1},
        ]
        captured = auth._capture_from_context(context, page)
        context.cookies.assert_called_once_with([auth.CHAT_URL])
        with httpx.Client(cookies=captured.cookie_jar()) as client:
            headers = client.build_request("POST","https://chat.deepseek.com/api/v0/chat/completion").headers
            self.assertEqual(headers.get("cookie"), "ds=fake-ds")
            self.assertNotIn("cookie", client.build_request("GET","https://unrelated.invalid/").headers)
            self.assertNotIn("ds=fake-ds", client.build_request("GET","http://chat.deepseek.com/").headers.get("cookie", ""))

    def test_cookie_names_are_not_collapsed_across_paths(self):
        captured = session()
        captured.cookies = [
            {"name":"same","value":"root","domain":".deepseek.com","path":"/"},
            {"name":"same","value":"api","domain":".deepseek.com","path":"/api"},
        ]
        with httpx.Client(cookies=captured.cookie_jar()) as client:
            self.assertEqual(client.build_request("GET",auth.CHAT_URL+"api/test").headers["cookie"],
                             "same=api; same=root")

    def test_session_storage_is_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"session"/"session.json"
            previous_umask = os.umask(0o022)
            try:
                saved = session()
                saved.save(path)
            finally:
                os.umask(previous_umask)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            baseline = path.read_bytes()
            saved.token = "fake-new"
            with patch.object(auth.os,"replace",side_effect=OSError("cannot replace")):
                with self.assertRaises(OSError):
                    saved.save(path)
            self.assertEqual(path.read_bytes(), baseline)
            self.assertEqual(list(path.parent.iterdir()), [path])
            self.assertEqual(auth.Session.load(path).token,"fake-token")

    def test_legacy_cookie_cache_requires_recapture(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"session.json"
            path.write_text(json.dumps({"token":"fake","cookies":{"foreign":"secret"},
                                        "user_agent":"fake","captured_at":time.time()}))
            self.assertIsNone(auth.Session.load(path))
        with self.assertRaises(auth.LoginRequired):
            auth.Session("fake",{},"fake",time.time()).cookie_jar()

    def test_fallback_handles_channel_exceptions_and_saves_requested_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"custom"/"session.json"
            with patch.object(auth,"_headless_refresh",side_effect=[RuntimeError("browser unavailable"),session()]) as refresh:
                with contextlib.redirect_stdout(io.StringIO()):
                    result = auth.get_session(session_file=path,channel="preferred",fallback_channel="fallback",
                                              allow_interactive=False,allow_refresh=True)
            self.assertEqual([call.args[1] for call in refresh.call_args_list],["preferred","fallback"])
            self.assertEqual(auth.Session.load(path).token,result.token)

    def test_failed_headless_login_returns_login_required(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(auth,"_headless_refresh",side_effect=RuntimeError("unavailable")):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(auth.LoginRequired):
                auth.get_session(session_file=Path(directory)/"missing.json",allow_interactive=False,allow_refresh=True)

    def test_profile_capture_is_serialized(self):
        state = {"active":0,"maximum":0}
        barrier = threading.Barrier(3)
        guard = threading.Lock()
        errors = []
        def refresh(*args):
            with guard:
                state["active"] += 1
                state["maximum"] = max(state["maximum"],state["active"])
            time.sleep(0.02)
            with guard:
                state["active"] -= 1
            return session()
        with tempfile.TemporaryDirectory() as directory, patch.object(auth,"_headless_refresh",side_effect=refresh):
            def capture(index):
                try:
                    barrier.wait(timeout=2)
                    auth.get_session(session_file=Path(directory)/str(index)/"session.json",max_age=0,
                                     allow_interactive=False,allow_refresh=True)
                except Exception as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=capture,args=(i,)) for i in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=2)
            for thread in threads:
                thread.join(timeout=3)
            self.assertFalse(errors)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(state["maximum"],1)

    def test_environment_is_loaded_before_auth_and_server_config(self):
        code = '''import os,json
from unittest.mock import patch
for key in ["RATE_LIMIT_PER_MINUTE","SERVER_INTERACTIVE_LOGIN","DEEPSEEK_PROFILE_DIR"]:
 os.environ.pop(key,None)
def load(*args,**kwargs):
 os.environ.update(RATE_LIMIT_PER_MINUTE="7",SERVER_INTERACTIVE_LOGIN="0",DEEPSEEK_PROFILE_DIR="/tmp/audit-profile")
with patch("dotenv.load_dotenv",load):
 import server.api as api
 import deepseek.auth as auth
 print(json.dumps([api.RATE_LIMIT_PER_MINUTE,api.SERVER_INTERACTIVE_LOGIN,str(auth.DEFAULT_PROFILE_DIR)]))
'''
        output = subprocess.check_output([sys.executable,"-c",code],cwd=auth.ROOT,text=True)
        self.assertEqual(json.loads(output),[7,False,"/tmp/audit-profile"])
