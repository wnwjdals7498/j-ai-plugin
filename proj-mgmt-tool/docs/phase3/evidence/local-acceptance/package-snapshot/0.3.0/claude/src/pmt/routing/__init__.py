"""Deterministic model and execution-route selection."""

from .service import (FILE_OPERATIONS, READ_OPERATIONS, WRITE_OPERATIONS,
                      handle, select_route)

__all__ = ["FILE_OPERATIONS", "READ_OPERATIONS", "WRITE_OPERATIONS", "handle", "select_route"]
