"""Tests for app/router.py — source chain selection."""
from app.limiter import SourceLimiter
from app.router import select_chain
from app.sources import Source, SourceRegistry


def _make_source(name="s", priority=1, enabled=True) -> Source:
    return Source(
        name=name,
        base_url="https://api.example.com/v1",
        model="some-model",
        api_key="key",
        rpm=None,
        rpd=None,
        priority=priority,
        enabled=enabled,
    )


def _open_limiter() -> SourceLimiter:
    """A limiter with no limits set — every source passes can_use()."""
    return SourceLimiter()


class TestSelectChain:
    def test_returns_available_sources_in_priority_order(self):
        sources = [_make_source("a", priority=1), _make_source("b", priority=2)]
        reg = SourceRegistry(sources)
        chain = select_chain(reg, _open_limiter())
        assert [s.name for s in chain] == ["a", "b"]

    def test_disabled_sources_excluded(self):
        sources = [
            _make_source("a", priority=1, enabled=True),
            _make_source("b", priority=2, enabled=False),
            _make_source("c", priority=3, enabled=True),
        ]
        reg = SourceRegistry(sources)
        chain = select_chain(reg, _open_limiter())
        assert [s.name for s in chain] == ["a", "c"]

    def test_empty_registry_returns_empty_chain(self):
        reg = SourceRegistry([])
        assert select_chain(reg, _open_limiter()) == []

    def test_all_disabled_returns_empty_chain(self):
        sources = [_make_source(enabled=False), _make_source(enabled=False)]
        reg = SourceRegistry(sources)
        assert select_chain(reg, _open_limiter()) == []

    def test_limiter_blocked_source_excluded(self):
        a = _make_source("a", priority=1)
        b = _make_source("b", priority=2)
        reg = SourceRegistry([a, b])
        limiter = SourceLimiter()
        limiter.mark_rate_limited(b)  # put "b" in cooldown
        chain = select_chain(reg, limiter)
        assert [s.name for s in chain] == ["a"]

    def test_all_limiter_blocked_returns_empty_chain(self):
        sources = [_make_source("a"), _make_source("b")]
        reg = SourceRegistry(sources)
        limiter = SourceLimiter()
        for s in sources:
            limiter.mark_rate_limited(s)
        assert select_chain(reg, limiter) == []

    def test_limiter_rpm_cap_excludes_source(self):
        src = _make_source(name="s", priority=1)
        src_with_rpm = Source(
            name="s", base_url=src.base_url, model=src.model, api_key=src.api_key,
            rpm=1, rpd=None, priority=1, enabled=True,
        )
        reg = SourceRegistry([src_with_rpm])
        limiter = SourceLimiter()
        limiter.record(src_with_rpm)  # hit the rpm=1 cap
        assert select_chain(reg, limiter) == []
