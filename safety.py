"""Bounded JSON pages and process-local, scope-bound continuation tokens."""
import base64
import hashlib
import hmac
import json
import secrets


def json_size(value):
    # Includes escaping; budgets apply to this JSON payload, not MCP framing.
    return len(json.dumps(value, ensure_ascii=True).encode("utf-8"))


class CursorCodec:
    def __init__(self):
        self._secret = secrets.token_bytes(32)

    def encode(self, scope, position):
        digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
        body = json.dumps([digest, position], separators=(",", ":"), sort_keys=True).encode()
        mac = hmac.new(self._secret, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac + body).decode()

    def decode(self, token, scope):
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError("Invalid cursor")
        try:
            raw = base64.b64decode(token, altchars=b"-_", validate=True)
            mac, body = raw[:32], raw[32:]
            if not hmac.compare_digest(mac, hmac.new(self._secret, body, hashlib.sha256).digest()):
                raise ValueError()
            stored_scope, position = json.loads(body)
            digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
            if stored_scope != digest:
                raise ValueError()
            return position
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("Invalid cursor, changed query, or restarted server") from None


def page(items, positions, scope, codec, more, max_bytes, **metadata):
    """Never silently skip an oversize item; an empty blocked page cannot advance."""
    result = {"items": list(items), "has_more": bool(more), "next_cursor": None,
              "content_complete": True, **metadata}
    def refresh():
        result["next_cursor"] = (codec.encode(scope, positions[len(result["items"]) - 1])
                                 if result["has_more"] and result["items"] else None)
    refresh()
    while result["items"] and json_size(result) > max_bytes:
        result["items"].pop()
        result["has_more"] = True
        refresh()
    if not result["items"] and items:
        result.update(content_complete=False, blocked_reason="item_exceeds_byte_budget")
    if json_size(result) > max_bytes:
        raise ValueError("Response byte budget too small for page metadata")
    return result
