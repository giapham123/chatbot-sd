"""Kafka message router — consumes from ai-agent-chat, routes chat_sd messages,
streams tokens to WebSocket, publishes OutputChatSD to bot-agent-service.

Incoming message format (mirrors ai-agent IncomingKafkaChatMessage):
    { "uri": "api/v1/chat_sd", "message_id": "<uuid>", "data": { <ChatSD fields> } }

Outgoing Kafka message (mirrors ai-agent OutputKafkaChatMessage):
    { "uri": "/api/v1/bot/chat/reply", "data": { <OutputChatSD fields> } }

WebSocket status flow (mirrors ai-agent):
    start  ->  processing (per token)  ->  done
    channel: channel.agent.{channel_id}
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING

from ..orchestration.conversation import ConversationService
from .consumer import KafkaConsumerService
from .producer import KafkaProducerService

if TYPE_CHECKING:
    from ..ws.ws_service import WebsocketClient

logger = logging.getLogger(__name__)

CHAT_URI_SD = "api/v1/chat_sd"
OUTPUT_URI = "/api/v1/bot/chat/reply"


def _lc_to_tuples(chat_history: list[dict] | None) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for msg in (chat_history or []):
        t = msg.get("type", "")
        if t == "human":
            result.append(("user", msg.get("content", "")))
        elif t in ("ai", "assistant"):
            result.append(("bot", msg.get("content", "")))
    return result


async def _handle_chat_sd(
    data: dict,
    conversation: ConversationService,
    message_id: str,
    ws_client: "WebsocketClient | None" = None,
) -> dict:
    channel_id = data.get("channel_id") or str(uuid.uuid4())
    agent_id = data.get("agent_id") or ""
    question = data.get("question") or ""
    history = _lc_to_tuples(data.get("chat_history"))

    if ws_client:
        await ws_client.send(agent_id, channel_id, message_id, "start", "")

    output: dict | None = None
    try:
        async for event in conversation.stream(
            channel_id,
            question,
            history,
            agent_id=agent_id,
            platform=data.get("platform") or "WEB",
            conversation_status=data.get("conversation_status", 0),
            error=data.get("error"),
        ):
            if event.event == "token" and ws_client:
                await ws_client.send(agent_id, channel_id, message_id, "processing", event.data)
            elif event.event == "output":
                output = json.loads(event.data)
    except Exception as exc:
        logger.error("Error streaming chat_sd channel=%s: %s", channel_id, exc, exc_info=True)
        if ws_client:
            await ws_client.send(agent_id, channel_id, message_id, "error", str(exc))

    if output is None:
        output = {
            "channel_id": channel_id,
            "agent_id": agent_id,
            "status": "error",
            "answer": "",
            "similarity": [],
            "tool_messages": None,
            "recursion_count": 0,
            "last_tool_name": "",
            "chat_history": None,
            "platform": data.get("platform") or "WEB",
            "conversation_status": 0,
            "identify": 2,
            "error": data.get("error"),
            "extra": None,
            "image_detection": None,
        }

    if ws_client:
        await ws_client.send(
            agent_id,
            channel_id,
            message_id,
            "done",
            output.get("answer", ""),
            similar=output.get("similarity", []),
            extra=output.get("extra"),
        )

    return output


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
                message_id = raw.get("message_id") or str(uuid.uuid4())
                data = raw.get("data", {})

                if uri == CHAT_URI_SD:
                    output_data = await _handle_chat_sd(
                        data, conversation, message_id, ws_client=ws_client
                    )
                    await producer.send(output_topic, {"uri": OUTPUT_URI, "data": output_data})
                else:
                    logger.error("Unknown Kafka URI '%s', message_id=%s", uri, message_id)

                await consumer.commit(msg.topic, msg.partition, msg.offset)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Error processing Kafka message partition=%s offset=%s: %s",
                    msg.partition, msg.offset, exc, exc_info=True,
                )
    except asyncio.CancelledError:
        pass
    finally:
        await consumer.stop()
        await producer.stop()
