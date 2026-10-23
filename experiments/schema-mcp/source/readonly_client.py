"""Bounded private pipe to the source-only SQLite worker."""

import base64
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from readonly_errors import SnapshotUnavailable
from extraction_budget import current_budget, ExtractionStopped


MAX_REQUEST = 16 * 1024
MAX_REPLY = 1024 * 1024
SESSION_SECONDS = 25
REAP_SECONDS = 2
POLL_SECONDS = 0.01


class _CancelEvent:
    """An unnamed, owned event inherited only by this worker child."""
    def __init__(self):
        self._mutex = threading.Lock()
        import ctypes as C
        from ctypes import wintypes as W
        class Security(C.Structure):
            _fields_ = [("length", W.DWORD), ("descriptor", C.c_void_p), ("inherit", W.BOOL)]
        self._kernel = C.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateEventW.argtypes = [C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR]
        self._kernel.CreateEventW.restype = W.HANDLE
        self._kernel.SetEvent.argtypes = [W.HANDLE]
        self._kernel.SetEvent.restype = W.BOOL
        self._kernel.CloseHandle.argtypes = [W.HANDLE]
        self._kernel.CloseHandle.restype = W.BOOL
        self._kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
        self._kernel.WaitForSingleObject.restype = W.DWORD
        security = Security(C.sizeof(Security), None, True)
        self.handle = self._kernel.CreateEventW(C.byref(security), True, False, None)
        if not self.handle:
            raise OSError("private cancellation event unavailable")

    def signal(self):
        mutex = self._mutex
        if mutex is None:
            return
        with mutex:
            if self.handle and not self._kernel.SetEvent(self.handle):
                raise OSError("private cancellation signal failed")

    def wait_child(self, process, milliseconds):
        result = self._kernel.WaitForSingleObject(process._handle, milliseconds)
        if result not in (0, 258):
            raise OSError("private child wait failed")

    def close(self):
        mutex = self._mutex
        if mutex is None:
            return
        with mutex:
            if self.handle:
                if not self._kernel.CloseHandle(self.handle):
                    raise OSError("private cancellation event cleanup failed")
                self.handle = None
            self._mutex = None


class _WindowsPipe:
    """Poll the two owned anonymous pipes without I/O threads or a reply queue."""

    def __init__(self, process):
        import ctypes as C
        from ctypes import wintypes as W
        import msvcrt

        self._ctypes = C
        self._dword = W.DWORD
        self._kernel = C.WinDLL("kernel32", use_last_error=True)
        self._kernel.SetNamedPipeHandleState.argtypes = [
            W.HANDLE, C.POINTER(W.DWORD), C.c_void_p, C.c_void_p]
        self._kernel.SetNamedPipeHandleState.restype = W.BOOL
        for name in ("ReadFile", "WriteFile"):
            function = getattr(self._kernel, name)
            function.argtypes = [W.HANDLE, C.c_void_p, W.DWORD,
                                 C.POINTER(W.DWORD), C.c_void_p]
            function.restype = W.BOOL
        self._input = msvcrt.get_osfhandle(process.stdin.fileno())
        self._output = msvcrt.get_osfhandle(process.stdout.fileno())
        mode = W.DWORD(1)  # PIPE_NOWAIT | PIPE_READMODE_BYTE, on each parent end.
        for handle in (self._input, self._output):
            if not self._kernel.SetNamedPipeHandleState(handle, C.byref(mode), None, None):
                raise OSError("private pipe configuration failed")

    def write(self, payload, offset):
        C = self._ctypes
        chunk = payload[offset:offset + 4096]
        buffer = C.create_string_buffer(chunk)
        written = self._dword()
        if not self._kernel.WriteFile(self._input, buffer, len(chunk), C.byref(written), None):
            raise OSError("private pipe write failed")
        if written.value > len(chunk):
            raise ValueError("invalid pipe write count")
        return written.value  # A full nonblocking byte pipe can report zero.

    def read(self, limit):
        C = self._ctypes
        buffer = C.create_string_buffer(min(4096, limit))
        received = self._dword()
        if not self._kernel.ReadFile(self._output, buffer, len(buffer), C.byref(received), None):
            if C.get_last_error() == 232:  # ERROR_NO_DATA: still connected, no bytes yet.
                return b""
            raise OSError("private pipe read failed")
        return buffer.raw[:received.value]


class WrongKeyError(Exception):
    def __str__(self):
        return "not a database"


class WorkerQueryError(Exception):
    def __str__(self):
        return "read query failed"


def _error_type(error):
    name = type(error).__name__
    return name if name in {"OSError", "TimeoutExpired", "SnapshotUnavailable", "WrongKeyError",
        "ExtractionStopped", "KeyboardInterrupt", "SystemExit", "ValueError", "RuntimeError"} else "UnexpectedError"


def _owned_error(error, worker, primary=None, cleanup=None):
    outgoing = error if isinstance(error, (SnapshotUnavailable, WrongKeyError, ExtractionStopped, KeyboardInterrupt, SystemExit)) else SnapshotUnavailable("source read unavailable")
    outgoing.worker = worker
    outgoing.primary_error_type = primary or _error_type(error)
    outgoing.cleanup_error_type = cleanup
    return outgoing


def _encode(value):
    if isinstance(value, bytes):
        return {"__binary__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("unsupported query parameter")


def _decode(value):
    if isinstance(value, dict) and set(value) == {"__binary__"}:
        return base64.b64decode(value["__binary__"], validate=True)
    if isinstance(value, dict):
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


class WorkerCursor:
    def __init__(self, connection, identifier):
        self.connection = connection
        self.identifier = identifier
        self.rows = []
        self.done = False

    def __iter__(self):
        return self

    def __next__(self):
        while not self.rows:
            if self.done:
                raise StopIteration
            reply = self.connection._request({"op": "fetch", "cursor": self.identifier})
            self.rows = [_decode(row) for row in reply["rows"]]
            self.done = reply["done"]
        return self.rows.pop(0)


class WorkerConnection:
    def __init__(self, path, key):
        self._extraction_budget = current_budget()
        if self._extraction_budget is not None:
            self._extraction_budget.check('reader', reserve=REAP_SECONDS)
        self._deadline = time.monotonic() + SESSION_SECONDS
        if self._extraction_budget is not None:
            self._deadline = min(self._deadline, self._extraction_budget.deadline)
        # Reserve bounded termination/reaping time inside the session budget.
        self._io_deadline = self._deadline - REAP_SECONDS
        self._closed = False
        self._released = False
        self._cancelled = False
        self._lock = threading.RLock()
        self._process = None
        self._cancel_event = None
        self._owns_source = False
        self._source_release_confirmed = True
        script = Path(__file__).resolve().with_name("readonly_worker.py")
        try:
            self._cancel_event = _CancelEvent()
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {"handle_list": [self._cancel_event.handle]}
            self._process = subprocess.Popen(
                [sys.executable, "-I", "-B", str(script)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, close_fds=True, bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                startupinfo=startup,
            )
            self._check_budget()  # Spawn consumes the same budget, before sending secrets.
            self._pipe = _WindowsPipe(self._process)
            self.diagnostics = self._request({"op": "open", "path": path, "key": key,
                "cancel_event": self._cancel_event.handle})["counts"]
            self._owns_source = self.diagnostics.get("read_opens", 0) >= 3
        except BaseException as error:
            self._dispose_after_error(error)

    def _dispose_after_error(self, error):
        # Preserve a primary interruption even if cleanup itself fails. A nested
        # exchange/constructor boundary must keep the original finite labels.
        primary = getattr(error, 'primary_error_type', None) or _error_type(error)
        cleanup = getattr(error, 'cleanup_error_type', None)
        try:
            self._dispose()
        except BaseException as cleanup_error:
            cleanup = getattr(cleanup_error, 'cleanup_error_type', None) or _error_type(cleanup_error)
            if (not isinstance(error, (KeyboardInterrupt, SystemExit))
                    and (isinstance(cleanup_error, (KeyboardInterrupt, SystemExit))
                         or not isinstance(error, ExtractionStopped))):
                error = cleanup_error
        raise _owned_error(error, self, primary, cleanup) from None

    def _check_budget(self):
        if getattr(self, '_extraction_budget', None) is not None:
            self._extraction_budget.check('reader', reserve=REAP_SECONDS)
        if (self._cancelled
                or time.monotonic() >= min(self._io_deadline, self._deadline,
                    getattr(self, '_cleanup_deadline', float('inf')))):
            raise SnapshotUnavailable("source read unavailable")

    def _cleanup_limit(self):
        now = time.monotonic()
        limit = min(now + REAP_SECONDS, self._deadline) if self._deadline > now else now + REAP_SECONDS
        # This pinned CPython runtime atomically installs one immutable deadline.
        # Concurrent cancel/close calls and later retries cannot renew the lease.
        return self.__dict__.setdefault('_cleanup_deadline', limit)

    def _request(self, value):
        # A single owner of each pipe also prevents concurrent reply mix-ups.
        lock = self._lock
        if lock is None:
            raise SnapshotUnavailable("read worker is closed")
        if not lock.acquire(timeout=max(0, min(self._deadline,
                getattr(self, '_cleanup_deadline', float('inf'))) - time.monotonic())):
            self._cancelled = True
            raise SnapshotUnavailable("source read unavailable")
        try:
            return self._exchange(value)
        finally:
            lock.release()

    def _exchange(self, value):
        if self._closed or self._process is None:
            raise SnapshotUnavailable("read worker is closed")
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(payload) > MAX_REQUEST:
            raise SnapshotUnavailable("read request limit exceeded")
        try:
            sent = 0
            line = bytearray()
            while True:
                self._check_budget()
                progress = 0
                if sent < len(payload):
                    progress = self._pipe.write(payload, sent)
                    sent += progress
                    if sent == len(payload) and value.get("op") == "open":
                        self._owns_source = True  # Until the initial reply proves no native source opened.
                chunk = self._pipe.read(MAX_REPLY + 1 - len(line))
                line.extend(chunk)
                if len(line) > MAX_REPLY:
                    raise ValueError("invalid worker reply")
                if b"\n" in line:
                    if sent != len(payload) or not line.endswith(b"\n") or line.count(b"\n") != 1:
                        raise ValueError("invalid worker reply")
                    self._check_budget()
                    break
                if self._process.poll() is not None and not chunk:
                    raise ValueError("worker exited without reply")
                if not progress and not chunk:
                    time.sleep(min(POLL_SECONDS, max(0, min(self._io_deadline, self._deadline)
                                                    - time.monotonic())))
            response = json.loads(line)
            if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
                raise ValueError("invalid worker reply")
            if not response["ok"]:
                code = response.get("code")
                if code == "wrong_key":
                    raise WrongKeyError()
                if code == "query_failed":
                    raise WorkerQueryError()
                if code == "source_shm_unavailable":
                    raise SnapshotUnavailable("source SHM is missing or unreadable")
                raise SnapshotUnavailable("source read unavailable")
            return response
        except (WrongKeyError, WorkerQueryError):
            raise
        except BaseException as error:
            self._dispose_after_error(error)

    def execute(self, sql, parameters=()):
        response = self._request({"op": "query", "sql": sql, "parameters": _encode(parameters)})
        return WorkerCursor(self, response["cursor"])

    def close(self):
        lock = self._lock
        if lock is not None:
            if not lock.acquire(timeout=max(0, self._cleanup_limit() - time.monotonic())):
                self._source_release_confirmed = False
                raise _owned_error(SnapshotUnavailable("read worker cleanup incomplete"), self)
            try:
                self._close()
            finally:
                lock.release()
        if not self._source_release_confirmed:
            error = SnapshotUnavailable("read worker cleanup incomplete")
            error.worker = self
            raise error

    def _close(self):
        if self._released:
            return
        if self._process is None:
            self._dispose()  # An event can be owned even when Popen never returned.
            return
        try:
            if self._process.poll() is None and not self._cancelled:
                self._request({"op": "close"})
                self._process.wait(timeout=max(0, min(self._io_deadline, self._deadline,
                                                     self._cleanup_limit())
                                               - time.monotonic()))
        except BaseException as error:
            self._dispose_after_error(error)
        abnormal = self._dispose()
        if abnormal:
            raise _owned_error(SnapshotUnavailable("read worker did not close cleanly"), self)

    def cancel(self):
        self._cancelled = True  # Wake an active exchange at its next bounded poll.
        if self._released:
            if not self._source_release_confirmed:
                error = SnapshotUnavailable("read worker cleanup incomplete")
                error.worker = self
                raise error
            return
        cleanup_deadline = self._cleanup_limit()
        # Signal the actual owned event before waiting for a query's lock.
        if self._cancel_event is not None:
            try:
                self._cancel_event.signal()
            except OSError:
                if self._released and self._source_release_confirmed:
                    return
                self._source_release_confirmed = False
                error = SnapshotUnavailable("read worker cleanup incomplete")
                error.worker = self
                raise error from None
        lock = self._lock
        if lock is not None:
            if not lock.acquire(timeout=max(0, cleanup_deadline - time.monotonic())):
                self._source_release_confirmed = False
                error = SnapshotUnavailable("read worker cleanup incomplete")
                error.worker = self
                raise error
            try:
                self._dispose()
            finally:
                lock.release()
        if not self._source_release_confirmed:
            error = SnapshotUnavailable("read worker cleanup incomplete")
            error.worker = self
            raise error

    def _dispose(self):
        if self._released:
            return False
        cleanup_deadline = self._cleanup_limit()
        self._closed = True
        process = self._process
        if process is None:
            try:
                if self._cancel_event is not None:
                    self._cancel_event.close()
            except BaseException as error:
                raise _owned_error(error, self, cleanup=_error_type(error)) from None
            self._released = True
            self._lock = None
            return False
        abnormal = False
        forced = False
        try:
            if process.poll() is None and not hasattr(self, "_pipe"):
                # A late spawn received no protocol input. Preserve that boundary:
                # terminate before even delivering EOF to the unconfigured child.
                process.kill()
                forced = True
                abnormal = True
            if process.poll() is None and not forced:
                # Cooperatively interrupt SQLite and give its explicit finally
                # cleanup a bounded chance before any forced termination.
                self._cancel_event.signal()
                process.stdin.close()  # EOF also wakes an idle read_request().
                remaining = max(0, cleanup_deadline - time.monotonic())
                grace = min(REAP_SECONDS / 2, remaining / 2)
                until = time.monotonic() + max(0, grace)
                while process.poll() is None and time.monotonic() < until:
                    if hasattr(self, "_pipe"):
                        try:
                            self._pipe.read(4096)  # Discard bounded output so child cannot block on stdout.
                        except OSError:
                            pass  # EOF is expected during explicit cleanup.
                    self._cancel_event.wait_child(process, max(0,
                        min(10, int((until - time.monotonic()) * 1000))))
                if process.poll() is None:
                    process.kill()  # This Popen's owned handle, never PID search.
                    forced = True
                    abnormal = True
            # Cooperation, lock waiting and reaping spend one absolute reserve,
            # including an expired session or a late CreateProcess return.
            process.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
            abnormal = abnormal or process.returncode != 0
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()  # Raw bufsize=0 pipes: no blocking flush on close.
            process._handle.Close()  # Release now, even if callers retain the Popen object.
            self._cancel_event.close()
        except (OSError, subprocess.TimeoutExpired):
            error = SnapshotUnavailable("read worker cleanup incomplete")
            error.worker = self  # Keep ownership for a retry; do not hide a live child.
            raise error from None
        self._source_release_confirmed = not (self._owns_source and (forced or process.returncode != 0))
        self._released = True
        self._lock = None
        if not self._source_release_confirmed:
            error = SnapshotUnavailable("read worker cleanup incomplete")
            error.worker = self
            raise error
        return abnormal

