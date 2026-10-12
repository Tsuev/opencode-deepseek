import base64
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from integrations.kimi_session import CaptureServer, handler_for, validate_capture, render_script, LIMIT


def payload(expiry=None):
    claims = {'exp': time.time() + 3600 if expiry is None else expiry, 'sub': 'owned-subject'}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return {'origin': 'https://www.kimi.ai', 'token': 'e30.' + encoded + '.signature',
            'user_agent': 'owned-fixture', 'refresh_token': 'owned-refresh'}


class KimiSessionTests(unittest.TestCase):
    def test_local_expiry_check_rejects_bad_and_unsafe_credentials(self):
        values = [payload(True), payload(float('inf')), payload(10 ** 1000)]
        for token in ('not-a-jwt', 'a.b.c\r\nSecret: value', 'a.b.c\0', 'a.W10.c'):
            values.append({**payload(), 'token': token})
        values += [{**payload(), 'origin': 'https://www.kimi.ai.evil.invalid'},
                   {**payload(), 'origin': 'http://www.kimi.ai'},
                   {**payload(), 'user_agent': 'private\r\nheader'},
                   {**payload(), 'extra': 'must not be stored'}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_capture(value)

    def test_expired_access_can_be_captured_with_refresh_for_guarded_renewal(self):
        value = validate_capture(payload(time.time() - 1))
        self.assertLess(value['expires_at'], time.time())
        self.assertEqual(value['refresh_token'], 'owned-refresh')

    @contextmanager
    def endpoint(self, expired=False):
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / 'private/session.json'
            handler = handler_for('owned-nonce', destination,
                                  time.monotonic() + (-1 if expired else 60))
            with CaptureServer(('127.0.0.1', 0), handler) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',
                                      trust_env=False, timeout=3) as client:
                        yield client, destination, server
                finally:
                    server.shutdown()
                    thread.join(3)

    def test_single_explicit_transfer_is_private_and_cannot_be_replayed(self):
        with self.endpoint() as (client, destination, server):
            first = payload()
            headers = {'Authorization': 'Bearer owned-nonce'}
            reply = client.post('/capture', headers=headers, json=first)
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.json(), {})
            self.assertNotIn('access-control-allow-origin', reply.headers)
            self.assertEqual(json.loads(destination.read_text())['token'], first['token'])
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(destination.parent.stat().st_mode), 0o700)
            saved = destination.read_bytes()
            self.assertEqual(client.post('/capture', headers=headers, json=payload()).status_code, 403)
            self.assertEqual(destination.read_bytes(), saved)
            self.assertTrue(server.captured)

    def test_wrong_nonce_origin_path_or_expired_capture_cannot_write(self):
        for expired in (False, True):
            with self.endpoint(expired) as (client, destination, _):
                for path, headers in (
                    ('/capture', {}),
                    ('/capture', {'Authorization': 'Bearer wrong'}),
                    ('/capture', {'Authorization': 'Bearer owned-nonce', 'Origin': 'https://evil.invalid'}),
                    ('/capture?nonce=owned-nonce', {'Authorization': 'Bearer owned-nonce'}),
                ):
                    self.assertEqual(client.post(path, headers=headers, json=payload()).status_code, 403)
                if expired:
                    self.assertEqual(client.post('/capture', headers={'Authorization': 'Bearer owned-nonce'},
                                                 json=payload()).status_code, 403)
                self.assertFalse(destination.exists())

    def test_safari_extension_origin_still_requires_the_one_time_nonce(self):
        origin = 'safari-web-extension://00000000-0000-0000-0000-000000000001'
        with self.endpoint() as (client, destination, _):
            for value in ('null', origin + '.evil.invalid', origin + '/path',
                          'https://00000000-0000-0000-0000-000000000001'):
                reply = client.post('/capture', headers={'Origin': value,
                                    'Authorization': 'Bearer owned-nonce'}, json=payload())
                self.assertEqual(reply.status_code, 403)
            reply = client.post('/capture', headers={'Origin': origin}, json=payload())
            self.assertEqual(reply.status_code, 403)
            self.assertFalse(destination.exists())
            reply = client.post('/capture', headers={'Origin': origin,
                                'Authorization': 'Bearer owned-nonce'}, json=payload())
            self.assertEqual(reply.status_code, 200)
            self.assertNotIn('access-control-allow-origin', reply.headers)

    def test_oversized_invalid_and_failed_writes_do_not_publish_credentials(self):
        with self.endpoint() as (client, destination, server):
            headers = {'Authorization': 'Bearer owned-nonce', 'Content-Type': 'application/json'}
            for value in (b'{', b'x' * (LIMIT + 1)):
                self.assertEqual(client.post('/capture', headers=headers, content=value).status_code, 400)
            with patch('providers.kimi_auth.write_private_json', side_effect=OSError('private-secret')):
                reply = client.post('/capture', headers=headers, json=payload())
                self.assertEqual(reply.status_code, 500)
                self.assertNotIn('private-secret', reply.text)
            self.assertFalse(destination.exists())
            self.assertFalse(getattr(server, 'captured', False))

    def test_only_private_rendered_script_receives_pairing_material(self):
        rendered = render_script('owned-nonce', 23456)
        self.assertNotIn('__CAPTURE_NONCE__', rendered)
        self.assertNotIn('__CAPTURE_PORT__', rendered)
        self.assertNotIn('__CAPTURE_EXPIRES_MS__', rendered)
        self.assertIn('owned-nonce', rendered)

    def test_cli_requires_actual_extension_save_folder_before_starting(self):
        with patch('sys.argv', ['kimi_session']), self.assertRaises(SystemExit) as stopped:
            from integrations.kimi_session import main
            main()
        self.assertEqual(stopped.exception.code, 2)

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for the isolated userscript fixture')
    def test_userscript_requires_one_trusted_click_before_reading_credentials(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(['node', str(root / 'tests/kimi_session_fixture.cjs'),
                                 str(root / 'browser/kimi-session-capture.user.js')],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
