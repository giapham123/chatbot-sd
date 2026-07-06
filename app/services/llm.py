"""OpenAI adapters for LLM chat and embeddings.

These are the only files that know about the OpenAI SDK. Everything else
depends on interfaces.LLMClient / interfaces.EmbeddingClient.

Langfuse tracing (when enabled) works via two mechanisms:
  1. langfuse.openai wrapper — patches the client at startup, auto-traces every call.
  2. Per-call kwargs (session_id, name, metadata, tags) — forwarded transparently
     to the wrapper so calls are linked to the right session/trace in Langfuse.
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from openai import AsyncOpenAI


class OpenAILLMClient:
    def __init__(self, client: AsyncOpenAI, chat_model: str) -> None:
        self._client = client
        self._chat_model = chat_model

    async def complete(self, messages: list[dict], **lf: Any) -> str:
        resp = await self._client.chat.completions.create(
            model=self._chat_model,
            temperature=0,
            messages=messages,
            **lf,
        )
        return resp.choices[0].message.content or ""

    async def complete_json(self, messages: list[dict], **lf: Any) -> str:
        resp = await self._client.chat.completions.create(
            model=self._chat_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=messages,
            **lf,
        )
        return resp.choices[0].message.content or ""

    async def stream_json(self, messages: list[dict], **lf: Any) -> AsyncIterator[str]:
        """Stream chunks from a JSON-format LLM call (stream=True + json_object)."""
        stream = await self._client.chat.completions.create(
            model=self._chat_model,
            temperature=0,
            response_format={"type": "json_object"},
            stream=True,
            messages=messages,
            **lf,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


class OpenAIEmbeddingClient:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def embed(self, texts: list[str], **lf: Any) -> list[list[float]]:
        if not texts:
            return []
        resp = await self._client.embeddings.create(model=self._model, input=texts, **lf)
        return [item.embedding for item in resp.data]