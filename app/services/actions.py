"""Terminal action executors (implements interfaces.ActionExecutor).

Default impl just logs the ticket. Swap for a real SD/ITSM integration later
without touching the rest of the app (Open/Closed + Dependency Inversion).
"""
from __future__ import annotations

import logging

from ..domain.models import ActionDef

logger = logging.getLogger("chatbot_sd.actions")


class LoggingActionExecutor:
    def execute(self, action: ActionDef, session_id: str, slots: dict[str, str]) -> None:
        payload = {k: slots.get(k, "") for k in action.payload}
        logger.info(
            "ACTION %s (type=%s) session=%s payload=%s",
            action.action_id, action.type, session_id, payload,
        )