"""LangGraph orchestration — SD rules-based conversation.

Single node per turn:
  respond ──► END

Each turn calls rag.answer_async() which retrieves KB context and calls the
main LLM with AGENT_SYSTEM_PROMPT_SD rules. The LLM returns structured JSON:
  {response, conversation_status, identify, error_email}

These values are emitted as a single TEXT emission so ConversationService can
extract them and include in the Kafka response back to bot-agent.
"""
from __future__ import annotations

import logging
from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from ..domain.models import EmissionKind

logger = logging.getLogger(__name__)


class ConversationState(TypedDict, total=False):
    user_input: str
    channel_id: str
    agent_id: str
    platform: str
    history: list
    conversation_status: int
    error_email: int
    emissions: list[dict]


def build_conversation_graph(rag, checkpointer=None):
    """Build a single-node graph that runs SD rules via rag.answer_async()."""

    async def respond(state: ConversationState) -> dict:
        logger.info("graph state IN: %s", {k: v for k, v in state.items() if k != "history"})

        query = state.get("user_input", "")
        history = state.get("history", [])
        conversation_status = state.get("conversation_status", 0)
        error_email = state.get("error_email", 0)

        result = await rag.answer_async(query, history, conversation_status, error_email)
        logger.info(
            "answer_async result: conv_status=%s identify=%s error_email=%s response=%r",
            result.get("conversation_status"),
            result.get("identify"),
            result.get("error_email"),
            result.get("response", "")[:80],
        )

        emission = {
            "kind": EmissionKind.TEXT.value,
            "text": result.get("response", ""),
            "conversation_status": result.get("conversation_status", 1),
            "identify": result.get("identify", 2),
            "error_email": result.get("error_email", 0),
        }
        return {"emissions": [emission]}

    graph = StateGraph(ConversationState)
    graph.add_node("respond", respond)
    graph.set_entry_point("respond")
    graph.add_edge("respond", END)
    return graph.compile(checkpointer=checkpointer or MemorySaver())
