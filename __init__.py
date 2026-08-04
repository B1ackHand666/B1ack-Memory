"""Hermes directory-plugin entry point."""

from .b1ack_memory.plugin import get_service
from .b1ack_memory.provider import B1ackMemoryProvider


def register(ctx):
    """Register the provider with Hermes' dedicated memory plugin loader."""
    ctx.register_memory_provider(B1ackMemoryProvider(get_service()))

__all__ = ["register"]
