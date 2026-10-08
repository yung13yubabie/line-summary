from types import MappingProxyType
import line_mcp_server as srv


def authorize(monkeypatch, **overrides):
    policy = srv._load_settings()
    policy.update(enabled=True, db_path='synthetic-only.edb',
                  allowed_chat_ids=frozenset({'c','c1','g1','g2','chatX'}), allow_contacts=True)
    policy.update(overrides)
    monkeypatch.setattr(srv, '_policy', MappingProxyType(policy))
    return policy


def result(items=None):
    return {'items': items or [], 'has_more': False, 'next_cursor': None, 'content_complete': True}
