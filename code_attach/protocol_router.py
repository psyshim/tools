"""In-process prototype entrance to the contract surface. The router itself lives in coding_service.protocol_router.

This wrapper builds the prototype Engine (which runs tools in THIS process: local experiment only) and mounts the shared
router over it. Production hosts never import this module; they install host_client and talk to the coding service.
"""
from coding_service.protocol_router import make_protocol_router_for

from .engine import Engine

def make_protocol_router(complete, authorize, resolve_workspace, test_commands=None, max_tasks=64, test_timeout=180, event_sink=None,
                         test_env=None, recycle_dir=None, allow_mutation=False, *, prototype=False):
    """Same collaborators as make_router. Read/propose only unless allow_mutation=True (local experiment)."""
    if not prototype:
        raise RuntimeError("this runs the tools inside the host process and is a local experiment only; production hosts use host_client "
                           "against the coding service (docs/HOST_KIT.md). Pass prototype=True to acknowledge that.")
    engine = Engine(complete, test_commands, max_tasks, test_timeout, event_sink, test_env, recycle_dir=recycle_dir, allow_mutation=allow_mutation)
    return make_protocol_router_for(engine, authorize, resolve_workspace)
