"""Durable addressed-mail inbox/outbox, without authorizing execution from Mail."""
import hashlib
import json
import msvcrt
import os
from pathlib import Path


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


class MailQueue:
    def __init__(self, root: Path, authorize=None):
        self.root = Path(root)
        protected = Path(__file__).resolve().parent.parent / 'protected'
        if self.root.resolve() == protected or protected in self.root.resolve().parents:
            raise ValueError('protected tree is read-only to this candidate')
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root / 'mail-journal.jsonl'
        self.lock = (self.root / 'mail-writer.lock').open('a+b')
        self.locked = False
        self.authorize = authorize  # controller callback: actual owner, ACK, epoch and budget gate
        self.pending = []
        self.intents = {}
        self.completed = {}
        self.inflight = {}
        self.outbox = {}
        self.cursor = None
        self.seen = {}
        self.rows = []
        self.paused = False
        try:
            self.lock.seek(0)
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            self.locked = True
            if self.file.exists():
                raw = self.file.read_bytes()
                if raw and not raw.endswith(b'\n'):
                    raise ValueError('partial Mail journal')
                for line in raw.splitlines():
                    row = json.loads(line)
                    check = row.pop('sha256', None)
                    if (check != digest(row) or row.get('sequence') != len(self.rows) + 1
                            or row.get('previous') != (self.rows[-1]['sha256'] if self.rows else None)):
                        raise ValueError('Mail journal continuity failure')
                    row['sha256'] = check
                    self.rows.append(row)
                    self._apply(row['event'], row['data'])
                self.paused = True
        except Exception:
            self.close()
            raise

    def _apply(self, event, data):
        if event == 'inbox':
            item = data['item']
            key = self._key(item)
            self.seen[key] = data['digest']
            self.pending.append(item)
        elif event == 'intent':
            self.intents[data['key']] = data['item']
            self.inflight[data['item']['seat']] = data['key']
        elif event == 'completed':
            self.completed[data['key']] = data['artifact_digest']
            item = self.intents[data['key']]
            self.inflight.pop(item['seat'], None)
        elif event == 'outbox':
            self.outbox[data['id']] = dict(data)
        elif event == 'sent':
            self.outbox[data['id']]['message_id'] = data['message_id']
        elif event == 'cursor':
            self.cursor = data['value']

    def _append(self, event, data):
        row = dict(sequence=len(self.rows) + 1,
                   previous=self.rows[-1]['sha256'] if self.rows else None,
                   event=event, data=data)
        row['sha256'] = digest(row)
        with self.file.open('ab') as stream:
            stream.write(encoded(row) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        self.rows.append(row)
        self._apply(event, data)

    @staticmethod
    def _key(item):
        return '/'.join(str(item[k]) for k in ('run', 'task', 'epoch', 'action_id'))

    def ingest(self, envelope: dict) -> str:
        for field in ('run', 'task', 'epoch', 'action_id', 'seat', 'revision', 'message_id', 'cursor'):
            if field not in envelope:
                return 'blocked'
        key = self._key(envelope)
        payload = {name: value for name, value in envelope.items() if name not in ('message_id', 'cursor')}
        value = digest(payload)
        if key in self.seen:
            if self.seen[key] != value:
                raise ValueError('conflicting action payload')
            return 'duplicate'
        self._append('inbox', dict(item=dict(envelope), digest=value))
        return 'queued'

    def next_for_seat(self, seat: str):
        if self.paused or seat in self.inflight or self.authorize is None:
            return None
        for item in self.pending:
            key = self._key(item)
            if item['seat'] == seat and key not in self.intents and key not in self.completed:
                if not self.authorize(dict(item)):
                    return None
                self._append('intent', dict(key=key, item=item))
                return dict(item)
        return None

    def complete(self, item: dict, artifact_digest: str):
        key = self._key(item)
        if key not in self.intents or key in self.completed or not artifact_digest:
            raise ValueError('missing/duplicate native intent or artifact')
        self._append('completed', dict(key=key, artifact_digest=artifact_digest))

    def prepare_send(self, application_id: str, thread: str, body: bytes):
        if not application_id or not thread or len(body) > 8192:
            raise ValueError('invalid outbox request')
        value = hashlib.sha256(body).hexdigest()
        previous = self.outbox.get(application_id)
        if previous:
            if previous['digest'] != value or previous['thread'] != thread:
                raise ValueError('conflicting outbox id')
            return False
        self._append('outbox', dict(id=application_id, thread=thread, digest=value))
        return True

    def commit_cursor(self, value: str):
        if self.inflight:
            raise ValueError('cannot commit cursor with uncertain native work')
        self._append('cursor', dict(value=value))

    def reconcile(self, history: list[dict], cursor: str | None, *, expired=False, reviewer_late=False):
        if expired:
            return dict(status='blocked', reason='expired_cursor', cursor=self.cursor)
        if reviewer_late and not history:
            return dict(status='blocked', reason='history_required', cursor=self.cursor)
        try:
            if cursor is not None and self.cursor is not None and int(cursor) > int(self.cursor):
                return dict(status='blocked', reason='ahead_cursor', cursor=self.cursor)
        except ValueError:
            return dict(status='blocked', reason='invalid_cursor', cursor=self.cursor)
        for seat, key in self.inflight.items():
            if key not in self.completed:
                return dict(status='blocked', reason='uncertain_native_intent', seat=seat, cursor=self.cursor)
        for application_id, item in list(self.outbox.items()):
            if item.get('message_id'):
                continue
            matches = [message for message in history if message.get('application_id') == application_id
                       and message.get('thread') == item['thread'] and isinstance(message.get('message_id'), int)]
            if len(matches) != 1:
                return dict(status='blocked', reason='uncertain_send', cursor=self.cursor)
            self._append('sent', dict(id=application_id, message_id=matches[0]['message_id']))
        return dict(status='ready', reason=None, cursor=self.cursor,
                    pending=[dict(i) for i in self.pending if self._key(i) not in self.intents])

    def close(self):
        if not self.lock.closed:
            if self.locked:
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_UNLCK, 1)
            self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
