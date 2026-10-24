"""Prototype in-process assistant (runs tools inside the host process): LOCAL EXPERIMENT ONLY, not the production path.

Importing this package stays cheap and side-effect free: `make_router` is loaded on first use, so the device side can import
`code_attach.tools` without pulling in the HTTP/engine layers (and without needing the service package at all).
"""

def __getattr__(name):
    if name == "make_router":
        from .router import make_router
        return make_router
    raise AttributeError(f"module 'code_attach' has no attribute {name!r}")

__all__ = ["make_router"]
