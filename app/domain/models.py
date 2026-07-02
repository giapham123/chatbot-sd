"""Domain models (data sheets + conversation events).

Plain, immutable-ish dataclasses. No behavior, no I/O — keeps the domain
decoupled from persistence and framework concerns (SRP).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# --------------------------------------------------------------------------- #
# Data sheets (mirror the CSV files in /data)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Response:
    response_key: str
    text: str
    variables: str = ""


@dataclass(frozen=True)
class ActionDef:
    action_id: str
    type: str  # "handoff" | "end"
    message_key: str
    payload: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class KBDoc:
    doc_id: str
    topic_id: str
    question: str
    answer: str
    source: str
    tags: str

    def as_text(self) -> str:
        return f"Q: {self.question}\nA: {self.answer}"


# --------------------------------------------------------------------------- #
# Conversation output — "emissions" the engine/services produce each turn.
# The transport layer (SSE) decides how to render them.
# --------------------------------------------------------------------------- #
class EmissionKind(str, Enum):
    TEXT = "text"            # fixed scripted message -> send as one chunk
    RAG_ANSWER = "rag"       # needs token streaming from the LLM
    HANDOFF = "handoff"      # control: transferred to human SD
    END = "end"             # control: conversation ended


@dataclass
class Emission:
    kind: EmissionKind
    text: str = ""
    # for RAG_ANSWER:
    query: str = ""
    context: list[KBDoc] = field(default_factory=list)