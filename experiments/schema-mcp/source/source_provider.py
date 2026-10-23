"""One explicit configured local source, through the existing normal key path.

No discovery of account databases, key persistence, output of settings values,
or automatic retry. Call only inside the protected schema worker.
"""
from dataclasses import dataclass
import re,time
from configured_child import explicit_local_file
from extraction_budget import ExtractionStopped

class SourcePolicyDenied(PermissionError):pass
class SourcePreparationError(RuntimeError):pass

_ERROR_KINDS={'ExtractionStopped','SnapshotUnavailable','WrongKeyError','OSError',
    'ValueError','RuntimeError','KeyboardInterrupt','SystemExit','DbAccessError',
    'SourcePolicyDenied','SourcePreparationError','UnexpectedError','TimeoutExpired'}

def _label(value):
    return value if type(value) is str and value in _ERROR_KINDS else 'UnexpectedError'

def _kind(error):
    return _label(type(error).__name__)

@dataclass(repr=False)
class PrivateSource:
    db_path:str
    key:str
    def __repr__(self):return '<PrivateSource: local memory only>'

def _settings(path):
    if not explicit_local_file(path):
        raise SourcePolicyDenied('one explicit local settings file is required')
    from line_mcp_server import _load_settings
    return _load_settings(path,reject_duplicate_keys=True)

def _key(path,**kwargs):
    from key_extractor import extract_key
    return extract_key(path,**kwargs)

class ConfiguredSourceProvider:
    def __init__(self,settings_loader=None,key_loader=None):
        self.settings_loader=_settings if settings_loader is None else settings_loader
        self.key_loader=_key if key_loader is None else key_loader
        self.retained_worker=None
        self._prepare_called=False
        self.state={'phase':'not_started','single_explicit_source':False,'key_preparation_started':False,
                    'private_source_work_started':False,'key_ready':False,
                    'key_loader_kind':'normal' if key_loader is None else 'injected_synthetic',
                    'preparation_release_confirmed':True}

    def _failure(self,error):
        owner=getattr(error,'worker',None)
        if owner is None:owner=self.retained_worker
        if owner is not None:self.retained_worker=owner
        self.state['preparation_release_confirmed']=False
        self.state['error_kind']=_kind(error)
        outgoing=error if isinstance(error,(ExtractionStopped,KeyboardInterrupt,SystemExit)) else SourcePreparationError('local key preparation failed')
        if owner is not None:outgoing.worker=owner
        primary=getattr(error,'primary_error_type',None)
        cleanup=getattr(error,'cleanup_error_type',None)
        outgoing.primary_error_type=_kind(error) if primary is None else _label(primary)
        outgoing.cleanup_error_type=None if cleanup is None else _label(cleanup)
        return outgoing

    def close(self):
        if self.retained_worker is not None:
            from schema_probe import release_evidence
            from line_process_identity import VerifiedProcess
            try:self.retained_worker.close()
            except BaseException as error:raise self._failure(error) from None
            if isinstance(self.retained_worker,VerifiedProcess):
                evidence=self.retained_worker.release_evidence()
                clean=(type(evidence) is dict and set(evidence)=={'native_handles_closed'}
                    and evidence['native_handles_closed'] is True)
            else:clean=all(release_evidence(self.retained_worker).values())
            self.state['preparation_release_confirmed']=clean
        return self.state['preparation_release_confirmed']

    def cancel(self):
        if self.retained_worker is not None:
            try:self.retained_worker.cancel()
            except BaseException as error:raise self._failure(error) from None

    def prepare(self,settings_path,budget):
        if self._prepare_called:
            raise self._failure(SourcePreparationError('source preparation is single use'))
        self._prepare_called=True
        budget.check('preflight')
        self.state['phase']='configuration'
        try:policy=self.settings_loader(settings_path)
        except ExtractionStopped:raise
        except Exception:raise SourcePreparationError('source settings unavailable') from None
        budget.check('preflight')
        if not isinstance(policy,dict) or policy.get('enabled') is not True:
            raise SourcePolicyDenied('local source policy disabled')
        path=policy.get('db_path')
        if not explicit_local_file(path,'.edb'):
            raise SourcePolicyDenied('one explicit local database is required')
        self.state['single_explicit_source']=True
        budget.check('preflight')
        self.state['phase']='key_preparation'
        self.state['key_preparation_started']=True
        self.state['preparation_release_confirmed']=False
        self.state['private_source_work_started']=self.state['key_loader_kind']=='normal'
        try:
            key=self.key_loader(path,require_consent=False,
                timeout=min(90.0,max(0,budget.deadline-time.monotonic())),cancelled=budget.cancelled)
        except BaseException as error:raise self._failure(error) from None
        budget.check('candidate_probe')  # A late native key must never become success.
        if not isinstance(key,str) or re.fullmatch(r'[0-9a-fA-F]{32}(?:[0-9a-fA-F]{32})?',key) is None:
            raise SourcePreparationError('supported verified key unavailable')
        self.state.update(phase='ready',key_ready=True,preparation_release_confirmed=True)
        return PrivateSource(path,key)
