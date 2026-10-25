"""Composition root for the deterministic controller; it contains no live bootstrap."""
from acceptance import evaluate
from completion import persist_completion
from run_store import Blocked


class Controller:
    def __init__(self, store, operator, mail, native):
        self.store = store
        self.operator = operator
        self.mail = mail
        self.native = native

    @staticmethod
    def _key(item):
        return tuple(item[name] for name in ('run', 'task', 'epoch', 'action_id', 'seat'))

    def dispatch_one(self, seat: str) -> dict:
        snapshot = self.store.snapshot()
        if snapshot['paused']:
            raise Blocked('dispatch_paused')
        completed = {self._key(item) for item in snapshot['finishes']}
        pending = [item for item in snapshot['inbox']
                   if item.get('seat') == seat and self._key(item) not in completed]
        if not pending:
            raise Blocked('no_dispatchable_work')
        item = pending[0]
        assignment = snapshot['tasks'].get(item['task'])
        if (not assignment or assignment.get('seat') != seat
                or assignment.get('epoch') != item['epoch']
                or assignment.get('revision') != item.get('revision')
                or not assignment.get('acknowledged')):
            raise Blocked('assignment_not_acknowledged')
        intent = {name: item[name] for name in ('run', 'task', 'epoch', 'action_id', 'seat')}
        limits = self.store.seed.get('limits', {})
        max_starts = limits.get('starts')
        if max_starts is not None and snapshot['budget']['starts'] >= max_starts:
            raise Blocked('budget_exhausted')
        max_active = limits.get('active', 2)
        active = sum(1 for entry in snapshot['intents'] if entry.get('uncertain'))
        if active >= max_active:
            raise Blocked('concurrency_limit')
        self.store.append('intent', intent)
        result = self.native.invoke(seat, assignment.get('session'), item.get('body', ''),
                                    self.store.root / 'workspaces' / seat)
        if not result.get('final_text'):
            raise Blocked('missing_filtered_final')
        artifact = persist_completion(
            self.store.root / 'artifacts',
            f"{item['task']}-{item['epoch']}-{item['action_id']}", result['final_text'],
            {'seat': seat, 'session': assignment.get('session'), 'revision': item['revision'],
             'epoch': item['epoch']})
        result['artifact'] = artifact
        self.store.append('finish', dict(intent, artifact_digest=artifact['sha256']))
        self.native.complete(seat)
        return {'task': item['task'], 'seat': seat, 'epoch': item['epoch'], 'result': result}

    @staticmethod
    def _reconciliation_digests(history, liveness):
        if (not isinstance(history, list) or not isinstance(liveness, dict)
                or any(not isinstance(item, dict) for item in history)
                or any(not isinstance(item, dict) for item in liveness.values())):
            raise Blocked('invalid_reconciliation_inputs')
        import hashlib
        import json
        encode = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'),
                                          ensure_ascii=True).encode()
        return hashlib.sha256(encode(history)).hexdigest(), hashlib.sha256(encode(liveness)).hexdigest()

    def reconcile(self, history: list[dict], liveness: dict, *, capability) -> dict:
        history_digest, liveness_digest = self._reconciliation_digests(history, liveness)
        if any(item.get('active') != 0 for item in liveness.values()):
            raise Blocked('active_liveness_requires_operator_resolution')
        self.store.append('reconcile', {'history_sha256': history_digest,
                                        'liveness_sha256': liveness_digest,
                                        'history_count': len(history)}, capability=capability)
        return self.recover(history, liveness)

    def recover(self, history: list[dict], liveness: dict) -> dict:
        snapshot = self.store.snapshot()
        try:
            history_digest, liveness_digest = self._reconciliation_digests(history, liveness)
        except Blocked as error:
            return {'status': 'blocked', 'reason': str(error), 'pending': []}
        reconciliation = snapshot.get('reconciliation')
        if not reconciliation:
            return {'status': 'blocked', 'reason': 'durable_reconciliation_required', 'pending': []}
        if (reconciliation.get('history_sha256') != history_digest
                or reconciliation.get('liveness_sha256') != liveness_digest):
            return {'status': 'blocked', 'reason': 'reconciliation_inputs_changed', 'pending': []}
        if any(item.get('active') != 0 for item in liveness.values()):
            return {'status': 'blocked', 'reason': 'active_liveness_requires_operator_resolution', 'pending': []}
        if any(not stop.get('verified') for stop in snapshot.get('stops', {}).values()):
            return {'status': 'blocked', 'reason': 'partial_stop_reconciliation_required', 'pending': []}
        if any(item.get('uncertain') for item in snapshot['intents']):
            return {'status': 'blocked', 'reason': 'uncertain_native_intent',
                    'pending': [item for item in snapshot['intents'] if item.get('uncertain')]}
        return {'status': 'ready', 'reason': None, 'pending': []}

    def submit_and_review(self, submission, reviews, checks, policy) -> dict:
        snapshot = self.store.snapshot()
        acceptance_state = {
            'withdrawn': snapshot['withdrawn'], 'rejected': snapshot['rejected'],
            'accepted': snapshot['accepted'], 'dependencies': snapshot['dependencies'], 'integrated': [],
        }
        accepted, reasons = evaluate(submission, reviews, checks, policy, acceptance_state)
        self.store.append('review', {'task': submission.get('task'), 'revision': submission.get('revision'),
                                     'epoch': submission.get('epoch'),
                                     'manifest_digest': submission.get('manifest_digest'),
                                     'dependencies': submission.get('dependencies', []), 'accepted': accepted,
                                     'reasons': reasons})
        return {'accepted': accepted, 'reasons': reasons}
