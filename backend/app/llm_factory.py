"""Shared LangChain LLM client factory.

Centralises construction of the single ``ChatOpenAI`` instance used across the
multi-agent orchestrator (``app.agents``) and the semantic/intent/chat services
(``app.semantic_service``).

Historically ``semantic_service`` imported ``llm`` directly from ``app.agents``
(which constructed it at module scope), while ``app.agents`` had to import
``app.semantic_service`` only *inside* function bodies to avoid a circular import.

Having both modules consume ``llm`` / ``parser`` from this module instead breaks
that load-order dependency. ``app.semantic_service`` no longer imports from
``app.agents``, so ``app.agents`` can import its semantic helpers at module
scope without a deferred-import workaround.
"""
import logging
from typing import Any, Dict

from langchain_core.output_parsers import JsonOutputParser
from langchain_openai import ChatOpenAI

from app.config import settings

logger = logging.getLogger("app.llm_factory")


class LMStudioChatOpenAI(ChatOpenAI):
    """Preserve LM Studio's non-standard ``reasoning_content`` field.

    Qwen 3.5 can return requested JSON entirely in ``reasoning_content`` with
    an empty standard ``content`` field even when thinking is disabled. The
    OpenAI SDK retains that extra field, but LangChain's converter drops it.
    Attach it to ``AIMessage.additional_kwargs`` so the structured parser's
    existing reasoning fallback can consume the actual model response.
    """

    def _create_chat_result(self, response: Any, generation_info: Any = None):
        result = super()._create_chat_result(response, generation_info)
        response_dict = response if isinstance(response, dict) else response.model_dump(
            exclude={"choices": {"__all__": {"message": {"parsed"}}}},
            warnings=False,
        )
        choices = response_dict.get("choices") or []
        for generation, choice in zip(result.generations, choices):
            message = choice.get("message") or {}
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            if isinstance(reasoning, str) and reasoning.strip():
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result


# Safe context limit mapping designed for MacBook Air/Pro M4 16GB execution bounds
def build_llm(max_tokens: int = 3000) -> ChatOpenAI:
    """Construct the shared ChatOpenAI client from app.config settings.

    ``max_tokens`` defaults to the 3000 structured-output budget shared by the
    gatherer/auditor nodes.

    Exposed as a function so tests and callers can build/replace the client
    without recreating module state by hand.
    """
    kwargs: Dict[str, Any] = {
        "base_url": settings.LM_STUDIO_URL,
        "api_key": settings.LM_STUDIO_API_KEY,
        "model": settings.LM_STUDIO_MODEL_FALLBACK,
        "temperature": settings.TEMPERATURE,
        "max_tokens": max_tokens,  # Optimized for structured output
        "seed": 42,  # Deterministic sampling; declared as a first-class param (avoids model_kwargs warning)
        "timeout": settings.LLM_REQUEST_TIMEOUT_SECONDS,
        "max_retries": 0,  # workflow-level retry/repair owns the latency budget
    }

    if settings.LM_STUDIO_DISABLE_THINKING:
        # Qwen3.x are reasoning models. Left untouched, the model spends its whole
        # max_tokens budget emitting `reasoning_content` and returns an EMPTY
        # `content` ("finish_reason": "length"), so JsonOutputParser receives an
        # empty string and the workflow fails every structured-JSON call. Turning
        # off thinking makes the model answer directly in `content`, which is both
        # fast and JSON-parseable. `extra_body` merges chat_template_kwargs into
        # the request body, matching the LM Studio OpenAI-compatible contract.
        kwargs["extra_body"] = {
            "chat_template_kwargs": {"enable_thinking": False}
        }

    return LMStudioChatOpenAI(**kwargs)


# Json Output Parser for strict structured JSON outputs
parser = JsonOutputParser()

# Single shared client for the whole process. Built eagerly at import time so the
# rest of the app can reference it without re-invoking the factory everywhere.
llm = build_llm()
