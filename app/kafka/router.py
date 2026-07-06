"""Kafka consume loop — entry point called from main.py lifespan.

Reads messages from the input topic and dispatches them to per-channel workers.
All queue / merge / history logic lives in worker.py.
All LLM streaming logic lives in handler.py.
All batch utilities live in batch.py.

Incoming : { "uri": "api/v1/chat_sd", "message_id": "<uuid>", "data": { <ChatSD> } }
Outgoing  : { "uri": "/api/v1/bot/chat/reply", "data": { <OutputChatSD> } }
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from ..orchestration.conversation import ConversationService
from .consumer import KafkaConsumerService
from .producer import KafkaProducerService
from .worker import _channel_history, _channels, dispatch, worker_tasks

if TYPE_CHECKING:
    from ..ws.ws_service import WebsocketClient

logger = logging.getLogger(__name__)

CHAT_URI_SD = "api/v1/chat_sd"


async def consume_loop(
    consumer: KafkaConsumerService,
    producer: KafkaProducerService,
    conversation: ConversationService,
    input_topic: str,
    output_topic: str,
    ws_client: "WebsocketClient | None" = None,
) -> None:
    await consumer.start()
    await producer.start()
    try:
        async for msg in consumer:
            try:
                raw = json.loads(msg.value.decode("utf-8"))
                uri = raw.get("uri", "")

                if uri == CHAT_URI_SD:
                    dispatch(raw, msg, consumer, producer, conversation, output_topic, ws_client)
                else:
                    logger.error("Unknown Kafka URI '%s'", uri)
                    await consumer.commit(msg.topic, msg.partition, msg.offset)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Failed to dispatch message: %s", exc, exc_info=True)

    except asyncio.CancelledError:
        # Graceful shutdown: cancel all running workers
        for task in list(worker_tasks):
            task.cancel()
        if worker_tasks:
            await asyncio.gather(*worker_tasks, return_exceptions=True)
    finally:
        _channels.clear()
        _channel_history.clear()
        await consumer.stop()
        await producer.stop()
