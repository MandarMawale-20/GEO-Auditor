"""Shared utilities used across engines and services."""
from __future__ import annotations

from datetime import datetime, timezone


def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime, tolerating trailing ``Z`` and offsets.

    Timezone-naive timestamps are assumed to be UTC. Returns ``None`` for
    empty or malformed input.
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

