import sys
import tempfile
import time
import unittest
from pathlib import Path

from native_supervisor import Supervisor, native_argv


class SupervisorTests(unittest.TestCase):
    def test_native_argv_targets_original_sessions_without_bypass(self):
        self.assertIn('--session', native_argv('E1', 'session-token', 'hello'))
        self.assertIn('session-token', native_argv('E1', 'session-token', 'hello'))
        self.assertIn('--session-id', native_argv('E2', 'session-token', 'hello'))
        for argv in (native_argv('E1', 'session-token', 'x'), native_argv('E2', 'session-token', 'x')):
            self.assertNotIn('--no-skills', argv)
            self.assertNotIn('--auto', argv)
        with self.assertRaises(ValueError):
            native_argv('R', '', 'x')

    def test_stop_freezes_second_dispatch_and_kills_child_counter(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            counter = root / 'counter.txt'
            child = root / 'child.py'
            child.write_text("import sys, time\nwhile True:\n    with open(sys.argv[1], 'a') as f:\n        f.write('x\\n')\n    time.sleep(.15)\n", encoding='utf-8')
            parent = ('import subprocess,sys,time; subprocess.Popen([sys.executable,'
                      'sys.argv[2],sys.argv[1]]); time.sleep(30)')
            supervisor = Supervisor()
            supervisor.launch('E2', [sys.executable, '-c', parent, str(counter), str(child)], root)
            try:
                deadline = time.monotonic() + 5
                while not counter.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(counter.exists())
                with self.assertRaises(ValueError):
                    supervisor.launch('E2', [sys.executable, '-c', 'pass'], root)
                report = supervisor.request_stop('E2', 'human')
                self.assertEqual(report.active, 0)
                self.assertLessEqual(report.freeze_seconds, 1)
                self.assertTrue(report.stable)
                before = counter.read_bytes()
                time.sleep(.2)
                self.assertEqual(counter.read_bytes(), before)
                self.assertFalse(supervisor.can_reassign('A'))
                with self.assertRaises(ValueError):
                    supervisor.launch('E2', [sys.executable, '-c', 'pass'], root)
            finally:
                supervisor.close()

    def test_unknown_liveness_blocks_reassignment(self):
        supervisor = Supervisor()
        supervisor.freeze('E1', 'human')
        self.assertFalse(supervisor.can_reassign('A'))
        self.assertFalse(supervisor.mark_verified('E1', active=None, children_stopped=True,
                                                  services_stopped=True))
        self.assertFalse(supervisor.can_reassign('A'))
        supervisor.close()

    def test_verified_shutdown_alone_does_not_grant_transfer_without_epoch_ack(self):
        supervisor = Supervisor()
        supervisor.freeze('E1', 'human')
        self.assertTrue(supervisor.mark_verified('E1', active=0, children_stopped=True,
                                                 services_stopped=True))
        state = {'tasks': {'A': {'stopped': True, 'previous_owner': 'E1', 'epoch': 2, 'owner': 'E2',
                                'revision': 'rev2', 'ack': None}}}
        self.assertFalse(supervisor.can_reassign('A', state))
        state['tasks']['A']['ack'] = ['A', 'E2', 2, 'rev2']
        self.assertTrue(supervisor.can_reassign('A', state))
        supervisor.close()


if __name__ == '__main__':
    unittest.main()
