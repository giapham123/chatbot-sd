"""RAG service: retrieve from KB then stream response tokens from the LLM.

Pipeline per turn:
  1. contextualize  → rewrite follow-up into standalone query
  2. embed + search → pull top candidates from Qdrant (>= min_score)
  3. rerank         → cheap LLM reorders by true relevance
  4. answer_stream  → main LLM streams JSON, yields response tokens then result dict
"""
from __future__ import annotations

import json
import logging
import re
from typing import AsyncIterator

from openai import APIConnectionError, APITimeoutError
from ..domain.interfaces import EmbeddingClient, KnowledgeRepository, LLMClient, VectorStore
from ..prompt.en_system_prompt_sd import AGENT_SYSTEM_PROMPT_SD, RERANK_PROMPT, REWRITE_PROMPT

logger = logging.getLogger(__name__)


def _extract_until_quote(text: str, escape_next: bool) -> tuple[str, bool, bool]:
    """Extract chars from a streaming JSON string value until the closing unescaped quote.

    Returns: (extracted_content, still_inside_value, new_escape_next_state)
    """
    out: list[str] = []
    in_value = True
    for c in text:
        if escape_next:
            escape_next = False
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(c, c))
        elif c == "\\":
            escape_next = True
        elif c == '"':
            in_value = False
            break
        else:
            out.append(c)
    return "".join(out), in_value, escape_next


class DefaultRagService:
    def __init__(
        self,
        embedder: EmbeddingClient,
        vector_store: VectorStore,
        llm: LLMClient,
        router_llm: LLMClient,
        knowledge: KnowledgeRepository,
        top_k: int,
        min_score: float,
        fallback_message: str,
        rerank_candidates: int = 8,
    ) -> None:
        self._embedder = embedder
        self._store = vector_store
        self._llm = llm
        self._router = router_llm
        self._docs = {d.doc_id: d for d in knowledge.all()}
        self._top_k = top_k
        self._min_score = min_score
        self._fallback_message = fallback_message
        self._rerank_candidates = max(rerank_candidates, top_k)

    async def answer_stream(
        self,
        query: str,
        history: list[tuple[str, str]],
        conversation_status: int = 0,
        error_email: int = 0,
        *,
        lf_session_id: str | None = None,
        lf_trace_name: str = "chat_sd",
        lf_metadata: dict | None = None,
        lf_tags: list[str] | None = None,
    ) -> AsyncIterator[str | dict]:
        """Yield response text tokens (str) as the LLM streams, then the result dict.

        Streams via stream=True + response_format=json_object, extracts only the
        'response' field value as clean text tokens, and yields the full parsed
        result dict as the final item.

        lf_* kwargs are forwarded to every OpenAI call so that langfuse.openai
        wrapper links them to the correct session/trace in Langfuse.
        """
        # Build Langfuse per-call kwargs forwarded to every OpenAI create() call.
        # Only kwargs explicitly listed in OpenAiArgsExtractor.__init__ are stripped
        # before reaching the real OpenAI API — session_id/tags are NOT stripped in
        # Langfuse 4.x (bug), so we embed them inside metadata instead.
        lf: dict = {}
        if lf_session_id:
            lf["metadata"] = {
                **(lf_metadata or {}),
                "session_id": lf_session_id,
                "tags": lf_tags or ["chat", "chatbot-sd", "SD"],
            }

        messages = await self._build_messages(
            query, history, conversation_status, error_email, lf=lf
        )

        raw_parts: list[str] = []
        in_response = False
        escape_next = False
        search_buf = ""

        async for chunk in self._llm.stream_json(messages, name=lf_trace_name, **lf):
            raw_parts.append(chunk)

            if not in_response:
                search_buf += chunk
                m = re.search(r'"response"\s*:\s*"', search_buf)
                if m:
                    in_response = True
                    after = search_buf[m.end():]
                    search_buf = ""
                    content, in_response, escape_next = _extract_until_quote(after, escape_next)
                    if content:
                        yield content
            else:
                content, in_response, escape_next = _extract_until_quote(chunk, escape_next)
                if content:
                    yield content

        raw = "".join(raw_parts)
        logger.debug("LLM stream accumulated: %r", raw)
        yield self._parse_structured(raw)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _build_messages(
        self,
        query: str,
        history: list[tuple[str, str]],
        conversation_status: int,
        error_email: int,
        lf: dict | None = None,
    ) -> list[dict]:
        """Run the full RAG pipeline and return the assembled LLM message list."""
        standalone = await self._contextualize(query, history)

        context_texts: list[str] = []
        vectors = await self._embedder.embed([standalone])
        if vectors:
            hits = await self._store.search(vectors[0], self._rerank_candidates)
            candidates = [
                self._docs[doc_id]
                for doc_id, score in hits
                if score >= self._min_score and doc_id in self._docs
            ]
            if candidates:
                ranked = await self._rerank(standalone, candidates)
                context_texts = [d.as_text() for d in ranked[: self._top_k]]

        last_bot_msg = ""
        for role, text in reversed(history):
            if role == "bot":
                last_bot_msg = text
                break

        system = AGENT_SYSTEM_PROMPT_SD.format(
            conversation_status=conversation_status,
            error_email=error_email,
        )

        messages: list[dict] = [{"role": "system", "content": system}]
        for role, text in history:
            messages.append({"role": "assistant" if role == "bot" else "user", "content": text})

        joined = "\n\n".join(context_texts) if context_texts else "(không có ngữ cảnh)"

        last_bot_hint = (
            f"\n\n⚠️ TIN NHẮN BOT GẦN NHẤT (KHÔNG được lặp lại): \"{last_bot_msg}\""
            if last_bot_msg else ""
        )

        history_summary = ""
        if history:
            lines = []
            for role, text in history:
                label = "Khách" if role == "user" else "Bot"
                lines.append(f"{label}: {text}")
            history_summary = "\n\nTÓM TẮT LỊCH SỬ HỘI THOẠI:\n" + "\n".join(lines)

        messages.append({
            "role": "user",
            "content": (
                f"NGỮ CẢNH KB:\n{joined}"
                f"{history_summary}"
                f"{last_bot_hint}"
                f"\n\nCÂU HỎI HIỆN TẠI: {query}"
            ),
        })

        return messages

    def _parse_structured(self, raw: str) -> dict:
        """Extract {response, conversation_status, identify, error_email} from LLM output."""
        try:
            text = re.sub(r"```(?:json)?", "", raw).strip().strip("`")
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                data = json.loads(m.group())
                return {
                    "response": str(data.get("response", self._fallback_message)).strip(),
                    "conversation_status": int(data.get("conversation_status", 1)),
                    "identify": int(data.get("identify", 2)),
                    "error_email": int(data.get("error_email", 0)),
                }
        except Exception as exc:
            logger.warning("Failed to parse LLM JSON (%s): %r", exc, raw)
        return {
            "response": self._fallback_message,
            "conversation_status": 2,
            "identify": 2,
            "error_email": 0,
        }

    async def _contextualize(self, query: str, history: list[tuple[str, str]]) -> str:
        if not history:
            return query
        try:
            messages: list[dict] = [{"role": "system", "content": REWRITE_PROMPT}]
            for role, text in history:
                messages.append(
                    {"role": "assistant" if role == "bot" else "user", "content": text}
                )
            messages.append({"role": "user", "content": f"Câu hỏi mới nhất: {query}"})
            rewritten = (await self._router.complete(messages)).strip()
            if rewritten:
                logger.info("Query rewrite: %r -> %r", query, rewritten)
                return rewritten
        except (APIConnectionError, APITimeoutError) as exc:
            logger.error("Query rewrite failed (%s); using original query", exc, exc_info=True)
        except Exception as exc:
            logger.warning("Query rewrite failed (%s); using original query", exc)
        return query

    async def _rerank(self, query: str, candidates: list) -> list:
        if len(candidates) <= 1:
            return candidates
        try:
            listing = "\n".join(
                f"[{i}] {d.question} :: {d.answer}" for i, d in enumerate(candidates)
            )
            messages = [
                {"role": "system", "content": RERANK_PROMPT},
                {"role": "user", "content": f"CÂU HỎI: {query}\n\nDANH SÁCH:\n{listing}"},
            ]
            raw = await self._router.complete(messages)
            order = [int(x) for x in re.findall(r"\d+", raw)]
            seen: set[int] = set()
            ranked = [
                candidates[i]
                for i in order
                if 0 <= i < len(candidates) and not (i in seen or seen.add(i))
            ]
            ranked += [d for i, d in enumerate(candidates) if i not in seen]
            if ranked:
                logger.info("Rerank order: %s", [d.doc_id for d in ranked])
                return ranked
        except (APIConnectionError, APITimeoutError) as exc:
            logger.error("Rerank failed (%s); using vector order", exc, exc_info=True)
        except Exception as exc:
            logger.warning("Rerank failed (%s); using vector order", exc)
        return candidates
