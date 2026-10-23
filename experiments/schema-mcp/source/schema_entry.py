"""Bounded local schema diagnostic entry. Current CLI enables synthetic sources only."""
from pathlib import Path
import hashlib,json,math,threading
import runner
from schema_probe import validate_counts
MAX_ARTIFACT=16*1024
RELEASE_KEYS={'reader_released','source_release_confirmed','query_lock_released',
              'cancel_event_closed','event_mutex_released','process_exit_zero',
              'process_handle_closed','streams_closed'}

def _in_owned_job():
    import ctypes as C
    from ctypes import wintypes as W
    kernel=C.WinDLL('kernel32',use_last_error=True,winmode=0x800)
    kernel.GetCurrentProcess.restype=W.HANDLE
    kernel.IsProcessInJob.argtypes=[W.HANDLE,W.HANDLE,C.POINTER(W.BOOL)]
    kernel.IsProcessInJob.restype=W.BOOL
    member=W.BOOL()
    return bool(kernel.IsProcessInJob(kernel.GetCurrentProcess(),None,C.byref(member)) and member.value)

def bounded_read(path):
    with Path(path).open('rb') as handle:raw=handle.read(MAX_ARTIFACT+1)
    if len(raw)>MAX_ARTIFACT:raise ValueError('artifact too large')
    return raw

def validate_artifact(raw,run_id,source_kind='synthetic'):
    if len(raw)>MAX_ARTIFACT:raise ValueError('artifact too large')
    value=json.loads(raw)
    required={'run_id','counts','application_ack','release_evidence'}
    if source_kind=='synthetic':required.add('source_bytes_unchanged')
    elif source_kind=='configured':required.update(('source_kind','key_preparation_cleanup_confirmed'))
    else:raise ValueError('fixed artifact source required')
    if not isinstance(value,dict) or set(value)!=required:
        raise ValueError('artifact fields rejected')
    if value['run_id']!=run_id or value['application_ack'] is not True:
        raise ValueError('artifact binding or cleanup rejected')
    if source_kind=='synthetic' and value['source_bytes_unchanged'] is not True:
        raise ValueError('synthetic source changed')
    if source_kind=='configured' and (value['source_kind']!='configured'
            or value['key_preparation_cleanup_confirmed'] is not True):
        raise ValueError('configured source preparation unconfirmed')
    evidence=value['release_evidence']
    if not isinstance(evidence,dict) or set(evidence)!=RELEASE_KEYS or not all(v is True for v in evidence.values()):
        raise ValueError('source release unconfirmed')
    return validate_counts(value['counts'])

def valid_completion_receipt(directory,run_id):
    try:
        directory=Path(directory);raw=bounded_read(directory/'completion.json')
        payload=json.loads(raw);commit=json.loads(bounded_read(directory/'completion.commit.json'))
        return (isinstance(payload,dict) and isinstance(commit,dict)
            and payload.get('run_id')==commit.get('run_id')==run_id
            and commit.get('payload_sha256')==hashlib.sha256(raw).hexdigest()
            and commit.get('completion_receipt_written') is True)
    except (OSError,ValueError,TypeError):return False

def _run(directory,case='success',*,overall=30,stage_budget=8,cleanup_budget=3,cancel_after=None,
         launch_context=None,source_kind='synthetic',settings_path=None,cancel_event=None):
    # Exact Job policy is queried by the native creator on its retained handle.
    # Child Job membership alone is not proof of which nested Job owns it.
    if (not isinstance(launch_context,dict)
            or set(launch_context)!={'outer_run_id','assigned_before_start','active_process_limit'}
            or launch_context['assigned_before_start'] is not True
            or type(launch_context['active_process_limit']) is not int
            or not 1<=launch_context['active_process_limit']<=8
            or not isinstance(launch_context['outer_run_id'],str)
            or len(launch_context['outer_run_id'])!=32
            or any(c not in '0123456789abcdef' for c in launch_context['outer_run_id'])
            or not _in_owned_job()):
        raise ValueError('schema entry requires the dedicated bounded outer owner')
    directory=Path(directory)
    configured=source_kind=='configured'
    private={'settings_path':settings_path} if configured else {}
    if cancel_event is not None:private['cancel_event']=cancel_event
    state=runner.run(case,directory,stage_budget=stage_budget,overall_budget=overall,
                     cleanup_budget=cleanup_budget,spawn_budget=min(5,overall),
                     cancel_after=cancel_after,entrypoint='schema_configured' if configured else 'schema_synthetic',**private)
    receipt_ok=valid_completion_receipt(directory,state['run_id'])
    os_closed=all(state.get(key) is True for key in (
        'process_exited','io_threads_joined','retained_handle_closed','owned_job_empty',
        'owned_job_closed','spawn_thread_joined'))
    result={'scope':'schema_aggregate_only','adapter':'synthetic_source_real_readonly_worker',
            'status':state['status'],'run_id':state['run_id'],'os_release_confirmed':os_closed,
            'application_ack':state['cleanup_confirmed'] is True,
            'completion_receipt_valid':receipt_ok,'runner':state,'LINE_read':False}
    if configured:
        result['adapter']='configured_source_real_readonly_worker'
        result['LINE_read']=None  # The route alone does not attest whether the configured source was private.
    if state['status']=='completed':
        try:
            if not receipt_ok or not os_closed or state['cleanup_confirmed'] is not True:
                raise ValueError('completion unconfirmed')
            raw=bounded_read(directory/'schema_result.json')
            commit=json.loads(bounded_read(directory/'schema_result.commit.json'))
            if (commit.get('run_id')!=state['run_id']
                    or commit.get('payload_sha256')!=hashlib.sha256(raw).hexdigest()):
                raise ValueError('schema commit rejected')
            result['counts']=validate_artifact(raw,state['run_id'],source_kind=source_kind)
        except (OSError,ValueError,TypeError):
            result['status']='output_or_receipt_rejected'
    result['successful']=result['status']=='completed' and 'counts' in result
    return result

def run(directory,case='success',*,overall=30,stage_budget=8,cleanup_budget=3,cancel_after=None,launch_context=None):
    return _run(directory,case,overall=overall,stage_budget=stage_budget,cleanup_budget=cleanup_budget,
        cancel_after=cancel_after,launch_context=launch_context)

def run_configured(directory,settings_path,*,overall=30,cleanup_budget=3,cancel_after=None,launch_context=None,cancel_event=None):
    if cancel_event is not None and not isinstance(cancel_event,threading.Event):
        raise ValueError('local cancellation event required')
    for value,ceiling in ((overall,30),(cleanup_budget,3)):
        if (isinstance(value,bool) or not isinstance(value,(int,float))
                or not math.isfinite(value) or not 0<value<=ceiling):
            raise ValueError('configured product budget exceeds its fixed bound')
    return _run(directory,'success',overall=overall,cleanup_budget=cleanup_budget,cancel_after=cancel_after,
        launch_context=launch_context,source_kind='configured',settings_path=settings_path,cancel_event=cancel_event)

if __name__=='__main__':
    import argparse,sys
    parser=argparse.ArgumentParser(description='Protected schema diagnostic; synthetic sources only in this candidate.')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    result=run(args.output)
    # Public output contains aggregates and fixed status fields, never paths or keys.
    print(json.dumps({key:value for key,value in result.items() if key!='runner'}))
    raise SystemExit(0 if result['successful'] else 3)
