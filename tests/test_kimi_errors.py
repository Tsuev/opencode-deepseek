import base64
import unittest

from providers.kimi_errors import connect_rejection


def detail(body):
    return {'type': 'common.error.v1.ErrorDetail',
            'value': base64.b64encode(body).decode()}


class KimiErrorTests(unittest.TestCase):
    def code(self, details):
        return connect_rejection({'code': 'resource_exhausted', 'details': details}).code

    def test_capacity_and_quota_are_distinct(self):
        self.assertEqual(self.code([detail(bytes([8, 117]))]), 'capacity_unavailable')
        for reason in (112, 116, 120, 121, 122, 123, 210, 212):
            encoded = bytes([reason]) if reason < 128 else bytes([reason | 128, 1])
            self.assertEqual(self.code([detail(b'\x08' + encoded)]), 'quota_exceeded')

    def test_localized_messages_and_paywall_do_not_change_classification(self):
        body = b'\x12\x05quota\x08\x75\x1a\x02\x08\x01'
        self.assertEqual(self.code([detail(body)]), 'capacity_unavailable')
        self.assertEqual(self.code([detail(b'\x08\x76')]), 'upstream_failure')

    def test_malformed_duplicate_and_untyped_details_fail_closed(self):
        malformed = (b'', b'\x08', b'\x08\x75\x08\x75', b'\x0a\x00\x08\x75',
                     b'\x08\x75\x12\x7f', b'\x08' + b'\xff' * 11, b'\x00')
        values = [[detail(body)] for body in malformed]
        values += [[detail(b'\x08\x75')] * 2, [{'value': 'CHU='}],
                   [{'type': 'common.error.v1.ErrorDetail', 'value': '!'}],
                   [{'type': 'common.error.v1.ErrorDetail', 'value': 'A' * 16385}],
                   [detail(b'\x08\x75')] * 17, {}, None]
        for value in values:
            with self.subTest(details=value):
                self.assertEqual(self.code(value), 'upstream_failure')

    def test_session_and_access_refusals_never_expose_error_message(self):
        for code, expected in (('unauthenticated', 'session_expired'),
                               ('permission_denied', 'access_denied'),
                               ('unknown', 'upstream_failure')):
            error = connect_rejection({'code': code, 'message': 'sensitive account data'})
            self.assertEqual(error.code, expected)
            self.assertNotIn('sensitive', str(error))


if __name__ == '__main__':
    unittest.main()
