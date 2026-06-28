"""Tests for app/limiter.py — per-source rate-limit accounting."""
from datetime import date
from unittest.mock import patch

from app.limiter import SourceLimiter, _DEFAULT_COOLDOWN
from app.sources import Source


def _make_source(name="test", rpm: int | None = None, rpd: int | None = None) -> Source:
    return Source(
        name=name,
        base_url="https://api.example.com/v1",
        model="some-model",
        api_key="key",
        rpm=rpm,
        rpd=rpd,
        priority=1,
        enabled=True,
    )


class TestCanUse:
    def test_fresh_source_is_available(self):
        limiter = SourceLimiter()
        assert limiter.can_use(_make_source(rpm=10, rpd=100)) is True

    def test_no_limits_always_available(self):
        limiter = SourceLimiter()
        src = _make_source(rpm=None, rpd=None)
        for _ in range(100):
            limiter.record(src)
        assert limiter.can_use(src) is True

    def test_rpm_limit_blocks_at_cap(self):
        limiter = SourceLimiter()
        src = _make_source(rpm=3)
        for _ in range(3):
            limiter.record(src)
        assert limiter.can_use(src) is False

    def test_rpm_limit_allows_below_cap(self):
        limiter = SourceLimiter()
        src = _make_source(rpm=3)
        for _ in range(2):
            limiter.record(src)
        assert limiter.can_use(src) is True

    def test_rpm_resets_after_60_seconds(self):
        limiter = SourceLimiter()
        src = _make_source(rpm=3)
        with patch("app.limiter.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            for _ in range(3):
                limiter.record(src)
            assert limiter.can_use(src) is False
            mock_time.monotonic.return_value = 61.0
            assert limiter.can_use(src) is True

    def test_rpd_limit_blocks_at_cap(self):
        limiter = SourceLimiter()
        src = _make_source(rpd=2)
        limiter.record(src)
        limiter.record(src)
        assert limiter.can_use(src) is False

    def test_rpd_limit_allows_below_cap(self):
        limiter = SourceLimiter()
        src = _make_source(rpd=2)
        limiter.record(src)
        assert limiter.can_use(src) is True

    def test_rpd_resets_on_new_day(self):
        limiter = SourceLimiter()
        src = _make_source(rpd=2)
        today = date(2026, 6, 27)
        tomorrow = date(2026, 6, 28)
        with patch("app.limiter.date") as mock_date:
            mock_date.today.return_value = today
            limiter.record(src)
            limiter.record(src)
            assert limiter.can_use(src) is False
            mock_date.today.return_value = tomorrow
            assert limiter.can_use(src) is True

    def test_cooldown_blocks_source(self):
        limiter = SourceLimiter()
        src = _make_source()
        with patch("app.limiter.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            limiter.mark_rate_limited(src, retry_after=30.0)
            mock_time.monotonic.return_value = 15.0
            assert limiter.can_use(src) is False

    def test_cooldown_unblocks_after_expiry(self):
        limiter = SourceLimiter()
        src = _make_source()
        with patch("app.limiter.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            limiter.mark_rate_limited(src, retry_after=30.0)
            mock_time.monotonic.return_value = 31.0
            assert limiter.can_use(src) is True

    def test_independent_sources_do_not_interfere(self):
        limiter = SourceLimiter()
        a = _make_source("a", rpm=2)
        b = _make_source("b", rpm=2)
        limiter.record(a)
        limiter.record(a)
        assert limiter.can_use(a) is False
        assert limiter.can_use(b) is True


class TestRecord:
    def test_increments_minute_count(self):
        limiter = SourceLimiter()
        src = _make_source()
        limiter.record(src)
        limiter.record(src)
        assert limiter.status(src)["minute_count"] == 2

    def test_increments_day_count(self):
        limiter = SourceLimiter()
        src = _make_source()
        limiter.record(src)
        limiter.record(src)
        assert limiter.status(src)["day_count"] == 2

    def test_day_count_resets_on_new_day(self):
        limiter = SourceLimiter()
        src = _make_source()
        today = date(2026, 6, 27)
        tomorrow = date(2026, 6, 28)
        with patch("app.limiter.date") as mock_date:
            mock_date.today.return_value = today
            limiter.record(src)
            limiter.record(src)
            mock_date.today.return_value = tomorrow
            limiter.record(src)
            assert limiter.status(src)["day_count"] == 1

    def test_old_minute_timestamps_pruned(self):
        limiter = SourceLimiter()
        src = _make_source()
        with patch("app.limiter.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            limiter.record(src)
            limiter.record(src)
            mock_time.monotonic.return_value = 61.0
            limiter.record(src)
            assert limiter.status(src)["minute_count"] == 1


class TestMarkRateLimited:
    def test_uses_provided_retry_after(self):
        limiter = SourceLimiter()
        src = _make_source()
        with patch("app.limiter.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            limiter.mark_rate_limited(src, retry_after=120.0)
            mock_time.monotonic.return_value = 119.0
            assert limiter.can_use(src) is False
            mock_time.monotonic.return_value = 121.0
            assert limiter.can_use(src) is True

    def test_uses_default_cooldown_when_no_retry_after(self):
        limiter = SourceLimiter()
        src = _make_source()
        with patch("app.limiter.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            limiter.mark_rate_limited(src)
            mock_time.monotonic.return_value = _DEFAULT_COOLDOWN - 1
            assert limiter.can_use(src) is False
            mock_time.monotonic.return_value = _DEFAULT_COOLDOWN + 1
            assert limiter.can_use(src) is True

    def test_overrides_previous_cooldown(self):
        limiter = SourceLimiter()
        src = _make_source()
        with patch("app.limiter.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            limiter.mark_rate_limited(src, retry_after=10.0)
            mock_time.monotonic.return_value = 5.0
            limiter.mark_rate_limited(src, retry_after=60.0)
            mock_time.monotonic.return_value = 60.0
            assert limiter.can_use(src) is False
            mock_time.monotonic.return_value = 66.0
            assert limiter.can_use(src) is True


class TestStatus:
    def test_zero_counts_for_fresh_source(self):
        limiter = SourceLimiter()
        s = limiter.status(_make_source())
        assert s["minute_count"] == 0
        assert s["day_count"] == 0
        assert s["in_cooldown"] is False
        assert s["cooldown_remaining_seconds"] == 0.0

    def test_correct_counts_after_records(self):
        limiter = SourceLimiter()
        src = _make_source()
        limiter.record(src)
        limiter.record(src)
        s = limiter.status(src)
        assert s["minute_count"] == 2
        assert s["day_count"] == 2

    def test_shows_cooldown_while_active(self):
        limiter = SourceLimiter()
        src = _make_source()
        with patch("app.limiter.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            limiter.mark_rate_limited(src, retry_after=45.0)
            mock_time.monotonic.return_value = 10.0
            s = limiter.status(src)
        assert s["in_cooldown"] is True
        assert s["cooldown_remaining_seconds"] == 35.0

    def test_cooldown_cleared_after_expiry(self):
        limiter = SourceLimiter()
        src = _make_source()
        with patch("app.limiter.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            limiter.mark_rate_limited(src, retry_after=10.0)
            mock_time.monotonic.return_value = 20.0
            s = limiter.status(src)
        assert s["in_cooldown"] is False
        assert s["cooldown_remaining_seconds"] == 0.0
