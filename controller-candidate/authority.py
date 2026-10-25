"""Single-writer deterministic task authority. Origin is supplied by a trusted local caller."""
import copy
import hashlib
import json
import msvcrt
import os
from pathlib import Path


class Refused(ValueError):
    pass


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def _hash(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def apply(state: dict, command: dict) -> dict:
    kind = command.get('kind')
    role = command.get('origin')
    actor = command.get('actor')
    task_id = command.get('task')
    epoch = command.get('epoch')
    key = command.get('action_id')
    if not key or not task_id or role not in ('operator', 'manager', 'executor'):
        raise Refused('untrusted or incomplete action')
    if role == 'manager' and actor != state['manager']:
        raise Refused('unbound manager')
    if role == 'operator' and actor != 'human':
        raise Refused('unbound operator')
    current = state['tasks'].get(task_id)
    if current and kind not in ('assign', 'pause', 'resume') and epoch != current['epoch']:
        raise Refused('stale epoch')
    digest = _hash(command)
    scoped_key = f'{task_id}/{epoch}/{key}'
    old = state['actions'].get(scoped_key)
    if old:
        if old != digest:
            raise Refused('conflicting action id')
        return copy.deepcopy(state)
    result = copy.deepcopy(state)
    task = result['tasks'].get(task_id)
    if kind == 'assign' and role == 'manager' and not task:
        if epoch != 1 or command.get('owner') not in ('E1', 'E2') or not command.get('revision'):
            raise Refused('invalid assignment')
        result['tasks'][task_id] = dict(owner=command['owner'], epoch=1,
                                         revision=command['revision'], ack=None, frozen=False,
                                         stopped=False, status='ready', excluded=[])
    elif kind == 'pause' and role == 'operator':
        result['paused'] = True
    elif kind == 'resume' and role == 'operator':
        if any(t['status'] == 'running' for t in result['tasks'].values()):
            raise Refused('running task requires reconciliation')
        result['paused'] = False
    else:
        if not task or epoch != task['epoch']:
            raise Refused('missing or stale assignment')
        if kind == 'ack' and role == 'executor' and actor == task['owner']:
            if task['frozen'] or command.get('revision') != task['revision']:
                raise Refused('stale acknowledgement')
            task['ack'] = [task_id, actor, epoch, task['revision']]
        elif kind == 'start' and role == 'executor' and actor == task['owner']:
            if result['paused'] or task['frozen'] or task['status'] != 'ready':
                raise Refused('dispatch frozen or already started')
            if task['ack'] != [task_id, actor, epoch, task['revision']] or command.get('revision') != task['revision']:
                raise Refused('exact assignment acknowledgement required')
            if result['spent']['starts'] >= result['limits']['starts']:
                raise Refused('cumulative budget exhausted')
            result['spent']['starts'] += 1
            task['status'] = 'running'
        elif kind == 'stop' and role == 'operator':
            task['frozen'] = True
            task['stopped'] = False
            task['status'] = 'interrupted'
        elif kind == 'verify_stop' and role == 'operator':
            if not task['frozen'] or command.get('active') != 0 or command.get('children_stopped') is not True or command.get('services_stopped') is not True:
                raise Refused('shutdown unverified')
            task['stopped'] = True
        elif kind == 'reject' and role == 'operator':
            if command.get('revision') != task['revision']:
                raise Refused('wrong rejected revision')
            task['frozen'] = True
            task['excluded'].append(task['owner'])
            result['withdrawals'].append(dict(task=task_id, revision=task['revision']))
        elif kind == 'transfer' and role == 'manager':
            new_owner = command.get('owner')
            if (not task['frozen'] or not task['stopped'] or new_owner not in ('E1', 'E2')
                    or new_owner == task['owner'] or new_owner in task['excluded']
                    or not command.get('revision')):
                raise Refused('transfer requires verified shutdown and eligible replacement')
            old_owner = task['owner']
            task.update(owner=new_owner, epoch=epoch + 1, revision=command['revision'],
                        ack=None, frozen=False, stopped=True, previous_owner=old_owner, status='ready')
        else:
            raise Refused('unauthorized transition')
    result['actions'][scoped_key] = digest
    return result


class Journal:
    """Single-process authority: caller handles must be bound outside untrusted Mail input.

    Object handles are a cooperative-process boundary, NOT cross-process authentication.
    No native/Mail code may receive a handle or invoke the pure replay reducer directly.
    """

    def __init__(self, root: Path, seed: dict, *, callers=None):
        self.root = Path(root)
        protected = Path(__file__).resolve().parent.parent / 'protected'
        if self.root.resolve() == protected or protected in self.root.resolve().parents:
            raise Refused('protected tree is read-only to this candidate')
        self.callers = callers or {}
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = (self.root / 'writer.lock').open('a+b')
        self.locked = False
        try:
            self.lock.seek(0)
            try:
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise Refused('second writer forbidden') from error
            self.locked = True
            self.path = self.root / 'journal.jsonl'
            self.state = copy.deepcopy(seed)
            self.events = []
            if self.path.exists():
                raw = self.path.read_bytes()
                if raw and not raw.endswith(b'\n'):
                    raise Refused('partial journal tail')
                for line in raw.splitlines():
                    row = json.loads(line)
                    check = row.pop('sha256', None)
                    if check != _hash(row) or row['sequence'] != len(self.events) + 1 or row['previous'] != (self.events[-1]['sha256'] if self.events else None):
                        raise Refused('journal continuity failed')
                    self.state = apply(self.state, row['command'])
                    row['sha256'] = check
                    self.events.append(row)
                self.state['paused'] = True
        except Exception:
            self.close()
            raise

    def execute(self, command: dict, *, caller=None) -> dict:
        if not any(caller is handle and (command.get('origin'), command.get('actor')) == role
                   for handle, role in self.callers.items()):
            raise Refused('unbound local caller')
        next_state = apply(self.state, command)
        if next_state == self.state:
            return copy.deepcopy(self.state)
        row = dict(sequence=len(self.events) + 1,
                   previous=self.events[-1]['sha256'] if self.events else None,
                   command=copy.deepcopy(command))
        row['sha256'] = _hash(row)
        with self.path.open('ab') as stream:
            stream.write(_bytes(row) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        self.events.append(row)
        self.state = next_state
        return copy.deepcopy(self.state)

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
