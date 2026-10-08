"""Synthetic regression: old local rows cannot prove which unread bodies synced."""
import sqlite3
from db_reader import DbReader


def _make_unread_db(path):
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE _chat (_id TEXT PRIMARY KEY, _lastUpdatedTime INTEGER, _unreadCount INTEGER, _firstUnreadId TEXT);
        CREATE TABLE _groupChat (_chatMid TEXT PRIMARY KEY, _chatName TEXT);
        CREATE TABLE _contact (_mid TEXT PRIMARY KEY, _displayName TEXT, _displayNameOverridden TEXT, _type INTEGER);
        CREATE TABLE _message (_id TEXT PRIMARY KEY, _chatId TEXT, _from TEXT, _createdTime INTEGER, _text TEXT, _contentType INTEGER, _contentMetadata TEXT);
        INSERT INTO _contact VALUES ('u1','王小明',NULL,0);
        INSERT INTO _contact VALUES ('off1','官方',NULL,16);
    ''')
    for cid, n in [('c1',3), ('g2',5), ('c2',2), ('c4',1), ('off1',4), ('c3',0)]:
        conn.execute('INSERT INTO _chat VALUES (?,4000,?,NULL)', (cid,n))
    for cid, count in [('c1',4), ('g2',2), ('c4',10), ('off1',2)]:
        for i in range(count):
            conn.execute('INSERT INTO _message VALUES (?,?,?, ?,?,0,NULL)',
                         (f'{cid}-{i}',cid,'u1',1000+i,f'old-{i}'))
    conn.commit(); conn.close()


def _reader(tmp_path):
    db = str(tmp_path / 'unread.db'); _make_unread_db(db)
    return DbReader(db, None, _test_mode=True)


def test_unread_lists_only_unread_and_excludes_official(tmp_path):
    assert {x['chat_id'] for x in _reader(tmp_path).get_unread()['items']} == {'c1','g2','c2','c4'}


def test_old_local_history_never_proves_unread_or_sync(tmp_path):
    chats = _reader(tmp_path).get_unread()['items']
    for chat in chats:
        assert chat['sync_status'] == 'unknown'
        assert chat['selection'] == 'latest_local_approximation'
        assert chat['unread_boundary_verified'] is False
        assert not {'fully_synced','missing_count','available_count'} & chat.keys()
        assert chat['returned_count'] == len(chat['messages']) <= chat['unread_count']
    c1 = next(x for x in chats if x['chat_id'] == 'c1')
    assert [m['content'] for m in c1['messages']] == ['old-1','old-2','old-3']


def test_empty_local_rows_still_report_unknown(tmp_path):
    c2 = next(x for x in _reader(tmp_path).get_unread()['items'] if x['chat_id']=='c2')
    assert c2['unread_count'] == 2 and c2['returned_count'] == 0
    assert c2['sync_status'] == 'unknown' and c2['messages'] == []


def test_unread_include_official_when_requested(tmp_path):
    assert 'off1' in {x['chat_id'] for x in _reader(tmp_path).get_unread(include_official=True)['items']}


def test_unread_limits_global_budget_and_chat_cursor(tmp_path):
    reader = _reader(tmp_path)
    first = reader.get_unread(total_message_limit=2)
    assert sum(len(x['messages']) for x in first['items']) == 2
    assert first['has_more'] and first['next_cursor']
    assert first['items'][0]['messages_limited']
    second = reader.get_unread(total_message_limit=2, cursor=first['next_cursor'])
    assert not set(x['chat_id'] for x in first['items']) & set(x['chat_id'] for x in second['items'])


def test_unread_allowlist_applies_before_message_query(tmp_path):
    items = _reader(tmp_path).get_unread(allowed_chat_ids=frozenset({'g2'}))['items']
    assert len(items)==1 and items[0]['chat_id']=='g2'
