"""One encrypted source connection in an isolated, short-lived Windows process.

The sole request channel is stdin/stdout. Paths and keys never appear in argv,
environment additions, stderr, or diagnostic replies.
"""

import base64
import json
import os
import re
import sys
import threading
from pathlib import Path

import apsw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from readonly_guard import Guard, SourceShmUnavailable


MAX_REQUEST = 16 * 1024
MAX_REPLY = 1024 * 1024
MAX_CURSORS = 8
_KEY = re.compile(r"[0-9a-fA-F]{32}(?:[0-9a-fA-F]{32})?\Z")
_TABLE_INFO = re.compile(r"PRAGMA\s+table_info\s*\(\s*_message\s*\)\s*;?\Z", re.I)


class ProtocolError(Exception):
    pass


class Cancellation:
    """One finite watcher; joins before a successful child exit."""
    def __init__(self, handle):
        import ctypes as C
        from ctypes import wintypes as W
        self.handle = handle
        self.connection = None
        self.cancelled = False
        self.mutex = threading.Lock()
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
        self.kernel.WaitForSingleObject.restype = W.DWORD
        self.kernel.SetEvent.argtypes = [W.HANDLE]
        self.kernel.SetEvent.restype = W.BOOL
        self.kernel.CloseHandle.argtypes = [W.HANDLE]
        self.kernel.CloseHandle.restype = W.BOOL
        self.thread = threading.Thread(target=self._watch, name="readonly-cancel", daemon=False)
        self.thread.start()

    def _watch(self):
        if self.kernel.WaitForSingleObject(self.handle, 30000) == 0:
            self.cancelled = True
            with self.mutex:
                connection = self.connection
                if connection is not None:
                    connection.interrupt()

    def attach(self, connection):
        with self.mutex:
            self.connection = connection

    def detach(self):
        with self.mutex:
            self.connection = None

    def close(self):
        self.detach()
        if not self.kernel.SetEvent(self.handle):
            raise RuntimeError("cancellation watcher cleanup failed")
        self.thread.join(timeout=1)
        if self.thread.is_alive():
            raise RuntimeError("cancellation watcher cleanup incomplete")
        if not self.kernel.CloseHandle(self.handle):
            raise RuntimeError("cancellation event cleanup failed")


def encode_value(value):
    if isinstance(value, bytes):
        return {"__binary__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(key): encode_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ProtocolError()


def decode_value(value):
    if isinstance(value, dict):
        if set(value) == {"__binary__"} and isinstance(value["__binary__"], str):
            return base64.b64decode(value["__binary__"], validate=True)
        raise ProtocolError()
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ProtocolError()


def read_request():
    line = sys.stdin.buffer.readline(MAX_REQUEST + 1)
    if not line:
        return None
    if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
        raise ProtocolError()
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ProtocolError()
    return value


def reply(value):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > MAX_REPLY:
        payload = b'{"ok":false,"code":"reply_too_large"}\n'
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def permitted_sql(sql):
    if not isinstance(sql, str) or len(sql) > 8192:
        return False
    stripped = sql.strip()
    if _TABLE_INFO.fullmatch(stripped):
        return True
    if not re.match(r"SELECT\b", stripped, re.I):
        return False
    return ";" not in stripped.rstrip(";")


def authorizer(action, arg1, arg2, database, source):
    if action in (apsw.SQLITE_SELECT, apsw.SQLITE_READ):
        return apsw.SQLITE_OK
    if action == apsw.SQLITE_FUNCTION and str(arg2 or arg1).lower() != "load_extension":
        return apsw.SQLITE_OK
    if action == apsw.SQLITE_PRAGMA and arg1 == "table_info" and arg2 == "_message":
        return apsw.SQLITE_OK
    return apsw.SQLITE_DENY


def as_row(cursor, row):
    return {column[0]: value for column, value in zip(cursor.getdescription(), row)}


def run():
    watchdog = threading.Timer(30, os._exit, args=(124,))
    watchdog.daemon = True
    watchdog.start()
    guard = None
    cancellation = None
    connection = None
    closing = False
    close_counts = {}
    cursors = {}
    pending = {}
    next_id = 0
    try:
        request = read_request()
        if (request is None or set(request) != {"op", "path", "key", "cancel_event"}
                or request["op"] != "open"
                or not isinstance(request["path"], str)
                or not isinstance(request["key"], str)
                or not _KEY.fullmatch(request["key"])
                or type(request["cancel_event"]) is not int or request["cancel_event"] <= 0):
            raise ProtocolError()
        cancellation = Cancellation(request["cancel_event"])
        source = request["path"]
        guard = Guard(source)
        uri = Path(source).as_uri() + "?mode=ro&readonly_shm=1"
        connection = apsw.Connection(uri, flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI,
                                     vfs="multipleciphers-win32")
        cancellation.attach(connection)
        # interrupt() before a statement starts does not affect future statements.
        # The progress predicate also covers that check-to-execute cancellation race.
        connection.set_progress_handler(lambda: cancellation.cancelled, 1000)
        connection.set_busy_timeout(1000)
        connection.execute("PRAGMA mmap_size=0")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cipher='aes128cbc'")
        connection.execute("PRAGMA key='" + request["key"] + "'")
        connection.execute("BEGIN")
        try:
            next(connection.execute("SELECT count(*) FROM sqlite_master"))
        except apsw.NotADBError:
            reply({"ok": False, "code": "wrong_key"})
            return
        guard.verify_committed_wal(connection, lambda: cancellation.cancelled)
        if cancellation.cancelled:
            raise ProtocolError()
        if guard.counts["read_opens"] < 3 or guard.counts["callback_errors"]:
            raise RuntimeError("guard not active")
        connection.setrowtrace(as_row)
        connection.set_authorizer(authorizer)
        reply({"ok": True, "counts": guard.counts})

        while (request := read_request()) is not None:
            if cancellation.cancelled:
                break
            operation = request.get("op")
            if operation == "close" and set(request) == {"op"}:
                close_counts = dict(guard.counts)
                closing = True
                break
            try:
                response = None
                if operation == "query" and set(request) == {"op", "sql", "parameters"}:
                    if not permitted_sql(request["sql"]) or len(cursors) >= MAX_CURSORS:
                        raise ProtocolError()
                    parameters = decode_value(request["parameters"])
                    if not isinstance(parameters, list) or len(parameters) > 64:
                        raise ProtocolError()
                    cursor = connection.cursor()
                    cursor.execute(request["sql"], parameters)
                    next_id += 1
                    cursors[next_id] = cursor
                    response = {"ok": True, "cursor": next_id}
                elif operation == "fetch" and set(request) == {"op", "cursor"}:
                    identifier = request["cursor"]
                    if not isinstance(identifier, int) or identifier not in cursors:
                        raise ProtocolError()
                    cursor = cursors[identifier]
                    rows = []
                    size = 128
                    exhausted = False
                    for _ in range(64):
                        try:
                            row = pending.pop(identifier) if identifier in pending else next(cursor)
                        except StopIteration:
                            exhausted = True
                            break
                        encoded = encode_value(row)
                        row_size = len(json.dumps(encoded, ensure_ascii=False).encode("utf-8"))
                        if row_size > MAX_REPLY - 2048:
                            raise ProtocolError()
                        if size + row_size > MAX_REPLY - 2048:
                            pending[identifier] = row
                            break
                        rows.append(encoded)
                        size += row_size
                    if exhausted:
                        del cursors[identifier]
                        pending.pop(identifier, None)
                        cursor.close()
                    response = {"ok": True, "rows": rows, "done": exhausted}
                else:
                    raise ProtocolError()
                if guard.counts["callback_errors"] or any(guard.counts[name] for name in
                       ("denied_write", "denied_truncate", "denied_delete", "denied_mapping")):
                    raise RuntimeError("source guard boundary failed")
                reply(response)
            except ProtocolError:
                reply({"ok": False, "code": "request_rejected"})
            except apsw.Error as error:
                boundary_failure = (
                    getattr(error, "result", None) != apsw.SQLITE_ERROR
                    or guard.counts["callback_errors"]
                    or any(guard.counts[name] for name in
                           ("denied_write", "denied_truncate", "denied_delete", "denied_mapping"))
                )
                if boundary_failure:
                    reply({"ok": False, "code": "source_unavailable"})
                    return
                reply({"ok": False, "code": "query_failed"})
            except BaseException:
                reply({"ok": False, "code": "source_unavailable"})
                return
    except SourceShmUnavailable:
        reply({"ok": False, "code": "source_shm_unavailable"})
    except (ProtocolError, ValueError, TypeError, json.JSONDecodeError):
        reply({"ok": False, "code": "request_rejected"})
    except BaseException:
        reply({"ok": False, "code": "source_unavailable"})
    finally:
        clean = True
        if cancellation is not None:
            cancellation.detach()  # No interrupt may race the subsequent native close.
        if connection is not None:
            try:
                connection.set_authorizer(None)
            except BaseException:
                pass
            try:
                connection.execute("ROLLBACK")
            except BaseException:
                pass
            try:
                connection.close()
            except BaseException:
                clean = False
        if guard is not None:
            try:
                guard.close()
            except BaseException:
                clean = False
        if cancellation is not None:
            try:
                cancellation.close()
            except BaseException:
                clean = False
        watchdog.cancel()
        watchdog.join(timeout=1)
        if watchdog.is_alive() or not clean:
            os._exit(125)  # Never report a successful exit while cleanup is unconfirmed.
    if closing:
        reply({"ok": True, "counts": close_counts})


if __name__ == "__main__":
    run()
