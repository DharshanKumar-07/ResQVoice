"""Small structured-event logger; deliberately excludes request bodies and secrets."""
import json
from datetime import datetime, timezone
from typing import Any


def log_event(event: str, **fields: Any) -> None:
    safe = {key: value for key, value in fields.items() if "secret" not in key.lower() and "token" not in key.lower()}
    safe["event"] = event
    safe["timestamp"] = datetime.now(timezone.utc).isoformat()
    print(f"[{event}] {json.dumps(safe, default=str, sort_keys=True)}")
