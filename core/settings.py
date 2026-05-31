"""Runtime settings for arcticdb-viewer.

The single most important setting is **read-only mode**, which is ON by
default. This makes the viewer safe for production market data: browsing,
charting, and every analysis feature are guaranteed never to write to
ArcticDB. Mutations are only possible when the operator explicitly opts in
by launching with ``ADBVIEW_READONLY=0``.

Both the web UI and the MCP server import the same guard, so there is a
single chokepoint that protects every backend.
"""
from __future__ import annotations

import os

# Values that disable read-only mode (i.e. permit writes).
_FALSEY = {"0", "false", "no", "off", "disable", "disabled", ""}


class ReadOnlyError(RuntimeError):
    """Raised when a write is attempted while the viewer is read-only."""

    def __init__(self, op: str = "write"):
        # Keep the message ASCII-only: it is sometimes placed in an HTTP
        # header (HX-Trigger), which must be latin-1 encodable.
        super().__init__(
            f"Read-only mode is enabled - '{op}' is blocked to protect your data. "
            f"Restart with the environment variable ADBVIEW_READONLY=0 to allow writes."
        )
        self.op = op


def _env_read_only() -> bool:
    """Resolve read-only from the environment. Default: True (safe)."""
    raw = os.getenv("ADBVIEW_READONLY")
    if raw is None:
        return True  # safe by default
    return raw.strip().lower() not in _FALSEY


# Resolved once at import. Writes are locked unless explicitly opted out of.
_READ_ONLY: bool = _env_read_only()


def is_read_only() -> bool:
    return _READ_ONLY


def set_read_only(value: bool) -> None:
    """Programmatic override (used by tests / the run_mcp --read-only flag)."""
    global _READ_ONLY
    _READ_ONLY = bool(value)


def guard_write(op: str = "write") -> None:
    """Raise :class:`ReadOnlyError` if the viewer is read-only.

    Called at the top of every mutating function in :mod:`core.operations`,
    so neither the web routes nor the MCP tools can bypass it.
    """
    if _READ_ONLY:
        raise ReadOnlyError(op)
