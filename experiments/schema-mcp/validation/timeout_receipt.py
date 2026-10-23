"""TEST ONLY: finite timeout evidence from one committed synthetic runner receipt."""
import hashlib
import json
from pathlib import Path
import re

def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate receipt field')
        value[key] = item
    return value

def _bounded(path):
    with path.open('rb') as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError('bounded synthetic receipt required')
    return raw

def timeout_observation(directory, expected_run):
    try:
        if type(expected_run) is not str or re.fullmatch('[0-9a-f]{32}', expected_run) is None:
            return {}
        directory = Path(directory)
        raw = _bounded(directory / 'completion.json')
        value = json.loads(raw, object_pairs_hook=_unique)
        commit = json.loads(_bounded(directory / 'completion.commit.json'), object_pairs_hook=_unique)
        if (type(value) is not dict or type(commit) is not dict
                or set(commit) != {'run_id', 'payload_sha256', 'completion_receipt_written'}
                or value.get('run_id') != commit['run_id'] or commit['run_id'] != expected_run
                or commit['completion_receipt_written'] is not True
                or commit['payload_sha256'] != hashlib.sha256(raw).hexdigest()):
            return {}
        # Unknown values never escape as public labels or proof.
        if (value.get('status') not in ('cleanup_unconfirmed', 'deadline_exceeded')
                or value.get('primary_status') not in (None, 'deadline_exceeded')
                or value.get('deadline_kind') not in ('stage', 'overall')
                or value.get('timeout_stage') != 'source_prepare'):
            return {}
        return {'same_configured_run': True, 'receipt_valid': True,
            **{name: value.get(name) for name in ('status', 'primary_status', 'deadline_kind', 'timeout_stage')}}
    except (OSError, ValueError, TypeError):
        return {}
