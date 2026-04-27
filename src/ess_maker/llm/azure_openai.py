"""Azure OpenAI chat client wrapper used by analyzer/judge/suggesters."""
from __future__ import annotations

import json
from typing import Any

from openai import AsyncAzureOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from ess_maker.settings import settings


def _client() -> AsyncAzureOpenAI:
    return AsyncAzureOpenAI(
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
        azure_endpoint=settings.azure_openai_endpoint,
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def chat_json(
    system: str,
    user: str,
    *,
    deployment: str | None = None,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Call AOAI and parse a JSON object response."""
    client = _client()
    resp = await client.chat.completions.create(
        model=deployment or settings.azure_openai_deployment,
        temperature=temperature,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    text = resp.choices[0].message.content or "{}"
    return json.loads(text)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def chat_text(
    system: str,
    user: str,
    *,
    deployment: str | None = None,
    temperature: float = 0.2,
) -> str:
    client = _client()
    resp = await client.chat.completions.create(
        model=deployment or settings.azure_openai_deployment,
        temperature=temperature,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return resp.choices[0].message.content or ""
