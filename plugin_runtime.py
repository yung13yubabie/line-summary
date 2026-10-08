"""In-flight-only request coalescing and process-local limits; no result cache."""
import asyncio
from collections import deque
import hashlib
import json
import math
import time


class RequestGate:
    def __init__(self, requests_per_minute=10, min_interval_seconds=2.0, clock=time.monotonic):
        self.requests_per_minute = requests_per_minute
        self.min_interval_seconds = min_interval_seconds
        self.clock = clock
        self._lock = asyncio.Lock()
        self._starts = deque()
        self._last_start = None
        self._pending = {}
        self._producers = set()

    async def _produce(self, key, future, operation):
        try:
            result = await operation()
            future.set_result((result, None, None))
        except asyncio.CancelledError:
            if not future.done():
                future.set_result((None, "request_cancelled", None))
            raise
        except Exception:
            # Never forward backend exception text, paths, queries or rows.
            future.set_result((None, "source_error", None))
        finally:
            async with self._lock:
                self._pending.pop(key, None)

    async def run(self, name, arguments, operation):
        """Return (payload, refusal, retry_after); no completed result is cached.

        Identical concurrent requests share only the in-flight computation.
        Every accepted caller consumes a rate slot and must separately charge
        output budgets. Caller cancellation cannot release the single backend
        slot while a to_thread read still runs.
        """
        key = hashlib.sha256(json.dumps([name, arguments], sort_keys=True, ensure_ascii=True).encode()).digest()
        async with self._lock:
            now = self.clock()
            while self._starts and self._starts[0] <= now - 60:
                self._starts.popleft()
            if len(self._starts) >= self.requests_per_minute:
                return None, "rate_limited", max(0.001, 60 - (now - self._starts[0]))
            future = self._pending.get(key)
            if future is None:
                if self._pending:
                    return None, "busy", max(1.0, self.min_interval_seconds)
                if self._last_start is not None and now - self._last_start < self.min_interval_seconds:
                    return None, "rate_limited", self.min_interval_seconds - (now - self._last_start)
                future = asyncio.get_running_loop().create_future()
                self._pending[key] = future
                self._last_start = now
                task = asyncio.create_task(self._produce(key, future, operation))
                self._producers.add(task)
                task.add_done_callback(self._producers.discard)
            self._starts.append(now)
        # A cancelled MCP caller does not cancel the shared computation or the
        # future of another caller. Entries disappear as soon as reading ends.
        return await asyncio.shield(future)


def retry_seconds(value):
    return None if value is None else math.ceil(value * 1000) / 1000
