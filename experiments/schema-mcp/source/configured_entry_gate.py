"""Stdlib-only ingress. No product/provider import or settings read before start."""
import json
import math
import os
from pathlib import Path
import sys
import threading
import time

MAX_START = 4096
MAX_OUTPUT = 16 * 1024
FAILURE_STATUSES = frozenset({'failed', 'cancelled', 'deadline_exceeded', 'cleanup_unconfirmed',
    'output_or_receipt_rejected', 'spawn_error', 'protocol_error', 'supervisor_error',
    'spawn_cleanup_unconfirmed', 'receipt_failed', 'output_limit', 'output_rejected'})
PROOF_FIELDS = ('application_ack', 'os_release_confirmed', 'completion_receipt_valid')
FRAME_FIELDS = frozenset({'outer_run_id', 'status', 'successful', *PROOF_FIELDS})


def _local_file(value):
    # Shared stdlib-only lexical policy; no settings/provider/native work.
    from configured_child import explicit_local_file
    return explicit_local_file(value)


def _unique_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate configured launch field')
        value[key] = item
    return value


def _read_start(stream):
    raw = stream.readline(MAX_START + 1)
    if not raw or len(raw) > MAX_START or not raw.endswith(b'\n'):
        raise ValueError('bounded configured launch required')
    try:
        value = json.loads(raw, object_pairs_hook=_unique_fields)
    except (ValueError, UnicodeError):
        raise ValueError('configured launch rejected') from None
    required = {'op', 'settings_path', 'outer_run_id', 'deadline', 'job_handle'}
    if (not isinstance(value, dict) or set(value) != required or value['op'] != 'start'
            or not _local_file(value['settings_path'])
            or type(value['outer_run_id']) is not str or len(value['outer_run_id']) != 32
            or any(c not in '0123456789abcdef' for c in value['outer_run_id'])
            or type(value['job_handle']) is not int or value['job_handle'] <= 0
            or type(value['deadline']) not in (int, float) or not math.isfinite(value['deadline'])):
        raise ValueError('configured launch rejected')
    return value


def _verify_job(handle):
    # This import occurs only after the bounded private start was accepted.
    from configured_native_owner import verify_inherited_job
    verify_inherited_job(handle)


def _load_entry(site_packages):
    site = Path(site_packages)
    if not site.is_absolute() or site.name != 'site-packages' or site.parent.name != 'Lib':
        raise ValueError('configured runtime rejected')
    # The base -I -S ingress avoids the venv redirector before assignment. Once
    # trusted, every nested product worker uses the creator's approved venv.
    venv = site.parent.parent
    sys.executable = str(venv / 'Scripts' / 'python.exe')
    sys.prefix = str(venv)
    sys.path.append(str(site))
    import schema_entry
    return schema_entry


def _counts(value):
    if (not isinstance(value, dict) or set(value) != {'table_count', 'view_count'}
            or any(type(v) is not int or not 0 <= v < 2**63 for v in value.values())):
        raise ValueError('schema aggregate rejected')
    return dict(value)


def _validate_frame(value, outer_run_id):
    if (not isinstance(value, dict) or value.get('outer_run_id') != outer_run_id
            or type(value.get('status')) is not str
            or type(value.get('successful')) is not bool
            or any(type(value.get(key)) is not bool for key in PROOF_FIELDS)):
        raise ValueError('configured gate output rejected')
    if value['status'] == 'completed':
        if (set(value) != FRAME_FIELDS | {'counts'} or value['successful'] is not True
                or not all(value[key] for key in PROOF_FIELDS)):
            raise ValueError('configured gate success rejected')
        return _counts(value['counts'])
    if (set(value) != FRAME_FIELDS or value['status'] not in FAILURE_STATUSES
            or value['successful'] is not False):
        raise ValueError('configured gate failure rejected')
    return None


def _watch_control(stream, cancelled, finished):
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError):
        return  # In-memory streams used by pure contract tests have no OS pipe.
    def watch():
        try:
            os.read(descriptor, 1)  # Private pipe: any trailing input or EOF cancels.
        except OSError:
            pass
        if not finished.is_set():
            cancelled.set()
    # Raw os.read holds no buffered-stdin lock at interpreter shutdown. The
    # exact gate process owns this pipe; normal completion may exit with it idle.
    threading.Thread(target=watch, name='configured-private-control', daemon=True).start()


def _execute(stream, directory, site_packages):
    start = _read_start(stream)
    # Close the inherited query-only Job handle before any provider work.
    _verify_job(start['job_handle'])
    remaining = start['deadline'] - time.monotonic()
    if not 0 < remaining <= 30:
        raise ValueError('configured launch deadline rejected')
    entry = _load_entry(site_packages)
    context = {'outer_run_id': start['outer_run_id'], 'assigned_before_start': True,
               'active_process_limit': 8}
    cancelled, finished = threading.Event(), threading.Event()
    _watch_control(stream, cancelled, finished)
    try:
        result = entry.run_configured(Path(directory) / 'entry', start['settings_path'],
            overall=remaining, cleanup_budget=3, launch_context=context, cancel_event=cancelled)
    finally:
        finished.set()
    required = {'status', 'successful', *PROOF_FIELDS}
    known = required | {'counts', 'scope', 'adapter', 'run_id', 'runner', 'LINE_read'}
    if not isinstance(result, dict) or not required <= set(result) <= known:
        raise ValueError('configured inner result rejected')
    public = {'outer_run_id': start['outer_run_id'], **{key: result[key] for key in required}}
    if 'counts' in result:
        public['counts'] = result['counts']
    _validate_frame(public, start['outer_run_id'])
    return public


def main():
    # Isolated -S interpreter: all imports above are stdlib. Redirect before parsing
    # the private frame so provider prints and raw errors cannot reach public pipes.
    protocol = os.dup(1)
    try:
        with open(os.devnull, 'wb') as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        if len(sys.argv) != 3:
            return 3
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        result = _execute(sys.stdin.buffer, sys.argv[1], sys.argv[2])
        raw = json.dumps(result, separators=(',', ':')).encode() + b'\n'
        if len(raw) > MAX_OUTPUT:
            return 3
        os.write(protocol, raw)
        return 0 if result['successful'] else 3
    except BaseException:
        # Missing/invalid start or Job proof is never an application acknowledgement.
        return 3
    finally:
        os.close(protocol)


if __name__ == '__main__':
    raise SystemExit(main())
