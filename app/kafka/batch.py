"""Batch merge utilities — pure functions, no I/O."""
from __future__ import annotations

import uuid
from typing import Any

from .consumer import KafkaConsumerService

# (raw_kafka_dict, aiokafka_msg_object)
QueueItem = tuple[dict, Any]


def lc_to_tuples(chat_history: list[dict] | None) -> list[tuple[str, str]]:
    """Convert LangChain [{type, content}] → [(role, text)] tuples."""
    result: list[tuple[str, str]] = []
    for msg in (chat_history or []):
        t = msg.get("type", "")
        if t == "human":
            result.append(("user", msg.get("content", "")))
        elif t in ("ai", "assistant"):
            result.append(("bot", msg.get("content", "")))
    return result


def merge_batch(batch: list[QueueItem]) -> tuple[dict, str, list[Any]]:
    """Merge N queued items into (merged_data, message_id, kafka_msgs).

    Rules:
      question            → all joined with "\\n"
      chat_history        → from FIRST message (overridden by server-side history later)
      channel_id /
      agent_id / platform → from FIRST message (same session)
      conversation_status → from LAST message
      message_id          → from LAST message
    """
    raws       = [item[0] for item in batch]
    kafka_msgs = [item[1] for item in batch]
    first_data = raws[0].get("data") or {}

    if len(raws) == 1:
        return first_data, raws[0].get("message_id") or str(uuid.uuid4()), kafka_msgs

    questions = [(r.get("data") or {}).get("question") or "" for r in raws]
    merged_data = {
        **first_data,
        "question": "\n".join(q for q in questions if q),
        "conversation_status": (raws[-1].get("data") or {}).get("conversation_status", 0),
    }
    return merged_data, raws[-1].get("message_id") or str(uuid.uuid4()), kafka_msgs


def drain(queue) -> list[QueueItem]:
    """Non-blocking: take every item currently in the queue."""
    import asyncio
    items: list[QueueItem] = []
    while not queue.empty():
        try:
            items.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return items


async def commit_batch(consumer: KafkaConsumerService, kafka_msgs: list[Any]) -> None:
    """Commit the highest offset per (topic, partition) for the whole batch."""
    best: dict[tuple[str, int], int] = {}
    for msg in kafka_msgs:
        key = (msg.topic, msg.partition)
        best[key] = max(best.get(key, -1), msg.offset)
    for (topic, partition), offset in best.items():
        await consumer.commit(topic, partition, offset)
