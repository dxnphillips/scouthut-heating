"""Rolling audit log of decisions, learning samples and outcomes.

The optimum-start seeds, clamps and thresholds were chosen from textbook
figures, not from this building. Every decision the controller makes and
every sample its learning accepts or rejects is appended here as a small
JSON-safe event, persisted across restarts, and exported through the
integration's diagnostics download — so a few weeks of real behaviour can be
analysed offline and the constants re-derived from the hut's actual physics.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any

# Bounded so the persisted snapshot and the diagnostics download stay small.
# Normal traffic is a handful of events per day per category, so this holds
# several weeks — long enough to cover a full booking cycle in every season.
MAX_EVENTS = 500


class AuditLog:
    """A bounded, JSON-safe ring buffer of controller events."""

    def __init__(self, maxlen: int = MAX_EVENTS) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def record(self, kind: str, when: datetime, **data: Any) -> None:
        """Append one event. None values are dropped, floats rounded."""
        event: dict[str, Any] = {"t": when.isoformat(timespec="seconds"), "event": kind}
        for key, value in data.items():
            if value is None:
                continue
            if isinstance(value, float):
                value = round(value, 2)
            event[key] = value
        self._events.append(event)

    def to_list(self) -> list[dict[str, Any]]:
        return list(self._events)

    def load(self, items: Any) -> None:
        """Restore persisted events (oldest first), ignoring malformed data."""
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict):
                self._events.append(item)

    def __len__(self) -> int:
        return len(self._events)
