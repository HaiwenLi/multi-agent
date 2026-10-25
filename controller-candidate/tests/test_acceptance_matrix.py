"""End-to-end acceptance matrix using only deterministic boundary fakes."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from controller import Controller
from mail_adapter import MailAdapter
from native_adapter import NativeAdapter
from operator_control import Operator
from run_store import Blocked, RunStore


def seed():
    return {'run': 'matrix-run', 'roles': {'M': 'opencode', 'E1': 'opencode', 'E2': 'pi', 'R': 'pi'},
            'limits': {'starts': 12}, 'review_policy': {}}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Transport:
    def __init__(self, events):
        self.events = events

    def inbox_events(self, *_args, **_kwargs):
        return list(self.events)

    def thread_history(self, _thread):
        return []


class Supervisor:
    def launch(self, *_args):
        pass

    def stop(self, _seat):
        return {'active': 0, 'children_stopped': True, 'services_stopped': True, 'verified': True}


class AcceptanceMatrixTests(unittest.TestCase):
    def test_forged_ack_is_not_dispatchable_and_exact_capability_ack_is(self):
        event = {'run': 'matrix-run', 'task': 'A', 'epoch': 1, 'action_id': 'work', 'seat': 'E1',
                 'revision': 'revA', 'body': 'brief', 'message_id': 1, 'cursor': '1', 'sender': 'M'}
        with tempfile.TemporaryDirectory() as folder:
            with RunStore(Path(folder), seed()) as store:
                manager, executor = store.capabilities['manager'], store.capabilities['E1']
                store.append('assignment', {'task': 'A', 'seat': 'E1', 'epoch': 1, 'revision': 'revA',
                                            'session': 's1', 'acknowledged': True}, capability=manager)
                MailAdapter(Transport([event]), store).poll('E1')
                controller = Controller(store, None, None, NativeAdapter(Supervisor(),
                                        lambda *_: [{'type': 'final', 'text': 'safe'}]))
                with self.assertRaisesRegex(Blocked, 'assignment_not_acknowledged'):
                    controller.dispatch_one('E1')
                store.append('ack', {'task': 'A', 'seat': 'E1', 'epoch': 1, 'revision': 'revA'},
                             capability=executor)
                self.assertEqual(controller.dispatch_one('E1')['task'], 'A')

    def test_recovery_requires_matching_durable_reconciliation(self):
        history, liveness = [{'message_id': 1}], {'E1': {'active': 0}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with RunStore(root, seed()) as store:
                store.append('pause', {'source': 'operator'}, capability=store.capabilities['operator'])
            with RunStore(root, seed()) as resumed:
                controller = Controller(resumed, None, None, None)
                self.assertEqual(controller.recover(history, liveness)['reason'],
                                 'durable_reconciliation_required')
                controller.reconcile(history, liveness, capability=resumed.capabilities['operator'])
                self.assertEqual(controller.recover(history, liveness)['status'], 'ready')
                self.assertEqual(controller.recover([], liveness)['reason'], 'reconciliation_inputs_changed')

    def test_transfer_and_stop_verification_require_their_local_capabilities(self):
        with tempfile.TemporaryDirectory() as folder:
            with RunStore(Path(folder), seed()) as store:
                manager = store.capabilities['manager']
                operator = store.capabilities['operator']
                store.append('assignment', {'task': 'A', 'seat': 'E1', 'epoch': 1,
                                            'revision': 'revA', 'session': 's1'}, capability=manager)
                with self.assertRaisesRegex(Blocked, 'missing_or_forged_capability'):
                    store.append('verify_stop', {'seat': 'E1', 'active': 0,
                                                 'children_stopped': True, 'services_stopped': True})
                store.append('stop', {'seat': 'E1'}, capability=operator)
                store.append('verify_stop', {'seat': 'E1', 'active': 0,
                                             'children_stopped': True, 'services_stopped': True},
                             capability=operator)
                with self.assertRaisesRegex(Blocked, 'missing_or_forged_capability'):
                    store.append('transfer', {'task': 'A', 'seat': 'E2', 'epoch': 2,
                                              'revision': 'revB', 'session': 's2'})
                store.append('transfer', {'task': 'A', 'seat': 'E2', 'epoch': 2,
                                          'revision': 'revB', 'session': 's2'}, capability=manager)
                self.assertFalse(store.snapshot()['tasks']['A']['acknowledged'])

    def test_manifest_bound_acceptance_and_human_rejection_withdraw_dependents(self):
        manifest_digest = digest(['brief.md', 'code.py', 'test.py'])
        submission = {'task': 'B', 'revision': 'revB', 'epoch': 1, 'current_epoch': 1, 'author': 'E2',
                      'files': ['brief.md', 'code.py', 'test.py'], 'manifest_digest': manifest_digest,
                      'dependencies': []}
        policy = {'task': 'B', 'revision': 'revB', 'manifest_digest': manifest_digest,
                  'required': {'correctness': 'R'}, 'checks': ['unit'], 'integration': False}
        reviews = [{'scope': 'correctness', 'reviewer': 'R', 'revision': 'revB', 'epoch': 1,
                    'manifest_digest': manifest_digest, 'approve': True, 'blockers': [],
                    'independently_reproduced': True}]
        checks = [{'name': 'unit', 'revision': 'revB', 'epoch': 1, 'manifest_digest': manifest_digest,
                   'producer': 'R', 'exit_code': 0}]
        with tempfile.TemporaryDirectory() as folder:
            with RunStore(Path(folder), seed()) as store:
                controller = Controller(store, None, None, None)
                self.assertTrue(controller.submit_and_review(submission, reviews, checks, policy)['accepted'])
                store.append('review', {'task': 'combined', 'revision': 'revC', 'epoch': 1,
                                        'manifest_digest': 'c' * 64, 'dependencies': ['B'],
                                        'accepted': True, 'reasons': []})
                Operator(store, Supervisor()).command('reject B revB')
                snapshot = store.snapshot()
                self.assertNotIn('B', snapshot['accepted'])
                self.assertNotIn('combined', snapshot['accepted'])
                self.assertEqual(set(snapshot['withdrawn']), {'B', 'combined'})
                self.assertFalse(controller.submit_and_review(
                    dict(submission, manifest_digest='changed'), reviews, checks, policy)['accepted'])


if __name__ == '__main__':
    unittest.main()
