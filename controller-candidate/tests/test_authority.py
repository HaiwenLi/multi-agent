import tempfile
import unittest
from pathlib import Path

from authority import Journal, Refused, apply


def initial():
    return {
        'manager': 'M', 'seats': {'M': 'opencode', 'E1': 'opencode', 'E2': 'pi', 'R': 'pi'},
        'tasks': {}, 'limits': {'starts': 12}, 'spent': {'starts': 0},
        'actions': {}, 'paused': False, 'withdrawals': [],
    }


def cmd(kind, origin='manager', actor='M', task='A', epoch=1, action_id=None, **fields):
    return dict(kind=kind, origin=origin, actor=actor, task=task, epoch=epoch,
                action_id=action_id or kind, **fields)


class AuthorityTests(unittest.TestCase):
    def assigned(self):
        return apply(initial(), cmd('assign', owner='E1', revision='revA'))

    def test_ack_is_exact_and_required_for_dispatch(self):
        state = self.assigned()
        with self.assertRaises(Refused):
            apply(state, cmd('start', actor='E1', origin='executor', revision='revA'))
        with self.assertRaises(Refused):
            apply(state, cmd('ack', actor='E1', origin='executor', revision='wrong'))
        state = apply(state, cmd('ack', actor='E1', origin='executor', revision='revA'))
        state = apply(state, cmd('start', actor='E1', origin='executor', revision='revA'))
        self.assertEqual(state['spent']['starts'], 1)
        self.assertEqual(state['tasks']['A']['status'], 'running')

    def test_spoofed_mail_and_conflicting_replay_refused(self):
        state = self.assigned()
        self.assertEqual(apply(state, cmd('assign', owner='E1', revision='revA')), state)
        with self.assertRaises(Refused):
            apply(state, cmd('assign', owner='E2', revision='revA'))
        with self.assertRaises(Refused):
            apply(state, cmd('stop', origin='mail', actor='human'))
        with self.assertRaises(Refused):
            apply(state, cmd('assign', origin='mail', owner='E2', revision='revB'))

    def test_stop_precedes_manager_and_transfer_requires_stop_and_new_ack(self):
        state = self.assigned()
        state = apply(state, cmd('ack', actor='E1', origin='executor', revision='revA'))
        state = apply(state, cmd('start', actor='E1', origin='executor', revision='revA'))
        state = apply(state, cmd('stop', origin='operator', actor='human'))
        self.assertTrue(state['tasks']['A']['frozen'])
        with self.assertRaises(Refused):
            apply(state, cmd('transfer', epoch=1, owner='E2', revision='revB'))
        state = apply(state, cmd('verify_stop', origin='operator', actor='human', active=0,
                                 children_stopped=True, services_stopped=True))
        state = apply(state, cmd('transfer', owner='E2', revision='revB'))
        self.assertEqual(state['tasks']['A']['epoch'], 2)
        self.assertEqual(state['spent']['starts'], 1)
        with self.assertRaises(Refused):
            apply(state, cmd('start', origin='executor', actor='E1', epoch=1, revision='revA'))
        with self.assertRaises(Refused):
            apply(state, cmd('start', origin='executor', actor='E2', epoch=2, revision='revB'))
        state = apply(state, cmd('ack', origin='executor', actor='E2', epoch=2, revision='revB'))
        state = apply(state, cmd('start', origin='executor', actor='E2', epoch=2, revision='revB'))
        self.assertEqual(state['spent']['starts'], 2)

    def test_rejected_owner_cannot_return_and_limits_survive_replay(self):
        state = self.assigned()
        state = apply(state, cmd('stop', origin='operator', actor='human'))
        state = apply(state, cmd('verify_stop', origin='operator', actor='human', active=0,
                                 children_stopped=True, services_stopped=True))
        state = apply(state, cmd('reject', origin='operator', actor='human', revision='revA'))
        with self.assertRaises(Refused):
            apply(state, cmd('transfer', owner='E1', revision='revB'))


class JournalTests(unittest.TestCase):
    def test_protected_tree_refuses_new_journal(self):
        with self.assertRaises(Refused):
            Journal(Path(__file__).resolve().parent.parent.parent / 'protected' / 'state', initial())

    def test_reopens_paused_preserving_budget_and_prevents_second_writer(self):
        with tempfile.TemporaryDirectory() as folder:
            manager, executor = object(), object()
            callers = {manager: ('manager', 'M'), executor: ('executor', 'E1')}
            with Journal(Path(folder), initial(), callers=callers) as journal:
                journal.execute(cmd('assign', owner='E1', revision='revA'), caller=manager)
                journal.execute(cmd('ack', actor='E1', origin='executor', revision='revA'), caller=executor)
                journal.execute(cmd('start', actor='E1', origin='executor', revision='revA'), caller=executor)
                with self.assertRaises(Refused):
                    Journal(Path(folder), initial())
            with Journal(Path(folder), initial(), callers=callers) as resumed:
                self.assertTrue(resumed.state['paused'])
                self.assertEqual(resumed.state['spent']['starts'], 1)
                with self.assertRaises(Refused):
                    resumed.execute(cmd('start', actor='E1', origin='executor', revision='revA', action_id='again'), caller=executor)

    def test_partial_tail_fails_closed_without_truncating_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = object()
            with Journal(root, initial(), callers={manager: ('manager', 'M')}) as journal:
                journal.execute(cmd('assign', owner='E1', revision='revA'), caller=manager)
            with (root / 'journal.jsonl').open('ab') as stream:
                stream.write(b'{"partial":')
            original = (root / 'journal.jsonl').read_bytes()
            with self.assertRaises(Refused):
                Journal(root, initial())
            self.assertEqual((root / 'journal.jsonl').read_bytes(), original)

    def test_unbound_caller_cannot_claim_operator_or_manager_authority(self):
        with tempfile.TemporaryDirectory() as folder:
            operator, manager, executor = object(), object(), object()
            with Journal(Path(folder), initial(), callers={operator: ('operator', 'human'),
                                                           manager: ('manager', 'M'),
                                                           executor: ('executor', 'E1')}) as journal:
                with self.assertRaises(Refused):
                    journal.execute(cmd('stop', origin='operator', actor='human'), caller=object())
                with self.assertRaises(Refused):
                    journal.execute(cmd('assign', origin='manager', actor='M', owner='E1', revision='revA'),
                                    caller=operator)
                journal.execute(cmd('assign', owner='E1', revision='revA'), caller=manager)
                journal.execute(cmd('stop', origin='operator', actor='human'), caller=operator)
                self.assertTrue(journal.state['tasks']['A']['frozen'])


if __name__ == '__main__':
    unittest.main()
