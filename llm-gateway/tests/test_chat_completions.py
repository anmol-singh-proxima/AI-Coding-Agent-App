"""
Tests for POST /v1/chat/completions

All outgoing httpx calls are intercepted so no real network traffic is made.

Scenarios covered:
  1. Input validation (FastAPI/Pydantic layer)
  2. Configuration guard (missing API key, stream=true)
  3. Successful forwarding (happy path, payload rewriting, field pass-through)
  4. Upstream rate-limit fallback (model cycling)
  5. Network-error fallback (timeout, connection error)
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

import app.main as main_module
from app.main import app

CLIENT = TestClient(app)

# ── helpers ───────────────────────────────────────────────────────────────────


def _fake_response(status_code: int, body: dict) -> MagicMock:
    """Build a minimal httpx.Response mock."""
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.json.return_value = body
    return r


def _patch_upstream(responses: list):
    """Patch httpx.AsyncClient so successive .post() calls consume *responses* in order.

    Each entry is either a fake response (MagicMock) or an exception instance to raise.
    Returns (patcher, calls) where calls records the kwargs of every .post() invocation.

    Usage:
        patcher, calls = _patch_upstream([...])
        with patcher:
            resp = CLIENT.post(...)
        assert calls[0]["json"]["model"] == "..."
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


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def with_api_key(monkeypatch):
    """Inject a dummy API key so the key-guard doesn't short-circuit every test.
    Tests that specifically need an empty key override this with their own setattr."""
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
        """An empty list is valid at the schema level; upstream decides whether to accept it."""
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

    def test_stream_true_returns_400(self):
        resp = CLIENT.post(
            "/v1/chat/completions",
            json={**VALID_BODY, "stream": True},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["type"] == "unsupported_operation"

    def test_stream_true_does_not_reach_upstream(self):
        patcher, calls = _patch_upstream([])
        with patcher:
            CLIENT.post("/v1/chat/completions", json={**VALID_BODY, "stream": True})
        assert len(calls) == 0

    def test_stream_false_is_accepted(self):
        patcher, _ = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            resp = CLIENT.post(
                "/v1/chat/completions",
                json={**VALID_BODY, "stream": False},
            )
        assert resp.status_code == 200

    def test_stream_defaults_to_false_when_omitted(self):
        """stream is optional — omitting it must not trigger the 400 guard."""
        patcher, _ = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code != 400


# ── 3. Successful forwarding ──────────────────────────────────────────────────


class TestForwarding:
    def test_happy_path_returns_upstream_body(self):
        upstream = {"choices": [{"message": {"role": "assistant", "content": "Hello!"}}]}
        patcher, _ = _patch_upstream([_fake_response(200, upstream)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 200
        assert resp.json() == upstream

    def test_caller_model_is_replaced_with_first_model_in_list(self):
        """Whatever model the caller sends must be overridden by _MODELS[0]."""
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post(
                "/v1/chat/completions",
                json={**VALID_BODY, "model": "caller-chosen-model"},
            )
        assert calls[0]["json"]["model"] == main_module._MODELS[0]

    def test_authorization_header_contains_api_key(self):
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert calls[0]["headers"]["Authorization"] == "Bearer sk-test-key"

    def test_extra_openai_fields_forwarded_unchanged(self):
        """Fields not in ChatRequest (tools, top_p, etc.) must reach the upstream."""
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post(
                "/v1/chat/completions",
                json={
                    **VALID_BODY,
                    "top_p": 0.95,
                    "tools": [{"type": "function", "function": {"name": "my_fn"}}],
                },
            )
        payload = calls[0]["json"]
        assert payload["top_p"] == 0.95
        assert "tools" in payload

    def test_temperature_and_max_tokens_forwarded(self):
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post(
                "/v1/chat/completions",
                json={**VALID_BODY, "temperature": 0.7, "max_tokens": 256},
            )
        payload = calls[0]["json"]
        assert payload["temperature"] == 0.7
        assert payload["max_tokens"] == 256

    def test_none_optional_fields_excluded_from_upstream_payload(self):
        """Optional fields not provided must not appear in the forwarded payload."""
        patcher, calls = _patch_upstream([_fake_response(200, {"choices": []})])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        payload = calls[0]["json"]
        assert "temperature" not in payload
        assert "max_tokens" not in payload

    def test_upstream_401_returned_immediately_without_fallback(self):
        error = {"error": {"message": "Missing Authentication header", "code": 401}}
        patcher, calls = _patch_upstream([_fake_response(401, error)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 401
        assert resp.json() == error
        assert len(calls) == 1

    def test_upstream_5xx_returned_immediately_without_fallback(self):
        error = {"error": {"message": "Internal Server Error"}}
        patcher, calls = _patch_upstream([_fake_response(500, error)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 500
        assert len(calls) == 1


# ── 4. Upstream rate-limit fallback ──────────────────────────────────────────


class TestUpstreamRateLimitFallback:
    def test_falls_back_to_second_model_when_first_rate_limited(self):
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
        """Each fallback attempt must use the next model id in _MODELS."""
        patcher, calls = _patch_upstream([
            _fake_response(429, UPSTREAM_RATE_LIMIT_BODY),
            _fake_response(200, {"choices": []}),
        ])
        with patcher:
            CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert calls[0]["json"]["model"] == main_module._MODELS[0]
        assert calls[1]["json"]["model"] == main_module._MODELS[1]

    def test_partial_fallback_succeeds_on_third_model(self):
        success = {"choices": [{"message": {"content": "from model 3"}}]}
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
        patcher, calls = _patch_upstream(
            [_fake_response(429, UPSTREAM_RATE_LIMIT_BODY)] * n
        )
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "all_models_exhausted"
        assert len(calls) == n

    def test_all_models_exhausted_lists_every_attempted_model(self):
        n = len(main_module._MODELS)
        patcher, _ = _patch_upstream(
            [_fake_response(429, UPSTREAM_RATE_LIMIT_BODY)] * n
        )
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        attempted = resp.json()["error"]["attempted"]
        assert len(attempted) == n
        for model_id in main_module._MODELS:
            assert any(model_id in entry for entry in attempted)

    def test_all_exhausted_response_includes_last_upstream_error(self):
        n = len(main_module._MODELS)
        patcher, _ = _patch_upstream(
            [_fake_response(429, UPSTREAM_RATE_LIMIT_BODY)] * n
        )
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.json()["error"]["last_upstream_error"] == UPSTREAM_RATE_LIMIT_BODY

    def test_regular_429_not_treated_as_upstream_rate_limit(self):
        """A 429 that is NOT 'Provider returned error' (e.g. our own key hitting
        OpenRouter's limit) must be returned immediately without trying more models."""
        key_limit_body = {"error": {"message": "Rate limit exceeded for your API key"}}
        patcher, calls = _patch_upstream([_fake_response(429, key_limit_body)])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 429
        assert len(calls) == 1


# ── 5. Network-error fallback ─────────────────────────────────────────────────


class TestNetworkErrorFallback:
    def test_timeout_triggers_fallback_to_next_model(self):
        success = {"choices": [{"message": {"content": "ok"}}]}
        patcher, calls = _patch_upstream([
            httpx.TimeoutException("timed out"),
            _fake_response(200, success),
        ])
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 200
        assert len(calls) == 2

    def test_connect_error_triggers_fallback_to_next_model(self):
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
        patcher, _ = _patch_upstream(
            [httpx.TimeoutException("timed out")] * n
        )
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "all_models_exhausted"

    def test_timeout_entries_labelled_in_attempted_list(self):
        n = len(main_module._MODELS)
        patcher, _ = _patch_upstream(
            [httpx.TimeoutException("timed out")] * n
        )
        with patcher:
            resp = CLIENT.post("/v1/chat/completions", json=VALID_BODY)
        for entry in resp.json()["error"]["attempted"]:
            assert "timeout" in entry

    def test_mixed_errors_all_exhaust_to_503(self):
        """Rate limit, timeout, and connection error interleaved all trigger fallback."""
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
