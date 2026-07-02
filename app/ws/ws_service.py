"""WebSocket client — publishes agent responses to a SocketCluster server.

Mirrors ai-agent AsyncWebsocketClient exactly:
  channel : channel.agent.{channel_id}
  status  : start | processing | done | error
  format  : WebsocketResponse (messageType, messageId, uri, data)
"""
from __future__ import annotations

import logging
import uuid

from pydantic import BaseModel, Field

from .async_ws_lib import AsyncSocketCluster

logger = logging.getLogger(__name__)

CHANNEL_NAME = "channel.agent.{channel_id}"


# ---------------------------------------------------------------------------
# Message schemas (mirrors ai-agent websocket_schemas.py)
# ---------------------------------------------------------------------------

class WebsocketResponseData(BaseModel):
    agent_id: str
    channel_id: str
    status: str = ""
    content: str = ""
    additional_content: str | None = None
    similar: list[str] = Field(default_factory=list)
    extra: list[str] | None = None


class WebsocketResponse(BaseModel):
    messageType: str = "RESPONSE"
    messageId: str
    uri: str = "ai-agent-query-done"
    data: WebsocketResponseData


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class WebsocketClient:
    """Singleton SocketCluster client (mirrors ai-agent AsyncWebsocketClient)."""

    _instance: "WebsocketClient | None" = None
    _initialized: bool = False

    def __new__(cls, url: str = ""):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, url: str = "") -> None:
        if not self._initialized:
            self.socket = AsyncSocketCluster(url)
            self.socket.setdelay(2)
            self.socket.setreconnection(True)
            self.socket.setBasicListener(
                self._on_connect, self._on_disconnect, self._on_error
            )
            WebsocketClient._initialized = True

    @staticmethod
    async def _on_connect(socket):
        logger.debug("WebSocket connected")

    @staticmethod
    async def _on_disconnect(socket):
        logger.debug("WebSocket disconnected")

    @staticmethod
    async def _on_error(socket, error):
        logger.error("WebSocket connection error: %s", error)

    @classmethod
    async def get_instance(cls, url: str = "") -> "WebsocketClient":
        instance = cls(url)
        if not instance.socket._is_connected():
            await instance.connect()
        return instance

    async def connect(self) -> None:
        await self.socket.connect()

    async def disconnect(self) -> None:
        await self.socket.disconnect()

    async def send(
        self,
        agent_id: str,
        channel_id: str,
        message_id: str,
        status: str,
        content: str,
        similar: list[str] | None = None,
        extra: list[str] | None = None,
    ) -> None:
        try:
            channel = CHANNEL_NAME.format(channel_id=channel_id)
            payload = WebsocketResponse(
                messageId=message_id or str(uuid.uuid4()),
                data=WebsocketResponseData(
                    agent_id=agent_id,
                    channel_id=channel_id,
                    status=status,
                    content=content,
                    similar=similar or [],
                    extra=extra,
                ),
            ).model_dump()

            def _ack(channel_name, error, data):
                if error:
                    logger.error("WS publish failed for channel %s: %s", channel_name, error)

            await self.socket.publishack(channel, payload, _ack)
        except Exception as exc:
            logger.error("WS send error [%s]: %s", message_id, exc)
