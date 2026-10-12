"""Private, durable one-use continuation links for stateless OpenAI clients.

Only hashes of the exact delivered history are retained, never source text.
Unknown or already consumed histories fail closed instead of creating chats.
"""
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from settings import ROOT
from .config import canonical_model
from .schemas import ChatMessage
from .openai_format import _text_of
from .recovery_status import RECOVERY_STOP_TEXT


class ConversationStateError(RuntimeError):
    pass


def normalized(message):
    value = message.model_dump(exclude_none=True) if hasattr(message, 'model_dump') else dict(message)
    content = value.get('content')
    if isinstance(content, list) and all(p.get('type') == 'text' for p in content):
        content = _text_of(content)
    value['content'] = content or ''
    if not value.get('tool_calls'):
        value.pop('tool_calls', None)
    for call in value.get('tool_calls', []):
        fn = call.get('function', {})
        if isinstance(fn.get('arguments'), str):
            try:
                fn['arguments'] = json.loads(fn['arguments'])
            except ValueError:
                pass
    return value


def digest(req, messages):
    # Owner-prefixed aliases (deepseek/deepseek-expert) must share history with
    # the bare public id used when the link was stored.
    model = canonical_model(req.model) or req.model
    payload = [model, req.user, req.thinking, req.search, req.tools,
               [normalized(m) for m in messages]]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


class ConversationCache:
    def __init__(self, path, account_file=None):
        raw = os.getenv('CONVERSATION_MAX_REPLIES', '20')
        if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= 1000:
            raise ValueError('CONVERSATION_MAX_REPLIES must be an integer between 1 and 1000')
        self.max_replies = int(raw)
        self.path = Path(path)
        self.account_file = Path(account_file) if account_file is not None else None

    def account_identity(self):
        scope = 'isolated'
        if self.account_file is not None:
            try:
                scope = hashlib.sha256(self.account_file.read_bytes()).hexdigest()
            except OSError as exc:
                raise ConversationStateError('Local account identity unavailable; no request sent.') from exc
        return scope

    def key(self, req, messages, scope=None):
        identity = self.account_identity() if scope is None else scope
        return hashlib.sha256((identity + digest(req, messages)).encode()).hexdigest()

    @contextmanager
    def connection(self):
        db = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
            db = sqlite3.connect(str(self.path), timeout=5)
            db.execute('CREATE TABLE IF NOT EXISTS links (key TEXT PRIMARY KEY, cid TEXT NOT NULL, used INTEGER NOT NULL, steps INTEGER NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            if 'reservation' not in [row[1] for row in db.execute('PRAGMA table_info(links)')]:
                db.execute('ALTER TABLE links ADD COLUMN reservation TEXT')
            yield db
            db.commit()
        except (OSError, sqlite3.Error) as exc:
            raise ConversationStateError('Local conversation state unavailable; no new chat was created.') from exc
        finally:
            if db is not None:
                db.close()

    def prepare(self, req, *, scope=None, claim=None, legacy_scopes=()):
        # Explicit protocol clients already own their conversation identity.
        if req.conversation_id:
            return req, 0
        positions = [i for i, m in enumerate(req.messages) if m.role == 'assistant']
        # A local recovery notice has no upstream reply ID. Resolve the original
        # delivered action instead, retaining ALL tool results for the next
        # real continuation. The original exact history hash, result IDs and
        # one-use claim are still checked below; never create an alias claim.
        if positions:
            last = positions[-1]
            message = req.messages[last]
            if not message.tool_calls and _text_of(message.content) == RECOVERY_STOP_TEXT:
                if (not any(m.role == 'user' for m in req.messages[last + 1:])
                        or any(m.role != 'user' for m in req.messages[last + 1:])):
                    raise ConversationStateError('Recovery is stopped; explicit continuation is required.')
                req = req.model_copy(update={'messages': [*req.messages[:last], *req.messages[last + 1:]]})
                positions = [i for i, m in enumerate(req.messages) if m.role == 'assistant']
                if not positions:
                    raise ConversationStateError('Recovery parent unavailable; no request sent.')
        if not positions:
            if any(m.role == 'tool' for m in req.messages):
                raise ConversationStateError('Tool history has no known assistant; no new chat was created.')
            # Verify durable storage before the first upstream dispatch too.
            self.key(req, req.messages, scope)
            with self.connection():
                pass
            return req, 0
        end = positions[-1] + 1
        tail = req.messages[end:]
        if not tail or any(m.role not in ('tool', 'user') for m in tail):
            raise ConversationStateError('Ambiguous continuation; no new chat was created.')
        calls = {call.get('id'): call for call in (req.messages[end - 1].tool_calls or [])}
        result_ids = [m.tool_call_id for m in tail if m.role == 'tool']
        if len(set(result_ids)) != len(result_ids) or set(result_ids) != set(calls):
            raise ConversationStateError('Tool results do not match the delivered calls.')
        key = self.key(req, req.messages[:end], scope)
        with self.connection() as db:
            row = db.execute('SELECT cid, used, steps FROM links WHERE key=?', (key,)).fetchone()
            # A namespace upgrade must not orphan histories stored before it.
            # The legacy link is claimed exactly like a current one.
            for legacy in () if row else legacy_scopes:
                legacy_key = self.key(req, req.messages[:end], legacy)
                row = db.execute('SELECT cid, used, steps FROM links WHERE key=?', (legacy_key,)).fetchone()
                if row:
                    key = legacy_key
                    break
            if not row or row[1]:
                raise ConversationStateError('History is unknown or already consumed; automatic replay and new-chat fallback are disabled.')
            if row[2] >= self.max_replies:
                raise ConversationStateError(f'Local conversation limit reached ({self.max_replies} accepted replies); continuation stopped. This per-conversation cap is separate from hourly/daily budgets and is not reset by /bridge-budget-reset.')
            # Claim before dispatch. An uncertain response must never replay.
            reservation = uuid.uuid4().hex
            db.execute('UPDATE links SET used=1, reservation=? WHERE key=?', (reservation, key))
        if claim is not None:
            claim.update(key=key, reservation=reservation)
        tail = [m.model_copy(update={'name': calls[m.tool_call_id]['function']['name']})
                if m.role == 'tool' else m for m in tail]
        if result_ids:
            # Bridge-generated IDs were not present in the upstream answer.
            # Supply the exact delivered calls to bind even out-of-order results.
            tail = [ChatMessage(role='assistant',
                     content='Previously requested calls: identifier mapping for the following results.',
                     tool_calls=[calls[cid] for cid in result_ids]), *tail]
        return req.model_copy(update={'conversation_id': row[0], 'messages': tail}), row[2]

    def release(self, claim):
        if not claim:
            return
        with self.connection() as db:
            db.execute('UPDATE links SET used=0, reservation=NULL WHERE key=? AND reservation=?',
                       (claim['key'], claim['reservation']))

    def claim_rollover(self, claim, previous_cid, archive):
        """Journal migration before clearing a refusal; never reopen an old link."""
        if not claim:
            raise ConversationStateError('Migration requires an owned history reservation.')
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS rollovers '
                       '(key TEXT PRIMARY KEY, previous_cid TEXT NOT NULL, archive TEXT NOT NULL)')
            owned = db.execute('SELECT used, reservation, cid FROM links WHERE key=?',
                               (claim['key'],)).fetchone()
            if owned != (1, claim['reservation'], previous_cid):
                raise ConversationStateError('Migration history reservation changed.')
            if db.execute('SELECT 1 FROM rollovers WHERE key=?', (claim['key'],)).fetchone():
                raise ConversationStateError('Migration already attempted; automatic replay disabled.')
            db.execute('INSERT INTO rollovers VALUES (?, ?, ?)',
                       (claim['key'], previous_cid, str(archive)))

    def remember(self, original, reply, content, calls, steps, *, scope=None):
        if original.conversation_id or not reply.conversation_id:
            return
        assistant = ChatMessage(role='assistant', content=content, tool_calls=calls)
        key = self.key(original, [*original.messages, assistant], scope)
        with self.connection() as db:
            db.execute('INSERT INTO links (key,cid,used,steps) VALUES (?, ?, 0, ?) ON CONFLICT(key) DO UPDATE SET used=1, reservation=NULL', (key, reply.conversation_id, steps + 1))


conversation_cache = ConversationCache(ROOT / 'session' / 'deepseek-conversations.sqlite3',
                                       ROOT / 'session' / 'session.json')
