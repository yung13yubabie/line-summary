"""Static error surfaces must not echo synthetic private arguments or paths."""
import asyncio
import logging
import sys
from unittest.mock import Mock

import pytest

import plugin_adapter as adapter

SENTINEL = "SYNTHETIC_PRIVATE_VALUE_/local/account.db"


def test_only_tools_are_advertised_and_registered():
    server,service=adapter.create_server()
    try:
        names={getattr(key,'__name__',str(key)) for key in server._mcp_server.request_handlers}
        assert 'ListToolsRequest' in names and 'CallToolRequest' in names
        assert not any('Prompt' in name or 'Resource' in name for name in names)
        tools=asyncio.run(server.list_tools())
        assert len(tools)==6 and all(tool.outputSchema for tool in tools)
    finally:
        service.close()


@pytest.mark.parametrize('arguments',[{}, {'limit':SENTINEL}, {'privacy_ready':True}])
def test_safe_wrapper_returns_static_error_without_session(arguments):
    server,service=adapter.create_server()
    try:
        _,payload=asyncio.run(server.call_tool('unknown_'+SENTINEL,arguments))
        assert payload['error_code']=='invalid_tool_request'
        assert SENTINEL not in str(payload)
    finally:
        service.close()


@pytest.mark.parametrize('logger_name', ['mcp.server.lowlevel.server', 'root'])
def test_log_filter_clears_format_args_tracebacks_and_stack(logger_name):
    try:
        raise RuntimeError(SENTINEL)
    except RuntimeError:
        source=str(adapter._MCP_LOG_DIRECTORY/'shared'/'session.py') if logger_name=='root' else __file__
        record=logging.LogRecord(logger_name,logging.ERROR,source,1,
                                 'private %s',(SENTINEL,),sys.exc_info())
    record.exc_text=SENTINEL
    record.stack_info=SENTINEL
    assert adapter._SafeMCPLogFilter().filter(record)
    assert SENTINEL not in logging.Formatter().format(record)
    assert record.args==() and record.exc_info is None and record.exc_text is None and record.stack_info is None
    other=logging.LogRecord('unrelated',logging.ERROR,__file__,1,'ordinary',(),None)
    adapter._SafeMCPLogFilter().filter(other)
    assert other.getMessage()=='ordinary'


def test_log_filter_invalid_unrelated_origin_keeps_ordinary_message():
    record=logging.LogRecord('unrelated',logging.WARNING,__file__,1,'ordinary',(),None)
    record.pathname=None
    assert adapter._SafeMCPLogFilter().filter(record)
    assert record.getMessage()=='ordinary'


@pytest.mark.parametrize('logger_name', ['', 'mcp.server.lowlevel.server'])
def test_log_filter_installation_is_idempotent(logger_name):
    logger=logging.getLogger(logger_name)
    handler=logging.StreamHandler()
    logger.addHandler(handler)
    try:
        adapter._install_safe_mcp_logging()
        adapter._install_safe_mcp_logging()
        assert sum(isinstance(f,adapter._SafeMCPLogFilter) for f in handler.filters)==1
    finally:
        logger.removeHandler(handler)


@pytest.mark.parametrize('arguments',[['--mode',SENTINEL],['--'+SENTINEL]])
def test_cli_invalid_values_are_never_echoed(monkeypatch,capsys,arguments):
    monkeypatch.setattr(sys,'argv',['private-program-path',*arguments])
    with pytest.raises(SystemExit) as result:
        adapter.main()
    assert result.value.code==2
    output=capsys.readouterr()
    assert SENTINEL not in output.out+output.err
    assert 'private-program-path' not in output.out+output.err


@pytest.mark.parametrize('boundary',['config','create','run','close'])
def test_main_boundaries_fail_with_static_error(monkeypatch,capsys,boundary):
    monkeypatch.setattr(sys,'argv',['prototype'])
    monkeypatch.setattr(adapter,'load_settings',lambda path:adapter.PluginSettings())
    server,service=Mock(),Mock()
    monkeypatch.setattr(adapter,'create_server',lambda settings:(server,service))
    failure=Mock(side_effect=RuntimeError(SENTINEL))
    if boundary=='config': monkeypatch.setattr(adapter,'load_settings',failure)
    elif boundary=='create': monkeypatch.setattr(adapter,'create_server',failure)
    elif boundary=='run': server.run=failure
    else: service.close=failure
    if boundary=='config':
        with pytest.raises(SystemExit) as result: adapter.main()
        assert result.value.code==2
    else:
        assert adapter.main()==2
    output=capsys.readouterr()
    assert SENTINEL not in output.out+output.err


def test_backend_string_and_malformed_result_are_not_error_echoes(monkeypatch):
    service=adapter.PluginService(adapter.PluginSettings(mode='mock',min_interval_seconds=0.0))
    try:
        monkeypatch.setattr(service,'_read',lambda name,args:SENTINEL)
        first=asyncio.run(service.call('line_status',{}))
        assert first.error_code=='source_error' and SENTINEL not in first.model_dump_json()
        monkeypatch.setattr(service,'_read',lambda name,args:{'private':SENTINEL})
        second=asyncio.run(service.call('line_status',{}))
        assert second.error_code=='source_error' and SENTINEL not in second.model_dump_json()
    finally:
        service.close()
