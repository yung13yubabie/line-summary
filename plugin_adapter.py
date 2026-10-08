"""ChatGPT-ready stdio contract prototype: disabled or synthetic data ONLY.

There is deliberately no live reader, key extraction, HTTP listener, tunnel
launcher, credential input or privacy-ready boolean in this adapter.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Literal

import mcp
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations, TextContent
from pydantic import BaseModel, ConfigDict, Field

from db_reader import DbReader
from plugin_runtime import RequestGate, retry_seconds
from mock_privacy import MockPrivacyChoice, CONTROL_KEYS
from safety import json_size

_MCP_LOG_DIRECTORY = Path(mcp.__file__).resolve().parent


class PluginSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    mode: Literal["disabled", "mock"] = "disabled"
    requests_per_minute: int = Field(default=10, ge=1, le=60)
    min_interval_seconds: float = Field(default=2.0, ge=0.0, le=60.0)
    max_messages_per_call: int = Field(default=100, ge=1, le=500)
    max_response_bytes: int = Field(default=262144, ge=4096, le=262144)
    max_session_messages: int = Field(default=5000, ge=1, le=5000)
    max_session_bytes: int = Field(default=2097152, ge=4096, le=2097152)


class PluginResponse(BaseModel):
    """One explicit JSON schema shared by all prototype tools."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok", "disabled", "rate_limited", "busy", "error", "requires_confirmation"]
    mode: Literal["disabled", "mock"]
    synthetic: bool
    items: list[dict[str, Any]] = Field(default_factory=list)
    has_more: bool = False
    next_cursor: str | None = None
    content_complete: bool = False
    fetched_at: str
    source_sync_at: None = None
    source_latest_at: str | None = None
    source_latest_at_scope: str | None = None
    database_changes_may_affect_pagination: bool = False
    match_mode: str | None = None
    range_since_ms: int | None = None
    range_until_ms: int | None = None
    latest_returned_at: str | None = None
    timestamp_timezone: str = "+08:00"
    coverage: str = "not_read"
    consistency: str = "not_read"
    error_code: str | None = None
    retry_after_seconds: float | None = None
    thread_support: Literal["unsupported"] = "unsupported"
    destination: str | None = None
    privacy_choice: Literal["deidentify", "original"] | None = None
    mock_scope_id: str | None = None
    scope_fingerprint: str | None = None
    request_scope: dict[str, Any] | None = None
    human_confirmation_verified: Literal[False] = False
    privacy_processing: Literal["not_implemented"] = "not_implemented"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value):
    # Importing the original server would also import the real key extractor.
    # Keep this adapter independent from that entry point.
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid_time")
    import re
    if re.search(r"[.,]\d{7,}", value):
        raise ValueError("invalid_time")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("invalid_time")
    delta = dt.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    us = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    return -(-us // 1000)


class SyntheticSource:
    """A private temporary SQLite fixture, never an account database path."""
    allowed_chat_ids = frozenset({"demo-project", "demo-family"})
    def __init__(self):
        self._temp = tempfile.TemporaryDirectory(prefix="line-summary-synthetic-")
        self.path = Path(self._temp.name) / "synthetic.sqlite"
        conn = sqlite3.connect(self.path)
        conn.executescript('''
          CREATE TABLE _chat (_id TEXT PRIMARY KEY, _lastUpdatedTime INTEGER, _unreadCount INTEGER);
          CREATE TABLE _groupChat (_chatMid TEXT PRIMARY KEY, _chatName TEXT);
          CREATE TABLE _contact (_mid TEXT PRIMARY KEY, _displayName TEXT, _displayNameOverridden TEXT, _type INTEGER);
          CREATE TABLE _message (_id TEXT PRIMARY KEY, _chatId TEXT, _from TEXT, _createdTime INTEGER, _text TEXT, _contentType INTEGER, _contentMetadata TEXT);
          INSERT INTO _chat VALUES ('demo-project',1790813100000,2);
          INSERT INTO _chat VALUES ('demo-family',1790812800000,1);
          INSERT INTO _chat VALUES ('demo-denied',1790812800000,1);
          INSERT INTO _groupChat VALUES ('demo-project','合成範例：專案群');
          INSERT INTO _groupChat VALUES ('demo-family','合成範例：生活群');
          INSERT INTO _groupChat VALUES ('demo-denied','不允許的合成群');
          INSERT INTO _contact VALUES ('demo-person-a','範例甲',NULL,0);
          INSERT INTO _contact VALUES ('demo-person-b','範例乙',NULL,0);
        ''')
        rows = [
            ("demo-001","demo-project","demo-person-a",1790812800000,"合成資料：報價 1000 元，交期週五。"),
            ("demo-002","demo-project","demo-person-b",1790812800000,"合成資料：待辦，確認報價內容。"),
            ("demo-003","demo-project","demo-person-a",1790813100000,"合成資料：完成 50%_test，不是萬用字元。"),
            ("demo-004","demo-family","demo-person-b",1790812800000,"合成資料：週末整理書架。"),
            ("demo-hidden","demo-denied","demo-person-a",1790812800000,"不可從此adapter取得的範例。"),
        ]
        conn.executemany('INSERT INTO _message VALUES (?,?,?,?,?,0,NULL)', rows)
        conn.commit(); conn.close()
        self.reader = DbReader(str(self.path), None, _test_mode=True)

    def close(self):
        self._temp.cleanup()


class PluginService:
    def __init__(self, settings=None, *, source_factory=SyntheticSource, clock=None):
        self.settings = settings or PluginSettings()
        # Disabled mode never even constructs the source factory.
        self._source = source_factory() if self.settings.mode == "mock" else None
        self.gate = RequestGate(self.settings.requests_per_minute, self.settings.min_interval_seconds,
                                **({"clock": clock} if clock else {}))
        self.privacy = MockPrivacyChoice(**({"clock":clock} if clock else {}))
        self._budget_lock = asyncio.Lock()
        self._messages = 0
        self._bytes = 0

    def close(self):
        if self._source is not None:
            self._source.close()

    def _response(self, **kwargs):
        result=PluginResponse(mode=self.settings.mode, synthetic=self.settings.mode == "mock",
                              fetched_at=_now(), **kwargs)
        if json_size(result.model_dump())>self.settings.max_response_bytes:
            return PluginResponse(status="error",mode=self.settings.mode,
                                  synthetic=self.settings.mode=="mock",fetched_at=_now(),
                                  error_code="output_budget_exhausted")
        return result

    def _scope(self, chat_id, since, until):
        if chat_id not in self._source.allowed_chat_ids:
            raise PermissionError("scope_denied")
        start, end = _parse_time(since), _parse_time(until)
        if end <= start or end - start > 31 * 86400000:
            raise ValueError("invalid_range")
        return start, end

    def _limit(self, limit):
        if type(limit) is not int or not 1<=limit<=5000:
            raise ValueError("invalid_limit")
        return min(limit, self.settings.max_messages_per_call,
                   max(1, self.settings.max_session_messages - self._messages))

    async def call(self, name, arguments):
        if self.settings.mode != "mock":
            return self._response(status="disabled", error_code="live_data_not_implemented")
        if name not in {"line_list_chats", "line_get_history", "line_search_messages", "line_get_unread", "line_get_contacts", "line_status"}:
            return self._response(status="error", error_code="unknown_tool")
        if name != "line_status":
            try:
                self._validate_scope_arguments(name,arguments)
                accepted,details,fingerprint=self.privacy.negotiate(name,arguments)
            except PermissionError:
                return self._response(status="error",error_code="scope_denied")
            except Exception:
                return self._response(status="error",error_code="invalid_request")
            if not accepted:
                return self._response(status="requires_confirmation",**details)
            try:
                return await self._execute(name,arguments,privacy_details=details)
            finally:
                self.privacy.release(fingerprint)
        return await self._execute(name,arguments)

    def _validate_scope_arguments(self,name,args):
        if name in {"line_get_history","line_search_messages"}:
            self._scope(args["chat_id"],args["since"],args["until"])
        query=args.get("query")
        if query is not None:
            if not isinstance(query,str) or len(query)>256:
                raise ValueError("invalid_query")
            query.encode("utf-8")
        if name=="line_search_messages" and (not query or not query.strip()):
            raise ValueError("invalid_query")
        for key in ("limit","limit_chats","per_chat_limit"):
            if key in args:
                self._limit(args[key])

    async def _execute(self,name,arguments,privacy_details=None):
        async with self._budget_lock:
            if self.settings.max_session_bytes - self._bytes < 4096 or self._messages >= self.settings.max_session_messages:
                return self._response(status="error", error_code="session_budget_exhausted")
        async def operation():
            data_arguments={key:value for key,value in arguments.items() if key not in CONTROL_KEYS}
            return await asyncio.to_thread(self._read, name, data_arguments)
        data, error, retry = await self.gate.run(name, arguments, operation)
        if error:
            status = error if error in {"rate_limited", "busy"} else "error"
            return self._response(status=status, error_code=error, retry_after_seconds=retry_seconds(retry))
        if isinstance(data, str):
            code = data if data in {"scope_denied", "invalid_request", "invalid_query"} else "source_error"
            return self._response(status="error", error_code=code)
        try:
            result, count = self._render(name, data)
            if privacy_details:
                result=result.model_copy(update=privacy_details)
        except Exception:
            return self._response(status="error", error_code="source_error")
        if result.error_code=="output_budget_exhausted":
            return result
        size = json_size(result.model_dump())
        async with self._budget_lock:
            # Every returned copy, including an in-flight duplicate, is charged.
            if size > self.settings.max_response_bytes or self._bytes + size > self.settings.max_session_bytes or self._messages + count > self.settings.max_session_messages:
                return self._response(status="error",error_code="output_budget_exhausted")
            self._bytes += size
            self._messages += count
        return result

    def _render(self, name, data):
        blocked = data.get("blocked_reason")
        if blocked not in {None, "item_exceeds_byte_budget"}:
            raise ValueError("source_error")
        result = self._response(status="error" if blocked else "ok", items=data["items"], has_more=data.get("has_more",False),
                                next_cursor=data.get("next_cursor"), content_complete=data.get("content_complete",True),
                                coverage=data.get("coverage","synthetic_metadata"),
                                consistency=data.get("consistency","synthetic_fixture"),
                                source_latest_at=data.get("source_latest_at"),
                                source_latest_at_scope=data.get("source_latest_at_scope"),
                                database_changes_may_affect_pagination=data.get("database_changes_may_affect_pagination",False),
                                match_mode=data.get("match_mode"),range_since_ms=data.get("range_since_ms"),
                                range_until_ms=data.get("range_until_ms"),error_code=data.get("blocked_reason"))
        timestamps=[]
        for item in result.items:
            timestamps += [m.get("sent_at") for m in item.get("messages",[])] if name == "line_get_unread" else [item.get("sent_at")]
        result.latest_returned_at=max((x for x in timestamps if x),default=None)
        count = (len(result.items) if name in {"line_get_history","line_search_messages"} else
                 sum(len(x["messages"]) for x in result.items) if name == "line_get_unread" else 0)
        return result, count

    def _read(self, name, args):
        reader=self._source.reader
        max_bytes=min(self.settings.max_response_bytes,self.settings.max_session_bytes-self._bytes)-2048
        try:
            if name == "line_status":
                return {"items":[{"real_data_enabled":False,"privacy_filter_implemented":False,
                                  "automatic_sync":False,"requests_per_minute":self.settings.requests_per_minute,
                                  "min_interval_seconds":self.settings.min_interval_seconds,"max_parallel_reads":1,
                                  "sample_window":"2026-10-01T00:00:00+08:00 / 2026-10-02T00:00:00+08:00"}]}
            if name in {"line_get_history","line_search_messages"}:
                start,end=self._scope(args["chat_id"],args["since"],args["until"])
                kwargs=dict(chat_id=args["chat_id"],since_ms=start,until_ms=end,
                            limit=self._limit(args["limit"]),cursor=args["cursor"],max_bytes=max_bytes)
                if name == "line_search_messages":
                    data=reader.search_messages(query=args["query"],**kwargs)
                else:
                    data=reader.get_history(include_source_ref=True,**kwargs)
                return data
            if name == "line_list_chats":
                if len(args["query"])>256: return "invalid_query"
                return reader.list_chats(query=args["query"],limit=self._limit(args["limit"]),cursor=args["cursor"],
                                         allowed_chat_ids=self._source.allowed_chat_ids,max_bytes=max_bytes)
            if name == "line_get_contacts":
                if len(args["query"])>256: return "invalid_query"
                return reader.get_contacts(query=args["query"],limit=self._limit(args["limit"]),cursor=args["cursor"],max_bytes=max_bytes)
            return reader.get_unread(limit_chats=self._limit(args["limit_chats"]),per_chat_limit=self._limit(args["per_chat_limit"]),
                                     total_message_limit=self._limit(self.settings.max_messages_per_call),
                                     allowed_chat_ids=self._source.allowed_chat_ids,cursor=args["cursor"],max_bytes=max_bytes,
                                     include_source_ref=True)
        except PermissionError:
            return "scope_denied"
        except (ValueError, TypeError, KeyError):
            return "invalid_request"


class _SafeMCPLogFilter(logging.Filter):
    def filter(self, record):
        # Some SDK transport validation paths use logging.warning() directly,
        # producing a root record even when a host already configured logging.
        # Match the SDK's code origin as well as its named loggers.
        try:
            sdk_origin = Path(record.pathname).resolve().is_relative_to(_MCP_LOG_DIRECTORY)
        except (OSError, TypeError, ValueError):
            sdk_origin = False
        if record.name == "mcp" or record.name.startswith("mcp.") or sdk_origin:
            # SDK transport validation runs before tools/call and can otherwise
            # interpolate raw JSON, arguments or exception details into stderr.
            record.msg = "MCP diagnostic details suppressed by prototype privacy boundary"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def _install_safe_mcp_logging():
    # Propagated child records bypass ancestor logger filters, so filter every
    # existing output handler after FastMCP has configured logging. Also guard
    # the SDK's direct low-level logger independently of handler arrangement.
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f,_SafeMCPLogFilter) for f in handler.filters):
            handler.addFilter(_SafeMCPLogFilter())
    for name in ("", "mcp.server.lowlevel.server", "mcp.server.stdio", "mcp.shared.session"):
        logger=logging.getLogger(name)
        if not any(isinstance(f,_SafeMCPLogFilter) for f in logger.filters):
            logger.addFilter(_SafeMCPLogFilter())
        for handler in logger.handlers:
            if not any(isinstance(f,_SafeMCPLogFilter) for f in handler.filters):
                handler.addFilter(_SafeMCPLogFilter())


class SafeFastMCP(FastMCP):
    """Sanitize SDK argument/tool errors before they reach protocol or logs."""
    def __init__(self, service):
        self._service = service
        super().__init__("line-summary-prototype",log_level="ERROR")
        _install_safe_mcp_logging()

    def _setup_handlers(self):
        # This prototype only supports tools. Do not expose FastMCP's default
        # prompts/resources surfaces, whose unknown-name errors echo inputs.
        self._mcp_server.list_tools()(self.list_tools)
        self._mcp_server.call_tool(validate_input=False)(self.call_tool)

    async def call_tool(self, name, arguments):
        try:
            tool = self._tool_manager.get_tool(name)
            if tool is None or not isinstance(arguments,dict):
                raise ValueError("invalid_tool_request")
            if set(arguments) - set(tool.parameters.get("properties",{})) or json_size(arguments) > 8192:
                raise ValueError("invalid_tool_request")
            return await super().call_tool(name,arguments)
        except asyncio.CancelledError:
            raise
        except Exception:
            result=self._service._response(status="error",error_code="invalid_tool_request")
            return ([TextContent(type="text",text=result.model_dump_json())],result.model_dump())


def create_server(settings=None, *, service=None):
    service=service or PluginService(settings)
    server=SafeFastMCP(service)
    annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False)
    @server.tool(annotations=annotations,structured_output=True)
    async def line_status() -> PluginResponse:
        """Check this prototype's mode and local design limits; no account access."""
        return await service.call("line_status",{})
    @server.tool(annotations=annotations,structured_output=True)
    async def line_list_chats(query:str="",limit:int=20,cursor:str|None=None,destination:str|None=None,privacy_choice:str|None=None,mock_scope_id:str|None=None) -> PluginResponse:
        """List allowed synthetic chats only. This prototype cannot read real LINE."""
        return await service.call("line_list_chats",dict(query=query,limit=limit,cursor=cursor,destination=destination,privacy_choice=privacy_choice,mock_scope_id=mock_scope_id))
    @server.tool(annotations=annotations,structured_output=True)
    async def line_get_history(chat_id:str,since:str,until:str,limit:int=100,cursor:str|None=None,destination:str|None=None,privacy_choice:str|None=None,mock_scope_id:str|None=None) -> PluginResponse:
        """Read synthetic local rows in timezone-aware [since,until); preserve cursor scope."""
        return await service.call("line_get_history",dict(chat_id=chat_id,since=since,until=until,limit=limit,cursor=cursor,destination=destination,privacy_choice=privacy_choice,mock_scope_id=mock_scope_id))
    @server.tool(annotations=annotations,structured_output=True)
    async def line_search_messages(chat_id:str,query:str,since:str,until:str,limit:int=100,cursor:str|None=None,destination:str|None=None,privacy_choice:str|None=None,mock_scope_id:str|None=None) -> PluginResponse:
        """Search synthetic message text by exact case-sensitive Unicode substring. %/_ are literal; no thread/context expansion."""
        return await service.call("line_search_messages",dict(chat_id=chat_id,query=query,since=since,until=until,limit=limit,cursor=cursor,destination=destination,privacy_choice=privacy_choice,mock_scope_id=mock_scope_id))
    @server.tool(annotations=annotations,structured_output=True)
    async def line_get_unread(limit_chats:int=20,per_chat_limit:int=50,cursor:str|None=None,destination:str|None=None,privacy_choice:str|None=None,mock_scope_id:str|None=None) -> PluginResponse:
        """Read synthetic unread counters and approximate latest rows; synchronization remains unknown."""
        return await service.call("line_get_unread",dict(limit_chats=limit_chats,per_chat_limit=per_chat_limit,cursor=cursor,destination=destination,privacy_choice=privacy_choice,mock_scope_id=mock_scope_id))
    @server.tool(annotations=annotations,structured_output=True)
    async def line_get_contacts(query:str="",limit:int=20,cursor:str|None=None,destination:str|None=None,privacy_choice:str|None=None,mock_scope_id:str|None=None) -> PluginResponse:
        """List synthetic contacts only; real contact/name privacy filtering is not implemented."""
        return await service.call("line_get_contacts",dict(query=query,limit=limit,cursor=cursor,destination=destination,privacy_choice=privacy_choice,mock_scope_id=mock_scope_id))
    return server,service


def load_settings(path):
    try:
        raw=json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PluginSettings()
    # Real mode or bypass flags fail validation. No secrets/path are interpolated.
    return PluginSettings.model_validate(raw)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal error embeds the untrusted value or unknown option.
        self.exit(2,"Invalid command options. Use --help for safe local options.\n")


def main():
    parser=SafeArgumentParser(prog="line-summary-prototype",description="Disabled/synthetic-only MCP prototype")
    parser.add_argument("--mode",choices=["disabled","mock"],default=None)
    parser.add_argument("--settings",default=str(Path(__file__).with_name("plugin_settings.json")))
    args=parser.parse_args()
    try:
        settings=load_settings(args.settings)
        if args.mode is not None:
            settings=settings.model_copy(update={"mode":args.mode})
    except Exception:
        parser.exit(2,"Invalid plugin settings. Live data mode is not implemented.\n")
    service=None
    failed=False
    try:
        server,service=create_server(settings)
        server.run(transport="stdio")
    except KeyboardInterrupt:
        pass
    except Exception:
        failed=True
        sys.stderr.write("Prototype stopped after an internal error; no source details are returned.\n")
    finally:
        if service is not None:
            try:
                service.close()
            except Exception:
                failed=True
                sys.stderr.write("Prototype cleanup failed; inspect locally without sharing private data.\n")
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
