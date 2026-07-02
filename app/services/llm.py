"""OpenAI adapters for LLM chat and embeddings.

These are the only files that know about the OpenAI SDK. Everything else
depends on interfaces.LLMClient / interfaces.EmbeddingClient.
"""
from __future__ import annotations

from typing import AsyncIterator

from openai import AsyncOpenAI


class OpenAILLMClient:
    def __init__(self, client: AsyncOpenAI, chat_model: str) -> None:
        self._client = client
        self._chat_model = chat_model

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        stream = await self._client.chat.completions.create(
            model=self._chat_model,
            temperature=0.2,
            stream=True,
            messages=messages,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    async def complete(self, messages: list[dict]) -> str:
        resp = await self._client.chat.completions.create(
            model=self._chat_model,
            temperature=0,
            messages=messages,
        )
        return resp.choices[0].message.content or ""


class OpenAIEmbeddingClient:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = await self._client.embeddings.create(model=self._model, input=texts)
        return [item.embedding for item in resp.data]