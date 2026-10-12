"""Virtual clock and owned fake transport; never launches a process or Win32 API."""
import json
from types import SimpleNamespace

import readonly_client as client


class FakeTransport:
    def __init__(self, monkeypatch, responses, *, exit_after_cancel=0.02, spawn_delay=0,
                 write_size=4096, read_size=4096):
        self.now = 0.0
        self.responses = list(responses)
        self.exit_after_cancel = exit_after_cancel
        self.exit_at = float("inf")
        self.returncode = None
        self.pending = bytearray()
        self.input = bytearray()
        self.requests = []
        self.spawn_calls = []
        self.kills = 0
        self.signals = 0
        self.wait_failures = 0
        self.write_size, self.read_size = write_size, read_size
        self.stdin = SimpleNamespace(closed=False, close=lambda: self.close_stream(self.stdin))
        self.stdout = SimpleNamespace(closed=False, close=lambda: self.close_stream(self.stdout))
        self._handle = SimpleNamespace(closed=False, Close=lambda: self.close_stream(self._handle))
        self.event = SimpleNamespace(handle=713, signal=self.signal, wait_child=self.wait_child,
                                     close=self.close_event)

        def spawn(argv, **kwargs):
            self.spawn_calls.append((argv, kwargs))
            self.now += spawn_delay
            return self

        # Local namespaces avoid mutating shared stdlib modules or other tests.
        self.subprocess = SimpleNamespace(**vars(client.subprocess))
        self.subprocess.STARTUPINFO = lambda: SimpleNamespace()
        self.subprocess.Popen = spawn
        monkeypatch.setattr(client, "subprocess", self.subprocess)
        monkeypatch.setattr(client, "time", SimpleNamespace(monotonic=lambda: self.now, sleep=self.advance))
        monkeypatch.setattr(client, "_CancelEvent", lambda: self.event)
        monkeypatch.setattr(client, "_WindowsPipe", lambda process: self)

    @staticmethod
    def close_stream(stream):
        stream.closed = True

    def close_event(self):
        self.event.handle = None

    def advance(self, seconds):
        self.now += seconds

    def signal(self):
        self.signals += 1
        self.exit_at = min(self.exit_at, self.now + self.exit_after_cancel)

    def wait_child(self, process, milliseconds):
        assert process is self
        # Real scheduling progresses even for a zero-millisecond native poll.
        self.now += max(milliseconds / 1000, 0.0001)

    def poll(self):
        if self.returncode is None and self.now >= self.exit_at:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.kills += 1
        self.returncode = 1

    def wait(self, timeout):
        if self.wait_failures:
            self.wait_failures -= 1
            raise self.subprocess.TimeoutExpired("synthetic-private-sentinel", timeout)
        if self.poll() is None:
            self.now += min(timeout, max(0, self.exit_at - self.now))
        if self.poll() is None:
            raise self.subprocess.TimeoutExpired("synthetic-private-sentinel", timeout)
        return self.returncode

    def write(self, payload, offset):
        chunk = payload[offset:offset + self.write_size]
        self.input.extend(chunk)
        if self.input.endswith(b"\n"):
            self.requests.append(json.loads(self.input))
            self.input.clear()
            if self.responses:
                value = self.responses.pop(0)
                if isinstance(value, dict):
                    value = json.dumps(value).encode() + b"\n"
                self.pending.extend(value)
        return len(chunk)

    def read(self, limit):
        size = min(limit, self.read_size, len(self.pending))
        value = bytes(self.pending[:size])
        del self.pending[:size]
        return value

    def assert_released(self):
        assert self.poll() is not None
        assert self.stdin.closed and self.stdout.closed and self._handle.closed
        assert self.event.handle is None
