from __future__ import annotations

import atexit
import threading
from typing import Any

from .provider import B1ackMemoryProvider
from .service import MemoryService

_lock = threading.Lock()
_service: MemoryService | None = None


def get_service(*, start_background: bool = False) -> MemoryService:
    global _service
    with _lock:
        if _service is None:
            _service = MemoryService(start_background=start_background)
            atexit.register(_service.shutdown)
        elif start_background:
            _service.start_background()
        return _service


def register(ctx: Any) -> None:
    provider = B1ackMemoryProvider(get_service())
    if hasattr(ctx, "register_memory_provider"):
        ctx.register_memory_provider(provider)
