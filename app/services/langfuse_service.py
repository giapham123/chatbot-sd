"""Langfuse observability service.

Mirrors the pattern in ai-agent/src/services/langfuse/langfuse_service.py:
  - Explicit Langfuse(secret_key, public_key, host, timeout) init
  - flush() after each request
  - Per-request trace context via langfuse.decorators.observe in the Kafka handler
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class LangfuseService:
    def __init__(self) -> None:
        self._client = None
        self._enabled = False

    def init(
        self,
        secret_key: str,
        public_key: str,
        host: str,
        timeout: int,   # milliseconds — same unit as ai-agent
    ) -> None:
        """Initialize with explicit params. Called once at startup from build_container()."""
        if not secret_key or not public_key:
            logger.warning("Langfuse keys missing — tracing disabled")
            return
        try:
            from langfuse import Langfuse
            self._client = Langfuse(
                secret_key=secret_key,
                public_key=public_key,
                host=host,
                timeout=timeout,
            )
            self._enabled = True
            logger.info("Langfuse initialized → %s", host)
        except Exception as exc:
            logger.warning("Langfuse init failed (%s) — tracing disabled", exc)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def flush(self) -> None:
        """Flush pending traces to Langfuse. Call after each request completes."""
        if self._client:
            try:
                self._client.flush()
                logger.info("Langfuse: flush ok")
            except Exception as exc:
                logger.error("Langfuse flush failed: %s", exc, exc_info=True)


# Module-level singleton — imported by container and handler
langfuse_service = LangfuseService()
