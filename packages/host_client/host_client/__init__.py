"""Powerless host client for the coding service. Depends only on the standard library and the `protocol` package."""
from .errors import AgentError, AgentUnavailable, ProtocolRefused
from .events import SafeDisplayEvent
from .gateway import AgentGateway, Available, CancelResult, DecisionResult, PollResult, TaskHandle, Unavailable

__version__ = "1.0.0"
__all__ = ["AgentGateway", "AgentError", "AgentUnavailable", "ProtocolRefused", "SafeDisplayEvent", "Available", "Unavailable", "TaskHandle", "CancelResult", "DecisionResult", "PollResult"]
