"""Foreground-only local operator control; never parse Mail or agent text here."""
import json
import os
from pathlib import Path

from run_store import Blocked


class Operator:
    def __init__(self, store, supervisor):
        self.store = store
        self.supervisor = supervisor
        self.capability = store.capabilities['operator']

    def _write_status(self):
        snapshot = self.store.snapshot()
        status = {
            'facts': {'paused': snapshot['paused'], 'cursor': snapshot['cursor'],
                      'budget': snapshot['budget'], 'withdrawn': snapshot['withdrawn']},
            'unknowns': {'same_user_hostile_isolation': True,
                         'live_native_permissions': True},
            'proposals': {},
        }
        path = Path(self.store.root) / 'state' / 'status.json'
        temporary = path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8', newline='') as stream:
            json.dump(status, stream, sort_keys=True, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return status

    def command(self, line: str) -> dict:
        words = line.split()
        if not words:
            raise Blocked('unknown_command')
        action = words[0]
        tasks = self.store.snapshot()['tasks']
        if action == 'status' and len(words) == 1:
            return self._write_status()
        if action == 'pause' and len(words) == 1:
            self.store.append('pause', {'source': 'operator', 'reason': 'operator_pause'},
                              capability=self.capability)
            return self._write_status()
        if action == 'stop' and len(words) == 2:
            seat = words[1]
            if seat not in self.store.seed.get('roles', {}):
                raise Blocked('unknown_seat')
            self.store.append('pause', {'source': 'operator', 'reason': 'operator_stop'},
                              capability=self.capability)
            self.store.append('stop', {'source': 'operator', 'seat': seat}, capability=self.capability)
            evidence = self.supervisor.stop(seat)
            if evidence.get('verified'):
                self.store.append('verify_stop', {'source': 'operator', 'seat': seat,
                                                   'active': evidence.get('active'),
                                                   'children_stopped': evidence.get('children_stopped'),
                                                   'services_stopped': evidence.get('services_stopped')},
                                  capability=self.capability)
            self._write_status()
            return dict(evidence)
        if action == 'reject' and len(words) == 3:
            task, revision = words[1:]
            assignment = tasks.get(task)
            accepted = self.store.snapshot()['accepted'].get(task)
            if ((not assignment or assignment.get('revision') != revision)
                    and (not accepted or accepted.get('revision') != revision)):
                raise Blocked('unknown_task_or_revision')
            self.store.append('withdraw', {'source': 'operator', 'task': task, 'revision': revision},
                              capability=self.capability)
            return self._write_status()
        if action == 'reassign' and len(words) == 3:
            task, seat = words[1:]
            if task not in tasks or seat not in self.store.seed.get('roles', {}) or seat == 'M':
                raise Blocked('invalid_reassignment')
            self.store.append('pause', {'source': 'operator', 'reason': 'reassign_requested'},
                              capability=self.capability)
            return {'status': 'pending_manager_transfer', 'task': task, 'seat': seat}
        if action == 'resume' and len(words) == 1:
            raise Blocked('durable_reconciliation_required')
        raise Blocked('unknown_command')
