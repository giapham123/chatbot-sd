"""Per-channel worker: process one channel's messages, one turn at a time.

Each channel_id gets its own queue and its own worker task. A worker loops:

    1. wait for the first message            (exit if the channel goes idle)
    2. gather a rapid burst into one batch    (debounce + drain)
    3. resolve the session/history for the turn
    4. produce ONE reply for the batch        (supersede if newer messages arrive)
    5. publish the reply + remember history
    6. commit Kafka offsets
    → back to 1

Three ways messages get merged into a single reply/turn:

  • Simultaneous   asyncio.create_task() only schedules the worker; consume_loop
                   keeps enqueuing until it runs out of Kafka data, so every
                   same-instant message is already queued when the worker drains.
  • Rapid burst    After the first message the worker waits a short debounce
                   window, gathering follow-ups typed a moment later (step 2).
  • In-flight      If a new message arrives while the reply is still being
                   produced, the worker cancels that reply and re-calls with the
                   merged batch ("supersede", step 4) — one reply, not two.
"""
from __future__ import annotations

import asyncio
import logging
import os
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

# A worker exits after this many seconds of silence on its channel; the next
# message simply starts a fresh worker (see dispatch).
CHANNEL_IDLE_TIMEOUT: float = 30.0

# Debounce window (ms): after the first message of a turn, wait this long for more
# messages so a rapid burst is merged into ONE turn. The window resets on each new
# message (idle-based) and is capped by CHANNEL_DEBOUNCE_MAX_S so a user who keeps
# typing can never delay the reply forever. Set CHANNEL_DEBOUNCE_MS=0 to disable.
CHANNEL_DEBOUNCE_MS: float = float(os.getenv("CHANNEL_DEBOUNCE_MS", "1200"))
CHANNEL_DEBOUNCE_MAX_S: float = float(os.getenv("CHANNEL_DEBOUNCE_MAX_S", "5"))


# ---------------------------------------------------------------------------
# Per-channel state
# ---------------------------------------------------------------------------

@dataclass
class ChannelState:
    queue:  asyncio.Queue       = field(default_factory=asyncio.Queue)
    worker: asyncio.Task | None = None


# One queue+worker per active channel; server-side history and "ended" flags per
# channel. router.py imports _channels / _channel_history / dispatch / worker_tasks.
_channels:        dict[str, ChannelState] = {}
_channel_history: dict[str, list[dict]]   = {}
_channel_ended:   set[str]                = set()  # channels whose last turn ended
worker_tasks:     set[asyncio.Task]       = set()


# ---------------------------------------------------------------------------
# Per-turn steps (small, single-purpose helpers)
# ---------------------------------------------------------------------------

async def _next_message_or_idle(queue: asyncio.Queue) -> QueueItem | None:
    """Block for the next message; return None if the channel is idle too long."""
    try:
        return await asyncio.wait_for(queue.get(), timeout=CHANNEL_IDLE_TIMEOUT)
    except asyncio.TimeoutError:
        return None


async def _gather_burst(queue: asyncio.Queue, first: QueueItem) -> list[QueueItem]:
    """Collect a rapid burst of messages into one batch.

    Starting from `first`, keep pulling messages while they keep arriving within
    CHANNEL_DEBOUNCE_MS of each other (the window resets on each one), capped at
    CHANNEL_DEBOUNCE_MAX_S total. Then drain anything else already queued. Result:
    N rapid messages → one batch → one LLM call.
    """
    gathered: list[QueueItem] = []
    if CHANNEL_DEBOUNCE_MS > 0:
        loop = asyncio.get_running_loop()
        window = CHANNEL_DEBOUNCE_MS / 1000.0
        deadline = loop.time() + CHANNEL_DEBOUNCE_MAX_S
        while (remaining := min(window, deadline - loop.time())) > 0:
            try:
                gathered.append(await asyncio.wait_for(queue.get(), timeout=remaining))
            except asyncio.TimeoutError:
                break   # a quiet gap → the burst is complete
    return [first, *gathered, *drain(queue)]


def _resolve_session(channel_id: str, batch: list[QueueItem]) -> dict:
    """Decide the history/reset for this turn — call ONCE per turn.

    Returns the fields to overlay onto the merged request (reused on every
    supersede re-call so a reset is never accidentally undone):

      • Session ended — client sent conv 2/3, or the previous turn ended it —
        → start fresh: empty history, conv=1, no error.
      • Otherwise      → inject the server-side history saved last turn (if any).
      • Nothing to change → return {} and use the request as-is.

    Has side effects on the _channel_history / _channel_ended caches, hence "once".
    """
    data, _, _ = merge_batch(batch)
    status = int(data.get("conversation_status") or 0)

    if status in (2, 3) or channel_id in _channel_ended:
        _channel_history.pop(channel_id, None)
        _channel_ended.discard(channel_id)
        return {"chat_history": [], "conversation_status": 1, "error": {}}

    if channel_id in _channel_history:
        return {"chat_history": _channel_history[channel_id]}

    return {}


async def _run_turn(
    channel_id: str,
    state: ChannelState,
    batch: list[QueueItem],
    session: dict,
    conversation: ConversationService,
    ws_client: "WebsocketClient | None",
) -> tuple[dict | None, list]:
    """Produce ONE reply for `batch`, superseding if a newer message arrives.

    Runs the reply while also watching the queue. If a new message for this
    channel arrives BEFORE the reply is produced, the in-flight reply is
    cancelled and the turn is re-called with the merged batch — so the user gets
    a single merged reply instead of two.

    Returns (output, kafka_msgs). output is None if the reply failed; kafka_msgs
    covers every message in the (possibly merged) batch so the caller can commit.
    """
    kafka_msgs: list = []
    while True:
        data, message_id, kafka_msgs = merge_batch(batch)
        data = {**data, **session}

        answer_task = asyncio.create_task(
            handle_chat_sd(data, conversation, message_id, ws_client)
        )
        newer_task = asyncio.create_task(state.queue.get())
        await asyncio.wait({answer_task, newer_task}, return_when=asyncio.FIRST_COMPLETED)

        if answer_task.done():
            # Reply is ready. If a message was picked up at the same instant,
            # put it back for the next turn (don't lose it); otherwise stop
            # waiting on the queue.
            if newer_task.done() and not newer_task.cancelled():
                state.queue.put_nowait(newer_task.result())
            else:
                newer_task.cancel()
                await asyncio.gather(newer_task, return_exceptions=True)
            try:
                return answer_task.result(), kafka_msgs
            except Exception as exc:
                logger.error("Reply failed channel=%s: %s", channel_id, exc, exc_info=True)
                return None, kafka_msgs

        # Reply not ready yet, but a newer message arrived → supersede: cancel the
        # in-flight reply, fold the new message in, and loop to re-call.
        answer_task.cancel()
        await asyncio.gather(answer_task, return_exceptions=True)
        batch = [*batch, newer_task.result(), *drain(state.queue)]
        logger.info(
            "Supersede channel=%s: newer message before reply; re-calling merged (batch=%d)",
            channel_id, len(batch),
        )


def _remember_history(channel_id: str, output: dict) -> None:
    """Save this turn's history for the next turn — unless the turn ended the
    session (conv 2/3), in which case drop it and mark the channel ended so the
    next message starts fresh regardless of what status the client sends."""
    status = int(output.get("conversation_status") or 0)
    if status in (2, 3):
        _channel_history.pop(channel_id, None)
        _channel_ended.add(channel_id)
    elif output.get("chat_history"):
        _channel_history[channel_id] = output["chat_history"]
        _channel_ended.discard(channel_id)


# ---------------------------------------------------------------------------
# Worker — orchestrates the per-turn steps above
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
            first = await _next_message_or_idle(state.queue)
            if first is None:
                break   # idle too long → exit; dispatch() starts a new worker later

            batch = await _gather_burst(state.queue, first)
            session = _resolve_session(channel_id, batch)

            output, kafka_msgs = await _run_turn(
                channel_id, state, batch, session, conversation, ws_client
            )
            try:
                if output is not None:
                    await producer.send(output_topic, {"uri": OUTPUT_URI, "data": output})
                    _remember_history(channel_id, output)
            finally:
                await commit_batch(consumer, kafka_msgs)

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
    """Enqueue the message for its channel, starting a worker if none is running."""
    channel_id = (raw.get("data") or {}).get("channel_id") or str(uuid.uuid4())

    state = _channels.setdefault(channel_id, ChannelState())
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
