"""ConversationService — orchestrates one SD turn with LangGraph token streaming.

Single call to stream() feeds all three transports without re-running the LLM:
  • HTTP SSE  — each StreamEvent yielded is formatted as an SSE frame in main.py
  • WebSocket — handler.py sends WS "processing" per token, "done" at end
  • Kafka     — worker.py publishes the final output dict to the output topic

error_email is extracted automatically from the error dict so all callers
(HTTP, Kafka handler) pass the same single `error` argument and get consistent
LangGraph tool-calling behaviour.
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
        error: dict | None = None,
        image_b64: list[tuple[str, str]] | None = None,
        lf_session_id: str | None = None,
        lf_trace_name: str = "chat_sd",
        lf_metadata: dict | None = None,
        lf_tags: list[str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # If the previous session ended (status=4) and the user sends a new message,
        # start a completely fresh session: clear history, error state, and reset status.
        if conversation_status == 4:
            history = []
            error = {}
            conversation_status = 1

        history = (history or [])[-self._history_turns:]

        # Extract error_email from the round-tripped error dict.
        # Centralising here means HTTP, WS, and Kafka callers all behave the same.
        error_email = 0
        if isinstance(error, dict):
            try:
                error_email = int(error.get("error_email", 0))
            except (TypeError, ValueError):
                pass

        answer_parts: list[str] = []
        result: dict = {}

        async for item in self._rag.answer_stream(
            user_input, history, conversation_status, error_email,
            image_b64=image_b64,
            lf_session_id=lf_session_id,
            lf_trace_name=lf_trace_name,
            lf_metadata=lf_metadata,
            lf_tags=lf_tags,
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
