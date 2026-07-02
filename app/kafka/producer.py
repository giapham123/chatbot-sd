"""Kafka producer wrapper."""
from __future__ import annotations

import json
import logging

from aiokafka import AIOKafkaProducer

logger = logging.getLogger(__name__)


class KafkaProducerService:
    def __init__(self, host: str) -> None:
        self._producer = AIOKafkaProducer(bootstrap_servers=host)
        self._started = False

    async def start(self) -> None:
        if not self._started:
            await self._producer.start()
            self._started = True

    async def stop(self) -> None:
        if self._started:
            await self._producer.stop()
            self._started = False

    async def send(self, topic: str, message: dict) -> None:
        try:
            payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
            await self._producer.send(topic, payload)
        except Exception as exc:
            logger.error("Kafka send failed: %s", exc)
