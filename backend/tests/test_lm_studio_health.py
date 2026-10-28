"""
Unit tests for Finding #40: LM Studio health check & graceful degradation.

Verifies that:
- ``check_lm_studio_health`` never raises across online / offline / HTTP error /
  malformed-payload paths and reports the configured model's load state;
- the probe result is TTL-cached so UI pollers don't hammer the local gateway;
- ``GET /api/health`` surfaces the live LM Studio readiness state while keeping
  the app-level ``200 online`` liveness contract intact.

Run from the ``backend`` directory::

    ../venv/bin/python -m pytest tests/test_lm_studio_health.py -v
"""

import pytest
from fastapi.testclient import TestClient

import httpx
import app.llm_client as llm_client
from app.main import app


# ==========================================
# Fake httpx plumbing
# ==========================================
class _FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://localhost:1234/v1/models")
            response = httpx.Response(self.status_code, text=self.text)
            raise httpx.HTTPStatusError("HTTP error", request=request, response=response)
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Emulates the subset of :class:`httpx.AsyncClient` the probe uses."""

    def __init__(self, *, result=None, error=None, **kwargs):
        self._result = result
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.last_url = url
        self.last_headers = headers
        if self._error is not None:
            raise self._error
        return self._result


def _factory(*, result=None, error=None, on_call=None):
    calls = {"n": 0}

    def build(**kwargs):
        calls["n"] += 1
        if on_call:
            on_call(calls["n"])
        return _FakeAsyncClient(result=result, error=error)

    return build, calls
# ==========================================
# check_lm_studio_health
# ==========================================
@pytest.mark.asyncio
async def test_probe_online_with_target_model_loaded():
    target = llm_client.settings.LM_STUDIO_MODEL_FALLBACK
    payload = {
        "object": "list",
        "data": [
            {"id": target, "object": "model"},
            {"id": "other-model", "object": "model"},
        ],
    }
    factory, _ = _factory(result=_FakeResponse(payload))
    result = await llm_client.check_lm_studio_health(client_factory=factory)

    assert result["online"] is True
    assert result["model_loaded"] is True
    assert result["target_model"] == target
    assert result["loaded_models"] == [target, "other-model"]
    assert result["error"] is None
    assert result["latency_ms"] is not None
    assert result["checked_at"]


@pytest.mark.asyncio
async def test_probe_online_but_configured_model_not_loaded():
    payload = {"data": [{"id": "mistral-7b", "object": "model"}]}
    factory, _ = _factory(result=_FakeResponse(payload))
    status = await llm_client.check_lm_studio_health(client_factory=factory)

    assert status["online"] is True
    assert status["model_loaded"] is False
    assert status["loaded_models"] == ["mistral-7b"]
    assert status["error"] is None


@pytest.mark.asyncio
async def test_probe_offline_returns_graceful_dict_not_raise():
    factory, _ = _factory(error=httpx.ConnectError("connection refused"))
    status = await llm_client.check_lm_studio_health(client_factory=factory)

    assert status["online"] is False
    assert status["model_loaded"] is False
    assert status["loaded_models"] == []
    assert status["error"] == "LM Studio is offline or unreachable."
    assert status["latency_ms"] is None


@pytest.mark.asyncio
async def test_probe_http_error_reported():
    factory, _ = _factory(result=_FakeResponse(payload="boom", status_code=503))
    status = await llm_client.check_lm_studio_health(client_factory=factory)

    assert status["online"] is False
    assert "503" in status["error"]


@pytest.mark.asyncio
async def test_probe_survives_malformed_payload():
    # Valid HTTP 200 but a body the parser cannot read must not crash the probe.
    factory, _ = _factory(result=_FakeResponse(payload="not-json-dict"))
    status = await llm_client.check_lm_studio_health(client_factory=factory)

    assert status["online"] is False
    assert "Malformed health payload" in status["error"]


@pytest.mark.asyncio
async def test_probe_result_is_ttl_cached():
    payload = {"data": [{"id": llm_client.settings.LM_STUDIO_MODEL_FALLBACK, "object": "model"}]}
    factory, calls = _factory(result=_FakeResponse(payload))

    first = await llm_client.check_lm_studio_health(client_factory=factory)
    second = await llm_client.check_lm_studio_health(client_factory=factory)
    third = await llm_client.check_lm_studio_health(client_factory=factory)

    # Only the first call should hit the fake network; later ones come from cache.
    assert calls["n"] == 1
    assert first == second == third

    # A fresh run performs the probe again after the cache is reset.
    llm_client.reset_lm_studio_health_cache()
    await llm_client.check_lm_studio_health(client_factory=factory)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_forced_probe_bypasses_recent_online_cache():
    online = {"data": [{"id": llm_client.settings.LM_STUDIO_MODEL_FALLBACK}]}
    factory, calls = _factory(result=_FakeResponse(online))
    await llm_client.check_lm_studio_health(client_factory=factory)

    await llm_client.check_lm_studio_health(client_factory=factory, force=True)

    assert calls["n"] == 2

# ==========================================================
# GET /api/health endpoint
# ==========================================================
def _fake_probe_offline():
    model = llm_client.settings.LM_STUDIO_MODEL_FALLBACK

    async def _probe(client_factory=None, *, force=False):
        return {
            "online": False,
            "latency_ms": None,
            "target_model": model,
            "model_loaded": False,
            "loaded_models": [],
            "error": "LM Studio is offline or unreachable.",
            "checked_at": "2026-08-15T12:00:00+00:00",
        }
    return _probe


def _fake_probe_online():
    model = llm_client.settings.LM_STUDIO_MODEL_FALLBACK

    async def _probe(client_factory=None, *, force=False):
        return {
            "online": True,
            "latency_ms": 12.3,
            "target_model": model,
            "model_loaded": True,
            "loaded_models": [model, "codesqwen"],
            "error": None,
            "checked_at": "2026-08-15T12:00:00+00:00",
        }
    return _probe


def test_health_endpoint_reports_app_liveness_and_lm_offline(monkeypatch):
    import app.routes.chat as chat_route
    monkeypatch.setattr(chat_route, "check_lm_studio_health", _fake_probe_offline())

    # No `with` block on purpose: lifespan (migrations/seeding) must not run here.
    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "online"  # app liveness contract preserved
    assert payload["lm_studio_online"] is False
    assert payload["lm_studio_model"] == llm_client.settings.LM_STUDIO_MODEL_FALLBACK
    assert payload["lm_studio_model_loaded"] is False
    assert payload["lm_studio_loaded_models"] == []
    assert "offline" in payload["lm_studio_error"]
    assert payload["lm_studio_last_checked"]


def test_health_endpoint_reports_lm_online_with_model(monkeypatch):
    import app.routes.chat as chat_route
    monkeypatch.setattr(chat_route, "check_lm_studio_health", _fake_probe_online())

    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["lm_studio_online"] is True
    assert payload["lm_studio_model_loaded"] is True
    assert payload["lm_studio_latency_ms"] == 12.3
    assert payload["lm_studio_error"] is None
    assert llm_client.settings.LM_STUDIO_MODEL_FALLBACK in payload["lm_studio_loaded_models"]

@pytest.fixture(autouse=True)
def _reset_cache():
    llm_client.reset_lm_studio_health_cache()
    yield
    llm_client.reset_lm_studio_health_cache()
