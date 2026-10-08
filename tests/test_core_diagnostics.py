"""Synthetic real-pipe regressions for the legacy core's SDK error boundary."""
import asyncio
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest
from mcp.types import JSONRPCMessage, LATEST_PROTOCOL_VERSION
from mcp.server.fastmcp.exceptions import ToolError
from types import MappingProxyType

ROOT = Path(__file__).resolve().parents[1]
MARKER = "QA_CORE_PRIVATE_MARKER"
BOOTSTRAP = r'''
import asyncio, logging, os, socket, sqlite3, subprocess, sys
if os.environ.get("CORE_TEST_LOG_LEVEL"):
    logging.basicConfig(level=getattr(logging, os.environ["CORE_TEST_LOG_LEVEL"]))
# Windows asyncio uses a private socketpair for its wakeup pipe. Construct that
# stdlib event loop before denying every subsequent network operation. Normal
# SDK win32api imports are not LINE access and must remain available.
loop = asyncio.new_event_loop()
asyncio.events.new_event_loop = lambda: loop
import line_mcp_server as core
import db_reader, key_extractor
class ForbiddenLiveAccess(BaseException):
    pass
def forbidden(*args, **kwargs):
    raise ForbiddenLiveAccess("Synthetic core test forbids live access")
socket.socket.connect = socket.socket.connect_ex = socket.create_connection = forbidden
socket.socket.bind = socket.socket.listen = forbidden
sqlite3.connect = forbidden
subprocess.Popen.__init__ = forbidden
subprocess.run = subprocess.check_output = os.system = forbidden
core.extract_key = key_extractor.extract_key = forbidden
key_extractor.find_line_pid = key_extractor._scan_memory_regions = forbidden
key_extractor.confirm_user_consent = forbidden
db_reader.DbReader.__init__ = db_reader._open_encrypted = forbidden
core._SETTINGS_PATH = sys.argv[1]
assert not os.path.exists(core._SETTINGS_PATH)
assert core._policy is None and core._reader is None
try:
    core.mcp.run(transport="stdio")
finally:
    assert core._get_policy()["enabled"] is False
    assert core._reader is None
    assert core._session_messages == core._session_bytes == 0
    assert not {"plugin_adapter", "plugin_runtime", "mock_privacy"} & set(sys.modules)
    sys.stderr.write("CORE_DIAGNOSTIC_NO_LIVE_ACCESS\n")
'''


@pytest.fixture(autouse=True)
def isolate_offline_server_settings():
    """The wire cases guard fresh children instead of loading parent settings."""


class Peer:
    def __init__(self, directory, level):
        self.raw = bytearray()
        self.stderr_path = directory / "stderr.txt"
        env = {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE_")}
        env.pop("COVERAGE_PROCESS_START", None)
        env["CORE_TEST_LOG_LEVEL"] = level
        with self.stderr_path.open("wb") as stderr:
            self.process = subprocess.Popen(
                [sys.executable, "-c", BOOTSTRAP, str(directory / "absent-settings.json")],
                cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            )
        self.counter = 0

    def send(self, method, params=None, *, notification=False):
        frame = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        if not notification:
            self.counter += 1
            frame["id"] = self.counter
        self.process.stdin.write((json.dumps(frame) + "\n").encode())
        self.process.stdin.flush()
        return frame.get("id")

    def receive(self, request_id):
        results = queue.Queue(maxsize=1)
        def read():
            try:
                while True:
                    line = self.process.stdout.readline()
                    if not line:
                        raise EOFError("Synthetic core ended before its response")
                    self.raw.extend(line)
                    JSONRPCMessage.model_validate_json(line)
                    frame = json.loads(line)
                    if frame.get("id") == request_id:
                        results.put(frame)
                        return
            except BaseException as exc:
                results.put(exc)
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        try:
            result = results.get(timeout=15)
        except queue.Empty:
            pytest.fail("Synthetic core response timed out")
        thread.join(timeout=1)
        assert not thread.is_alive()
        if isinstance(result, BaseException):
            raise result
        return result

    def request(self, method, params=None):
        return self.receive(self.send(method, params))

    def initialize(self):
        result = self.request("initialize", {
            "protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "synthetic-core-diagnostic-test", "version": "1"},
        })
        assert "serverInfo" in result["result"]
        self.send("notifications/initialized", notification=True)

    def stop(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
            pytest.fail("Synthetic core failed to exit")
        self.raw.extend(self.process.stdout.read())
        self.process.stdout.close()
        assert self.process.returncode == 0
        stderr = self.stderr_path.read_text(encoding="utf-8")
        assert "CORE_DIAGNOSTIC_NO_LIVE_ACCESS" in stderr
        assert "ForbiddenLiveAccess" not in stderr
        return self.raw.decode("utf-8"), stderr


@pytest.mark.parametrize("level", ["", "WARNING", "DEBUG"])
@pytest.mark.parametrize("case", ["malformed_name", "malformed_json", "unknown_tool", "wrong_limit", "wrong_query", "unknown_prompt", "unknown_resource"])
def test_core_sdk_errors_never_echo_synthetic_private_inputs(tmp_path, level, case):
    peer = Peer(tmp_path, level)
    try:
        peer.initialize()
        if case == "malformed_json":
            peer.process.stdin.write(("{\"" + MARKER + "\":\n").encode())
            peer.process.stdin.flush()
            assert "result" in peer.request("ping")
        elif case == "malformed_name":
            peer.request("tools/call", {"name": {"private": MARKER}})
        elif case == "unknown_tool":
            peer.request("tools/call", {"name": MARKER, "arguments": {}})
        elif case == "wrong_limit":
            peer.request("tools/call", {"name": "line_get_history", "arguments": {"chat_id": "synthetic", "since": "2026-10-01T00:00:00Z", "until": "2026-10-02T00:00:00Z", "limit": {"private": MARKER}}})
        elif case == "wrong_query":
            peer.request("tools/call", {"name": "line_list_chats", "arguments": {"query": {"private": MARKER}}})
        elif case == "unknown_prompt":
            peer.request("prompts/get", {"name": MARKER})
        else:
            peer.request("resources/read", {"uri": "https://example.invalid/" + MARKER})
    finally:
        stdout, stderr = peer.stop()
    assert MARKER.lower() not in (stdout + stderr).lower()
    assert "Traceback" not in stderr


@pytest.fixture
def core(monkeypatch, tmp_path):
    import line_mcp_server as server
    monkeypatch.setattr(server, "_SETTINGS_PATH", str(tmp_path / "absent-settings.json"))
    monkeypatch.setattr(server, "_policy", None)
    monkeypatch.setattr(server, "_reader", None)
    monkeypatch.setattr(server, "_session_messages", 0)
    monkeypatch.setattr(server, "_session_bytes", 0)
    return server


@pytest.mark.parametrize("tool", ["line_list_chats", "line_get_history", "line_get_unread", "line_get_contacts"])
@pytest.mark.parametrize("failure", [RuntimeError, PermissionError])
def test_backend_errors_do_not_echo_rows_paths_or_exception_text(core, monkeypatch, tool, failure):
    policy = dict(core._DEFAULTS)
    policy.update(enabled=True, allowed_chat_ids=frozenset({"synthetic"}), allow_contacts=True)
    monkeypatch.setattr(core, "_policy", MappingProxyType(policy))
    class BrokenSyntheticReader:
        def __getattr__(self, name):
            def read(*args, **kwargs):
                raise failure(MARKER + "_/synthetic/private/account.db")
            return read
    monkeypatch.setattr(core, "_get_reader", lambda: BrokenSyntheticReader())
    arguments = {"chat_id": "synthetic", "since": "2026-10-01T00:00:00Z", "until": "2026-10-02T00:00:00Z"} if tool == "line_get_history" else {}
    with pytest.raises(ToolError) as error:
        asyncio.run(core.mcp.call_tool(tool, arguments))
    assert str(error.value) == "LINE tool request failed"


def test_disabled_policy_keeps_a_safe_actionable_diagnostic(core):
    with pytest.raises(ToolError) as error:
        asyncio.run(core.mcp.call_tool("line_list_chats", {}))
    assert str(error.value) == core._DISABLED_DIAGNOSTIC
    assert core._reader is None


@pytest.mark.parametrize("name,arguments", [(MARKER, {}), ("line_list_chats", {"query": {"private": MARKER}})])
def test_direct_sdk_validation_errors_are_fixed_before_data_access(core, name, arguments):
    with pytest.raises(ToolError) as error:
        asyncio.run(core.mcp.call_tool(name, arguments))
    assert str(error.value) == "LINE tool request failed"
    assert core._reader is None and core._policy is None


@pytest.mark.parametrize("surface", ["prompt", "resource"])
def test_direct_unknown_surfaces_do_not_echo_identifiers(core, surface):
    operation = core.mcp.get_prompt(MARKER, {}) if surface == "prompt" else core.mcp.read_resource("https://example.invalid/" + MARKER)
    with pytest.raises(Exception) as error:
        asyncio.run(operation)
    assert str(error.value) == ("LINE prompt request failed" if surface == "prompt" else "LINE resource request failed")


@pytest.mark.parametrize("tool", ["line_list_chats", "line_get_history", "line_get_unread", "line_get_contacts"])
def test_successful_legacy_tools_keep_text_json(core, monkeypatch, tool):
    envelope = {"items": [], "has_more": False, "next_cursor": None, "content_complete": True}
    policy = dict(core._DEFAULTS)
    policy.update(enabled=True, allowed_chat_ids=frozenset({"synthetic"}), allow_contacts=True)
    monkeypatch.setattr(core, "_policy", MappingProxyType(policy))
    class SyntheticReader:
        def __getattr__(self, name):
            return lambda *args, **kwargs: dict(envelope)
    monkeypatch.setattr(core, "_get_reader", lambda: SyntheticReader())
    arguments = {"chat_id": "synthetic", "since": "2026-10-01T00:00:00Z", "until": "2026-10-02T00:00:00Z"} if tool == "line_get_history" else {}
    result = asyncio.run(core.mcp.call_tool(tool, arguments))
    assert len(result) == 1 and result[0].type == "text"
    assert json.loads(result[0].text) == envelope


@pytest.mark.parametrize("tool,arguments,field,converted", [
    ("line_list_chats", {"limit": "1"}, "limit", 1),
    ("line_get_unread", {"include_official": "false"}, "include_official", False),
    ("line_get_contacts", {"limit": "1.0"}, "limit", 1),
])
def test_legacy_sdk_ad_hoc_conversion_still_reaches_the_reader(core, monkeypatch, tool, arguments, field, converted):
    policy = dict(core._DEFAULTS)
    policy.update(enabled=True, allowed_chat_ids=frozenset({"synthetic"}), allow_contacts=True)
    monkeypatch.setattr(core, "_policy", MappingProxyType(policy))
    received = []
    class SyntheticReader:
        def __getattr__(self, name):
            def read(*args, **kwargs):
                received.append(kwargs)
                return {"items": [], "has_more": False, "next_cursor": None, "content_complete": True}
            return read
    monkeypatch.setattr(core, "_get_reader", lambda: SyntheticReader())
    result = asyncio.run(core.mcp.call_tool(tool, arguments))
    assert len(result) == 1 and result[0].type == "text"
    assert len(received) == 1 and received[0][field] == converted
    assert type(received[0][field]) is type(converted)


@pytest.mark.parametrize("origin", ["named", "root", "invalid"])
def test_sdk_log_filter_clears_all_dynamic_diagnostic_fields(core, origin):
    name = "root" if origin == "root" else "mcp.shared.session"
    path = str(core._MCP_LOG_DIRECTORY / "shared" / "session.py") if origin == "root" else __file__
    try:
        raise RuntimeError(MARKER)
    except RuntimeError:
        record = logging.LogRecord(name, logging.WARNING, path, 1, "%s", (MARKER,), sys.exc_info())
    if origin == "invalid":
        record.pathname = None
    record.exc_text = record.stack_info = MARKER
    assert core._MCPDiagnosticFilter().filter(record)
    assert MARKER not in logging.Formatter().format(record)
    assert record.args == () and record.exc_info is record.exc_text is record.stack_info is None
    other = logging.LogRecord("unrelated", logging.WARNING, __file__, 1, "ordinary", (), None)
    core._MCPDiagnosticFilter().filter(other)
    assert other.getMessage() == "ordinary"


def test_log_filter_installation_is_idempotent(core):
    handler = logging.StreamHandler()
    logger = logging.getLogger("mcp.server.lowlevel.server")
    logger.addHandler(handler)
    try:
        core._install_diagnostic_filters()
        core._install_diagnostic_filters()
        assert sum(isinstance(item, core._MCPDiagnosticFilter) for item in handler.filters) == 1
    finally:
        logger.removeHandler(handler)

