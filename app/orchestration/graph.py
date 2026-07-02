"""LangGraph orchestration — conversational RAG (no scripted flows).

The bot answers like a person, grounded in the knowledge base + chat history:

    respond ──► END

Per turn (single node):
  * first message      -> welcome greeting
  * waiting for MSNV   -> forward MSNV/email + the unanswered question to admin
  * small talk         -> let the LLM reply naturally (no KB needed)
  * otherwise          -> RAG: if the KB covers it, stream a grounded answer;
                          if not, ask for MSNV/email to forward to the administrator

State is checkpointed per session (thread_id = channel_id) so `greeted` and the
"awaiting MSNV" state survive between requests.
"""
from __future__ import annotations

import logging
import re
from typing import Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from ..domain.interfaces import ActionExecutor, RagService
from ..domain.models import ActionDef, Emission, EmissionKind

logger = logging.getLogger(__name__)

# A message is "small talk" if EVERY word is social (greeting / thanks / filler).
_SOCIAL = {
    "hi", "hello", "hey", "chào", "chao", "xin", "alo", "dạ", "da", "vâng", "vang",
    "cảm", "cám", "ơn", "on", "thanks", "thank", "you", "tks",
    "ok", "oke", "okay", "okie", "bye", "tạm", "biệt", "biet",
    "em", "ạ", "a", "anh", "chị", "chi", "nhé", "nhe", "nha", "nhá",
    "nhiều", "nhieu", "bạn", "ban", "good", "morning", "afternoon", "ad",
}


def _serialize(emission: Emission) -> dict:
    data: dict = {"kind": emission.kind.value, "text": emission.text, "identify": emission.identify}
    if emission.kind == EmissionKind.RAG_ANSWER:
        data["query"] = emission.query
        data["context"] = [doc.as_text() for doc in emission.context]
        data["doc_ids"] = [doc.doc_id for doc in emission.context]
    return data


def _is_smalltalk(message: str) -> bool:
    tokens = re.findall(r"\w+", message.lower())
    return bool(tokens) and all(t in _SOCIAL for t in tokens)


class ConversationState(TypedDict, total=False):
    user_input: str
    channel_id: str
    agent_id: str
    platform: str
    history: list              # prior turns (from client) used for query rewrite
    greeted: bool
    awaiting_admin: bool       # we asked for MSNV/email to forward to admin
    unanswered: str            # the question the bot couldn't answer
    emissions: list[dict]


def build_conversation_graph(
    rag: RagService,
    action_executor: ActionExecutor,
    welcome_text: str,
    ask_msnv_text: str,
    forwarded_text: str,
    notify_action: Optional[ActionDef],
    checkpointer=None,
):
    async def respond(state: ConversationState) -> dict:
        # State coming IN to the node this turn (as checkpointed for the session).
        logger.info("graph state IN: %s", dict(state))
        delta = await _decide(state)
        # State delta the node writes back (merged into the session's checkpoint).
        logger.info("graph state OUT (delta): %s", delta)
        return delta

    async def _decide(state: ConversationState) -> dict:
        message = state.get("user_input", "")

        # 1) First contact -> greet.
        if not state.get("greeted"):
            return {"greeted": True,
                    "emissions": [{"kind": EmissionKind.TEXT.value, "text": welcome_text}]}

        # 2) We asked for MSNV/email -> forward it + the unanswered question to admin.
        if state.get("awaiting_admin"):
            if notify_action is not None:
                action_executor.execute(
                    notify_action, state.get("channel_id", ""),
                    {"msnv_email": message.strip(), "unanswered": state.get("unanswered", "")},
                )
            return {
                "awaiting_admin": False, "unanswered": "",
                "emissions": [
                    {"kind": EmissionKind.TEXT.value, "text": forwarded_text, "identify": 1},
                    {"kind": EmissionKind.HANDOFF.value, "text": "", "identify": 1},
                ],
            }

        # 3) Small talk -> answer naturally (no KB grounding needed).
        if _is_smalltalk(message):
            plan = Emission(kind=EmissionKind.RAG_ANSWER, query=message, context=[])
            return {"emissions": [_serialize(plan)]}

        # 4) RAG: answer from the KB, or ask for MSNV/email to forward to admin.
        plan = await rag.plan_async(message, state.get("history", []))
        if plan.kind == EmissionKind.RAG_ANSWER:
            return {"emissions": [_serialize(plan)]}
        return {"awaiting_admin": True, "unanswered": message,
                "emissions": [{"kind": EmissionKind.TEXT.value, "text": ask_msnv_text}]}


    graph = StateGraph(ConversationState)
    graph.add_node("respond", respond)
    graph.set_entry_point("respond")
    graph.add_edge("respond", END)
    return graph.compile(checkpointer=checkpointer or MemorySaver())