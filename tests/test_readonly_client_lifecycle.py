"""Adversarial local Python children only; no LINE data or real keys."""

import ctypes as C
from ctypes import wintypes as W
import subprocess
import sys
import threading
import time

import pytest

import readonly_client as client
from readonly_errors import SnapshotUnavailable


HANDSHAKE = (
    "import sys,json,time\n"
    "sys.stdin.buffer.readline()\n"
    "sys.stdout.buffer.write(b'{\"ok\":true,\"counts\":{}}\\n'); sys.stdout.buffer.flush()\n"
)


def launch(monkeypatch, code, seconds=0.8):
    original = subprocess.Popen
    children = []

    def spawn(argv, **kwargs):
        assert len(argv) == 4 and argv[1:3] == ["-I", "-B"]
        assert "env" not in kwargs
        process = original([sys.executable, "-I", "-B", "-c", code], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(client, "SESSION_SECONDS", seconds)
    # Leave a small bounded reap reserve in these short synthetic sessions.
    monkeypatch.setattr(client, "REAP_SECONDS", 0.2, raising=False)
    monkeypatch.setattr(client.subprocess, "Popen", spawn)
    return children


def assert_released(process):
    assert process.poll() is not None
    assert process.stdin.closed and process.stdout.closed
    assert process._handle.closed


def handles():
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = W.HANDLE
    kernel.GetProcessHandleCount.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
    kernel.GetProcessHandleCount.restype = W.BOOL
    result = W.DWORD()
    assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), C.byref(result))
    return result.value


def test_child_not_reading_stdin_times_out_before_it_wakes(monkeypatch):
    children = launch(monkeypatch, "import time; time.sleep(10)")
    start = time.monotonic()
    with pytest.raises(SnapshotUnavailable):
        client.WorkerConnection("synthetic-only.edb", "1" * 12000)
    assert time.monotonic() - start < 1.5
    assert_released(children[0])


@pytest.mark.parametrize("body", [
    "time.sleep(10)",
    "sys.stdout.buffer.write(b'x'*4096); sys.stdout.buffer.flush(); time.sleep(10)",
])
def test_absent_or_partial_stdout_times_out_and_reaps(monkeypatch, body):
    children = launch(monkeypatch, "import sys,time; sys.stdin.buffer.readline(); " + body)
    start = time.monotonic()
    with pytest.raises(SnapshotUnavailable):
        client.WorkerConnection("synthetic-only.edb", "1" * 32)
    assert time.monotonic() - start < 1.5
    assert_released(children[0])


def test_stdout_flood_is_bounded_and_reaped(monkeypatch):
    children = launch(monkeypatch,
        "import sys,time; sys.stdin.buffer.readline(); "
        "sys.stdout.buffer.write(b'x'*(2*1024*1024)); sys.stdout.buffer.flush(); time.sleep(10)",
        seconds=3)
    with pytest.raises(SnapshotUnavailable):
        client.WorkerConnection("synthetic-only.edb", "1" * 32)
    assert_released(children[0])


def test_many_short_stdout_lines_do_not_create_queue_or_reader_thread(monkeypatch):
    children = launch(monkeypatch, HANDSHAKE +
        "sys.stdin.buffer.readline()\n"
        "while True: sys.stdout.buffer.write(b'{\"ok\":true}\\n'*1000); sys.stdout.buffer.flush()\n",
        seconds=3)
    baseline = set(threading.enumerate())
    reader = client.WorkerConnection("synthetic-only.edb", "1" * 32)
    try:
        assert set(threading.enumerate()) == baseline
        assert not hasattr(reader, "_lines")
        with pytest.raises(SnapshotUnavailable):
            reader._request({"op": "synthetic"})
        assert_released(children[0])
    finally:
        reader.close()


def test_cancel_during_blocked_request_reaps_only_owned_child(monkeypatch):
    original = subprocess.Popen
    unrelated = original([sys.executable, "-I", "-B", "-c", "import time; time.sleep(10)"],
                         creationflags=subprocess.CREATE_NO_WINDOW)
    children = launch(monkeypatch, HANDSHAKE + "time.sleep(10)\n", seconds=3)
    reader = client.WorkerConnection("synthetic-only.edb", "1" * 32)
    result = []
    entered = threading.Event()

    def request():
        entered.set()
        try:
            reader._request({"op": "synthetic", "data": "x" * 12000})
        except BaseException as error:
            result.append(type(error))

    actor = threading.Thread(target=request)
    try:
        actor.start()
        assert entered.wait(1)
        start = time.monotonic()
        reader.cancel()
        actor.join(2)
        assert not actor.is_alive()
        assert time.monotonic() - start < 1
        assert result == [SnapshotUnavailable]
        assert_released(children[0])
        assert unrelated.poll() is None
    finally:
        try:
            reader.close()
        except SnapshotUnavailable:
            pass
        if actor.is_alive():
            children[0].kill()
            actor.join(2)
        unrelated.kill()
        unrelated.wait(timeout=2)
        unrelated._handle.Close()


def test_close_reply_followed_by_child_hang_uses_same_deadline(monkeypatch):
    children = launch(monkeypatch, HANDSHAKE +
        "sys.stdin.buffer.readline()\n"
        "sys.stdout.buffer.write(b'{\"ok\":true}\\n'); sys.stdout.buffer.flush()\n"
        "time.sleep(10)\n", seconds=3)
    reader = client.WorkerConnection("synthetic-only.edb", "1" * 32)
    reader._deadline = time.monotonic() + 0.5
    reader._io_deadline = reader._deadline - 0.2
    start = time.monotonic()
    with pytest.raises(SnapshotUnavailable, match="did not close cleanly"):
        reader.close()
    assert time.monotonic() - start < 1.5
    assert_released(children[0])
    reader.close()


def test_delayed_spawn_consumes_budget_and_never_sends_open(monkeypatch):
    children = launch(monkeypatch,
        "import sys; data=sys.stdin.buffer.read(); sys.exit(9 if data else 0)")
    spawn = client.subprocess.Popen

    def delayed(argv, **kwargs):
        process = spawn(argv, **kwargs)
        time.sleep(0.85)
        return process

    monkeypatch.setattr(client.subprocess, "Popen", delayed)

    def unexpected_pipe(_process):
        raise AssertionError("expired spawn must not configure or send an open request")

    monkeypatch.setattr(client, "_WindowsPipe", unexpected_pipe)
    with pytest.raises(SnapshotUnavailable):
        client.WorkerConnection("synthetic-only.edb", "1" * 32)
    assert_released(children[0])


def test_repeated_close_and_cancel_release_handles_without_gc(monkeypatch):
    children = launch(monkeypatch, HANDSHAKE +
        "sys.stdin.buffer.readline()\n"
        "sys.stdout.buffer.write(b'{\"ok\":true}\\n'); sys.stdout.buffer.flush()\n",
        seconds=3)
    baseline = handles()
    readers = []  # Keep Popen objects alive: GC must not be the cleanup mechanism.
    for index in range(12):
        reader = client.WorkerConnection("synthetic-only.edb", "1" * 32)
        readers.append(reader)
        if index % 2:
            reader.cancel()
        else:
            reader.close()
        reader.close()
        assert_released(children[-1])
    # CPython 3.11 retains one _waitpid_lock semaphore per live Popen object,
    # even on Windows. Exclude those inert stdlib locks, not process/pipe handles.
    assert handles() <= baseline + len(children)


@pytest.mark.parametrize("output", [
    b"not JSON private-sentinel", b'{"ok":"yes"}\n',
    b'{"ok":false,"code":"untrusted private-sentinel"}\n',
])
def test_untrusted_reply_errors_are_redacted_and_cleanup_is_immediate(monkeypatch, capsys, output):
    children = launch(monkeypatch,
        "import sys,time; sys.stdin.buffer.readline(); "
        f"sys.stdout.buffer.write({output!r}); sys.stdout.buffer.flush(); time.sleep(10)")
    with pytest.raises(SnapshotUnavailable) as caught:
        client.WorkerConnection("private-sentinel.edb", "1" * 32)
    visible = str(caught.value) + repr(capsys.readouterr())
    assert "private-sentinel" not in visible and "1" * 32 not in visible
    assert_released(children[0])


def test_keyboard_interrupt_cleans_before_propagating(monkeypatch):
    children = launch(monkeypatch, HANDSHAKE + "time.sleep(10)\n", seconds=3)
    reader = client.WorkerConnection("synthetic-only.edb", "1" * 32)
    original = client.time.sleep

    def interrupt(_seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(client.time, "sleep", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            reader._request({"op": "synthetic"})
        assert_released(children[0])
    finally:
        monkeypatch.setattr(client.time, "sleep", original)
        reader.close()


def test_cleanup_timeout_retains_owned_handle_and_reports_retry(monkeypatch):
    children = launch(monkeypatch, HANDSHAKE + "time.sleep(10)\n", seconds=3)
    reader = client.WorkerConnection("synthetic-only.edb", "1" * 32)
    process = children[0]
    original_wait = process.wait
    failed = False

    def fail_once(timeout):
        nonlocal failed
        if not failed:
            failed = True
            raise subprocess.TimeoutExpired("untrusted private-sentinel", timeout)
        return original_wait(timeout=timeout)

    monkeypatch.setattr(process, "wait", fail_once)
    with pytest.raises(SnapshotUnavailable, match="cleanup incomplete") as caught:
        reader.cancel()
    assert caught.value.worker is reader
    assert not reader._released and not process._handle.closed
    assert "private-sentinel" not in str(caught.value)
    reader.cancel()  # Retry uses the original owned handle, not a PID lookup.
    assert_released(process)


def test_spawn_exception_is_redacted_without_child(monkeypatch, capsys):
    def fail(_argv, **_kwargs):
        raise OSError("untrusted private-sentinel " + "1" * 32)

    monkeypatch.setattr(client.subprocess, "Popen", fail)
    with pytest.raises(SnapshotUnavailable) as caught:
        client.WorkerConnection("private-sentinel.edb", "1" * 32)
    visible = str(caught.value) + repr(capsys.readouterr())
    assert "private-sentinel" not in visible and "1" * 32 not in visible

