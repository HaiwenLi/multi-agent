"""Exact-revision multi-scope approval; never inferred from a Mail ACK."""
import copy
import hashlib
import json
from pathlib import Path


def manifest(directory: Path):
    root = Path(directory)
    entries = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or path.is_junction():
            raise ValueError('snapshot links forbidden')
        if path.is_file():
            data = path.read_bytes()
            entries.append(dict(path=path.relative_to(root).as_posix(), bytes=len(data),
                                sha256=hashlib.sha256(data).hexdigest()))
    raw = json.dumps(entries, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest(), entries


def evaluate(submission: dict, reviews: list[dict], checks: list[dict],
             policy: dict, state: dict) -> tuple[bool, list[str]]:
    reasons = []
    task = submission.get('task')
    revision = submission.get('revision')
    epoch = submission.get('epoch')
    manifest_digest = submission.get('manifest_digest')
    if not revision or not task or submission.get('current_epoch') != epoch or (
            policy.get('task'), policy.get('revision')) != (task, revision):
        reasons.append('stale task/revision/epoch policy')
    if not manifest_digest or policy.get('manifest_digest') != manifest_digest:
        reasons.append('immutable manifest digest mismatch')
    if task in state.get('withdrawn', ()) or any(x['revision'] == revision for x in state.get('rejected', ())):
        reasons.append('human rejected/withdrawn')
    if not submission.get('files'):
        reasons.append('snapshot file list missing')
    if any(dependency not in state.get('accepted', ()) for dependency in submission.get('dependencies', ())):
        reasons.append('dependency unaccepted')
    required = policy.get('required') or {}
    if not required or (policy.get('integration') and 'integration' not in required):
        reasons.append('required review coverage absent')
    for scope, reviewer in required.items():
        if reviewer == submission.get('author'):
            reasons.append('self review ' + scope)
        matching = [r for r in reviews if r.get('scope') == scope and r.get('reviewer') == reviewer]
        if len(matching) != 1 or any(r.get('revision') != revision or r.get('epoch') != epoch
                                      or r.get('manifest_digest') != manifest_digest
                                      or r.get('approve') is not True or r.get('blockers') != []
                                     or r.get('independently_reproduced') is not True for r in matching):
            reasons.append('missing/invalid scope approval ' + scope)
    if not policy.get('checks'):
        reasons.append('no mandatory checks')
    for name in policy.get('checks', []):
        matching = [c for c in checks if c.get('name') == name]
        if len(matching) != 1 or matching[0].get('revision') != revision or matching[0].get('epoch') != epoch or (
                matching[0].get('manifest_digest') != manifest_digest) or (
                matching[0].get('producer') not in set(required.values()) or matching[0].get('exit_code') != 0):
            reasons.append('missing/failed independent check ' + name)
    return not reasons, reasons


def reject(state: dict, revision: str, reason: str, *, task: str) -> dict:
    if not revision or not reason or task not in state.get('accepted', []):
        raise ValueError('rejection must identify a current accepted submission')
    if task in state.get('integrated', ()):
        raise ValueError('already integrated: needs separately authorized corrective plan')
    affected = {task}
    while True:
        extra = {name for name, dependencies in state.get('dependencies', {}).items()
                 if set(dependencies) & affected}
        if extra <= affected:
            break
        affected |= extra
    if affected & set(state.get('integrated', ())):
        raise ValueError('integrated dependent requires corrective plan')
    result = copy.deepcopy(state)
    result['rejected'].append(dict(task=task, revision=revision, reason=reason))
    result['withdrawn'] = sorted(set(result.get('withdrawn', [])) | affected)
    result['accepted'] = [name for name in result['accepted'] if name not in affected]
    return result
