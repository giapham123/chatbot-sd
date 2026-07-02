"""Kafka consumer wrapper."""
from __future__ import annotations

import logging

from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.structs import OffsetAndMetadata

logger = logging.getLogger(__name__)


class KafkaConsumerService:
    def __init__(self, host: str, group_id: str, topics: list[str]) -> None:
        self._consumer = AIOKafkaConsumer(
            *topics,
            bootstrap_servers=host,
            group_id=group_id,
            auto_offset_reset="latest",
            enable_auto_commit=False,
        )
        self._started = False

    async def start(self) -> None:
        if not self._started:
            await self._consumer.start()
            self._started = True

    async def stop(self) -> None:
        if self._started:
            await self._consumer.stop()
            self._started = False

    def __aiter__(self):
        return self._consumer.__aiter__()

    async def commit(self, topic: str, partition: int, offset: int) -> None:
        try:
            tp = TopicPartition(topic, partition)
            await self._consumer.commit({tp: OffsetAndMetadata(offset + 1, "")})
        except Exception as exc:
            logger.error("Kafka commit failed: %s", exc)
