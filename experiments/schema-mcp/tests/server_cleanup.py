"""Adopted test-client cleanup for already registered exact owned objects.

This module performs no work at import and imports no product/native module.
"""
import time


def close_server(self):
    if self.closed:
        return
    now = time.monotonic()
    # Preserve the existing outer client's 3.05-second cooperative EOF window.
    # The following one second proves only exact force/exit/Job-empty release;
    # it is neither new application work time nor the former 50-second wait.
    cooperative = self.__dict__.setdefault('_cooperative_deadline', now + 3.05)
    deadline = self.__dict__.setdefault('_cleanup_deadline', cooperative + 1)
    errors = []
    child, job = self.child, self.job
    drain_enabled = True

    def attempt(label, operation):
        try:
            return True, operation()
        except BaseException as error:
            errors.append((label, error))
            return False, None

    def drain():
        nonlocal drain_enabled
        if drain_enabled:
            drain_enabled = attempt('output_drain', self.drain)[0]

    def proof():
        if child is None or child._handle is None:
            done = True  # No CreateProcess handle was acquired.
        elif child.returncode is not None:
            done = True
        elif child._handle.closed:
            done = False  # An unobserved process cannot be inferred exited.
        else:
            ok, result = attempt('process_poll', child.poll)
            done = ok and result is not None
        ok, active = attempt('job_active', job.active) if job is not None else (True, 0)
        return done, ok and type(active) is int and active == 0

    if child is not None and child.stdin is not None and not child.stdin.closed:
        attempt('stdin_close', child.stdin.close)

    # An unassigned child has never been permitted to run; terminate its exact
    # handle immediately. A Job cannot terminate a child outside that Job.
    forced_once = False
    while True:
        drain()
        exited, empty = proof()
        if exited and empty:
            break
        now = time.monotonic()
        if not forced_once and (not self.assigned or now >= cooperative):
            forced_once = True
            self.force_attempted = True
            self.forced = True  # Even an interrupted native call is uncertain.
            if self.assigned and job is not None:
                attempt('terminate_exact_job', job.terminate)
            elif child is not None and child._handle is not None and not exited:
                attempt('terminate_exact_child', child.kill)
        if now >= deadline:
            break
        time.sleep(min(.005, max(0, deadline - time.monotonic())))

    drain()
    attempt('output_finish', self.frames.finish)
    exited, empty = proof()

    # A protocol error does not erase native ownership. Independently attempt
    # every release whose prerequisite is proved, even after another close fails.
    if child is not None:
        for label in ('stdin', 'stdout', 'stderr'):
            stream = getattr(child, label)
            if stream is not None and not stream.closed:
                attempt(label + '_close', stream.close)
        startup_ok, startup = attempt('startup_close', lambda: self.backend.close_startup(child))
        startup_ok = startup_ok and startup is True
        if exited and empty:
            for label in ('_thread', '_handle'):
                handle = getattr(child, label)
                if handle is not None and not handle.closed:
                    attempt(label + '_close', handle.Close)
        child_closed = (startup_ok
            and all(getattr(child, label) is None or getattr(child, label).closed
                    for label in ('stdin', 'stdout', 'stderr', '_thread', '_handle')))
    else:
        child_closed = True

    if job is not None and empty:
        attempt('job_close', job.close)
    job_closed = (job is None or (job.handle is None and job.inherited_handle is None))
    self.closed = exited and empty and child_closed and job_closed
    self.cleanup_errors = [{'operation': label, 'type': type(error).__name__}
                           for label, error in errors[:16]]
    if self.closed:
        # Use the Server class's original globals even when this implementation
        # is supplied as a separate function for review/adoption.
        registry = type(self).__init__.__globals__['RETAINED']
        if registry.get('fixed-server') is self:
            registry.pop('fixed-server')
    if errors:
        raise errors[0][1]
    if not self.closed:
        raise RuntimeError('Exact server cleanup incomplete')
