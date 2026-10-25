import tempfile
import unittest
from pathlib import Path

from native_adapter import NativeAdapter
from run_store import Blocked


class Supervisor:
    def __init__(self):
        self.launches = []

    def launch(self, seat, argv, cwd):
        self.launches.append((seat, argv, cwd))

    def request_stop(self, seat, reason):
        return type('Evidence', (), {'seat': seat, 'freeze_seconds': 0.01,
                                      'active': 0, 'stable': True})()


def capture(seat, session, brief, cwd):
    return [
        {'type': 'reasoning', 'text': 'private'},
        {'type': 'tool', 'name': 'read', 'output': 'private file'},
        {'type': 'tool', 'name': 'bash', 'denied': True},
        {'type': 'final', 'text': 'project-safe final'},
        {'type': 'usage', 'input_tokens': 2, 'output_tokens': 3},
    ]


class NativeAdapterTests(unittest.TestCase):
    def test_filters_native_events_and_binds_distinct_sessions(self):
        supervisor = Supervisor()
        adapter = NativeAdapter(supervisor, capture)
        with tempfile.TemporaryDirectory() as folder:
            result = adapter.invoke('E2', 'pi-e2', 'brief', Path(folder))
            self.assertEqual(result['session'], 'pi-e2')
            self.assertEqual(result['tool_names'], ['read', 'bash'])
            self.assertTrue(result['denied'])
            self.assertEqual(result['final_text'], 'project-safe final')
            self.assertEqual(result['usage_available'], True)
            self.assertNotIn('private', str(result))
            with self.assertRaisesRegex(Blocked, 'seat_busy'):
                adapter.invoke('E2', 'pi-other', 'brief', Path(folder))
            with self.assertRaisesRegex(Blocked, 'session_already_bound'):
                adapter.invoke('R', 'pi-e2', 'brief', Path(folder))

    def test_alternate_secret_spellings_and_raw_events_never_reach_results(self):
        secrets = [
            'token ghp_AbCdEf0123456789abcdefghijklmnopqrstuvwxyz leaked',
            'config password=hunter2 here',
            'route sk-proj-AbCdEf0123456789abcdefghijklmnopqrstuvwxyz',
            '-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----',
            'header Authorization: Bearer abc.def.ghi',
        ]
        for secret in secrets:
            with self.subTest(secret=secret[:30]):
                def capture(*_args, text=secret):
                    return [{'type': 'final', 'text': text}]
                with tempfile.TemporaryDirectory() as folder:
                    with self.assertRaisesRegex(Blocked, 'unfiltered_native_output'):
                        NativeAdapter(Supervisor(), capture).invoke('E2', 'session-token',
                                                                    'brief', Path(folder))

        def capture(*_args):
            return [{'type': 'raw', 'text': 'private reasoning password=hunter2'},
                    {'type': 'final', 'text': 'project-safe final'}]
        with tempfile.TemporaryDirectory() as folder:
            result = NativeAdapter(Supervisor(), capture).invoke('E2', 'session-token',
                                                                 'brief', Path(folder))
            self.assertNotIn('hunter2', str(result))
            self.assertEqual(result['final_text'], 'project-safe final')

    def test_stop_returns_partial_when_service_or_permission_is_unknown(self):
        adapter = NativeAdapter(Supervisor(), capture)
        report = adapter.stop('E1')
        self.assertEqual(report['seat'], 'E1')
        self.assertFalse(report['verified'])
        self.assertEqual(report['status'], 'partial')


if __name__ == '__main__':
    unittest.main()
