"""Abstractions (ports). Concrete adapters live elsewhere and are injected.

This is the backbone of Dependency Inversion + Interface Segregation:
high-level policy (services, graph) depends only on these small Protocols,
never on OpenAI / CSV / FastAPI directly.
"""
from __future__ import annotations

from typing import AsyncIterator, Optional, Protocol

from .models import ActionDef, KBDoc


# --------------------------------------------------------------------------- #
# Repositories — one focused interface per sheet (ISP)
# --------------------------------------------------------------------------- #
class ResponseRepository(Protocol):
    def text(self, response_key: str) -> str: ...


class ActionRepository(Protocol):
    def get(self, action_id: str) -> Optional[ActionDef]: ...


class KnowledgeRepository(Protocol):
    def all(self) -> list[KBDoc]: ...


# --------------------------------------------------------------------------- #
# Service ports
# --------------------------------------------------------------------------- #
class EmbeddingClient(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class LLMClient(Protocol):
    async def complete(self, messages: list[dict]) -> str:
        """One-shot completion — used for query rewrite / rerank."""
        ...
    async def complete_json(self, messages: list[dict]) -> str:
        """Like complete() but forces JSON output via response_format."""
        ...
    async def stream_json(self, messages: list[dict]) -> AsyncIterator[str]:
        """Stream tokens from a JSON-format completion (stream=True + json_object)."""
        ...


class VectorStore(Protocol):
    async def search(self, vector: list[float], top_k: int) -> list[tuple[str, float]]:
        """Return up to `top_k` (doc_id, score) hits, most similar first."""
        ...


class ActionExecutor(Protocol):
    """Performs a side effect (e.g. forward an unanswered request to the admin)."""
    def execute(self, action: ActionDef, session_id: str, slots: dict[str, str]) -> None: ...