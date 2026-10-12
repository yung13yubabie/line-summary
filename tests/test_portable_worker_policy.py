"""Actual worker policy functions; native Guard and run() are explicitly forbidden."""
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import apsw
import pytest


@pytest.fixture
def worker(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("portable policy tests must not use native storage or worker runtime")

    stub = ModuleType("readonly_guard")
    stub.Guard = forbidden
    stub.SourceShmUnavailable = type("ForbiddenSourceShm", (Exception,), {})
    monkeypatch.setitem(sys.modules, "readonly_guard", stub)
    monkeypatch.setattr(sys, "path", list(sys.path))
    path = Path(__file__).resolve().parents[1] / "readonly_worker.py"
    spec = importlib.util.spec_from_file_location("portable_worker_policy_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "run", forbidden)
    monkeypatch.setattr(module, "Cancellation", forbidden)
    monkeypatch.setattr(module, "apsw", SimpleNamespace(**vars(apsw)))
    monkeypatch.setattr(module.apsw, "Connection", forbidden)
    monkeypatch.setattr(module, "sys", SimpleNamespace(
        stdin=SimpleNamespace(buffer=io.BytesIO()), stdout=SimpleNamespace(buffer=io.BytesIO())))
    return module


@pytest.mark.parametrize("sql,allowed", [
    ("SELECT 1", True), ("select ?;", True), ("PRAGMA table_info(_message);", True),
    ("PRAGMA journal_mode=WAL", False), ("PRAGMA table_info(other)", False),
    ("UPDATE _message SET value=1", False), ("SELECT 1; DELETE FROM _message", False),
    ("SELECT " + "x" * 8193, False), (None, False),
])
def test_sql_gate(worker, sql, allowed):
    assert worker.permitted_sql(sql) is allowed


@pytest.mark.parametrize("action,arg1,arg2,allowed", [
    (apsw.SQLITE_SELECT, None, None, True), (apsw.SQLITE_READ, "_message", "value", True),
    (apsw.SQLITE_FUNCTION, None, "count", True), (apsw.SQLITE_FUNCTION, None, "LoAd_Extension", False),
    (apsw.SQLITE_PRAGMA, "table_info", "_message", True),
    (apsw.SQLITE_PRAGMA, "journal_mode", "WAL", False),
    (apsw.SQLITE_DELETE, "_message", None, False), (apsw.SQLITE_ATTACH, "other.db", None, False),
])
def test_authorizer_gate(worker, action, arg1, arg2, allowed):
    assert worker.authorizer(action, arg1, arg2, "main", None) == (
        apsw.SQLITE_OK if allowed else apsw.SQLITE_DENY)


def test_binary_codec_and_unsupported_values(worker):
    values = [b"\x00\xff", None, "text", 42, 1.5, True]
    assert worker.decode_value(worker.encode_value(values)) == values
    for value in ({"unexpected": 1}, {"__binary__": 4}, object()):
        with pytest.raises(worker.ProtocolError):
            worker.decode_value(value)
    with pytest.raises(ValueError):
        worker.decode_value({"__binary__": "!"})
    with pytest.raises(worker.ProtocolError):
        worker.encode_value(object())


@pytest.mark.parametrize("data", [b"{}", b"[]\n",
    b'{"data":"' + b"x" * (2 * 16 * 1024) + b'"}\n', b"not-json\n"],
    ids=["unterminated", "nonobject", "oversized-valid-object", "invalid-json"])
def test_request_refuses_nonframed_oversized_or_nonobject(worker, data):
    worker.sys.stdin.buffer = io.BytesIO(data)
    with pytest.raises((worker.ProtocolError, json.JSONDecodeError)):
        worker.read_request()
    assert worker.sys.stdin.buffer.tell() <= worker.MAX_REQUEST + 1
    if len(data) > worker.MAX_REQUEST:
        assert worker.sys.stdin.buffer.tell() == worker.MAX_REQUEST + 1


def test_request_reads_one_frame_and_reply_caps_output(worker):
    worker.sys.stdin.buffer = io.BytesIO(b'{"op":"query"}\n{"op":"close"}\n')
    assert worker.read_request() == {"op": "query"}
    assert worker.read_request() == {"op": "close"}
    assert worker.read_request() is None
    worker.reply({"ok": True, "data": "synthetic-private-sentinel" + "x" * worker.MAX_REPLY})
    assert worker.sys.stdout.buffer.getvalue() == b'{"ok":false,"code":"reply_too_large"}\n'
