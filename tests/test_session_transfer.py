import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from deepseek.auth import Session
from providers.access import AccessGuard, ProviderRejected
from server import api, session_transfer as module
from server.config import DEEPSEEK_MODEL_MAP
from server.session_transfer import SessionTransfer, TransferError


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root/'session.json'
        self.old = Session('a'*64, [], 'Safari fixture', 1)
        self.old.save(self.path)
        self.guard = AccessGuard(self.root/'guard.json', interval=0)
        self.record = {'pause':'session_expired','pending':'b'*32,'dispatched':True,
                       'recoverable_auth':True,'next_at':123,'next_http_at':124,'usage':[[123,'http']]}
        with self.guard._state() as state:
            state['deepseek'] = copy.deepcopy(self.record)
            self.guard._save(state)
        self.transfer = SessionTransfer(self.path)
        self.payload = {'token':'c'*64,'account_id':'owned-account','cookie':'ds_cookie_preference=all',
                        'user_agent':'Safari fixture','same_account':True}
        token = self.root/'admin-token'; token.write_text('d'*64)
        for p in (patch.object(api, 'guard', self.guard), patch.object(api, '_request_gate', asyncio.Lock()),
                  patch.dict(api.MODEL_MAP, DEEPSEEK_MODEL_MAP),
                  patch.object(module, 'transfer', self.transfer),
                  patch.dict('os.environ', {'BRIDGE_BUDGET_TOKEN_FILE':str(token)})):
            p.start(); self.addCleanup(p.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app, client=('127.0.0.1',123)),
                                       base_url='http://127.0.0.1',headers={'Authorization':'Bearer '+'d'*64})
        self.addAsyncCleanup(self.client.aclose)

    async def test_human_flow_preserves_history_scope_usage_pacing_and_no_model(self):
        started = (await self.client.post('/admin/session/deepseek/start')).json()
        with patch.object(api, 'get_client') as factory:
            r = await self.client.post('/session/deepseek/capture', json=self.payload,
                                      headers={'Authorization':'Bearer '+started['nonce'],'Origin':module.ORIGIN})
            self.assertEqual(r.json(), {'status':'saved','goal_resumed':False})
            factory.assert_not_called()
        fresh = Session.load(self.path)
        self.assertEqual(fresh.continuation_scope, self.old.identity())
        self.assertEqual(fresh.account_id, 'owned-account')
        record = json.loads(self.guard.path.read_text())['deepseek']
        self.assertEqual(record, {k:v for k,v in self.record.items() if k in ('usage','next_at','next_http_at')})
        self.assertEqual((await self.client.get('/admin/session/deepseek/status')).json()['status'], 'saved')
        before = self.path.read_bytes()
        r = await self.client.post('/session/deepseek/capture',json=self.payload,headers={'Authorization':'Bearer '+started['nonce']})
        self.assertEqual(r.status_code,409)
        self.assertEqual(self.path.read_bytes(),before)

    async def test_first_pair_requires_confirmation_then_account_match_is_mandatory(self):
        ticket = self.transfer.start(self.guard)
        self.payload['same_account'] = False
        self.assertEqual(self.transfer.capture(ticket['nonce'],self.payload,self.guard)['status'],'confirmation_required')
        self.assertEqual(Session.load(self.path).token,self.old.token)
        self.payload['same_account'] = True
        self.transfer.capture(ticket['nonce'],self.payload,self.guard)
        ticket = self.transfer.start(self.guard)
        self.payload.update(account_id='different-account',token='e'*64)
        with self.assertRaises(TransferError): self.transfer.capture(ticket['nonce'],self.payload,self.guard)
        self.payload.update(account_id='owned-account',same_account=False)
        self.transfer.capture(ticket['nonce'],self.payload,self.guard)
        self.assertEqual(Session.load(self.path).continuation_scope,self.old.identity())

    async def test_expired_replaced_nonce_unchanged_token_and_mutated_state_fail_closed(self):
        ticket = self.transfer.start(self.guard)
        replacement = self.transfer.start(self.guard)
        with self.assertRaises(TransferError): self.transfer.capture(ticket['nonce'],self.payload,self.guard)
        self.payload['token'] = self.old.token
        self.assertEqual(self.transfer.capture(replacement['nonce'],self.payload,self.guard)['status'],'new_login_required')
        self.payload['token'] = 'c'*64
        self.transfer.ticket['expires'] = 0
        with self.assertRaises(TransferError): self.transfer.capture(replacement['nonce'],self.payload,self.guard)
        self.assertEqual(self.transfer.status()['status'],'expired')
        ticket = self.transfer.start(self.guard)
        with self.guard._state() as state:
            state['deepseek']['usage'].append([124,'http']); self.guard._save(state)
        with self.assertRaises(TransferError): self.transfer.capture(ticket['nonce'],self.payload,self.guard)
        self.assertEqual(Session.load(self.path).token,self.old.token)

    async def test_other_restrictions_and_uncertain_requests_cannot_be_cleared(self):
        for record in ({'pause':'account_restricted'}, {'pause':'quota_exceeded'},
                       {'pause':'session_expired','pending':'f'*32,'dispatched':True}, {'pending':'f'*32}):
            with self.guard._state() as state:
                state['deepseek'] = record; self.guard._save(state)
            self.assertEqual((await self.client.post('/admin/session/deepseek/start')).status_code,409)
        async with api._request_gate:
            self.assertEqual((await self.client.post('/admin/session/deepseek/start')).status_code,409)

    async def test_auth_origin_size_and_validation_boundaries(self):
        for headers in ({'Authorization':'Bearer wrong'}, {'Origin':module.ORIGIN}, {'Sec-Fetch-Site':'same-site'}):
            self.assertEqual((await self.client.post('/admin/session/deepseek/start',headers=headers)).status_code,403)
        ticket = self.transfer.start(self.guard)
        headers = {'Authorization':'Bearer '+ticket['nonce'],'Origin':'https://evil.invalid'}
        self.assertEqual((await self.client.post('/session/deepseek/capture',json=self.payload,headers=headers)).status_code,403)
        headers['Origin'] = module.ORIGIN
        self.assertEqual((await self.client.post('/session/deepseek/capture',content=b'x'*32769,headers={**headers,'Content-Type':'application/json'})).status_code,413)
        for key,value in (('token','bad\r\ntoken'),('account_id',''),('user_agent','x\ny'),('cookie','bad'),('same_account','yes')):
            data = {**self.payload,key:value}
            self.assertEqual((await self.client.post('/session/deepseek/capture',json=data,headers=headers)).status_code,409)
        self.assertEqual(Session.load(self.path).token,self.old.token)

    async def test_capture_uses_actual_generation_queue_and_never_rotates_in_flight(self):
        from server.schemas import ChatCompletionRequest, ChatMessage
        req = ChatCompletionRequest(model='deepseek-chat',messages=[ChatMessage(role='user',content='offline')])
        gate = api._request_queue(req)
        self.assertIs(gate, api._request_gate)
        ticket = self.transfer.start(self.guard)
        before = self.path.read_bytes()
        async with gate:
            result = await self.client.post('/session/deepseek/capture',json=self.payload,
                                           headers={'Authorization':'Bearer '+ticket['nonce']})
            self.assertEqual(result.status_code,409)
        self.assertEqual(self.path.read_bytes(),before)

    async def test_changed_credentials_cancel_and_write_failure_do_not_replay(self):
        ticket = self.transfer.start(self.guard)
        self.transfer.cancel()
        with self.assertRaises(TransferError): self.transfer.capture(ticket['nonce'],self.payload,self.guard)
        ticket = self.transfer.start(self.guard)
        with patch.object(Session,'save',side_effect=OSError('offline disk failure')):
            with self.assertRaises(ProviderRejected): self.transfer.capture(ticket['nonce'],self.payload,self.guard)
        with self.assertRaises(TransferError): self.transfer.capture(ticket['nonce'],self.payload,self.guard)
        self.assertEqual(Session.load(self.path).token,self.old.token)
        ticket = self.transfer.start(self.guard)
        changed = Session('f'*64, [], 'changed', 2); changed.save(self.path)
        with self.assertRaises(TransferError): self.transfer.capture(ticket['nonce'],self.payload,self.guard)

    async def test_safety_save_failure_leaves_runtime_fail_closed(self):
        ticket = self.transfer.start(self.guard)
        with patch.object(self.guard,'_save',side_effect=OSError('offline disk failure')):
            with self.assertRaises(ProviderRejected):
                self.transfer.capture(ticket['nonce'],self.payload,self.guard)
        with self.assertRaises(ProviderRejected): self.guard.check('deepseek')
        self.assertEqual(json.loads(self.guard.path.read_text())['deepseek']['pause'],'session_expired')

    async def test_deadline_is_rechecked_after_waiting_for_state_lock(self):
        self.transfer.clock = lambda: 0
        ticket = self.transfer.start(self.guard)
        with patch.object(self.transfer, 'clock', side_effect=[0, 601]):
            with self.assertRaises(TransferError):
                self.transfer.capture(ticket['nonce'], self.payload, self.guard)
        self.assertEqual(Session.load(self.path).token, self.old.token)
