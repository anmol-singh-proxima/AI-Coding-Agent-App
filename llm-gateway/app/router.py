from app.sources import Source, SourceRegistry


def select_chain(registry: SourceRegistry) -> list[Source]:
    """Return sources to attempt for a request, ordered by priority.

    M4: returns all enabled sources; the caller tries them in order and skips
    any that fail. M5 will add a limiter parameter so over-quota sources are
    excluded here before the first attempt.
    """
    return registry.available_sources()
