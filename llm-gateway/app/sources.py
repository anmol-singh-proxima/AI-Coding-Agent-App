from dataclasses import dataclass


@dataclass
class Source:
    name: str
    base_url: str
    model: str
    api_key: str       # empty string for keyless sources (e.g. local Ollama)
    rpm: int | None    # requests-per-minute limit; None = unlimited
    rpd: int | None    # requests-per-day limit; None = unlimited
    priority: int      # lower = tried first
    enabled: bool


class SourceRegistry:
    """Holds all configured sources sorted by priority.

    M5 will extend available_sources() to also exclude sources that have
    hit their local RPM/RPD counters via limiter.py.
    """

    def __init__(self, sources: list[Source]) -> None:
        self._sources = sorted(sources, key=lambda s: s.priority)

    def available_sources(self) -> list[Source]:
        """Return enabled sources in priority order."""
        return [s for s in self._sources if s.enabled]

    def all_sources(self) -> list[Source]:
        return list(self._sources)
