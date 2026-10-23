"""One fixed schema aggregate over the production read-only worker connection.

Paths and keys are never copied into state. A return requires source release,
not merely a zero process exit or successful SQL.
"""
from extraction_budget import extraction_scope
from readonly_client import WorkerConnection
SCHEMA_SQL = ("SELECT COUNT(CASE WHEN type='table' THEN 1 END) AS table_count, "
              "COUNT(CASE WHEN type='view' THEN 1 END) AS view_count "
              "FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
class SchemaCleanupUnconfirmed(RuntimeError): pass
class SchemaOutputRejected(ValueError): pass

def validate_counts(value):
    if (not isinstance(value,dict) or set(value)!={'table_count','view_count'}
            or any(type(item) is not int or not 0<=item<2**63 for item in value.values())):
        raise SchemaOutputRejected('schema aggregate rejected')
    return dict(value)

def error_kind(error):
    name=type(error).__name__
    return name if name in {'ExtractionStopped','SnapshotUnavailable','WrongKeyError',
                            'SchemaCleanupUnconfirmed','SchemaOutputRejected','ValueError',
                            'RuntimeError','KeyboardInterrupt','SystemExit'} else 'UnexpectedError'

def release_evidence(connection):
    process=getattr(connection,'_process',None)
    event=getattr(connection,'_cancel_event',None)
    return {
        'reader_released':getattr(connection,'_released',None) is True,
        'source_release_confirmed':getattr(connection,'_source_release_confirmed',None) is True,
        'query_lock_released':getattr(connection,'_lock',False) is None,
        'cancel_event_closed':event is not None and event.handle is None,
        'event_mutex_released':event is not None and getattr(event,'_mutex',False) is None,
        'process_exit_zero':process is not None and type(process.returncode) is int and process.returncode==0,
        'process_handle_closed':process is not None and process._handle.closed is True,
        'streams_closed':process is not None and all(
            getattr(process,name,None) is None or getattr(process,name).closed is True
            for name in ('stdin','stdout','stderr')),
    }

class SchemaProbe:
    def __init__(self,factory=None):
        self.factory=WorkerConnection if factory is None else factory
        self.connection=None
        self.state={'source_attempted':False,'resource_opened':False,'application_ack':None,
                    'release_evidence':{},'cancel_requested':False,'cleanup_error':None}

    def cancel(self):
        self.state['cancel_requested']=True
        if self.connection is not None:
            try:self.connection.cancel()
            except BaseException as error:
                self.state['cancel_error']=error_kind(error)
                raise

    def run(self,path,key,budget,stage=None):
        phase=stage or (lambda name,event:None)
        failure=None;counts=None
        try:
            with extraction_scope(budget):
                budget.check('reader')
                phase('reader_open','enter')
                self.state['source_attempted']=True
                self.connection=self.factory(path,key)
                self.state['resource_opened']=True
                phase('reader_open','exit')
                phase('sql','enter')
                budget.check('reader')
                rows=list(self.connection.execute(SCHEMA_SQL))
                if len(rows)!=1:raise SchemaOutputRejected('one aggregate row required')
                counts=validate_counts(rows[0])
                budget.check('reader')
                phase('sql','exit')
        except BaseException as error:
            failure=error
            self.state['primary_error']=error_kind(error)
            for field in ('primary_error_type','cleanup_error_type'):
                if getattr(error,field,None) is not None:self.state[field]=getattr(error,field)
            if self.connection is None:self.connection=getattr(error,'worker',None)
        finally:
            if self.connection is not None:
                try:self.connection.close()
                except BaseException as error:
                    self.state['cleanup_error']=error_kind(error)
                    if (failure is None or isinstance(error,(KeyboardInterrupt,SystemExit))
                            and not isinstance(failure,(KeyboardInterrupt,SystemExit))):
                        failure=error
                self.state['release_evidence']=release_evidence(self.connection)
                self.state['application_ack']=all(self.state['release_evidence'].values())
            else:self.state['application_ack']=not self.state['source_attempted']
        if failure is not None:raise failure
        if self.state['application_ack'] is not True:
            raise SchemaCleanupUnconfirmed('source cleanup unconfirmed')
        budget.check('reader')
        if self.state['cancel_requested']:
            raise SchemaCleanupUnconfirmed('cancelled schema cannot return counts')
        self.state['counts']=counts
        return counts
