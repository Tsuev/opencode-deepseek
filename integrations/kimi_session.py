"""Explicit, short-lived transfer from the user's signed-in Kimi tab.

This command does not send model requests, refresh credentials, or clear pauses.
It runs separately from the API and deletes its private userscript on exit.
"""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import time

from providers.kimi_auth import ORIGINS, save_capture, validate_session
from settings import ROOT

LIMIT = 65536
CAPTURE_SECONDS = 600


def allowed_origin(origin):
    # GM.xmlHttpRequest is sent by the extension, not by the page origin.
    # This admits Safari extension origins only; the independent one-time
    # nonce remains mandatory, and no CORS access is granted to websites.
    return (origin is None or origin in ORIGINS or isinstance(origin, str)
            and re.fullmatch(r'safari-web-extension://[0-9a-fA-F]{8}'
                             r'(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', origin) is not None)


class CaptureServer(HTTPServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)  # Bound header reads as well as body reads.
        return connection, address


def validate_capture(value, now=None):
    # An expired access token can still accompany a usable refresh token.
    # Capture does not assert upstream validity or perform any renewal itself.
    return validate_session(value, now=now, require_fresh=False)


def handler_for(nonce, destination, expires_at, clock=time.monotonic):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never print bodies, request targets, headers, or credentials.

        def respond(self, status):
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'{}')

        def do_POST(self):
            self.connection.settimeout(3)
            authorization = self.headers.get('Authorization', '')
            if (self.path != '/capture' or clock() >= expires_at
                    or getattr(self.server, 'captured', False)
                    or not allowed_origin(self.headers.get('Origin'))
                    or not hmac.compare_digest(authorization.encode('utf-8'),
                                               ('Bearer ' + nonce).encode('ascii'))):
                origin = self.headers.get('Origin')
                origin_kind = ('none' if origin is None else 'opaque' if origin == 'null'
                               else 'safari-extension' if origin.startswith('safari-web-extension://')
                               else 'kimi' if origin in ORIGINS else 'other')
                self.server.rejection_summary = {
                    'path_ok': self.path == '/capture', 'expired': clock() >= expires_at,
                    'already_captured': bool(getattr(self.server, 'captured', False)),
                    'origin_kind': origin_kind,
                    'nonce_ok': hmac.compare_digest(authorization.encode('utf-8'),
                                                    ('Bearer ' + nonce).encode('ascii')),
                }
                self.respond(403)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= LIMIT or self.headers.get('Transfer-Encoding'):
                    raise ValueError()
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError()
                value = validate_capture(json.loads(self.rfile.read(length)))
            except (ValueError, TypeError, OSError):
                self.respond(400)
                return
            if clock() >= expires_at:
                self.respond(403)
                return
            try:
                save_capture(destination, value)
            except (OSError, UnicodeError):
                self.respond(500)
                return
            self.server.captured = True
            self.respond(200)

        def do_GET(self):
            self.respond(404)

    return Handler


def render_script(nonce, port, expires_at=None):
    expires_at = time.time() + CAPTURE_SECONDS if expires_at is None else expires_at
    template = (Path(__file__).parent.parent / 'browser' / 'kimi-session-capture.user.js').read_text()
    return (template.replace('__CAPTURE_NONCE__', nonce)
            .replace('__CAPTURE_PORT__', str(port))
            .replace('__CAPTURE_EXPIRES_MS__', str(int(expires_at * 1000))))


def capture(script_directory, destination):
    script_directory, destination = Path(script_directory), Path(destination)
    script_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = script_directory / 'kimi-session-capture.user.js'
    nonce = secrets.token_urlsafe(32)
    deadline = time.monotonic() + CAPTURE_SECONDS
    with CaptureServer(('127.0.0.1', 0),
                    handler_for(nonce, destination, deadline)) as server:
        server.timeout = .5
        # Refuse to overwrite a user's existing script, including a symlink.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        identity = os.fstat(fd)
        try:
            with os.fdopen(fd, 'w') as output:
                output.write(render_script(nonce, server.server_port))
            print('Temporary script written to the selected Userscripts save folder:', script_directory)
            print('Reload the signed-in Kimi tab and click "Save Kimi session locally". '
                  'Capture expires in ten minutes; Ctrl+C cancels. No model request is sent.', flush=True)
            while not getattr(server, 'captured', False) and time.monotonic() < deadline:
                server.handle_request()
                if getattr(server, 'rejection_summary', None) is not None:
                    print('Local capture rejected:', json.dumps(server.rejection_summary), flush=True)
                    server.rejection_summary = None
            if not getattr(server, 'captured', False):
                raise RuntimeError('Capture timed out')
            print('Private Kimi session saved. Provider pauses are unchanged; access is not yet verified.', flush=True)
        finally:
            try:
                current = target.lstat()
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    target.unlink()
            except FileNotFoundError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--script-directory', type=Path, required=True,
                        help='Actual Save Location shown by the Userscripts app; no inferred default')
    parser.add_argument('--session-file', type=Path, default=ROOT / 'session/kimi/session.json')
    args = parser.parse_args()
    try:
        capture(args.script_directory, args.session_file)
    except (KeyboardInterrupt, OSError, RuntimeError):
        print('Session capture stopped. No retry was made.', flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
