"""
Shared utilities used across engines and services.
"""
from __future__ import annotations

from datetime import datetime, timezone


def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string robustly.

    Handles:
      * Trailing ``Z`` (UTC designator).
      * Explicit timezone offsets (``+05:30``, ``-08:00``).
      * Timezone-naive values (assumed UTC).
      * Malformed / empty input (returns ``None``).

    Policy: timezone-naive timestamps are assumed to be UTC for consistency.
    """
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed_dt = datetime.fromisoformat(normalized)
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        return parsed_dt
    except (ValueError, TypeError):
        return None
