import logging
import os

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from typing import Any

from app.config import load_config
from app.sources import Source, SourceRegistry

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="LLM Gateway")

# Non-streaming: wait up to 2 minutes for the full response body.
_TIMEOUT = httpx.Timeout(120.0)

# Streaming: allow 30s to connect and receive response headers; no per-chunk
# read timeout (None) because token gaps from large models can be seconds long.
_STREAM_TIMEOUT = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=5.0)

# Registry is initialised lazily on the first request so that the module can
# be imported in tests without requiring a real config.yaml on disk.
_registry: SourceRegistry | None = None


def _get_registry() -> SourceRegistry:
    global _registry
    if _registry is None:
        _registry = SourceRegistry(load_config())
    return _registry


class ChatRequest(BaseModel):
    # Declared fields are shown in Swagger; any extra OpenAI-compatible fields
    # (tools, top_p, etc.) are allowed and forwarded unchanged.
    model_config = ConfigDict(extra="allow")

    messages: list[dict[str, Any]]
    model: str | None = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


def _make_headers(source: Source) -> dict[str, str]:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:8080",
        "X-Title": "LLM Gateway",
    }
    if source.api_key:
        headers["Authorization"] = f"Bearer {source.api_key}"
    return headers


def _is_upstream_rate_limit(response: httpx.Response) -> bool:
    """True when OpenRouter signals that the model itself is throttled by its provider.

    Not used in M3 (single source, no failover decision to make), but retained
    here because M4 will use it to decide whether to try the next source.
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
    sources = _get_registry().available_sources()
    if not sources:
        logger.error("No sources are available — check config.yaml and API keys")
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "No sources are configured or available. Check config.yaml and your API keys.",
                    "type": "no_sources",
                }
            },
        )

    # M3: always use the first available (highest-priority) source.
    # M4 will add the fallover loop that tries subsequent sources on failure.
    source = sources[0]

    if body.stream:
        return await _handle_streaming(body, source)
    return await _handle_nonstreaming(body, source)


async def _handle_nonstreaming(body: ChatRequest, source: Source) -> JSONResponse:
    payload = body.model_dump(exclude_none=True)
    payload["model"] = source.model
    headers = _make_headers(source)

    logger.info("Forwarding to source=%s model=%s", source.name, source.model)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"{source.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
    except httpx.TimeoutException:
        logger.error("Timeout on source %s", source.name)
        return JSONResponse(
            status_code=504,
            content={"error": {"message": "Upstream request timed out", "type": "timeout"}},
        )
    except httpx.RequestError as exc:
        logger.error("Connection error on source %s: %s", source.name, exc)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"Could not reach upstream: {exc}", "type": "connection_error"}},
        )

    logger.info("Source %s responded HTTP %d", source.name, response.status_code)
    return JSONResponse(content=response.json(), status_code=response.status_code)


async def _handle_streaming(body: ChatRequest, source: Source):
    """Relay SSE chunks from the upstream as they arrive.

    Failover is only possible before the first byte reaches the client — the
    upstream status code is checked before committing to the StreamingResponse.
    If the upstream dies mid-stream the client receives a truncated response;
    there is no way to switch sources at that point. M4 will add multi-source
    failover at the pre-commit stage.
    """
    payload = body.model_dump(exclude_none=True)
    payload["model"] = source.model
    headers = _make_headers(source)

    logger.info("Streaming from source=%s model=%s", source.name, source.model)

    client = httpx.AsyncClient(timeout=_STREAM_TIMEOUT)
    try:
        request = client.build_request(
            "POST",
            f"{source.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        response = await client.send(request, stream=True)
    except httpx.TimeoutException:
        await client.aclose()
        logger.error("Timeout connecting to source %s (stream)", source.name)
        return JSONResponse(
            status_code=504,
            content={"error": {"message": "Upstream request timed out", "type": "timeout"}},
        )
    except httpx.RequestError as exc:
        await client.aclose()
        logger.error("Connection error on source %s (stream): %s", source.name, exc)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"Could not reach upstream: {exc}", "type": "connection_error"}},
        )

    if response.status_code != 200:
        await response.aread()
        error_body = response.json()
        await response.aclose()
        await client.aclose()
        logger.warning("Source %s returned HTTP %d (stream)", source.name, response.status_code)
        return JSONResponse(content=error_body, status_code=response.status_code)

    # HTTP 200 — commit to streaming. Keep client + response alive for the
    # duration; both are closed when the generator is exhausted or cancelled.
    logger.info("Source %s streaming HTTP 200", source.name)

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
