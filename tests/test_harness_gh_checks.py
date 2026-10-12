import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import patch
from integrations.harness_gh_checks import main


class ChecksTests(unittest.TestCase):
    def run_checks(self, status=1, bucket='fail', state='FAILURE', stderr='', payload=None, args=None):
        rows = [dict(name='CI', bucket=bucket, state=state, link='https://example.test/job', workflow='test')]
        result = subprocess.CompletedProcess([], status, json.dumps(rows) if payload is None else payload, stderr)
        out, err = io.StringIO(), io.StringIO()
        with patch('integrations.harness_gh_checks.subprocess.run', return_value=result) as run:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(['/mock/gh', *(args or ['69'])])
            self.assertEqual(run.call_count, 1)
            self.assertIn('--json', run.call_args.args[0])
        return code, out.getvalue(), err.getvalue()

    def test_statuses_continue_and_remain_visible(self):
        for code, bucket, state in [(1, 'fail', 'FAILURE'), (8, 'pending', 'IN_PROGRESS'),
                                    (0, 'pending', 'IN_PROGRESS'), (0, 'fail', 'FAILURE'),
                                    (0, 'pass', 'SUCCESS')]:
            with self.subTest(bucket=bucket):
                result, out, _ = self.run_checks(code, bucket, state)
                self.assertEqual(result, 0)
                self.assertEqual(json.loads(out)[0]['bucket'], bucket)

    def test_json_export_mixed_checks_preserves_statuses(self):
        rows = [dict(name=bucket, bucket=bucket, state=state, link='', workflow='CI')
                for bucket, state in [('pending', 'IN_PROGRESS'), ('pass', 'SUCCESS'),
                                      ('skipping', 'SKIPPED')]]
        code, out, err = self.run_checks(status=0, payload=json.dumps(rows))
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out), rows)

    def test_errors_and_invalid_payload_never_normalized(self):
        for kwargs in [dict(stderr='authentication failed'), dict(payload='network error'), dict(payload='[]'),
                       dict(payload='{}'), dict(payload='[{"bucket":"fail"}]'), dict(state='SUCCESS'),
                       dict(status=1, bucket='pass', state='SUCCESS'), dict(status=8), dict(status=2),
                       dict(status=0, payload='invalid'), dict(status=-15),
                       dict(status=0, bucket='pending', state='IN_PROGRESS', stderr='authentication failed'),
                       dict(status=0, bucket='pending', state='SUCCESS'), dict(status=0, payload='[]'),
                       dict(status=1, bucket='pending', state='IN_PROGRESS')]:
            with self.subTest(kwargs=kwargs):
                self.assertNotEqual(self.run_checks(**kwargs)[0], 0)

    def test_options_and_native_modes(self):
        self.assertEqual(self.run_checks(args=['69', '--required', '-R', 'owner/repo'])[0], 0)
        for args in [['69', '--watch'], ['69', '--json', 'name'], ['--web'], ['--repo']]:
            with patch('integrations.harness_gh_checks.subprocess.call', return_value=17) as call:
                self.assertEqual(main(['/mock/gh', *args]), 17)
                call.assert_called_once_with(['/mock/gh', 'pr', 'checks', *args])
