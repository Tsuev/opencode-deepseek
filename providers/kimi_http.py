"""One guarded Kimi completion, using an explicitly captured session."""

import json
import os
import struct
import time

import httpx

from chat_protocol import Reply
from .access import provider_attempt, rejection
from .common import BufferedStream, ProviderUnavailable, completion_timeout
from .conversations import decode, encode
from .kimi_auth import SESSION_FILE, request_headers, usable_session
from .kimi_protocol import kimi_reply
from .kimi_models import MODELS, request_payload

MODEL = 'k2d6-chat'
CHAT_PATH = '/apiv2/kimi.gateway.chat.v1.ChatService/Chat'


def direct_enabled():
    mode = os.getenv('KIMI_TRANSPORT', 'browser')
    if mode not in ('browser', 'direct'):
        raise ValueError('KIMI_TRANSPORT must be browser or direct')
    return mode == 'direct'


class KimiClient:
    def __init__(self, check_cancelled=lambda: None, session_file=SESSION_FILE,
                 client_factory=httpx.Client, renew=None):
        self.check_cancelled = check_cancelled
        self.session_file, self.client_factory = session_file, client_factory
        self.renew = (os.getenv('KIMI_RENEW_SESSION', '0').lower() not in ('', '0', 'false', 'no', 'off')
                      if renew is None else renew)

    def chat(self, prompt, conversation_id=None, model=None, thinking=False, search=False):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 1024 * 1024:
            raise ValueError('Kimi requires a nonempty text prompt up to 1 MiB')
        if thinking or search:
            raise ValueError('Direct Kimi uses fixed model modes; thinking/search switches are unsupported')
        chat = parent = None
        if conversation_id:
            model, reference = decode(conversation_id, 'kimi')
            if model not in MODELS:
                raise ValueError('A browser conversation cannot be resumed through direct Kimi')
            _, _, chat, _, parent = reference.split('/')
        if model not in MODELS:
            raise ValueError('Unsupported direct Kimi model')
        timeout = completion_timeout()
        with provider_attempt('kimi', self.check_cancelled) as attempt:
            session = usable_session(attempt, self.session_file, self.renew, self.client_factory)
            headers = {**request_headers(session), 'Authorization': 'Bearer ' + session['token'],
                       'Content-Type': 'application/connect+json', 'Accept': 'application/connect+json'}
            payload = request_payload(prompt, model, chat, parent)
            data = json.dumps(payload, separators=(',', ':')).encode()
            wire = b'\0' + struct.pack('>I', len(data)) + data
            with self.client_factory(trust_env=False, follow_redirects=False, timeout=min(timeout, 30)) as client:
                attempt.dispatch()
                deadline = time.monotonic() + timeout
                try:
                    with client.stream('POST', session['origin'] + CHAT_PATH, headers=headers, content=wire) as response:
                        if response.status_code != 200:
                            raise rejection(status=response.status_code)
                        if response.headers.get('content-type', '').split(';')[0] != 'application/connect+json':
                            raise ProviderUnavailable('Kimi returned an unsupported response type')
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            attempt.check()
                            if time.monotonic() >= deadline or len(body) + len(chunk) > 8 * 1024 * 1024:
                                raise ProviderUnavailable('Kimi response exceeded the time or size limit')
                            body.extend(chunk)
                except httpx.HTTPError:
                    raise rejection() from None
            attempt.check()
            text, chat, assistant = kimi_reply(body, prompt, chat, parent,
                                               model, MODELS[model]['scenario'])
            token = encode('kimi', model, '/chat/' + chat + '/parent/' + assistant)
            return Reply(text, token)

    def stream(self, *args, **kwargs):
        return BufferedStream(self.chat(*args, **kwargs))
