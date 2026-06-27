"""
Tests for POST /v1/chat/completions

All outgoing httpx calls are intercepted so no real network traffic is made.
The source registry is replaced with a single test source so tests run without
a real config.yaml or API keys on disk.

Scenarios covered:
  1. Input validation (FastAPI/Pydantic layer)
  2. Source availability guard (no sources configured)
  3. Non-streaming: happy path, payload rewriting, field pass-through
  4. Non-streaming: upstream error pass-through, network errors
  5. Streaming: happy path, payload rewriting, SSE chunk relay
  6. Streaming: error pass-through, network errors
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

import app.main as main_module
from app.main import app
from app.sources import Source, SourceRegistry

CLIENT = TestClient(app)

# ── helpers — non-streaming ───────────────────────────────────────────────────


def _fake_response(status_code: int, body: dict) -> MagicMock:
    """Minimal mock for a fully-buffered httpx.Response (client.post())."""
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.json.return_value = body
    return r


def _patch_upstream(responses: list):
    """Patch httpx.AsyncClient for the non-streaming path (async-with + post()).

    Returns (patcher, calls) where calls records the kwargs of each post().
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
) -> MagicMock:
    """Minimal mock for a streaming httpx.Response (client.send(stream=True))."""
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = error_body or {}
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


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def with_test_source(monkeypatch) -> Source:
    """Replace the live registry with a single predictable test source.

    All tests get this by default. Tests that need zero sources can override
    available_sources() on their own mock registry.
    """
    mock_reg = MagicMock(spec=SourceRegistry)
    mock_reg.available_sources.return_value = [TEST_SOURCE]
    monkeypatch.setattr(main_module, "_registry", mock_reg)
    return TEST_SOURCE


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

    def test_upstream_error_passed_through_unchanged(self):
        error = {"error": {"message": "Missing Authentication header", "code": 401}}
        patcher, _ = _patch_upstream([_fake_response(401, error)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 401
        assert resp.json() == error

    def test_upstream_5xx_passed_through_unchanged(self):
        error = {"error": {"message": "Internal Server Error"}}
        patcher, _ = _patch_upstream([_fake_response(500, error)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 500


# ── 4. Non-streaming: network errors ─────────────────────────────────────────


class TestNonStreamingNetworkErrors:
    def test_timeout_returns_504(self):
        patcher, _ = _patch_upstream([httpx.TimeoutException("timed out")])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 504
        assert resp.json()["error"]["type"] == "timeout"

    def test_connect_error_returns_502(self):
        patcher, _ = _patch_upstream([httpx.ConnectError("connection refused")])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 502
        assert resp.json()["error"]["type"] == "connection_error"


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

    def test_upstream_error_passed_through_immediately(self):
        error = {"error": {"message": "Invalid API key", "code": 401}}
        patcher, _ = _patch_stream_upstream([_fake_stream_response(401, error_body=error)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 401
        assert resp.json() == error

    def test_upstream_5xx_passed_through(self):
        error = {"error": {"message": "Internal Server Error"}}
        patcher, _ = _patch_stream_upstream([_fake_stream_response(500, error_body=error)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 500


# ── 6. Streaming: network errors ─────────────────────────────────────────────


class TestStreamingNetworkErrors:
    def test_timeout_returns_504(self):
        patcher, _ = _patch_stream_upstream([httpx.TimeoutException("timed out")])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 504
        assert resp.json()["error"]["type"] == "timeout"

    def test_connect_error_returns_502(self):
        patcher, _ = _patch_stream_upstream([httpx.ConnectError("connection refused")])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 502
        assert resp.json()["error"]["type"] == "connection_error"
