"""The deterministic MVP's only durable authority and event log."""
import copy
import hashlib
import json
import msvcrt
import os
from pathlib import Path


class Blocked(RuntimeError):
    """A fail-closed condition that needs operator reconciliation."""


_KINDS = frozenset({
    'assignment', 'ack', 'inbox', 'intent', 'finish', 'outbox', 'send_receipt',
    'cursor', 'pause', 'stop', 'verify_stop', 'transfer', 'review', 'withdraw',
    'budget', 'reconcile',
})
_RESERVED = frozenset({'sequence', 'previous', 'kind', 'payload', 'sha256', 'seed_sha256'})
_PROTECTED = Path(__file__).resolve().parent.parent / 'protected'


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


class RunStore:
    """One fsynced append-only event stream for a fresh local run root."""

    def __init__(self, root: Path, seed: dict):
        self.root = Path(root)
        resolved = self.root.resolve()
        if resolved == _PROTECTED or _PROTECTED in resolved.parents:
            raise Blocked('protected_read_only')
        if not isinstance(seed, dict) or not seed.get('run'):
            raise Blocked('invalid_seed')
        self.seed = copy.deepcopy(seed)
        self.seed_sha256 = _digest(self.seed)
        # These opaque process-local objects are deliberately never serialized or
        # accepted from Mail.  They model the trusted local composition boundary.
        self.capabilities = {'manager': object(), 'operator': object()}
        self.capabilities.update({seat: object() for seat in self.seed.get('roles', {})
                                  if seat.startswith('E')})
        self.state_dir = self.root / 'state'
        self.events_path = self.state_dir / 'events.jsonl'
        self.lock = None
        self.locked = False
        self.events = []
        self.state = self._initial_state()
        self.paused = False
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.lock = (self.state_dir / 'writer.lock').open('a+b')
            self.lock.seek(0)
            try:
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise Blocked('second_writer') from error
            self.locked = True
            self._replay()
        except Exception:
            self.close()
            raise

    def _initial_state(self):
        return {
            'run': self.seed['run'], 'seed_sha256': self.seed_sha256, 'tasks': {},
            'inbox': [], 'intents': [], 'finishes': [], 'outbox': {}, 'receipts': {},
            'cursor': None, 'budget': {'starts': 0}, 'reviews': [], 'withdrawn': [],
            'accepted': {}, 'dependencies': {}, 'rejected': [], 'stops': {},
            'reconciliation': None, 'paused': False,
        }

    def _replay(self):
        if not self.events_path.exists():
            return
        raw = self.events_path.read_bytes()
        if raw and not raw.endswith(b'\n'):
            raise Blocked('partial_log')
        for line in raw.splitlines():
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise Blocked('corrupt_log') from error
            checksum = row.get('sha256')
            unsigned = {key: value for key, value in row.items() if key != 'sha256'}
            if (checksum != _digest(unsigned) or row.get('sequence') != len(self.events) + 1
                    or row.get('previous') != (self.events[-1]['sha256'] if self.events else None)
                    or row.get('seed_sha256') != self.seed_sha256):
                raise Blocked('journal_continuity')
            self._validate(row['kind'], row['payload'])
            self._project(row['kind'], row['payload'])
            self.events.append(row)
        self.paused = True
        self.state['paused'] = True

    def _validate(self, kind, payload):
        if kind not in _KINDS or not isinstance(payload, dict):
            raise Blocked('unexpected_event')
        if _RESERVED & set(payload):
            raise Blocked('reserved_integrity_field')

    def _require_capability(self, kind, payload, capability):
        if kind in ('assignment', 'transfer'):
            expected = self.capabilities['manager']
        elif kind in ('pause', 'stop', 'verify_stop', 'withdraw', 'reconcile'):
            expected = self.capabilities['operator']
        elif kind == 'ack':
            expected = self.capabilities.get(payload.get('seat'))
        else:
            return
        if capability is not expected:
            raise Blocked('missing_or_forged_capability')

    @staticmethod
    def _intent_key(payload):
        required = ('run', 'task', 'epoch', 'action_id', 'seat')
        if any(not payload.get(name) for name in required):
            raise Blocked('invalid_intent')
        return tuple(payload[name] for name in required[:-1])

    def _project(self, kind, payload):
        if kind == 'inbox':
            self.state['inbox'].append(copy.deepcopy(payload))
        elif kind == 'intent':
            key = self._intent_key(payload)
            if any(self._intent_key(item) == key for item in self.state['intents']):
                raise Blocked('duplicate_intent')
            item = copy.deepcopy(payload)
            item['uncertain'] = True
            self.state['intents'].append(item)
            self.state['budget']['starts'] += 1
        elif kind == 'finish':
            key = self._intent_key(payload)
            if not payload.get('artifact_digest') or not any(self._intent_key(item) == key for item in self.state['intents']):
                raise Blocked('finish_without_intent')
            self.state['finishes'].append(copy.deepcopy(payload))
            for item in self.state['intents']:
                if self._intent_key(item) == key:
                    item['uncertain'] = False
        elif kind == 'outbox':
            application_id = payload.get('application_id')
            if not application_id or not payload.get('thread') or not payload.get('body_sha256'):
                raise Blocked('invalid_outbox')
            old = self.state['outbox'].get(application_id)
            if old and old != payload:
                raise Blocked('conflicting_outbox')
            self.state['outbox'][application_id] = copy.deepcopy(payload)
        elif kind == 'send_receipt':
            application_id = payload.get('application_id')
            if application_id not in self.state['outbox'] or not payload.get('message_id'):
                raise Blocked('receipt_without_outbox')
            self.state['receipts'][application_id] = copy.deepcopy(payload)
        elif kind == 'cursor':
            if payload.get('value') is None:
                raise Blocked('invalid_cursor')
            self.state['cursor'] = payload['value']
        elif kind == 'pause':
            self.state['paused'] = True
        elif kind == 'stop':
            seat = payload.get('seat')
            if not seat:
                raise Blocked('invalid_stop')
            self.state['paused'] = True
            self.state['stops'][seat] = {'verified': False}
        elif kind == 'verify_stop':
            seat = payload.get('seat')
            stop = self.state['stops'].get(seat)
            if (not stop or payload.get('active') != 0 or payload.get('children_stopped') is not True
                    or payload.get('services_stopped') is not True):
                raise Blocked('unverified_stop')
            stop.update({'verified': True, 'evidence': copy.deepcopy(payload)})
        elif kind == 'withdraw':
            task = payload.get('task')
            if not task:
                raise Blocked('invalid_withdrawal')
            revision = payload.get('revision')
            accepted = self.state['accepted'].get(task)
            if accepted and revision != accepted['revision']:
                raise Blocked('wrong_rejected_revision')
            affected = {task}
            while True:
                dependents = {name for name, deps in self.state['dependencies'].items()
                              if set(deps) & affected}
                if dependents <= affected:
                    break
                affected |= dependents
            self.state['rejected'].append({'task': task, 'revision': revision,
                                           'reason': payload.get('reason', 'human_rejection')})
            self.state['withdrawn'] = sorted(set(self.state['withdrawn']) | affected)
            for name in affected:
                self.state['accepted'].pop(name, None)
        elif kind == 'review':
            self.state['reviews'].append(copy.deepcopy(payload))
            if payload.get('accepted'):
                task = payload.get('task')
                manifest_digest = payload.get('manifest_digest')
                revision = payload.get('revision')
                epoch = payload.get('epoch')
                if not task or not revision or not epoch or not manifest_digest:
                    raise Blocked('invalid_accepted_review')
                accepted = {'revision': revision, 'epoch': epoch, 'manifest_digest': manifest_digest}
                old = self.state['accepted'].get(task)
                if old and old != accepted:
                    raise Blocked('accepted_submission_immutable')
                self.state['accepted'][task] = accepted
                self.state['dependencies'][task] = list(payload.get('dependencies', []))
        elif kind == 'assignment':
            task = payload.get('task')
            if not task or task in self.state['tasks']:
                raise Blocked('invalid_assignment')
            if (payload.get('seat') not in self.capabilities or payload.get('epoch') != 1
                    or not payload.get('revision') or not payload.get('session')):
                raise Blocked('invalid_assignment')
            assignment = copy.deepcopy(payload)
            assignment['acknowledged'] = False
            self.state['tasks'][task] = assignment
        elif kind == 'ack':
            task = payload.get('task')
            assignment = self.state['tasks'].get(task)
            if (not assignment or assignment.get('seat') != payload.get('seat')
                    or assignment.get('epoch') != payload.get('epoch')
                    or assignment.get('revision') != payload.get('revision')):
                raise Blocked('invalid_acknowledgement')
            assignment['acknowledged'] = True
        elif kind == 'transfer':
            task = payload.get('task')
            prior = self.state['tasks'].get(task)
            prior_seat = prior.get('seat') if prior else None
            stopped = self.state['stops'].get(prior_seat, {})
            if (not prior or not stopped.get('verified') or payload.get('epoch') != prior.get('epoch', 0) + 1
                    or payload.get('seat') not in self.capabilities or payload.get('seat') == prior_seat
                    or not payload.get('revision') or not payload.get('session')):
                raise Blocked('transfer_without_task')
            assignment = copy.deepcopy(payload)
            assignment['acknowledged'] = False
            self.state['tasks'][task] = assignment
        elif kind == 'reconcile':
            if not payload.get('history_sha256') or not payload.get('liveness_sha256'):
                raise Blocked('invalid_reconciliation')
            self.state['reconciliation'] = copy.deepcopy(payload)

    def append(self, kind: str, payload: dict, *, capability=None) -> dict:
        self._validate(kind, payload)
        self._require_capability(kind, payload, capability)
        next_state = copy.deepcopy(self.state)
        old_state = self.state
        self.state = next_state
        try:
            self._project(kind, payload)
            row = {
                'sequence': len(self.events) + 1,
                'previous': self.events[-1]['sha256'] if self.events else None,
                'seed_sha256': self.seed_sha256,
                'kind': kind,
                'payload': copy.deepcopy(payload),
            }
            row['sha256'] = _digest(row)
            with self.events_path.open('ab') as stream:
                stream.write(_encoded(row) + b'\n')
                stream.flush()
                os.fsync(stream.fileno())
            self.events.append(row)
            return copy.deepcopy(row)
        except Exception:
            self.state = old_state
            raise

    def snapshot(self) -> dict:
        result = copy.deepcopy(self.state)
        result['paused'] = self.paused or result['paused']
        return result

    def close(self):
        if self.lock and not self.lock.closed:
            if self.locked:
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_UNLCK, 1)
            self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
