import logging
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from app.sources import Source

logger = logging.getLogger(__name__)

# config.yaml lives at the project root, one level above this package.
_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def load_config(config_path: Path = _DEFAULT_CONFIG_PATH) -> list[Source]:
    """Read sources from config.yaml and resolve API keys from the environment.

    Design decisions:
    - Loads .env before reading env vars so the file is respected when the
      server starts without keys exported in the shell.
    - A source whose api_key_env names a missing or empty env var is disabled
      with a warning rather than crashing — a missing Gemini key should not
      prevent OpenRouter from working.
    - base_url trailing slashes are stripped so callers can safely append paths.
    """
    load_dotenv()

    if not config_path.exists():
        logger.error("config.yaml not found at %s — no sources will be available", config_path)
        return []

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    sources: list[Source] = []
    for entry in raw.get("sources", []):
        name = entry.get("name", "<unnamed>")
        key_env = entry.get("api_key_env", "")
        api_key = os.getenv(key_env, "") if key_env else ""

        enabled = entry.get("enabled", True)
        if key_env and not api_key:
            logger.warning(
                "Source '%s': env var %s is not set — disabling this source",
                name,
                key_env,
            )
            enabled = False

        sources.append(Source(
            name=name,
            base_url=entry["base_url"].rstrip("/"),
            model=entry["model"],
            api_key=api_key,
            rpm=entry.get("rpm"),
            rpd=entry.get("rpd"),
            priority=entry.get("priority", 99),
            enabled=enabled,
        ))

    enabled_count = sum(1 for s in sources if s.enabled)
    logger.info("Loaded %d source(s) from config (%d enabled)", len(sources), enabled_count)
    return sources
