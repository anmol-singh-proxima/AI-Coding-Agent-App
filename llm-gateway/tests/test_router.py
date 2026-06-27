"""Tests for app/router.py — source chain selection."""
from unittest.mock import MagicMock

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


class TestSelectChain:
    def test_returns_available_sources_in_priority_order(self):
        sources = [
            _make_source("a", priority=1),
            _make_source("b", priority=2),
        ]
        reg = SourceRegistry(sources)
        chain = select_chain(reg)
        assert [s.name for s in chain] == ["a", "b"]

    def test_disabled_sources_excluded(self):
        sources = [
            _make_source("a", priority=1, enabled=True),
            _make_source("b", priority=2, enabled=False),
            _make_source("c", priority=3, enabled=True),
        ]
        reg = SourceRegistry(sources)
        chain = select_chain(reg)
        assert [s.name for s in chain] == ["a", "c"]

    def test_empty_registry_returns_empty_chain(self):
        reg = SourceRegistry([])
        assert select_chain(reg) == []

    def test_all_disabled_returns_empty_chain(self):
        sources = [_make_source(enabled=False), _make_source(enabled=False)]
        reg = SourceRegistry(sources)
        assert select_chain(reg) == []
