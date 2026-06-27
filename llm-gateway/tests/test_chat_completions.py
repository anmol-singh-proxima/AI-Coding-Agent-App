"""
Tests for POST /v1/chat/completions

All outgoing httpx calls are intercepted so no real network traffic is made.

Scenarios covered:
  1. Input validation (FastAPI/Pydantic layer)
  2. Configuration guard (missing API key)
  3. Non-streaming: happy path, payload rewriting, field pass-through
  4. Non-streaming: upstream rate-limit fallback (model cycling)
  5. Non-streaming: network-error fallback (timeout, connection error)
  6. Streaming: happy path, payload rewriting, SSE chunk relay
  7. Streaming: upstream rate-limit fallback before first byte
  8. Streaming: network-error fallback
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

import app.main as main_module
from app.main import app

CLIENT = TestClient(app)

# ── helpers — non-streaming ───────────────────────────────────────────────────


def _fake_response(status_code: int, body: dict) -> MagicMock:
    """Minimal mock for a fully-buffered httpx.Response (used by client.post())."""
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.json.return_value = body
    return r


def _patch_upstream(responses: list):
    """Patch httpx.AsyncClient for the non-streaming path (uses async-with + post()).

    Each entry in *responses* is a fake response mock or an exception to raise.
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
    """Minimal mock for a streaming httpx.Response (used by client.send(stream=True)).

    For non-200 responses supply error_body; the mock makes it available via json().
    For 200 responses supply chunks; aiter_bytes() yields them one by one.
    """
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
    """Patch httpx.AsyncClient for the streaming path (uses build_request + send()).

    Each entry in *responses* is either a _fake_stream_response mock or an exception.
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

# The exact error body OpenRouter sends when a model is throttled by its provider.
UPSTREAM_RATE_LIMIT_BODY = {
    "error": {
        "message": "Provider returned error",
        "code": 429,
        "metadata": {"raw": "model is temporarily rate-limited upstream."},
    }
}

# Typical SSE chunks from an OpenAI-compatible streaming endpoint.
SSE_CHUNKS = [
    b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"!"}}]}\n\n',
    b"data: [DONE]\n\n",
]


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def with_api_key(monkeypatch):
    """Inject a dummy API key so the key-guard doesn't short-circuit every test."""
    monkeypatch.setattr(main_module, "_UPSTREAM_KEY", "sk-test-key")


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
        resp = CLIENT.post(
            "/v1/chat/completions",
            json={"messages": "not a list"},
        )
        assert resp.status_code == 422

    def test_empty_messages_list_is_structurally_valid(self):
        """An empty list is valid at the schema level; upstream decides."""
        patcher, _ = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={"messages": []})
        assert resp.status_code == 200


# ── 2. Configuration guard ────────────────────────────────────────────────────


class TestConfigurationGuard:
    def test_missing_api_key_returns_503(self, monkeypatch):
        monkeypatch.setattr(main_module, "_UPSTREAM_KEY", "")
        resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "configuration_error"

    def test_missing_api_key_does_not_reach_upstream(self, monkeypatch):
        monkeypatch.setattr(main_module, "_UPSTREAM_KEY", "")
        patcher, calls = _patch_upstream([])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert len(calls) == 0

    def test_stream_false_is_accepted(self):
        patcher, _ = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": False})
        assert resp.status_code == 200

    def test_stream_defaults_to_false_when_omitted(self):
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

    def test_caller_model_is_replaced_with_first_model_in_list(self):
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "model": "caller-model"})
        assert calls[0]["json"]["model"] == main_module._MODELS[0]

    def test_authorization_header_contains_api_key(self):
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert calls[0]["headers"]["Authorization"] == "Bearer sk-test-key"

    def test_extra_openai_fields_forwarded_unchanged(self):
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post(
                "/v1/chat/completions",
                json={**VALID_BODY, "top_p": 0.95, "tools": [{"type": "function"}]},
            )
        payload = calls[0]["json"]
        assert payload["top_p"] == 0.95
        assert "tools" in payload

    def test_temperature_and_max_tokens_forwarded(self):
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "temperature": 0.7, "max_tokens": 256})
        payload = calls[0]["json"]
        assert payload["temperature"] == 0.7
        assert payload["max_tokens"] == 256

    def test_none_optional_fields_excluded_from_payload(self):
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        payload = calls[0]["json"]
        assert "temperature" not in payload
        assert "max_tokens" not in payload

    def test_upstream_401_returned_immediately_no_fallback(self):
        error = {"error": {"message": "Missing Authentication header", "code": 401}}
        patcher, calls = _patch_upstream([_fake_response(401, error)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 401
        assert resp.json() == error
        assert len(calls) == 1

    def test_upstream_5xx_returned_immediately_no_fallback(self):
        error = {"error": {"message": "Internal Server Error"}}
        patcher, calls = _patch_upstream([_fake_response(500, error)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 500
        assert len(calls) == 1


# ── 4. Non-streaming: rate-limit fallback ────────────────────────────────────


class TestNonStreamingRateLimitFallback:
    def test_falls_back_to_second_model_on_rate_limit(self):
        success = {"choices": [{"message": {"content": "ok from model 2"}}]}
        patcher, calls = _patch_upstream([
            _fake_response(429, UPSTREAM_RATE_LIMIT_BODY),
            _fake_response(200, success),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 200
        assert resp.json() == success
        assert len(calls) == 2

    def test_second_attempt_uses_next_model_id(self):
        patcher, calls = _patch_upstream([
            _fake_response(429, UPSTREAM_RATE_LIMIT_BODY),
            _fake_response(200, {"choices": []}),
        ])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert calls[0]["json"]["model"] == main_module._MODELS[0]
        assert calls[1]["json"]["model"] == main_module._MODELS[1]

    def test_partial_fallback_succeeds_on_third_model(self):
        success = {"choices": [{"message": {"content": "model 3"}}]}
        patcher, calls = _patch_upstream([
            _fake_response(429, UPSTREAM_RATE_LIMIT_BODY),
            _fake_response(429, UPSTREAM_RATE_LIMIT_BODY),
            _fake_response(200, success),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 200
        assert len(calls) == 3

    def test_all_models_exhausted_returns_503(self):
        n = len(main_module._MODELS)
        patcher, calls = _patch_upstream([_fake_response(429, UPSTREAM_RATE_LIMIT_BODY)] * n)
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "all_models_exhausted"
        assert len(calls) == n

    def test_exhausted_response_lists_every_attempted_model(self):
        n = len(main_module._MODELS)
        patcher, _ = _patch_upstream([_fake_response(429, UPSTREAM_RATE_LIMIT_BODY)] * n)
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        attempted = resp.json()["error"]["attempted"]
        assert len(attempted) == n
        for model_id in main_module._MODELS:
            assert any(model_id in entry for entry in attempted)

    def test_exhausted_response_includes_last_upstream_error(self):
        n = len(main_module._MODELS)
        patcher, _ = _patch_upstream([_fake_response(429, UPSTREAM_RATE_LIMIT_BODY)] * n)
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.json()["error"]["last_upstream_error"] == UPSTREAM_RATE_LIMIT_BODY

    def test_regular_429_not_treated_as_upstream_rate_limit(self):
        """Our own key hitting OpenRouter's limit must not trigger model fallback."""
        key_limit_body = {"error": {"message": "Rate limit exceeded for your API key"}}
        patcher, calls = _patch_upstream([_fake_response(429, key_limit_body)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 429
        assert len(calls) == 1


# ── 5. Non-streaming: network-error fallback ──────────────────────────────────


class TestNonStreamingNetworkErrorFallback:
    def test_timeout_triggers_fallback(self):
        success = {"choices": [{"message": {"content": "ok"}}]}
        patcher, calls = _patch_upstream([
            httpx.TimeoutException("timed out"),
            _fake_response(200, success),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 200
        assert len(calls) == 2

    def test_connect_error_triggers_fallback(self):
        success = {"choices": [{"message": {"content": "ok"}}]}
        patcher, calls = _patch_upstream([
            httpx.ConnectError("connection refused"),
            _fake_response(200, success),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 200
        assert len(calls) == 2

    def test_all_timeouts_returns_503(self):
        n = len(main_module._MODELS)
        patcher, _ = _patch_upstream([httpx.TimeoutException("timed out")] * n)
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "all_models_exhausted"

    def test_timeout_entries_labelled_in_attempted_list(self):
        n = len(main_module._MODELS)
        patcher, _ = _patch_upstream([httpx.TimeoutException("timed out")] * n)
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        for entry in resp.json()["error"]["attempted"]:
            assert "timeout" in entry

    def test_mixed_errors_all_exhaust_to_503(self):
        n = len(main_module._MODELS)
        errors = []
        for i in range(n):
            if i % 3 == 0:
                errors.append(_fake_response(429, UPSTREAM_RATE_LIMIT_BODY))
            elif i % 3 == 1:
                errors.append(httpx.TimeoutException("timed out"))
            else:
                errors.append(httpx.ConnectError("refused"))
        patcher, calls = _patch_upstream(errors)
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 503
        assert len(calls) == n


# ── 6. Streaming: happy path ──────────────────────────────────────────────────


class TestStreaming:
    def test_stream_true_is_accepted(self):
        """stream=true must no longer return 400 — it's fully supported in M2."""
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

    def test_model_overridden_with_first_model_in_list(self):
        patcher, calls = _patch_stream_upstream([_fake_stream_response(200, chunks=SSE_CHUNKS)])
        with patcher:
            CLIENT.post(
                "/v1/chat/completions",
                json={**VALID_BODY, "stream": True, "model": "caller-model"},
            )
        assert calls[0]["json"]["model"] == main_module._MODELS[0]

    def test_authorization_header_set(self):
        patcher, calls = _patch_stream_upstream([_fake_stream_response(200, chunks=SSE_CHUNKS)])
        with patcher:
            CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert calls[0]["headers"]["Authorization"] == "Bearer sk-test-key"

    def test_extra_fields_forwarded_unchanged(self):
        patcher, calls = _patch_stream_upstream([_fake_stream_response(200, chunks=SSE_CHUNKS)])
        with patcher:
            CLIENT.post(
                "/v1/chat/completions",
                json={**VALID_BODY, "stream": True, "top_p": 0.9},
            )
        assert calls[0]["json"]["top_p"] == 0.9

    def test_non_200_non_rate_limit_returned_immediately_no_fallback(self):
        """A 401 from the upstream must be returned immediately, not trigger fallback."""
        error = {"error": {"message": "Invalid API key", "code": 401}}
        patcher, calls = _patch_stream_upstream([
            _fake_stream_response(401, error_body=error)
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 401
        assert resp.json() == error
        assert len(calls) == 1

    def test_upstream_5xx_returned_immediately_no_fallback(self):
        error = {"error": {"message": "Internal Server Error"}}
        patcher, calls = _patch_stream_upstream([
            _fake_stream_response(500, error_body=error)
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 500
        assert len(calls) == 1


# ── 7. Streaming: upstream rate-limit fallback ────────────────────────────────


class TestStreamingRateLimitFallback:
    def test_falls_back_to_second_model_on_upstream_rate_limit(self):
        patcher, calls = _patch_stream_upstream([
            _fake_stream_response(429, error_body=UPSTREAM_RATE_LIMIT_BODY),
            _fake_stream_response(200, chunks=SSE_CHUNKS),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 200
        assert resp.content == b"".join(SSE_CHUNKS)
        assert len(calls) == 2

    def test_second_attempt_uses_next_model_id(self):
        patcher, calls = _patch_stream_upstream([
            _fake_stream_response(429, error_body=UPSTREAM_RATE_LIMIT_BODY),
            _fake_stream_response(200, chunks=SSE_CHUNKS),
        ])
        with patcher:
            CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert calls[0]["json"]["model"] == main_module._MODELS[0]
        assert calls[1]["json"]["model"] == main_module._MODELS[1]

    def test_partial_fallback_succeeds_on_third_model(self):
        patcher, calls = _patch_stream_upstream([
            _fake_stream_response(429, error_body=UPSTREAM_RATE_LIMIT_BODY),
            _fake_stream_response(429, error_body=UPSTREAM_RATE_LIMIT_BODY),
            _fake_stream_response(200, chunks=SSE_CHUNKS),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 200
        assert len(calls) == 3

    def test_all_models_exhausted_returns_503(self):
        n = len(main_module._MODELS)
        patcher, calls = _patch_stream_upstream(
            [_fake_stream_response(429, error_body=UPSTREAM_RATE_LIMIT_BODY)] * n
        )
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "all_models_exhausted"
        assert len(calls) == n

    def test_regular_429_not_treated_as_upstream_rate_limit(self):
        key_limit_body = {"error": {"message": "Rate limit exceeded for your API key"}}
        patcher, calls = _patch_stream_upstream([
            _fake_stream_response(429, error_body=key_limit_body)
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 429
        assert len(calls) == 1


# ── 8. Streaming: network-error fallback ─────────────────────────────────────


class TestStreamingNetworkErrorFallback:
    def test_timeout_triggers_fallback(self):
        patcher, calls = _patch_stream_upstream([
            httpx.TimeoutException("timed out"),
            _fake_stream_response(200, chunks=SSE_CHUNKS),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 200
        assert len(calls) == 2

    def test_connect_error_triggers_fallback(self):
        patcher, calls = _patch_stream_upstream([
            httpx.ConnectError("connection refused"),
            _fake_stream_response(200, chunks=SSE_CHUNKS),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 200
        assert len(calls) == 2

    def test_all_timeouts_returns_503(self):
        n = len(main_module._MODELS)
        patcher, _ = _patch_stream_upstream([httpx.TimeoutException("timed out")] * n)
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "all_models_exhausted"

    def test_timeout_entries_labelled_in_attempted_list(self):
        n = len(main_module._MODELS)
        patcher, _ = _patch_stream_upstream([httpx.TimeoutException("timed out")] * n)
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        for entry in resp.json()["error"]["attempted"]:
            assert "timeout" in entry
