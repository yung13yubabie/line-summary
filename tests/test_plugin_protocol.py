"""Real JSON-RPC/stdio checks for the disabled and synthetic-only adapter.

These tests launch a fresh Python process through MCP's stdio transport. They
never start HTTP, access LINE, use credentials, or depend on an account login.
"""
import asyncio
import importlib
import json
from pathlib import Path
import subprocess
import sys
from datetime import timedelta

import jsonschema
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.types import JSONRPCMessage, LATEST_PROTOCOL_VERSION


ROOT = Path(__file__).resolve().parents[1]
SINCE = "2026-10-01T00:00:00+08:00"
UNTIL = "2026-10-02T00:00:00+08:00"
CALLS = {
    "line_status": {},
    "line_list_chats": {},
    "line_get_history": {"chat_id": "demo-project", "since": SINCE, "until": UNTIL},
    "line_search_messages": {"chat_id": "demo-project", "query": "報價", "since": SINCE, "until": UNTIL},
    "line_get_unread": {},
    "line_get_contacts": {},
}
SENTINEL = "PRIVATE_SENTINEL_/home/private/account.sqlite_QUERY_TOKEN"

# Guard in the actual child, before importing the adapter. Catch both ordinary
# imports and importlib imports. BaseException is intentional: the production
# sanitizing Exception handlers must not turn a prohibited access into a pass.
BOOTSTRAP = r'''
import builtins
import importlib.abc
import socket
import sys
import os

blocked = {"key_extractor", "line_mcp_server", "apsw", "psutil", "win32api", "win32process"}
class ForbiddenLiveAccess(BaseException):
    pass

def forbid(reason):
    sys.stderr.write("FORBIDDEN_LIVE_ACCESS: " + reason + "\n")
    sys.stderr.flush()
    raise ForbiddenLiveAccess(reason)

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split(".")[0] in blocked:
        forbid("import")
    return original_import(name, *args, **kwargs)
class ImportTripwire(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            forbid("importlib")
builtins.__import__ = guarded_import
sys.meta_path.insert(0, ImportTripwire())

def no_network(*args, **kwargs):
    forbid("network")
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.create_connection = no_network

import plugin_adapter
import db_reader
if os.environ.get("PLUGIN_TEST_PRIVACY_CLOCK"):
    import json
    clock_times = json.loads(os.environ["PLUGIN_TEST_PRIVACY_CLOCK"])
    original_privacy_init = plugin_adapter.MockPrivacyChoice.__init__
    def sequenced_privacy_init(self, *args, **kwargs):
        calls = 0
        def synthetic_clock():
            nonlocal calls
            now = clock_times[min(calls, len(clock_times) - 1)]
            calls += 1
            return now
        original_privacy_init(self, clock=synthetic_clock)
    plugin_adapter.MockPrivacyChoice.__init__ = sequenced_privacy_init
if os.environ.get("PLUGIN_TEST_SLOW_READ_DIR"):
    from pathlib import Path
    import time
    marker_dir = Path(os.environ["PLUGIN_TEST_SLOW_READ_DIR"])
    original_read = plugin_adapter.PluginService._read
    def coordinated_synthetic_read(self, name, arguments):
        (marker_dir / "started").touch()
        deadline = time.monotonic() + 10
        while not (marker_dir / "release").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("Synthetic test coordination timed out")
            time.sleep(0.005)
        return original_read(self, name, arguments)
    plugin_adapter.PluginService._read = coordinated_synthetic_read
original_reader_init = db_reader.DbReader.__init__
def synthetic_reader_only(self, db_path, key, _test_mode=False):
    if _test_mode is not True or key is not None:
        forbid("live reader")
    return original_reader_init(self, db_path, key, _test_mode=True)
db_reader.DbReader.__init__ = synthetic_reader_only
db_reader._open_encrypted = lambda *args, **kwargs: forbid("encrypted database")

if os.environ.get("PLUGIN_TEST_SOURCE_ERROR") == "1":
    def broken_read(self, name, arguments):
        raise RuntimeError("PRIVATE_SENTINEL_/home/user/line.db_SECRET_QUERY")
    plugin_adapter.PluginService._read = broken_read

lifecycle_error = os.environ.get("PLUGIN_TEST_LIFECYCLE_ERROR")
if lifecycle_error == "initialize":
    def broken_initialize(self):
        raise RuntimeError("SYNTHETIC_PRIVATE_PROTOCOL_SENTINEL_/home/private/source.sqlite")
    plugin_adapter.SyntheticSource.__init__ = broken_initialize
elif lifecycle_error == "cleanup":
    original_close = plugin_adapter.SyntheticSource.close
    def broken_cleanup(self):
        original_close(self)
        raise RuntimeError("SYNTHETIC_PRIVATE_PROTOCOL_SENTINEL_/home/private/source.sqlite")
    plugin_adapter.SyntheticSource.close = broken_cleanup
elif lifecycle_error == "run":
    def broken_run(self, *args, **kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_PROTOCOL_SENTINEL_/home/private/source.sqlite")
    plugin_adapter.SafeFastMCP.run = broken_run

try:
    raise SystemExit(plugin_adapter.main())
finally:
    assert not (blocked & set(sys.modules)), "A live-access module was loaded"
    sys.stderr.write("PLUGIN_TEST_NO_LIVE_IMPORTS\n")
'''


@pytest.fixture(autouse=True)
def isolate_offline_server_settings():
    """Do not import the legacy server via the suite's unrelated autofixture."""


def _assert_no_meta(value):
    if isinstance(value, dict):
        assert "_meta" not in value, "Prototype results must not conceal data in _meta"
        for child in value.values():
            _assert_no_meta(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_meta(child)


def _assert_envelope(result, tool, mode):
    wire = result.model_dump(by_alias=True, exclude_none=True)
    _assert_no_meta(wire)
    assert not result.isError
    payload = result.structuredContent
    assert isinstance(payload, dict)
    jsonschema.Draft202012Validator.check_schema(tool.outputSchema)
    jsonschema.Draft202012Validator(tool.outputSchema).validate(payload)
    assert payload["mode"] == mode
    assert payload["synthetic"] is (mode == "mock")
    assert payload["thread_support"] == "unsupported"
    assert payload["human_confirmation_verified"] is False
    assert payload["privacy_processing"] == "not_implemented"
    assert payload["source_sync_at"] is None
    assert payload["fetched_at"]
    # Text fallback and structured output must describe exactly the same result.
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert json.loads(result.content[0].text) == payload
    return payload


def _wire_exchange(tmp_path, monkeypatch, *, mode=None, calls=None, source_error=False, min_interval=0.0, unsupported_surfaces=False, scenario=None, expire_privacy=False, privacy_clock=None, slow_read_dir=None, max_response_bytes=None):
    """Run initialize/list/call over real pipes and record every stdout byte."""
    settings_path = tmp_path / "plugin-config.json"
    if mode is not None:
        config = {"mode": mode, "min_interval_seconds": min_interval}
        if max_response_bytes is not None:
            config["max_response_bytes"] = max_response_bytes
        settings_path.write_text(json.dumps(config), encoding="utf-8")
    # For mode=None, the file intentionally does not exist: exercise defaults.
    args = ["-c", BOOTSTRAP, "--settings", str(settings_path)]
    parameters = StdioServerParameters(
        command=sys.executable, args=args, cwd=str(ROOT),
        env={"PLUGIN_TEST_SOURCE_ERROR": "1" if source_error else "0",
             "PLUGIN_TEST_PRIVACY_CLOCK": json.dumps(privacy_clock or [1000.0, 1120.0]) if expire_privacy or privacy_clock else "",
             "PLUGIN_TEST_SLOW_READ_DIR": str(slow_read_dir) if slow_read_dir else ""},
    )
    stdio_module = importlib.import_module("mcp.client.stdio")
    original_text_stream = stdio_module.TextReceiveStream
    stdout_chunks = []

    async def record_stdout(*args, **kwargs):
        async for chunk in original_text_stream(*args, **kwargs):
            stdout_chunks.append(chunk)
            yield chunk

    monkeypatch.setattr(stdio_module, "TextReceiveStream", record_stdout)
    stderr_path = tmp_path / "server.stderr"

    async def exchange(errlog):
        async with stdio_client(parameters, errlog=errlog) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
                initialized = await session.initialize()
                assert initialized.capabilities.tools is not None
                assert initialized.capabilities.prompts is None
                assert initialized.capabilities.resources is None
                listed = await session.list_tools()
                tool_map = {tool.name: tool for tool in listed.tools}
                results = []
                if scenario is not None:
                    results = await scenario(session, tool_map)
                else:
                    for name, arguments in (calls or list(CALLS.items())):
                        results.append((name, await session.call_tool(name, arguments=arguments)))
                if unsupported_surfaces:
                    for operation in (
                        lambda: session.get_prompt(SENTINEL),
                        lambda: session.read_resource("test://" + SENTINEL),
                    ):
                        with pytest.raises(McpError) as error:
                            await operation()
                        assert error.value.error.code == -32601
                        assert error.value.error.message == "Method not found"
                        assert error.value.error.data is None
                        assert "PRIVATE_SENTINEL" not in str(error.value)
                return tool_map, results

    with stderr_path.open("w", encoding="utf-8") as errlog:
        tool_map, results = asyncio.run(asyncio.wait_for(exchange(errlog), timeout=40))
    stderr = stderr_path.read_text(encoding="utf-8")
    assert "PLUGIN_TEST_NO_LIVE_IMPORTS" in stderr
    assert "FORBIDDEN_LIVE_ACCESS" not in stderr
    raw_stdout = "".join(stdout_chunks)
    assert raw_stdout and raw_stdout.endswith("\n")
    frames = raw_stdout.splitlines()
    assert len(frames) >= 2 + len(results)
    for frame in frames:
        # Reject banners, tracebacks, print debugging, blank lines and partial
        # frames even if ClientSession would otherwise ignore transport errors.
        JSONRPCMessage.model_validate_json(frame)
        assert json.loads(frame)["jsonrpc"] == "2.0"
    assert "private_sentinel" not in raw_stdout.lower()
    assert "private_sentinel" not in stderr.lower()
    return tool_map, results


async def _wire_preview(session, tool_map, name, arguments, *, destination="local-test"):
    """An explicit synthetic preview, never a claim of verified human approval."""
    request = {**arguments, "destination": destination}
    result = await session.call_tool(name, arguments=request)
    payload = _assert_envelope(result, tool_map[name], "mock")
    assert payload["status"] == "requires_confirmation"
    assert payload["error_code"] == "privacy_choice_required"
    assert payload["items"] == [] and payload["content_complete"] is False
    return request, payload


async def _wire_demo_call(session, tool_map, name, arguments, *, choice, destination="local-test"):
    request, preview = await _wire_preview(session, tool_map, name, arguments, destination=destination)
    return await session.call_tool(name, arguments={
        **request, "privacy_choice": choice, "mock_scope_id": preview["mock_scope_id"],
    })


@pytest.mark.parametrize("mode", [None, "disabled", "mock"])
def test_real_stdio_initialize_list_and_all_six_tools(tmp_path, monkeypatch, mode):
    async def chosen_examples(session, tool_map):
        results = []
        for name, arguments in CALLS.items():
            result = (await session.call_tool(name, arguments=arguments) if name == "line_status" else
                      await _wire_demo_call(session, tool_map, name, arguments, choice="deidentify"))
            results.append((name, result))
        return results
    tool_map, results = _wire_exchange(tmp_path, monkeypatch, mode=mode,
                                      scenario=chosen_examples if mode == "mock" else None)
    assert set(tool_map) == set(CALLS)
    for tool in tool_map.values():
        assert tool.outputSchema
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True
        assert tool.annotations.openWorldHint is False
        _assert_no_meta(tool.model_dump(by_alias=True, exclude_none=True))
    for name, result in results:
        payload = _assert_envelope(result, tool_map[name], mode or "disabled")
        if mode != "mock":
            assert payload["status"] == "disabled"
            assert payload["error_code"] == "live_data_not_implemented"
            assert payload["items"] == []
            assert payload["has_more"] is False
            assert payload["next_cursor"] is None
            assert payload["content_complete"] is False
            assert payload["coverage"] == "not_read"
            assert payload["consistency"] == "not_read"
            assert payload["source_latest_at"] is None
            assert payload["latest_returned_at"] is None
        else:
            assert payload["status"] == "ok", (name, payload)
            assert payload["items"], name
            assert "demo-denied" not in json.dumps(payload, ensure_ascii=False)
            assert "demo-hidden" not in json.dumps(payload, ensure_ascii=False)
            if name == "line_status":
                status = payload["items"][0]
                assert status["real_data_enabled"] is False
                assert status["privacy_filter_implemented"] is False
                assert status["automatic_sync"] is False
            if name in {"line_get_history", "line_search_messages"}:
                assert all(item["source_ref"]["chat_id"] == "demo-project" for item in payload["items"])
                assert all("合成資料" in item["content"] for item in payload["items"])


@pytest.mark.parametrize(("tool_name", "arguments"), [
    ("line_list_chats", {"query": SENTINEL, "limit": SENTINEL}),
    ("line_search_messages", {**CALLS["line_search_messages"], "query": {SENTINEL: SENTINEL}}),
    ("line_get_history", {**CALLS["line_get_history"], "since": SENTINEL}),
    ("line_get_history", {**CALLS["line_get_history"], "chat_id": SENTINEL}),
    ("line_status", {"mode": "real"}),
    ("line_status", {"privacy_ready": True}),
    ("line_status", {"db_path": SENTINEL}),
    ("line_status", {"unexpected": SENTINEL}),
    ("line_get_history", {}),
])
def test_wire_errors_do_not_echo_arguments_paths_or_queries(tmp_path, monkeypatch, tool_name, arguments):
    tools, results = _wire_exchange(tmp_path, monkeypatch, mode="mock", calls=[(tool_name, arguments)])
    result = results[0][1]
    payload = _assert_envelope(result, tools[tool_name], "mock")
    assert payload["status"] == "error"
    assert payload["items"] == []
    assert payload["error_code"] in {"invalid_request", "invalid_tool_request", "scope_denied"}
    assert payload["content_complete"] is False


@pytest.mark.parametrize("raw", [
    '{"mode":"real"}',
    json.dumps({"privacy_ready": True, "db_path": SENTINEL}),
    json.dumps({"mode": "mock", "db_path": SENTINEL}),
    json.dumps({"mode": "mock", "unknown": SENTINEL}),
    '{"mode":',
])
def test_cli_rejects_unsafe_settings_with_static_stderr(tmp_path, raw):
    path = tmp_path / (SENTINEL.replace("/", "_") + ".json")
    path.write_text(raw, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-c", BOOTSTRAP, "--settings", str(path)],
        cwd=ROOT, capture_output=True, text=True, timeout=15, check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Invalid plugin settings. Live data mode is not implemented." in completed.stderr
    assert "PLUGIN_TEST_NO_LIVE_IMPORTS" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert "PRIVATE_SENTINEL" not in completed.stderr


def test_wire_backend_failure_uses_schema_and_static_error(tmp_path, monkeypatch):
    tools, results = _wire_exchange(
        tmp_path, monkeypatch, mode="mock", calls=[("line_status", {})], source_error=True,
    )
    payload = _assert_envelope(results[0][1], tools["line_status"], "mock")
    assert payload["status"] == "error"
    assert payload["error_code"] == "source_error"
    assert payload["items"] == []


def test_wire_rate_limit_has_schema_valid_retry_after(tmp_path, monkeypatch):
    tools, results = _wire_exchange(
        tmp_path, monkeypatch, mode="mock",
        calls=[("line_status", {}), ("line_status", {})], min_interval=2.0,
    )
    first = _assert_envelope(results[0][1], tools["line_status"], "mock")
    limited = _assert_envelope(results[1][1], tools["line_status"], "mock")
    assert first["status"] == "ok"
    assert limited["status"] == limited["error_code"] == "rate_limited"
    assert 0 < limited["retry_after_seconds"] <= 2.0
    assert limited["items"] == []


def test_wire_unknown_tool_name_is_not_echoed(tmp_path, monkeypatch):
    tools, results = _wire_exchange(tmp_path, monkeypatch, mode="mock", calls=[(SENTINEL, {})])
    payload = _assert_envelope(results[0][1], tools["line_status"], "mock")
    assert payload["status"] == "error"
    assert payload["error_code"] == "invalid_tool_request"
    assert payload["items"] == []


PROTOCOL_SENTINEL = "SYNTHETIC_PRIVATE_PROTOCOL_SENTINEL"


def _assert_static_process_output(completed):
    # Check short prefixes too: Pydantic can truncate a long input value, and
    # URI handling can normalize its casing before an unsafe diagnostic.
    for marker in ("synthetic_private", "private_sentinel", "/home/private/source.sqlite"):
        assert marker not in completed.stdout.lower()
        assert marker not in completed.stderr.lower()
    assert "Traceback" not in completed.stderr
    assert "FORBIDDEN_LIVE_ACCESS" not in completed.stderr
    assert "PLUGIN_TEST_NO_LIVE_IMPORTS" in completed.stderr
    frames = []
    for line in completed.stdout.splitlines():
        JSONRPCMessage.model_validate_json(line)
        frame = json.loads(line)
        _assert_no_meta(frame)
        frames.append(frame)
    return frames


@pytest.mark.parametrize("malformed_line", [
    PROTOCOL_SENTINEL,
    '{"' + PROTOCOL_SENTINEL + '":',
    json.dumps(PROTOCOL_SENTINEL),
    json.dumps({"jsonrpc": "2.0", "id": 70, "method": {"private": PROTOCOL_SENTINEL}}),
    json.dumps({"jsonrpc": "1.0", "id": 71, "method": "tools/list", "params": {"query": PROTOCOL_SENTINEL}}),
    json.dumps({"jsonrpc": "2.0", "id": 72, "method": "tools/call", "params": {"name": {"private": PROTOCOL_SENTINEL}}}),
    json.dumps({"jsonrpc": "2.0", "id": 73, "result": {"private": PROTOCOL_SENTINEL}}),
])
@pytest.mark.parametrize("preconfigured_logging", [False, True])
def test_raw_stdio_malformed_protocol_never_echoes_private_input(tmp_path, malformed_line, preconfigured_logging):
    # These frames deliberately bypass ClientSession's outgoing validation so
    # the actual server stdio parser and pre-tools/call SDK branches execute.
    initialize = {
        "jsonrpc": "2.0", "id": 100, "method": "initialize",
        "params": {"protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": {"name": "offline-protocol-regression", "version": "1"}},
    }
    bootstrap = ("import logging\nlogging.basicConfig(level=logging.DEBUG)\n" if preconfigured_logging else "") + BOOTSTRAP
    completed = subprocess.run(
        [sys.executable, "-c", bootstrap, "--settings", str(tmp_path / "missing.json")],
        cwd=ROOT, input=malformed_line + "\n" + json.dumps(initialize) + "\n",
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert completed.returncode == 0
    frames = _assert_static_process_output(completed)
    # A valid initialization after malformed input must still be processed.
    assert any(frame.get("id") == 100 and "serverInfo" in frame.get("result", {}) for frame in frames)
    for frame in frames:
        if "error" in frame:
            assert frame["error"].get("data") in (None, "")
            assert frame["error"]["message"] == "Invalid request parameters"


@pytest.mark.parametrize("arguments", [
    ["--mode", PROTOCOL_SENTINEL],
    ["--mode=" + PROTOCOL_SENTINEL],
    ["--" + PROTOCOL_SENTINEL],
    ["--unknown-option", PROTOCOL_SENTINEL],
    [PROTOCOL_SENTINEL],
])
def test_cli_invalid_options_never_echo_private_values(tmp_path, arguments):
    completed = subprocess.run(
        [sys.executable, "-c", BOOTSTRAP, "--settings", str(tmp_path / "missing.json"), *arguments],
        cwd=ROOT, capture_output=True, text=True, timeout=15, check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    _assert_static_process_output(completed)
    assert "Invalid command options. Use --help for safe local options." in completed.stderr


@pytest.mark.parametrize("path_kind", ["directory", "invalid_utf8"])
def test_cli_unreadable_config_path_uses_static_failure(tmp_path, path_kind):
    path = tmp_path / PROTOCOL_SENTINEL
    if path_kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"\xff\xfe" + PROTOCOL_SENTINEL.encode())
    completed = subprocess.run(
        [sys.executable, "-c", BOOTSTRAP, "--settings", str(path)],
        cwd=ROOT, capture_output=True, text=True, timeout=15, check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    _assert_static_process_output(completed)
    assert "Invalid plugin settings. Live data mode is not implemented." in completed.stderr


@pytest.mark.parametrize(("phase", "expected"), [
    ("initialize", "Prototype stopped after an internal error; no source details are returned."),
    ("run", "Prototype stopped after an internal error; no source details are returned."),
    ("cleanup", "Prototype cleanup failed; inspect locally without sharing private data."),
])
def test_cli_lifecycle_exceptions_never_expose_paths_or_raw_details(tmp_path, phase, expected):
    # The injected source/run failures contain only test sentinels. No account,
    # credential, encrypted database or external service is involved.
    import os
    completed = subprocess.run(
        [sys.executable, "-c", BOOTSTRAP, "--mode", "mock", "--settings", str(tmp_path / "missing.json")],
        cwd=ROOT, input="", capture_output=True, text=True, timeout=15, check=False,
        env={**os.environ, "PLUGIN_TEST_LIFECYCLE_ERROR": phase},
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    _assert_static_process_output(completed)
    assert expected in completed.stderr


def test_unused_prompt_and_resource_surfaces_are_not_exposed(tmp_path, monkeypatch):
    _wire_exchange(
        tmp_path, monkeypatch, calls=[("line_status", {})], unsupported_surfaces=True,
    )



def test_wire_malformed_cursor_after_choice_never_echoes_private_input(tmp_path, monkeypatch):
    async def scenario(session, tool_map):
        result = await _wire_demo_call(session, tool_map, "line_get_history", {
            **CALLS["line_get_history"], "cursor": SENTINEL,
        }, choice="deidentify")
        return [("line_get_history", result)]
    tools, results = _wire_exchange(tmp_path, monkeypatch, mode="mock", scenario=scenario)
    payload = _assert_envelope(results[0][1], tools["line_get_history"], "mock")
    assert payload["status"] == "error" and payload["error_code"] == "invalid_request"
    assert payload["items"] == []


def test_wire_all_data_tools_need_choice_and_status_is_exempt(tmp_path, monkeypatch):
    calls = [(name, {**arguments, "destination": "chatgpt"}) for name, arguments in CALLS.items() if name != "line_status"]
    calls.append(("line_status", {}))
    tools, results = _wire_exchange(tmp_path, monkeypatch, mode="mock", calls=calls)
    for name, result in results:
        payload = _assert_envelope(result, tools[name], "mock")
        if name == "line_status":
            assert payload["status"] == "ok"
        else:
            assert payload["status"] == "requires_confirmation"
            assert payload["error_code"] == "privacy_choice_required"
            assert payload["items"] == [] and payload["next_cursor"] is None
            assert payload["content_complete"] is False
            assert payload["mock_scope_id"]


@pytest.mark.parametrize("destination", [None, "unrecognized-host", SENTINEL])
def test_wire_unknown_destination_never_releases_or_echoes_data(tmp_path, monkeypatch, destination):
    tools, results = _wire_exchange(tmp_path, monkeypatch, mode="mock", calls=[
        ("line_get_history", {**CALLS["line_get_history"], "destination": destination, "privacy_choice": "original"}),
    ])
    payload = _assert_envelope(results[0][1], tools["line_get_history"], "mock")
    assert payload["status"] == "requires_confirmation"
    assert payload["error_code"] == "destination_required"
    assert payload["destination"] is None and payload["mock_scope_id"] is None
    assert payload["items"] == [] and payload["content_complete"] is False


def test_wire_no_host_has_an_original_only_bypass(tmp_path, monkeypatch):
    calls = [("line_get_history", {
        **CALLS["line_get_history"], "destination": destination, "privacy_choice": "original",
    }) for destination in ["local-test", "chatgpt", "claude", "gemini", "grok", "deepseek", "qwen"]]
    tools, results = _wire_exchange(tmp_path, monkeypatch, mode="mock", calls=calls)
    for name, result in results:
        payload = _assert_envelope(result, tools[name], "mock")
        assert payload["status"] == "requires_confirmation"
        assert payload["error_code"] == "scope_confirmation_required"
        assert payload["items"] == []


def test_wire_changed_chat_dates_query_cursor_limit_and_destination_refuse_prior_choice(tmp_path, monkeypatch):
    async def scenario(session, tools):
        name = "line_search_messages"
        request, preview = await _wire_preview(session, tools, name, CALLS[name], destination="chatgpt")
        chosen = {**request, "privacy_choice": "original", "mock_scope_id": preview["mock_scope_id"]}
        results = []
        for change in [
            {"chat_id": "demo-family"}, {"since": "2026-10-01T00:01:00+08:00"},
            {"until": "2026-10-03T00:00:00+08:00"}, {"query": SENTINEL},
            {"cursor": SENTINEL}, {"limit": 1}, {"destination": "claude"},
        ]:
            result = await session.call_tool(name, arguments={**chosen, **change})
            payload = _assert_envelope(result, tools[name], "mock")
            assert payload["status"] == "requires_confirmation"
            assert payload["error_code"] == "scope_confirmation_required"
            assert payload["mock_scope_id"] != preview["mock_scope_id"]
            assert payload["items"] == []
            results.append((name, result))
        return results
    _wire_exchange(tmp_path, monkeypatch, mode="mock", scenario=scenario)


def test_wire_cursor_continuation_needs_its_own_choice(tmp_path, monkeypatch):
    async def scenario(session, tools):
        name = "line_get_history"
        request, preview = await _wire_preview(session, tools, name, {**CALLS[name], "limit": 1})
        chosen = {**request, "privacy_choice": "deidentify", "mock_scope_id": preview["mock_scope_id"]}
        first_result = await session.call_tool(name, arguments=chosen)
        first = _assert_envelope(first_result, tools[name], "mock")
        assert first["status"] == "ok" and first["has_more"] is True
        cursor = first["next_cursor"]
        assert cursor
        blocked_result = await session.call_tool(name, arguments={**chosen, "cursor": cursor})
        blocked = _assert_envelope(blocked_result, tools[name], "mock")
        assert blocked["status"] == "requires_confirmation" and blocked["items"] == []
        assert blocked["request_scope"]["continuation"] is True
        second_result = await session.call_tool(name, arguments={
            **chosen, "cursor": cursor, "mock_scope_id": blocked["mock_scope_id"],
        })
        second = _assert_envelope(second_result, tools[name], "mock")
        assert second["status"] == "ok" and len(second["items"]) == 1
        assert second["items"][0]["message_id"] != first["items"][0]["message_id"]
        return [(name, first_result), (name, blocked_result), (name, second_result)]
    _wire_exchange(tmp_path, monkeypatch, mode="mock", scenario=scenario)


@pytest.mark.parametrize("choice", ["deidentify", "original"])
def test_wire_choices_never_claim_verified_consent_or_redaction_and_replay_is_refused(tmp_path, monkeypatch, choice):
    async def scenario(session, tools):
        name = "line_get_history"
        request, preview = await _wire_preview(session, tools, name, CALLS[name], destination="chatgpt")
        chosen = {**request, "privacy_choice": choice, "mock_scope_id": preview["mock_scope_id"]}
        result = await session.call_tool(name, arguments=chosen)
        payload = _assert_envelope(result, tools[name], "mock")
        assert payload["status"] == "ok" and payload["privacy_choice"] == choice
        assert payload["destination"] == "chatgpt"
        assert any("1000" in item["content"] for item in payload["items"])
        results = [(name, result)]
        for _ in range(3):
            replay_result = await session.call_tool(name, arguments=chosen)
            replay = _assert_envelope(replay_result, tools[name], "mock")
            assert replay["status"] == "requires_confirmation" and replay["items"] == []
            assert replay["error_code"] == "scope_confirmation_required"
            assert replay["mock_scope_id"] != chosen["mock_scope_id"]
            results.append((name, replay_result))
        return results
    _wire_exchange(tmp_path, monkeypatch, mode="mock", scenario=scenario)


def test_wire_expired_scope_requires_confirmation_again(tmp_path, monkeypatch):
    async def scenario(session, tools):
        name = "line_get_history"
        request, preview = await _wire_preview(session, tools, name, CALLS[name])
        result = await session.call_tool(name, arguments={
            **request, "privacy_choice": "original", "mock_scope_id": preview["mock_scope_id"],
        })
        return [(name, result)]
    tools, results = _wire_exchange(tmp_path, monkeypatch, mode="mock", scenario=scenario, expire_privacy=True)
    payload = _assert_envelope(results[0][1], tools["line_get_history"], "mock")
    assert payload["status"] == "requires_confirmation"
    assert payload["error_code"] == "scope_confirmation_required"
    assert payload["items"] == []


@pytest.mark.parametrize("secret_field", ["key", "api_key", "token", "password", "db_path", "privacy_ready", "human_confirmation_verified"])
def test_wire_original_does_not_allow_credentials_or_live_bypass_fields(tmp_path, monkeypatch, secret_field):
    tools, results = _wire_exchange(tmp_path, monkeypatch, mode="mock", calls=[
        ("line_get_history", {**CALLS["line_get_history"], "destination": "chatgpt",
                              "privacy_choice": "original", secret_field: SENTINEL}),
    ])
    payload = _assert_envelope(results[0][1], tools["line_get_history"], "mock")
    assert payload["status"] == "error" and payload["error_code"] == "invalid_tool_request"
    assert payload["items"] == []


def test_wire_disabled_mode_cannot_be_unlocked_with_original_choice(tmp_path, monkeypatch):
    tools, results = _wire_exchange(tmp_path, monkeypatch, mode="disabled", calls=[
        ("line_get_history", {**CALLS["line_get_history"], "destination": "chatgpt",
                              "privacy_choice": "original", "mock_scope_id": "a" * 64}),
    ])
    payload = _assert_envelope(results[0][1], tools["line_get_history"], "disabled")
    assert payload["status"] == "disabled" and payload["items"] == []
    assert payload["error_code"] == "live_data_not_implemented"



async def _wait_for_synthetic_read(marker_dir):
    async def wait():
        while not (marker_dir / "started").exists():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), timeout=5)


def test_wire_preview_during_active_read_never_revives_old_generation(tmp_path, monkeypatch):
    marker_dir = tmp_path / "synthetic-read-markers"
    marker_dir.mkdir()

    async def scenario(session, tools):
        name = "line_get_history"
        request, preview = await _wire_preview(session, tools, name, CALLS[name])
        old = {**request, "privacy_choice": "original", "mock_scope_id": preview["mock_scope_id"]}
        first_call = asyncio.create_task(session.call_tool(name, arguments=old))
        try:
            await _wait_for_synthetic_read(marker_dir)
            _, fresh = await _wire_preview(session, tools, name, CALLS[name])
            assert fresh["mock_scope_id"] != old["mock_scope_id"]
            assert fresh["scope_fingerprint"] == preview["scope_fingerprint"]
            (marker_dir / "release").touch()
            first = await first_call
            assert _assert_envelope(first, tools[name], "mock")["status"] == "ok"
            results = [(name, first)]
            for _ in range(3):
                rejected = await session.call_tool(name, arguments=old)
                payload = _assert_envelope(rejected, tools[name], "mock")
                assert payload["status"] == "requires_confirmation" and payload["items"] == []
                results.append((name, rejected))
            # A separately previewed, unexpired generation still works.
            fresh_result = await session.call_tool(name, arguments={**old, "mock_scope_id": fresh["mock_scope_id"]})
            assert _assert_envelope(fresh_result, tools[name], "mock")["status"] == "ok"
            results.append((name, fresh_result))
            return results
        finally:
            (marker_dir / "release").touch()
            await first_call
    _wire_exchange(tmp_path, monkeypatch, mode="mock", scenario=scenario, slow_read_dir=marker_dir)


def test_wire_expired_active_generation_cannot_join_but_started_read_can_finish(tmp_path, monkeypatch):
    marker_dir = tmp_path / "synthetic-read-markers"
    marker_dir.mkdir()

    async def scenario(session, tools):
        name = "line_get_history"
        request, preview = await _wire_preview(session, tools, name, CALLS[name])
        old = {**request, "privacy_choice": "deidentify", "mock_scope_id": preview["mock_scope_id"]}
        first_call = asyncio.create_task(session.call_tool(name, arguments=old))
        try:
            await _wait_for_synthetic_read(marker_dir)
            results = []
            for _ in range(3):
                rejected = await session.call_tool(name, arguments=old)
                payload = _assert_envelope(rejected, tools[name], "mock")
                assert payload["status"] == "requires_confirmation" and payload["items"] == []
                assert payload["error_code"] == "scope_confirmation_required"
                results.append((name, rejected))
            (marker_dir / "release").touch()
            first = await first_call
            assert _assert_envelope(first, tools[name], "mock")["status"] == "ok"
            return [(name, first), *results]
        finally:
            (marker_dir / "release").touch()
            await first_call
    _wire_exchange(tmp_path, monkeypatch, mode="mock", scenario=scenario,
                   slow_read_dir=marker_dir, privacy_clock=[1000.0, 1000.0, 1120.0])


def test_wire_restart_does_not_accept_previous_process_generation_after_new_preview(tmp_path, monkeypatch):
    old_request = {}

    async def first_process(session, tools):
        name = "line_get_history"
        request, preview = await _wire_preview(session, tools, name, CALLS[name])
        old_request.update({**request, "privacy_choice": "original", "mock_scope_id": preview["mock_scope_id"]})
        # Return a harmless status result to preserve complete frame validation.
        return [("line_status", await session.call_tool("line_status", arguments={}))]

    first_dir = tmp_path / "first-process"
    first_dir.mkdir()
    with monkeypatch.context() as context:
        _wire_exchange(first_dir, context, mode="mock", scenario=first_process)

    async def second_process(session, tools):
        name = "line_get_history"
        request, fresh = await _wire_preview(session, tools, name, CALLS[name])
        assert fresh["mock_scope_id"] != old_request["mock_scope_id"]
        results = []
        for _ in range(3):
            rejected = await session.call_tool(name, arguments=old_request)
            payload = _assert_envelope(rejected, tools[name], "mock")
            assert payload["status"] == "requires_confirmation" and payload["items"] == []
            results.append((name, rejected))
        valid = await session.call_tool(name, arguments={
            **request, "privacy_choice": "original", "mock_scope_id": fresh["mock_scope_id"],
        })
        assert _assert_envelope(valid, tools[name], "mock")["status"] == "ok"
        return [*results, (name, valid)]

    second_dir = tmp_path / "second-process"
    second_dir.mkdir()
    _wire_exchange(second_dir, monkeypatch, mode="mock", scenario=second_process)



def test_wire_huge_limits_cannot_bypass_small_response_cap_before_choice(tmp_path, monkeypatch):
    calls = []
    for index, (name, field) in enumerate([
        ("line_list_chats", "limit"), ("line_get_history", "limit"),
        ("line_search_messages", "limit"), ("line_get_contacts", "limit"),
        ("line_get_unread", "limit_chats"), ("line_get_unread", "per_chat_limit"),
    ]):
        request = {**CALLS[name], field: 10 ** 3500,
                   "destination": "chatgpt" if index % 2 else "unknown-host"}
        assert len(json.dumps(request, ensure_ascii=False).encode("utf-8")) < 8192
        calls.append((name, request))
    tools, results = _wire_exchange(tmp_path, monkeypatch, mode="mock", calls=calls, max_response_bytes=4096)
    for name, result in results:
        payload = _assert_envelope(result, tools[name], "mock")
        assert payload["status"] == "error" and payload["error_code"] == "invalid_request"
        assert payload["items"] == [] and payload["request_scope"] is None
        assert payload["content_complete"] is False
        assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= 4096
        assert len(result.content[0].text.encode("utf-8")) <= 4096
