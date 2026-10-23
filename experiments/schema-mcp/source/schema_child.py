"""Protected schema worker. Real read-only engine; synthetic source factory only."""
import argparse,hashlib,json,os,sys,threading,time
from pathlib import Path

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--case',required=True);parser.add_argument('--resource',required=True)
    parser.add_argument('--run-id',required=True);parser.add_argument('--deadline',type=float,required=True)
    args=parser.parse_args();directory=Path(args.resource).resolve().parent
    # No engine, fixture, reader, source, or secret work until the owned start gate.
    if sys.stdin.buffer.readline(4096)!=b'{"op":"start"}\n':
        print('{"event":"cleanup_ack","resource_opened":false,"resources_closed":true}',flush=True)
        return 3
    protocol=os.dup(1)
    with open(os.devnull,'wb') as sink:
        os.dup2(sink.fileno(),1);os.dup2(sink.fileno(),2)
    def emit(value):
        raw=json.dumps(value,separators=(',',':')).encode()+b'\n'
        if len(raw)>8192:raise ValueError('bounded protocol')
        os.write(protocol,raw)
    def audit(event,values):
        if event in ('socket.connect','socket.bind','os.system','os.kill'):raise RuntimeError('offline schema verification')
        if event in ('open','os.listdir','os.scandir') and values and isinstance(values[0],(str,bytes)):
            value=os.fsdecode(values[0]).replace('/','\\').casefold()
            if '\\line\\data' in value or Path(value).name=='settings.json':
                raise RuntimeError('private source denied in synthetic entry')
    sys.addaudithook(audit)
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from extraction_budget import ExtractionBudget,ExtractionStopped
    from schema_probe import SchemaProbe,error_kind
    import ctypes as C
    from ctypes import wintypes as W
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=W.HANDLE
    kernel.IsProcessInJob.argtypes=[W.HANDLE,W.HANDLE,C.POINTER(W.BOOL)]
    kernel.IsProcessInJob.restype=W.BOOL
    member=W.BOOL()
    if not kernel.IsProcessInJob(kernel.GetCurrentProcess(),None,C.byref(member)) or not member.value:
        raise RuntimeError('owned job required before source work')
    (directory/'schema_backend_started.json').write_text(json.dumps(
        {'run_id':args.run_id,'after_start':True,'in_job':True,'synthetic_only':True}),encoding='utf-8')
    probe=SchemaProbe();stop=threading.Event();done=threading.Event();controller_errors=[]
    def control():
        try:
            while sys.stdin.buffer.readline(4096):
                if not done.is_set():
                    stop.set()
                    try:probe.cancel()
                    except BaseException:controller_errors.append('owned_cancel_failed')
            if not done.is_set():
                stop.set()
                try:probe.cancel()
                except BaseException:controller_errors.append('owned_cancel_failed')
        except BaseException:controller_errors.append('owned_control_failed');stop.set()
    controller=threading.Thread(target=control,name='schema-control',daemon=False)
    controller.start()
    writer=None;writer_closed=False;before=None;stable=False;status='failed';counts=None;kind=None
    source=directory/'synthetic_schema.edb'
    def digests():
        return {suffix:hashlib.sha256(Path(str(source)+suffix).read_bytes()).hexdigest()
                for suffix in ('','-wal','-shm')}
    entered=False
    def stage(name,event):
        nonlocal entered
        if name=='reader_open' and event=='enter':
            if entered:return
            entered=True
        emit({'event':'stage_'+event,'stage':name})
        if (name,event)==('sql','enter'):
            if args.case in ('slow_sql','ignore_cancel'):
                stop.wait(10)
            if args.case=='exception':
                os.write(1,b'SCHEMA_QA_PRIVATE_SENTINEL');os.write(2,b'SCHEMA_QA_PRIVATE_SENTINEL')
                emit({'event':'error','error_type':'ValueError','detail':'SCHEMA_QA_PRIVATE_SENTINEL'})
                raise ValueError('SCHEMA_QA_PRIVATE_SENTINEL')
    try:
        remaining=args.deadline-time.monotonic()
        if remaining<=0:raise ExtractionStopped('deadline','reader')
        budget=ExtractionBudget(remaining,stop.is_set)
        budget.deadline=min(budget.deadline,args.deadline)
        budget.check('reader');stage('reader_open','enter')
        import apsw
        writer=apsw.Connection(str(source))
        writer.execute("PRAGMA cipher='aes128cbc'")
        writer.execute("PRAGMA key='" + '1'*32 + "'")
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE synthetic_first(value TEXT)")
        writer.execute("INSERT INTO synthetic_first VALUES('SCHEMA_QA_PRIVATE_SENTINEL')")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # Second table and view exist only in hot WAL, not the base DB.
        writer.execute("CREATE TABLE synthetic_second(value INTEGER)")
        writer.execute("CREATE VIEW synthetic_view AS SELECT value FROM synthetic_second")
        writer.execute("INSERT INTO synthetic_second VALUES(23)")
        before=digests()
        counts=probe.run(str(source),'1'*32,budget,stage=stage)
        stable=digests()==before
        if not stable:raise RuntimeError('synthetic source changed')
        value={'run_id':args.run_id,'counts':counts,'application_ack':probe.state['application_ack'],
               'release_evidence':probe.state['release_evidence'],'source_bytes_unchanged':stable}
        raw=json.dumps(value,separators=(',',':')).encode()
        with (directory/'schema_result.json').open('wb') as handle:
            handle.write(raw);handle.flush();os.fsync(handle.fileno())
        commit={'run_id':args.run_id,'payload_sha256':hashlib.sha256(raw).hexdigest()}
        with (directory/'schema_result.commit.json').open('w',encoding='utf-8') as handle:
            json.dump(commit,handle);handle.flush();os.fsync(handle.fileno())
        status='completed'
    except BaseException as error:
        kind=error_kind(error)
        status='cancelled' if stop.is_set() or isinstance(error,ExtractionStopped) else 'failed'
        counts=None
    finally:
        if before is not None:
            try:stable=digests()==before
            except BaseException:stable=False
        if writer is not None:
            try:writer.close();writer_closed=True
            except BaseException:writer_closed=False
        else:writer_closed=True
        done.set()
        emit({'event':'result','status':status})
        emit({'event':'cleanup_ready'})
        controller.join(timeout=1)
        acknowledged=(probe.state['application_ack'] is True and writer_closed
                      and not controller.is_alive() and not controller_errors)
        observation={'run_id':args.run_id,'status':status,'error_kind':kind,
                     'probe':probe.state,'source_bytes_unchanged':stable,'fixture_writer_closed':writer_closed,
                     'control_joined':not controller.is_alive(),'control_errors':controller_errors}
        (directory/'schema_observation.json').write_text(json.dumps(observation,indent=2),encoding='utf-8')
        if args.case!='missing_ack':
            emit({'event':'cleanup_ack','resource_opened':probe.state['resource_opened'],
                  'resources_closed':acknowledged})
        os.close(protocol)
    return 0 if status in ('completed','cancelled') and acknowledged else 3

if __name__=='__main__':raise SystemExit(main())
