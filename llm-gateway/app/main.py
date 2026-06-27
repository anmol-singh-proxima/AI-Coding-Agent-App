import logging
import os
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="LLM Gateway")

_UPSTREAM_BASE = "https://openrouter.ai/api/v1"
_UPSTREAM_KEY = os.getenv("OPENROUTER_API_KEY", "")

# Ordered fallback list of free models strong at coding and technical reasoning.
# The gateway tries each in sequence whenever the previous one is rate-limited
# upstream. Add or reorder freely; remove the ":free" suffix to use a paid slot.
_MODELS: list[str] = [
    "qwen/qwen3-coder:free",                  # Alibaba — purpose-built for coding, 1M ctx
    "google/gemma-4-31b-it:free",             # Google — strong general coding, 262K ctx
    "meta-llama/llama-3.3-70b-instruct:free", # Meta — reliable, well-tested, 131K ctx
    "nvidia/nemotron-3-super-120b:free",      # NVIDIA — heavy reasoning, 1M ctx
    "openai/gpt-oss-120b:free",               # OpenAI open-source, 120B params, 131K ctx
]


class ChatRequest(BaseModel):
    # Declared fields are shown in Swagger; any extra OpenAI-compatible fields
    # (tools, top_p, etc.) are allowed and forwarded unchanged.
    model_config = ConfigDict(extra="allow")

    messages: list[dict[str, Any]]
    model: str | None = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


def _is_upstream_rate_limit(response: httpx.Response) -> bool:
    """True when OpenRouter reports that the model itself is throttled by its provider.

    This is distinct from hitting OpenRouter's own per-key limit (which would
    also be a 429 but with a different message). We only want to fall over to
    the next model for the provider-side throttle, not for our own quota issues.
    """
    if response.status_code != 429:
        return False
    try:
        body = response.json()
        message = body.get("error", {}).get("message", "")
        return "Provider returned error" in message or "rate-limited upstream" in message.lower()
    except Exception:
        return False


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatRequest) -> JSONResponse:
    if not _UPSTREAM_KEY:
        logger.error("OPENROUTER_API_KEY is not set")
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "OPENROUTER_API_KEY is not configured",
                    "type": "configuration_error",
                }
            },
        )

    # Streaming is not supported until M2.
    if body.stream:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "Streaming is not supported in this version. Set stream=false.",
                    "type": "unsupported_operation",
                }
            },
        )

    headers = {
        "Authorization": f"Bearer {_UPSTREAM_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:8080",
        "X-Title": "LLM Gateway",
    }

    skipped: list[str] = []
    last_rate_limit_body: dict | None = None

    for model in _MODELS:
        payload = body.model_dump(exclude_none=True)
        payload["model"] = model  # override with the candidate model

        logger.info("Trying model: %s", model)

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{_UPSTREAM_BASE}/chat/completions",
                    json=payload,
                    headers=headers,
                )
        except httpx.TimeoutException:
            logger.warning("Timeout on model %s, skipping", model)
            skipped.append(f"{model} (timeout)")
            continue
        except httpx.RequestError as exc:
            logger.warning("Connection error on model %s: %s, skipping", model, exc)
            skipped.append(f"{model} (connection error)")
            continue

        if _is_upstream_rate_limit(response):
            logger.warning("Model %s is rate-limited upstream, trying next", model)
            skipped.append(f"{model} (upstream rate limit)")
            last_rate_limit_body = response.json()
            continue

        # Any other response — success or a non-rate-limit error — is returned
        # directly. We don't fall over on 4xx client errors because retrying with
        # a different model won't fix a malformed request.
        logger.info("Model %s served the request (HTTP %d)", model, response.status_code)
        return JSONResponse(content=response.json(), status_code=response.status_code)

    # Every model in the list was rate-limited or unreachable.
    logger.error("All models exhausted. Skipped: %s", skipped)
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "message": "All models are currently rate-limited or unreachable. Try again in a moment.",
                "type": "all_models_exhausted",
                "attempted": skipped,
                "last_upstream_error": last_rate_limit_body,
            }
        },
    )
