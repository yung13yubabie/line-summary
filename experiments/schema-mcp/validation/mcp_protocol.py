"""TEST ONLY: bounded JSON-RPC framing and acceptance checks; no I/O or spawn."""
import json

TOOL_NAMES = frozenset({'line_list_chats', 'line_get_history', 'line_get_unread',
    'line_get_contacts', 'line_schema_counts'})

def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate protocol field')
        result[key]=value
    return result

class Frames:
    def __init__(self, frame_limit=16384, wire_limit=65536):
        self.frame_limit=frame_limit; self.wire_limit=wire_limit
        self.buffer=bytearray(); self.received=0
    def feed(self, raw):
        if type(raw) is not bytes: raise TypeError('bytes required')
        self.received+=len(raw)
        if self.received>self.wire_limit: raise ValueError('wire limit exceeded')
        self.buffer.extend(raw); values=[]
        while b'\n' in self.buffer:
            line,_,remaining=self.buffer.partition(b'\n')
            self.buffer=bytearray(remaining)
            if not line or len(line)>self.frame_limit: raise ValueError('frame limit exceeded')
            value=json.loads(line.decode('utf-8'),object_pairs_hook=_unique)
            if type(value) is not dict or value.get('jsonrpc')!='2.0':
                raise ValueError('JSON-RPC envelope required')
            values.append(value)
        if len(self.buffer)>self.frame_limit: raise ValueError('unterminated frame limit exceeded')
        return values
    def finish(self):
        if self.buffer: raise ValueError('truncated JSON-RPC at EOF')

def request(method, request_id, params=None):
    if type(method) is not str or not method or type(request_id) is not int:
        raise ValueError('fixed request fields required')
    value={'jsonrpc':'2.0','id':request_id,'method':method}
    if params is not None:value['params']=params
    raw=json.dumps(value,separators=(',',':'),ensure_ascii=True).encode()+b'\n'
    if len(raw)>16384:raise ValueError('request frame limit exceeded')
    return raw

def require_response(value, request_id):
    if (type(value) is not dict or set(value)!={'jsonrpc','id','result'}
            or value['jsonrpc']!='2.0' or type(value['id']) is not type(request_id)
            or value['id']!=request_id or type(value['result']) is not dict):
        raise ValueError('bound successful protocol response required')
    return value['result']

def require_tool_set(value):
    if type(value) is not dict or type(value.get('tools')) is not list:
        raise ValueError('tools list required')
    tools=value['tools']
    if len(tools)!=5 or {row.get('name') for row in tools}!=TOOL_NAMES:
        raise ValueError('exact five tools required')
    tool=next(row for row in tools if row['name']=='line_schema_counts')
    shape=tool.get('inputSchema',{})
    if (shape.get('type')!='object' or shape.get('properties')!={} or shape.get('required',[])!=[]
            or shape.get('additionalProperties') is not False):
        raise ValueError('schema tool must expose no configuration arguments')
    return tools

STATUSES=frozenset({'completed','disabled','busy','cancelled','deadline_exceeded','cleanup_unconfirmed',
    'failed','output_rejected','receipt_failed','spawn_error','output_limit','supervisor_error',
    'output_or_receipt_rejected','protocol_error','spawn_cleanup_unconfirmed'})
FIELDS=frozenset({'status','successful','application_ack','os_release_confirmed','retry_required'})

def require_schema_result(protocol_result):
    if type(protocol_result) is not dict or protocol_result.get('isError',False) is not False:
        raise ValueError('structured schema result required')
    value=protocol_result.get('structuredContent')
    if (type(value) is not dict or value.get('status') not in STATUSES
            or not FIELDS<=set(value)<=FIELDS|{'counts'}
            or any(type(value.get(name)) is not bool for name in FIELDS-{'status'})):
        raise ValueError('finite schema result fields required')
    if value['successful']:
        counts=value.get('counts')
        if (value['status']!='completed' or value['application_ack'] is not True
                or value['os_release_confirmed'] is not True or value['retry_required'] is not False
                or type(counts) is not dict or set(counts)!={'table_count','view_count'}
                or any(type(count) is not int or not 0<=count<2**63 for count in counts.values())):
            raise ValueError('strict schema success required')
    elif 'counts' in value or value['status']=='completed':
        raise ValueError('failed schema result cannot contain counts')
    if value['status'] in ('disabled','busy','cleanup_unconfirmed') and value['application_ack']:
        raise ValueError('no inferred application acknowledgement')
    # The text block must convey the same structured object without hidden data.
    content=protocol_result.get('content')
    if (type(content) is not list or len(content)!=1 or content[0].get('type')!='text'
            or json.loads(content[0].get('text',''),object_pairs_hook=_unique)!=value):
        raise ValueError('matching text and structured content required')
    return value
