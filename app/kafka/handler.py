"""Single LLM call handler — called once per merged batch."""
from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING

from ..orchestration.conversation import ConversationService
from ..services.langfuse_service import langfuse_service
from ..services.minio_service import minio_service
from .batch import lc_to_tuples

if TYPE_CHECKING:
    from ..ws.ws_service import WebsocketClient

logger = logging.getLogger(__name__)


def _extract_error_email(data: dict) -> int:
    """Read error_email from the error dict (round-tripped from previous response)."""
    error = data.get("error")
    if isinstance(error, dict):
        try:
            return int(error.get("error_email", 0))
        except (TypeError, ValueError):
            pass
    return 0


async def handle_chat_sd(
    data: dict,
    conversation: ConversationService,
    message_id: str,
    ws_client: "WebsocketClient | None" = None,
) -> dict:
    """Stream one LLM response for `data`, send WS events, return OutputChatSD dict."""
    channel_id          = data.get("channel_id") or str(uuid.uuid4())
    agent_id            = data.get("agent_id") or ""
    question            = data.get("question") or ""
    platform            = data.get("platform") or "WEB"
    history             = lc_to_tuples(data.get("chat_history"))
    conversation_status = int(data.get("conversation_status") or 0)
    error_email         = _extract_error_email(data)
    error               = data.get("error") if isinstance(data.get("error"), dict) else {}
    image_url       = data.get("image_url") or []
    image_detection = minio_service.get_image_urls(image_url, platform)
    image_b64: list[tuple[str, str]] = []
    for _key in image_url:
        _result = await minio_service.aget_image_b64(_key)
        if _result:
            image_b64.append(_result)

    if ws_client:
        await ws_client.send(agent_id, channel_id, message_id, "start", "")

    lf_metadata = {
        "message_id": message_id,
        "channel_id": channel_id,
        "question": question,
        "agent_id": agent_id,
        "platform": platform,
        "conversation_status": conversation_status,
    }

    output: dict | None = None
    try:
        async for event in conversation.stream(
            channel_id, question, history,
            agent_id=agent_id,
            platform=platform,
            conversation_status=conversation_status,
            error_email=error_email,
            error=error,
            image_b64=image_b64 or None,
            lf_session_id=channel_id,
            lf_trace_name=f"CHAT_SD_{platform.upper()}" if platform else "CHAT_SD",
            lf_metadata=lf_metadata,
            lf_tags=["chat", "chatbot-sd", "SD"],
        ):
            if event.event == "token" and ws_client:
                await ws_client.send(agent_id, channel_id, message_id, "processing", event.data)
            elif event.event == "output":
                output = json.loads(event.data)
    except Exception as exc:
        logger.error("chat_sd stream error channel=%s: %s", channel_id, exc, exc_info=True)
        if ws_client:
            await ws_client.send(agent_id, channel_id, message_id, "error", str(exc))
    finally:
        # Flush traces after each request — same pattern as ai-agent
        if langfuse_service.enabled:
            langfuse_service.flush()

    if output is None:
        output = {
            "channel_id": channel_id, "agent_id": agent_id,
            "status": "error", "answer": "", "similarity": [],
            "tool_messages": None, "recursion_count": 0, "last_tool_name": "",
            "chat_history": None, "platform": platform,
            "conversation_status": 0, "identify": 2,
            "error": {"error_email": error_email}, "extra": None,
            "image_detection": image_detection or None,
        }
    else:
        output["image_detection"] = image_detection or None

    if ws_client:
        await ws_client.send(
            agent_id, channel_id, message_id, "done",
            output.get("answer", ""),
            similar=output.get("similarity", []),
            extra=output.get("extra"),
        )

    return output
