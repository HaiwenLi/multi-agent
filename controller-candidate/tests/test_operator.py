import json
import importlib.util
import tempfile
import unittest
from pathlib import Path

from run_store import Blocked, RunStore


def load_operator():
    path = Path(__file__).parents[1] / 'operator_control.py'
    spec = importlib.util.spec_from_file_location('mvp_local_operator', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Operator


Operator = load_operator()


def seed():
    return {'run': 'operator-run', 'roles': {'M': 'opencode', 'E1': 'opencode', 'E2': 'pi', 'R': 'pi'},
            'limits': {'starts': 12}, 'review_policy': {}}


class Supervisor:
    def __init__(self):
        self.stops = []

    def stop(self, seat):
        self.stops.append(seat)
        return {'active': 0, 'children_stopped': True, 'services_stopped': True, 'verified': True}


class OperatorTests(unittest.TestCase):
    def test_stop_freezes_before_supervisor_and_unknown_seat_is_non_mutating(self):
        with tempfile.TemporaryDirectory() as folder:
            with RunStore(Path(folder), seed()) as store:
                store.append('assignment', {'task': 'A', 'seat': 'E1', 'epoch': 1, 'revision': 'revA', 'session': 's1'}, capability=store.capabilities['manager'])
                supervisor = Supervisor()
                operator = Operator(store, supervisor)
                result = operator.command('stop E1')
                self.assertTrue(result['verified'])
                self.assertEqual([event['kind'] for event in store.events[-3:]],
                                 ['pause', 'stop', 'verify_stop'])
                self.assertEqual(supervisor.stops, ['E1'])
                before = len(store.events)
                with self.assertRaises(Blocked):
                    operator.command('stop missing')
                self.assertEqual(len(store.events), before)
                status = json.loads((Path(folder) / 'state' / 'status.json').read_text(encoding='utf-8'))
                self.assertEqual(status['facts']['paused'], True)

    def test_reject_withdraws_and_resume_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as folder:
            with RunStore(Path(folder), seed()) as store:
                store.append('assignment', {'task': 'B', 'seat': 'E2', 'epoch': 1, 'revision': 'revB', 'session': 's2'}, capability=store.capabilities['manager'])
                operator = Operator(store, Supervisor())
                operator.command('reject B revB')
                self.assertIn('B', store.snapshot()['withdrawn'])
                store.append('intent', {'run': 'operator-run', 'task': 'B', 'epoch': 1,
                                        'action_id': 'work', 'seat': 'E2'})
                with self.assertRaisesRegex(Blocked, 'reconciliation_required'):
                    operator.command('resume')


if __name__ == '__main__':
    unittest.main()
