"""Synthetic supervisor: absolute deadlines, bounded I/O, exact retained owners.
No live adapter, process enumeration, process signals or termination API.
The owned Windows job has no termination-on-close flag and is never terminated.
"""
from pathlib import Path
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid

STAGES=('identity','memory_scan','candidate_probe','reader_open','sql')
CASES=('success',)+tuple('slow_'+s for s in STAGES)+('exception','ignore_cancel','missing_ack')
RETAINED_FOR_RETRY={}
_RETAINED_LOCK=threading.RLock()
_CREATE_LOCK=threading.Lock()
MAX_QUEUE=32
MAX_FRAMES=4096
MAX_OUTPUT_BYTES=1024*1024
MAX_FRAME_BYTES=8192
MAX_EVENTS=256
MAX_RECEIPT_BYTES=128*1024


def retry_retained(run_id,cleanup_budget=1.5):
    """Close only a retained exact owner; never start/retry work or rewrite its first result."""
    if isinstance(cleanup_budget,bool) or not isinstance(cleanup_budget,(int,float)) or not math.isfinite(cleanup_budget) or cleanup_budget<=0:
        raise ValueError('finite positive cleanup budget required')
    deadline=time.monotonic()+cleanup_budget
    if not _RETAINED_LOCK.acquire(timeout=cleanup_budget):
        raise TimeoutError('retained owner registry busy')
    try:
        retained=RETAINED_FOR_RETRY[run_id]
        # Snapshot the callable before retirement can remove it from the owner.
        owner={'retry_cleanup':retained['retry_cleanup']}
    finally:_RETAINED_LOCK.release()
    return owner['retry_cleanup'](max(0,deadline-time.monotonic()))


def anchor(process):
    if os.name!='nt':return {'method':'retained_Popen','platform':'non-Windows'}
    class FILETIME(ctypes.Structure):
        _fields_=[('low',wintypes.DWORD),('high',wintypes.DWORD)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True,winmode=0x800)
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(FILETIME)]*4
    kernel.GetProcessTimes.restype=wintypes.BOOL
    times=[FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(int(process._handle),*(ctypes.byref(t) for t in times)):
        raise OSError('owned creation anchor unavailable')
    return {'method':'GetProcessTimes_on_retained_Popen_handle',
            'creation_filetime':(times[0].high<<32)|times[0].low,'pid':process.pid}


class OwnedJob:
    """Un-named newly owned handle; query only accounting for this job."""
    def __init__(self):
        self.handle=None;self.assigned=False;self.primary_handle=None;self.resume_confirmed=False
        self.primary_is_original=False;self.raw_process_handle=None;self.raw_thread_handle=None
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True,winmode=0x800)
        for name,args,result in (
            ('CreateJobObjectW',[wintypes.LPVOID,wintypes.LPCWSTR],wintypes.HANDLE),
            ('AssignProcessToJobObject',[wintypes.HANDLE,wintypes.HANDLE],wintypes.BOOL),
            ('QueryInformationJobObject',[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD,wintypes.LPVOID],wintypes.BOOL),
            ('ResumeThread',[wintypes.HANDLE],wintypes.DWORD),
            ('CloseHandle',[wintypes.HANDLE],wintypes.BOOL)):
            fn=getattr(self.kernel,name);fn.argtypes=args;fn.restype=result
        self.handle=self.kernel.CreateJobObjectW(None,None)
        if not self.handle:raise OSError('owned job unavailable')

    def create(self,argv,**kwargs):
        import _winapi
        thread_id=threading.get_ident()
        with _CREATE_LOCK:
            original=_winapi.CreateProcess
            original_close=_winapi.CloseHandle
            raw_thread=None
            def owned_close(handle):
                if threading.get_ident()==thread_id and handle==raw_thread and self.primary_is_original:
                    if self.resume_confirmed:
                        original_close(handle);self.primary_handle=None
                    # Failed resume: retain the exact primary-thread handle.
                    return
                return original_close(handle)
            def owned_create(*args):
                nonlocal raw_thread
                if threading.get_ident()!=thread_id:return original(*args)
                values=list(args);values[5]|=4  # CREATE_SUSPENDED before any child code
                hp,ht,pid,tid=original(*values)
                self.raw_process_handle=hp;self.raw_thread_handle=ht;raw_thread=ht
                # Popen closes ht. Duplicate this exact newly returned thread
                # handle so a failed ResumeThread does not erase retry ownership.
                current=_winapi.GetCurrentProcess()
                try:self.primary_handle=_winapi.DuplicateHandle(current,ht,current,0,False,2)
                except BaseException:
                    self.primary_handle=ht;self.primary_is_original=True
                self.assigned=bool(self.kernel.AssignProcessToJobObject(self.handle,hp))
                # Assignment is the ingress gate: failure retains a suspended
                # process and its exact thread owner for explicit retry/cleanup.
                if self.assigned:
                    self.resume_confirmed=self.kernel.ResumeThread(ht)!=0xffffffff
                    if self.resume_confirmed and not self.primary_is_original and self.primary_handle is not None:
                        if self.kernel.CloseHandle(self.primary_handle):self.primary_handle=None
                return hp,ht,pid,tid
            _winapi.CreateProcess=owned_create
            _winapi.CloseHandle=owned_close
            try:
                p=subprocess.Popen(argv,**kwargs);self.raw_process_handle=None;self.raw_thread_handle=None
                return p
            finally:
                _winapi.CreateProcess=original;_winapi.CloseHandle=original_close

    def active(self):
        class Accounting(ctypes.Structure):
            _fields_=[('times',ctypes.c_longlong*4),('page_faults',wintypes.DWORD),
                      ('total',wintypes.DWORD),('active',wintypes.DWORD),('terminated',wintypes.DWORD)]
        value=Accounting()
        if not self.kernel.QueryInformationJobObject(self.handle,1,ctypes.byref(value),ctypes.sizeof(value),None):
            raise OSError('owned job accounting unavailable')
        return value.active

    def close(self):
        if self.raw_process_handle is not None:
            raise OSError('unreturned owned process handle requires retry')
        if self.primary_handle is not None:
            if not self.kernel.CloseHandle(self.primary_handle):raise OSError('owned primary-thread close failed')
            self.primary_handle=None
        if self.handle is not None:
            if not self.kernel.CloseHandle(self.handle):raise OSError('owned job close failed')
            self.handle=None


def run(case,directory,stage_budget=.15,overall_budget=5.0,cancel_after=None,
        spawn_delay=0.0,spawn_budget=2.0,cleanup_budget=1.5,entrypoint='synthetic',settings_path=None,cancel_event=None):
    if cancel_event is not None and not isinstance(cancel_event,threading.Event):
        raise ValueError('local cancellation event required')
    if entrypoint not in ('synthetic','schema_synthetic','schema_configured'):raise ValueError('fixed entrypoint required')
    start_payload=b'{"op":"start"}\n'
    if entrypoint=='schema_configured':
        from source_provider import explicit_local_file
        if case!='success' or not explicit_local_file(settings_path):raise ValueError('explicit configured start required')
        start_payload=(json.dumps({'op':'start','settings_path':settings_path},separators=(',',':'))+'\n').encode()
        if len(start_payload)>4096:raise ValueError('bounded configured start required')
    elif settings_path is not None:raise ValueError('settings require configured entry')
    if case not in CASES:raise ValueError('synthetic case only')
    stages=(('source_prepare','reader_open','sql') if entrypoint=='schema_configured' else
        ('reader_open','sql') if entrypoint=='schema_synthetic' else STAGES)
    output_budget=16*1024 if entrypoint.startswith('schema_') else MAX_OUTPUT_BYTES
    for value in (stage_budget,overall_budget,spawn_budget,cleanup_budget):
        if not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            raise ValueError('finite positive budget required')
    if not math.isfinite(spawn_delay) or spawn_delay<0:raise ValueError('finite nonnegative delay required')
    if cancel_after is not None and (not math.isfinite(cancel_after) or cancel_after<0):
        raise ValueError('finite nonnegative cancellation delay required')
    directory=Path(directory).resolve();directory.mkdir(parents=True,exist_ok=True)
    run_id=uuid.uuid4().hex;receipt=directory/'completion.json'
    started=time.monotonic();overall_deadline=started+overall_budget
    state={'run_id':run_id,'adapter':'synthetic_only' if entrypoint=='synthetic' else entrypoint,'status':'running','stage':'spawn',
           'owner_creation_anchor':None,'cleanup_confirmed':None,'process_exited':False,
           'completion_receipt_written':False,'live_cleanup_status':'unchanged_unknown',
           'events':[],'stderr_bytes_discarded':0,'stdout_bytes_discarded':0,'rejected_frames':0,
           'forced_termination':False,'resource_opened':False,'retained_handle_closed':False,
           'io_threads_joined':False,'cleanup_errors':[],'receipt_errors':[],
           'descendant_ownership':'owned_job_required'}
    lock=threading.RLock()
    spawned=queue.Queue(maxsize=1)
    frames=queue.Queue(maxsize=MAX_QUEUE)  # second queue, as observed by review
    publish=queue.Queue(maxsize=1)
    cancel=threading.Event();abandoned=threading.Event();output_fault=threading.Event()
    send_cancel=threading.Event();close_control=threading.Event()
    stdout_eof=threading.Event();stderr_eof=threading.Event()
    owner={'process':None,'job':None,'threads':[],'creation_anchor':None,'closed':False}
    process=None;readers=[];old_signals={}
    cleanup_deadline=None;active_stage=None;stage_deadline=None;next_stage=0
    terminal=None;ack=False;ack_seen=False;protocol_failed=False
    publisher_stop=threading.Event();persistence_lock=threading.Lock()

    def event(label,**fields):
        with lock:
            if len(state['events'])<MAX_EVENTS:
                state['events'].append({'event':label,'utc':datetime.now(timezone.utc).isoformat(),**fields})
            else:
                state['events_dropped']=state.get('events_dropped',0)+1
                if label=='supervisor_finished':
                    state['events'][-1]={'event':label,'utc':datetime.now(timezone.utc).isoformat(),**fields}

    def error(label):
        with lock:
            if label not in state['cleanup_errors']:state['cleanup_errors'].append(label)

    def snapshot():
        with lock:
            state['elapsed']=round(time.monotonic()-started,4)
            return json.loads(json.dumps(state))

    def persist(payload,final=False):
        # Proposed payload cannot truthfully claim its own future write.
        payload['completion_receipt_written']=False
        raw=json.dumps(payload,indent=2).encode('utf-8')
        if len(raw)>MAX_RECEIPT_BYTES:raise OSError('bounded receipt exceeded')
        with persistence_lock:
            temporary=directory/('receipt-'+run_id+'.tmp')
            with temporary.open('wb') as out:
                out.write(raw);out.flush()
                if final:os.fsync(out.fileno())
            os.replace(temporary,receipt)
            if final:
                commit=directory/'completion.commit.json';tmp_commit=directory/('commit-'+run_id+'.tmp')
                data={'run_id':run_id,'payload_sha256':hashlib.sha256(raw).hexdigest(),'completion_receipt_written':True}
                with tmp_commit.open('w',encoding='utf-8') as out:
                    json.dump(data,out);out.flush();os.fsync(out.fileno())
                os.replace(tmp_commit,commit)

    def receipt_writer():
        while True:
            try:payload,final,done=publish.get(timeout=.02)
            except queue.Empty:
                if publisher_stop.is_set():break
                continue
            try:
                persist(payload,final)
                if final:
                    with lock:state['completion_receipt_written']=True
            except BaseException:
                with lock:
                    state['completion_receipt_written']=False
                    state['receipt_errors'].append('final_persistence_failed' if final else 'initial_persistence_failed')
            finally:done.set()
            # A final receipt has no successor. Exit now instead of re-entering
            # a timed queue wait while the caller is joining this exact writer.
            if final:return

    writer=threading.Thread(target=receipt_writer,name='owned-receipt-writer',daemon=True)
    owner['receipt_thread']=writer

    def submit_receipt(final):
        done=threading.Event();item=(snapshot(),final,done)
        try:publish.put_nowait(item)
        except queue.Full:
            try:old=publish.get_nowait();old[2].set()
            except queue.Empty:pass
            publish.put_nowait(item)
        return done

    def drain_stdout(p):
        total=count=0;pending=bytearray();dropping=False
        try:
            while True:
                chunk=p.stdout.read(4096)
                if not chunk:break
                total+=len(chunk)
                if total>output_budget:output_fault.set();dropping=True
                if dropping:
                    with lock:state['stdout_bytes_discarded']+=len(chunk)
                    continue
                for byte in chunk:
                    if byte==10:
                        count+=1
                        if count>MAX_FRAMES:output_fault.set();dropping=True;pending.clear();break
                        try:
                            value=json.loads(pending.decode('utf-8'));pending.clear()
                            if not isinstance(value,dict):raise ValueError()
                            frames.put_nowait(value)
                        except queue.Full:output_fault.set()
                        except (ValueError,UnicodeError):
                            pending.clear()
                            with lock:state['rejected_frames']+=1
                    else:
                        pending.append(byte)
                        if len(pending)>MAX_FRAME_BYTES:output_fault.set();dropping=True;pending.clear();break
            if pending:
                with lock:state['rejected_frames']+=1
        except BaseException:error('stdout_read_failed')
        finally:stdout_eof.set()

    def drain_stderr(p):
        try:
            while data:=p.stderr.read(4096):
                with lock:state['stderr_bytes_discarded']+=len(data)
        except BaseException:error('stderr_read_failed')
        finally:stderr_eof.set()

    def control(p,start_allowed):
        try:
            condition=cancel_event._cond if cancel_event is not None else None
            if condition is not None and not condition.acquire(timeout=max(0,overall_deadline-time.monotonic())):
                start_allowed=False
            else:
                try:
                    if cancel_event is not None and cancel_event.is_set():start_allowed=False
                    if start_allowed:
                        p.stdin.write(start_payload);p.stdin.flush();event('start_sent')
                finally:
                    if condition is not None:condition.release()
            if start_allowed:
                while not close_control.wait(.01):
                    if send_cancel.is_set():break
                if send_cancel.is_set():
                    p.stdin.write(b'{"op":"cancel"}\n');p.stdin.flush();event('cancel_sent_on_owned_pipe')
            p.stdin.close()
        except (BrokenPipeError,OSError,ValueError):
            # A peer that already exited can reject cancel; close must still succeed.
            try:
                if not p.stdin.closed:p.stdin.close()
            except BaseException:error('stdin_close_failed')
        except BaseException:error('control_thread_failed')

    def begin_cleanup():
        nonlocal cleanup_deadline
        if cleanup_deadline is None:cleanup_deadline=time.monotonic()+cleanup_budget

    def fail_protocol():
        nonlocal protocol_failed
        protocol_failed=True
        if state['status'] in ('running','completed'):state['status']='protocol_error'
        event('protocol_rejected');begin_cleanup();send_cancel.set();close_control.set()

    def parse(value):
        nonlocal active_stage,stage_deadline,next_stage,terminal,ack,ack_seen
        now=time.monotonic();label=value.get('event')
        if label=='stage_enter':
            stage=value.get('stage')
            if stage not in stages or terminal is not None:fail_protocol()
            elif stage==active_stage:state['rejected_frames']+=1  # never renew deadline
            elif active_stage is not None or next_stage>=len(stages) or stage!=stages[next_stage]:fail_protocol()
            else:
                active_stage=stage;state['stage']=stage
                # Schema reader startup keeps its existing finite session budget;
                # a short SQL fault budget must not abort a legitimate cold open.
                duration=min(25,overall_budget) if entrypoint.startswith('schema_') and stage in ('source_prepare','reader_open') else stage_budget
                stage_deadline=min(overall_deadline,now+duration)
                event('stage_enter',stage=stage)
        elif label=='stage_exit':
            if value.get('stage')!=active_stage or active_stage is None or terminal is not None:fail_protocol()
            else:
                event('stage_exit',stage=active_stage);active_stage=None;stage_deadline=None;next_stage+=1
        elif label=='error':
            kind=value.get('error_type')
            state['error_type']=kind if kind in ('ValueError','RuntimeError','KeyboardInterrupt') else 'UnexpectedChildError'
            event('child_error',error_type=state['error_type'])
        elif label=='result':
            status=value.get('status')
            if terminal is not None or status not in ('completed','failed','cancelled') or status=='completed' and active_stage is not None:fail_protocol()
            else:
                terminal=status
                if state['status']=='running':state['status']=status
                event('result_received',status=status);begin_cleanup();close_control.set()
        elif label=='cleanup_ready':begin_cleanup();close_control.set()
        elif label=='cleanup_ack':
            if ack_seen:fail_protocol()
            else:
                ack_seen=True;ack=value.get('resources_closed') is True
                state['resource_opened']=value.get('resource_opened') is True
                event('cleanup_ack',resources_closed=ack);begin_cleanup();close_control.set()
        else:state['rejected_frames']+=1

    def finish_resources(p,job,threads,deadline):
        exited=waited=handle_closed=group_empty=job_closed=False;pipes_closed=True
        if getattr(getattr(p,'_handle',None),'closed',False) and p.returncode is not None:
            waited=True
        else:
            try:p.wait(timeout=max(0,deadline-time.monotonic()));waited=True
            except subprocess.TimeoutExpired:error('child_wait_deadline')
            except BaseException:error('child_wait_failed')
        try:exited=p.poll() is not None
        except BaseException:error('child_poll_failed')
        for thread in threads:
            try:thread.join(timeout=max(0,deadline-time.monotonic()))
            except BaseException:error('io_join_failed')
        joined=all(not t.is_alive() for t in threads)
        if exited and joined:
            for stream in (p.stdin,p.stdout,p.stderr):
                try:
                    if stream is not None and not stream.closed:stream.close()
                    if stream is not None and not stream.closed:pipes_closed=False
                except BaseException:pipes_closed=False;error('pipe_close_failed')
        else:pipes_closed=False
        if exited and waited:
            try:
                if hasattr(p,'_handle'):
                    if not getattr(p._handle,'closed',False):p._handle.Close()
                    if not getattr(p._handle,'closed',False):raise OSError('owned handle close unconfirmed')
                handle_closed=True
            except BaseException:error('process_handle_close_failed')
        if job is not None and job.assigned:
            try:
                if job.handle is None:
                    group_empty=owner.get('job_empty_confirmed') is True
                    job_closed=True
                    if not group_empty:error('owned_job_empty_unconfirmed')
                else:
                    while job.active() and time.monotonic()<deadline:time.sleep(.005)
                    group_empty=job.active()==0
                    if group_empty:
                        owner['job_empty_confirmed']=True
                        job.close();job_closed=job.handle is None
            except BaseException:error('owned_job_cleanup_failed')
        else:error('descendant_ownership_unconfirmed')
        clean=exited and waited and joined and pipes_closed and handle_closed and group_empty and job_closed
        return clean,exited,joined,handle_closed,group_empty,job_closed

    def retain_owner():
        with lock:
            if 'retry_lock' not in owner:owner['retry_lock']=threading.Lock()
            with _RETAINED_LOCK:
                owner['retry_cleanup']=retry_cleanup
                RETAINED_FOR_RETRY[run_id]=owner

    def retire_owner():
        with lock:
            if owner['receipt_thread'].is_alive():return False
            with _RETAINED_LOCK:
                RETAINED_FOR_RETRY.pop(run_id,None)
                owner.pop('retry_cleanup',None)
            # A callback already captured by another caller still owns this lock.
            # Keeping the inert lock adds no cycle; removing the callback breaks it.
            return True

    def retry_cleanup(budget):
        started_retry=time.monotonic();deadline=started_retry+budget
        initial=owner.get('initial_result',{})
        result={'run_id':run_id,'initial_status':initial.get('status',state['status']),
                'initial_cleanup_confirmed':initial.get('cleanup_confirmed'),
                'resource_cleanup_confirmed':False,'application_ack':False,
                'spawn_thread_joined':False,'receipt_writer_joined':False,
                'completion_receipt_written':False,'retry_confirmed':False,'errors':[]}
        retry_lock=owner['retry_lock']
        if not retry_lock.acquire(timeout=max(0,deadline-time.monotonic())):
            result['errors'].append('owner_retry_busy');return result
        try:
            spawner=owner.get('spawn_thread')
            if spawner is not None and spawner.is_alive():
                if spawner is threading.current_thread():
                    result['errors'].append('retry_from_owner_thread');return result
                spawner.join(timeout=max(0,deadline-time.monotonic()))
            result['spawn_thread_joined']=spawner is None or not spawner.is_alive()
            if not result['spawn_thread_joined']:return result
            p=owner['process'];job=owner['job'];threads=owner['threads']
            if p is None:
                try:
                    if job is not None and job.handle is not None:job.close()
                    clean=job is None or job.handle is None
                except BaseException:
                    result['errors'].append('owned_job_retry_incomplete');clean=False
            else:
                already_closed=(getattr(p._handle,'closed',False) and p.returncode is not None
                    and job is not None and job.handle is None and owner.get('job_empty_confirmed') is True
                    and all(not t.is_alive() for t in threads)
                    and all(stream is None or stream.closed for stream in (p.stdin,p.stdout,p.stderr)))
                if already_closed:clean=True
                else:
                    send_cancel.set();close_control.set()
                    clean,*_=finish_resources(p,job,threads,deadline)
                while not frames.empty():
                    try:parse(frames.get_nowait())
                    except queue.Empty:break
            result['resource_cleanup_confirmed']=bool(clean)
            result['application_ack']=bool(ack)
            publisher=owner.get('receipt_thread')
            if publisher is not None and publisher.is_alive():
                if publisher is threading.current_thread():
                    result['errors'].append('retry_from_receipt_thread');return result
                publisher.join(timeout=max(0,deadline-time.monotonic()))
            result['receipt_writer_joined']=publisher is None or not publisher.is_alive()
            result['completion_receipt_written']=state['completion_receipt_written'] is True
            result['retry_confirmed']=bool(result['resource_cleanup_confirmed'] and result['application_ack']
                and result['receipt_writer_joined'] and result['completion_receipt_written'] and not result['errors'])
            result['historical_cleanup_errors']=list(state['cleanup_errors'])
            if result['retry_confirmed']:
                owner['closed']=True;retire_owner()
            return result
        finally:
            result['elapsed']=round(time.monotonic()-started_retry,4)
            owner['last_retry']=dict(result)
            retry_lock.release()

    def late_cleanup(p,job):
        late_threads=[]
        for fn in (drain_stdout,drain_stderr):
            thread=threading.Thread(target=fn,args=(p,),daemon=True);late_threads.append(thread);thread.start()
        thread=threading.Thread(target=control,args=(p,False),daemon=True);late_threads.append(thread);thread.start()
        owner['threads']=late_threads
        clean,exited,joined,closed,empty,job_closed=finish_resources(p,job,late_threads,time.monotonic()+max(cleanup_budget,1))
        while not frames.empty():
            try:parse(frames.get_nowait())
            except queue.Empty:break
        with lock:state.update(late_spawn_exited=exited,late_spawn_handle_closed=closed,
                              late_spawn_no_start=True,late_cleanup_confirmed=bool(clean and ack))
        event('late_spawn_finished',cleanup_confirmed=bool(clean and ack))
        with lock:state['completion_receipt_written']=False
        try:
            persist(snapshot(),True)
            with lock:state['completion_receipt_written']=True
        except BaseException:
            with lock:state['receipt_errors'].append('late_persistence_failed')
        if clean and ack:
            owner['closed']=True
            if not owner['receipt_thread'].is_alive() and state['completion_receipt_written']:
                retire_owner()

    def spawn():
        p=job=None;failure=None
        try:
            if spawn_delay:time.sleep(spawn_delay)
            if os.name!='nt':raise OSError('owned descendant scope unavailable')
            job=OwnedJob();owner['job']=job
            # The fixed lifecycle fixture uses only the stdlib. Avoid the venv
            # redirector/site startup consuming its finite no-start cleanup.
            # Engine/source entrypoints still require the complete venv.
            interpreter=sys._base_executable if entrypoint=='synthetic' else sys.executable
            flags=['-I','-S','-B'] if entrypoint=='synthetic' else ['-I','-B']
            script={'synthetic':'child.py','schema_synthetic':'schema_child.py','schema_configured':'configured_child.py'}[entrypoint]
            argv=[interpreter,*flags,str(Path(__file__).with_name(script)),
                '--case',case,'--resource',str(directory/'synthetic_resource.bin')]
            if entrypoint.startswith('schema_'):
                argv.extend(['--run-id',run_id,'--deadline',str(overall_deadline)])
            p=job.create(argv,
                         stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                         bufsize=0,creationflags=subprocess.CREATE_NO_WINDOW)
            owner['process']=p;creation=anchor(p);owner['creation_anchor']=creation
            if not job.assigned or not job.resume_confirmed:raise OSError('owned descendant startup unavailable')
        except BaseException:failure='OwnedSpawnError'
        with lock:
            if abandoned.is_set():late=True
            else:late=False;spawned.put_nowait((p,job,failure))
        if late and p is not None:late_cleanup(p,job)
        elif late and job is not None:
            try:job.close();retire_owner()
            except BaseException:error('owned_job_close_failed')

    worker=threading.Thread(target=spawn,name='owned-synthetic-spawn',daemon=True)
    owner['spawn_thread']=worker
    try:
        # Outer finally is established before the first installed signal handler.
        if threading.current_thread() is threading.main_thread():
            for sig in dict.fromkeys((signal.SIGINT,getattr(signal,'SIGBREAK',signal.SIGINT))):
                old_signals[sig]=signal.getsignal(sig);signal.signal(sig,lambda *args:cancel.set())
        writer.start();event('spawn_entered');submit_receipt(False);worker.start()
        spawn_deadline=min(overall_deadline,started+spawn_budget);spawn_expired=False
        while process is None:
            if cancel_event is not None and cancel_event.is_set():cancel.set()
            try:process,job,failure=spawned.get(timeout=.01)
            except queue.Empty:
                if time.monotonic()>=spawn_deadline or cancel.is_set():
                    state.update(status='cancelled' if cancel.is_set() else 'deadline_exceeded',timeout_stage='spawn',deadline_kind='spawn')
                    spawn_expired=True;begin_cleanup();break
                continue
            if failure:
                state.update(status='spawn_error',error_type=failure);begin_cleanup();spawn_expired=True;break
        if process is None and spawn_expired:
            while time.monotonic()<cleanup_deadline:
                try:process,job,failure=spawned.get(timeout=.01);break
                except queue.Empty:continue
            if process is None:
                with lock:
                    try:process,job,failure=spawned.get_nowait()
                    except queue.Empty:abandoned.set()
                if process is None and worker.is_alive():
                    state['status']='spawn_cleanup_unconfirmed';retain_owner()
        if process is not None:
            state['owner_creation_anchor']=owner['creation_anchor']
            for fn in (drain_stdout,drain_stderr):
                thread=threading.Thread(target=fn,args=(process,),name=fn.__name__,daemon=True)
                readers.append(thread);thread.start()
            allow_start=not spawn_expired and state['status']!='spawn_error'
            controller=threading.Thread(target=control,args=(process,allow_start),daemon=True)
            controller.start();owner['threads']=readers+[controller]
            if not allow_start:event('late_spawn_no_start')
            while True:
                now=time.monotonic()
                if cancel_event is not None and cancel_event.is_set():cancel.set()
                if cancel_after is not None and now-started>=cancel_after:cancel.set()
                if cancel.is_set() and state['status']=='running':
                    state['status']='cancelled';event('interrupt_requested');begin_cleanup();send_cancel.set();close_control.set()
                if now>=overall_deadline or stage_deadline is not None and now>=stage_deadline:
                    if state['status'] in ('running','completed'):
                        state.update(status='deadline_exceeded',timeout_stage=active_stage or state['stage'],
                                     deadline_kind='overall' if now>=overall_deadline else 'stage')
                        event('deadline_exceeded',stage=state['timeout_stage']);begin_cleanup();send_cancel.set();close_control.set()
                if output_fault.is_set():fail_protocol()
                try:parse(frames.get(timeout=.005))
                except queue.Empty:pass
                exited=process.poll() is not None
                if exited:begin_cleanup();close_control.set()
                group_empty=job is not None and job.assigned and job.active()==0
                if exited and stdout_eof.is_set() and stderr_eof.is_set() and frames.empty() and group_empty:break
                if cleanup_deadline is not None and time.monotonic()>=cleanup_deadline:break
    except BaseException:
        state['status']='supervisor_error';state['error_type']='SupervisorError'
        event('supervisor_error');begin_cleanup();send_cancel.set();close_control.set()
    finally:
        for sig,previous in old_signals.items():
            try:signal.signal(sig,previous)
            except BaseException:error('signal_restore_failed')
        if process is not None:
            begin_cleanup();close_control.set()
            clean,exited,joined,closed,empty,job_closed=finish_resources(process,owner['job'],owner['threads'],cleanup_deadline)
            while not frames.empty():
                try:parse(frames.get_nowait())
                except queue.Empty:break
                except BaseException:error('final_protocol_parse_failed')
            state.update(process_exited=exited,exit_code=process.returncode,io_threads_joined=joined,
                         retained_handle_closed=closed,owned_job_empty=empty,owned_job_closed=job_closed)
            state['cleanup_confirmed']=bool(clean and ack and not state['cleanup_errors'])
            if not state['cleanup_confirmed']:
                if clean and not ack:
                    state['primary_status']=state['status'];state['status']='cleanup_unconfirmed'
                elif state['status'] not in ('spawn_error','protocol_error','deadline_exceeded','supervisor_error','cancelled'):
                    state['status']='cleanup_unconfirmed'
                retain_owner()
            else:
                owner['closed']=True
                if state['status']=='completed' and (terminal!='completed' or process.returncode!=0 or protocol_failed):
                    state['status']='failed' if process.returncode else 'protocol_error'
                elif state['status']=='running':state['status']='protocol_error'
        elif not worker.is_alive() and owner['job'] is not None:
            try:owner['job'].close()
            except BaseException:error('owned_job_close_failed');retain_owner()
        if worker.ident is not None:worker.join(timeout=0)
        state['spawn_thread_joined']=not worker.is_alive()
        event('supervisor_finished',cleanup_confirmed=state['cleanup_confirmed'])
        if writer.ident is not None:
            done=submit_receipt(True);done.wait(timeout=min(cleanup_budget,.5));publisher_stop.set();writer.join(timeout=.05)
            if writer.is_alive():
                retain_owner();error('receipt_thread_unjoined')
        else:state['completion_receipt_written']=False
    result=snapshot()
    owner['initial_result']=json.loads(json.dumps(result))
    return result
