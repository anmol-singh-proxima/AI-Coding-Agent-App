from app.limiter import SourceLimiter
from app.sources import Source, SourceRegistry


def select_chain(registry: SourceRegistry, limiter: SourceLimiter) -> list[Source]:
    """Return sources to attempt, in priority order.

    Excludes disabled sources (via registry) and sources that are over their
    RPM/RPD cap or in 429-cooldown (via limiter).
    """
    return [s for s in registry.available_sources() if limiter.can_use(s)]
