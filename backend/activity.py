from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


SCHEMA = "infertree.activity.v1"
TERMINAL_STATES = {"completed", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class ActivitySpan:
    kind: str
    action: str
    title: str
    parent_id: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"act_{uuid.uuid4().hex}")
    started_at: str = field(default_factory=_now)
    _started: float = field(default_factory=time.perf_counter, repr=False)

    def event(
        self,
        state: str,
        message: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if state not in {"started", *TERMINAL_STATES}:
            raise ValueError(f"Unsupported activity state: {state}")
        payload: Dict[str, Any] = {
            "schema": SCHEMA,
            "id": self.id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "action": self.action,
            "state": state,
            "title": self.title,
            "message": message or self.title,
            "started_at": self.started_at,
            "timestamp": _now(),
            "detail": {**self.detail, **(detail or {})},
        }
        if state in TERMINAL_STATES:
            payload["duration_ms"] = max(0, round((time.perf_counter() - self._started) * 1000))
        return payload
