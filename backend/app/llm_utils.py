import asyncio
import json
import logging
import re
import time
from typing import Any, Callable, Dict, Optional, Type

from langchain_core.prompts import PromptTemplate
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger("app.llm_utils")


def extract_llm_content(response: Any) -> str:
    """
    Robustly extracts the text payload from a LangChain LLM response.

    Handles AIMessage-like objects (.content), plain dictionaries holding a
    "content" key, and providers that nest the answer under additional_kwargs
    (e.g. reasoning models exposing "content" / "final_answer").

    Reasoning models served by LM Studio (e.g. Qwen3.x) frequently place the
    actual answer inside ``reasoning_content`` while leaving ``content`` EMPTY
    (verified live: even with ``enable_thinking=false`` the server still emits
    the JSON answer in reasoning_content). When content is blank, the reasoning
    text is the only payload available, so it is returned for JSON parsing —
    returning an empty string here used to make every structured generation
    fail and fall through to the error path.
    """
    # Priority 1: top-level .content. Newer OpenAI-compatible responses may
    # expose content as blocks instead of one string.
    if hasattr(response, "content"):
        content = response.content
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            blocks = []
            for block in content:
                if isinstance(block, str):
                    blocks.append(block)
                elif isinstance(block, dict):
                    value = block.get("text") or block.get("content") or block.get("value")
                    if isinstance(value, str):
                        blocks.append(value)
            joined = "\n".join(part for part in blocks if part.strip())
            if joined:
                return joined
    # Priority 1b: reasoning models answering in reasoning_content with empty content
    if hasattr(response, "additional_kwargs"):
        kwargs = response.additional_kwargs or {}
        reasoning = kwargs.get("reasoning_content") or kwargs.get("reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning
    # Priority 2: dict with "content" (non-blank) or a reasoning fallback
    if isinstance(response, dict):
        if "content" in response and str(response.get("content") or "").strip():
            return str(response.get("content") or "")
        reasoning = response.get("reasoning_content") or response.get("reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning
    # Priority 3: provider-specific kwargs (reasoning models)
    if hasattr(response, "additional_kwargs"):
        kwargs = response.additional_kwargs or {}
        if kwargs.get("content"):
            return str(kwargs["content"])
        if kwargs.get("final_answer"):
            return str(kwargs["final_answer"])
    # Priority 4: response_metadata
    if hasattr(response, "response_metadata") and response.response_metadata:
        metadata = response.response_metadata
        if metadata.get("content"):
            return str(metadata["content"])
        reasoning = metadata.get("reasoning_content") or metadata.get("reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning
    return str(response)


def strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences (```json / ```) surrounding a JSON payload."""
    clean = text.strip()
    if clean.startswith("```json"):
        clean = clean[7:]
    elif clean.startswith("```"):
        clean = clean[3:]
    if clean.endswith("```"):
        clean = clean[:-3]
    return clean.strip()


def parse_json_object(text: str) -> Dict[str, Any]:
    """Parse safe, common local-model JSON variants without another LLM call.

    Surrounding prose/fences and trailing commas are repaired. Truncated
    strings or missing values remain errors because guessing would corrupt
    requirements or audit evidence.
    """
    clean = strip_markdown_fences(text)
    candidates = [clean]
    first, last = clean.find("{"), clean.rfind("}")
    if first >= 0 and last > first:
        candidates.append(clean[first:last + 1])
    for candidate in candidates:
        for value in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise json.JSONDecodeError("No complete JSON object found", clean, 0)


def normalize_output(parsed_output: Any) -> Dict[str, Any]:
    """Convert a Pydantic model or mapping into a plain dict."""
    if hasattr(parsed_output, "model_dump"):
        return parsed_output.model_dump()
    if isinstance(parsed_output, dict):
        return parsed_output
    return dict(parsed_output)


async def invoke_llm_structured(
    llm,
    prompt_template: PromptTemplate,
    schema: Type[BaseModel],
    variables: Dict[str, Any],
    *,
    description: str = "LLM structured output",
    format_instructions: str,
    validate: Optional[Callable[[Dict[str, Any]], bool]] = None,
    native_json_schema: bool = False,
) -> Dict[str, Any]:
    """
    Invoke the LLM and return the parsed result as a plain dict.

    Attempt 1 (primary): ONE raw invocation carrying the full
    ``format_instructions``; markdown fences are stripped and the content is
    parsed as JSON. Raw parsing is the primary path because the deployed
    LM Studio / Qwen build does not honor json-schema enforcement —
    ``with_structured_output`` fails on EVERY call there (the answer lands in
    ``reasoning_content``), which silently DOUBLED each generation's latency
    and token cost before the fallback ever ran. ``extract_llm_content``
    recovers the answer from ``reasoning_content`` when ``content`` is empty.

    Attempt 2 (fallback): ``prompt_template | llm.with_structured_output(schema)``
    for providers whose structured-output mode actually works.

    The same optional ``validate`` callback gates both attempts.

    Returns the parsed result as a plain dict. Raises ``RuntimeError`` when both
    attempts fail so the caller can apply its own heuristic fallback.
    """
    deadline = time.monotonic() + settings.LLM_STRUCTURED_TOTAL_TIMEOUT_SECONDS

    async def bounded(awaitable):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Calls construct the coroutine before entering this helper. Close
            # it when the shared budget is already exhausted to avoid an
            # "unawaited coroutine" warning during timeout handling.
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise TimeoutError(f"{description} exceeded its total inference budget")
        return await asyncio.wait_for(
            awaitable,
            timeout=min(settings.LLM_REQUEST_TIMEOUT_SECONDS, remaining),
        )

    inference_llm = llm
    if native_json_schema:
        # LM Studio supports OpenAI-compatible response_format=json_schema.
        # This constrains token generation itself, unlike prompt-only requests
        # where a long local-model response may contain prose or no JSON.
        schema_name = re.sub(r"[^a-zA-Z0-9_-]", "_", schema.__name__)[:64]
        native_schema = schema.model_json_schema()
        # Pydantic defaults make fields optional in generated JSON Schema.
        # AuditorOutput uses defaults for backwards-compatible parsing, but a
        # constrained generation must emit every top-level field; otherwise the
        # model can legally return `{}` and the defaults resemble a real audit.
        native_schema["required"] = list(native_schema.get("properties", {}).keys())
        inference_llm = llm.bind(response_format={
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": False,
                "schema": native_schema,
            },
        })

    # Attempt 1: single raw invocation + tolerant JSON parsing
    raw_failure = "unknown"
    raw_text = ""
    try:
        raw_variables = {**variables, "format_instructions": format_instructions}
        raw_chain = prompt_template | inference_llm
        raw_response = await bounded(raw_chain.ainvoke(raw_variables))
        raw_text = extract_llm_content(raw_response)
        result = parse_json_object(raw_text)
        result = schema.model_validate(result).model_dump()
        if validate is None or validate(result):
            logger.info(f"Successfully parsed {description} from raw LLM output.")
            return result
        raw_failure = f"raw output failed validation for {description}"
        logger.warning(f"{raw_failure}.")
    except TimeoutError as e:
        # A second structured-output request cannot repair a provider timeout;
        # it only makes the user wait through the remaining total budget before
        # failing the same way. Let the caller apply its safe fallback now.
        raise RuntimeError(f"{description} timed out before returning structured output") from e
    except Exception as e:
        raw_failure = str(e)
        metadata = getattr(locals().get("raw_response"), "response_metadata", {}) or {}
        logger.warning(
            "Raw parse failed for %s: %s (content_chars=%s, finish_reason=%s)",
            description, e, len(raw_text), metadata.get("finish_reason"),
        )

    metadata = getattr(locals().get("raw_response"), "response_metadata", {}) or {}
    if metadata.get("finish_reason") == "length":
        raw_failure += "; finish_reason=length (output token limit)"
    elif "raw_response" in locals() and not raw_text.strip():
        raw_failure += "; empty model output"

    # A malformed but non-empty response can often be repaired cheaply without
    # sending the large requirements/documents prompt a second time. This call
    # sees only the broken output and the compact contract.
    if raw_text.strip():
        try:
            repair_prompt = PromptTemplate.from_template(
                "Repair the candidate into one valid JSON object. Preserve its facts; "
                "do not invent evidence. Return JSON only.\n\nRequired contract:\n{contract}\n\n"
                "Candidate:\n{candidate}"
            )
            repair_chain = repair_prompt | inference_llm
            repair_response = await bounded(repair_chain.ainvoke({
                "contract": format_instructions,
                "candidate": raw_text[:20000],
            }))
            repaired = parse_json_object(extract_llm_content(repair_response))
            result = schema.model_validate(repaired).model_dump()
            if validate is None or validate(result):
                logger.info("Successfully repaired %s without repeating the source prompt.", description)
                return result
            raw_failure = f"repaired output failed validation for {description}"
        except TimeoutError as exc:
            raise RuntimeError(f"{description} timed out during compact JSON repair") from exc
        except Exception as exc:
            raw_failure = f"{raw_failure}; compact repair failed: {exc}"
            logger.warning("Compact JSON repair failed for %s: %s", description, exc)

    if not settings.LLM_STRUCTURED_PROVIDER_FALLBACK:
        raise RuntimeError(
            f"Unable to parse {description} from the model response: {raw_failure}"
        )

    # Attempt 2: provider structured output (opt-in fallback)
    try:
        logger.info(f"Attempting .with_structured_output() for {description}.")
        chain = prompt_template | llm.with_structured_output(schema)
        parsed_output = await bounded(chain.ainvoke(variables))
        result = normalize_output(parsed_output)
        if validate is None or validate(result):
            logger.info(f"Successfully obtained structured output for {description}.")
            return result
        raise RuntimeError(f"structured output failed validation for {description}")
    except Exception as e2:
        raise RuntimeError(
            f"All parsing attempts failed for {description} (raw attempt: {raw_failure}): {str(e2)}"
        ) from e2
