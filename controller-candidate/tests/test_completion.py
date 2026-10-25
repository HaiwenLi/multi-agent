import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from completion import notification, persist_completion


FIELDS = dict(run='next-run', task='A', epoch=2, revision='revA', ack='confirmed',
              sender='E2', recipient='M')


class CompletionTests(unittest.TestCase):
    def test_cjk_evidence_is_persisted_before_bounded_notification(self):
        with tempfile.TemporaryDirectory() as folder:
            data = persist_completion(Path(folder), 'A-2', '中' * 5000, dict(seat='E2'))
            self.assertEqual(data['sha256'], hashlib.sha256(('中' * 5000).encode()).hexdigest())
            self.assertEqual((Path(folder) / data['path']).read_text(encoding='utf-8'), '中' * 5000)
            result = notification(data, FIELDS)
            self.assertLessEqual(len(result), 8192)
            decoded = json.loads(result.decode('utf-8'))
            self.assertTrue(decoded['truncated'])
            self.assertEqual(decoded['epoch'], 2)
            self.assertEqual(decoded['artifact']['sha256'], data['sha256'])

    def test_mixed_unicode_limits_never_split_utf8(self):
        for size in (8191, 8192, 8193):
            with self.subTest(size=size):
                with tempfile.TemporaryDirectory() as folder:
                    data = persist_completion(Path(folder), str(size), 'x' * (size - 4) + '中🙂', {})
                    result = notification(data, FIELDS)
                    self.assertLessEqual(len(result), 8192)
                    self.assertIsInstance(json.loads(result.decode('utf-8'))['text'], str)

    def test_empty_and_ascii_stay_referenced(self):
        with tempfile.TemporaryDirectory() as folder:
            data = persist_completion(Path(folder), 'empty', '', {})
            self.assertEqual(json.loads(notification(data, FIELDS))['text'], '')
            data = persist_completion(Path(folder), 'ascii', 'x' * 8192, {})
            self.assertTrue(json.loads(notification(data, FIELDS))['truncated'])

    def test_required_fields_too_long_fail_without_corrupting_artifact(self):
        with tempfile.TemporaryDirectory() as folder:
            data = persist_completion(Path(folder), 'A', 'saved', {})
            with self.assertRaises(ValueError):
                notification(data, dict(FIELDS, revision='x' * 8192))
            self.assertEqual((Path(folder) / data['path']).read_text(encoding='utf-8'), 'saved')

    def test_path_escape_and_unfiltered_secrets_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                persist_completion(Path(folder), '../escape', 'x', {})
            with self.assertRaises(ValueError):
                persist_completion(Path(folder), 'A', 'Authorization: Bearer secret', {})

    def test_protected_evidence_is_never_a_writable_candidate_root(self):
        protected = Path(__file__).resolve().parent.parent.parent / 'protected' / 'evidence'
        with self.assertRaises(ValueError):
            persist_completion(protected, 'candidate-test', 'x', {})


if __name__ == '__main__':
    unittest.main()
