"""Model layer: the gateway to external LLM providers (impl-plan §6.8)."""

from girder.models.gateway import (
    Message,
    ModelError,
    ModelGateway,
    ModelResponse,
    ModelToolCall,
    Usage,
)

__all__ = [
    "Message",
    "ModelError",
    "ModelGateway",
    "ModelResponse",
    "ModelToolCall",
    "Usage",
]
