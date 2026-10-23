"""TEST ONLY bounded raw MCP client, with a new exact suspended server owner."""
from pathlib import Path
import json,os,sys,time,subprocess,threading

ROOT=Path(__file__).resolve().parents[1]
out=Path(sys.argv[1]).resolve();mode=sys.argv[2];site=Path(sys.argv[3]).resolve()
assert not out.exists() and site.name=='site-packages' and site.parent.name=='Lib'
out.mkdir(parents=True)
sys.path[:0]=[str(ROOT),str(ROOT/'source'),str(ROOT/'validation'),str(ROOT/'tests')]
sys.path.append(str(site));sys.prefix=str(site.parent.parent)
sys.executable=str(Path(sys.prefix)/'Scripts/python.exe')
fixture={'basic':'success','cross_request':'success','timeout':'timeout_key',
         'cancel':'cancel_key','eof_active':'cancel_key','cancel_then_success':'cancel_then_success',
         'eof_idle':'success'}.get(mode,mode)
assert fixture in {'success','wrong_key','missing_wal','missing_shm','invalid_db','exception','cancel_key','timeout_key','cancel_then_success'}
EXPECTED_SERVER=[sys._base_executable,'-I','-S','-B',str(ROOT/'synthetic_stdio.py'),
                 '--site-packages',str(site),'--artifacts',str(out/'artifacts'),'--case',fixture]
client_guard={'server_spawn_calls':0,'blocked_subprocess':0,'blocked_socket':0,'blocked_source':0}
spawn_window=False
spawn_context=threading.local()
def client_audit(event,args):
    if event in ('socket.__new__','socket.connect','socket.bind'):
        client_guard['blocked_socket']+=1
        raise RuntimeError('synchronous stdio client forbids sockets')
    if event in ('os.system','os.kill'):raise RuntimeError('client forbids arbitrary system commands')
    if event=='subprocess.Popen':
        client_guard['blocked_event']='subprocess.Popen'
        client_guard['audit_shape']={'length':len(args),'argument_types':[type(value).__name__ for value in args],
            'command_match':args[1]==subprocess.list2cmdline(EXPECTED_SERVER),
            'vector_match':args[1]==EXPECTED_SERVER,'spawn_window':spawn_window}
        client_guard['blocked_subprocess']+=1
        raise RuntimeError('client uses the exact suspended native creator only')
    if event=='_winapi.CreateProcess':
        client_guard['audit_shape']={'length':len(args),'argument_types':[type(value).__name__ for value in args],
            'application_match':args[0]==EXPECTED_SERVER[0],
            'command_match':args[1]==subprocess.list2cmdline(EXPECTED_SERVER),'spawn_window':spawn_window}
        if (not spawn_window or client_guard['server_spawn_calls']!=0
                or not native_guard.audit_matches(args,(EXPECTED_SERVER[0],subprocess.list2cmdline(EXPECTED_SERVER)))):
            client_guard['blocked_subprocess']+=1
            raise RuntimeError('client permits only its one fixed suspended server')
        client_guard['server_spawn_calls']+=1
    if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
        path=Path(os.fsdecode(args[0]))
        name=path.name.casefold()
        if path.suffix.casefold() in ('.cfg','.edb') or name.endswith(('.edb-wal','.edb-shm')) or name=='settings.json':
            client_guard['blocked_source']+=1
            raise RuntimeError('client never reads settings or database files')
sys.addaudithook(client_audit)
from configured_native_owner import Backend
from mcp_protocol import Frames,request,require_response,require_tool_set,require_schema_result
import _winapi
from wire_boundary import WireBoundary
from lifecycle_trace import Trace, PHASES
from owned_cpu import sample_owner
from mcp_native_guard import NativeCreateGuard
native_guard=NativeCreateGuard(_winapi,lambda:getattr(spawn_context,'expected',None),
    lambda:getattr(spawn_context,'inherited',None))

RETAINED={}
SECRETS=(b'MCP_TRANSPORT_PRIVATE_SENTINEL',b'SYNTHETIC_PRIVATE_SENTINEL',b'1'*32,b'f'*32,
         b'provider_settings.cfg',b'provider_fixture.edb',str(out).encode(),
         json.dumps(str(out))[1:-1].encode(),str(ROOT).encode(),json.dumps(str(ROOT))[1:-1].encode())
SECRET_TAIL=max(map(len,SECRETS))-1


class Server:
    def __init__(self):
        self.trace=Trace();self.server_events=[];self.first_stream=set()
        self.backend=Backend();self.job=None;self.child=None
        self.frames=Frames();self.pending=[];self.responses={};self.stderr_bytes=0
        self.stage_lines=[];self.stderr_pending=b''
        self.boundary=WireBoundary(SECRETS);self.forced=False;self.closed=False
        self.assigned=False;self.started=False;self.deadline=time.monotonic()+50
        RETAINED['fixed-server']=self
    def open(self):
        global spawn_window
        self.trace.mark('client_open')
        self.job=self.backend.new_job(lambda value:setattr(self,'job',value));self.job.configure()
        assert self.job.policy()=={'kill_on_close':True,'active_process_limit':8}
        inherited=self.job.inherit()
        spawn_window=True
        spawn_context.expected=(EXPECTED_SERVER[0],subprocess.list2cmdline(EXPECTED_SERVER))
        spawn_context.inherited=inherited
        try:self.child=self.backend.spawn(list(EXPECTED_SERVER),(inherited,),lambda value:setattr(self,'child',value))
        finally:
            spawn_window=False;spawn_context.expected=None;spawn_context.inherited=None
        self.trace.mark('client_spawned',resumed=self.child.resumed)
        self.job.close_inherited();assert not self.child.resumed
        self.job.assign(self.child);assert self.job.member(self.child)
        self.assigned=True
        self.trace.mark('client_assigned',assigned=True)
        assert self.job.policy()=={'kill_on_close':True,'active_process_limit':8}
        self.child.resume()
        self.started=True
        self.trace.mark('client_resumed',resumed=self.child.resumed)
    def send(self,raw):
        if len(raw)>16384 or not raw.endswith(b'\n'):raise ValueError('bounded request required')
        self.child.stdin.write(raw);self.child.stdin.flush()
    def notify(self,method,params=None):
        value={'jsonrpc':'2.0','method':method}
        if params is not None:value['params']=params
        self.send(json.dumps(value,separators=(',',':')).encode()+b'\n')
    def drain(self):
        if self.child is None:return
        for label in ('stdout','stderr'):
            for _ in range(4):
                raw=self.backend.read_available(self.child,label,4096)
                if not raw:break
                if label not in self.first_stream:
                    self.first_stream.add(label);self.trace.mark('client_'+label+'_first')
                self.boundary.feed(label,raw)
                if label=='stderr':
                    self.stderr_bytes+=len(raw)
                    self.stderr_pending+=raw
                    while b'\n' in self.stderr_pending:
                        line,self.stderr_pending=self.stderr_pending.split(b'\n',1)
                        if line.startswith(b'Synthetic trace: '):
                            event=json.loads(line[len(b'Synthetic trace: '):])
                            if (type(event) is not dict or set(event)!={'phase','wall_ns','budget_clock_ns','cpu_ns','thread_ns'}
                                    or event.get('phase') not in PHASES
                                    or any(type(event[k]) is not int or event[k]<0 for k in ('wall_ns','budget_clock_ns','cpu_ns','thread_ns'))
                                    or len(self.server_events)>=64):
                                raise ValueError('Finite server lifecycle required')
                            self.server_events.append(event)
                        fields=line.decode('ascii',errors='replace').split()
                        if len(fields)==4 and fields[:2]==['Synthetic','stage:'] and fields[2] in {'runtime','owned-directory','guarded-import','tool-binding','ready'}:
                            self.stage_lines.append({'stage':fields[2],'seconds':float(fields[3])})
                    if self.stderr_bytes>16384:raise ValueError('stderr bound exceeded')
                else:
                    for value in self.frames.feed(raw):
                        if 'id' in value:
                            identifier=value['id']
                            if identifier is not None and identifier in self.responses:
                                raise ValueError('duplicate response or late result')
                            if identifier is not None:self.responses[identifier]=value
                        self.pending.append(value)
    def receive(self,identifier,seconds=2,allow_error=False):
        deadline=min(self.deadline,time.monotonic()+seconds)
        while time.monotonic()<deadline:
            self.drain()
            for index,value in enumerate(self.pending):
                if value.get('id')==identifier:
                    self.pending.pop(index)
                    if allow_error:return value
                    return require_response(value,identifier)
            if self.child.poll() is not None:raise RuntimeError('server exited before response')
            time.sleep(.005)
        raise TimeoutError('bounded MCP response wait expired')
    def exchange(self,identifier,method,params=None,**kwargs):
        if identifier==1 and method=='initialize':self.trace.mark('client_initialize_sent')
        self.send(request(method,identifier,params))
        value=self.receive(identifier,**kwargs)
        if identifier==1 and method=='initialize':self.trace.mark('client_initialize_received')
        return value
    def diagnostic(self):
        deadline=min(self.deadline,time.monotonic()+2)
        while time.monotonic()<deadline:
            self.drain()
            for value in self.pending:
                if value.get('method')=='notifications/message':
                    params=value.get('params',{})
                    if (params.get('level')=='error' and params.get('data')=='Internal Server Error'
                            and params.get('logger')=='mcp.server.exception_handler'):return
            time.sleep(.005)
        raise ValueError('fixed malformed-input diagnostic missing')
    def marker(self):
        deadline=min(self.deadline,time.monotonic()+12)
        while time.monotonic()<deadline:
            self.drain()
            markers=list((out/'artifacts').glob('*/entry/fixture_timeout_ready.json'))
            if len(markers)>1:raise ValueError('ambiguous fixture loop marker')
            if markers:
                value=json.loads(markers[0].read_text('utf-8'))
                if value=={'candidate_loop_checked':True}:return
            if self.child.poll() is not None:raise RuntimeError('server exited before injected loop')
            time.sleep(.005)
        raise TimeoutError('synthetic fault not reached')
    def close(self):
        return close_server(self)

    def sample_cpu(self):
        return sample_owner(self.child, self.job)



import mcp_protocol as protocol
from bootstrap_cases import run_case, after_close
from server_cleanup import close_server
server=Server();checks={};results=[];error=None;failure_frames=[]
try:
    checks,results=run_case(server,out,mode,protocol,checks,results)
except BaseException as failure:
    error=type(failure).__name__
    trace=failure.__traceback__
    while trace is not None:
        failure_frames.append({'file':Path(trace.tb_frame.f_code.co_filename).name,'line':trace.tb_lineno})
        trace=trace.tb_next
finally:
    try:server.close()
    except BaseException as failure:error=error or type(failure).__name__
try:checks.update(after_close(server,out,mode))
except BaseException as failure:error=error or type(failure).__name__
checks.update(client_exact_spawn=client_guard['server_spawn_calls']==1 and client_guard['blocked_subprocess']==0,
    client_no_network=client_guard['blocked_socket']==0,client_no_source_read=client_guard['blocked_source']==0,
    exact_assigned_before_resume=server.assigned,no_retained_server=not RETAINED)
checks['client_diagnostics_healthy'] = not getattr(server,'diagnostic_errors',[]) and server.trace.emit_failures == 0
startup=checks.pop('startup_seconds',None)
accepted=error is None and bool(checks) and all(value is True for value in checks.values())
receipt={'accepted':accepted,'mode':mode,'checks':checks,'error_kind':error,'results':results,
    'startup_seconds':startup,'client_lifecycle':server.trace.snapshot(),'server_lifecycle':server.server_events,
    'server_returncode':None if server.child is None else server.child.returncode,'stages':server.stage_lines,'failure_frames':failure_frames[-8:],
    'stdout_bytes':server.frames.received,'stderr_bytes':server.stderr_bytes,
    'client_guard':client_guard,'private_start_frame_sent':False,'raw_wire_persisted':False,'cleanup_errors':getattr(server,'cleanup_errors',[]),
    'diagnostic_errors':getattr(server,'diagnostic_errors',[])}
(out/'result.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
print(json.dumps({'mode':mode,'accepted':accepted,'error_kind':error,'startup_seconds':startup,
    'failed_checks':[name for name,passed in checks.items() if passed is not True],
    'results':results,'exact_cleanup':server.closed,'forced':server.forced}),flush=True)
raise SystemExit(0 if accepted else 3)
