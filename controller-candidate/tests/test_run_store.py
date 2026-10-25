import tempfile
import unittest
from pathlib import Path

from run_store import Blocked, RunStore


def seed():
    return {
        'run': 'mvp-test-run',
        'roles': {'M': 'opencode', 'E1': 'opencode', 'E2': 'pi', 'R': 'pi'},
        'limits': {'starts': 12},
        'review_policy': {'required': {'integration': 'R'}},
    }


class RunStoreTests(unittest.TestCase):
    def test_reopen_pauses_and_preserves_durable_intent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with RunStore(root, seed()) as store:
                store.append('inbox', {'run': 'mvp-test-run', 'task': 'A', 'epoch': 1,
                                       'action_id': 'one', 'seat': 'E1', 'body': 'brief'})
                store.append('intent', {'run': 'mvp-test-run', 'task': 'A', 'epoch': 1,
                                        'action_id': 'one', 'seat': 'E1'})
                self.assertFalse(store.paused)
                self.assertEqual(store.snapshot()['budget']['starts'], 1)
            with RunStore(root, seed()) as resumed:
                self.assertTrue(resumed.paused)
                self.assertEqual(len(resumed.snapshot()['intents']), 1)
                self.assertEqual(resumed.snapshot()['budget']['starts'], 1)
                self.assertTrue(resumed.snapshot()['intents'][0]['uncertain'])

    def test_tampered_or_partial_log_fails_without_rewriting_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with RunStore(root, seed()) as store:
                store.append('pause', {'reason': 'test'}, capability=store.capabilities['operator'])
            log = root / 'state' / 'events.jsonl'
            original = log.read_bytes()
            log.write_bytes(original[:-2] + b'X\n')
            tampered = log.read_bytes()
            with self.assertRaises(Blocked):
                RunStore(root, seed())
            self.assertEqual(log.read_bytes(), tampered)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with RunStore(root, seed()) as store:
                store.append('pause', {'reason': 'test'}, capability=store.capabilities['operator'])
            log = root / 'state' / 'events.jsonl'
            with log.open('ab') as stream:
                stream.write(b'{"partial"')
            partial = log.read_bytes()
            with self.assertRaises(Blocked):
                RunStore(root, seed())
            self.assertEqual(log.read_bytes(), partial)

    def test_single_writer_reserved_integrity_and_protected_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with RunStore(root, seed()) as store:
                with self.assertRaises(Blocked):
                    RunStore(root, seed())
                with self.assertRaises(Blocked):
                    store.append('pause', {'sha256': 'forged'}, capability=store.capabilities['operator'])
        with self.assertRaises(Blocked):
            RunStore(Path(__file__).resolve().parent.parent.parent / 'protected' / 'new-state', seed())


if __name__ == '__main__':
    unittest.main()
