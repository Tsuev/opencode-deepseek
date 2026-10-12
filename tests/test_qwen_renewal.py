"""Offline renewal fixtures: synthetic credentials, isolated disk and guards."""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import httpx

from providers.access import AccessGuard, ProviderRejected, current_attempt
from qwen.auth import LoginRequired, Session
from qwen.renewal import AUTH_REFRESH_URL, renew_session

NOW = 2_000_000_000


def jwt(**changes):
    payload = {"id": "synthetic-account", "type": "access", "exp": NOW - 10, **changes}
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return encode({"alg": "HS256"}) + "." + encode(payload) + ".c3ludGhldGlj"


def cached():
    return Session(jwt(), [
        {"name": "refresh_token", "value": "synthetic-refresh", "domain": "auth.qwen.ai",
         "path": "/api/v2/auths", "secure": True, "httpOnly": True, "expires": NOW + 5000},
        {"name": "chat-only", "value": "synthetic-chat", "domain": "chat.qwen.ai",
         "path": "/", "secure": True},
    ], "synthetic-user-agent", NOW - 900, expires_at=NOW - 10)


def success(**changes):
    return {"success": True, "data": {"access_token": jwt(exp=NOW + 900),
                                      "expires_at": NOW + 900, **changes}}


class QwenRenewalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "session.json"
        self.guard = AccessGuard(Path(self.temp.name) / "pauses.json", interval=0)
        self.original = cached()
        self.original.save(self.path)
        self.requests = []
        self.respond = lambda request: httpx.Response(200, json=success())
        clock = patch("qwen.renewal.time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def handler(self, request):
        self.requests.append(request)
        self.assertEqual(str(request.url), AUTH_REFRESH_URL)
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.content, b"")
        attempt = current_attempt("qwen")
        self.assertIsNotNone(attempt)
        self.assertTrue(attempt.dispatched)
        return self.respond(request)

    def run_renewal(self, cancelled=None):
        with self.guard.attempt("qwen", cancelled):
            return renew_session(self.path, transport=httpx.MockTransport(self.handler))

    def assert_preflight_failure(self, state):
        if state is None:
            self.path.unlink(missing_ok=True)
        else:
            state.save(self.path)
        with self.assertRaises(LoginRequired):
            self.run_renewal()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.guard.status(), {})

    def assert_post_dispatch_failure(self, code="upstream_failure"):
        before = self.path.read_bytes()
        with patch.object(self.guard, "reject", wraps=self.guard.reject) as pause:
            with self.assertRaises(ProviderRejected) as failure:
                self.run_renewal()
            self.assertEqual(pause.call_count, 1)
        self.assertEqual(failure.exception.code, code)
        self.assertNotIn("synthetic", str(failure.exception))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(AccessGuard(self.guard.path, interval=0).status()["qwen"], code)
        with self.assertRaises(ProviderRejected):
            self.run_renewal()
        self.assertEqual(len(self.requests), 1)

    def test_one_fixed_auth_request_preserves_headers_and_scoped_cookies(self):
        renewed = self.run_renewal()
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.headers["user-agent"], self.original.user_agent)
        self.assertEqual(request.headers["origin"], "https://chat.qwen.ai")
        self.assertEqual(request.headers["referer"], "https://chat.qwen.ai/")
        self.assertEqual(request.headers["x-request-origin"], "https://chat.qwen.ai")
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(request.headers["version"], "0.3.14")
        self.assertEqual(request.headers["source"], "web")
        self.assertEqual(uuid.UUID(request.headers["x-request-id"]).version, 4)
        self.assertNotIn("sec-fetch-site", request.headers)
        self.assertEqual(request.headers["accept-language"], "en-US,en;q=0.9")
        self.assertRegex(request.headers["timezone"],
                         r"^[A-Za-z]{3} [A-Za-z]{3} [0-9]{2} [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT[+-][0-9]{4}$")
        self.assertEqual(request.headers["cookie"], "refresh_token=synthetic-refresh")
        self.assertNotIn("authorization", request.headers)
        self.assertTrue(renewed.usable)
        self.assertEqual(renewed.user_agent, self.original.user_agent)
        self.assertEqual(Session.load(self.path), renewed)
        self.assertEqual(self.guard.status(), {})

    def test_timezone_header_uses_current_host_local_datetime_and_offset(self):
        for minutes, suffix in ((570, "+0930"), (-210, "-0330")):
            with self.subTest(offset=suffix):
                self.original.save(self.path)
                self.requests.clear()
                local = datetime(2026, 10, 10, 13, 14, 15,
                                 tzinfo=timezone(timedelta(minutes=minutes)))
                with patch("qwen.renewal.datetime") as clock:
                    clock.now.return_value.astimezone.return_value = local
                    self.run_renewal()
                    clock.now.assert_called_once_with()
                    clock.now.return_value.astimezone.assert_called_once_with()
                self.assertEqual(self.requests[0].headers["timezone"],
                                 "Sat Oct 10 2026 13:14:15 GMT" + suffix)
                self.assertEqual(self.requests[0].headers["accept-language"], "en-US,en;q=0.9")

    def test_reloads_disk_and_returns_already_usable_session_without_http(self):
        valid = cached()
        valid.token, valid.expires_at = jwt(exp=NOW + 900), NOW + 900
        valid.save(self.path)
        self.assertEqual(self.run_renewal(), valid)
        self.assertEqual(self.requests, [])

    def test_missing_or_malformed_cache_requires_manual_login_without_http(self):
        self.assert_preflight_failure(None)
        self.path.write_text("{invalid")
        with self.assertRaises(LoginRequired):
            self.run_renewal()
        self.assertEqual(self.requests, [])

    def test_jwt_metadata_and_local_expiry_are_required_before_http(self):
        for token in ("malformed", jwt(id=""), jwt(id=None), jwt(type=""), jwt(exp=None),
                      jwt(exp=True), jwt(exp=float("nan")), jwt(exp=NOW + 900), "x" * 32769):
            with self.subTest(token_case=token[:20]):
                state = cached()
                state.token = token
                self.assert_preflight_failure(state)
        state = cached()
        state.expires_at = float("nan")
        self.assert_preflight_failure(state)

    def test_refresh_cookie_must_be_secure_unexpired_and_match_fixed_host_path(self):
        for change in ({"name": "other"}, {"value": ""}, {"secure": False},
                       {"domain": "chat.qwen.ai"}, {"domain": "qwen.ai"},
                       {"domain": ".evil.qwen.ai"}, {"domain": "evil.example"},
                       {"path": "/api/v2/auths/refresh/"}, {"path": "/other"},
                       {"expires": NOW - 1}):
            with self.subTest(change=change):
                state = cached()
                state.cookies[0].update(change)
                self.assert_preflight_failure(state)
        state = cached()
        state.cookies = []
        self.assert_preflight_failure(state)

    def test_parent_domain_session_cookie_can_renew(self):
        state = cached()
        state.cookies[0].update(domain=".qwen.ai", expires=-1, path="/")
        state.save(self.path)
        self.assertTrue(self.run_renewal().usable)

    def test_http_denials_pause_once_and_prevent_a_second_call(self):
        for status, code in ((401, "session_expired"), (403, "access_denied"), (429, "quota_exceeded")):
            with self.subTest(status=status):
                isolated = AccessGuard(Path(self.temp.name) / f"pause-{status}.json", interval=0)
                with patch.object(self, "guard", isolated):
                    self.requests.clear()
                    self.respond = lambda request, status=status: httpx.Response(status, text="synthetic-secret")
                    self.assert_post_dispatch_failure(code)

    def test_business_refusal_is_sanitized_and_not_replayed(self):
        self.respond = lambda request: httpx.Response(200, json={"success": False,
            "data": {"code": "muted", "message": "synthetic-secret"}})
        self.assert_post_dispatch_failure("account_restricted")

    def test_response_shape_diagnostics_distinguish_envelope_failures_without_secrets(self):
        cases = [
            ({"synthetic-private-key": "synthetic-private-value"}, "missing_success", "null"),
            ({"success": False, "data": None}, "success_false", "null"),
            ({"success": True, "data": "synthetic-private-value"}, "invalid_data", "string"),
            ({"success": "synthetic-private-value", "data": []}, "invalid_success", "array"),
            (["synthetic-private-key", "synthetic-private-value"], "missing_success", "null"),
        ]
        for index, (payload, reason, data_type) in enumerate(cases):
            with self.subTest(reason=reason, index=index):
                isolated = AccessGuard(Path(self.temp.name) / f"shape-{index}.json", interval=0)
                with patch.object(self, "guard", isolated):
                    self.requests.clear()
                    self.respond = lambda request, payload=payload: httpx.Response(200, json=payload,
                        headers={"synthetic-private-header": "synthetic-private-value"})
                    with self.assertLogs("qwen.renewal", level="WARNING") as output:
                        self.assert_post_dispatch_failure()
                    rendered = " ".join(output.output)
                    self.assertIn("reason=" + reason, rendered)
                    self.assertIn("data_type=" + data_type, rendered)
                    self.assertIn("success_present=", rendered)
                    self.assertIn("success_true=", rendered)
                    self.assertNotIn("synthetic", rendered)

    def test_response_diagnostics_only_emit_allowlisted_numeric_business_codes(self):
        cases = [(401, "401"), (403, "403"), (429, "429"),
                 ("401", "401"), ("403", "403"), ("429", "429"),
                 (451, "None"), (123456, "None"), ("123456", "None"),
                 (999999, "None"), ("000403", "None"), (0, "None"),
                 (1000000, "None"), (-1, "None"), (True, "None"),
                 (401.5, "None"), ("synthetic-private-code", "None"),
                 ({"synthetic-private-key": "synthetic-private-value"}, "None")]
        for index, (code, expected) in enumerate(cases):
            with self.subTest(index=index):
                isolated = AccessGuard(Path(self.temp.name) / f"shape-code-{index}.json", interval=0)
                with patch.object(self, "guard", isolated):
                    self.requests.clear()
                    self.respond = lambda request, code=code: httpx.Response(200, json={
                        "success": False, "data": {"code": code, "message": "synthetic-private-value"}})
                    with self.assertLogs("qwen.renewal", level="WARNING") as output:
                        pause = {"401": "session_expired", "403": "access_denied",
                                 "429": "quota_exceeded"}.get(str(code), "upstream_failure")
                        self.assert_post_dispatch_failure(pause)
                    shape = next(line for line in output.output if "response rejected:" in line)
                    self.assertTrue(shape.endswith("business_code=" + expected), shape)
                    self.assertNotIn("synthetic", " ".join(output.output))

    def test_known_business_codes_are_logged_canonically_without_changing_rejections(self):
        cases = [("Unauthorized", "unauthorized", "session_expired"),
                 ("invalid token", "invalid token", "upstream_failure"),
                 ("FORBIDDEN", "forbidden", "access_denied"),
                 ("access_prohibited", "access_prohibited", "upstream_failure"),
                 ("account_pending", "ACCOUNT_PENDING", "upstream_failure"),
                 ("data_inspection_failed", "data_inspection_failed", "upstream_failure"),
                 ("Bad_Request", "Bad_Request", "upstream_failure"),
                 ("Bad_Request synthetic-private-value", "None", "upstream_failure")]
        for index, (code, expected, pause) in enumerate(cases):
            with self.subTest(index=index):
                isolated = AccessGuard(Path(self.temp.name) / f"known-code-{index}.json", interval=0)
                with patch.object(self, "guard", isolated):
                    self.requests.clear()
                    self.respond = lambda request, code=code: httpx.Response(200, json={
                        "success": False, "data": {"code": code, "message": "synthetic-private-value"}})
                    with self.assertLogs("qwen.renewal", level="WARNING") as output:
                        self.assert_post_dispatch_failure(pause)
                    shape = next(line for line in output.output if "response rejected:" in line)
                    self.assertTrue(shape.endswith("business_code=" + expected), shape)
                    self.assertNotIn("synthetic", " ".join(output.output))

    def test_numeric_business_codes_map_to_the_same_guard_pause(self):
        for status, code in ((401, "session_expired"), ("403", "access_denied"), (429, "quota_exceeded")):
            with self.subTest(status=status):
                isolated = AccessGuard(Path(self.temp.name) / f"biz-{status}.json", interval=0)
                with patch.object(self, "guard", isolated):
                    self.requests.clear()
                    self.respond = lambda request, status=status: httpx.Response(200,
                        json={"success": False, "data": {"code": status}})
                    self.assert_post_dispatch_failure(code)

    def test_network_failure_is_sanitized_and_not_replayed(self):
        def fail(request):
            raise httpx.ReadTimeout("synthetic-secret", request=request)
        self.respond = fail
        self.assert_post_dispatch_failure()

    def test_redirect_is_rejected_without_contacting_location(self):
        self.respond = lambda request: httpx.Response(302, headers={"Location": "https://evil.example/model"})
        self.assert_post_dispatch_failure()

    def test_response_account_type_and_expiries_fail_closed(self):
        bad = [success(access_token=jwt(id="other", exp=NOW + 900)),
               success(access_token=jwt(type="refresh", exp=NOW + 900)),
               success(access_token="malformed"), success(access_token="x" * 32769),
               success(access_token=jwt(exp=NOW + 30)), success(expires_at=NOW + 60),
               success(expires_at=NOW - 20), success(expires_at=0),
               success(expires_at=True),
               success(expires_at=float("nan")), success(expires_at="invalid"),
               {"success": 1, "data": success()["data"]}, {"success": True, "data": []}]
        for index, payload in enumerate(bad):
            with self.subTest(index=index):
                isolated = AccessGuard(Path(self.temp.name) / f"bad-{index}.json", interval=0)
                with patch.object(self, "guard", isolated):
                    self.requests.clear()
                    self.respond = lambda request, payload=payload: httpx.Response(200, content=json.dumps(payload))
                    self.assert_post_dispatch_failure()

    def test_malformed_oversized_or_duplicate_json_is_rejected(self):
        for index, body in enumerate((b"{", b" " * 65537,
                b'{"success":true,"success":false,"data":{}}')):
            with self.subTest(index=index):
                isolated = AccessGuard(Path(self.temp.name) / f"json-{index}.json", interval=0)
                with patch.object(self, "guard", isolated):
                    self.requests.clear()
                    self.respond = lambda request, body=body: httpx.Response(200, content=body)
                    self.assert_post_dispatch_failure()

    def test_response_and_jwt_expiry_use_the_safe_minimum(self):
        self.respond = lambda request: httpx.Response(200, json=success(expires_at=NOW + 800))
        self.assertEqual(self.run_renewal().expires_at, NOW + 800)

    def test_null_response_expiry_uses_validated_jwt_expiry_like_the_website(self):
        self.respond = lambda request: httpx.Response(200, json=success(expires_at=None))
        self.assertEqual(self.run_renewal().expires_at, NOW + 900)

    def test_failure_diagnostics_omit_exception_details_and_credentials(self):
        def fail(request):
            raise httpx.ReadTimeout("synthetic-private-cookie-and-token", request=request)
        self.respond = fail
        with self.assertLogs("qwen.renewal", level="WARNING") as output:
            self.assert_post_dispatch_failure()
        rendered = " ".join(output.output)
        self.assertIn("exception=ReadTimeout", rendered)
        self.assertNotIn("synthetic", rendered)

    def test_missing_server_expiry_uses_the_validated_fresh_jwt_expiry(self):
        payload = success()
        del payload["data"]["expires_at"]
        self.respond = lambda request: httpx.Response(200, json=payload)
        renewed = self.run_renewal()
        self.assertEqual(renewed.expires_at, NOW + 900)
        self.assertTrue(renewed.usable)
        self.assertEqual(len(self.requests), 1)

    def test_missing_server_expiry_still_requires_fresh_bound_jwt_metadata(self):
        for index, token in enumerate((jwt(), jwt(exp=NOW + 60), jwt(id="other", exp=NOW + 900),
                jwt(type="refresh", exp=NOW + 900), jwt(id=None, exp=NOW + 900),
                jwt(exp=float("nan")), "malformed")):
            with self.subTest(index=index):
                isolated = AccessGuard(Path(self.temp.name) / f"no-expiry-{index}.json", interval=0)
                with patch.object(self, "guard", isolated):
                    self.requests.clear()
                    self.respond = lambda request, token=token: httpx.Response(200,
                        json={"success": True, "data": {"access_token": token}})
                    self.assert_post_dispatch_failure()

    def test_set_cookie_rotation_preserves_other_scoped_cookies(self):
        self.respond = lambda request: httpx.Response(200, json=success(), headers=[
            ("Set-Cookie", "refresh_token=rotated; Path=/api/v2/auths; Secure; HttpOnly"),
            ("Set-Cookie", "auth-new=fresh; Path=/; Secure"),
            ("Set-Cookie", "foreign=secret; Domain=evil.example; Path=/; Secure"),
        ])
        renewed = self.run_renewal()
        cookies = {cookie["name"]: cookie for cookie in renewed.cookies}
        self.assertEqual(cookies["refresh_token"]["value"], "rotated")
        self.assertTrue(cookies["refresh_token"]["secure"])
        self.assertTrue(cookies["refresh_token"]["httpOnly"])
        self.assertEqual(cookies["refresh_token"]["path"], "/api/v2/auths")
        self.assertEqual(cookies["chat-only"]["domain"], "chat.qwen.ai")
        self.assertEqual(cookies["auth-new"]["value"], "fresh")
        self.assertNotIn("foreign", cookies)

    def test_missing_wrong_or_dispatched_attempt_forbids_http(self):
        with self.assertRaises(ProviderRejected):
            renew_session(self.path, transport=httpx.MockTransport(self.handler))
        with self.guard.attempt("deepseek"):
            with self.assertRaises(ProviderRejected):
                renew_session(self.path, transport=httpx.MockTransport(self.handler))
        with self.guard.attempt("qwen") as attempt:
            attempt.dispatch()
            with self.assertRaises(ProviderRejected):
                renew_session(self.path, transport=httpx.MockTransport(self.handler))
        self.assertEqual(self.requests, [])

    def test_cancellation_before_dispatch_sends_nothing_and_does_not_pause(self):
        def cancelled():
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            self.run_renewal(cancelled)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.guard.status(), {})

    def test_cancellation_after_response_prevents_save_and_latches_pause(self):
        cancel = False
        def cancelled():
            if cancel:
                raise asyncio.CancelledError()
        def response(request):
            nonlocal cancel
            cancel = True
            return httpx.Response(200, json=success())
        self.respond = response
        with self.assertRaises(asyncio.CancelledError):
            self.run_renewal(cancelled)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(Session.load(self.path), self.original)
        self.assertEqual(self.guard.status()["qwen"], "upstream_failure")

    def test_directory_sync_failure_after_auth_is_terminal(self):
        with patch("qwen.auth._sync_session_directory", side_effect=OSError("owned disk failure")):
            with self.assertRaises(ProviderRejected):
                self.run_renewal()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.guard.status()["qwen"], "upstream_failure")
        with self.assertRaises(ProviderRejected):
            self.run_renewal()
        self.assertEqual(len(self.requests), 1)

    def test_cancellation_between_small_chunks_stops_reading_without_save(self):
        cancel = False
        chunks_read = []
        class Stream(httpx.SyncByteStream):
            def __iter__(stream):
                nonlocal cancel
                chunks_read.append(1)
                yield b'{"success":'
                cancel = True
                chunks_read.append(2)
                yield b'true,"data":'
                chunks_read.append(3)
                yield b'{} }'
        def cancelled():
            if cancel:
                raise asyncio.CancelledError()
        self.respond = lambda request: httpx.Response(200, stream=Stream())
        with self.assertRaises(asyncio.CancelledError):
            self.run_renewal(cancelled)
        self.assertEqual(chunks_read, [1, 2])
        self.assertEqual(Session.load(self.path), self.original)
        self.assertEqual(self.guard.status()["qwen"], "upstream_failure")

    def test_total_body_deadline_stops_small_chunk_drip(self):
        elapsed = [0]
        chunks_read = []
        class Stream(httpx.SyncByteStream):
            def __iter__(stream):
                for index in range(10):
                    elapsed[0] += 10
                    chunks_read.append(index)
                    yield b" "
        self.respond = lambda request: httpx.Response(200, stream=Stream())
        with patch("qwen.renewal.time.monotonic", side_effect=lambda: elapsed[0]):
            self.assert_post_dispatch_failure()
        self.assertEqual(chunks_read, [0, 1, 2])

    def test_disk_rotation_during_dispatch_validation_prevents_http(self):
        with self.guard.attempt("qwen") as attempt:
            original_dispatch = attempt.dispatch
            def rotate(**kwargs):
                newer = cached()
                newer.token, newer.expires_at = jwt(exp=NOW + 900), NOW + 900
                newer.save(self.path)
                return original_dispatch(**kwargs)
            with patch.object(attempt, "dispatch", side_effect=rotate), self.assertRaises(ProviderRejected):
                renew_session(self.path, transport=httpx.MockTransport(self.handler))
        self.assertEqual(self.requests, [])

    def test_cookie_expiry_during_pacing_prevents_http(self):
        state = cached()
        state.cookies[0]["expires"] = NOW + 1
        state.save(self.path)
        with self.guard.attempt("qwen") as attempt:
            original_dispatch = attempt.dispatch
            def expire(**kwargs):
                with patch("qwen.renewal.time.time", return_value=NOW + 2):
                    return original_dispatch(**kwargs)
            with patch.object(attempt, "dispatch", side_effect=expire), self.assertRaises(ProviderRejected):
                renew_session(self.path, transport=httpx.MockTransport(self.handler))
        self.assertEqual(self.requests, [])

    def test_cas_race_cannot_overwrite_manual_signin(self):
        newer = cached()
        newer.token, newer.expires_at = jwt(id="manual-signin", exp=NOW + 1200), NOW + 1200
        def rotate(request):
            newer.save(self.path)
            return httpx.Response(200, json=success())
        self.respond = rotate
        with self.assertRaises(ProviderRejected):
            self.run_renewal()
        self.assertEqual(Session.load(self.path), newer)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.guard.status()["qwen"], "upstream_failure")

    def test_revoked_guard_owner_before_save_cannot_write_or_poison_new_owner(self):
        replacement = None
        def revoke(request):
            nonlocal replacement
            other = AccessGuard(self.guard.path, interval=0)
            other.resume("qwen")
            replacement = other.begin("qwen")
            return httpx.Response(200, json=success())
        self.respond = revoke
        with self.assertRaises(ProviderRejected):
            self.run_renewal()
        self.assertEqual(Session.load(self.path), self.original)
        replacement.check()
        replacement.complete()
        self.assertEqual(self.guard.status(), {})
