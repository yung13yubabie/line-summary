"""Synthetic-only service, fail-closed configuration and budget regressions."""
import asyncio
import json
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from plugin_adapter import PluginService, PluginSettings, load_settings
from safety import json_size


SINCE = "2026-10-01T00:00:00+08:00"
UNTIL = "2026-10-02T00:00:00+08:00"
HISTORY = {"chat_id": "demo-project", "since": SINCE, "until": UNTIL, "limit": 100, "cursor": None}
CALLS = {
    "line_status": {},
    "line_list_chats": {"query": "", "limit": 20, "cursor": None},
    "line_get_history": HISTORY,
    "line_search_messages": {**HISTORY, "query": "報價"},
    "line_get_unread": {"limit_chats": 20, "per_chat_limit": 50, "cursor": None},
    "line_get_contacts": {"query": "", "limit": 20, "cursor": None},
}
SENTINEL = "PRIVATE_SENTINEL_/home/user/line.db_SECRET_QUERY"


@pytest.fixture(autouse=True)
def isolate_offline_server_settings():
    """Override the legacy server autofixture: this adapter is independent."""


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def service():
    instance = PluginService(PluginSettings(mode="mock", min_interval_seconds=0.0))
    try:
        yield instance
    finally:
        instance.close()


def call(service, name="line_status", arguments=None):
    return asyncio.run(service.call(name, CALLS[name] if arguments is None else arguments))


async def demo_arguments(service, name, arguments=None, *, choice, destination="local-test"):
    """Explicitly exercise synthetic negotiation; this is never human approval."""
    request = {**(CALLS[name] if arguments is None else arguments), "destination": destination}
    preview = await service.call(name, request)
    assert preview.status == "requires_confirmation"
    assert preview.items == []
    assert preview.human_confirmation_verified is False
    assert preview.privacy_processing == "not_implemented"
    return {**request, "privacy_choice": choice, "mock_scope_id": preview.mock_scope_id}


def demo_call(service, name, arguments=None, *, choice, destination="local-test"):
    async def run():
        request = await demo_arguments(service, name, arguments, choice=choice, destination=destination)
        return await service.call(name, request)
    return asyncio.run(run())


def assert_static_error(response, code):
    data = response.model_dump()
    assert data["status"] == "error"
    assert data["error_code"] == code
    assert data["items"] == []
    assert data["has_more"] is False
    assert data["next_cursor"] is None
    assert data["content_complete"] is False
    assert SENTINEL not in json.dumps(data)
    assert "_meta" not in data


@pytest.mark.parametrize("name", CALLS)
def test_disabled_never_constructs_source_or_returns_private_data(name):
    source_factory = Mock(side_effect=AssertionError("Disabled source must never be initialized"))
    service = PluginService(source_factory=source_factory)
    response = call(service, name)
    assert response.status == "disabled"
    assert response.mode == "disabled"
    assert response.synthetic is False
    assert response.items == []
    assert response.error_code == "live_data_not_implemented"
    assert response.source_sync_at is None
    assert response.source_latest_at is None
    assert response.latest_returned_at is None
    assert service._source is None
    assert service._messages == service._bytes == 0
    source_factory.assert_not_called()
    service.close()


@pytest.mark.parametrize("bad", [
    {"mode": "real"}, {"mode": "live"}, {"privacy_ready": True},
    {"db_path": SENTINEL}, {"key": SENTINEL}, {"unknown": True},
    {"mode": "mock", "privacy_ready": True}, {"requests_per_minute": True},
    {"requests_per_minute": 0}, {"requests_per_minute": 61},
    {"min_interval_seconds": -1.0}, {"min_interval_seconds": 61.0},
    {"min_interval_seconds": float("nan")}, {"min_interval_seconds": float("inf")},
    {"max_messages_per_call": 0}, {"max_messages_per_call": 501},
    {"max_response_bytes": 4095}, {"max_response_bytes": 262145},
    {"max_session_messages": 0}, {"max_session_messages": 5001},
    {"max_session_bytes": 4095}, {"max_session_bytes": 2097153},
])
def test_settings_reject_live_bypass_unknown_and_unbounded_fields(bad, tmp_path):
    with pytest.raises(ValidationError):
        PluginSettings.model_validate(bad)
    path = tmp_path / "plugin.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(path)


def test_missing_settings_are_disabled_and_limits_are_conservative(tmp_path):
    settings = load_settings(tmp_path / "not-present.json")
    assert settings.mode == "disabled"
    assert settings.requests_per_minute == 10
    assert settings.min_interval_seconds == 2.0
    assert settings.max_messages_per_call == 100
    assert settings.max_session_messages == 5000
    assert settings.max_session_bytes == 2097152
    with pytest.raises(ValidationError):
        settings.mode = "mock"


@pytest.mark.parametrize("name", CALLS)
def test_all_mock_tools_are_synthetic_and_scoped(service, name):
    response = (call(service, name) if name == "line_status" else
                demo_call(service, name, choice="deidentify"))
    assert response.status == "ok"
    assert response.mode == "mock"
    assert response.synthetic is True
    assert response.items
    assert response.source_sync_at is None
    assert response.thread_support == "unsupported"
    assert response.human_confirmation_verified is False
    assert response.privacy_processing == "not_implemented"
    text = response.model_dump_json()
    assert "demo-denied" not in text
    assert "demo-hidden" not in text
    assert str(service._source.path) not in text
    assert '"_meta"' not in text


def test_minimum_interval_retry_after_uses_fake_monotonic_clock():
    clock = FakeClock()
    service = PluginService(PluginSettings(mode="mock"), clock=clock)
    try:
        assert call(service).status == "ok"
        clock.advance(0.125)
        blocked = call(service)
        assert blocked.status == blocked.error_code == "rate_limited"
        assert blocked.retry_after_seconds == 1.875
        assert blocked.items == []
        clock.advance(1.875)
        assert call(service).status == "ok"
    finally:
        service.close()


def test_rolling_minute_retry_after_and_boundary_use_fake_clock():
    clock = FakeClock()
    service = PluginService(PluginSettings(mode="mock", requests_per_minute=2, min_interval_seconds=0.0), clock=clock)
    try:
        assert call(service).status == "ok"
        clock.advance(0.125)
        assert call(service).status == "ok"
        blocked = call(service)
        assert blocked.status == "rate_limited"
        assert blocked.retry_after_seconds == 59.875
        clock.advance(59.875)
        assert call(service).status == "ok"
    finally:
        service.close()


@pytest.mark.parametrize(("name", "args", "code"), [
    ("line_get_history", {**HISTORY, "chat_id": SENTINEL}, "scope_denied"),
    ("line_get_history", {**HISTORY, "since": SENTINEL}, "invalid_request"),
    ("line_get_history", {**HISTORY, "since": "2026-10-01T00:00:00"}, "invalid_request"),
    ("line_get_history", {**HISTORY, "until": SINCE}, "invalid_request"),
    ("line_get_history", {**HISTORY, "until": "2026-12-01T00:00:00+08:00"}, "invalid_request"),
    ("line_get_history", {**HISTORY, "limit": 0}, "invalid_request"),
    ("line_get_history", {**HISTORY, "limit": True}, "invalid_request"),
    ("line_search_messages", {**HISTORY, "query": SENTINEL * 100}, "invalid_request"),
    ("line_list_chats", {**CALLS["line_list_chats"], "query": SENTINEL * 100}, "invalid_request"),
    ("line_get_contacts", {**CALLS["line_get_contacts"], "query": SENTINEL * 100}, "invalid_request"),
])
def test_invalid_requests_return_only_static_codes(service, name, args, code):
    assert_static_error(call(service, name, args), code)


def test_unknown_tool_is_static_and_never_echoes_name(service):
    assert_static_error(asyncio.run(service.call(SENTINEL, {})), "unknown_tool")


def test_backend_exception_does_not_leak_raw_exception_path_or_query(service, monkeypatch):
    def broken_read(*args):
        raise RuntimeError(f"database failure at {SENTINEL}; row and query are private")
    monkeypatch.setattr(service, "_read", broken_read)
    assert_static_error(call(service), "source_error")


def test_message_session_budget_is_charged_and_stops_further_calls():
    service = PluginService(PluginSettings(mode="mock", min_interval_seconds=0.0, max_session_messages=2))
    try:
        response = demo_call(service, "line_get_history", choice="deidentify")
        assert response.status == "ok"
        assert len(response.items) == service._messages == 2
        assert response.has_more
        assert_static_error(call(service), "session_budget_exhausted")
    finally:
        service.close()


def test_oversized_serialized_response_is_refused_without_rows(service, monkeypatch):
    monkeypatch.setattr(service, "_read", lambda *args: {"items": [{"text": "x" * 300000}]})
    assert_static_error(call(service), "output_budget_exhausted")
    assert service._messages == service._bytes == 0


@pytest.mark.parametrize("message_budget", [1, 2])
def test_concurrent_duplicate_results_are_each_charged(monkeypatch, message_budget):
    service = PluginService(PluginSettings(mode="mock", min_interval_seconds=0.0, max_session_messages=message_budget))
    started = threading.Event()
    release = threading.Event()
    calls = []
    def slow_read(name, args):
        calls.append(name)
        started.set()
        assert release.wait(timeout=5)
        return {"items": [{"message_id": "synthetic-only", "text": "Synthetic"}]}
    monkeypatch.setattr(service, "_read", slow_read)

    async def exercise():
        request = await demo_arguments(service, "line_get_history", choice="deidentify")
        first = asyncio.create_task(service.call("line_get_history", request))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            second = asyncio.create_task(service.call("line_get_history", request))
            for _ in range(100):
                if len(service.gate._starts) == 2:
                    break
                await asyncio.sleep(0.001)
            assert len(service.gate._starts) == 2
            release.set()
            return await asyncio.gather(first, second)
        finally:
            release.set()
    try:
        responses = asyncio.run(exercise())
        assert calls == ["line_get_history"]
        expected = ["error", "ok"] if message_budget == 1 else ["ok", "ok"]
        assert sorted(response.status for response in responses) == expected
        if message_budget == 1:
            assert_static_error(next(response for response in responses if response.status == "error"), "output_budget_exhausted")
        delivered = [response for response in responses if response.status == "ok"]
        assert service._messages == len(delivered) == message_budget
        assert service._bytes == sum(json_size(response.model_dump()) for response in delivered)
    finally:
        service.close()


def test_synthetic_temporary_database_is_removed_on_close():
    service = PluginService(PluginSettings(mode="mock"))
    path = Path(service._source.path)
    assert path.is_file()
    service.close()
    assert not path.exists()


def test_minimum_valid_byte_budget_allows_first_small_response():
    service = PluginService(PluginSettings(
        mode="mock", min_interval_seconds=0.0,
        max_response_bytes=4096, max_session_bytes=4096,
    ))
    try:
        result = call(service)
        assert result.status == "ok"
        assert 0 < service._bytes <= 4096
    finally:
        service.close()


def test_completed_results_are_not_cached(service, monkeypatch):
    read = Mock(return_value={"items": [{"synthetic": True}]})
    monkeypatch.setattr(service, "_read", read)
    assert call(service).status == "ok"
    assert call(service).status == "ok"
    assert read.call_count == 2
    assert not service.gate._pending


def test_cancelled_caller_does_not_release_running_backend_slot(service, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = []
    def slow_read(name, arguments):
        calls.append(name)
        started.set()
        assert release.wait(timeout=5)
        return {"items": [{"synthetic": True}]}
    monkeypatch.setattr(service, "_read", slow_read)

    async def exercise():
        first = asyncio.create_task(service.call("line_status", {}))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            request = await demo_arguments(service, "line_list_chats", choice="deidentify")
            busy = await service.call("line_list_chats", request)
            assert busy.status == busy.error_code == "busy"
            assert busy.retry_after_seconds > 0
            assert busy.items == []
            assert calls == ["line_status"]
            release.set()
            for _ in range(1000):
                if not service.gate._pending:
                    break
                await asyncio.sleep(0.001)
            assert not service.gate._pending
            assert (await service.call("line_status", {})).status == "ok"
            assert calls == ["line_status", "line_status"]
        finally:
            release.set()
    asyncio.run(exercise())



def test_malformed_cursor_after_synthetic_choice_is_static_error(service):
    result = demo_call(service, "line_get_history", {**HISTORY, "cursor": SENTINEL}, choice="deidentify")
    assert_static_error(result, "invalid_request")


@pytest.mark.parametrize("name", [name for name in CALLS if name != "line_status"])
def test_missing_choice_never_reads_source_or_charges_budget(service, monkeypatch, name):
    read = Mock(side_effect=AssertionError("No synthetic rows before a choice"))
    monkeypatch.setattr(service, "_read", read)
    response = call(service, name, {**CALLS[name], "destination": "chatgpt"})
    assert response.status == "requires_confirmation"
    assert response.error_code == "privacy_choice_required"
    assert response.items == [] and response.next_cursor is None
    assert response.content_complete is False
    assert response.human_confirmation_verified is False
    assert response.privacy_processing == "not_implemented"
    assert service._bytes == service._messages == 0
    assert not service.gate._starts
    read.assert_not_called()


@pytest.mark.parametrize("destination", [None, "", "unrecognized-model", SENTINEL])
@pytest.mark.parametrize("choice", [None, "deidentify", "original"])
def test_unknown_destination_never_reads_or_echoes_recipient(service, monkeypatch, destination, choice):
    read = Mock(side_effect=AssertionError("Unknown recipient must never read rows"))
    monkeypatch.setattr(service, "_read", read)
    result = call(service, "line_get_history", {**HISTORY, "destination": destination, "privacy_choice": choice})
    assert result.status == "requires_confirmation"
    assert result.error_code == "destination_required"
    assert result.destination is None and result.mock_scope_id is None
    assert result.items == [] and result.content_complete is False
    assert SENTINEL not in result.model_dump_json()
    assert not service.privacy._pending
    read.assert_not_called()


@pytest.mark.parametrize("destination", ["local-test", "chatgpt", "claude", "gemini", "grok", "deepseek", "qwen"])
@pytest.mark.parametrize("choice", ["deidentify", "original"])
def test_every_destination_requires_preview_for_either_synthetic_choice(service, destination, choice):
    request = {**HISTORY, "destination": destination, "privacy_choice": choice}
    first = call(service, "line_get_history", request)
    assert first.status == "requires_confirmation"
    assert first.error_code == "scope_confirmation_required"
    assert first.items == []
    result = call(service, "line_get_history", {**request, "mock_scope_id": first.mock_scope_id})
    assert result.status == "ok" and result.synthetic is True
    assert result.destination == destination and result.privacy_choice == choice
    assert result.human_confirmation_verified is False
    assert result.privacy_processing == "not_implemented"
    # The deidentify option demonstrates a choice, not implemented redaction.
    assert any("1000" in row["content"] for row in result.items)


@pytest.mark.parametrize("change", [
    {"chat_id": "demo-family"},
    {"since": "2026-10-01T00:01:00+08:00"},
    {"until": "2026-10-03T00:00:00+08:00"},
    {"query": "待辦"},
    {"cursor": "changed-synthetic-cursor"},
    {"limit": 1},
    {"destination": "claude"},
])
def test_changed_scope_requires_new_choice_without_source_read(service, monkeypatch, change):
    request = asyncio.run(demo_arguments(service, "line_search_messages", choice="original"))
    read = Mock(side_effect=AssertionError("Changed scope cannot reuse a choice"))
    monkeypatch.setattr(service, "_read", read)
    result = call(service, "line_search_messages", {**request, **change})
    assert result.status == "requires_confirmation"
    assert result.error_code == "scope_confirmation_required"
    assert result.mock_scope_id != request["mock_scope_id"]
    assert result.items == []
    read.assert_not_called()


def test_search_preview_never_echoes_private_query(service):
    result = call(service, "line_search_messages", {
        **CALLS["line_search_messages"], "query": SENTINEL, "destination": "chatgpt",
    })
    assert result.status == "requires_confirmation"
    assert SENTINEL not in result.model_dump_json()
    assert len(result.request_scope["query_fingerprint"]) == 64
    assert "query" not in result.request_scope


def test_mock_choice_is_consumed_and_replay_does_not_read(service, monkeypatch):
    request = asyncio.run(demo_arguments(service, "line_get_history", choice="original"))
    assert call(service, "line_get_history", request).status == "ok"
    assert not service.privacy._active and not service.privacy._pending
    read = Mock(side_effect=AssertionError("Completed choice cannot authorize another operation"))
    monkeypatch.setattr(service, "_read", read)
    for _ in range(3):
        replay = call(service, "line_get_history", request)
        assert replay.status == "requires_confirmation"
        assert replay.items == []
        assert replay.mock_scope_id != request["mock_scope_id"]
    read.assert_not_called()


def test_expired_choice_does_not_read_source():
    clock = FakeClock()
    service = PluginService(PluginSettings(mode="mock", min_interval_seconds=0.0), clock=clock)
    try:
        request = asyncio.run(demo_arguments(service, "line_get_history", choice="deidentify"))
        clock.advance(120)
        result = call(service, "line_get_history", request)
        assert result.status == "requires_confirmation" and result.items == []
        assert not service.gate._starts
    finally:
        service.close()


@pytest.mark.parametrize("name,limit_field", [
    ("line_list_chats", "limit"), ("line_get_history", "limit"),
    ("line_search_messages", "limit"), ("line_get_contacts", "limit"),
    ("line_get_unread", "limit_chats"), ("line_get_unread", "per_chat_limit"),
])
@pytest.mark.parametrize("privacy", [
    {"destination": "chatgpt"},
    {"destination": "unknown-host"},
    {"destination": "unknown-host", "privacy_choice": "original"},
    {"destination": "chatgpt", "privacy_choice": "original"},
])
def test_huge_limits_cannot_overflow_confirmation_or_error_envelopes(monkeypatch, name, limit_field, privacy):
    service = PluginService(PluginSettings(mode="mock", min_interval_seconds=0.0,
                                         max_response_bytes=4096, max_session_bytes=4096))
    read = Mock(side_effect=AssertionError("Huge limits must fail before any synthetic data read"))
    monkeypatch.setattr(service, "_read", read)
    try:
        request = {**CALLS[name], **privacy, limit_field: 10 ** 3500}
        # Stay below the wrapper's separate 8192-byte input guard.
        assert json_size(request) < 8192
        result = call(service, name, request)
        assert_static_error(result, "invalid_request")
        assert json_size(result.model_dump()) <= 4096
        assert len(result.model_dump_json().encode("utf-8")) <= 4096
        assert result.request_scope is None
        assert service._messages == service._bytes == 0
        read.assert_not_called()
    finally:
        service.close()
