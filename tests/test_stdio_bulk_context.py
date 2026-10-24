"""Real MCP pipes: large Chinese local context, quotas, and parent/thread scope."""
import asyncio
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from safety import json_size
from tests.test_db_reader import _make_test_db

ROOT = Path(__file__).resolve().parents[1]
COUNT = 6001
BOOTSTRAP = r'''
import sys
from types import MappingProxyType
import line_mcp_server as srv
from db_reader import DbReader
def forbidden(*args, **kwargs):
    raise RuntimeError("Synthetic bulk smoke must never access real LINE")
srv.extract_key = forbidden
mode = sys.argv[2]
policy = dict(srv._DEFAULTS, enabled=True, db_path=sys.argv[1],
    allowed_chat_ids=frozenset({'c1'}), allow_all_local_context=mode != 'denied')
if mode == 'all':
    policy.update(max_session_messages=None, max_session_bytes=None)
elif mode == 'bounded':
    policy.update(max_session_messages=1000, max_session_bytes=None)
srv._policy = MappingProxyType(policy)
srv._reader = DbReader(sys.argv[1], None, _test_mode=True)
srv.mcp.run()
'''


def content(index):
    return f'中文內容{index:04d}：' + '摘要測試' * 8


def make_bulk_db(path):
    _make_test_db(str(path))
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript('''
            ALTER TABLE _chat ADD COLUMN _unreadCount INTEGER DEFAULT 0;
            UPDATE _chat SET _unreadCount=999 WHERE _id='c1';
            DELETE FROM _message;
            CREATE INDEX chat_time ON _message (_chatId,_createdTime);
            CREATE TABLE _squareThread (
                _threadMid TEXT PRIMARY KEY, _chatMid TEXT, _messageId TEXT);
            INSERT INTO _squareThread VALUES ('thread-one','c1','m00000');
        ''')
        conn.executemany('INSERT INTO _message VALUES (?,?,?,?,?,0,NULL)', [
            (f'm{i:05d}', 'thread-one' if i == COUNT - 1 else 'c1', 'u1',
             1718600000000 + i // 2, content(i)) for i in range(COUNT)
        ])
        conn.execute("INSERT INTO _message VALUES ('secret','u2','u2',1718600000000,'不能讀取其他聊天室',0,NULL)")
        conn.commit()


async def exercise(path, mode):
    params = StdioServerParameters(
        command=sys.executable, args=['-u', '-c', BOOTSTRAP, str(path), mode],
        cwd=str(path.parent), env={'PYTHONPATH': str(ROOT), 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'},
    )
    with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as diagnostics:
        async with stdio_client(params, errlog=diagnostics) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=30)) as client:
                await client.initialize()
                schema = await client.list_tools()
                unread = next(t for t in schema.tools if t.name == 'line_get_unread')
                assert 'chat_id' in unread.inputSchema['properties']
                args = {'chat_id': 'c1', 'per_chat_limit': 500}
                first = await client.call_tool('line_get_unread', args)
                if mode == 'denied':
                    assert first.isError
                    assert first.content[0].text == 'LINE tool request failed'
                    return
                assert not first.isError
                payload = json.loads(first.content[0].text)
                items, total_bytes, pages = [], 0, 0
                while True:
                    assert payload['selection'] == 'all_local_context'
                    assert payload['sync_status'] == 'unknown'
                    assert payload['unread_boundary_verified'] is False
                    assert payload['unread_count_at_start'] == 999
                    assert payload['content_complete'] is True
                    assert len(payload['items']) <= 500
                    assert json_size(payload) <= 262144
                    items.extend(payload['items'])
                    total_bytes += json_size(payload)
                    pages += 1
                    if not payload['has_more']:
                        assert payload['next_cursor'] is None
                        break
                    assert payload['next_cursor'] and pages < 100
                    args['cursor'] = payload['next_cursor']
                    with closing(sqlite3.connect(path)) as conn:
                        conn.execute("UPDATE _chat SET _unreadCount=0 WHERE _id='c1'")
                        conn.commit()
                    result = await client.call_tool('line_get_unread', args)
                    if mode == 'bounded' and len(items) == 1000:
                        assert result.isError
                        assert result.content[0].text == 'LINE tool request failed'
                        return
                    assert not result.isError
                    payload = json.loads(result.content[0].text)
                assert mode == 'all'
                assert len(items) == COUNT and total_bytes > 2097152
                assert [m['message_id'] for m in items] == [f'm{i:05d}' for i in range(COUNT)]
                assert [m['content'] for m in items] == [content(i) for i in range(COUNT)]
                assert all(m['sender'] == '王小明' for m in items)
                assert items[-1]['thread_id'] == 'thread-one'
                assert items[-1]['thread_root_message_id'] == 'm00000'
                denied = await client.call_tool('line_get_unread', {'chat_id': 'u2'})
                assert denied.isError and denied.content[0].text == 'LINE tool request failed'


def test_stdio_reads_6001_messages_and_thread_beyond_count_and_session_defaults(tmp_path):
    path = tmp_path / '大量中文.db'
    make_bulk_db(path)
    asyncio.run(exercise(path, 'all'))


def test_stdio_still_stops_at_explicit_session_message_quota(tmp_path):
    path = tmp_path / '有限額度.db'
    make_bulk_db(path)
    asyncio.run(exercise(path, 'bounded'))


def test_stdio_all_local_context_requires_owner_opt_in(tmp_path):
    asyncio.run(exercise(tmp_path / 'never-read.db', 'denied'))
