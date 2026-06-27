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

# M1: single hardcoded upstream — OpenRouter
_UPSTREAM_BASE = "https://openrouter.ai/api/v1"
_UPSTREAM_MODEL = "qwen/qwen3-coder:free"
_UPSTREAM_KEY = os.getenv("OPENROUTER_API_KEY", "")


class ChatRequest(BaseModel):
    # Declare the fields Swagger should show; extra fields pass through unchanged.
    model_config = ConfigDict(extra="allow")

    messages: list[dict[str, Any]]
    model: str | None = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatRequest) -> JSONResponse:
    if not _UPSTREAM_KEY:
        logger.error("OPENROUTER_API_KEY is not set")
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "OPENROUTER_API_KEY is not configured", "type": "configuration_error"}},
        )

    # Streaming is not supported in M1 — reject cleanly so the caller knows.
    if body.stream:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "Streaming is not supported in this version. Set stream=false.", "type": "unsupported_operation"}},
        )

    # Build the forwarded payload: start from all fields the caller sent,
    # then override the model id with the upstream's actual model.
    payload = body.model_dump(exclude_none=True)
    payload["model"] = _UPSTREAM_MODEL

    headers = {
        "Authorization": f"Bearer {_UPSTREAM_KEY}",
        "Content-Type": "application/json",
        # OpenRouter recommends these; not required but good practice.
        "HTTP-Referer": "http://localhost:8080",
        "X-Title": "LLM Gateway",
    }

    logger.info("Forwarding request to OpenRouter (model=%s)", _UPSTREAM_MODEL)

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{_UPSTREAM_BASE}/chat/completions",
                json=payload,
                headers=headers,
            )
    except httpx.TimeoutException:
        logger.error("Request to OpenRouter timed out")
        return JSONResponse(
            status_code=504,
            content={"error": {"message": "Upstream request timed out", "type": "timeout"}},
        )
    except httpx.RequestError as exc:
        logger.error("Connection error talking to OpenRouter: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"Could not reach upstream: {exc}", "type": "connection_error"}},
        )

    logger.info("OpenRouter responded with HTTP %d", response.status_code)

    # Pass the upstream status code and body straight through.
    return JSONResponse(content=response.json(), status_code=response.status_code)
