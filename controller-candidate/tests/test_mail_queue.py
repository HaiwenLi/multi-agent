import tempfile
import unittest
from pathlib import Path

from mail_queue import MailQueue


def envelope(seat='E1', action='work', body='brief', mid=1):
    return dict(run='new-run', task='A', epoch=1, action_id=action, seat=seat,
                revision='revA', body=body, message_id=mid, cursor=str(mid))


class QueueTests(unittest.TestCase):
    def test_protected_tree_refuses_candidate_mail_state(self):
        with self.assertRaises(ValueError):
            MailQueue(Path(__file__).resolve().parent.parent.parent / 'protected' / 'any-state')

    def test_busy_seat_and_duplicate_delivery_do_not_reinvoke(self):
        with tempfile.TemporaryDirectory() as folder:
            with MailQueue(Path(folder), authorize=lambda item: item['revision'] == 'revA') as q:
                self.assertEqual(q.ingest(envelope()), 'queued')
                self.assertEqual(q.ingest(envelope(mid=2)), 'duplicate')
                self.assertEqual(q.ingest(envelope(action='next', mid=3)), 'queued')
                first = q.next_for_seat('E1')
                self.assertEqual(first['action_id'], 'work')
                self.assertIsNone(q.next_for_seat('E1'))
                q.complete(first, 'artifact-sha')
                self.assertEqual(q.next_for_seat('E1')['action_id'], 'next')
                with self.assertRaises(ValueError):
                    q.ingest(envelope(body='tampered', mid=4))

    def test_uncertain_intent_restart_pauses_not_replays(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            q = MailQueue(root, authorize=lambda item: item['revision'] == 'revA')
            q.ingest(envelope())
            self.assertEqual(q.next_for_seat('E1')['action_id'], 'work')
            q.close()
            with MailQueue(root, authorize=lambda item: item['revision'] == 'revA') as resumed:
                self.assertTrue(resumed.paused)
                self.assertIsNone(resumed.next_for_seat('E1'))
                self.assertEqual(resumed.reconcile([], '1')['status'], 'blocked')
                self.assertEqual(resumed.reconcile([], '1')['reason'], 'uncertain_native_intent')

    def test_outbox_reconciles_remote_accept_before_receipt_without_resend(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with MailQueue(root) as q:
                q.prepare_send('reply-1', 'thread-1', b'safe')
            with MailQueue(root) as q:
                self.assertEqual(q.reconcile([{'application_id': 'reply-1', 'thread': 'thread-1',
                                                'message_id': 23}], None)['status'], 'ready')
                self.assertEqual(q.outbox['reply-1']['message_id'], 23)
                with self.assertRaises(ValueError):
                    q.prepare_send('reply-1', 'thread-1', b'changed')

    def test_cursor_errors_typed_and_late_review_requires_history(self):
        with tempfile.TemporaryDirectory() as folder:
            with MailQueue(Path(folder)) as q:
                q.ingest(envelope())
                q.commit_cursor('1')
                self.assertEqual(q.reconcile([], '99')['reason'], 'ahead_cursor')
                self.assertEqual(q.reconcile([{'cursor': '10', 'message_id': 10}], '1',
                                             expired=True)['reason'], 'expired_cursor')
                self.assertEqual(q.reconcile([], '1', reviewer_late=True)['reason'], 'history_required')
                self.assertEqual(q.cursor, '1')

    def test_truncated_journal_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with MailQueue(root) as q:
                q.ingest(envelope())
            with (root / 'mail-journal.jsonl').open('ab') as stream:
                stream.write(b'{')
            with self.assertRaises(ValueError):
                MailQueue(root)


if __name__ == '__main__':
    unittest.main()
