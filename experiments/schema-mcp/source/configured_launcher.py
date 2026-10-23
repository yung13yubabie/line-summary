"""Fixed local configured-schema launcher. No MCP registration or arbitrary argv."""
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

from configured_entry_gate import _local_file, _unique_fields, _validate_frame

WORK_SECONDS = 30
CLEANUP_SECONDS = 3
MAX_BYTES = 16 * 1024
RETAINED = {}
_REGISTRY_LOCK = threading.RLock()


def _backend():
    from configured_native_owner import Backend
    return Backend()


def _label(error):
    name = type(error).__name__
    return name if name in {'OSError', 'ValueError', 'TimeoutError', 'TimeoutExpired',
                           'KeyboardInterrupt', 'SystemExit', 'RuntimeError'} else 'UnexpectedError'


def _write_result(path, value):
    raw = json.dumps(value, separators=(',', ':')).encode()
    if len(raw) > MAX_BYTES:
        raise ValueError('bounded launcher receipt required')
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(raw)
    temporary.replace(path)


class _Owner:
    def __init__(self, directory, settings_path, cancel_event=None):
        self.directory = Path(directory)
        self.token = uuid.uuid4().hex
        self.started = time.monotonic()
        self.work_deadline = self.started + WORK_SECONDS
        self.cleanup_deadline = None
        self._cooperative_deadline = None
        self._settings = settings_path
        self.cancel_event = cancel_event
        self.backend = self.job = self.child = None
        self.assigned = self.start_sent = self.forced = False
        self.spawn_done = threading.Event()
        self.stop = threading.Event()
        self.cleanup_lock = threading.RLock()
        self.output = bytearray()
        self.output_bytes = 0
        self.errors = []
        self.status = 'starting'
        self.cleaned = False
        self.initial_result = None
        self.primary_interrupt = None
        # Ownership precedes every native acquisition and fallible persistence.
        with _REGISTRY_LOCK:
            RETAINED[self.token] = self

    def error(self, point, error):
        if len(self.errors) < 16:
            self.errors.append({'operation': point, 'type': _label(error)})
        if isinstance(error, (KeyboardInterrupt, SystemExit)) and self.primary_interrupt is None:
            self.primary_interrupt = error

    def _register_job(self, job):
        self.job = job

    def _register_child(self, child):
        self.child = child

    def _allowed(self):
        return (not self.stop.is_set() and not self._cancelled()
                and time.monotonic() < self.work_deadline)

    def _cancelled(self):
        event = getattr(self, 'cancel_event', None)
        return event is not None and event.is_set()

    def _spawn(self):
        try:
            if not self._allowed():
                return
            self.backend = _backend()
            self.job = self.backend.new_job(self._register_job)
            self.job.configure()
            if self.job.policy() != {'kill_on_close': True, 'active_process_limit': 8}:
                raise OSError('configured Job policy rejected')
            if not self._allowed():
                return
            inherited = self.job.inherit()
            argv = [sys._base_executable, '-I', '-S', '-B',
                    str(Path(__file__).with_name('configured_entry_gate.py')),
                    str(self.directory), str(Path(sys.prefix) / 'Lib' / 'site-packages')]
            self.child = self.backend.spawn(argv, (inherited,), self._register_child)
            self.job.close_inherited()
            if not self._allowed():
                return  # A late child stays suspended and receives no private start.
            if self.child.resumed:
                raise OSError('configured child was not suspended')
            self.job.assign(self.child)
            self.assigned = True
            if not self.job.member(self.child):
                raise OSError('exact configured membership rejected')
            if self.job.policy() != {'kill_on_close': True, 'active_process_limit': 8}:
                raise OSError('configured Job policy changed')
            if not self._allowed():
                return
            self.child.resume()
            if not self._allowed():
                return
            payload = (json.dumps({'op': 'start', 'settings_path': self._settings,
                'outer_run_id': self.token, 'deadline': self.work_deadline,
                'job_handle': inherited}, separators=(',', ':')) + '\n').encode()
            if len(payload) > 4096:
                raise ValueError('bounded configured launch required')
            # CPython Event.set uses this same condition. Serialize cancellation
            # with the private start commit, instead of a check/write race.
            condition = self.cancel_event._cond if self.cancel_event is not None else None
            if condition is not None and not condition.acquire(timeout=max(0, self.work_deadline-time.monotonic())):
                self.status = 'deadline_exceeded'
                return
            try:
                if not self._allowed():
                    return
                self.child.stdin.write(payload)
                self.child.stdin.flush()
                self.start_sent = True
                self.status = 'running'
            finally:
                if condition is not None:condition.release()
        except BaseException as error:
            self.status = 'spawn_error'
            self.error('spawn', error)
        finally:
            self._settings = None
            self.spawn_done.set()
            if self.stop.is_set() and self.cleanup_deadline is not None:
                self.cleanup(self.cleanup_deadline)

    def pump(self):
        if self.child is None or self.backend is None:
            return
        for label in ('stdout', 'stderr'):
            for _ in range(4):
                raw = self.backend.read_available(self.child, label, 4096)
                if not raw:
                    break
                self.output_bytes += len(raw)
                if self.output_bytes > MAX_BYTES:
                    self.status = 'output_limit'
                    self.stop.set()
                    return
                if label == 'stdout':
                    self.output.extend(raw)
                else:
                    self.status = 'output_rejected'
                    self.stop.set()

    def supervise(self):
        while True:
            self.pump()
            if self.stop.is_set():
                return
            if self._cancelled():
                self.status = 'cancelled'
                return
            if self.spawn_done.is_set():
                if self.child is None or self.status == 'spawn_error':
                    return
            if time.monotonic() >= self.work_deadline:
                self.status = 'deadline_exceeded'
                return
            if self.spawn_done.is_set():
                if self.child.poll() is not None:
                    self.pump()
                    # Polling or the final drain may cross the work deadline.
                    # An output rejection keeps its already recorded cause.
                    if not self.stop.is_set() and time.monotonic() >= self.work_deadline:
                        self.status = 'deadline_exceeded'
                    if self._cancelled():
                        self.status = 'cancelled'
                    return
            time.sleep(0.005)

    def _active(self):
        return self.job.active() if self.job is not None else 0

    def cleanup(self, deadline):
        self.stop.set()
        if not self.cleanup_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            return False
        try:
            if self.cleaned:
                return True
            if self._cooperative_deadline is None:
                # One cooperative window per owner, clipped by this cleanup's
                # absolute deadline. Exact retries cannot renew the allowance.
                self._cooperative_deadline = min(deadline, time.monotonic() + 1)
            terminated = False
            while True:
                child = self.child
                if child is not None:
                    try:
                        if child.stdin is not None and not child.stdin.closed:
                            child.stdin.close()  # EOF is the cooperative request.
                        self.pump()
                    except BaseException as error:
                        self.error('cleanup_io', error)
                try:
                    done = child is None or child._handle is None or child.poll() is not None
                    active = self._active()
                except BaseException as error:
                    self.error('cleanup_poll', error)
                    done, active = False, None
                if self.spawn_done.is_set() and done and active == 0:
                    break
                now = time.monotonic()
                cooperative = (self.assigned and self.start_sent
                               and now < self._cooperative_deadline)
                # An unstarted creation stays suspended and is cleaned at once.
                # Only started, assigned descendants may finish after EOF.
                if not terminated and not cooperative:
                    try:
                        if self.assigned and self.job is not None:
                            self.job.terminate()
                            terminated = True
                            self.forced = True
                        elif child is not None and child._handle is not None and not done:
                            child.kill()
                            terminated = True
                            self.forced = True
                    except BaseException as error:
                        self.error('terminate', error)
                if time.monotonic() >= deadline:
                    return False
                wait_until = min(deadline, self._cooperative_deadline) if cooperative else deadline
                time.sleep(min(0.005, max(0, wait_until - time.monotonic())))
            # Creation has ceased; these exact handles are no longer used by spawn.
            if child is not None:
                if child._handle is not None:
                    child.wait(timeout=max(0, deadline - time.monotonic()))
                for name in ('stdin', 'stdout', 'stderr'):
                    stream = getattr(child, name)
                    if stream is not None and not stream.closed:
                        stream.close()
                if not self.backend.close_startup(child):
                    return False
                for handle in (child._thread, child._handle):
                    if handle is not None and not handle.closed:
                        handle.Close()
            if self.job is not None:
                self.job.close()
            self.cleaned = True
            with _REGISTRY_LOCK:
                RETAINED.pop(self.token, None)
            return True
        except BaseException as error:
            self.error('cleanup', error)
            return False
        finally:
            self.cleanup_lock.release()

    def public(self):
        if self._cancelled() and self.status in {'starting', 'running'}:
            self.status = 'cancelled'
        result = {'outer_run_id': self.token, 'adapter': 'configured_local_launcher',
                  'status': self.status, 'successful': False, 'application_ack': False,
                  'os_release_confirmed': self.cleaned, 'retry_required': not self.cleaned,
                  'assigned_before_resume': self.assigned, 'start_sent': self.start_sent,
                  'forced_termination': self.forced, 'errors': list(self.errors)}
        if not self.cleaned:
            result.update(status='cleanup_unconfirmed', primary_status=self.status)
        elif self.forced and self.status == 'running':
            result.update(status='cleanup_unconfirmed', primary_status=self.status)
        try:
            value = json.loads(bytes(self.output), object_pairs_hook=_unique_fields)
            counts = _validate_frame(value, self.token)
            if self.start_sent and self.status == 'running':
                expected_exit = 0 if value['successful'] else 3
                if self.child.returncode != expected_exit:
                    raise ValueError('configured gate exit rejected')
                if self.forced:
                    # A validated inner outcome is provenance only after force.
                    # OS release cannot stand in for application acknowledgement.
                    result.update(status='cleanup_unconfirmed', primary_status=value['status'])
                elif self.cleaned:
                    result.update(status=value['status'], application_ack=value['application_ack'])
                    if value['successful']:
                        result.update(successful=True, counts=counts)
        except (ValueError, TypeError, AttributeError):
            if result['status'] == 'running':
                result['status'] = 'output_rejected'
        return result


def run(directory, settings_path, *, cancel_event=None):
    """Run the one configured schema aggregate under a newly created exact Job."""
    if cancel_event is not None and not isinstance(cancel_event, threading.Event):
        raise ValueError('local cancellation event required')
    if not _local_file(settings_path):
        raise ValueError('explicit local settings required')
    # Reserve frame metadata inside the same 4096-byte private input limit.
    if len(json.dumps({'settings_path': settings_path}).encode()) > 3800:
        raise ValueError('bounded configured settings required')
    if not _local_file(os.fspath(directory)):
        raise ValueError('explicit local output required')
    directory = Path(directory).resolve()
    if not _local_file(str(directory)):
        raise ValueError('explicit local output required')
    directory.mkdir(parents=True, exist_ok=False)
    owner = _Owner(directory, settings_path, cancel_event)
    thread = threading.Thread(target=owner._spawn, name='configured-owned-spawn', daemon=True)
    try:
        thread.start()
        owner.supervise()
    except BaseException as error:
        owner.error('supervise', error)
        owner.status = 'supervisor_error'
    finally:
        owner.stop.set()
        owner.cleanup_deadline = min(time.monotonic() + CLEANUP_SECONDS,
                                     owner.work_deadline + CLEANUP_SECONDS)
        owner.cleanup(owner.cleanup_deadline)
    result = owner.public()
    owner.initial_result = dict(result)
    try:
        _write_result(directory / 'launcher_completion.json', result)
    except BaseException as error:
        owner.error('receipt', error)
        result.update(successful=False, status='receipt_failed')
        result.pop('counts', None)
    if owner.primary_interrupt is not None:
        owner.primary_interrupt.launcher_owner = owner
        raise owner.primary_interrupt from None
    return result


def retry_cleanup(outer_run_id):
    """Recover only a retained exact owner; preserve its original result."""
    with _REGISTRY_LOCK:
        owner = RETAINED[outer_run_id]
    owner.cleanup(time.monotonic() + CLEANUP_SECONDS)
    recovery = {'outer_run_id': owner.token, 'os_release_confirmed': owner.cleaned,
                'retry_required': not owner.cleaned, 'errors': list(owner.errors)}
    _write_result(owner.directory / 'launcher_recovery.json', recovery)
    return recovery
