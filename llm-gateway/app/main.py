import logging
import os
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
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

# Non-streaming: wait up to 2 minutes for the full response body.
_TIMEOUT = httpx.Timeout(120.0)

# Streaming: allow 30s to connect and receive response headers; no per-chunk
# read timeout (None) because token gaps from large models can be seconds long.
_STREAM_TIMEOUT = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=5.0)


class ChatRequest(BaseModel):
    # Declared fields are shown in Swagger; any extra OpenAI-compatible fields
    # (tools, top_p, etc.) are allowed and forwarded unchanged.
    model_config = ConfigDict(extra="allow")

    messages: list[dict[str, Any]]
    model: str | None = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


def _make_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_UPSTREAM_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:8080",
        "X-Title": "LLM Gateway",
    }


def _is_upstream_rate_limit(response: httpx.Response) -> bool:
    """True when OpenRouter reports the model itself is throttled by its provider.

    Distinct from our own key hitting OpenRouter's limit (also a 429 but different
    message). We only fall over to the next model for provider-side throttles.
    For streaming responses, call await response.aread() before this function.
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
async def chat_completions(body: ChatRequest):
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

    if body.stream:
        return await _handle_streaming(body)
    return await _handle_nonstreaming(body)


async def _handle_nonstreaming(body: ChatRequest) -> JSONResponse:
    headers = _make_headers()
    skipped: list[str] = []
    last_rate_limit_body: dict | None = None

    for model in _MODELS:
        payload = body.model_dump(exclude_none=True)
        payload["model"] = model

        logger.info("Trying model: %s", model)

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
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
        # directly. We don't fall over on 4xx errors because retrying a different
        # model won't fix a malformed request.
        logger.info("Model %s served the request (HTTP %d)", model, response.status_code)
        return JSONResponse(content=response.json(), status_code=response.status_code)

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


async def _handle_streaming(body: ChatRequest):
    """Try each model in order and relay SSE chunks from the first that returns 200.

    Failover is only possible before the first byte reaches the client — we check
    the upstream status code before committing to the StreamingResponse. On a 429
    upstream rate limit we fall over cleanly. If the upstream dies mid-stream, the
    client receives a truncated response; there is no way to switch models at that
    point without restarting from scratch.
    """
    headers = _make_headers()
    skipped: list[str] = []
    last_rate_limit_body: dict | None = None

    for model in _MODELS:
        payload = body.model_dump(exclude_none=True)
        payload["model"] = model

        logger.info("Trying model (stream): %s", model)

        client = httpx.AsyncClient(timeout=_STREAM_TIMEOUT)
        try:
            # build_request + send(stream=True) gives us status + headers without
            # consuming any body bytes, so we can still fail over at this point.
            request = client.build_request(
                "POST",
                f"{_UPSTREAM_BASE}/chat/completions",
                json=payload,
                headers=headers,
            )
            response = await client.send(request, stream=True)
        except httpx.TimeoutException:
            await client.aclose()
            logger.warning("Timeout connecting to model %s (stream)", model)
            skipped.append(f"{model} (timeout)")
            continue
        except httpx.RequestError as exc:
            await client.aclose()
            logger.warning("Connection error on model %s (stream): %s", model, exc)
            skipped.append(f"{model} (connection error)")
            continue

        if response.status_code != 200:
            # Error bodies are always small — read fully so json() works.
            await response.aread()
            if _is_upstream_rate_limit(response):
                last_rate_limit_body = response.json()
                await response.aclose()
                await client.aclose()
                logger.warning("Model %s rate-limited upstream (stream), trying next", model)
                skipped.append(f"{model} (upstream rate limit)")
                continue
            # Non-rate-limit error — pass through without falling over.
            error_body = response.json()
            await response.aclose()
            await client.aclose()
            logger.info("Model %s returned HTTP %d (stream)", model, response.status_code)
            return JSONResponse(content=error_body, status_code=response.status_code)

        # HTTP 200 — commit to streaming. The generator keeps client + response
        # alive for the duration; both are closed when the generator is exhausted
        # or the client disconnects.
        logger.info("Streaming from model %s", model)

        async def _relay(resp=response, cli=client):
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()
                await cli.aclose()

        return StreamingResponse(
            _relay(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    logger.error("All models exhausted for streaming. Skipped: %s", skipped)
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
