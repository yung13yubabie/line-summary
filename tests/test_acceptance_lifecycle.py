"""Bounded synthetic pipe lifecycle acceptance, including unsupported boundaries.

No global quota service or real host is exercised. A passing NOT_SUPPORTED test
records the *absence* of cross-process enforcement, never a capability claim.
The original core case is intentionally NOT_RUN in an isolated mock bundle.
"""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest
from mcp.types import CallToolResult, JSONRPCMessage, LATEST_PROTOCOL_VERSION, Tool

ROOT = Path(__file__).resolve().parents[1]
# Load the existing guarded child bootstrap without importing tests.conftest.
_spec = importlib.util.spec_from_file_location("lifecycle_protocol_helpers", Path(__file__).with_name("test_plugin_protocol.py"))
_wire = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_wire)
HISTORY = {**_wire.CALLS["line_get_history"], "limit": 1}


@pytest.fixture(autouse=True)
def isolate_offline_server_settings():
    """Keep the legacy autofixture out of these separately guarded children."""


# Extend the existing network/import/reader tripwires only with cleanup evidence.
_LIFECYCLE_OBSERVATION = r'''
if os.environ.get("PLUGIN_TEST_SLOW_READ_DIR"):
    observed_read = plugin_adapter.PluginService._read
    def lifecycle_read(self, name, arguments):
        try:
            return observed_read(self, name, arguments)
        finally:
            (marker_dir / "finished").touch()
    plugin_adapter.PluginService._read = lifecycle_read
    observed_close = plugin_adapter.SyntheticSource.close
    def lifecycle_close(self):
        if not (marker_dir / "finished").exists():
            (marker_dir / "closed_before_read_finished").touch()
        observed_close(self)
        (marker_dir / "cleaned").touch()
    plugin_adapter.SyntheticSource.close = lifecycle_close
'''
LIFECYCLE_BOOTSTRAP = _wire.BOOTSTRAP.replace(
    "try:\n    raise SystemExit(plugin_adapter.main())",
    _LIFECYCLE_OBSERVATION + "\ntry:\n    raise SystemExit(plugin_adapter.main())",
)


class PipePeer:
    """A tiny bounded real-pipe JSON-RPC peer, with complete observed-byte checks."""

    def __init__(self, directory, *, settings=None, markers=None, core=False):
        directory.mkdir()
        self.directory, self.markers, self.core = directory, markers, core
        self.raw, self.next_id = bytearray(), 0
        self.stopped = False
        self.stderr_path = directory / "stderr.txt"
        config = directory / "PRIVATE_SENTINEL_CONFIG.json"
        if settings is not None:
            config.write_text(json.dumps(settings), encoding="utf-8")
        self.child_tmp = directory / "child-tmp"
        self.child_tmp.mkdir()
        env = {**os.environ, "TMPDIR": str(self.child_tmp),
               "PLUGIN_TEST_SOURCE_ERROR": "0", "PLUGIN_TEST_LIFECYCLE_ERROR": "",
               "PLUGIN_TEST_PRIVACY_CLOCK": "",
               "PLUGIN_TEST_SLOW_READ_DIR": str(markers) if markers else ""}
        if core:
            # The all-SQLite tripwire must remain active. pytest-cov's child
            # collector writes its own SQLite file at atexit, so do not inject
            # that unrelated instrumentation into this no-database process.
            # Other core tests still measure runtime coverage at the same floor.
            env = {key: value for key, value in env.items() if not key.startswith("COV_CORE_")}
            env.pop("COVERAGE_PROCESS_START", None)
        bootstrap = CORE_DISABLED_BOOTSTRAP if core else LIFECYCLE_BOOTSTRAP
        args = [sys.executable, "-c", bootstrap]
        args += [str(config)] if core else ["--settings", str(config)]
        with self.stderr_path.open("wb") as stderr:
            self.process = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=stderr, bufsize=0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()

    def send(self, method, params=None, *, notification=False):
        frame = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        if not notification:
            self.next_id += 1
            frame["id"] = self.next_id
        self.process.stdin.write((json.dumps(frame) + "\n").encode())
        self.process.stdin.flush()
        return frame.get("id")

    def receive(self, request_id):
        # Windows select() only supports sockets, not subprocess pipes. Keep
        # this bounded reader portable without adding a host or network path.
        responses = queue.Queue(maxsize=1)
        def read_response():
            try:
                while True:
                    line = self.process.stdout.readline()
                    if not line:
                        raise EOFError("Synthetic stdio ended before the expected response")
                    self.raw.extend(line)
                    JSONRPCMessage.model_validate_json(line)
                    frame = json.loads(line)
                    if frame.get("id") == request_id:
                        responses.put(frame)
                        return
            except BaseException as exc:
                responses.put(exc)
        reader = threading.Thread(target=read_response, daemon=True)
        reader.start()
        try:
            response = responses.get(timeout=10)
        except queue.Empty:
            pytest.fail("Synthetic stdio response timed out")
        reader.join(timeout=1)
        assert not reader.is_alive(), "Synthetic response reader failed to finish"
        if isinstance(response, BaseException):
            raise response
        return response

    def request(self, method, params=None):
        return self.receive(self.send(method, params))

    def initialize(self):
        frame = self.request("initialize", {
            "protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "offline-lifecycle-acceptance", "version": "1"},
        })
        assert "serverInfo" in frame["result"]
        self.send("notifications/initialized", notification=True)
        listed = self.request("tools/list")["result"]["tools"]
        self.tools = {tool["name"]: Tool.model_validate(tool) for tool in listed}

    def call(self, name, arguments):
        frame = self.request("tools/call", {"name": name, "arguments": arguments})
        return _wire._assert_envelope(CallToolResult.model_validate(frame["result"]), self.tools[name], "mock")

    def chosen_history(self, destination):
        request = {**HISTORY, "destination": destination}
        preview = self.call("line_get_history", request)
        assert preview["status"] == "requires_confirmation" and preview["items"] == []
        return {**request, "privacy_choice": "original", "mock_scope_id": preview["mock_scope_id"]}

    def stop(self):
        if self.stopped:
            return
        if not self.process.stdin.closed:
            self.process.stdin.close()
        if self.markers:
            (self.markers / "release").touch()
        try:
            self.process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
            pytest.fail("Synthetic child failed to exit after pipe closure and read release")
        finally:
            self.stopped = True
            if not self.process.stdout.closed:
                self.raw.extend(self.process.stdout.read())
                self.process.stdout.close()
        stderr = self.stderr_path.read_text(encoding="utf-8")
        stdout = self.raw.decode("utf-8")
        assert self.process.returncode in ({0} if self.core else {0, 2})
        for marker in ("private_sentinel", "synthetic_private", "forbidden_live_access"):
            assert marker not in (stdout + stderr).lower()
        assert "Traceback" not in stderr
        assert ("CORE_TEST_NO_LIVE_ACCESS" if self.core else "PLUGIN_TEST_NO_LIVE_IMPORTS") in stderr
        assert not stdout or stdout.endswith("\n")
        for line in stdout.splitlines():
            JSONRPCMessage.model_validate_json(line)
            _wire._assert_no_meta(json.loads(line))
        assert not list(self.child_tmp.glob("line-summary-synthetic-*")), "Synthetic source was not cleaned up"


def wait_for_marker(path):
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert path.exists(), "Synthetic read did not start within its bounded deadline"


@pytest.mark.parametrize("closed_pipe", ["stdin_eof", "both_pipes"])
def test_inflight_real_pipe_closure_exits_and_restart_rejects_old_choice(tmp_path, closed_pipe, record_property):
    markers = tmp_path / "read-markers"
    markers.mkdir()
    settings = {"mode": "mock", "min_interval_seconds": 0.0}
    with PipePeer(tmp_path / "first", settings=settings, markers=markers) as first:
        first.initialize()
        old = first.chosen_history("chatgpt")
        first.send("tools/call", {"name": "line_get_history", "arguments": old})
        wait_for_marker(markers / "started")
        assert first.process.poll() is None and not (markers / "finished").exists()
        if closed_pipe == "both_pipes":
            first.process.stdout.close()
        first.process.stdin.close()
        # The actual pipe is closed before the blocked worker can finish.
        (markers / "release").touch()
        first.stop()
    assert (markers / "finished").exists() and (markers / "cleaned").exists()
    assert not (markers / "closed_before_read_finished").exists()
    record_property("disconnect_exit_code", first.process.returncode)
    record_property("unread_closed_stdout", "NOT_OBSERVABLE" if closed_pipe == "both_pipes" else "CAPTURED")
    with PipePeer(tmp_path / "restarted", settings=settings) as restarted:
        restarted.initialize()
        status = restarted.call("line_status", {})
        assert status["status"] == "ok" and status["items"][0]["real_data_enabled"] is False
        refused = restarted.call("line_get_history", old)
        assert refused["status"] == "requires_confirmation" and refused["items"] == []
        assert refused["error_code"] == "scope_confirmation_required"
        fresh = restarted.chosen_history("chatgpt")
        assert fresh["mock_scope_id"] != old["mock_scope_id"]
        accepted = restarted.call("line_get_history", fresh)
        assert accepted["status"] == "ok" and len(accepted["items"]) == 1


@pytest.mark.parametrize("budget", ["session_messages", "requests_per_minute"])
def test_not_supported_global_budget_across_host_processes(tmp_path, budget, record_property):
    """Two synthetic destination labels are NOT actual connected host clients."""
    record_property("global_budget", "NOT_SUPPORTED: limits are process-local")
    record_property("real_multi_host_execution", "NOT_RUN: local synthetic destination labels only")
    settings = {"mode": "mock", "min_interval_seconds": 0.0,
                "max_session_messages": 1 if budget == "session_messages" else 5000,
                "requests_per_minute": 1 if budget == "requests_per_minute" else 60}
    with ExitStack() as stack:
        peers = [stack.enter_context(PipePeer(tmp_path / host, settings=settings))
                 for host in ("chatgpt", "claude")]
        for peer in peers:
            peer.initialize()
        started = time.monotonic()
        delivered = []
        for host, peer in zip(("chatgpt", "claude"), peers):
            result = peer.call("line_get_history", peer.chosen_history(host))
            assert result["status"] == "ok" and len(result["items"]) == 1
            delivered.extend(result["items"])
        assert time.monotonic() - started < 60
        # Aggregate exceeds a cap of one: explicitly a missing global guarantee.
        assert len(delivered) == 2
        for host, peer in zip(("chatgpt", "claude"), peers):
            limited = peer.call("line_get_history", peer.chosen_history(host))
            expected = "session_budget_exhausted" if budget == "session_messages" else "rate_limited"
            assert limited["error_code"] == expected and limited["items"] == []
            assert limited["content_complete"] is False


CORE_DISABLED_BOOTSTRAP = r'''
import builtins, importlib.abc, os, socket, sqlite3, subprocess, sys
assert not any(key.startswith("COV_CORE_") for key in os.environ)
class ForbiddenLiveAccess(BaseException):
    pass
def forbidden(*args, **kwargs):
    sys.stderr.write("FORBIDDEN_LIVE_ACCESS\n")
    raise ForbiddenLiveAccess()
blocked = {"plugin_adapter", "plugin_runtime", "mock_privacy", "apsw", "psutil", "win32api", "win32process"}
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split(".")[0] in blocked:
        forbidden()
    return original_import(name, *args, **kwargs)
class ImportTripwire(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            forbidden()
builtins.__import__ = guarded_import
sys.meta_path.insert(0, ImportTripwire())
socket.socket.connect = socket.socket.connect_ex = socket.create_connection = forbidden
socket.socket.bind = socket.socket.listen = forbidden
subprocess.Popen.__init__ = forbidden
subprocess.run = subprocess.check_output = os.system = forbidden
sqlite3.connect = forbidden
import key_extractor
key_extractor.extract_key = key_extractor.find_line_pid = key_extractor._scan_memory_regions = forbidden
key_extractor.confirm_user_consent = forbidden
import db_reader
db_reader.DbReader.__init__ = db_reader._open_encrypted = forbidden
import line_mcp_server as core
core._SETTINGS_PATH = sys.argv[1]
assert not os.path.exists(core._SETTINGS_PATH)
assert core._policy is None and core._reader is None
try:
    core.mcp.run(transport="stdio")
finally:
    assert core._get_policy()["enabled"] is False
    assert core._reader is None
    assert core._session_messages == core._session_bytes == 0
    assert not (blocked & set(sys.modules))
    sys.stderr.write("CORE_TEST_NO_LIVE_ACCESS\n")
'''


@pytest.mark.skipif(not (ROOT / "line_mcp_server.py").is_file(),
                    reason="NOT_RUN: original core is intentionally excluded from the synthetic-only bundle")
def test_original_core_default_disabled_stdio_never_falls_back_to_synthetic(tmp_path, record_property):
    record_property("core_disabled_child_coverage", "NOT_MEASURED: all SQLite is forbidden; core coverage comes from other tests")
    with PipePeer(tmp_path / "original-core", core=True) as peer:
        peer.initialize()
        assert set(peer.tools) == {"line_list_chats", "line_get_history", "line_get_unread", "line_get_contacts"}
        calls = {
            "line_list_chats": {"query": _wire.SENTINEL},
            "line_get_history": {**HISTORY, "chat_id": _wire.SENTINEL},
            "line_get_unread": {},
            "line_get_contacts": {"query": _wire.SENTINEL},
        }
        for name, arguments in calls.items():
            response = peer.request("tools/call", {"name": name, "arguments": arguments})["result"]
            assert response["isError"] is True
            assert response.get("structuredContent") is None
            assert len(response["content"]) == 1 and response["content"][0]["type"] == "text"
            message = response["content"][0]["text"]
            assert "LINE access disabled" in message
            assert _wire.SENTINEL not in message and "synthetic" not in message.lower()
            assert "human_confirmation_verified" not in message
