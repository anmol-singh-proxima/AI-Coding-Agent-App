"""Tests for app/sources.py — Source dataclass and SourceRegistry."""
from app.sources import Source, SourceRegistry


def _make_source(name="s", priority=1, enabled=True, api_key="key") -> Source:
    return Source(
        name=name,
        base_url="https://api.example.com/v1",
        model="some-model",
        api_key=api_key,
        rpm=None,
        rpd=None,
        priority=priority,
        enabled=enabled,
    )


class TestSourceRegistry:
    def test_available_sources_returns_only_enabled(self):
        sources = [
            _make_source("a", priority=1, enabled=True),
            _make_source("b", priority=2, enabled=False),
            _make_source("c", priority=3, enabled=True),
        ]
        reg = SourceRegistry(sources)
        available = reg.available_sources()
        assert [s.name for s in available] == ["a", "c"]

    def test_available_sources_sorted_by_priority(self):
        sources = [
            _make_source("high", priority=10),
            _make_source("low", priority=1),
            _make_source("mid", priority=5),
        ]
        reg = SourceRegistry(sources)
        names = [s.name for s in reg.available_sources()]
        assert names == ["low", "mid", "high"]

    def test_all_sources_returns_every_source_including_disabled(self):
        sources = [_make_source("a", enabled=True), _make_source("b", enabled=False)]
        reg = SourceRegistry(sources)
        assert len(reg.all_sources()) == 2

    def test_empty_registry_returns_empty_lists(self):
        reg = SourceRegistry([])
        assert reg.available_sources() == []
        assert reg.all_sources() == []

    def test_single_disabled_source_returns_empty_available(self):
        reg = SourceRegistry([_make_source(enabled=False)])
        assert reg.available_sources() == []

    def test_constructor_does_not_mutate_input_list(self):
        sources = [_make_source("a", priority=2), _make_source("b", priority=1)]
        original_order = [s.name for s in sources]
        SourceRegistry(sources)
        assert [s.name for s in sources] == original_order
