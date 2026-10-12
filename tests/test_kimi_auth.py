import base64
import json
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from providers.access import AccessGuard, ProviderRejected
from providers.kimi_auth import (LoginRequired, REFRESH_URL, save_capture,
                                 usable_session, validate_session)


def session(expiry=None, subject='owned-subject', refresh='owned-refresh'):
    claims = {'sub': subject, 'exp': time.time() + 600 if expiry is None else expiry,
              'iss': 'owned-issuer', 'app_id': 'owned-app'}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return {'origin': 'https://www.kimi.ai', 'token': 'e30.' + encoded + '.signature',
            'refresh_token': refresh, 'user_agent': 'owned-fixture'}


class KimiAuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'private/session.json'
        self.guard = AccessGuard(Path(self.directory.name) / 'guard.json', interval=0)
        self.old = session(time.time() - 1)
        save_capture(self.path, validate_session(self.old, require_fresh=False))
        self.requests = []

    def factory(self, handler):
        def create(**options):
            self.assertIs(options['trust_env'], False)
            self.assertIs(options['follow_redirects'], False)
            def checked(request):
                self.requests.append(request)
                self.assertEqual(str(request.url), REFRESH_URL)
                self.assertEqual(request.method, 'POST')
                self.assertNotIn('authorization', request.headers)
                self.assertEqual(json.loads(request.content), {'refresh_token': 'owned-refresh'})
                return handler(request)
            return httpx.Client(transport=httpx.MockTransport(checked), **options)
        return create

    def run_refresh(self, handler, renew=True):
        with self.guard.attempt('kimi') as attempt:
            return usable_session(attempt, path=self.path, renew=renew, client_factory=self.factory(handler))

    @staticmethod
    def response(value):
        return httpx.Response(200, json={'access_token': value['token'], 'refresh_token': value['refresh_token']})

    def test_refresh_once_persists_rotation_and_next_request_uses_cache(self):
        new = session(refresh='rotated-refresh')
        result = self.run_refresh(lambda request: self.response(new))
        self.assertEqual(result['token'], new['token'])
        self.assertEqual(json.loads(self.path.read_text())['refresh_token'], 'rotated-refresh')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.run_refresh(lambda request: self.fail('Fresh session must not renew'))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.guard.status(), {})

    def test_standard_proto_json_camelcase_and_explicit_refresh(self):
        fresh = session()
        save_capture(self.path, validate_session(fresh))
        new = session(refresh='rotated-refresh')
        with self.guard.attempt('kimi') as attempt:
            result = usable_session(attempt, self.path, True,
                self.factory(lambda request: httpx.Response(200, json={
                    'accessToken': new['token'], 'refreshToken': new['refresh_token']})),
                force_refresh=True)
        self.assertEqual(result['refresh_token'], 'rotated-refresh')
        self.assertEqual(len(self.requests), 1)

    def test_missing_expired_or_renew_disabled_never_dispatches(self):
        for value in (None, self.old, {**self.old, 'refresh_token': ''}):
            if value is None:
                self.path.unlink(missing_ok=True)
            else:
                self.path.write_text(json.dumps(value))
            with self.assertRaises(LoginRequired):
                self.run_refresh(lambda request: self.fail('No network expected'), renew=False)
            self.assertEqual(self.guard.status(), {})
        self.assertEqual(self.requests, [])

    def test_refusal_redirect_timeout_invalid_rotation_pause_without_retry(self):
        cases = [
            lambda request: httpx.Response(401), lambda request: httpx.Response(403),
            lambda request: httpx.Response(429),
            lambda request: httpx.Response(302, headers={'Location': 'https://evil.invalid'}),
            lambda request: httpx.Response(200, json={'access_token': 'bad', 'refresh_token': 'secret'}),
            lambda request: self.response(session(subject='another-account')),
            lambda request: self.response(session(time.time() + 20)),
            lambda request: httpx.Response(200, content=b'x' * 65537, headers={'Content-Type': 'application/json'}),
        ]
        def timeout(request):
            raise httpx.ReadTimeout('private-secret', request=request)
        cases.append(timeout)
        for handler in cases:
            with self.subTest(handler=handler):
                # Isolate failures, never resume the same rejected session.
                self.guard = AccessGuard(None, interval=0)
                before = self.path.read_bytes()
                count = len(self.requests)
                with self.assertRaises(ProviderRejected) as rejected:
                    self.run_refresh(handler)
                self.assertNotIn('private-secret', str(rejected.exception))
                self.assertEqual(len(self.requests), count + 1)
                self.assertEqual(self.path.read_bytes(), before)
                with self.assertRaises(ProviderRejected):
                    self.run_refresh(handler)
                self.assertEqual(len(self.requests), count + 1)

    def test_failed_persistence_stops_before_using_rotated_access_token(self):
        with patch('providers.kimi_auth._persist', side_effect=OSError('private-secret')):
            with self.assertRaises(ProviderRejected):
                self.run_refresh(lambda request: self.response(session()))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.guard.status()['kimi'], 'upstream_failure')

    def test_cancellation_after_exchange_preserves_pair_but_does_not_return_access(self):
        cancelled = False
        def check():
            if cancelled:
                raise RuntimeError('Cancelled')
        def handler(request):
            nonlocal cancelled
            cancelled = True
            return self.response(session(refresh='rotated-refresh'))
        with self.assertRaises(RuntimeError), self.guard.attempt('kimi', check) as attempt:
            usable_session(attempt, self.path, True, self.factory(handler))
        self.assertEqual(json.loads(self.path.read_text())['refresh_token'], 'rotated-refresh')
        self.assertEqual(self.guard.status()['kimi'], 'upstream_failure')

    def test_manual_capture_waits_for_rotation_then_wins(self):
        entered, captured = threading.Event(), threading.Event()
        manual = validate_session(session(subject='manual-account'))
        worker = None
        def handler(request):
            nonlocal worker
            def save():
                entered.set()
                save_capture(self.path, manual)
                captured.set()
            worker = threading.Thread(target=save)
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(captured.wait(.05))
            return self.response(session(refresh='rotated-refresh'))
        self.run_refresh(handler)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(json.loads(self.path.read_text()), manual)


if __name__ == '__main__':
    unittest.main()
