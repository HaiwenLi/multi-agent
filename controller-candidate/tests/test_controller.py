import tempfile
import unittest
from pathlib import Path

from controller import Controller
from native_adapter import NativeAdapter
from run_store import Blocked, RunStore


def seed():
    return {'run': 'controller-run', 'roles': {'M': 'opencode', 'E1': 'opencode', 'E2': 'pi', 'R': 'pi'},
            'limits': {'starts': 12}, 'review_policy': {}}


class Native:
    def __init__(self):
        self.calls = []

    def invoke(self, seat, session, brief, cwd):
        self.calls.append((seat, session, brief, cwd))
        return {'seat': seat, 'session': session, 'final_text': 'safe'}

    def complete(self, seat):
        pass


def setup_assigned_task(store, task, seat, revision):
    store.append('assignment', {'task': task, 'seat': seat, 'epoch': 1, 'revision': revision,
                                'session': 's-' + task}, capability=store.capabilities['manager'])
    store.append('ack', {'task': task, 'seat': seat, 'epoch': 1, 'revision': revision},
                 capability=store.capabilities[seat])
    store.append('inbox', {'run': 'controller-run', 'task': task, 'epoch': 1, 'action_id': 'work',
                           'seat': seat, 'revision': revision, 'body': task})


class ControllerTests(unittest.TestCase):
    def test_real_native_adapter_persists_filtered_artifact_before_finish(self):
        class Supervisor:
            def launch(self, *_):
                pass
        def capture(*_):
            return [{'type': 'final', 'text': 'project-safe final'}]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with RunStore(root, seed()) as store:
                store.append('assignment', {'task': 'A', 'seat': 'E1', 'epoch': 1, 'revision': 'revA',
                                            'session': 'ses-a'}, capability=store.capabilities['manager'])
                store.append('ack', {'task': 'A', 'seat': 'E1', 'epoch': 1, 'revision': 'revA'},
                             capability=store.capabilities['E1'])
                store.append('inbox', {'run': 'controller-run', 'task': 'A', 'epoch': 1,
                                       'action_id': 'work', 'seat': 'E1', 'revision': 'revA', 'body': 'A'})
                result = Controller(store, None, None, NativeAdapter(Supervisor(), capture)).dispatch_one('E1')
                artifact = root / 'artifacts' / result['result']['artifact']['path']
                self.assertEqual(artifact.read_text(encoding='utf-8'), 'project-safe final')
                self.assertEqual(store.snapshot()['finishes'][0]['artifact_digest'],
                                 result['result']['artifact']['sha256'])

    def test_dispatches_isolated_seats_once_and_recovers_paused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with RunStore(root, seed()) as store:
                store.append('assignment', {'task': 'A', 'seat': 'E1', 'epoch': 1, 'revision': 'revA',
                                            'session': 'ses-a'}, capability=store.capabilities['manager'])
                store.append('assignment', {'task': 'B', 'seat': 'E2', 'epoch': 1, 'revision': 'revB',
                                            'session': 'pi-b'}, capability=store.capabilities['manager'])
                store.append('ack', {'task': 'A', 'seat': 'E1', 'epoch': 1, 'revision': 'revA'}, capability=store.capabilities['E1'])
                store.append('ack', {'task': 'B', 'seat': 'E2', 'epoch': 1, 'revision': 'revB'}, capability=store.capabilities['E2'])
                for task, seat, revision in [('A', 'E1', 'revA'), ('B', 'E2', 'revB')]:
                    store.append('inbox', {'run': 'controller-run', 'task': task, 'epoch': 1,
                                           'action_id': 'work', 'seat': seat, 'revision': revision,
                                           'body': task})
                native = Native()
                controller = Controller(store, None, None, native)
                self.assertEqual(controller.dispatch_one('E1')['task'], 'A')
                self.assertEqual(controller.dispatch_one('E2')['task'], 'B')
                self.assertEqual({call[0] for call in native.calls}, {'E1', 'E2'})
                with self.assertRaisesRegex(Blocked, 'no_dispatchable_work'):
                    controller.dispatch_one('E1')
            with RunStore(root, seed()) as restarted:
                result = Controller(restarted, None, None, Native()).recover([], {})
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['reason'], 'durable_reconciliation_required')

    def test_budget_and_concurrency_limits_block_before_new_intent(self):
        with tempfile.TemporaryDirectory() as folder:
            tight = seed()
            tight['limits'] = {'starts': 1, 'active': 2}
            with RunStore(Path(folder), tight) as store:
                setup_assigned_task(store, 'A', 'E1', 'revA')
                setup_assigned_task(store, 'B', 'E2', 'revB')
                native = Native()
                controller = Controller(store, None, None, native)
                self.assertEqual(controller.dispatch_one('E1')['task'], 'A')
                with self.assertRaisesRegex(Blocked, 'budget_exhausted'):
                    controller.dispatch_one('E2')
                self.assertEqual(len(native.calls), 1)
                self.assertEqual(len(store.snapshot()['intents']), 1)

        with tempfile.TemporaryDirectory() as folder:
            tight = seed()
            tight['limits'] = {'starts': 12, 'active': 1}
            with RunStore(Path(folder), tight) as store:
                setup_assigned_task(store, 'A', 'E1', 'revA')
                store.append('intent', {'run': 'controller-run', 'task': 'B', 'epoch': 1,
                                        'action_id': 'occupied', 'seat': 'E2'})
                native = Native()
                controller = Controller(store, None, None, native)
                with self.assertRaisesRegex(Blocked, 'concurrency_limit'):
                    controller.dispatch_one('E1')
                self.assertEqual(native.calls, [])
                self.assertEqual(len(store.snapshot()['intents']), 1)

    def test_stale_review_or_missing_check_never_accepts(self):
        with tempfile.TemporaryDirectory() as folder:
            with RunStore(Path(folder), seed()) as store:
                controller = Controller(store, None, None, Native())
                submission = {'task': 'A', 'revision': 'revA', 'epoch': 1, 'current_epoch': 1,
                              'author': 'E1', 'files': ['code.py']}
                policy = {'task': 'A', 'revision': 'revA', 'required': {'correctness': 'R'},
                          'checks': ['unit'], 'integration': False}
                review = {'scope': 'correctness', 'reviewer': 'R', 'revision': 'old', 'epoch': 1,
                          'approve': True, 'blockers': [], 'independently_reproduced': True}
                self.assertFalse(controller.submit_and_review(submission, [review], [], policy)['accepted'])


if __name__ == '__main__':
    unittest.main()
