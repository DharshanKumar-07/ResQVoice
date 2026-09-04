"""Backend import facade for the canonical shared ResQVoice schema.

The backend is commonly launched from ``backend/``. Add the repository root to
the module search path once, then re-export the actual shared classes rather than
maintaining a second copy of them here.
"""
from pathlib import Path
import sys

_REPOSITORY_ROOT = str(Path(__file__).resolve().parents[2])
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)

from shared.python.schemas import *  # noqa: E402,F401,F403
