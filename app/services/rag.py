"""RAG service: retrieve from the KB and decide answerability
(implements interfaces.RagService).

Pipeline per query:
  1. contextualize  -> rewrite a follow-up into a standalone query using history
  2. embed + search -> pull the top candidates from Qdrant (>= min_score)
  3. rerank         -> a cheap LLM reorders the candidates by true relevance
  4. decide         -> strong candidates -> RAG_ANSWER (grounded, streamed reply)
                       none               -> HANDOFF (collect MSNV/email for admin)
"""
from __future__ import annotations

import logging
import re
from typing import AsyncIterator, Optional

from ..domain.interfaces import EmbeddingClient, KnowledgeRepository, LLMClient, VectorStore
from ..domain.models import Emission, EmissionKind, KBDoc

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Bạn là Trợ lý ảo Service Desk của Công ty Tài chính Mirae Asset (MAFC). "
    "Hãy trả lời như một nhân viên hỗ trợ thật: thân thiện, tự nhiên, ngắn gọn, "
    "bằng tiếng Việt, xưng 'em' và gọi khách là 'Anh/Chị'. "
    "Ưu tiên dùng thông tin trong NGỮ CẢNH và lịch sử trò chuyện để trả lời đúng trọng tâm. "
    "Nếu là lời chào/cảm ơn/trò chuyện xã giao thì đáp lại lịch sự, ngắn gọn. "
    "TUYỆT ĐỐI không bịa thông tin không có trong ngữ cảnh; nếu không chắc, hãy nói rằng "
    "em sẽ hỗ trợ chuyển tiếp cho bộ phận phù hợp."
)

REWRITE_PROMPT = (
    "Bạn viết lại câu hỏi mới nhất của người dùng thành MỘT câu hỏi độc lập, đầy đủ ngữ cảnh "
    "dựa trên lịch sử trò chuyện (thay các từ như 'nó', 'cái đó', 'vẫn vậy' bằng nội dung cụ thể). "
    "Giữ nguyên ngôn ngữ tiếng Việt. CHỈ trả về đúng câu hỏi, không giải thích."
)

RERANK_PROMPT = (
    "Bạn xếp hạng các mục kiến thức theo mức độ phù hợp để trả lời CÂU HỎI. "
    "Chỉ trả về các số thứ tự (index) của những mục phù hợp, xếp giảm dần theo độ liên quan, "
    "cách nhau bằng dấu phẩy. Bỏ qua các mục không liên quan. Ví dụ: 2,0,1"
)


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

    async def plan_async(
        self, query: str, history: Optional[list[tuple[str, str]]] = None
    ) -> Emission:
        # 1) Rewrite follow-ups into a standalone query for better retrieval.
        standalone = await self._contextualize(query, history or [])

        # 2) Embed + retrieve a wider candidate set (for reranking).
        vectors = await self._embedder.embed([standalone])
        if not vectors:
            return self._fallback()
        hits = await self._store.search(vectors[0], self._rerank_candidates)
        candidates = [
            self._docs[doc_id]
            for doc_id, score in hits
            if score >= self._min_score and doc_id in self._docs
        ]
        if not candidates:
            return self._fallback()

        # 3) Rerank, then keep the top_k most relevant.
        ranked = await self._rerank(standalone, candidates)
        context = ranked[: self._top_k]
        return Emission(kind=EmissionKind.RAG_ANSWER, query=query, context=context)

    async def _contextualize(self, query: str, history: list[tuple[str, str]]) -> str:
        """Rewrite a follow-up into a standalone query (no-op on first turn)."""
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
        except Exception as exc:  # never let rewrite break the flow
            logger.warning("Query rewrite failed (%s); using original query", exc)
        return query

    async def _rerank(self, query: str, candidates: list[KBDoc]) -> list[KBDoc]:
        """LLM reorders candidates by relevance; falls back to vector order."""
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
            # Keep any candidates the LLM omitted (preserve their vector order).
            ranked += [d for i, d in enumerate(candidates) if i not in seen]
            if ranked:
                logger.info("Rerank order: %s", [d.doc_id for d in ranked])
                return ranked
        except Exception as exc:  # never let rerank break the flow
            logger.warning("Rerank failed (%s); using vector order", exc)
        return candidates

    def _fallback(self) -> Emission:
        # Can't answer -> forward to a human (nhân viên hỗ trợ / SD).
        return Emission(kind=EmissionKind.HANDOFF, text=self._fallback_message)

    async def stream_answer(
        self, query: str, context: list[str], history: list[tuple[str, str]]
    ) -> AsyncIterator[str]:
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        # Prior turns give the model multi-turn context (follow-up questions).
        for role, text in history:
            if role == "user":
                messages.append({"role": "user", "content": text})
            elif role == "bot":
                messages.append({"role": "assistant", "content": text})
        joined = "\n\n".join(context) if context else "(không có ngữ cảnh — trả lời xã giao/tự nhiên)"
        messages.append({"role": "user", "content": f"NGỮ CẢNH:\n{joined}\n\nCÂU HỎI: {query}"})
        async for token in self._llm.stream(messages):
            yield token