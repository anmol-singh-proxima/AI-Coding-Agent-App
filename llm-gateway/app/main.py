import logging
import time

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from typing import Any

from app.config import load_config
from app.limiter import SourceLimiter
from app.sources import Source, SourceRegistry
from app import router

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

# Both globals are initialised lazily on the first request so that the module
# can be imported in tests without requiring a real config.yaml on disk.
_registry: SourceRegistry | None = None
_limiter: SourceLimiter | None = None


def _get_registry() -> SourceRegistry:
    global _registry
    if _registry is None:
        _registry = SourceRegistry(load_config())
    return _registry


def _get_limiter() -> SourceLimiter:
    global _limiter
    if _limiter is None:
        _limiter = SourceLimiter()
    return _limiter


class Message(BaseModel):
    role: str
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "messages": [{"role": "user", "content": "Hello, how are you?"}],
                "stream": False,
                "temperature": 0.7,
                "max_tokens": 512,
            }
        }
    )

    messages: list[Message]
    model: str | None = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    stop: str | list[str] | None = None
    n: int | None = None
    seed: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    user: str | None = None


def _make_headers(source: Source) -> dict[str, str]:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:8080",
        "X-Title": "LLM Gateway",
    }
    if source.api_key:
        headers["Authorization"] = f"Bearer {source.api_key}"
    return headers


def _should_failover(status_code: int) -> bool:
    """True when the failure is source-specific and the next source may succeed.

    We fail over on auth errors (401/403) because the issue is this source's
    key, not our request payload. We never fail over on 400/422 because those
    mean our payload is malformed — retrying a different source would repeat the
    same error.
    """
    if status_code == 429:
        return True
    if status_code in (401, 403):
        return True  # key/permission issue specific to this source
    if status_code in (502, 504):
        return True  # gateway-generated: connection error or timeout
    if 500 <= status_code < 600:
        return True  # upstream server error
    return False


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatRequest):
    chain = router.select_chain(_get_registry(), _get_limiter())
    if not chain:
        logger.error("No sources available — check config.yaml and API keys")
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "No sources are configured or available. Check config.yaml and your API keys.",
                    "type": "no_sources",
                }
            },
        )

    failures: list[dict] = []
    limiter = _get_limiter()
    for source in chain:
        logger.info("Trying source=%s model=%s", source.name, source.model)
        limiter.record(source)

        if body.stream:
            result = await _handle_streaming(body, source)
        else:
            result = await _handle_nonstreaming(body, source)

        if isinstance(result, StreamingResponse):
            logger.info("Served by source=%s model=%s (stream)", source.name, source.model)
            return result

        status = result.status_code
        if _should_failover(status):
            if status == 429:
                retry_after_str = result.headers.get("retry-after")
                retry_after: float | None = None
                if retry_after_str:
                    try:
                        retry_after = float(retry_after_str)
                    except ValueError:
                        pass
                limiter.mark_rate_limited(source, retry_after)
            logger.warning(
                "Source %s HTTP %d — failing over to next source",
                source.name, status,
            )
            failures.append({"source": source.name, "status": status})
            continue

        if status == 200:
            logger.info("Served by source=%s model=%s", source.name, source.model)
        else:
            logger.info("Source %s returned %d (non-retriable)", source.name, status)
        return result

    logger.error("All %d source(s) failed: %s", len(failures), failures)
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "message": "All sources failed or are unavailable.",
                "type": "all_sources_failed",
                "attempts": failures,
            }
        },
    )


@app.get("/status")
async def status():
    """Per-source counters and availability — useful for debugging which source served you."""
    registry = _get_registry()
    limiter = _get_limiter()
    return {
        "sources": [
            {
                "name": s.name,
                "model": s.model,
                "priority": s.priority,
                "enabled": s.enabled,
                "rpm": s.rpm,
                "rpd": s.rpd,
                "available": s.enabled and limiter.can_use(s),
                **limiter.status(s),
            }
            for s in registry.all_sources()
        ]
    }


@app.get("/v1/models")
async def models():
    """OpenAI-compatible model list — Cline uses this to populate its model dropdown.

    Each configured source is returned as a separate model entry. The gateway
    ignores the model field callers send and always routes by priority, so any
    entry here is valid to select.
    """
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": s.name,
                "object": "model",
                "created": now,
                "owned_by": "llm-gateway",
            }
            for s in _get_registry().all_sources()
        ],
    }


async def _handle_nonstreaming(body: ChatRequest, source: Source) -> JSONResponse:
    payload = body.model_dump(exclude_none=True)
    payload["model"] = source.model
    headers = _make_headers(source)

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

    logger.info("Source %s HTTP %d", source.name, response.status_code)
    extra_headers: dict[str, str] = {}
    retry_after = response.headers.get("retry-after")
    if retry_after:
        extra_headers["retry-after"] = retry_after
    return JSONResponse(
        content=response.json(),
        status_code=response.status_code,
        headers=extra_headers or None,
    )


async def _handle_streaming(body: ChatRequest, source: Source):
    """Attempt a streaming connection to source and relay SSE chunks if successful.

    Failover is only possible before the first byte reaches the client. We check
    the upstream status code before committing to a StreamingResponse. If the
    upstream returns non-200 (e.g. 429), we return a JSONResponse so the
    failover loop in chat_completions() can try the next source. Once we return
    a StreamingResponse (HTTP 200 confirmed), we are committed — if the upstream
    dies mid-stream, the client receives a truncated response.
    """
    payload = body.model_dump(exclude_none=True)
    payload["model"] = source.model
    headers = _make_headers(source)

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
        extra_headers: dict[str, str] = {}
        retry_after = response.headers.get("retry-after")
        if retry_after:
            extra_headers["retry-after"] = retry_after
        await response.aclose()
        await client.aclose()
        logger.warning("Source %s HTTP %d (stream)", source.name, response.status_code)
        return JSONResponse(
            content=error_body,
            status_code=response.status_code,
            headers=extra_headers or None,
        )

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
