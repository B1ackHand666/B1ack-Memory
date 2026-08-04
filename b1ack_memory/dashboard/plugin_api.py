from pathlib import Path
import sys

_PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(_PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_PARENT))

from b1ack_memory.web import create_router

router = create_router(local_only=False)
