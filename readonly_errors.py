"""Fixed, path-free source boundary errors."""


class SnapshotUnavailable(RuntimeError):
    """The source cannot be read without weakening the read-only boundary."""

