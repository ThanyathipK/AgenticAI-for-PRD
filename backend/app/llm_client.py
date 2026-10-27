import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger("app.llm_client")


class LMStudioError(Exception):
    """Base class for LM Studio inference failures (service/domain layer).

    Raised by :func:`call_lm_studio` instead of framework-specific web errors so
    the client service stays decoupled from the HTTP framework and can be consumed
    by route and non-route callers alike. HTTP routes are responsible for mapping
    these exceptions onto the appropriate ``HTTPException`` status codes.
    """


class LMStudioGatewayError(LMStudioError):
    """LM Studio responded with an error HTTP status (e.g. 4xx/5xx)."""


class LMStudioUnavailableError(LMStudioError):
    """LM Studio is unreachable (offline, DNS/connection error or timeout)."""


class LMStudioOutputParsingError(LMStudioError):
    """LM Studio returned a payload that could not be parsed as JSON."""


#: Max attempts (initial + retries) for a single LM Studio inference call.
LM_INFERENCE_RETRY_ATTEMPTS = 2
#: Base back-off (seconds) between retries; multiplied by the attempt index.
LM_INFERENCE_RETRY_BACKOFF_SECONDS = 0.5
#: Upper bound for a server-provided ``Retry-After`` delay. A header is trusted
#: only up to this cap: an unbounded value (e.g. a misbehaving proxy answering
#: ``Retry-After: 3600``) would otherwise suspend the request coroutine — and
#: with it the user's chat turn — for an hour before the first retry.
LM_INFERENCE_RETRY_AFTER_MAX_SECONDS = 5.0


def _retry_backoff(attempt: int, retry_after: Optional[str] = None) -> float:
    """Return the wait before retrying, preferring the server's ``Retry-After``.

    The ``Retry-After`` value is honoured only when it parses as a positive
    integer AND stays below :data:`LM_INFERENCE_RETRY_AFTER_MAX_SECONDS`;
    anything larger (or unparsable) falls back to the linear back-off.
    """
    if retry_after:
        try:
            seconds = int(retry_after)
            if 0 < seconds <= LM_INFERENCE_RETRY_AFTER_MAX_SECONDS:
                return float(seconds)
        except (TypeError, ValueError):
            pass
    return LM_INFERENCE_RETRY_BACKOFF_SECONDS * attempt


# CHAT 6.1 — LLM transport shared by the chat paths (CHAT 5.4 direct, CHAT 4.1.1
#            fact extraction) and by the agent flows: OpenAI-compatible
#            POST {LM_STUDIO_URL}/chat/completions over httpx, non-streaming.
#            Retries 429/5xx up to LM_INFERENCE_RETRY_ATTEMPTS with a capped
#            Retry-After/linear back-off, then raises the domain errors mapped to
#            503 (unreachable) / 502 (gateway) / 422 (unparsable JSON).
async def call_lm_studio(
    prompt_messages: List[Dict[str, str]],
    response_format_schema: Any = None,
    max_tokens: int = 1500,
) -> Dict[str, Any]:
    """
    Direct low-latency route utility to LM Studio.
    Forces strict JSON outputs using custom parameters, staying within MacBook memory parameters.

    ``max_tokens`` defaults to the 1500 safe generation budget for short
    structured payloads. Whole-document generation (e.g. the LaTeX PRD export)
    passes a larger allowance - a document body alone is several thousand tokens.

    Raises:
        LMStudioGatewayError: LM Studio returned a non-2xx HTTP response.
        LMStudioUnavailableError: LM Studio could not be reached (offline/timeout).
        LMStudioOutputParsingError: LM Studio returned content that was not valid JSON.
    """
    headers = {
        "Authorization": f"Bearer {settings.LM_STUDIO_API_KEY}",
        "Content-Type": "application/json"
    }

    # Configure request payload staying strictly inside context window memory budgets
    payload: Dict[str, Any] = {
        "model": settings.LM_STUDIO_MODEL_FALLBACK,
        "messages": prompt_messages,
        "temperature": settings.TEMPERATURE,
        "max_tokens": max_tokens,  # Retain safe generation budget within the 8192 limit
        "stream": False
    }

    # If schema is specified, request structured output constraints from LM Studio JSON mode
    if response_format_schema:
        payload["response_format"] = {
            "type": "json_object",
            "schema": response_format_schema.model_json_schema()
        }

    # --- Retry transient failures (network blips, 5xx, 429) ---------------------
    # A transient LM Studio failure can otherwise surface as a spurious 502/503
    # to the UI and trigger the frontend fallback. Retry a couple of times with a
    # short back-off so transient issues self-heal; real outages still raise the
    # domain exceptions below.
    for attempt in range(1, LM_INFERENCE_RETRY_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                logger.info(
                    f"Dispatching inference task to local LM Studio at "
                    f"{settings.LM_STUDIO_URL} (attempt {attempt}/{LM_INFERENCE_RETRY_ATTEMPTS})"
                )
                response = await client.post(
                    f"{settings.LM_STUDIO_URL}/chat/completions",
                    headers=headers,
                    json=payload
                )
                # Transient gateway/rate-limit responses are worth a retry.
                if response.status_code in (429,) or response.status_code >= 500:
                    if attempt < LM_INFERENCE_RETRY_ATTEMPTS:
                        headers_map = getattr(response, "headers", None)
                        retry_after = (
                            headers_map.get("retry-after")
                            if isinstance(headers_map, dict)
                            else None
                        )
                        await asyncio.sleep(_retry_backoff(attempt, retry_after))
                        continue
                response.raise_for_status()
                result = response.json()

            # Access response text safely
            content_text = result["choices"][0]["message"]["content"]
            logger.info("Successfully fetched response from local LLM.")
            return json.loads(content_text) if response_format_schema else {"text": content_text}

        except httpx.HTTPStatusError as http_err:
            if (
                http_err.response.status_code in (429,)
                or http_err.response.status_code >= 500
            ) and attempt < LM_INFERENCE_RETRY_ATTEMPTS:
                logger.warning(
                    "LM Studio transient HTTP %s; retrying (%s/%s).",
                    http_err.response.status_code,
                    attempt,
                    LM_INFERENCE_RETRY_ATTEMPTS,
                )
                await asyncio.sleep(_retry_backoff(attempt))
                continue
            logger.error(f"LM Studio server returned status error: {http_err.response.status_code} - {http_err.response.text}")
            raise LMStudioGatewayError(
                f"Inference gateway error: {str(http_err)}"
            ) from http_err
        except httpx.RequestError as req_err:
            if attempt < LM_INFERENCE_RETRY_ATTEMPTS:
                logger.warning(
                    "LM Studio network error (%s); retrying (%s/%s).",
                    req_err,
                    attempt,
                    LM_INFERENCE_RETRY_ATTEMPTS,
                )
                await asyncio.sleep(_retry_backoff(attempt))
                continue
            logger.error(f"Failed to connect to local LM Studio instance: {str(req_err)}")
            raise LMStudioUnavailableError(
                "LM Studio is offline or unavailable. Ensure it runs on localhost:1234 with API keys."
            ) from req_err
        except json.JSONDecodeError as json_err:
            logger.error(f"Failed to parse LLM structured output block: {str(json_err)}")
            raise LMStudioOutputParsingError(
                "Local LLM output failed to resolve as a valid compliance schema."
            ) from json_err

    # Defensive terminal guard: the loop above always returns or raises today,
    # but if the attempt count or control flow ever changes, silently falling
    # off the end would return ``None`` and crash the caller with an opaque
    # AttributeError instead of a clean 503.
    raise LMStudioUnavailableError(
        "LM Studio did not respond after the configured retry attempts."
    )


# ==========================================
# FINDING #40 — LM STUDIO HEALTH / READINESS PROBE
# ==========================================
LM_STUDIO_PROBE_TIMEOUT_SECONDS = 3.0
# Short TTL cache so the frontend can poll readiness without hammering the
# local gateway between polls (one LLM call per ~15s worst case).
LM_STUDIO_HEALTH_CACHE_TTL_SECONDS = 15.0

_health_cache: Dict[str, Any] = {}
_health_cache_ts: float = 0.0


def reset_lm_studio_health_cache() -> None:
    """Clear the in-process TTL cache (exposed for tests and manual refresh)."""
    global _health_cache_ts
    _health_cache.clear()
    _health_cache_ts = 0.0


# CHAT 6.2 — LM Studio readiness probe (GET /models, TTL-cached so repeated UI
#            polls cannot hammer the gateway). Never raises: an offline/slow
#            gateway is reported as online=False + error text. Consumed by the
#            health route (CHAT 6.2.1) and the app lifespan startup probe.
async def check_lm_studio_health(client_factory: Any = None) -> Dict[str, Any]:
    """
    Probe LM Studio's OpenAI-compatible ``GET /models`` endpoint.

    Unlike :func:`call_lm_studio` this probe **never raises**: it returns a
    structured readiness dict so callers (startup check, ``GET /api/health``,
    UI polls) can degrade gracefully when the local LLM is offline (Finding #40).

    Returns::

        {
            "online": bool,
            "latency_ms": float | None,
            "target_model": settings.LM_STUDIO_MODEL_FALLBACK,
            "model_loaded": bool,
            "loaded_models": [model ids currently served],
            "error": str | None,
            "checked_at": ISO-8601 UTC timestamp,
        }

    The result is cached for ``LM_STUDIO_HEALTH_CACHE_TTL_SECONDS``.
    ``client_factory`` is a dependency-injection seam used by unit tests; it
    defaults to :class:`httpx.AsyncClient`.
    """
    global _health_cache, _health_cache_ts

    now = time.monotonic()
    if _health_cache and (now - _health_cache_ts) < LM_STUDIO_HEALTH_CACHE_TTL_SECONDS:
        return dict(_health_cache)

    target_model = settings.LM_STUDIO_MODEL_FALLBACK
    result: Dict[str, Any] = {
        "online": False,
        "latency_ms": None,
        "target_model": target_model,
        "model_loaded": False,
        "loaded_models": [],
        "error": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    headers = {
        "Authorization": f"Bearer {settings.LM_STUDIO_API_KEY}",
        "Content-Type": "application/json",
    }
    probe_url = f"{settings.LM_STUDIO_URL.rstrip('/')}/models"
    started = time.perf_counter()
    factory = client_factory or httpx.AsyncClient

    try:
        async with factory(timeout=LM_STUDIO_PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(probe_url, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as http_err:
        result["error"] = (
            f"LM Studio returned HTTP {http_err.response.status_code} on {probe_url}."
        )
    except httpx.RequestError:
        result["error"] = "LM Studio is offline or unreachable."
    except Exception as err:  # malformed JSON / unexpected failures must not crash
        result["error"] = f"Health probe failed: {err}"

    if result["error"] is None:
        try:
            loaded_ids = [
                str(model.get("id", ""))
                for model in (payload.get("data", []) or [])
                if model.get("id")
            ]
            result.update({
                "online": True,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "loaded_models": loaded_ids,
                "model_loaded": target_model in loaded_ids,
            })
            if result["model_loaded"]:
                logger.info(
                    f"LM Studio health probe: online, model '{target_model}' loaded. "
                    f"loaded={loaded_ids}"
                )
            else:
                logger.warning(
                    f"LM Studio is online but model '{target_model}' is NOT loaded. "
                    f"loaded={loaded_ids or 'none'}"
                )
        except Exception as err:
            result["error"] = f"Malformed health payload: {err}"
    else:
        logger.warning(
            f"LM Studio health probe failed: {result['error']} "
            f"(gateway={settings.LM_STUDIO_URL}). Inference calls will fail with 503 "
            f"until the local server is started and a model is loaded."
        )

    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    _health_cache = dict(result)
    _health_cache_ts = time.monotonic()
    return dict(result)