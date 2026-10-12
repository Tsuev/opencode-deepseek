"""Private failed-stream evidence and offline, review-only reply reconstruction.

Never releases a safety pause, edits history, executes tools or calls a provider.
"""
import argparse
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import time
import uuid

from deepseek.auth import write_private_json
from settings import ROOT

DIRECTORY = ROOT / 'session' / 'failed-streams'
MAX_BYTES = 8 * 1024 * 1024
MAX_FILES = 4
MAX_FILE_BYTES = 12 * 1024 * 1024
LOG = logging.getLogger(__name__)


class StreamCapture:
    def __init__(self):
        setting = os.getenv('DEEPSEEK_STREAM_DIAGNOSTICS', '0')
        self.enabled = setting.lower() in ('1', 'true', 'yes', 'on')
        self.raw = bytearray()
        self.truncated = False
        self.eof = False

    def wrap(self, response):
        capture = self

        class Observed:
            def iter_bytes(self):
                for chunk in response.iter_bytes():
                    if capture.enabled:
                        available = MAX_BYTES - len(capture.raw)
                        capture.raw.extend(chunk[:available])
                        if len(chunk) > available:
                            capture.truncated = True
                    yield chunk
                capture.eof = True

        return Observed()

    def save(self, *, reason, session_id, parent_id):
        if not self.enabled or not self.raw:
            return
        try:
            DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Never silently delete earlier evidence. Bound disk use instead.
            if len(list(DIRECTORY.iterdir())) >= MAX_FILES:
                LOG.warning('Private stream evidence directory is full; capture not saved')
                return
            name = uuid.uuid4().hex + '.json'
            write_private_json(DIRECTORY / name, {
                'version': 1, 'captured_at': time.time(), 'reason': reason,
                'eof': self.eof, 'truncated': self.truncated,
                'session_id': session_id, 'parent_id': parent_id,
                'sha256': hashlib.sha256(self.raw).hexdigest(),
                'body_base64': base64.b64encode(self.raw).decode('ascii'),
            })
            LOG.warning('Private failed stream saved: %s', name)
        except (OSError, ValueError, TypeError):
            LOG.warning('Private stream evidence could not be saved')


def reconstruct(path):
    """Reparse evidence after a parser fix; output is NOT a delivery receipt.

    EOF alone is not completion. Require a complete capture, a recognized
    terminal marker and a fresh response ID. Still require manual review of
    the failure and tool schemas before any history/delivery repair.
    """
    from deepseek.client import _parse_sse, _encode_cid
    from providers.stream_limits import bounded_lines

    with Path(path).open('rb') as source:
        encoded = source.read(MAX_FILE_BYTES + 1)
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError('Evidence exceeds size limit')
    data = json.loads(encoded)
    if (not isinstance(data, dict) or data.get('version') != 1
            or data.get('eof') is not True or data.get('truncated') is not False):
        raise ValueError('Capture is incomplete; no reply can be recovered')
    raw = base64.b64decode(data['body_base64'], validate=True)
    if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != data.get('sha256'):
        raise ValueError('Evidence integrity check failed')
    sid, parent = data.get('session_id'), data.get('parent_id')
    if (not isinstance(sid, str) or not sid or len(sid) > 256 or ':' in sid
            or any(ord(c) <= 32 or ord(c) >= 127 for c in sid)
            or parent is not None and (type(parent) is not int or parent <= 0)):
        raise ValueError('Invalid conversation binding')

    class OfflineResponse:
        def iter_bytes(self):
            yield raw

    meta = {}
    lines = iter(bounded_lines(OfflineResponse()))
    text = ''.join(_parse_sse(lines, meta))
    if any(line.strip() for line in lines):
        raise ValueError('Unvalidated data follows the terminal marker')
    identity = meta.get('message_id')
    if type(identity) is not int or identity <= 0 or identity == parent:
        raise ValueError('Missing or stale response identity')
    if meta.get('finish_reason') != 'stop' or not text.strip():
        raise ValueError('Response did not finish normally with answer text')
    return {'status': 'review_only', 'source_sha256': hashlib.sha256(encoded).hexdigest(),
            'text': text, 'conversation_id': _encode_cid(sid, identity),
            'finish_reason': 'stop', 'goal_resumed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    args = parser.parse_args()
    try:
        reply = reconstruct(args.capture)
        # Keep private output next to evidence, never dump provider text to CLI.
        output = args.capture.with_name(args.capture.name + '.review.json')
        if output.exists():
            raise ValueError('Review output already exists')
        write_private_json(output, reply)
    except Exception:
        print('Recovery refused. Evidence is incomplete, invalid, or output already exists. No state changed.')
        return 1
    print('Private review candidate saved next to capture (.review.json). No tools executed; no state changed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
