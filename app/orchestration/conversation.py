"""ConversationService — orchestrates one SD turn with real LLM token streaming.

Calls the RAG service directly (bypassing LangGraph) for the streaming path.
The graph is kept for non-streaming callers (e.g. the /chat SSE endpoint).

Chat history is kept client-side and sent per request as LangChain format
([{"type": "human"|"ai", "content": "..."}]); the Kafka layer converts it to
(role, text) tuples before calling stream().
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import AsyncIterator

from ..services.rag import DefaultRagService


@dataclass
class StreamEvent:
    event: str   # "token" | "message_end" | "output"
    data: str = ""


class ConversationService:
    def __init__(self, rag: DefaultRagService, history_turns: int = 6) -> None:
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
        error_email: int = 0,
        error: dict | None = None,
    ) -> AsyncIterator[StreamEvent]:
        history = (history or [])[-self._history_turns:]

        answer_parts: list[str] = []
        result: dict = {}

        async for item in self._rag.answer_stream(
            user_input, history, conversation_status, error_email
        ):
            if isinstance(item, str):
                answer_parts.append(item)
                yield StreamEvent("token", item)
            else:
                result = item

        answer = "".join(answer_parts)
        yield StreamEvent("message_end")

        out_conversation_status = result.get("conversation_status", 1)
        out_identify = result.get("identify", 2)
        out_error_email = result.get("error_email", 0)

        out_chat_history = [
            *({"type": "human" if r == "user" else "ai", "content": t} for r, t in history),
            {"type": "human", "content": user_input},
            {"type": "ai", "content": answer},
        ]

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
