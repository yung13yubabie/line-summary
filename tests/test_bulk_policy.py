"""Bulk context is an explicit owner choice; defaults and per-call caps remain."""
import json

import pytest

import line_mcp_server as server
from tests.helpers import authorize


def test_session_quotas_can_be_disabled_explicitly_without_changing_defaults(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'max_session_messages': None, 'max_session_bytes': None}), encoding='utf-8')
    monkeypatch.setattr(server, '_SETTINGS_PATH', str(path))
    policy = server._load_settings()
    assert policy['max_session_messages'] is None and policy['max_session_bytes'] is None
    assert policy['allow_all_local_context'] is False
    assert server._DEFAULTS['max_session_messages'] == 5000
    assert server._DEFAULTS['max_session_bytes'] == 2097152
    assert server._DEFAULTS['max_messages_per_call'] == 500
    assert server._DEFAULTS['max_response_bytes'] == 262144


def test_owner_can_choose_larger_finite_session_quotas(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'max_session_messages': 12000, 'max_session_bytes': 67108864}), encoding='utf-8')
    monkeypatch.setattr(server, '_SETTINGS_PATH', str(path))
    policy = server._load_settings()
    assert policy['max_session_messages'] == 12000
    assert policy['max_session_bytes'] == 67108864


@pytest.mark.parametrize('field', ['max_messages_per_call', 'max_response_bytes'])
def test_per_call_limits_cannot_be_disabled(tmp_path, monkeypatch, field):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({field: None}), encoding='utf-8')
    monkeypatch.setattr(server, '_SETTINGS_PATH', str(path))
    with pytest.raises(ValueError):
        server._load_settings()


def test_explicit_unbounded_session_still_clamps_each_call_and_counts_usage(monkeypatch):
    authorize(monkeypatch, allowed_chat_ids=frozenset({'parent'}),
              max_session_messages=None, max_session_bytes=None)
    monkeypatch.setattr(server, '_session_messages', 10001)
    monkeypatch.setattr(server, '_session_bytes', 3_000_000)
    policy = server._authorize(chat_id='parent')
    assert server._message_budget(policy, 99999) == 500
    assert server._byte_budget(policy) == 262144
    payload = {'items': [{'content': '中文內容'}]}
    assert server._deliver(payload, 1, policy) is payload
    assert server._session_messages == 10002
    assert server._session_bytes > 3_000_000


def test_context_is_denied_before_reader_unless_owner_enabled(monkeypatch):
    authorize(monkeypatch, allowed_chat_ids=frozenset({'parent'}))
    def forbidden():
        pytest.fail('A denied bulk request must not open LINE')
    monkeypatch.setattr(server, '_get_reader', forbidden)
    with pytest.raises(PermissionError, match='allow_all_local_context'):
        server.line_get_unread(chat_id='parent')


def test_context_respects_allowlist_and_fixed_dates_before_reader(monkeypatch):
    authorize(monkeypatch, allowed_chat_ids=frozenset({'parent'}),
              allow_all_local_context=True, allowed_since=0)
    def forbidden():
        pytest.fail('A denied request must not open LINE')
    monkeypatch.setattr(server, '_get_reader', forbidden)
    with pytest.raises(PermissionError, match='allowed_chat_ids'):
        server.line_get_unread(chat_id='other')
    with pytest.raises(PermissionError, match='date scope'):
        server.line_get_unread(chat_id='parent')


def test_context_routes_flat_messages_and_keeps_per_call_budget(monkeypatch):
    authorize(monkeypatch, allowed_chat_ids=frozenset({'parent'}),
              allow_all_local_context=True, max_messages_per_call=2)
    class Reader:
        def get_unread_context(self, **kwargs):
            self.arguments = kwargs
            return {'items': [{'content': '中文回覆'}, {'content': '下一則'}],
                    'has_more': True, 'next_cursor': 'opaque', 'content_complete': True}
    reader = Reader()
    monkeypatch.setattr(server, '_reader', reader)
    result = server.line_get_unread(chat_id='parent', per_chat_limit=500)
    assert result['items'][0]['content'] == '中文回覆'
    assert reader.arguments['chat_id'] == 'parent'
    assert reader.arguments['limit'] == 2
    assert reader.arguments['allowed_chat_ids'] == frozenset({'parent'})
    assert server._session_messages == 2


def test_context_flag_must_be_a_boolean(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'allow_all_local_context': 1}), encoding='utf-8')
    monkeypatch.setattr(server, '_SETTINGS_PATH', str(path))
    with pytest.raises(ValueError):
        server._load_settings()
