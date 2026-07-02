"""ConversationService — the streaming orchestrator.

Runs the LangGraph decision graph for one turn, then renders the resulting
emissions to a stream of transport-agnostic events:
  * scripted text  -> a single `message` event
  * RAG answer     -> `token` events streamed from the LLM, then `message_end`
  * handoff / end  -> a control event

Chat history is kept client-side (localStorage) and sent with each request; the
web layer (main.py) turns events into SSE frames and the client persists them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator

from ..domain.interfaces import RagService
from ..domain.models import EmissionKind


@dataclass
class StreamEvent:
    event: str            # "message" | "token" | "message_end" | "handoff" | "end"
    data: str = ""


class ConversationService:
    def __init__(
        self,
        graph: Any,
        rag: RagService,
        history_turns: int = 6,
    ) -> None:
        self._graph = graph
        self._rag = rag
        self._history_turns = history_turns

    async def stream(
        self,
        session_id: str,
        user_input: str,
        history: list[tuple[str, str]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # Prior turns come from the client (localStorage); trim to the last N for context.
        history = (history or [])[-self._history_turns:]

        config = {"configurable": {"thread_id": session_id}}
        state = await self._graph.ainvoke(
            {"user_input": user_input, "history": history}, config
        )

        for em in state.get("emissions", []):
            kind = em.get("kind")

            if kind == EmissionKind.TEXT.value:
                yield StreamEvent("message", em.get("text", ""))

            elif kind == EmissionKind.RAG_ANSWER.value:
                async for token in self._rag.stream_answer(
                    em.get("query", ""), em.get("context", []), history
                ):
                    yield StreamEvent("token", token)
                yield StreamEvent("message_end")

            elif kind == EmissionKind.HANDOFF.value:
                if em.get("text"):
                    yield StreamEvent("message", em["text"])
                yield StreamEvent("handoff")

            elif kind == EmissionKind.END.value:
                yield StreamEvent("end", em.get("text", ""))