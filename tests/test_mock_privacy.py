"""Ephemeral, synthetic-only choice negotiation; no human consent is asserted."""
import hashlib
import json

import pytest

from mock_privacy import CHOICES, DESTINATIONS, MockPrivacyChoice


@pytest.fixture(autouse=True)
def isolate_offline_server_settings():
    """This pure negotiation unit does not import the real LINE server."""


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def gate():
    return MockPrivacyChoice(clock=Clock())


REQUEST = {
    "chat_id": "demo-project", "since": "2026-10-01T00:00:00+08:00",
    "until": "2026-10-02T00:00:00+08:00", "query": "synthetic query",
    "limit": 10, "cursor": None, "destination": "local-test",
}


def preview(gate, arguments=None, *, tool="line_search_messages"):
    accepted, details, active = gate.negotiate(tool, REQUEST if arguments is None else arguments)
    assert accepted is False and active is None
    assert details["human_confirmation_verified"] is False
    assert details["privacy_processing"] == "not_implemented"
    return details


def chosen_request(gate, *, choice="deidentify", arguments=None, tool="line_search_messages"):
    request = dict(REQUEST if arguments is None else arguments)
    details = preview(gate, request, tool=tool)
    return {**request, "privacy_choice": choice, "mock_scope_id": details["mock_scope_id"]}


@pytest.mark.parametrize("destination", sorted(DESTINATIONS))
@pytest.mark.parametrize("choice", sorted(CHOICES))
def test_all_demo_destinations_need_preview_then_allow_either_choice(gate, destination, choice):
    request = {**REQUEST, "destination": destination, "privacy_choice": choice}
    first = preview(gate, request)
    assert first["error_code"] == "scope_confirmation_required"
    accepted, details, active = gate.negotiate("line_search_messages", {**request, "mock_scope_id": first["mock_scope_id"]})
    assert accepted is True
    assert active == first["mock_scope_id"]
    assert details["destination"] == destination and details["privacy_choice"] == choice
    assert details["human_confirmation_verified"] is False
    assert details["privacy_processing"] == "not_implemented"
    assert "error_code" not in details
    gate.release(active)
    assert gate._active == gate._pending == {}


@pytest.mark.parametrize("choice", [None, "", "yes", "always-original", "credential", True, [], {}])
def test_missing_or_unrecognized_choice_cannot_release_rows(gate, choice):
    request = chosen_request(gate)
    details = preview(gate, {**request, "privacy_choice": choice})
    assert details["error_code"] == "privacy_choice_required"
    assert details["privacy_choice"] is None


@pytest.mark.parametrize("destination", [None, "", "other", "https://chatgpt.com", "CHATGPT", [], {}, True])
def test_unknown_recipient_never_gets_scope_id_or_pending_entry(gate, destination):
    details = preview(gate, {**REQUEST, "destination": destination, "privacy_choice": "original"})
    assert details["error_code"] == "destination_required"
    assert details["destination"] is None and details["mock_scope_id"] is None
    assert details["request_scope"]["destination"] is None
    assert gate._pending == gate._active == {}


def test_scope_fingerprint_is_nonsecret_metadata_and_generation_id_is_fresh(gate):
    data = {key: value for key, value in REQUEST.items() if key != "destination"}
    body = json.dumps(["line_search_messages", "local-test", data], sort_keys=True, ensure_ascii=True)
    predictable_fingerprint = hashlib.sha256(body.encode()).hexdigest()
    # The deterministic scope digest is metadata, not a generation's ID.
    first = preview(gate, {**REQUEST, "mock_scope_id": predictable_fingerprint, "privacy_choice": "original"})
    second = preview(gate, REQUEST)
    assert first["scope_fingerprint"] == second["scope_fingerprint"] == predictable_fingerprint
    assert first["mock_scope_id"] != predictable_fingerprint
    assert second["mock_scope_id"] != first["mock_scope_id"]
    assert first["error_code"] == "scope_confirmation_required"


@pytest.mark.parametrize("change", [
    {"chat_id": "demo-family"}, {"since": "2026-10-01T00:01:00+08:00"},
    {"until": "2026-10-03T00:00:00+08:00"}, {"query": "other synthetic query"},
    {"limit": 11}, {"limit_chats": 1}, {"per_chat_limit": 1},
    {"cursor": "synthetic-next-page"}, {"destination": "claude"},
])
def test_changed_scope_cannot_reuse_preview(gate, change):
    request = chosen_request(gate, choice="original")
    details = preview(gate, {**request, **change})
    assert details["error_code"] == "scope_confirmation_required"
    assert details["mock_scope_id"] != request["mock_scope_id"]
    assert gate._active == {}


def test_different_operation_cannot_reuse_preview(gate):
    request = chosen_request(gate)
    details = preview(gate, request, tool="line_get_history")
    assert details["error_code"] == "scope_confirmation_required"
    assert details["mock_scope_id"] != request["mock_scope_id"]


def test_preview_hides_search_text_and_cursor(gate):
    secret = "SYNTHETIC_PRIVATE_QUERY_AND_CURSOR"
    details = preview(gate, {**REQUEST, "query": secret, "cursor": secret})
    assert secret not in json.dumps(details)
    assert details["request_scope"]["query_fingerprint"] == hashlib.sha256(secret.encode()).hexdigest()
    assert details["request_scope"]["continuation"] is True


@pytest.mark.parametrize("age,accepted_expected", [(119.999, True), (120, False), (120.001, False)])
def test_pending_scope_expires_at_exact_boundary(gate, age, accepted_expected):
    request = chosen_request(gate)
    gate.clock.now += age
    accepted, details, active = gate.negotiate("line_search_messages", request)
    assert accepted is accepted_expected
    if accepted_expected:
        gate.release(active)
    else:
        assert details["error_code"] == "scope_confirmation_required" and active is None


def test_pending_entries_are_bounded_and_oldest_cannot_be_consumed(gate):
    first = chosen_request(gate)
    for number in range(32):
        preview(gate, {**REQUEST, "query": "synthetic query " + str(number)})
        assert len(gate._pending) <= 32
    assert first["mock_scope_id"] not in gate._pending
    details = preview(gate, first)
    assert details["error_code"] == "scope_confirmation_required"
    assert len(gate._pending) == 32


def test_identical_concurrent_scope_can_coalesce_then_completed_replay_is_refused(gate):
    request = chosen_request(gate)
    first, _, scope = gate.negotiate("line_search_messages", request)
    second, _, same_scope = gate.negotiate("line_search_messages", request)
    assert first is second is True and scope == same_scope
    assert gate._pending == {}
    gate.release(scope)
    assert scope in gate._active
    gate.release(scope)
    assert gate._active == {}
    for _ in range(3):
        replay = preview(gate, request)
        assert replay["error_code"] == "scope_confirmation_required"
        assert replay["mock_scope_id"] != request["mock_scope_id"]


def test_concurrent_different_choice_cannot_piggyback_on_active_scope(gate):
    request = chosen_request(gate, choice="deidentify")
    accepted, _, scope = gate.negotiate("line_search_messages", request)
    assert accepted is True
    details = preview(gate, {**request, "privacy_choice": "original"})
    assert details["error_code"] == "scope_confirmation_required"
    gate.release(scope)


def test_pending_scope_is_not_persisted_or_shared_between_instances(gate):
    request = chosen_request(gate, choice="original")
    other = MockPrivacyChoice(clock=Clock())
    fresh = chosen_request(other, choice="original")
    assert fresh["mock_scope_id"] != request["mock_scope_id"]
    for _ in range(3):
        details = preview(other, request)
        assert details["error_code"] == "scope_confirmation_required"
    assert other.negotiate("line_search_messages", fresh)[0] is True
    gate.release("unrecognized-fingerprint")
    assert gate._active == {}



def test_preview_during_active_read_cannot_rearm_its_completed_generation(gate):
    request = chosen_request(gate, choice="original")
    accepted, _, active = gate.negotiate("line_search_messages", request)
    assert accepted is True
    fresh = chosen_request(gate, choice="original")
    assert fresh["mock_scope_id"] != request["mock_scope_id"]
    gate.release(active)
    for _ in range(3):
        details = preview(gate, request)
        assert details["error_code"] == "scope_confirmation_required"
    accepted, _, active = gate.negotiate("line_search_messages", fresh)
    assert accepted is True
    gate.release(active)


@pytest.mark.parametrize("age", [120, 120.001, 240])
def test_expired_active_generation_cannot_admit_late_joiners(gate, age):
    request = chosen_request(gate)
    accepted, _, active = gate.negotiate("line_search_messages", request)
    assert accepted is True
    gate.clock.now += age
    for _ in range(3):
        details = preview(gate, request)
        assert details["error_code"] == "scope_confirmation_required"
        assert details["mock_scope_id"] != request["mock_scope_id"]
    # The operation already in progress can finish and release its own slot.
    gate.release(active)
    assert not gate._active
