"""Real MCP pipes with Chinese fixtures and the repaired page/scope contract."""
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.test_db_reader import _make_test_db

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_MARKER = "PRIVATE_CODEX_INVALID_TIME"
BOOTSTRAP = r'''
import sys
from pathlib import Path
from types import MappingProxyType
import line_mcp_server as srv
from db_reader import DbReader
def forbidden(*args, **kwargs):
    raise RuntimeError("Synthetic Codex smoke must never access live LINE")
srv.extract_key = forbidden
srv._SETTINGS_PATH = str(Path(sys.argv[1]).with_name("absent-settings.json"))
if sys.argv[2] == "synthetic":
    policy = dict(srv._DEFAULTS, enabled=True, db_path=sys.argv[1],
                  allowed_chat_ids=frozenset({"c1"}))
    srv._policy = MappingProxyType(policy)
    srv._reader = DbReader(sys.argv[1], key=None, _test_mode=True)
srv.mcp.run()
'''


async def exercise(db, *, enabled):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-u", "-c", BOOTSTRAP, str(db), "synthetic" if enabled else "disabled"],
        cwd=str(db.parent),
        env={"PYTHONPATH": str(ROOT), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as diagnostics:
        async with stdio_client(params, errlog=diagnostics) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert {t.name for t in tools.tools} == {
                    "line_list_chats", "line_get_history", "line_get_contacts", "line_get_unread"
                }
                chats = await client.call_tool("line_list_chats", {"query": "家族群"})
                if not enabled:
                    assert chats.isError
                    assert chats.content[0].text == (
                        "LINE access disabled. Configure settings.json locally, then restart the server."
                    )
                    return
                assert not chats.isError
                page = json.loads(chats.content[0].text)
                assert [(c["chat_id"], c["name"], c["type"]) for c in page["items"]] == [
                    ("c1", "家族群", "group")
                ]
                assert page["has_more"] is False and page["content_complete"] is True

                args = {
                    "chat_id": "c1", "since": "2024-06-17T00:00:00+08:00",
                    "until": "2024-06-18T00:00:00+08:00", "limit": 1,
                }
                first = await client.call_tool("line_get_history", args)
                assert not first.isError
                page = json.loads(first.content[0].text)
                assert page["items"] == [{
                    "message_id": "m1", "type": "text", "sender": "王小明",
                    "content": "大家好 https://youtu.be/xxx", "urls": ["https://youtu.be/xxx"],
                    "sent_at": "2024-06-17T12:53:20+08:00",
                }]
                assert page["has_more"] is True and page["next_cursor"]
                assert page["content_complete"] is True
                args["cursor"] = page["next_cursor"]

                second = await client.call_tool("line_get_history", args)
                assert not second.isError
                page = json.loads(second.content[0].text)
                assert page["items"][0]["message_id"] == "m2"
                assert page["items"][0]["type"] == "image"
                assert page["items"][0]["sender"] == "李小美"
                assert page["has_more"] is True and page["next_cursor"]
                args["cursor"] = page["next_cursor"]

                third = await client.call_tool("line_get_history", args)
                assert not third.isError
                page = json.loads(third.content[0].text)
                assert [(m["message_id"], m["content"], m["sent_at"]) for m in page["items"]] == [
                    ("m-last", "最後一毫秒", "2024-06-17T23:59:59.999000+08:00")
                ]
                assert page["has_more"] is False and page["next_cursor"] is None

                invalid = await client.call_tool("line_get_history", {
                    "chat_id": "c1", "since": PRIVATE_MARKER, "until": args["until"],
                })
                assert invalid.isError and invalid.content[0].text == "LINE tool request failed"
                assert PRIVATE_MARKER not in invalid.model_dump_json()
                denied = await client.call_tool("line_get_history", {
                    "chat_id": "u2", "since": args["since"], "until": args["until"],
                })
                assert denied.isError and denied.content[0].text == "LINE tool request failed"
        diagnostics.seek(0)
        assert PRIVATE_MARKER not in diagnostics.read()


def test_stdio_chinese_pages_half_open_day_and_safe_errors(tmp_path):
    db = tmp_path / "中文測試.db"
    _make_test_db(str(db))
    with sqlite3.connect(db) as conn:
        conn.executemany("INSERT INTO _message VALUES (?,?,?,?,?,0,NULL)", [
            ("m-last", "c1", "u1", 1718639999999, "最後一毫秒"),
            ("m-next", "c1", "u1", 1718640000000, "隔日不應包含"),
        ])
    asyncio.run(exercise(db, enabled=True))


def test_stdio_default_policy_denies_without_live_access(tmp_path):
    asyncio.run(exercise(tmp_path / "never-opened.db", enabled=False))
