"""Per-source rate-limit accounting (RPM, RPD, 429-cooldown).

All state is in-memory. This is a single-process local tool so no
persistence or locking is needed.
"""
import time
from collections import deque
from datetime import date

from app.sources import Source

_DEFAULT_COOLDOWN = 60.0  # seconds to wait after 429 when no Retry-After header


class SourceLimiter:
    """Tracks per-source request counts and enforces RPM/RPD limits.

    Two independent windows are maintained per source:
    - Rolling 60-second minute window (using monotonic time).
    - Calendar-day window that resets at local midnight.

    A 429 from the upstream can additionally force a cooldown period
    regardless of local counters (the upstream may count differently).
    """

    def __init__(self) -> None:
        # Monotonic timestamps of recent requests, keyed by source name.
        self._minute_ts: dict[str, deque[float]] = {}
        # (date, count) for the current calendar day, keyed by source name.
        self._day_counts: dict[str, tuple[date, int]] = {}
        # Monotonic deadline after which the source is un-blocked.
        self._cooldown_until: dict[str, float] = {}

    # ── public API ────────────────────────────────────────────────────────────

    def can_use(self, source: Source) -> bool:
        """Return False when the source has hit its RPM/RPD cap or is in 429-cooldown."""
        name = source.name
        now = time.monotonic()

        if now < self._cooldown_until.get(name, 0.0):
            return False

        if source.rpm is not None:
            ts = self._minute_ts.get(name, deque())
            recent = sum(1 for t in ts if t > now - 60.0)
            if recent >= source.rpm:
                return False

        if source.rpd is not None:
            _, count = self._current_day_count(name)
            if count >= source.rpd:
                return False

        return True

    def record(self, source: Source) -> None:
        """Increment counters when a request is dispatched to this source."""
        name = source.name
        now = time.monotonic()

        ts = self._minute_ts.setdefault(name, deque())
        while ts and ts[0] <= now - 60.0:
            ts.popleft()
        ts.append(now)

        today = date.today()
        stored_date, count = self._day_counts.get(name, (today, 0))
        count = count + 1 if stored_date == today else 1
        self._day_counts[name] = (today, count)

    def mark_rate_limited(self, source: Source, retry_after: float | None = None) -> None:
        """Block source for `retry_after` seconds (default 60s) after a 429."""
        cooldown = retry_after if retry_after is not None else _DEFAULT_COOLDOWN
        self._cooldown_until[source.name] = time.monotonic() + cooldown

    def status(self, source: Source) -> dict:
        """Return a snapshot of current counters for the source."""
        name = source.name
        now = time.monotonic()

        ts = self._minute_ts.get(name, deque())
        minute_count = sum(1 for t in ts if t > now - 60.0)

        _, day_count = self._current_day_count(name)

        cooldown_until = self._cooldown_until.get(name, 0.0)
        in_cooldown = now < cooldown_until
        cooldown_remaining = round(max(0.0, cooldown_until - now), 1) if in_cooldown else 0.0

        return {
            "minute_count": minute_count,
            "day_count": day_count,
            "in_cooldown": in_cooldown,
            "cooldown_remaining_seconds": cooldown_remaining,
        }

    # ── private helpers ───────────────────────────────────────────────────────

    def _current_day_count(self, name: str) -> tuple[date, int]:
        today = date.today()
        stored_date, count = self._day_counts.get(name, (today, 0))
        return today, (count if stored_date == today else 0)
