"""Staff active-status verification via MAFC ITSD API.

Called before the LLM generates its response so the correct text is streamed
from the start — avoids the race condition where "Cảm ơn Anh/Chị..." is
already sent to the UI before we know whether the staff record exists.
"""
from __future__ import annotations

import logging
import uuid

import httpx

logger = logging.getLogger(__name__)


class StaffCheckService:
    def __init__(self) -> None:
        self._url = ""
        self._auth = ""          # base64-encoded "user:pass" (no "Basic " prefix)
        self._timeout = 5.0
        self.enabled = False

    def init(self, url: str, auth: str, timeout: float = 5.0) -> None:
        self._url = url.strip()
        self._auth = auth.strip()
        self._timeout = timeout
        self.enabled = bool(self._url and self._auth)

    async def verify(self, employee_id: str, email_id: str) -> bool | None:
        """Check whether a staff member is active in the MAFC HR system.

        Returns:
            True   — staff is active (accept the info)
            False  — staff not found or inactive (reject the info)
            None   — API unreachable / unexpected error (fail open: accept)
        """
        if not self.enabled:
            return None
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    self._url,
                    headers={
                        "X-Request-Id":  str(uuid.uuid4()),
                        "Content-Type":  "application/json",
                        "Accept":        "application/json",
                        "User-Agent":    "MAFC-Chatbot-SD/1.0",
                        "Authorization": f"Basic {self._auth}",
                    },
                    json={"Employee_ID": employee_id, "Email_ID": email_id},
                )
            logger.info(
                "Staff check %s/%s → HTTP %d body=%r",
                employee_id, email_id, resp.status_code, resp.text[:200],
            )

            # HTTP 404 / 400 → not found
            if resp.status_code in (400, 404):
                return False

            if resp.status_code != 200:
                logger.warning("Staff check unexpected status %d body=%r", resp.status_code, resp.text[:200])
                return None   # fail open

            try:
                data = resp.json()
            except Exception:
                logger.warning("Staff check HTTP 200 but non-JSON body=%r — failing open", resp.text[:200])
                return None

            return self._parse_active(data)

        except httpx.TimeoutException:
            logger.warning("Staff check timed out for %s — failing open", employee_id)
            return None
        except Exception as exc:
            logger.error("Staff check error: %s", exc, exc_info=True)
            return None

    @staticmethod
    def _parse_active(data: object) -> bool | None:
        """Parse MAFC staff-check response: verifyResult==true AND status=="Active"."""
        if not isinstance(data, dict):
            return None

        verify_result = data.get("verifyResult")
        status        = str(data.get("status", "")).strip().lower()

        if verify_result is True and status == "active":
            return True
        if verify_result is False or (verify_result is True and status != "active"):
            return False

        logger.warning("Staff check: cannot parse active status from %r", data)
        return None


staff_check_service = StaffCheckService()
