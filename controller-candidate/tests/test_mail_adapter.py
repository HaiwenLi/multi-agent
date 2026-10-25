import hashlib
import tempfile
import unittest
from pathlib import Path

from completion import notification, persist_completion
from mail_adapter import MailAdapter
from run_store import Blocked, RunStore


def seed():
    return {'run': 'mail-run', 'roles': {'M': 'opencode', 'E1': 'opencode', 'E2': 'pi', 'R': 'pi'},
            'limits': {'starts': 12}, 'review_policy': {},
            'senders': {'E1': ['M'], 'E2': ['M'], 'R': ['M']}}


class FakeTransport:
    def __init__(self, pages=None, history=None):
        self.pages = pages or []
        self.history = history or []
        self.sends = []

    def inbox_events(self, seat, after, limit=20):
        return list(self.pages)

    def message(self, seat, message_id):
        return next(event for event in self.pages if event['message_id'] == message_id)

    def thread_history(self, thread):
        return [item for item in self.history if item['thread'] == thread]

    def send(self, sender, recipient, thread, application_id, body):
        self.sends.append((sender, recipient, thread, application_id, body))
        return {'message_id': 41}


def envelope(body='brief', action_id='work', message_id=1, cursor='1', sender='M'):
    return {'run': 'mail-run', 'task': 'A', 'epoch': 1, 'action_id': action_id,
            'seat': 'E1', 'revision': 'revA', 'body': body, 'message_id': message_id,
            'cursor': cursor, 'sender': sender}


class MailAdapterTests(unittest.TestCase):
    def test_untrusted_sender_and_initial_ahead_cursor_refuse_without_side_effects(self):
        with tempfile.TemporaryDirectory() as folder:
            with RunStore(Path(folder), seed()) as store:
                adapter = MailAdapter(FakeTransport([envelope(sender='X')]), store)
                with self.assertRaisesRegex(Blocked, 'untrusted_sender'):
                    adapter.poll('E1')
                snapshot = store.snapshot()
                self.assertEqual(snapshot['inbox'], [])
                self.assertIsNone(snapshot['cursor'])

        with tempfile.TemporaryDirectory() as folder:
            with RunStore(Path(folder), seed()) as store:
                adapter = MailAdapter(FakeTransport([envelope(cursor='999')]), store)
                with self.assertRaisesRegex(Blocked, 'ahead_cursor'):
                    adapter.poll('E1')
                snapshot = store.snapshot()
                self.assertEqual(snapshot['inbox'], [])
                self.assertIsNone(snapshot['cursor'])

    def test_duplicate_is_queued_once_but_conflicting_action_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            with RunStore(Path(folder), seed()) as store:
                transport = FakeTransport([envelope(), envelope(message_id=2)])
                adapter = MailAdapter(transport, store)
                self.assertEqual(len(adapter.poll('E1')), 1)
                self.assertEqual(len(store.snapshot()['inbox']), 1)
                transport.pages = [envelope(body='changed', message_id=3)]
                with self.assertRaisesRegex(Blocked, 'conflicting_action'):
                    adapter.poll('E1')

    def test_cursor_failures_are_typed_and_late_reviewer_requires_history(self):
        with tempfile.TemporaryDirectory() as folder:
            with RunStore(Path(folder), seed()) as store:
                adapter = MailAdapter(FakeTransport([envelope(cursor='1')]), store)
                adapter.poll('E1')
                adapter.transport.pages = [envelope(action_id='later', message_id=2, cursor='9')]
                with self.assertRaisesRegex(Blocked, 'ahead_cursor'):
                    adapter.poll('E1')
                adapter.transport.pages = [{'status': 'expired'}]
                with self.assertRaisesRegex(Blocked, 'expired_cursor'):
                    adapter.poll('R')

    def test_remote_accept_before_local_receipt_is_reconciled_without_resend(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with RunStore(root, seed()) as store:
                store.append('outbox', {'application_id': 'reply-1', 'thread': 'thread-1',
                                        'body_sha256': hashlib.sha256(b'safe').hexdigest()})
            with RunStore(root, seed()) as resumed:
                transport = FakeTransport(history=[{'application_id': 'reply-1', 'thread': 'thread-1',
                                                    'message_id': 23}])
                result = MailAdapter(transport, resumed).send_once('reply-1', 'thread-1', b'safe')
                self.assertEqual(result['message_id'], 23)
                self.assertEqual(transport.sends, [])

    def test_outbox_retry_with_changed_bytes_is_refused_before_resend(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with RunStore(root, seed()) as store:
                MailAdapter(FakeTransport(), store).send_once('reply-1', 'thread-1', b'safe')
            with RunStore(root, seed()) as resumed:
                transport = FakeTransport(history=[{'application_id': 'reply-1', 'thread': 'thread-1',
                                                    'message_id': 23}])
                with self.assertRaisesRegex(Blocked, 'conflicting_outbox'):
                    MailAdapter(transport, resumed).send_once('reply-1', 'thread-1', b'changed')
                self.assertEqual(transport.sends, [])

    def test_utf8_artifact_is_persisted_before_bounded_send(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with RunStore(root, seed()) as store:
                completion = persist_completion(root / 'artifacts', 'A-1', '中' * 5000, {'seat': 'E1'})
                body = notification(completion, {'run': 'mail-run', 'task': 'A', 'epoch': 1,
                                                 'revision': 'revA', 'ack': 'yes', 'sender': 'E1',
                                                 'recipient': 'M'})
                self.assertTrue((root / 'artifacts' / completion['path']).exists())
                self.assertLessEqual(len(body), 8192)
                transport = FakeTransport()
                MailAdapter(transport, store).send_once('reply-2', 'thread-2', body)
                self.assertEqual(len(transport.sends), 1)


if __name__ == '__main__':
    unittest.main()
