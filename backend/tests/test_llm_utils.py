"""Structured-output repair and inference-budget regression tests."""
import asyncio
import time

import pytest
from langchain_core.messages import AIMessage
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from app.config import settings
from app.llm_factory import LMStudioChatOpenAI
from app.llm_utils import extract_llm_content, invoke_llm_structured, parse_json_object


class SmallOutput(BaseModel):
    name: str
    count: int


class SlowRunnable(RunnableLambda):
    def with_structured_output(self, _schema):
        return self


def test_parse_json_object_repairs_fences_prose_and_trailing_commas():
    parsed = parse_json_object('Answer:\n```json\n{"name":"ok","items":[1,2,],}\n```')
    assert parsed == {"name": "ok", "items": [1, 2]}


def test_extract_llm_content_supports_block_responses():
    response = AIMessage(content=[{"type": "text", "text": '{"name":"ok","count":1}'}])
    assert extract_llm_content(response) == '{"name":"ok","count":1}'


def test_lm_studio_client_preserves_reasoning_content_for_json_parser():
    client = LMStudioChatOpenAI(
        base_url="http://localhost:1234/v1",
        api_key="lm-studio",
        model="test-model",
    )
    result = client._create_chat_result({
        "id": "chatcmpl-test",
        "model": "test-model",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "reasoning_content": '{"name":"reasoning","count":3}',
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })

    message = result.generations[0].message
    assert message.additional_kwargs["reasoning_content"] == (
        '{"name":"reasoning","count":3}'
    )
    assert extract_llm_content(message) == '{"name":"reasoning","count":3}'


@pytest.mark.asyncio
async def test_structured_invoke_repairs_without_repeating_source_prompt():
    calls = []

    def respond(value):
        calls.append(value)
        if len(calls) == 1:
            return AIMessage(content='Here is the result: name=ready, count=2')
        return AIMessage(content='{"name":"ready","count":2}')

    llm = RunnableLambda(respond)
    prompt = PromptTemplate.from_template("Large source: {source}\n{format_instructions}")
    result = await invoke_llm_structured(
        llm, prompt, SmallOutput, {"source": "sensitive-large-input"},
        format_instructions="Return name and count as JSON",
    )
    assert result == {"name": "ready", "count": 2}
    assert len(calls) == 2
    assert "sensitive-large-input" not in str(calls[1])


@pytest.mark.asyncio
async def test_structured_invoke_validates_and_coerces_raw_json():
    llm = RunnableLambda(lambda _: AIMessage(content='Result: {"name":"ready","count":"2",}'))
    prompt = PromptTemplate.from_template("{format_instructions}")
    result = await invoke_llm_structured(
        llm,
        prompt,
        SmallOutput,
        {},
        format_instructions="Return JSON",
    )
    assert result == {"name": "ready", "count": 2}


@pytest.mark.asyncio
async def test_structured_invoke_obeys_shared_total_timeout(monkeypatch):
    async def slow(_):
        await asyncio.sleep(1)
        return AIMessage(content='{"name":"late","count":1}')

    llm = SlowRunnable(lambda _: None, afunc=slow)
    prompt = PromptTemplate.from_template("{format_instructions}")
    monkeypatch.setattr(settings, "LLM_REQUEST_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(settings, "LLM_STRUCTURED_TOTAL_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out before returning structured output"):
        await invoke_llm_structured(
            llm,
            prompt,
            SmallOutput,
            {},
            format_instructions="Return JSON",
        )
    assert time.monotonic() - started < 0.2
