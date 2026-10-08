"""Local synthetic-only MCP CLI. Does not connect to any provider or real LINE."""
import argparse
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]
WINDOW={'since':'2026-10-01T00:00:00+08:00','until':'2026-10-02T00:00:00+08:00'}


class SafeParser(argparse.ArgumentParser):
    def error(self,message):
        self.exit(2,'Invalid mock CLI options. Use --help.\n')


async def request(session,name,args,choice=None,destination='local-test',interactive=False):
    if name=='line_status':
        response=await session.call_tool(name,{})
        return response.structuredContent
    base={**args,'destination':destination}
    # This is explicitly a synthetic choice demonstration, not verification
    # that a host model actually asked a person or that live data is approved.
    for attempt in range(2):
        preview=(await session.call_tool(name,base)).structuredContent
        if not preview or preview.get('status')!='requires_confirmation':
            return preview
        selected=choice
        if selected is None and interactive:
            print(json.dumps(preview,ensure_ascii=False,indent=2),file=sys.stderr)
            print('Synthetic data only. Choose deidentify / original / stop: ',end='',file=sys.stderr,flush=True)
            entered=await asyncio.to_thread(sys.stdin.readline)
            selected=entered.strip() if entered.strip() in {'deidentify','original'} else None
        if selected is None or not preview.get('mock_scope_id'):
            return preview
        approved={**base,'privacy_choice':selected,'mock_scope_id':preview['mock_scope_id']}
        result=(await session.call_tool(name,approved)).structuredContent
        if result and result.get('status') in {'rate_limited','busy'} and attempt==0:
            delay=result.get('retry_after_seconds')
            if isinstance(delay,(int,float)) and 0<=delay<=60:
                await asyncio.sleep(delay)
                continue
        return result
    return result


async def run(args):
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'plugin_adapter.py'),'--mode','mock'],cwd=str(ROOT))
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=90)) as session:
            await session.initialize()
            listed=await session.list_tools()
            names={tool.name for tool in listed.tools}
            if args.command=='self-test':
                calls=[('line_status',{}),('line_list_chats',{'query':'合成','limit':20}),
                       ('line_get_history',{'chat_id':'demo-project',**WINDOW,'limit':100}),
                       ('line_search_messages',{'chat_id':'demo-project','query':'報價',**WINDOW,'limit':100}),
                       ('line_get_unread',{'limit_chats':20,'per_chat_limit':50}),
                       ('line_get_contacts',{'query':'範例','limit':20})]
                results=[]
                for name,arguments in calls:
                    payload=await request(session,name,arguments,choice='original')
                    valid=bool(payload and payload.get('status')=='ok' and payload.get('synthetic') is True
                               and payload.get('human_confirmation_verified') is False)
                    results.append({'tool':name,'passed':valid,'status':payload.get('status') if payload else 'invalid'})
                return {'verification':'local_synthetic_only','tools_discovered':sorted(names),'results':results,
                        'actual_host_tests':'NOT_RUN','real_LINE_tests':'NOT_RUN'},all(x['passed'] for x in results)
            mapping={'status':'line_status','chats':'line_list_chats','history':'line_get_history',
                     'search':'line_search_messages','unread':'line_get_unread','contacts':'line_get_contacts'}
            name=mapping[args.command]
            if name not in names:
                return {'status':'error','error_code':'tool_unavailable'},False
            fields={'status':(), 'chats':('query','limit'), 'contacts':('query','limit'),
                    'history':('chat_id','since','until','limit','cursor'),
                    'search':('chat_id','query','since','until','limit','cursor'),
                    'unread':('limit_chats','per_chat_limit','cursor')}[args.command]
            arguments={field:getattr(args,field) for field in fields}
            result=await request(session,name,arguments,getattr(args,"privacy",None),getattr(args,"destination","local-test"),getattr(args,"interactive",False))
            return result,bool(result and result.get('status') in {'ok','requires_confirmation'})


def main():
    parser=SafeParser(prog='line-summary-mock-cli',description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ['status','chats','history','search','unread','contacts','self-test']:
        command=sub.add_parser(name)
        if name in {'chats','contacts','search'}:command.add_argument('--query',default='',required=name=='search')
        if name in {'history','search'}:
            command.add_argument('--chat-id',required=True)
            command.add_argument('--since',required=True)
            command.add_argument('--until',required=True)
        if name in {'chats','contacts','history','search'}:command.add_argument('--limit',type=int,default=20)
        if name in {'history','search','unread'}:command.add_argument('--cursor',default=None)
        if name=='unread':
            command.add_argument('--limit-chats',type=int,default=20)
            command.add_argument('--per-chat-limit',type=int,default=50)
        if name not in {'status','self-test'}:
            command.add_argument('--destination',default='local-test',help='Synthetic receiver label only; no provider connection')
            command.add_argument('--privacy',choices=['deidentify','original'],default=None,
                                 help='Synthetic choice demonstration; never proves live approval or redaction')
            command.add_argument('--interactive',action='store_true',help='Ask locally for a synthetic choice; no default acceptance')
    args=parser.parse_args()
    try:
        payload,ok=asyncio.run(run(args))
        print(json.dumps(payload,ensure_ascii=False,indent=2))
        return 0 if ok else 2
    except (Exception,KeyboardInterrupt):
        print('Mock MCP invocation stopped; no private error details returned.',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
