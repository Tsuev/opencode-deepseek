import contextlib
import io
import json
from pathlib import Path
from urllib.parse import urlparse
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import httpx

from deepseek.auth import Session as DeepSeekSession
from playwright.sync_api import Error as PlaywrightError

from qwen.auth import LoginRequired, Session, _capture, _launch, get_session, keep_open, login, main
from qwen.client import QwenClient, QwenStreamError, _decode_cid, _parse_sse
from qwen.renewal import AUTH_REFRESH_URL
from providers.access import ProviderRejected

CHAT = "00000000-0000-4000-8000-000000000001"
MESSAGE = "00000000-0000-4000-8000-000000000002"
SECONDARY = "00000000-0000-4000-8000-000000000003"


def session():
    return Session("fake-qwen-token", [], "test-agent", time.time(), expires_at=time.time() + 900)


def events(*objects, done=True):
    result = []
    for obj in objects:
        result += ["data: " + json.dumps(obj), ""]
    if done:
        result += ["data: [DONE]", ""]
    return result


def answer(content, **extra):
    return {"response_id": MESSAGE, "choices": [{"index": 0, "delta": {"phase": "answer", "content": content, **extra}}]}


class QwenSessionTests(unittest.TestCase):
    def test_capture_keeps_expiry_separate_from_continuation_identity(self):
        expires = time.time() + 900
        context = Mock()
        context.cookies.return_value = []
        page = Mock(url='https://chat.qwen.ai/')
        page.evaluate.side_effect = [{'token': 'synthetic-token', 'expires_at': expires}, 'test-agent']
        captured = _capture(context, page)
        self.assertTrue(captured.usable)
        self.assertEqual(captured.expires_at, expires)
        self.assertIsNone(captured.continuation_scope)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'session.json'
            captured.save(path)
            self.assertEqual(Session.load(path), captured)

    def test_provider_cookies_never_cross_the_boundary(self):
        qwen = session()
        qwen.cookies = [
            {"name": "qwen", "value": "fake", "domain": ".qwen.ai", "path": "/", "secure": True},
            {"name": "auth", "value": "fake", "domain": "auth.qwen.ai", "path": "/api/v2/auths", "secure": True},
            {"name": "deepseek", "value": "fake", "domain": ".deepseek.com", "path": "/"},
            {"name": "google", "value": "fake", "domain": ".google.com", "path": "/"},
        ]
        with httpx.Client(cookies=qwen.cookie_jar()) as client:
            self.assertEqual(client.build_request("GET", "https://chat.qwen.ai/api/v2/models/").headers["cookie"], "qwen=fake")
            self.assertNotIn("cookie", client.build_request("GET", "https://chat.deepseek.com/").headers)
            self.assertNotIn("cookie", client.build_request("GET", "https://google.com/").headers)
            self.assertNotIn("cookie", client.build_request("GET", "http://chat.qwen.ai/").headers)
        ds = DeepSeekSession("fake", qwen.cookies, "test", time.time())
        with httpx.Client(cookies=ds.cookie_jar()) as client:
            self.assertNotIn("cookie", client.build_request("GET", "https://chat.qwen.ai/").headers)

    def test_capture_never_inspects_an_oauth_provider(self):
        page = Mock(url="https://accounts.google.com/")
        self.assertIsNone(_capture(Mock(), page))
        page.evaluate.assert_not_called()

    def test_capture_includes_refresh_path_cookies_without_unrelated_paths(self):
        cookies = [
            {"name": "refresh_token", "value": "synthetic", "domain": "auth.qwen.ai",
             "path": "/api/v2/auths", "secure": True},
            {"name": "unrelated", "value": "synthetic", "domain": "auth.qwen.ai",
             "path": "/unrelated", "secure": True},
        ]
        def site_cookies(urls):
            return [cookie for cookie in cookies if any(
                urlparse(url).hostname == cookie["domain"]
                and urlparse(url).path.startswith(cookie["path"]) for url in urls)]
        context = Mock()
        context.cookies.side_effect = site_cookies
        page = Mock(url="https://chat.qwen.ai/")
        page.evaluate.side_effect = [{"token": "synthetic", "expires_at": time.time() + 900},
                                     "synthetic-user-agent"]
        captured = _capture(context, page)
        self.assertEqual(captured.cookies, cookies[:1])
        self.assertIn(AUTH_REFRESH_URL, context.cookies.call_args.args[0])
        with httpx.Client(cookies=captured.cookie_jar()) as client:
            self.assertEqual(client.build_request("GET", AUTH_REFRESH_URL).headers["cookie"],
                             "refresh_token=synthetic")

    def test_capture_rejects_expired_or_invalid_expiry(self):
        page = Mock(url="https://chat.qwen.ai/")
        for expires in (time.time() - 1, float("nan"), "invalid"):
            page.evaluate.return_value = {"token": "fake", "expires_at": expires}
            self.assertIsNone(_capture(Mock(), page))

    def test_capture_rejects_empty_and_blank_tokens_before_reading_cookies(self):
        for token in ("", " \t\n", "\u2003"):
            with self.subTest(token=repr(token)):
                page, context = Mock(url="https://chat.qwen.ai/"), Mock()
                page.evaluate.side_effect = [{"token": token, "expires_at": time.time() + 900}, "test-agent"]
                context.cookies.return_value = []
                self.assertIsNone(_capture(context, page))
                context.cookies.assert_not_called()

    def test_blank_cached_token_is_not_usable_and_requires_refresh(self):
        cached, fresh = session(), session()
        cached.token = " \t\n"
        self.assertFalse(cached.usable)
        with patch.object(Session, "load", return_value=cached), patch("qwen.auth._capture_profile", return_value=fresh) as capture, patch.object(Session, "save"):
            self.assertIs(get_session(allow_refresh=True, allow_interactive=False), fresh)
        capture.assert_called_once()

    def test_unusable_capture_is_not_saved_and_does_not_block_fallback(self):
        for token in ("", " \t\n"):
            with self.subTest(token=repr(token)):
                invalid, fresh = session(), session()
                invalid.token = token
                with patch.object(Session, "load", return_value=None), patch("qwen.auth._capture_profile", side_effect=[invalid, fresh]) as capture, patch.object(Session, "save", autospec=True) as save:
                    result = get_session(allow_refresh=True, allow_interactive=False, channel="preferred", fallback_channel="fallback")
                self.assertIs(result, fresh)
                self.assertEqual([call.args[2] for call in capture.call_args_list], ["preferred", "fallback"])
                save.assert_called_once_with(fresh, unittest.mock.ANY)

    def test_login_refuses_an_unusable_capture(self):
        invalid = session()
        invalid.token = " \t"
        with patch("qwen.auth._capture_profile", return_value=invalid), patch.object(Session, "save") as save:
            with self.assertRaises(LoginRequired):
                login()
        save.assert_not_called()

    def test_session_storage_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qwen" / "session.json"
            saved = session()
            saved.save(path)
            loaded = Session.load(path)
            self.assertTrue(loaded.usable)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded.expires_at = time.time() + 30
            self.assertFalse(loaded.usable)

    def test_session_compare_and_swap_preserves_newer_manual_signin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            old, renewed, manual = session(), session(), session()
            old.save(path)
            renewed.token = "renewed-fixture-token"
            self.assertTrue(renewed.save_if_current(old, path))
            self.assertEqual(Session.load(path), renewed)
            manual.token = "manual-fixture-token"
            manual.save(path)
            self.assertFalse(old.save_if_current(renewed, path))
            self.assertEqual(Session.load(path), manual)
            self.assertEqual(path.with_suffix(".lock").stat().st_mode & 0o777, 0o600)

    def test_session_compare_and_swap_checks_cancellation_after_writer_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            old, renewed = session(), session()
            old.save(path)
            cancelled = Mock(side_effect=RuntimeError("owned cancellation"))
            with self.assertRaisesRegex(RuntimeError, "owned cancellation"):
                renewed.save_if_current(old, path, check=cancelled)
            cancelled.assert_called_once()
            self.assertEqual(Session.load(path), old)

    def test_session_writers_sync_directory_before_reporting_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            old, fresh = session(), session()
            with patch("qwen.auth._sync_session_directory") as sync:
                old.save(path)
                self.assertTrue(fresh.save_if_current(old, path))
            self.assertEqual(sync.call_args_list, [unittest.mock.call(path), unittest.mock.call(path)])

    def test_short_expiry_triggers_headless_refresh_even_for_a_fresh_capture(self):
        cached, fresh = session(), session()
        cached.expires_at = time.time() + 30
        with patch.object(Session, "load", return_value=cached), patch("qwen.auth._capture_profile", return_value=fresh) as capture, patch.object(Session, "save") as save:
            self.assertIs(get_session(allow_refresh=True, allow_interactive=False), fresh)
        self.assertTrue(capture.call_args.args[1])
        save.assert_called_once()

    def test_disabled_fallback_and_headless_mode_never_open_login(self):
        with patch.object(Session, "load", return_value=None), patch("qwen.auth._capture_profile", return_value=None) as capture, patch("qwen.auth.login") as login:
            with self.assertRaises(LoginRequired):
                get_session(allow_refresh=True, allow_interactive=False, fallback_channel="")
        self.assertEqual(capture.call_count, 1)
        self.assertTrue(capture.call_args.args[1])
        login.assert_not_called()


class QwenKeepOpenTests(unittest.TestCase):
    def watch(self, states, shutdown="interrupt"):
        page, context = Mock(), Mock()
        context.pages = [page]
        page.is_closed.return_value = False
        waits = 0

        def wait(_):
            nonlocal waits
            waits += 1
            if waits == len(states):
                if shutdown == "interrupt":
                    raise KeyboardInterrupt
                if shutdown == "context":
                    context.on.call_args.args[1]()
                else:
                    page.is_closed.return_value = True
                raise PlaywrightError("closed with fake-secret-detail")

        page.wait_for_timeout.side_effect = wait
        output = io.StringIO()
        with patch("qwen.auth.sync_playwright") as playwright, \
                patch("qwen.auth._launch", return_value=context) as launch, \
                patch("qwen.auth._capture", side_effect=states) as capture, \
                patch.object(Session, "save", autospec=True) as save, \
                contextlib.redirect_stdout(output):
            keep_open(Path("unused-profile"), Path("unused-session.json"))
        launch.assert_called_once_with(playwright.return_value.__enter__.return_value,
                                       Path("unused-profile"), False, "chrome")
        page.goto.assert_called_once_with("https://chat.qwen.ai/auth", wait_until="commit", timeout=60000)
        self.assertEqual(capture.call_count, len(states))
        context.close.assert_called_once()
        self.assertNotIn("fake-secret-detail", output.getvalue())
        return save, output.getvalue()

    def test_unchanged_session_is_saved_once_despite_capture_time_and_cookie_order(self):
        initial = session()
        initial.cookies = [{"name": "b", "value": "cookie-secret"}, {"name": "a", "value": "other"}]
        later = Session(**{**vars(initial), "captured_at": initial.captured_at + 1,
                           "cookies": list(reversed(initial.cookies))})
        save, output = self.watch([initial, later, later])
        save.assert_called_once_with(initial, Path("unused-session.json"))
        self.assertEqual(output.count("Valid session saved"), 1)
        self.assertIn("Ctrl+C", output)
        self.assertNotIn(initial.token, output)
        self.assertNotIn("cookie-secret", output)

    def test_rotation_of_token_expiry_cookies_or_user_agent_is_saved(self):
        initial = session()
        for changes in ({"token": "rotated-secret"}, {"expires_at": initial.expires_at + 900},
                        {"cookies": [{"name": "auth", "value": "cookie-secret"}]},
                        {"user_agent": "rotated-agent-secret"}):
            with self.subTest(field=next(iter(changes))):
                rotated = Session(**{**vars(initial), **changes})
                save, output = self.watch([initial, rotated, rotated])
                self.assertEqual([call.args[0] for call in save.call_args_list], [initial, rotated])
                self.assertEqual(output.count("Valid session saved"), 2)
                self.assertNotIn("rotated-secret", output)
                self.assertNotIn("cookie-secret", output)
                self.assertNotIn("rotated-agent-secret", output)

    def test_invalid_or_expiring_state_is_never_saved_or_announced_valid(self):
        blank = session()
        blank.token = " \t"
        expired = session()
        expired.expires_at = time.time() - 1
        expiring = session()
        expiring.expires_at = time.time() + 30
        invalid = session()
        invalid.expires_at = float("nan")
        save, output = self.watch([None, blank, expired, expiring, invalid])
        save.assert_not_called()
        self.assertNotIn("Valid session", output)

    def test_invalidation_clears_ready_notice_without_rewriting_unchanged_recovery(self):
        valid = session()
        save, output = self.watch([valid, None, None, valid])
        save.assert_called_once()
        self.assertEqual(output.count("Website session is missing or expired"), 1)
        self.assertEqual(output.count("Valid session available"), 1)

    def test_page_close_stops_gracefully(self):
        save, output = self.watch([session()], shutdown="page")
        save.assert_called_once()
        self.assertIn("Website window closed; capture stopped.", output)

    def test_context_close_stops_gracefully(self):
        save, output = self.watch([session()], shutdown="context")
        save.assert_called_once()
        self.assertIn("Website window closed; capture stopped.", output)

    def test_startup_preserves_existing_local_browser_fallback(self):
        playwright, profile = Mock(), Mock()
        playwright.chromium.launch_persistent_context.side_effect = [PlaywrightError("missing executable"), Mock()]
        _launch(playwright, profile, False, "chrome")
        self.assertEqual(playwright.chromium.launch_persistent_context.call_count, 2)
        self.assertEqual(playwright.chromium.launch_persistent_context.call_args_list[0].kwargs,
                         {"headless": False, "channel": "chrome"})
        self.assertEqual(playwright.chromium.launch_persistent_context.call_args_list[1].kwargs,
                         {"headless": False})

    def test_navigation_failure_does_not_retry_or_expose_details(self):
        page, context = Mock(), Mock()
        context.pages = [page]
        page.is_closed.return_value = False
        page.goto.side_effect = PlaywrightError("fake-secret-navigation-detail")
        output = io.StringIO()
        with patch("qwen.auth.sync_playwright"), patch("qwen.auth._launch", return_value=context) as launch, \
                patch("qwen.auth._capture") as capture, patch.object(Session, "save") as save, \
                contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
            main(["--keep-open"])
        self.assertEqual(stopped.exception.code, 1)
        launch.assert_called_once()
        page.goto.assert_called_once()
        capture.assert_not_called()
        save.assert_not_called()
        context.close.assert_called_once()
        self.assertNotIn("fake-secret-navigation-detail", output.getvalue())

    def test_default_login_still_closes_after_initial_capture(self):
        page, context = Mock(), Mock()
        context.pages = [page]
        valid = session()
        with patch("qwen.auth.sync_playwright"), patch("qwen.auth._launch", return_value=context) as launch, \
                patch("qwen.auth._capture", return_value=valid), patch.object(Session, "save") as save, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(login(), valid)
        self.assertFalse(launch.call_args.args[2])
        context.close.assert_called_once()
        page.wait_for_timeout.assert_not_called()
        save.assert_called_once()

    def test_cli_keep_open_is_explicit(self):
        with patch("qwen.auth.login", return_value=session()) as normal, \
                patch("qwen.auth.keep_open") as watcher, contextlib.redirect_stdout(io.StringIO()):
            main([])
            normal.assert_called_once_with()
            watcher.assert_not_called()
            main(["--keep-open"])
            watcher.assert_called_once_with()
            normal.assert_called_once_with()

    def test_default_get_session_never_starts_a_browser(self):
        with patch.object(Session, "load", return_value=None) as load, \
                patch("qwen.auth._capture_profile") as capture, patch("qwen.auth.login") as normal, \
                patch("qwen.auth.keep_open") as watcher, self.assertRaises(LoginRequired) as failure:
            get_session()
        load.assert_called_once()
        capture.assert_not_called()
        normal.assert_not_called()
        watcher.assert_not_called()
        self.assertIn("python -m qwen.auth", str(failure.exception))
        self.assertIn("expired", str(failure.exception))
        self.assertIn("QWEN_RENEW_SESSION", str(failure.exception))
        self.assertNotIn("\n", str(failure.exception))


class QwenStreamTests(unittest.TestCase):
    def test_explicit_response_ids_are_validated_before_routing_or_text(self):
        created = {"response.created": {"response_id": MESSAGE, "response_index": "0"}}
        unowned = {"choices": [{"delta": {"content": "unowned text"}}]}
        for invalid in (None, False, 42, "", " ", "not-a-uuid", {}, []):
            for location in ("created", "delta", "stopped"):
                with self.subTest(response_id=invalid, location=location):
                    if location == "created":
                        frame = {"response.created": {"response_id": invalid, "response_index": "1"}}
                    elif location == "stopped":
                        frame = {"response.stopped": {"response_id": invalid}}
                    else:
                        frame = {"response_id": invalid, **unowned}
                    iterator = _parse_sse(events(created, frame, unowned), {})
                    with self.assertRaisesRegex(QwenStreamError, "response_id"):
                        next(iterator)

    def test_created_response_requires_an_unambiguous_id(self):
        invalid_frames = [
            {"response.created": {"response_index": "1"}},
            {"response.created": None},
            {"response.created": {"response_id": MESSAGE}, "response_id": SECONDARY},
        ]
        for frame in invalid_frames:
            with self.subTest(frame=frame), self.assertRaises((QwenStreamError, ProviderRejected)):
                list(_parse_sse(events(frame, answer("must not escape")), {}))

    def test_malformed_choices_and_deltas_never_become_a_successful_prefix(self):
        invalid_choices = [None, False, 0, "", {}, [None], [42], [False], [[]],
                           *[[{"delta": value}] for value in (None, False, 0, "", [])],
                           [{"index": 1, "delta": []}]]
        for choices in invalid_choices:
            for response_id in (MESSAGE, SECONDARY):
                with self.subTest(choices=choices, response_id=response_id):
                    frames = events(
                        {"response.created": {"response_id": MESSAGE, "response_index": "0"}},
                        {"response.created": {"response_id": SECONDARY, "response_index": "1"}},
                        answer("prefix"), {"response_id": response_id, "choices": choices},
                    )
                    with self.assertRaises((QwenStreamError, ProviderRejected)):
                        list(_parse_sse(frames, {}))

    def test_missing_or_empty_delta_remains_valid_for_a_finish_frame(self):
        for last in ({"finish_reason": "stop"}, {"delta": {}, "finish_reason": "stop"}):
            with self.subTest(last=last):
                meta = {}
                frames = events(answer("complete"), {"response_id": MESSAGE, "choices": [last]})
                self.assertEqual("".join(_parse_sse(frames, meta)), "complete")
                self.assertEqual(meta, {"message_id": MESSAGE, "finish_reason": "stop"})

    def test_secondary_stop_does_not_discard_or_finish_the_primary(self):
        stops = [
            {"response.stopped": {"response_id": SECONDARY}},
            {"response.stopped": {}, "response_id": SECONDARY},
            {"response.stopped": {"response_id": SECONDARY}, "response_id": SECONDARY},
        ]
        for stop in stops:
            for before_finish in (False, True):
                with self.subTest(stop=stop, before_finish=before_finish):
                    frames = [
                        {"response.created": {"response_id": MESSAGE, "response_index": "0"}},
                        {"response.created": {"response_id": SECONDARY, "response_index": "1"}},
                    ]
                    completion = answer("complete primary", status="finished")
                    frames.extend([stop, completion] if before_finish else [completion, stop])
                    meta = {}
                    self.assertEqual("".join(_parse_sse(events(*frames, done=False), meta)), "complete primary")
                    self.assertEqual(meta, {"message_id": MESSAGE, "finish_reason": "stop"})
        with self.assertRaises((QwenStreamError, ProviderRejected)):
            list(_parse_sse(events(
                {"response.created": {"response_id": MESSAGE, "response_index": "0"}},
                {"response.created": {"response_id": SECONDARY, "response_index": "1"}},
                answer("partial"), stops[0], done=False), {}))

    def test_primary_and_ambiguous_stops_remain_errors_after_primary_finishes(self):
        unknown = "00000000-0000-4000-8000-000000000004"
        stops = [
            {"response.stopped": {"response_id": MESSAGE}},
            {"response.stopped": {"response_id": unknown}},
            {"response.stopped": {}},
            {"response.stopped": None},
            {"response.stopped": False},
            {"response.stopped": {"response_id": SECONDARY}, "response_id": MESSAGE},
            {"response.created": {"response_id": MESSAGE}, "response.stopped": {"response_id": SECONDARY}},
        ]
        for stop in stops:
            with self.subTest(stop=stop), self.assertRaises((QwenStreamError, ProviderRejected)):
                list(_parse_sse(events(
                    {"response.created": {"response_id": MESSAGE, "response_index": "0"}},
                    {"response.created": {"response_id": SECONDARY, "response_index": "1"}},
                    answer("complete primary", status="finished"), stop), {}))

    def test_named_error_is_sanitized_and_never_treated_as_done(self):
        payload = json.dumps({"error": {"code": "invalid_token", "message": "Access rejected"}})
        with self.assertLogs("qwen.client", level="WARNING") as logs:
            with self.assertRaisesRegex(ProviderRejected, "Sign in manually") as failure:
                list(_parse_sse(["event: error", "data: " + payload, ""], {}))
        self.assertEqual(failure.exception.code, "session_expired")
        self.assertEqual(failure.exception.business_code, "invalid_token")
        rendered = " ".join(logs.output)
        self.assertIn("Qwen envelope rejected:", rendered)
        self.assertIn("business_code=invalid_token", rendered)
        self.assertIn("raw_code=invalid_token", rendered)
        self.assertIn("error_keys=code,message", rendered)
        self.assertNotIn("Access rejected", rendered)
        for payload in ("", "[DONE]", "{broken", "{}"):
            with self.subTest(payload=payload), self.assertRaises((QwenStreamError, ProviderRejected)):
                list(_parse_sse(["event: error", "data: " + payload, ""], {}))

    def test_envelope_log_exposes_unknown_short_codes_without_message_text(self):
        from qwen.client import _log_envelope
        with self.assertLogs("qwen.client", level="WARNING") as logs:
            business, raw = _log_envelope(
                {"data": {"code": "RateLimited_V2", "status": 503,
                          "message": "secret=private please wait"}},
                "completion_non_sse",
                content_type="application/json",
            )
        self.assertIsNone(business)
        self.assertEqual(raw, "RateLimited_V2")
        rendered = " ".join(logs.output)
        self.assertIn("content_type=application/json", rendered)
        self.assertIn("data_type=object", rendered)
        self.assertIn("data_keys=code,status,message", rendered)
        self.assertIn("data_fields=code:string,message:string,status:number", rendered)
        self.assertIn("data_key_count=3", rendered)
        self.assertIn("raw_code=RateLimited_V2", rendered)
        self.assertIn("raw_status=503", rendered)
        self.assertIn("code_type=string", rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("please wait", rendered)

    def test_envelope_log_lists_safe_data_field_names_for_empty_known_keys(self):
        from qwen.client import _log_envelope
        with self.assertLogs("qwen.client", level="WARNING") as logs:
            _log_envelope(
                {"data": {"choices": [], "requestId": "abc", "result": {"ok": False}}},
                "completion_non_sse",
                content_type="application/json",
            )
        rendered = " ".join(logs.output)
        self.assertIn("data_keys=requestId", rendered)
        self.assertIn("data_fields=choices:array,requestId:string,result:object", rendered)
        self.assertIn("data_key_count=3", rendered)
        self.assertIn("top_fields=data:object", rendered)
        self.assertNotIn("abc", rendered)

    def test_only_answer_text_is_exposed(self):
        meta = {"chat_id": CHAT}
        stream = events(
            {"response.created": {"response_id": MESSAGE, "chat_id": CHAT}},
            {"choices": [{"delta": {"phase": "think", "content": "hidden reasoning", "status": "finished"}}]},
            answer("hello"), answer(" world", status="finished"),
        )
        self.assertEqual("".join(_parse_sse(stream, meta)), "hello world")
        self.assertEqual(meta["message_id"], MESSAGE)
        self.assertEqual(meta["finish_reason"], "stop")

    def test_eof_and_reasoning_completion_do_not_count_as_success(self):
        for stream in (events(answer("partial"), done=False),
                       events({"choices": [{"delta": {"phase": "think", "status": "finished"}}]}, done=False)):
            with self.assertRaises((QwenStreamError, ProviderRejected)):
                list(_parse_sse(stream, {}))

    def test_errors_and_stopped_responses_are_not_successful(self):
        for error in ({"error": {"message": "failed"}}, {"success": False, "data": {"code": "invalid_token"}},
                      {"response.stopped": {"response_id": MESSAGE}}):
            with self.assertRaises((QwenStreamError, ProviderRejected)):
                list(_parse_sse(events(answer("partial"), error), {}))

    def test_plain_text_daily_quota_notice_is_never_exposed_as_an_answer(self):
        notice = ("You've reached today's Qwen3.8-Max limit. Upgrade your membership "
                  "or switch to another model above to continue.")
        variants = [notice, "  " + notice.replace(" ", "\n") + "  ", notice.replace("'", "’")]
        for text in variants:
            for split in range(len(text) + 1):
                frames = events(answer(text[:split]), answer(text[split:], status="finished"))
                received = []
                with self.subTest(split=split), self.assertRaises(ProviderRejected) as failure:
                    for part in _parse_sse(frames, {}):
                        received.append(part)
                self.assertEqual(failure.exception.code, "quota_exceeded")
                self.assertEqual(received, [])

    def test_quoting_or_discussing_limits_remains_an_ordinary_answer(self):
        notice = ("You've reached today's Qwen3.8-Max limit. Upgrade your membership "
                  "or switch to another model above to continue.")
        for text in ('The site says: ' + notice, '"' + notice + '"',
                     notice + ' This is a quoted example.', "You've reached the end.",
                     'x' * 2048, "You've"):
            with self.subTest(text=text):
                self.assertEqual(''.join(_parse_sse(events(answer(text, status='finished')), {})), text)

    def test_ordinary_answer_still_streams_before_the_terminal_marker(self):
        chunks = iter(_parse_sse(events(answer('normal answer'), answer(' tail', status='finished')), {}))
        self.assertEqual(next(chunks), 'normal answer')
        self.assertEqual(''.join(chunks), ' tail')

    def test_output_limit_is_reported(self):
        meta = {}
        self.assertEqual("".join(_parse_sse(events({"choices": [{"delta": {"content": "cut"}, "finish_reason": "length"}]}), meta)), "cut")
        self.assertEqual(meta["finish_reason"], "length")

    def test_invalid_and_foreign_conversation_ids_are_rejected(self):
        for cid in ("deepseek:2", f"qwen:other:{CHAT}:{MESSAGE}", "qwen:qwen3.8-max:../foreign:invalid"):
            with self.assertRaises(ValueError):
                _decode_cid(cid)

    def test_different_chat_ids_are_rejected(self):
        with self.assertRaises((QwenStreamError, ProviderRejected)):
            list(_parse_sse(events({"response.created": {"chat_id": MESSAGE}}), {"chat_id": CHAT}))

    def test_parallel_responses_are_not_interleaved_or_used_as_the_resume_parent(self):
        other = "00000000-0000-4000-8000-000000000003"
        meta = {"chat_id": CHAT}
        stream = events(
            {"response.created": {"response_id": MESSAGE, "chat_id": CHAT, "response_index": "0"}},
            {"response.created": {"response_id": other, "chat_id": CHAT, "response_index": "1"}},
            answer("hello"),
            {"response_id": other, "choices": [{"delta": {"phase": "answer", "content": "duplicate"}, "finish_reason": "length"}]},
            answer(" world", status="finished"),
            {"response_id": other, "choices": [{"delta": {"phase": "answer", "content": " tail", "status": "finished"}}]},
        )
        self.assertEqual("".join(_parse_sse(stream, meta)), "hello world")
        self.assertEqual(meta["message_id"], MESSAGE)
        self.assertEqual(meta["finish_reason"], "stop")

    def test_secondary_response_arriving_first_is_ignored(self):
        other = "00000000-0000-4000-8000-000000000003"
        meta = {"chat_id": CHAT}
        stream = events(
            {"response.created": {"response_id": other, "chat_id": CHAT, "response_index": "1"}},
            {"response_id": other, "choices": [{"delta": {"content": "not primary"}}]},
            {"response.created": {"response_id": MESSAGE, "chat_id": CHAT, "response_index": "0"}},
            answer("primary", status="finished"),
        )
        self.assertEqual("".join(_parse_sse(stream, meta)), "primary")
        self.assertEqual(meta["message_id"], MESSAGE)

    def test_parallel_content_without_a_response_id_is_rejected(self):
        other = "00000000-0000-4000-8000-000000000003"
        stream = events(
            {"response.created": {"response_id": MESSAGE, "response_index": "0"}},
            {"response.created": {"response_id": other, "response_index": "1"}},
            {"choices": [{"delta": {"phase": "answer", "content": "ambiguous"}}]},
        )
        with self.assertRaisesRegex(QwenStreamError, "Ambiguous"):
            list(_parse_sse(stream, {}))

    def test_transport_model_and_resume_contract(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path == "/api/v2/chats/new":
                return httpx.Response(200, json={"success": True, "data": {"id": CHAT}})
            self.assertEqual(request.url.path, "/api/v2/chat/completions")
            mid = SECONDARY if json.loads(request.content).get("parent_id") else MESSAGE
            text = "\n".join(events({"response.created": {"response_id": mid, "chat_id": CHAT}}, {**answer("OK"), "response_id": mid}))
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        client = QwenClient(session(), access_guard=None, transport=httpx.MockTransport(handler))
        try:
            reply = client.chat("hello", model="qwen3.8-max")
            self.assertEqual(reply.text, "OK")
            self.assertEqual(reply.conversation_id, f"qwen:qwen3.8-max:{CHAT}:{MESSAGE}")
            request = json.loads(requests[-1].content)
            self.assertEqual(request["model"], "qwen3.8-max")
            self.assertFalse(request["messages"][0]["feature_config"]["auto_search"])
            client.chat("continue", reply.conversation_id)
            self.assertEqual(len([r for r in requests if r.url.path.endswith("/chats/new")]), 1)
            self.assertEqual(json.loads(requests[-1].content)["parent_id"], MESSAGE)
        finally:
            client.close()

    def test_non_sse_business_error_is_read_and_preserves_auth_rejection(self):
        def handler(request):
            if request.url.path == "/api/v2/chats/new":
                return httpx.Response(200, json={"success": True, "data": {"id": CHAT}})
            payload = json.dumps({"success": False, "data": {"code": "invalid_token", "message": "Sign in again"}}).encode()
            return httpx.Response(200, stream=httpx.ByteStream(payload), headers={"content-type": "application/json"})
        client = QwenClient(session(), access_guard=None, transport=httpx.MockTransport(handler))
        try:
            with self.assertLogs("qwen.client", level="WARNING") as logs:
                with self.assertRaisesRegex(ProviderRejected, "Sign in manually"):
                    client.chat("hello")
            rendered = " ".join(logs.output)
            self.assertIn("phase=completion_non_sse", rendered)
            self.assertIn("content_type=application/json", rendered)
            self.assertIn("raw_code=invalid_token", rendered)
        finally:
            client.close()
