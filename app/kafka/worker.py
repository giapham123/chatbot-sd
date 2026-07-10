"""Per-channel worker — Solution 3 core.

CHANNEL_IDLE_TIMEOUT  Worker exits after this many seconds of silence.
                      Default 30 s. While the worker is alive every new message
                      goes into the same queue (serialised, history preserved).

How simultaneous messages are merged
-------------------------------------
asyncio.create_task() schedules the worker but does NOT run it immediately.
The consume_loop keeps dispatching messages to the queue until it hits its own
await (no more messages in the Kafka buffer).  Only THEN does the event loop
start the worker.  By that point all simultaneously-produced messages are
already in the queue → one non-blocking drain collects all of them → 1 LLM
call → 1 reply.

How "bot is busy" messages are merged
---------------------------------------
While the LLM call is in progress (seconds), the consume_loop keeps dispatching
new messages into the same channel queue.  When the LLM finishes the worker
loops back, drains the queue again → merges all waiting messages → 1 more LLM
call → 1 more reply.

Flow per loop iteration
-----------------------
1. Wait for first message   blocking; exits on CHANNEL_IDLE_TIMEOUT
2. Drain queue immediately   non-blocking; collects every message already queued
3. Merge batch               N questions → 1 question
4. Inject server history     replaces stale client chat_history with server copy
5. ONE LLM call              → one reply
6. Store output history      next iteration has full context
7. Commit all offsets
8. Loop back to step 1
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..orchestration.conversation import ConversationService
from .batch import QueueItem, commit_batch, drain, merge_batch
from .consumer import KafkaConsumerService
from .handler import handle_chat_sd
from .producer import KafkaProducerService

if TYPE_CHECKING:
    from ..ws.ws_service import WebsocketClient

logger = logging.getLogger(__name__)

OUTPUT_URI = "/api/v1/bot/chat/reply"

# Worker stays alive this many seconds after the last message.
# Any message that arrives within this window goes into the same queue.
CHANNEL_IDLE_TIMEOUT: float = 30.0


# ---------------------------------------------------------------------------
# Per-channel state
# ---------------------------------------------------------------------------

@dataclass
class ChannelState:
    queue:  asyncio.Queue  = field(default_factory=asyncio.Queue)
    worker: asyncio.Task | None = None


_channels:        dict[str, ChannelState] = {}
_channel_history: dict[str, list[dict]]  = {}
worker_tasks:     set[asyncio.Task]       = set()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

async def channel_worker(
    channel_id: str,
    state: ChannelState,
    consumer: KafkaConsumerService,
    producer: KafkaProducerService,
    conversation: ConversationService,
    output_topic: str,
    ws_client: "WebsocketClient | None",
) -> None:
    try:
        while True:
            # 1 — wait for first message; exit if channel has been idle too long
            try:
                first: QueueItem = await asyncio.wait_for(
                    state.queue.get(), timeout=CHANNEL_IDLE_TIMEOUT
                )
            except asyncio.TimeoutError:
                break   # 30 s of silence → exit worker

            # 2 — drain ALL messages already in the queue right now (non-blocking)
            #
            #     Why this works for simultaneous messages:
            #     asyncio.create_task() only schedules this worker — it doesn't
            #     run it.  The consume_loop keeps dispatching messages until it
            #     has no more Kafka data (hits its own await).  By the time we
            #     reach this line, every "same-time" message is already queued.
            #
            #     Why this works for "bot is busy" messages:
            #     While the LLM call below is running, consume_loop dispatches
            #     new messages into this queue.  On the next loop iteration this
            #     drain picks them all up at once.
            batch: list[QueueItem] = [first, *drain(state.queue)]

            # 3 — merge N messages into 1
            data, message_id, kafka_msgs = merge_batch(batch)

            # 4 — inject server-side history (or clear it if session just ended)
            incoming_status = int(data.get("conversation_status") or 0)
            if incoming_status in (2, 3):
                _channel_history.pop(channel_id, None)
            elif channel_id in _channel_history:
                data = {**data, "chat_history": _channel_history[channel_id]}

            # 5 — ONE LLM call for the entire batch
            try:
                output = await handle_chat_sd(data, conversation, message_id, ws_client)
                await producer.send(output_topic, {"uri": OUTPUT_URI, "data": output})

                # 6 — store history so the next batch gets A's answer in context
                if output.get("chat_history"):
                    _channel_history[channel_id] = output["chat_history"]

            except Exception as exc:
                logger.error("Worker error channel=%s: %s", channel_id, exc, exc_info=True)
            finally:
                # 7 — commit all offsets in this batch
                await commit_batch(consumer, kafka_msgs)

            # 8 — loop back; worker stays alive for CHANNEL_IDLE_TIMEOUT

    except asyncio.CancelledError:
        pass
    finally:
        _channels.pop(channel_id, None)


# ---------------------------------------------------------------------------
# Dispatcher — called by consume_loop for every incoming chat_sd message
# ---------------------------------------------------------------------------

def dispatch(
    raw: dict,
    kafka_msg,
    consumer: KafkaConsumerService,
    producer: KafkaProducerService,
    conversation: ConversationService,
    output_topic: str,
    ws_client: "WebsocketClient | None",
) -> None:
    """Enqueue message; start a worker for this channel if none is running."""
    channel_id = (raw.get("data") or {}).get("channel_id") or str(uuid.uuid4())

    if channel_id not in _channels:
        _channels[channel_id] = ChannelState()

    state = _channels[channel_id]
    state.queue.put_nowait((raw, kafka_msg))

    if state.worker is None or state.worker.done():
        task = asyncio.create_task(
            channel_worker(
                channel_id, state,
                consumer, producer, conversation,
                output_topic, ws_client,
            ),
            name=f"worker-{channel_id}",
        )
        state.worker = task
        worker_tasks.add(task)
        task.add_done_callback(worker_tasks.discard)
