"""
Global application context access.

This provides a controlled singleton-style accessor for the current
application context without requiring explicit dependency passing
through every constructor.
"""

from typing import Optional

from .context import Moin2GitContext

_ctx: Optional[Moin2GitContext] = None


def init_context(ctx: Moin2GitContext):
    """
    Initialize the global application context.

    Must be called once during CLI startup.
    """
    global _ctx

    if _ctx is not None:
        raise RuntimeError("Application context already initialized")

    _ctx = ctx


def get_context() -> Moin2GitContext:
    """
    Return the global application context.
    """
    if _ctx is None:
        raise RuntimeError("Application context not initialized")

    return _ctx
