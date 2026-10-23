"""Bounded in-memory stdout/stderr leak detector for synthetic tests."""
import re

LOCAL_PATH = re.compile(rb'(?<![A-Za-z0-9_])[A-Za-z]:[\\/]{1,2}')


class WireBoundary:
    def __init__(self, secrets):
        self.secrets = tuple(secrets)
        self.keep = max(8, max(map(len, self.secrets), default=0)) - 1
        self.tail = {'stdout': b'', 'stderr': b''}

    def feed(self, label, raw):
        combined = self.tail[label] + raw
        if any(value in combined for value in self.secrets) or LOCAL_PATH.search(combined):
            raise ValueError('Private value or local path reflected on wire')
        self.tail[label] = combined[-self.keep:]
