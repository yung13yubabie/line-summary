"""Fixed configured-source schema worker. Private input arrives only on its start pipe."""
import argparse,json,os,re,sys,threading,time,hashlib
from pathlib import Path,PureWindowsPath

def explicit_local_file(value,suffix=None):
    """Pure lexical policy shared by start ingress and the configured provider."""
    if (type(value) is not str or not value or len(value)>2048
            or any(ord(char)<32 for char in value)):
        return False
    path=PureWindowsPath(value)
    if (not path.is_absolute() or re.fullmatch(r'[a-zA-Z]:',path.drive) is None
            or '..' in path.parts or any(':' in part or part.endswith((' ','.'))
                or any(char in part for char in '<>"|?*')
                or PureWindowsPath(part).is_reserved() for part in path.parts[1:])):
        return False
    return suffix is None or path.suffix.casefold()==suffix

def _unique_fields(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError('duplicate configured start field')
        value[key]=item
    return value

def read_start(stream):
    raw=stream.readline(4097)
    if not raw or len(raw)>4096 or not raw.endswith(b'\n'):
        raise ValueError('bounded configured start required')
    try:value=json.loads(raw,object_pairs_hook=_unique_fields)
    except (ValueError,UnicodeError):raise ValueError('configured start rejected') from None
    if (not isinstance(value,dict) or set(value)!={'op','settings_path'} or value['op']!='start'
            or type(value['settings_path']) is not str):
        raise ValueError('configured start rejected')
    path=value['settings_path']
    if not explicit_local_file(path):
        raise ValueError('configured start rejected')
    return path

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--case',required=True);parser.add_argument('--resource',required=True)
    parser.add_argument('--run-id',required=True);parser.add_argument('--deadline',type=float,required=True)
    args=parser.parse_args();directory=Path(args.resource).resolve().parent
    try:settings_path=read_start(sys.stdin.buffer)
    except ValueError:
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
        if event in ('socket.connect','socket.bind','os.system','os.kill'):
            raise RuntimeError('offline configured schema')
    sys.addaudithook(audit)
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from schema_entry import _in_owned_job
    from schema_probe import SchemaProbe,error_kind
    from source_provider import ConfiguredSourceProvider,_kind
    from extraction_budget import ExtractionBudget,ExtractionStopped
    if args.case!='success' or not _in_owned_job():
        raise RuntimeError('trusted configured owner required')
    (directory/'configured_backend_started.json').write_text(json.dumps({
        'run_id':args.run_id,'after_start':True,'in_job':True,'source_kind':'configured'}),encoding='utf-8')
    provider=ConfiguredSourceProvider();probe=SchemaProbe()
    stop=threading.Event();done=threading.Event();control_errors=[]
    def control():
        try:
            while sys.stdin.buffer.readline(4096):
                if not done.is_set():
                    stop.set()
                    for owner in (provider,probe):
                        try:owner.cancel()
                        except BaseException:control_errors.append('owned_cancel_failed')
            if not done.is_set():
                stop.set()
                for owner in (provider,probe):
                    try:owner.cancel()
                    except BaseException:control_errors.append('owned_cancel_failed')
        except BaseException:control_errors.append('owned_control_failed');stop.set()
    controller=threading.Thread(target=control,name='configured-control',daemon=False)
    controller.start()
    status='failed';kind=None;counts=None;source=None
    def stage(name,event):emit({'event':'stage_'+event,'stage':name})
    try:
        remaining=args.deadline-time.monotonic()
        if remaining<=0:raise ExtractionStopped('deadline','preflight')
        budget=ExtractionBudget(remaining,stop.is_set);budget.deadline=min(budget.deadline,args.deadline)
        stage('source_prepare','enter')
        source=provider.prepare(settings_path,budget)
        stage('source_prepare','exit')
        counts=probe.run(source.db_path,source.key,budget,stage=stage)
        if provider.state['preparation_release_confirmed'] is not True:
            raise RuntimeError('source preparation cleanup unconfirmed')
        value={'run_id':args.run_id,'counts':counts,'application_ack':probe.state['application_ack'],
            'release_evidence':probe.state['release_evidence'],'source_kind':'configured',
            'key_preparation_cleanup_confirmed':True}
        raw=json.dumps(value,separators=(',',':')).encode()
        with (directory/'schema_result.json').open('wb') as handle:
            handle.write(raw);handle.flush();os.fsync(handle.fileno())
        with (directory/'schema_result.commit.json').open('w',encoding='utf-8') as handle:
            json.dump({'run_id':args.run_id,'payload_sha256':hashlib.sha256(raw).hexdigest()},handle)
            handle.flush();os.fsync(handle.fileno())
        status='completed'
    except BaseException as error:
        kind=_kind(error)
        status='cancelled' if stop.is_set() or isinstance(error,ExtractionStopped) else 'failed'
        counts=None
        emit({'event':'error','error_type':kind})
    finally:
        source=None;settings_path=None
        try:preparation_clean=provider.close() is True
        except BaseException as error:preparation_clean=False;kind=kind or _kind(error)
        probe_clean=(not probe.state['source_attempted'] or probe.state['application_ack'] is True)
        done.set();emit({'event':'result','status':status});emit({'event':'cleanup_ready'})
        controller.join(timeout=1)
        acknowledged=preparation_clean and probe_clean and not controller.is_alive() and not control_errors
        observation={'run_id':args.run_id,'status':status,'error_kind':kind,
            'provider':provider.state,'probe':probe.state,'key_preparation_cleanup_confirmed':preparation_clean,
            'control_joined':not controller.is_alive(),'control_errors':control_errors}
        (directory/'configured_observation.json').write_text(json.dumps(observation,indent=2),encoding='utf-8')
        emit({'event':'cleanup_ack','resource_opened':probe.state['resource_opened'],'resources_closed':acknowledged})
        os.close(protocol)
    return 0 if status=='completed' and acknowledged else 3

if __name__=='__main__':
    try:code=main()
    except BaseException:code=3
    raise SystemExit(code)
