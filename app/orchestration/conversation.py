"""ConversationService — orchestrates one SD turn via LangGraph.

Runs the graph for one turn, extracts structured fields from the TEXT emission
({response, conversation_status, identify, error_email}), and yields StreamEvents
so the Kafka handler can forward them to bot-agent.

Chat history is kept client-side and sent per request as LangChain format
([{"type": "human"|"ai", "content": "..."}]); the Kafka layer converts it to
(role, text) tuples before calling stream().
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

from ..domain.models import EmissionKind


@dataclass
class StreamEvent:
    event: str   # "token" | "message_end" | "output"
    data: str = ""


class ConversationService:
    def __init__(self, graph: Any, history_turns: int = 6) -> None:
        self._graph = graph
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
        error_email: int = 0,
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
                "conversation_status": conversation_status,
                "error_email": error_email,
            },
            config,
        )

        answer = ""
        out_conversation_status = 1
        out_identify = 2
        out_error_email = 0

        for em in state.get("emissions", []):
            if em.get("kind") == EmissionKind.TEXT.value:
                answer = em.get("text", "")
                out_conversation_status = em.get("conversation_status", 1)
                out_identify = em.get("identify", 2)
                out_error_email = em.get("error_email", 0)
                # Yield the full answer as one token so WebSocket gets the text.
                yield StreamEvent("token", answer)
                yield StreamEvent("message_end")

        out_chat_history = [
            *({"type": "human" if r == "user" else "ai", "content": t} for r, t in history),
            {"type": "human", "content": user_input},
            {"type": "ai", "content": answer},
        ]

        # Merge updated error_email back into the error dict for round-trip state.
        out_error = dict(error or {})
        out_error["error_email"] = out_error_email

        output: dict = {
            "channel_id": channel_id,
            "agent_id": agent_id,
            "status": "done",
            "answer": answer,
            "similarity": [],
            "tool_messages": None,
            "recursion_count": 0,
            "last_tool_name": "",
            "chat_history": out_chat_history,
            "platform": platform,
            "conversation_status": out_conversation_status,
            "identify": out_identify,
            "error": out_error,
            "extra": None,
            "image_detection": None,
        }
        yield StreamEvent("output", json.dumps(output, ensure_ascii=False))
