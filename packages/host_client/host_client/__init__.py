"""Powerless host client for the coding service. Depends only on the standard library and the `protocol` package."""
from .errors import AgentError, AgentUnavailable, ProtocolRefused
from .events import SafeDisplayEvent
from .wrapper import ROUTES, CodingAdapter, CodingEndpoints, HostEnvironment, Reply, Route
from .gateway import AgentGateway, Available, CancelResult, DecisionResult, PollResult, TaskHandle, Unavailable

__version__ = "1.1.0"
__all__ = ["AgentGateway", "AgentError", "AgentUnavailable", "ProtocolRefused", "SafeDisplayEvent", "Available", "Unavailable", "TaskHandle", "CancelResult", "DecisionResult", "PollResult",
           "HostEnvironment", "CodingEndpoints", "CodingAdapter", "ROUTES", "Route", "Reply"]
