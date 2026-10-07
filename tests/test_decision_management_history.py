"""Request evidence tests independent of network, process startup and model weights."""
from __future__ import annotations

import hashlib
import json
import time
import unittest

from astrbot_ex.core.decision.history import RequestHistory, display
from astrbot_ex.core.decision.owned_laya import fixed_warmup_snapshot


def prepared(snapshot_id):
    snapshot = fixed_warmup_snapshot().to_dict()
    snapshot['snapshot_id'] = snapshot_id
    return {'kind': 'prepared', 'snapshot_id': snapshot_id, 'snapshot': snapshot,
            'time_ns': time.monotonic_ns(), 'backend': 'laya', 'backend_type': 'LayaBackend',
            'service_generation': 'owned-generation-1'}


class DecisionManagementHistoryTests(unittest.TestCase):
    def history(self, **kwargs):
        history = RequestHistory(**kwargs)
        self.addCleanup(history.close)
        return history

    def test_progress_distinguishes_prepared_socket_write_and_received_response(self):
        history = self.history()
        history.consume(prepared('one'))
        value = history.latest()
        self.assertTrue(value['prepared'])
        self.assertFalse(value['post_attempted'])
        self.assertFalse(value['response_received'])
        history.consume({'kind': 'backend_progress', 'snapshot_id': 'one', 'time_ns': time.monotonic_ns(),
            'values': {'snapshot_id': 'one', 'post_attempted': True, 'post_written_to_socket': True}})
        value = history.latest()
        self.assertTrue(value['post_written_to_socket'])
        self.assertFalse(value['response_received'])
        history.consume({'kind': 'backend_progress', 'snapshot_id': 'one', 'time_ns': time.monotonic_ns(),
            'values': {'snapshot_id': 'one', 'http_status': 200, 'raw_response': {'fixture': 'response'}}})
        self.assertTrue(history.latest()['response_received'])

    def test_different_previous_last_record_is_not_attached_to_current_request(self):
        history = self.history()
        history.consume(prepared('current'))
        history.consume({'kind': 'backend_returned', 'snapshot_id': 'current', 'time_ns': time.monotonic_ns(),
            'record': {'snapshot_id': 'previous', 'request_body': 'PREVIOUS-SENSITIVE-INPUT',
                       'raw_response': {'previous': True}, 'post_attempted': True},
            'result': None, 'error_code': 'restart_required'})
        value = history.latest()
        self.assertIsNone(value['actual_request'])
        self.assertIsNone(value['actual_response'])
        self.assertFalse(value['post_attempted'])
        self.assertTrue(value['backend_record_missing'])
        self.assertEqual(value['backend_error_code'], 'restart_required')

    def test_actual_full_hash_survives_redaction_and_sse_notice_contains_only_ids(self):
        notices, marker = [], 'SECRET-MARKER-ONLY'
        history = self.history(notify=notices.append, redact=lambda value:
            json.loads(json.dumps(value).replace(marker, '[REDACTED]')))
        history.consume(prepared('one'))
        body = json.dumps({'state': marker, 'questions': {}})
        sha = hashlib.sha256(body.encode()).hexdigest()
        history.consume({'kind': 'backend_returned', 'snapshot_id': 'one', 'time_ns': time.monotonic_ns(),
            'record': {'snapshot_id': 'one', 'request_body': body, 'input_sha256': sha},
            'result': None, 'error_code': 'fixture_error'})
        value = history.latest()
        self.assertEqual(value['input_sha256'], sha)
        self.assertNotIn(marker, json.dumps(value))
        self.assertNotIn(marker, json.dumps(notices))
        self.assertTrue(all(set(notice) == {'request_id', 'snapshot_id', 'sequence', 'kind'} for notice in notices))

    def test_ex_outcome_and_commands_are_recorded_separately_from_model_choice(self):
        history = self.history()
        history.consume(prepared('one'))
        history.consume({'kind': 'backend_returned', 'snapshot_id': 'one', 'time_ns': time.monotonic_ns(),
            'record': {'snapshot_id': 'one'}, 'result': {'choices': [{'option_id': 'model-option'}]},
            'error_code': None})
        self.assertEqual(history.latest()['command_ids'], [])
        history.consume({'kind': 'outcome', 'snapshot_id': 'one', 'time_ns': time.monotonic_ns(),
            'outcome': 'discarded', 'reason_code': 'goal_revision_changed', 'details': {}})
        value = history.latest()
        self.assertEqual(value['ex_outcome'], 'discarded')
        self.assertEqual(value['command_ids'], [])
        self.assertEqual(value['ex_outcomes'][0]['reason_code'], 'goal_revision_changed')

    def test_storage_and_pages_are_bounded_without_losing_sequential_records(self):
        history = self.history()
        for index in range(130):
            sid = 'sequence-' + str(index)
            history.consume(prepared(sid))
            history.consume({'kind': 'backend_returned', 'snapshot_id': sid, 'time_ns': time.monotonic_ns(),
                'record': {'snapshot_id': sid}, 'result': None, 'error_code': 'fixture'})
        first = history.page(limit=100)
        second = history.page(cursor=first['next_cursor'], limit=100)
        self.assertEqual(first['retained'], 128)
        self.assertEqual(len(first['items']), 100)
        self.assertEqual(len(second['items']), 28)
        self.assertEqual(first['lost_events'], 0)
        self.assertEqual([row['snapshot_id'] for row in first['items'] + second['items']],
                         ['sequence-' + str(index) for index in range(2, 130)])
        for limit in (0, 101, True):
            with self.assertRaises(ValueError):
                history.page(limit=limit)

    def test_display_marks_truncation_and_preserves_original_display_size(self):
        value = {'observation': 'x' * 70000}
        shown = display(value)
        self.assertTrue(shown['truncated'])
        self.assertIsNone(shown['value'])
        self.assertGreater(shown['original_bytes'], 65536)
        self.assertLessEqual(len(shown['preview'].encode()), 65536)
        self.assertEqual(value['observation'], 'x' * 70000)


if __name__ == '__main__':
    unittest.main()
