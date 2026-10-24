"""Attachment for an existing FastAPI chat backend: lets an LLM read, edit and test a local project.

Host wiring is `make_router(...)` only; see docs/ATTACH_V0.md.
"""
from .router import make_router

__all__ = ["make_router"]
