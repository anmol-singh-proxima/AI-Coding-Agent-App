"""
Tests for POST /v1/chat/completions and GET /status

All outgoing httpx calls are intercepted so no real network traffic is made.
The source registry and limiter are replaced with test doubles so tests run
without a real config.yaml or API keys on disk.

Scenarios covered:
  1. Input validation (FastAPI/Pydantic layer)
  2. Source availability guard (no sources configured)
  3. Non-streaming: happy path, payload rewriting, field pass-through
  4. Non-streaming: error handling with a single source
  5. Streaming: happy path, payload rewriting, SSE chunk relay
  6. Streaming: error handling with a single source
  7. Failover: non-streaming (429/5xx/401/timeout → next source)
  8. Failover: streaming (429/5xx/401 before first byte → next source)
  9. Limiter integration: record/mark_rate_limited wiring and pre-flight filtering
  10. Status endpoint: /status returns per-source counters and availability

M4 behaviour note:
  Retriable errors (429, 401, 403, 5xx, network timeouts/connection failures)
  trigger the failover loop. When all sources are exhausted the gateway returns
  503 all_sources_failed. Non-retriable errors (400, 422) are returned directly
  to the caller because failing over would repeat the same malformed request.

M5 behaviour note:
  Before each attempt, limiter.record(source) is called. On 429, the gateway
  also calls limiter.mark_rate_limited(source, retry_after). Sources already
  over-quota are excluded from the chain before the first attempt.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

import app.main as main_module
from app.limiter import SourceLimiter
from app.main import app
from app.sources import Source, SourceRegistry

CLIENT = TestClient(app)

# ── helpers — non-streaming ───────────────────────────────────────────────────


def _fake_response(
    status_code: int,
    body: dict,
    retry_after: str | None = None,
) -> MagicMock:
    """Minimal mock for a fully-buffered httpx.Response (client.post())."""
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.json.return_value = body
    r.headers = httpx.Headers({"retry-after": retry_after} if retry_after else {})
    return r


def _patch_upstream(responses: list):
    """Patch httpx.AsyncClient for the non-streaming path (async-with + post()).

    Returns (patcher, calls) where calls records the kwargs of each post().
    The response_iter is shared across all source attempts so multi-source
    failover tests can supply a response per attempt.
    """
    response_iter = iter(responses)
    calls: list[dict] = []

    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url: str, **kwargs):
            calls.append({"url": url, **kwargs})
            nxt = next(response_iter)
            if isinstance(nxt, BaseException):
                raise nxt
            return nxt

    patcher = patch("httpx.AsyncClient", return_value=_FakeAsyncClient())
    return patcher, calls


# ── helpers — streaming ───────────────────────────────────────────────────────


def _fake_stream_response(
    status_code: int,
    chunks: list[bytes] = (),
    error_body: dict | None = None,
    retry_after: str | None = None,
) -> MagicMock:
    """Minimal mock for a streaming httpx.Response (client.send(stream=True))."""
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = error_body or {}
    r.headers = httpx.Headers({"retry-after": retry_after} if retry_after else {})
    r.aread = AsyncMock()
    r.aclose = AsyncMock()

    async def _aiter_bytes():
        for chunk in chunks:
            yield chunk

    r.aiter_bytes = _aiter_bytes
    return r


def _patch_stream_upstream(responses: list):
    """Patch httpx.AsyncClient for the streaming path (build_request + send()).

    Returns (patcher, calls) where calls records kwargs of each build_request().
    """
    response_iter = iter(responses)
    calls: list[dict] = []

    class _FakeStreamClient:
        def build_request(self, method: str, url: str, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            return MagicMock()

        async def send(self, request, *, stream=False):
            nxt = next(response_iter)
            if isinstance(nxt, BaseException):
                raise nxt
            return nxt

        async def aclose(self):
            pass

    patcher = patch("httpx.AsyncClient", return_value=_FakeStreamClient())
    return patcher, calls


# ── shared constants ──────────────────────────────────────────────────────────

VALID_BODY = {"messages": [{"role": "user", "content": "say hi"}]}

SSE_CHUNKS = [
    b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"!"}}]}\n\n',
    b"data: [DONE]\n\n",
]

TEST_SOURCE = Source(
    name="test-openrouter",
    base_url="https://openrouter.ai/api/v1",
    model="test-model-id",
    api_key="sk-test-key",
    rpm=None,
    rpd=None,
    priority=1,
    enabled=True,
)

PRIMARY_SOURCE = Source(
    name="primary",
    base_url="https://primary.example.com/v1",
    model="primary-model",
    api_key="sk-primary",
    rpm=None,
    rpd=None,
    priority=1,
    enabled=True,
)

FALLBACK_SOURCE = Source(
    name="fallback",
    base_url="https://fallback.example.com/v1",
    model="fallback-model",
    api_key="sk-fallback",
    rpm=None,
    rpd=None,
    priority=2,
    enabled=True,
)


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def with_test_source(monkeypatch) -> Source:
    """Replace the live registry and limiter with single-source test doubles.

    All tests get this by default; tests that need a different registry or
    limiter can monkeypatch those globals themselves (their setattr runs after
    this one and takes precedence for the duration of the test).
    """
    mock_reg = MagicMock(spec=SourceRegistry)
    mock_reg.available_sources.return_value = [TEST_SOURCE]
    mock_reg.all_sources.return_value = [TEST_SOURCE]
    monkeypatch.setattr(main_module, "_registry", mock_reg)
    monkeypatch.setattr(main_module, "_limiter", SourceLimiter())
    return TEST_SOURCE


@pytest.fixture
def with_two_sources(monkeypatch):
    """Replace the live registry with PRIMARY → FALLBACK (priority order).

    Used by failover tests. Because with_test_source is autouse and runs
    first, this fixture's monkeypatch.setattr wins for the test duration.
    Returns (primary, fallback) so tests can check URLs/models.
    """
    mock_reg = MagicMock(spec=SourceRegistry)
    mock_reg.available_sources.return_value = [PRIMARY_SOURCE, FALLBACK_SOURCE]
    mock_reg.all_sources.return_value = [PRIMARY_SOURCE, FALLBACK_SOURCE]
    monkeypatch.setattr(main_module, "_registry", mock_reg)
    monkeypatch.setattr(main_module, "_limiter", SourceLimiter())
    return PRIMARY_SOURCE, FALLBACK_SOURCE


# ── 1. Input validation ───────────────────────────────────────────────────────


class TestInputValidation:
    def test_empty_body_returns_422(self):
        resp = CLIENT.post(
            "/v1/chat/completions",
            content=b"",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_body_missing_messages_field_returns_422(self):
        resp = CLIENT.post("/v1/chat/completions", json={"stream": False})
        assert resp.status_code == 422

    def test_messages_wrong_type_returns_422(self):
        resp = CLIENT.post("/v1/chat/completions", json={"messages": "not a list"})
        assert resp.status_code == 422

    def test_empty_messages_list_is_structurally_valid(self):
        patcher, _ = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={"messages": []})
        assert resp.status_code == 200


# ── 2. Source availability guard ─────────────────────────────────────────────


class TestSourceAvailabilityGuard:
    def test_no_sources_returns_503(self, monkeypatch):
        mock_reg = MagicMock(spec=SourceRegistry)
        mock_reg.available_sources.return_value = []
        monkeypatch.setattr(main_module, "_registry", mock_reg)
        resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "no_sources"

    def test_no_sources_does_not_reach_upstream(self, monkeypatch):
        mock_reg = MagicMock(spec=SourceRegistry)
        mock_reg.available_sources.return_value = []
        monkeypatch.setattr(main_module, "_registry", mock_reg)
        patcher, calls = _patch_upstream([])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert len(calls) == 0

    def test_stream_false_reaches_first_available_source(self):
        patcher, _ = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": False})
        assert resp.status_code == 200

    def test_stream_omitted_defaults_to_false(self):
        patcher, _ = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 200


# ── 3. Non-streaming: forwarding ─────────────────────────────────────────────


class TestNonStreamingForwarding:
    def test_happy_path_returns_upstream_body(self):
        upstream = {"choices": [{"message": {"role": "assistant", "content": "Hello!"}}]}
        patcher, _ = _patch_upstream([_fake_response(200, upstream)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 200
        assert resp.json() == upstream

    def test_caller_model_replaced_with_source_model(self, with_test_source):
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "model": "caller-model"})
        assert calls[0]["json"]["model"] == with_test_source.model

    def test_authorization_header_uses_source_api_key(self, with_test_source):
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert calls[0]["headers"]["Authorization"] == f"Bearer {with_test_source.api_key}"

    def test_keyless_source_omits_authorization_header(self, monkeypatch):
        keyless = Source(
            name="ollama", base_url="http://localhost:11434/v1",
            model="devstral", api_key="",
            rpm=None, rpd=None, priority=99, enabled=True,
        )
        mock_reg = MagicMock(spec=SourceRegistry)
        mock_reg.available_sources.return_value = [keyless]
        monkeypatch.setattr(main_module, "_registry", mock_reg)
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert "Authorization" not in calls[0]["headers"]

    def test_request_routed_to_source_base_url(self, with_test_source):
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert calls[0]["url"] == f"{with_test_source.base_url}/chat/completions"

    def test_extra_openai_fields_forwarded_unchanged(self):
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post("/v1/chat/completions",
                        json={**VALID_BODY, "top_p": 0.95,
                              "tools": [{"type": "function", "function": {"name": "fn"}}]})
        payload = calls[0]["json"]
        assert payload["top_p"] == 0.95
        assert "tools" in payload

    def test_temperature_and_max_tokens_forwarded(self):
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post("/v1/chat/completions",
                        json={**VALID_BODY, "temperature": 0.7, "max_tokens": 256})
        assert calls[0]["json"]["temperature"] == 0.7
        assert calls[0]["json"]["max_tokens"] == 256

    def test_none_optional_fields_excluded_from_payload(self):
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert "temperature" not in calls[0]["json"]
        assert "max_tokens" not in calls[0]["json"]

    def test_bad_request_returned_directly_without_failover(self):
        # 400 is non-retriable: the payload is malformed; trying another source
        # would produce the same error, so we return it to the caller immediately.
        error = {"error": {"message": "messages array is required", "code": 400}}
        patcher, calls = _patch_upstream([_fake_response(400, error)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 400
        assert resp.json() == error
        assert len(calls) == 1  # only one source was tried

    def test_single_source_retriable_error_returns_503(self):
        # 5xx is retriable, but with only one source the chain exhausts → 503.
        patcher, _ = _patch_upstream([_fake_response(500, {"error": {"message": "oops"}})])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "all_sources_failed"


# ── 4. Non-streaming: single-source network errors ───────────────────────────


class TestNonStreamingNetworkErrors:
    def test_timeout_single_source_returns_503(self):
        # Timeout is retriable. Single source exhausted → 503 all_sources_failed.
        # The attempts list records the gateway-side 504 status.
        patcher, _ = _patch_upstream([httpx.TimeoutException("timed out")])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "all_sources_failed"
        assert resp.json()["error"]["attempts"][0]["status"] == 504

    def test_connect_error_single_source_returns_503(self):
        # Connection error is retriable. Single source exhausted → 503.
        patcher, _ = _patch_upstream([httpx.ConnectError("connection refused")])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "all_sources_failed"
        assert resp.json()["error"]["attempts"][0]["status"] == 502


# ── 5. Streaming: happy path ──────────────────────────────────────────────────


class TestStreaming:
    def test_stream_true_is_accepted(self):
        patcher, _ = _patch_stream_upstream([_fake_stream_response(200, chunks=SSE_CHUNKS)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 200

    def test_response_content_type_is_event_stream(self):
        patcher, _ = _patch_stream_upstream([_fake_stream_response(200, chunks=SSE_CHUNKS)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert "text/event-stream" in resp.headers["content-type"]

    def test_sse_chunks_relayed_in_order(self):
        patcher, _ = _patch_stream_upstream([_fake_stream_response(200, chunks=SSE_CHUNKS)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.content == b"".join(SSE_CHUNKS)

    def test_model_replaced_with_source_model(self, with_test_source):
        patcher, calls = _patch_stream_upstream([_fake_stream_response(200, chunks=SSE_CHUNKS)])
        with patcher:
            CLIENT.post("/v1/chat/completions",
                        json={**VALID_BODY, "stream": True, "model": "caller-model"})
        assert calls[0]["json"]["model"] == with_test_source.model

    def test_authorization_header_set(self, with_test_source):
        patcher, calls = _patch_stream_upstream([_fake_stream_response(200, chunks=SSE_CHUNKS)])
        with patcher:
            CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert calls[0]["headers"]["Authorization"] == f"Bearer {with_test_source.api_key}"

    def test_request_routed_to_source_base_url(self, with_test_source):
        patcher, calls = _patch_stream_upstream([_fake_stream_response(200, chunks=SSE_CHUNKS)])
        with patcher:
            CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert calls[0]["url"] == f"{with_test_source.base_url}/chat/completions"

    def test_keyless_source_omits_authorization_header(self, monkeypatch):
        keyless = Source(
            name="ollama", base_url="http://localhost:11434/v1",
            model="devstral", api_key="",
            rpm=None, rpd=None, priority=99, enabled=True,
        )
        mock_reg = MagicMock(spec=SourceRegistry)
        mock_reg.available_sources.return_value = [keyless]
        monkeypatch.setattr(main_module, "_registry", mock_reg)
        patcher, calls = _patch_stream_upstream([_fake_stream_response(200, chunks=SSE_CHUNKS)])
        with patcher:
            CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert "Authorization" not in calls[0]["headers"]

    def test_extra_fields_forwarded_unchanged(self):
        patcher, calls = _patch_stream_upstream([_fake_stream_response(200, chunks=SSE_CHUNKS)])
        with patcher:
            CLIENT.post("/v1/chat/completions",
                        json={**VALID_BODY, "stream": True, "top_p": 0.9})
        assert calls[0]["json"]["top_p"] == 0.9

    def test_bad_request_returned_directly_without_failover(self):
        # 400 is non-retriable for streaming too.
        error = {"error": {"message": "messages required", "code": 400}}
        patcher, calls = _patch_stream_upstream(
            [_fake_stream_response(400, error_body=error)]
        )
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 400
        assert resp.json() == error
        assert len(calls) == 1

    def test_single_source_retriable_error_returns_503(self):
        # 5xx before streaming starts → retriable → chain exhausted → 503.
        error = {"error": {"message": "Internal Server Error"}}
        patcher, _ = _patch_stream_upstream([_fake_stream_response(500, error_body=error)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "all_sources_failed"


# ── 6. Streaming: single-source network errors ───────────────────────────────


class TestStreamingNetworkErrors:
    def test_timeout_single_source_returns_503(self):
        patcher, _ = _patch_stream_upstream([httpx.TimeoutException("timed out")])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "all_sources_failed"
        assert resp.json()["error"]["attempts"][0]["status"] == 504

    def test_connect_error_single_source_returns_503(self):
        patcher, _ = _patch_stream_upstream([httpx.ConnectError("connection refused")])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "all_sources_failed"
        assert resp.json()["error"]["attempts"][0]["status"] == 502


# ── 7. Failover: non-streaming ────────────────────────────────────────────────


class TestFailover:
    def test_429_triggers_failover_to_next_source(self, with_two_sources):
        primary, fallback = with_two_sources
        patcher, calls = _patch_upstream([
            _fake_response(429, {"error": {"message": "rate limited"}}),
            _fake_response(200, {"choices": [{"message": {"content": "ok"}}]}),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 200
        assert len(calls) == 2
        assert calls[0]["url"] == f"{primary.base_url}/chat/completions"
        assert calls[1]["url"] == f"{fallback.base_url}/chat/completions"

    def test_5xx_triggers_failover(self, with_two_sources):
        patcher, calls = _patch_upstream([
            _fake_response(500, {"error": {"message": "internal error"}}),
            _fake_response(200, {"choices": []}),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 200
        assert len(calls) == 2

    def test_401_triggers_failover(self, with_two_sources):
        # Invalid key on source #1 → auth error is source-specific → try source #2.
        patcher, calls = _patch_upstream([
            _fake_response(401, {"error": {"message": "Unauthorized"}}),
            _fake_response(200, {"choices": []}),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 200
        assert len(calls) == 2

    def test_timeout_triggers_failover(self, with_two_sources):
        patcher, calls = _patch_upstream([
            httpx.TimeoutException("timed out"),
            _fake_response(200, {"choices": []}),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 200
        assert len(calls) == 2

    def test_connect_error_triggers_failover(self, with_two_sources):
        patcher, calls = _patch_upstream([
            httpx.ConnectError("refused"),
            _fake_response(200, {"choices": []}),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 200
        assert len(calls) == 2

    def test_bad_request_not_retried_on_next_source(self, with_two_sources):
        # 400 means our payload is malformed — retrying another source repeats the
        # same error, so the gateway returns 400 immediately without trying source #2.
        error = {"error": {"message": "bad request"}}
        patcher, calls = _patch_upstream([_fake_response(400, error)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 400
        assert len(calls) == 1  # fallback was NOT tried

    def test_all_sources_exhausted_returns_503(self, with_two_sources):
        patcher, _ = _patch_upstream([
            _fake_response(429, {"error": {"message": "rate limited"}}),
            _fake_response(500, {"error": {"message": "server error"}}),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "all_sources_failed"

    def test_all_sources_failed_response_lists_both_attempts(self, with_two_sources):
        primary, fallback = with_two_sources
        patcher, _ = _patch_upstream([
            _fake_response(429, {"error": {"message": "rate limited"}}),
            _fake_response(500, {"error": {"message": "server error"}}),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        attempts = resp.json()["error"]["attempts"]
        assert len(attempts) == 2
        assert attempts[0]["source"] == primary.name
        assert attempts[1]["source"] == fallback.name

    def test_fallback_source_model_used_after_failover(self, with_two_sources):
        primary, fallback = with_two_sources
        patcher, calls = _patch_upstream([
            _fake_response(429, {"error": {"message": "rate limited"}}),
            _fake_response(200, {"choices": []}),
        ])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert calls[1]["json"]["model"] == fallback.model


# ── 8. Failover: streaming ────────────────────────────────────────────────────


class TestStreamingFailover:
    def test_429_before_stream_triggers_failover(self, with_two_sources):
        patcher, calls = _patch_stream_upstream([
            _fake_stream_response(429, error_body={"error": {"message": "rate limited"}}),
            _fake_stream_response(200, chunks=SSE_CHUNKS),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 200
        assert len(calls) == 2

    def test_5xx_before_stream_triggers_failover(self, with_two_sources):
        patcher, calls = _patch_stream_upstream([
            _fake_stream_response(503, error_body={"error": {"message": "unavailable"}}),
            _fake_stream_response(200, chunks=SSE_CHUNKS),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 200
        assert len(calls) == 2

    def test_401_before_stream_triggers_failover(self, with_two_sources):
        patcher, calls = _patch_stream_upstream([
            _fake_stream_response(401, error_body={"error": {"message": "Unauthorized"}}),
            _fake_stream_response(200, chunks=SSE_CHUNKS),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 200
        assert len(calls) == 2

    def test_timeout_before_stream_triggers_failover(self, with_two_sources):
        patcher, calls = _patch_stream_upstream([
            httpx.TimeoutException("timed out"),
            _fake_stream_response(200, chunks=SSE_CHUNKS),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 200
        assert len(calls) == 2

    def test_all_stream_sources_exhausted_returns_503(self, with_two_sources):
        patcher, _ = _patch_stream_upstream([
            _fake_stream_response(429, error_body={"error": {"message": "rate limited"}}),
            _fake_stream_response(500, error_body={"error": {"message": "error"}}),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "all_sources_failed"

    def test_fallback_chunks_relayed_after_failover(self, with_two_sources):
        patcher, _ = _patch_stream_upstream([
            _fake_stream_response(429, error_body={"error": {"message": "rate limited"}}),
            _fake_stream_response(200, chunks=SSE_CHUNKS),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.content == b"".join(SSE_CHUNKS)

    def test_fallback_source_model_used_in_stream_request(self, with_two_sources):
        primary, fallback = with_two_sources
        patcher, calls = _patch_stream_upstream([
            _fake_stream_response(429, error_body={"error": {"message": "rate limited"}}),
            _fake_stream_response(200, chunks=SSE_CHUNKS),
        ])
        with patcher:
            CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert calls[1]["json"]["model"] == fallback.model


# ── 9. Limiter integration ────────────────────────────────────────────────────


class TestLimiterIntegration:
    def test_record_called_for_each_attempt(self, monkeypatch):
        mock_limiter = MagicMock(spec=SourceLimiter)
        mock_limiter.can_use.return_value = True
        monkeypatch.setattr(main_module, "_limiter", mock_limiter)

        patcher, _ = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)

        mock_limiter.record.assert_called_once()

    def test_429_calls_mark_rate_limited_on_offending_source(self, with_two_sources, monkeypatch):
        primary, _ = with_two_sources
        mock_limiter = MagicMock(spec=SourceLimiter)
        mock_limiter.can_use.return_value = True
        monkeypatch.setattr(main_module, "_limiter", mock_limiter)

        patcher, _ = _patch_upstream([
            _fake_response(429, {"error": {"message": "rate limited"}}),
            _fake_response(200, {"choices": []}),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)

        assert resp.status_code == 200
        mock_limiter.mark_rate_limited.assert_called_once()
        assert mock_limiter.mark_rate_limited.call_args[0][0].name == primary.name

    def test_429_with_retry_after_header_passes_cooldown_to_limiter(self, with_two_sources, monkeypatch):
        mock_limiter = MagicMock(spec=SourceLimiter)
        mock_limiter.can_use.return_value = True
        monkeypatch.setattr(main_module, "_limiter", mock_limiter)

        patcher, _ = _patch_upstream([
            _fake_response(429, {"error": {"message": "rate limited"}}, retry_after="120"),
            _fake_response(200, {"choices": []}),
        ])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)

        args = mock_limiter.mark_rate_limited.call_args[0]
        assert args[1] == 120.0

    def test_non_429_does_not_call_mark_rate_limited(self, monkeypatch):
        mock_limiter = MagicMock(spec=SourceLimiter)
        mock_limiter.can_use.return_value = True
        monkeypatch.setattr(main_module, "_limiter", mock_limiter)

        patcher, _ = _patch_upstream([_fake_response(500, {"error": {"message": "oops"}})])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)

        mock_limiter.mark_rate_limited.assert_not_called()

    def test_limiter_blocked_source_not_attempted(self, with_two_sources, monkeypatch):
        primary, _ = with_two_sources
        mock_limiter = MagicMock(spec=SourceLimiter)
        mock_limiter.can_use.side_effect = lambda s: s.name != primary.name
        monkeypatch.setattr(main_module, "_limiter", mock_limiter)

        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)

        assert resp.status_code == 200
        assert len(calls) == 1
        assert primary.base_url not in calls[0]["url"]

    def test_all_limiter_blocked_returns_503(self, monkeypatch):
        mock_limiter = MagicMock(spec=SourceLimiter)
        mock_limiter.can_use.return_value = False
        monkeypatch.setattr(main_module, "_limiter", mock_limiter)

        resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "no_sources"

    def test_stream_429_calls_mark_rate_limited(self, with_two_sources, monkeypatch):
        primary, _ = with_two_sources
        mock_limiter = MagicMock(spec=SourceLimiter)
        mock_limiter.can_use.return_value = True
        monkeypatch.setattr(main_module, "_limiter", mock_limiter)

        patcher, _ = _patch_stream_upstream([
            _fake_stream_response(429, error_body={"error": {"message": "rate limited"}}),
            _fake_stream_response(200, chunks=SSE_CHUNKS),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})

        assert resp.status_code == 200
        mock_limiter.mark_rate_limited.assert_called_once()
        assert mock_limiter.mark_rate_limited.call_args[0][0].name == primary.name


# ── 10. Status endpoint ───────────────────────────────────────────────────────


class TestStatusEndpoint:
    def test_returns_200(self):
        resp = CLIENT.get("/status")
        assert resp.status_code == 200

    def test_response_has_sources_key(self):
        resp = CLIENT.get("/status")
        assert "sources" in resp.json()

    def test_lists_configured_source(self):
        resp = CLIENT.get("/status")
        sources = resp.json()["sources"]
        assert len(sources) == 1
        assert sources[0]["name"] == TEST_SOURCE.name

    def test_source_entry_has_required_fields(self):
        resp = CLIENT.get("/status")
        entry = resp.json()["sources"][0]
        for field in ("name", "model", "priority", "enabled", "rpm", "rpd",
                      "available", "minute_count", "day_count", "in_cooldown"):
            assert field in entry, f"missing field: {field}"

    def test_enabled_source_with_no_limits_is_available(self):
        resp = CLIENT.get("/status")
        assert resp.json()["sources"][0]["available"] is True

    def test_disabled_source_is_not_available(self, monkeypatch):
        disabled = Source(
            name="off", base_url="https://x.com/v1", model="m",
            api_key="k", rpm=None, rpd=None, priority=1, enabled=False,
        )
        mock_reg = MagicMock(spec=SourceRegistry)
        mock_reg.available_sources.return_value = []
        mock_reg.all_sources.return_value = [disabled]
        monkeypatch.setattr(main_module, "_registry", mock_reg)

        resp = CLIENT.get("/status")
        assert resp.json()["sources"][0]["available"] is False
