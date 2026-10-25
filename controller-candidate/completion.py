"""Filtered evidence before Mail; no native/raw output is accepted by this module."""
import hashlib
import json
import os
from pathlib import Path
import re


def persist_completion(root: Path, key: str, filtered_text: str, metadata: dict) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', key):
        raise ValueError('unsafe artifact key')
    if not isinstance(filtered_text, str) or re.search(r'(?i)authorization\s*:\s*bearer\s+\S+', filtered_text):
        raise ValueError('completion is not allowlisted/filtered')
    if set(metadata) - {'seat', 'session', 'revision', 'epoch'}:
        raise ValueError('unapproved evidence metadata')
    root = Path(root)
    protected = Path(__file__).resolve().parent.parent / 'protected'
    if root.resolve() == protected or protected in root.resolve().parents:
        raise ValueError('protected evidence is read-only to this candidate')
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or root.is_junction():
        raise ValueError('linked artifact root forbidden')
    data = filtered_text.encode('utf-8')
    name = key + '.txt'
    path = root / name
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return {'path': name, 'sha256': hashlib.sha256(data).hexdigest(),
            'text': filtered_text, 'metadata': dict(metadata)}


def notification(completion: dict, fields: dict, max_bytes: int = 8192) -> bytes:
    required = ('run', 'task', 'epoch', 'revision', 'ack', 'sender', 'recipient')
    if any(k not in fields for k in required) or set(fields) != set(required):
        raise ValueError('missing or unapproved nontruncatable fields')
    artifact = {k: completion[k] for k in ('path', 'sha256')}
    text = completion['text']

    def render(prefix, clipped):
        return json.dumps(dict(fields, artifact=artifact, text=prefix, truncated=clipped),
                          ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    if len(render('', True)) > max_bytes:
        raise ValueError('required identity/artifact fields exceed bound')
    full = render(text, False)
    if len(full) <= max_bytes:
        return full
    marker = '… [truncated; full artifact above]'
    if len(render(marker, True)) > max_bytes:
        raise ValueError('cannot fit truncation marker')
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(render(text[:middle] + marker, True)) <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return render(text[:low] + marker, True)
