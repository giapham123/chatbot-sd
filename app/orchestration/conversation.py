"""ConversationService — the streaming orchestrator.

Runs the LangGraph decision graph for one turn, then renders the resulting
emissions to a stream of transport-agnostic events:
  * scripted text  -> a single `message` event
  * RAG answer     -> `token` events streamed from the LLM, then `message_end`
  * handoff / end  -> a control event

After all emissions, an `output` event is yielded carrying the full
OutputChatSD-compatible JSON so callers get the structured response.

Chat history is kept client-side and sent with each request in LangChain format
([{"type": "human"|"ai", "content": "..."}]); the web layer converts it to
(role, text) tuples before calling stream().
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

from ..domain.interfaces import RagService
from ..domain.models import EmissionKind


@dataclass
class StreamEvent:
    event: str            # "message" | "token" | "message_end" | "handoff" | "end" | "output"
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
        channel_id: str,
        user_input: str,
        history: list[tuple[str, str]] | None = None,
        *,
        agent_id: str | None = None,
        platform: str = "WEB",
        conversation_status: int = 0,
        error: dict | None = None,
    ) -> AsyncIterator[StreamEvent]:
        history = (history or [])[-self._history_turns:]

        config = {"configurable": {"thread_id": channel_id}}
        state = await self._graph.ainvoke(
            {
                "user_input": user_input,
                "channel_id": channel_id,
                "agent_id": agent_id or "",
                "platform": platform,
                "history": history,
            },
            config,
        )

        accumulated: list[str] = []
        out_conversation_status = 1
        out_identify = 2
        out_similarity: list[str] = []

        for em in state.get("emissions", []):
            kind = em.get("kind")
            em_identify = em.get("identify", 2)

            if kind == EmissionKind.TEXT.value:
                text = em.get("text", "")
                accumulated.append(text)
                yield StreamEvent("message", text)

            elif kind == EmissionKind.RAG_ANSWER.value:
                out_similarity = em.get("doc_ids", [])
                rag_tokens: list[str] = []
                async for token in self._rag.stream_answer(
                    em.get("query", ""), em.get("context", []), history
                ):
                    rag_tokens.append(token)
                    yield StreamEvent("token", token)
                accumulated.append("".join(rag_tokens))
                yield StreamEvent("message_end")

            elif kind == EmissionKind.HANDOFF.value:
                out_conversation_status = 2
                out_identify = em_identify
                if em.get("text"):
                    accumulated.append(em["text"])
                    yield StreamEvent("message", em["text"])
                yield StreamEvent("handoff")

            elif kind == EmissionKind.END.value:
                out_conversation_status = 3
                out_identify = em_identify
                text = em.get("text", "")
                if text:
                    accumulated.append(text)
                yield StreamEvent("end", text)

        answer = "".join(accumulated)
        out_chat_history = [
            *({"type": "human" if r == "user" else "ai", "content": t} for r, t in history),
            {"type": "human", "content": user_input},
            {"type": "ai", "content": answer},
        ]

        output: dict = {
            "channel_id": channel_id,
            "agent_id": agent_id,
            "status": "done",
            "answer": answer,
            "similarity": out_similarity,
            "tool_messages": None,
            "recursion_count": 0,
            "last_tool_name": "",
            "chat_history": out_chat_history,
            "platform": platform,
            "conversation_status": out_conversation_status,
            "identify": out_identify,
            "error": error,
            "extra": None,
            "image_detection": None,
        }
        yield StreamEvent("output", json.dumps(output, ensure_ascii=False))
