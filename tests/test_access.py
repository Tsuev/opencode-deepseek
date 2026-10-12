from tests import TEST_API_TOKEN
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient
from openai import OpenAI, PermissionDeniedError, BadRequestError, APIError

from deepseek.auth import get_session, LoginRequired, Session
from deepseek.client import DeepSeekClient, _biz, _parse_sse
from providers.access import AccessGuard, ProviderRejected
from providers.common import ProviderUnavailable
from server import api
from server.conversation_cache import ConversationCache
from server.config import DEEPSEEK_MODEL_MAP, QWEN_MODEL_MAP
from tests.test_api import TOOLS
from tests.test_stream import event
from tests.test_tools import block

MUTED = {"code": 0, "msg": "", "data": {"biz_code": 5, "biz_msg": "user is muted", "biz_data": None}}


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "pauses.json"
        cache_patch = patch.object(api, 'conversation_cache', ConversationCache(Path(self.directory.name) / 'conversations.sqlite3'))
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

    def test_pause_survives_restart_and_resume_in_separate_process_view(self):
        guard = AccessGuard(self.path, interval=0)
        with self.assertLogs("providers.access", level="WARNING"):
            guard.reject("deepseek", ProviderRejected("account_restricted"))
        restarted = AccessGuard(self.path, interval=0)
        with self.assertRaises(ProviderRejected) as failure:
            restarted.check("deepseek")
        self.assertEqual(failure.exception.code, "account_restricted")
        restarted.check("qwen")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        restarted.resume("deepseek")
        guard.check("deepseek")

    def test_new_pause_logs_sanitized_http_shape_not_payload_text(self):
        request = httpx.Request("POST", "https://chat.qwen.ai/api/v2/chat/completions")
        response = httpx.Response(
            403,
            headers={"content-type": "application/json; charset=utf-8",
                     "set-cookie": "secret=private"},
            json={"success": False, "data": {"code": "403", "message": "secret=private token=leak"}},
            request=request,
        )
        failure = httpx.HTTPStatusError("denied", request=request, response=response)
        guard = AccessGuard(self.path, interval=0)
        with self.assertLogs("providers.access", level="WARNING") as logs:
            error = guard.reject("qwen", failure)
        self.assertEqual(error.code, "access_denied")
        rendered = " ".join(logs.output)
        self.assertIn("Provider pause set:", rendered)
        self.assertIn("provider=qwen", rendered)
        self.assertIn("code=access_denied", rendered)
        self.assertIn("reason=http_status", rendered)
        self.assertIn("exc_type=HTTPStatusError", rendered)
        self.assertIn("http_status=403", rendered)
        self.assertIn("content_type=application/json", rendered)
        self.assertIn("business_code=403", rendered)
        self.assertIn("success_true=False", rendered)
        self.assertIn("response_type=object", rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("private", rendered)
        self.assertNotIn("token=leak", rendered)
        self.assertNotIn("set-cookie", rendered)
        with self.assertRaises(AssertionError):
            with self.assertLogs("providers.access", level="WARNING"):
                guard.reject("qwen", failure)

    def test_new_pause_logs_html_gateway_shape_without_markup(self):
        request = httpx.Request("POST", "https://chat.qwen.ai/api/v2/chat/completions")
        response = httpx.Response(
            403,
            headers={"content-type": "text/html"},
            content=b"<html><title>secret-challenge</title></html>",
            request=request,
        )
        failure = httpx.HTTPStatusError("denied", request=request, response=response)
        guard = AccessGuard(self.path, interval=0)
        with self.assertLogs("providers.access", level="WARNING") as logs:
            self.assertEqual(guard.reject("qwen", failure).code, "access_denied")
        rendered = " ".join(logs.output)
        self.assertIn("response_type=html", rendered)
        self.assertIn("http_status=403", rendered)
        self.assertNotIn("secret-challenge", rendered)
        self.assertNotIn("<html>", rendered)

    def test_new_pause_logs_protocol_token_for_qwen_stream_errors(self):
        from qwen.client import QwenStreamError
        failure = QwenStreamError("Qwen stream ended before a terminal marker")
        guard = AccessGuard(self.path, interval=0)
        with self.assertLogs("providers.access", level="WARNING") as logs:
            error = guard.reject("qwen", failure)
        self.assertEqual(error.code, "upstream_failure")
        rendered = " ".join(logs.output)
        self.assertIn("exc_type=QwenStreamError", rendered)
        self.assertIn("protocol=stream_no_terminal", rendered)
        self.assertIn("reason=invalid_protocol", rendered)
        self.assertNotIn("terminal marker", rendered)

    def test_new_pause_logs_attached_business_diagnostics(self):
        failure = ProviderRejected("upstream_failure")
        failure.phase = "sse_error"
        failure.protocol = "business_error"
        failure.business_code = "muted"
        failure.response_type = "object"
        failure.success_true = False
        guard = AccessGuard(self.path, interval=0)
        with self.assertLogs("providers.access", level="WARNING") as logs:
            self.assertEqual(guard.reject("qwen", failure).code, "upstream_failure")
        rendered = " ".join(logs.output)
        self.assertIn("phase=sse_error", rendered)
        self.assertIn("protocol=business_error", rendered)
        self.assertIn("business_code=muted", rendered)
        self.assertIn("success_true=False", rendered)

    def test_new_pause_logs_raw_code_from_exception(self):
        failure = ProviderRejected("upstream_failure")
        failure.phase = "completion_non_sse"
        failure.protocol = "business_error"
        failure.content_type = "application/json"
        failure.raw_code = "RateLimited_V2"
        failure.response_type = "object"
        guard = AccessGuard(self.path, interval=0)
        with self.assertLogs("providers.access", level="WARNING") as logs:
            guard.reject("qwen", failure)
        rendered = " ".join(logs.output)
        self.assertIn("raw_code=RateLimited_V2", rendered)
        self.assertIn("content_type=application/json", rendered)

    def test_malformed_state_and_failed_write_stop_requests(self):
        for text in ('{', '[]', '{"qwen": []}', '{"qwen": "invented"}'):
            self.path.write_text(text)
            with self.subTest(text=text), self.assertRaises(ProviderRejected) as error:
                AccessGuard(self.path, interval=0).check("qwen")
            self.assertEqual(error.exception.code, "safety_state_unavailable")
        self.path.unlink()
        guard = AccessGuard(self.path, interval=0)
        with patch.object(guard, "_save", side_effect=OSError("disk full")):
            error = guard.reject("qwen", RuntimeError("quota secret=private"))
        self.assertEqual(error.code, "safety_state_unavailable")
        with self.assertRaises(ProviderRejected):
            guard.check("qwen")

    def test_pacing_can_be_cancelled_without_starting_another_attempt(self):
        guard = AccessGuard(path=None, interval=60)
        guard.wait("qwen")
        with self.assertRaises(asyncio.CancelledError):
            guard.wait("qwen", lambda: (_ for _ in ()).throw(asyncio.CancelledError()))

    def test_business_errors_are_checked_in_json_and_sse_and_sanitized(self):
        for operation in (lambda: _biz(MUTED), lambda: list(_parse_sse(event(MUTED).splitlines()))):
            with self.assertRaises(ProviderRejected) as error:
                operation()
            self.assertEqual(error.exception.code, "account_restricted")
            self.assertNotIn("biz_data", str(error.exception))
        with self.assertRaises(ProviderRejected) as error:
            _biz({"code": 123, "msg": "secret=private"})
        self.assertNotIn("private", str(error.exception))

    def test_auth_codes_in_sse_are_terminal_without_message_matching(self):
        for code in (40002, 40003):
            for payload in ({"code": code}, {"v": {"code": code}}, {"code": code, "error": "private detail"}):
                with self.subTest(payload=payload), self.assertRaises(ProviderRejected) as error:
                    list(_parse_sse(event(payload).splitlines()))
                self.assertEqual(error.exception.code, "session_expired")
                self.assertNotIn("private", str(error.exception))

    def test_expired_session_does_not_launch_or_recapture_a_browser(self):
        with patch("deepseek.auth.Session.load", return_value=None), patch("deepseek.auth._headless_refresh") as refresh, patch("deepseek.auth.login") as login:
            with self.assertRaises(LoginRequired):
                get_session(allow_interactive=True)
            refresh.assert_not_called()
            login.assert_not_called()

    def test_clean_install_has_no_implicitly_enabled_provider(self):
        env = {**os.environ, "DEEPSEEK_ENABLED": "0", "QWEN_ENABLED": "0", **{name + "_ENABLED": "0" for name in ("GROK", "MISTRAL", "KIMI", "GLM", "GEMINI", "CHATGPT")}}
        result = subprocess.run([sys.executable, "-c", "from server.config import MODEL_MAP; print(MODEL_MAP)"], env=env, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "{}")

    def test_failed_pause_write_and_crash_keep_dispatch_durably_blocked(self):
        guard = AccessGuard(self.path, interval=0)
        attempt = guard.begin("deepseek")
        attempt.dispatch()
        with patch.object(guard, "_save", side_effect=OSError("disk full")):
            attempt.abort(ProviderRejected("account_restricted"))
        restarted = AccessGuard(self.path, interval=0)
        with self.assertRaises(ProviderRejected):
            restarted.begin("deepseek")

    def test_failed_journal_write_prevents_dispatch(self):
        guard = AccessGuard(self.path, interval=0)
        with patch.object(guard, "_save", side_effect=OSError("disk full")), self.assertRaises(ProviderRejected):
            guard.begin("deepseek")

    def test_resume_revokes_old_attempt_without_poisoning_a_new_owner(self):
        guard = AccessGuard(self.path, interval=0)
        old = guard.begin("gemini")
        old.dispatch()
        other = AccessGuard(self.path, interval=0)
        other.resume("gemini")
        with self.assertRaises(ProviderRejected):
            old.check()
        new = other.begin("gemini")
        old.abort(ProviderRejected())
        self.assertNotIn("pause", json.loads(self.path.read_text())["gemini"])
        new.check()
        new.complete()
        guard.check("gemini")

    def test_revoked_undispatched_attempt_does_not_mark_a_new_owner_unavailable(self):
        guard = AccessGuard(self.path, interval=0)
        old = guard.begin('glm')
        guard.resume('glm')
        new = guard.begin('glm')
        old.abort(asyncio.CancelledError())
        new.check()
        new.complete()
        guard.check('glm')

    def test_resume_during_cli_initialization_prevents_stdin_submission(self):
        from providers import access, antigravity
        from tests.test_providers import cli_init
        guard = AccessGuard(self.path, interval=0)
        script = Path(self.directory.name) / "agy-fixture"
        sent = Path(self.directory.name) / "sent"
        script.write_text("#!" + sys.executable + "\nimport sys,json,pathlib\nprint(" + repr(json.dumps(cli_init())) + ",flush=True)\nvalue=sys.stdin.readline()\nif value: pathlib.Path(" + repr(str(sent)) + ").write_text(value)\n")
        script.chmod(0o700)
        original = antigravity.validate_init
        def revoke(value, model):
            original(value, model)
            AccessGuard(self.path, interval=0).resume("gemini")
        with patch.object(access, "guard", guard), patch.dict(os.environ, {"ANTIGRAVITY_BIN": str(script)}), patch.object(antigravity, "validate_init", side_effect=revoke):
            with self.assertRaises(ProviderRejected):
                antigravity.AntigravityClient().chat("must not send", model="flash")
        self.assertFalse(sent.exists())

    def test_cancellation_before_dispatch_and_after_verified_finish_are_harmless(self):
        guard = AccessGuard(self.path, interval=0)
        before = guard.begin("deepseek")
        before.abort(asyncio.CancelledError())
        guard.check("deepseek")
        verified = guard.begin("deepseek")
        verified.dispatch()
        verified.complete()
        verified.abort(GeneratorExit())
        guard.check("deepseek")
        submitted = guard.begin("deepseek")
        submitted.dispatch()
        submitted.abort(asyncio.CancelledError())
        with self.assertRaises(ProviderRejected):
            AccessGuard(self.path, interval=0).check("deepseek")

    def test_pacing_reservation_is_shared_with_another_process(self):
        guard = AccessGuard(self.path, interval=2)
        guard.wait("qwen")
        code = "from providers.access import AccessGuard; from pathlib import Path; import sys,time; g=AccessGuard(Path(sys.argv[1]),interval=2); g.wait('qwen'); print(time.time())"
        first_dispatch = json.loads(self.path.read_text())["qwen"]["next_at"] - 2
        run = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True, text=True, check=True, timeout=60)
        self.assertGreaterEqual(float(run.stdout.strip()) - first_dispatch, 1.95)

    def test_paused_direct_browser_and_cli_adapters_create_no_job_or_process(self):
        from providers import access, tab_bridge, antigravity
        guard = AccessGuard(self.path, interval=0)
        for provider in ("glm", "gemini"):
            guard.reject(provider, ProviderRejected("access_denied"))
        with patch.object(access, "guard", guard), patch.object(tab_bridge.broker, "submit") as submit, patch.object(antigravity.subprocess, "Popen") as spawn:
            for client in (tab_bridge.TabClient("glm"), antigravity.AntigravityClient()):
                with self.assertRaises(ProviderRejected):
                    client.chat("offline", model="default")
            submit.assert_not_called()
            spawn.assert_not_called()

    def test_existing_browser_lease_is_revoked_by_pause_and_submitted_cancel_latches(self):
        from concurrent.futures import ThreadPoolExecutor
        from providers.tab_bridge import Broker
        for submitted in (False, True):
            guard = AccessGuard(self.path, interval=0)
            guard.resume("glm")
            broker, cancelled = Broker(), threading.Event()
            def check():
                if cancelled.is_set():
                    raise asyncio.CancelledError()
            def submit():
                with guard.attempt("glm", check):
                    return broker.submit("glm", "offline", None, check, 10)
            with ThreadPoolExecutor() as pool:
                future = pool.submit(submit)
                deadline = time.monotonic() + 5
                while not broker.pending and time.monotonic() < deadline:
                    time.sleep(.005)
                job = broker.claim("glm", "owner-123456789012345", "document-123456789012345")
                self.assertIsNotNone(job)
                args = ("glm", job["id"], "owner-123456789012345", job["lease"], "document-123456789012345")
                if submitted:
                    broker.begin(*args)
                    cancelled.set()
                    with self.assertRaises(asyncio.CancelledError):
                        future.result(timeout=5)
                    self.assertEqual(guard.status()["glm"], "upstream_failure")
                else:
                    pending = broker.pending["glm"]
                    guard.reject("glm", ProviderRejected("account_restricted"))
                    self.assertFalse(broker.active(*args))
                    with self.assertRaises(ValueError):
                        broker.begin(*args)
                    self.assertFalse(pending.submitted)
                    with self.assertRaises(ProviderRejected):
                        future.result(timeout=5)


class SafetySdkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        cache_patch = patch.object(api, 'conversation_cache', ConversationCache(Path(self.directory.name) / 'conversations.sqlite3'))
        cache_patch.start()
        self.addCleanup(cache_patch.stop)
        self.guard = AccessGuard(Path(self.directory.name) / "pauses.json", interval=0)
        for patcher in (
            patch.object(api, "guard", self.guard),
            patch.dict(api.MODEL_MAP, {**DEEPSEEK_MODEL_MAP, **QWEN_MODEL_MAP}, clear=True),
            # Keep direct HTTP Qwen path regardless of the developer's .env transport.
            patch.object(api, "qwen_uses_browser", return_value=False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.transport = TestClient(api.app, client=(self.id(), 123))
        self.transport.__enter__()
        self.addCleanup(self.transport.__exit__, None, None, None)
        # Keep the SDK's actual default retry policy enabled.
        self.sdk = OpenAI(api_key=TEST_API_TOKEN, base_url="http://testserver/v1", http_client=self.transport)
        self.addCleanup(self.sdk.close)
        self.messages = [{"role": "user", "content": "offline fixture"}]

    def deepseek_fixture(self, rejected_path, rejection_payload=MUTED):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path == rejected_path:
                return httpx.Response(200, json=rejection_payload)
            data = {"chat_session": {"id": "fixture"}} if request.url.path.endswith("/create") else {"challenge": {}}
            return httpx.Response(200, json={"code": 0, "data": {"biz_code": 0, "biz_data": data}})
        client = DeepSeekClient.__new__(DeepSeekClient)
        client._request_lock = threading.Lock()
        client._pow_lock = threading.Lock()
        client._pow = Mock()
        client._pow.make_header.return_value = "fixture"
        client._http = httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client, calls

    def test_invalid_auth_codes_stop_each_phase_and_survive_restart(self):
        for code in (40002, 40003):
            for path in ("/api/v0/chat_session/create", "/api/v0/chat/create_pow_challenge", "/api/v0/chat/completion"):
                case_guard = AccessGuard(Path(self.directory.name) / (str(code) + path.replace("/", "_") + ".json"), interval=0)
                client, calls = self.deepseek_fixture(path, {"code": code, "msg": "untranslated private detail"})
                with self.subTest(code=code, path=path), patch.object(api, "get_client", return_value=client) as factory, patch.object(api, "guard", case_guard):
                    with self.assertRaises(PermissionDeniedError) as failure:
                        self.sdk.chat.completions.create(model="deepseek-chat", messages=self.messages)
                    self.assertEqual(failure.exception.body["code"], "session_expired")
                    self.assertFalse(failure.exception.body["retryable"])
                    restarted = AccessGuard(case_guard.path, interval=0)
                    with patch.object(api, "guard", restarted), self.assertRaises(BadRequestError) as resumed:
                        self.sdk.chat.completions.create(model="deepseek-chat", messages=self.messages)
                    self.assertEqual(resumed.exception.body["code"], "session_expired")
                    self.assertFalse(resumed.exception.body["retryable"])
                    self.assertEqual(calls.count(path), 1)
                    self.assertEqual(factory.call_count, 1)

    def test_muted_at_each_protocol_step_stops_sdk_and_later_requests(self):
        for path in ("/api/v0/chat_session/create", "/api/v0/chat/create_pow_challenge", "/api/v0/chat/completion"):
            self.guard.resume("deepseek")
            client, calls = self.deepseek_fixture(path)
            with self.subTest(path=path), patch.object(api, "get_client", return_value=client) as factory, \
                    self.assertLogs('deepseek.client', level='WARNING') as diagnostics:
                for attempt in range(2):
                    # First refusal is upstream (403); later hits are local pause (400).
                    expected = PermissionDeniedError if attempt == 0 else BadRequestError
                    with self.assertRaises(expected) as failure:
                        self.sdk.chat.completions.create(model="deepseek-chat", messages=self.messages)
                    self.assertFalse(failure.exception.body["retryable"])
                    self.assertEqual(failure.exception.body["code"], "account_restricted")
                    message = failure.exception.body["message"]
                    self.assertIn("providers.access resume deepseek", message)
                    if attempt == 0:
                        self.assertIn("Upstream provider `deepseek` refused", message)
                        self.assertIn("enabled a durable providers.access pause", message)
                    else:
                        self.assertIn("Manual providers.access pause is blocking", message)
                        self.assertIn("No request was sent", message)
                self.assertEqual(calls.count(path), 1)
                self.assertEqual(factory.call_count, 1)
            expected_phase = {'/api/v0/chat_session/create': 'create_chat',
                              '/api/v0/chat/create_pow_challenge': 'challenge',
                              '/api/v0/chat/completion': 'completion'}[path]
            self.assertEqual(len(diagnostics.records), 1)
            self.assertIn('phase=' + expected_phase + ' http_status=200 reason=account_restricted',
                          diagnostics.output[0])
            self.assertNotIn('user is muted', diagnostics.output[0])

    def test_deepseek_transport_diagnostics_do_not_log_exception_text(self):
        client, calls = self.deepseek_fixture('unused')
        with patch.object(client._http, 'post', side_effect=httpx.ConnectError('private-token-and-network-detail')), \
                self.assertLogs('deepseek.client', level='WARNING') as diagnostics:
            with self.assertRaises(httpx.ConnectError):
                client.chat('owned fixture')
        self.assertEqual(calls, [])
        self.assertEqual(len(diagnostics.records), 1)
        self.assertIn('phase=create_chat http_status=None reason=transport_error', diagnostics.output[0])
        self.assertNotIn('private-token', diagnostics.output[0])

    def test_stream_error_is_terminal_and_reconnect_is_blocked_before_auth(self):
        for tools in (None, TOOLS):
            self.guard.resume("deepseek")
            client, calls = self.deepseek_fixture("/api/v0/chat/completion")
            with patch.object(api, "get_client", return_value=client) as factory:
                with self.sdk.chat.completions.create(model="deepseek-chat", messages=self.messages, tools=tools, stream=True) as stream:
                    with self.assertRaises(APIError):
                        list(stream)
                with self.assertRaises(BadRequestError) as blocked:
                    self.sdk.chat.completions.create(model="deepseek-chat", messages=self.messages, tools=tools, stream=True)
                self.assertIn("providers.access resume deepseek", blocked.exception.body["message"])
                self.assertEqual(calls.count("/api/v0/chat/completion"), 1)
                self.assertEqual(factory.call_count, 1)

    def test_tool_continuation_does_not_mask_or_retry_protective_refusal(self):
        client = Mock()
        from chat_protocol import Reply
        client.chat.side_effect = [Reply('```tool_calls\n[{"name":"write","arguments":{"content":"', "fixture:2", "length"), ProviderRejected("quota_exceeded")]
        with patch.object(api, "get_client", return_value=client), patch.object(api, "_MAX_CONTINUATIONS", 1):
            with self.assertRaises(PermissionDeniedError) as error:
                self.sdk.chat.completions.create(model="deepseek-chat", messages=self.messages, tools=TOOLS)
        self.assertEqual(error.exception.body["code"], "quota_exceeded")
        self.assertEqual(client.chat.call_count, 2)

    def test_browser_or_cli_denial_is_terminal_and_provider_scoped(self):
        for provider in ("grok", "mistral", "kimi", "glm", "gemini", "chatgpt", "claude"):
            name = provider + "-fixture"
            fake = Mock()
            fake.chat.side_effect = ProviderUnavailable("quota or regional restriction")
            with patch.dict(api.MODEL_MAP, {name: "fixture"}), patch.dict(api.model_provider.__globals__["OPTIONAL_MODEL_PROVIDERS"], {name: provider}), patch.object(api, "build_provider_client", return_value=fake) as factory:
                with self.assertRaises(PermissionDeniedError):
                    self.sdk.chat.completions.create(model=name, messages=self.messages)
                with self.assertRaises(BadRequestError) as blocked:
                    self.sdk.chat.completions.create(model=name, messages=self.messages)
                self.assertIn("providers.access resume " + provider, blocked.exception.body["message"])
                self.assertEqual(factory.call_count, 1)
                self.assertEqual(fake.chat.call_count, 1)
        self.guard.check("qwen")

    def test_disabled_deepseek_never_initializes_a_session(self):
        with patch.dict(api.MODEL_MAP, QWEN_MODEL_MAP, clear=True), patch.object(api, "get_client") as factory:
            response = self.transport.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": self.messages})
        self.assertEqual(response.status_code, 404)
        factory.assert_not_called()

    def test_no_background_capture_even_with_legacy_refresh_opt_in(self):
        with patch.object(api, "SESSION_REFRESH_ENABLED", True), patch.object(api, "_build_client") as build:
            with TestClient(api.app):
                pass
        build.assert_not_called()

    def test_pause_after_factory_prevents_actual_http_dispatch(self):
        client, calls = self.deepseek_fixture("/api/v0/chat/completion")
        def factory():
            self.guard.reject("deepseek", ProviderRejected("account_restricted"))
            return client
        with patch.object(api, "get_client", side_effect=factory), self.assertRaises(BadRequestError) as blocked:
            self.sdk.chat.completions.create(model="deepseek-chat", messages=self.messages)
        self.assertIn("providers.access resume deepseek", blocked.exception.body["message"])
        self.assertEqual(calls, [])

    def test_signin_and_resume_replace_rejected_clients_without_server_restart(self):
        from chat_protocol import Reply
        for provider, model in (("deepseek", "deepseek-chat"), ("qwen", "qwen3.8-max")):
            old, fresh = Mock(), Mock()
            if provider == "deepseek":
                old.session = Session("synthetic-old", [], "fixture", time.time())
                fresh.session = Session("synthetic-fresh", [], "fixture", time.time())
            else:
                old.session.age = 0
                old.session.usable = True
            old.chat.side_effect = ProviderRejected("session_expired")
            fresh.chat.return_value = Reply("verified", None)
            cache = "_client" if provider == "deepseek" else "_qwen_client"
            builder = "_build_client" if provider == "deepseek" else "QwenClient"
            with patch.object(api, "get_session", side_effect=[old.session, fresh.session]), patch.object(api, "_client_identity", api._session_identity(old.session) if provider == "deepseek" else "isolated"), patch.object(api, cache, old), patch.object(api, builder, return_value=fresh) as build, patch.object(api, "get_qwen_session", side_effect=[old.session, fresh.session]):
                with self.assertRaises(PermissionDeniedError):
                    self.sdk.chat.completions.create(model=model, messages=self.messages)
                AccessGuard(self.guard.path, interval=0).resume(provider)
                reply = self.sdk.chat.completions.create(model=model, messages=self.messages)
                self.assertEqual(reply.choices[0].message.content, "verified")
                self.assertEqual(old.chat.call_count, 1)
                self.assertEqual(fresh.chat.call_count, 1)
                build.assert_called_once()

    def test_close_real_stream_before_terminal_blocks_another_completion(self):
        from tests.test_stream import mock_client, snapshot
        client = mock_client(snapshot("partial"))
        client.access_guard = self.guard
        stream = iter(client.stream("fixture"))
        self.assertEqual(next(stream), "partial")
        stream.close()
        with self.assertRaises(ProviderRejected):
            list(client.stream("must not send"))
        client.close()
