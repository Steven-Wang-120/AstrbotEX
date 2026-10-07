"""Precise regression checks for B08 request and evidence boundaries."""
from __future__ import annotations

import hashlib
import http.client
import json
import time
import unittest

from astrbot_ex.core.decision.history import RequestHistory
from astrbot_ex.core.decision.owned_laya import fixed_warmup_snapshot
from tests import test_decision_management_http as http_fixture


def prepared(snapshot_id='boundary'):
    snapshot = fixed_warmup_snapshot().to_dict()
    snapshot['snapshot_id'] = snapshot_id
    return {'kind': 'prepared', 'snapshot_id': snapshot_id, 'snapshot': snapshot,
        'time_ns': time.monotonic_ns(), 'backend': 'laya', 'backend_type': 'LayaBackend',
        'service_generation': 'generation-boundary'}


class DecisionManagementRequestBoundaryTests(http_fixture.ManagementHTTPFixture, unittest.TestCase):
    def test_non_ascii_wrong_bearer_returns_401_without_handler_failure(self):
        code, value, _ = self.request('/api/v1/ex/decision/status', authorization='Bearer é-invalid-credential')
        self.assertEqual(code, 401)
        self.assertEqual(value['code'], 'unauthorized')
        self.assert_idle()

    def test_duplicate_security_headers_and_ambiguous_or_oversize_query_return_400(self):
        port = self.server.server_address[1]
        host = '127.0.0.1:' + str(port)
        for duplicated, values in (('Host', (host, host)),
                ('Authorization', ('Bearer ' + self.token, 'Bearer wrong')),
                ('Content-Length', ('2', '2'))):
            with self.subTest(duplicated=duplicated):
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                try:
                    connection.putrequest('GET', '/api/v1/ex/decision/status', skip_host=True)
                    for name, value in [('Host', host), ('Authorization', 'Bearer ' + self.token)]:
                        if name != duplicated:
                            connection.putheader(name, value)
                    for value in values:
                        connection.putheader(duplicated, value)
                    connection.endheaders()
                    response = connection.getresponse()
                    value = json.loads(response.read())
                    self.assertEqual(response.status, 400)
                    self.assertEqual(value['code'], 'duplicate_security_header')
                finally:
                    connection.close()
        queries = ('?limit=1&limit=2', '?limit=1&limit=',
            '?' + '&'.join('p' + str(index) + '=1' for index in range(9)), '?cursor=' + '1' * 5000)
        for query in queries:
            with self.subTest(query=query[:70]):
                self.assertEqual(self.request('/api/v1/ex/decision/decisions' + query)[0], 400)
        self.assert_idle()

    def test_real_event_bus_publishes_id_notice_without_full_snapshot_or_input(self):
        marker = 'FULL-INPUT-MUST-NOT-BE-IN-SSE'
        history = self.server.decision_management.history
        event = prepared()
        event['snapshot']['goal']['goal_text_en'] = marker
        history.consume(event)
        events = self.server.controller.runtime.event_bus.recent()
        notice = [event for event in events if event.type == 'decision_changed']
        self.assertEqual(len(notice), 1)
        self.assertEqual(set(notice[0].data), {'request_id', 'snapshot_id', 'sequence', 'kind'})
        self.assertEqual(notice[0].data['snapshot_id'], 'boundary')
        self.assertNotIn(marker, json.dumps(notice[0].data))
        self.assertEqual(history.latest()['snapshot']['value']['goal']['goal_text_en'], marker)


class DecisionManagementRecordBoundaryTests(unittest.TestCase):
    def test_large_metadata_keeps_entire_display_record_within_budget_and_full_input_hash(self):
        history = RequestHistory()
        self.addCleanup(history.close)
        body = json.dumps({'state': 'x' * 15000, 'questions': {}})
        sha = hashlib.sha256(body.encode()).hexdigest()
        history.consume(prepared())
        history.consume({'kind': 'backend_returned', 'snapshot_id': 'boundary',
            'time_ns': time.monotonic_ns(), 'record': {'snapshot_id': 'boundary', 'request_body': body,
                'input_sha256': sha, 'raw_response': {'data': 'y' * 15000},
                'owner_mapping': {'owner': 'o' * 70000}, 'probability_normalization': {'meta': 'p' * 70000},
                'token_budgets': {'meta': 't' * 70000}},
            'result': {'choices': ['m' * 70000]}, 'error_code': None})
        history.consume({'kind': 'outcome', 'snapshot_id': 'boundary', 'time_ns': time.monotonic_ns(),
            'outcome': 'discarded', 'reason_code': 'stale_result', 'details': {'large': 'd' * 70000}})
        record = history.latest()
        size = len(json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode())
        self.assertLessEqual(size, 65536)
        self.assertTrue(record['record_truncated'])
        self.assertGreater(record['original_record_bytes'], 65536)
        self.assertEqual(record['input_sha256'], sha)
        self.assertEqual(record['actual_request_bytes'], len(body.encode()))
        self.assertEqual(record['snapshot_id'], 'boundary')
        self.assertEqual(record['ex_outcomes'][0]['reason_code'], 'stale_result')
        self.assertEqual(record['ex_outcomes'][0]['details'], {})
        self.assertTrue(record['ex_outcomes'][0]['details_display']['truncated'])


if __name__ == '__main__':
    unittest.main()
