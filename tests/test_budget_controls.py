import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import httpx
from providers.access import AccessGuard, _configured_budgets
from server import api


class BudgetControls(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.token = 'a' * 64
        token_file = root/'token'; token_file.write_text(self.token)
        self.guard = AccessGuard(root/'state.json', interval=30)
        now = time.time()
        self.record = {'usage':[[now-10,'generation'],[now-5,'chat']], 'pause':'account_restricted', 'next_at':now+30}
        with self.guard._state() as state:
            state['deepseek'] = self.record.copy(); self.guard._save(state)
        for p in (patch.object(api,'guard',self.guard), patch.dict(os.environ,{'BRIDGE_BUDGET_TOKEN_FILE':str(token_file)})):
            p.start(); self.addCleanup(p.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app,client=('127.0.0.1',123)),base_url='http://127.0.0.1',headers={'Authorization':'Bearer '+self.token})
        self.addAsyncCleanup(self.client.aclose)

    async def test_read_counts_and_reset_preserve_pause_and_pacing(self):
        r = await self.client.get('/admin/budget'); self.assertEqual(r.status_code,200)
        d = r.json(); self.assertEqual(d['conversation']['max_replies'], api.conversation_cache.max_replies)
        self.assertFalse(d['conversation']['reset_by_budget_reset'])
        self.assertIsNone(d['session_policy']['max_age_seconds'])
        self.assertEqual([b['used'] for b in d['budgets']], [2,2,1,1,1,1])
        result = await self.client.post('/admin/budget/reset',json={'revision':d['revision'],'confirmation':'RESET LOCAL BUDGET'})
        self.assertEqual(result.status_code,200); self.assertTrue(all(b['used']==0 for b in result.json()['budgets']))
        saved = json.loads(self.guard.path.read_text())['deepseek'];self.assertEqual(saved['pause'],'account_restricted');self.assertEqual(saved['next_at'],self.record['next_at'])

    async def test_unauthorized_and_cross_origin_cannot_read_or_reset(self):
        for headers in ({'Authorization':'Bearer wrong'}, {'Origin':'https://evil.invalid'}, {'Sec-Fetch-Site':'cross-site'}):
            for method,path in (('GET','/admin/budget'),('POST','/admin/budget/reset')):
                self.assertEqual((await self.client.request(method,path,headers=headers)).status_code,403)

    async def test_stream_capture_configuration_is_reported_without_mutation(self):
        before = self.guard.path.read_bytes()
        for setting, expected in [('1', True), ('0', False)]:
            with patch.dict(os.environ, DEEPSEEK_STREAM_DIAGNOSTICS=setting):
                data = (await self.client.get('/admin/budget')).json()
            self.assertEqual(data['stream_diagnostics'], {'enabled': expected})
        self.assertEqual(before, self.guard.path.read_bytes())

    async def test_reset_requires_confirmation_and_current_revision(self):
        self.assertEqual((await self.client.post('/admin/budget/reset',json={})).status_code,400)
        d=(await self.client.get('/admin/budget')).json()
        with self.guard._state() as state:
            state['deepseek']['usage'].append([time.time(),'http']);self.guard._save(state)
        payload={'revision':d['revision'],'confirmation':'RESET LOCAL BUDGET'}
        self.assertEqual((await self.client.post('/admin/budget/reset',json=payload)).status_code,409)
        payload['revision']=(await self.client.get('/admin/budget')).json()['revision']
        with self.guard._state() as state:
            state['deepseek']['pending']='a'*32;self.guard._save(state)
        self.assertEqual((await self.client.post('/admin/budget/reset',json=payload)).status_code,409)

    async def test_two_thousand_ledger_and_config(self):
        with patch.dict(os.environ,{'DEEPSEEK_MAX_HTTP_PER_DAY':'2000'}):
            self.assertIn((86400,None,2000),_configured_budgets())
        data={'deepseek':{'usage':[[time.time(),'http']]*2000}}
        self.assertEqual(len(AccessGuard._decode(data)['deepseek']['usage']),2000)
        data['deepseek']['usage'].append([time.time(),'http'])
        with self.assertRaises(ValueError):AccessGuard._decode(data)

    async def test_installer_refuses_concurrent_profile_edit(self):
        from integrations import harness_budget
        root = Path(self.temp.name)
        folder = root/'profiles/desktop';folder.mkdir(parents=True)
        target = folder/'cordis.patch.yml';target.write_text('- id: other\n  config: {value: before}\n')
        original_copy = harness_budget.install_plugin_copy
        changed = '- id: other\n  config: {value: user-change}\n'
        def edit(*args):
            target.write_text(changed)  # edit lands between read and atomic replace
            return original_copy(*args)
        with patch.object(harness_budget, 'install_plugin_copy', side_effect=edit):
            with self.assertRaisesRegex(ValueError, 'changed'):
                harness_budget.install(root, 'desktop', root/'token')
        self.assertEqual(target.read_text(), changed)

    async def test_installer_uses_insert_preserves_other_rows_and_model_updates(self):
        from integrations.harness_budget import install
        from integrations.harness import yaml_codec, patch_document
        root = Path(self.temp.name)
        folder=root/'profiles/desktop';folder.mkdir(parents=True)
        target=folder/'cordis.patch.yml';target.write_text('- id: other\n  config: {value: preserved}\n')
        install(root,'desktop',root/'token');install(root,'desktop',root/'token')
        rows=yaml_codec().load(target.read_text())
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1]['insert'][0]['id'],'local-bridge-budget')
        updated=patch_document(rows,'http://127.0.0.1:8000/v1',[{'id':'deepseek-chat'}])
        self.assertEqual(updated[0],rows[0]);self.assertEqual(updated[1],rows[1])
