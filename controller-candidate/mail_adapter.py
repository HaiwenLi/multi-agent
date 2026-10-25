"""Injected deterministic Mail boundary; it never turns Mail text into authority."""
import hashlib
import json

from run_store import Blocked


def _action_key(item):
    return tuple(item[name] for name in ('run', 'task', 'epoch', 'action_id'))


def _action_digest(item):
    payload = {name: value for name, value in item.items() if name not in ('message_id', 'cursor')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()


class MailAdapter:
    def __init__(self, transport, store):
        self.transport = transport
        self.store = store

    def poll(self, seat: str) -> list[dict]:
        events = self.transport.inbox_events(seat, self.store.snapshot()['cursor'], limit=20)
        queued = []
        snapshot = self.store.snapshot()
        current_cursor = snapshot['cursor']
        known = {_action_key(item): _action_digest(item) for item in snapshot['inbox']}
        for event in events:
            if event.get('status') == 'expired':
                raise Blocked('expired_cursor')
            required = ('run', 'task', 'epoch', 'action_id', 'seat', 'revision', 'message_id', 'cursor', 'sender')
            if any(name not in event for name in required) or event['seat'] != seat:
                raise Blocked('invalid_mail_envelope')
            if event['run'] != snapshot['run']:
                raise Blocked('wrong_run')
            try:
                if current_cursor is not None and int(event['cursor']) > int(current_cursor) + 1:
                    raise Blocked('ahead_cursor')
            except ValueError as error:
                raise Blocked('invalid_cursor') from error
            key, digest = _action_key(event), _action_digest(event)
            if key in known:
                if known[key] != digest:
                    raise Blocked('conflicting_action')
            else:
                self.store.append('inbox', dict(event))
                known[key] = digest
                queued.append(dict(event))
            self.store.append('cursor', {'value': event['cursor']})
            current_cursor = event['cursor']
        return queued

    def send_once(self, application_id: str, thread: str, body: bytes) -> dict:
        if not application_id or not thread or not isinstance(body, bytes) or len(body) > 8192:
            raise Blocked('invalid_outbox')
        snapshot = self.store.snapshot()
        existing = snapshot['outbox'].get(application_id)
        if existing:
            if existing['thread'] != thread:
                raise Blocked('conflicting_outbox')
            receipt = snapshot['receipts'].get(application_id)
            if receipt:
                return dict(receipt)
            matches = [item for item in self.transport.thread_history(thread)
                       if item.get('application_id') == application_id and item.get('thread') == thread
                       and item.get('message_id') is not None]
            if len(matches) != 1:
                raise Blocked('uncertain_send')
            receipt = {'application_id': application_id, 'message_id': matches[0]['message_id']}
            self.store.append('send_receipt', receipt)
            return receipt
        body_sha256 = hashlib.sha256(body).hexdigest()
        self.store.append('outbox', {'application_id': application_id, 'thread': thread,
                                    'body_sha256': body_sha256})
        response = self.transport.send('controller', 'mail', thread, application_id, body)
        if not isinstance(response, dict) or not response.get('message_id'):
            raise Blocked('uncertain_send')
        receipt = {'application_id': application_id, 'message_id': response['message_id']}
        self.store.append('send_receipt', receipt)
        return receipt
