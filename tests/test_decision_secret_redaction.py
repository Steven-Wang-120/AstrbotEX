"""Focused secret-redaction regression; no HTTP server or model inference."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from astrbot_ex.core.decision.config import SecretStore


class DecisionSecretRedactionTests(unittest.TestCase):
    def test_legal_quoted_backslash_unicode_secret_is_redacted_from_actual_body_and_nested_json(self):
        secret = 'B08-legal-"quote"-\\path-中文密钥'
        with tempfile.TemporaryDirectory() as directory:
            store = SecretStore(Path(directory))
            reference = store.create(secret)
            self.assertEqual(store.read(reference), secret)
            for ensure_ascii in (False, True):
                with self.subTest(ensure_ascii=ensure_ascii):
                    state = json.dumps({'observations': [{'api_key': secret}], 'goal': 'Move safely.'},
                        ensure_ascii=ensure_ascii, separators=(',', ':'))
                    # Actual protocol shape: JSON body holds an already serialized state string.
                    body = json.dumps({'model': 'typed-decisions', 'state': state,
                        'questions': {'q0': {'type': 'choice', 'criteria': {'A': 'Wait.'}}}},
                        ensure_ascii=ensure_ascii, separators=(',', ':'))
                    nested = json.dumps({'actual_request': body}, ensure_ascii=ensure_ascii, separators=(',', ':'))
                    nested_twice = json.dumps({'serialized_record': nested},
                        ensure_ascii=ensure_ascii, separators=(',', ':'))
                    payload = {'direct': secret, 'actual_request': body, 'serialized_record': nested,
                        'nested_twice': nested_twice, 'objects': [{secret: secret}], 'tuple': (secret,)}
                    original = copy.deepcopy(payload)
                    safe = store.redact(payload)
                    self.assertEqual(payload, original, 'redaction mutated original evidence')
                    self.assertEqual(safe['direct'], '[REDACTED]')
                    self.assertEqual(safe['objects'], [{'[REDACTED]': '[REDACTED]'}])
                    self.assertEqual(safe['tuple'], ['[REDACTED]'])
                    bodies = [safe['actual_request'], json.loads(safe['serialized_record'])['actual_request'],
                        json.loads(json.loads(safe['nested_twice'])['serialized_record'])['actual_request']]
                    for actual in bodies:
                        decoded_body = json.loads(actual)
                        decoded_state = json.loads(decoded_body['state'])
                        self.assertEqual(decoded_state['observations'][0]['api_key'], '[REDACTED]')
                        self.assertEqual(decoded_state['goal'], 'Move safely.')
                        self.assertEqual(decoded_body['questions']['q0']['criteria']['A'], 'Wait.')
                    self.assertNotIn(secret, json.dumps(safe, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
