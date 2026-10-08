"""Original four-tool stdio contract, with only an injected synthetic reader."""
import asyncio
import json
from datetime import timedelta
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]
BOOTSTRAP=r'''
import sqlite3,tempfile,os,socket
from pathlib import Path
from types import MappingProxyType
import line_mcp_server as core
import key_extractor
from db_reader import DbReader

def forbidden(*args,**kwargs):
    raise AssertionError('Live boundary is forbidden in core compatibility test')
core.extract_key=forbidden
key_extractor.extract_key=forbidden
key_extractor.find_line_pid=forbidden
key_extractor._scan_memory_regions=forbidden
socket.create_connection=forbidden
socket.socket.connect=forbidden
import db_reader
db_reader._open_encrypted=forbidden
with tempfile.TemporaryDirectory(prefix='core-synthetic-compat-') as temporary:
    path=Path(temporary)/'synthetic.sqlite'
    conn=sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE _chat (_id TEXT PRIMARY KEY,_lastUpdatedTime INTEGER,_unreadCount INTEGER);
      CREATE TABLE _contact (_mid TEXT PRIMARY KEY,_displayName TEXT,_displayNameOverridden TEXT,_type INTEGER);
      CREATE TABLE _message (_id TEXT PRIMARY KEY,_chatId TEXT,_from TEXT,_createdTime INTEGER,_text TEXT,_contentType INTEGER,_contentMetadata TEXT);
      INSERT INTO _chat VALUES ('c',1000,1);
      INSERT INTO _contact VALUES ('c','Legacy synthetic chat',NULL,0);
      INSERT INTO _contact VALUES ('u','Legacy synthetic sender',NULL,0);
      INSERT INTO _message VALUES ('m','c','u',1000,'Legacy synthetic message',0,NULL);
    """)
    conn.commit();conn.close()
    reader=DbReader(str(path),None,_test_mode=True)
    core._get_reader=lambda:reader
    policy=dict(core._DEFAULTS)
    policy.update(enabled=True,db_path='not-used',allowed_chat_ids=frozenset({'c'}),allow_contacts=True)
    core._policy=MappingProxyType(policy)
    core.mcp.run(transport='stdio')
'''


def test_original_mcp_stdio_tool_contract_and_calls_unchanged():
    async def scenario():
        parameters=StdioServerParameters(command=sys.executable,args=['-c',BOOTSTRAP],cwd=str(ROOT))
        async with stdio_client(parameters) as (read,write):
            async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=20)) as session:
                await session.initialize()
                listed=await session.list_tools()
                expected={
                    'line_list_chats':{'query','chat_type','limit','cursor'},
                    'line_get_history':{'chat_id','since','until','limit','cursor'},
                    'line_get_unread':{'limit_chats','include_official','per_chat_limit','cursor'},
                    'line_get_contacts':{'query','limit','cursor'},
                }
                assert {tool.name for tool in listed.tools}==set(expected)
                for tool in listed.tools:
                    assert set(tool.inputSchema['properties'])==expected[tool.name]
                calls=[('line_list_chats',{}),('line_get_history',{'chat_id':'c','since':'1970-01-01T00:00:00Z','until':'1970-01-02T00:00:00Z'}),
                       ('line_get_unread',{}),('line_get_contacts',{})]
                for name,args in calls:
                    output=await session.call_tool(name,args)
                    assert not output.isError
                    # The legacy entry point returns text JSON. Do not change
                    # it to the plugin's typed structured-content contract.
                    assert output.structuredContent is None
                    payload=json.loads(next(item.text for item in output.content if item.type=='text'))
                    assert payload and payload['items'] and payload['has_more'] is False
                    assert not {'privacy_choice','requires_confirmation','synthetic','human_confirmation_verified'} & payload.keys()
                    if name=='line_get_history':
                        assert payload['items'][0]['content']=='Legacy synthetic message'
                        assert 'source_ref' not in payload['items'][0]
    asyncio.run(scenario())
