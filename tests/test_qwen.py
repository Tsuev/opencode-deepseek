import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import httpx

from deepseek.auth import Session as DeepSeekSession
from qwen.auth import LoginRequired, Session, _capture, get_session, login
from qwen.client import QwenClient, QwenStreamError, _decode_cid, _parse_sse
from providers.access import ProviderRejected

CHAT = "00000000-0000-4000-8000-000000000001"
MESSAGE = "00000000-0000-4000-8000-000000000002"
SECONDARY = "00000000-0000-4000-8000-000000000003"


def session():
    return Session("fake-qwen-token", [], "test-agent", time.time(), time.time() + 900)


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
        with self.assertRaisesRegex(ProviderRejected, "Sign in manually"):
            list(_parse_sse(["event: error", "data: " + payload, ""], {}))
        for payload in ("", "[DONE]", "{broken", "{}"):
            with self.subTest(payload=payload), self.assertRaises((QwenStreamError, ProviderRejected)):
                list(_parse_sse(["event: error", "data: " + payload, ""], {}))

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
            text = "\n".join(events({"response.created": {"response_id": MESSAGE, "chat_id": CHAT}}, answer("OK")))
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
            with self.assertRaisesRegex(ProviderRejected, "Sign in manually"):
                client.chat("hello")
        finally:
            client.close()
