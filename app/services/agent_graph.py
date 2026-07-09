"""LangGraph ReAct agent with prompt-based entry router and explicit three-node graph.

Graph topology
──────────────
  decide_tool ──[check_staff]──> check_staff ──> agent ──> END
              ──[qdrant]──────> qdrant      ──> agent ──> END
              ──[agent]───────> agent                ──> END

Phase 1 — graph.ainvoke():
  decide_tool (entry) uses a cheap LLM call + ROUTER_PROMPT to classify the question
  and route directly to the right first node, skipping unnecessary agent turns.
  check_staff / qdrant nodes execute their work and loop back to agent for final routing.
  agent exits to END once it has no more tool calls.

Phase 2 — stream_json():
  The agent's draft AIMessage is dropped; stream_json is called on the accumulated
  messages with response_format=json_object so the LLM always returns parseable JSON.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import uuid
from typing import Annotated, AsyncIterator, Callable, Coroutine

from langchain_core.messages import (
    AIMessage, AnyMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
)
from langchain_core.tools import tool as lc_tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from openai import AsyncOpenAI
from typing_extensions import TypedDict

from .llm import OpenAILLMClient
from .staff_check_service import staff_check_service
from ..prompt.en_system_prompt_sd import END_CHAT_PROMPT, ROUTER_PROMPT, THINK_PROMPT

logger = logging.getLogger(__name__)

# Per-request token queue — set by stream_answer, read by _call_agent.
# Using ContextVar so concurrent requests each have their own queue.
_token_queue_var: contextvars.ContextVar[
    asyncio.Queue | None
] = contextvars.ContextVar("_agent_token_queue", default=None)

_MAX_AGENT_TURNS = 6

_EMAIL_RE = re.compile(r'\b[\w.+-]+@mafc\.com\.vn\b', re.IGNORECASE)
_MSNV_RE  = re.compile(r'\b[A-Za-z]{2,}[0-9]{2,}[A-Za-z0-9]*\b')


def _extract_staff_ids(text: str) -> tuple[str, str]:
    """Return (employee_id, email_id) from user message text."""
    email_match = _EMAIL_RE.search(text)
    if email_match:
        email = email_match.group(0)
        return email.split("@")[0], email
    msnv_match = _MSNV_RE.search(text)
    if msnv_match:
        return msnv_match.group(0), ""
    return "", ""


# ---------------------------------------------------------------------------
# Tools  (each maps to a graph node via _TOOL_TO_NODE)
# ---------------------------------------------------------------------------

@lc_tool
async def check_staff_active(employee_id: str, email_id: str) -> str:
    """Xác minh nhân viên MAFC có tồn tại và đang hoạt động không.

    Gọi khi người dùng cung cấp MSNV (ví dụ MAFCOS4430) hoặc
    email công ty (ví dụ MAFCOS4430@mafc.com.vn) để đăng ký hỗ trợ.

    Args:
        employee_id: Mã số nhân viên (MSNV), ví dụ "MAFCOS4430"
        email_id: Email công ty, ví dụ "MAFCOS4430@mafc.com.vn"
    """
    return ""   # execution handled by _call_check_staff node


@lc_tool
async def search_knowledge_base(query: str) -> str:
    """Tìm kiếm thêm thông tin trong KB của MAFC khi ngữ cảnh hiện tại không đủ.

    Gọi khi câu hỏi cần thêm chi tiết kỹ thuật hoặc hướng dẫn cụ thể từ KB.

    Args:
        query: Câu truy vấn cụ thể cần tìm kiếm thêm trong KB.
    """
    return ""   # result injected by the qdrant node


TOOLS = [check_staff_active, search_knowledge_base]
_TOOL_TO_NODE = {
    check_staff_active.name:    "check_staff",
    search_knowledge_base.name: "qdrant",
}


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    messages:            Annotated[list[AnyMessage], add_messages]
    next_node:           str   # written by decide_tool, read by _route_from_decide
    conversation_status: int  # 1=ongoing, 4=end — written by _call_agent


# ---------------------------------------------------------------------------
# Message converters
# ---------------------------------------------------------------------------

def _to_lc_messages(openai_messages: list[dict]) -> list[BaseMessage]:
    """OpenAI-format dicts → LangChain BaseMessage list."""
    out: list[BaseMessage] = []
    for m in openai_messages:
        role = m.get("role", "")
        content = m.get("content") or ""
        if role == "system":
            out.append(SystemMessage(content=content if isinstance(content, str) else ""))
        elif role == "user":
            out.append(HumanMessage(content=content))
        elif role == "assistant":
            out.append(AIMessage(content=str(content)))
    return out


def _to_openai_messages(lc_messages: list[BaseMessage]) -> list[dict]:
    """LangChain BaseMessage list → OpenAI-format dicts (preserves tool call history)."""
    out: list[dict] = []
    for m in lc_messages:
        if isinstance(m, SystemMessage):
            out.append({"role": "system", "content": m.content})
        elif isinstance(m, HumanMessage):
            out.append({"role": "user", "content": m.content})
        elif isinstance(m, AIMessage):
            msg: dict = {"role": "assistant", "content": m.content or ""}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["args"]),
                        },
                    }
                    for tc in m.tool_calls
                ]
            out.append(msg)
        elif isinstance(m, ToolMessage):
            out.append({
                "role": "tool",
                "content": str(m.content),
                "tool_call_id": m.tool_call_id,
            })
    return out


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

QdrantSearchFn = Callable[[str], Coroutine[None, None, str]]


class AgentGraph:
    def __init__(self) -> None:
        self._llm_client: OpenAILLMClient | None = None
        self._llm_with_tools = None
        self._graph = None
        self._qdrant_search: QdrantSearchFn | None = None

    @property
    def enabled(self) -> bool:
        return self._llm_client is not None

    def set_qdrant_search(self, fn: QdrantSearchFn) -> None:
        self._qdrant_search = fn

    def build(
        self,
        chat_model: str,
        api_key: str,
        qdrant_search_fn: QdrantSearchFn | None = None,
    ) -> None:
        self._llm_client = OpenAILLMClient(AsyncOpenAI(api_key=api_key), chat_model)
        self._qdrant_search = qdrant_search_fn

        llm = ChatOpenAI(
            model=chat_model,
            temperature=0,
            openai_api_key=api_key,
            model_kwargs={"parallel_tool_calls": False},
        )
        self._llm_with_tools = llm.bind_tools(TOOLS)

        graph = StateGraph(AgentState)
        graph.add_node("decide_tool", self._decide_tool)
        graph.add_node("agent",       self._call_agent)
        graph.add_node("check_staff", self._call_check_staff)
        graph.add_node("qdrant",      self._call_qdrant)

        graph.set_entry_point("decide_tool")

        graph.add_conditional_edges(
            "decide_tool",
            self._route_from_decide,
            {
                "check_staff": "check_staff",
                "qdrant":      "qdrant",
                "agent":       "agent",
            },
        )

        graph.add_conditional_edges(
            "agent",
            self._route,
            {
                "check_staff": "check_staff",
                "qdrant":      "qdrant",
                END:           END,
            },
        )

        graph.add_edge("check_staff", "agent")
        graph.add_edge("qdrant",      "agent")

        self._graph = graph.compile()
        logger.info(
            "AgentGraph built — model=%s tools=[%s]",
            chat_model, ", ".join(t.name for t in TOOLS),
        )

    # ------------------------------------------------------------------
    # Entry node — decide_tool
    # ------------------------------------------------------------------

    async def _decide_tool(self, state: AgentState) -> dict:
        """Classify the current question only — history is intentionally excluded.

        When routing to check_staff:
          - Extracts MSNV / email from the current message
          - Emits a synthetic AIMessage(tool_calls=[check_staff_active(employee_id, email_id)])
            so _call_check_staff always sees a proper tool_calls entry.

        When routing to qdrant or agent:
          - No messages added; the node just sets next_node.
        """
        # _decide_tool is the graph entry point — the last message is always the current question
        last = state["messages"][-1] if state["messages"] else None
        if not isinstance(last, HumanMessage):
            return {"next_node": "agent"}

        # Extract raw text (handles multimodal list content)
        raw_content = last.content
        if isinstance(raw_content, list):
            raw_text = " ".join(
                p.get("text", "")
                for p in raw_content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        else:
            raw_text = str(raw_content)

        # rag.py prefixes KB context + history before the actual question.
        # Strip everything before the marker so the router sees only the bare question.
        _QUESTION_MARKER = "CÂU HỎI HIỆN TẠI:"
        question = raw_text.split(_QUESTION_MARKER, 1)[-1].strip()

        routing_messages = [
            {"role": "system", "content": ROUTER_PROMPT},
            {"role": "user",   "content": str(question)},
        ]

        raw = (await self._llm_client.complete(routing_messages)).strip()

        # Parse structured JSON response from ROUTER_PROMPT
        employee_id = ""
        email_id    = ""
        try:
            parsed      = json.loads(raw)
            next_node   = str(parsed.get("route", "agent")).strip().lower()
            employee_id = str(parsed.get("employee_id") or "").strip()
            email_id    = str(parsed.get("email_id") or "").strip()
        except (json.JSONDecodeError, ValueError):
            # Fallback: keyword position search in raw text
            lower = raw.lower()
            VALID = {"check_staff", "qdrant", "agent"}
            positions = {kw: lower.find(kw) for kw in VALID if kw in lower}
            next_node = min(positions, key=positions.get) if positions else "agent"
            if next_node == "check_staff":
                employee_id, email_id = _extract_staff_ids(str(question))

        if next_node not in {"check_staff", "qdrant", "agent"}:
            next_node = "agent"

        result: dict = {"next_node": next_node}

        if next_node == "check_staff":
            # Fallback to regex if LLM returned empty ids
            if not employee_id and not email_id:
                employee_id, email_id = _extract_staff_ids(str(question))
            tc_id = f"call_{uuid.uuid4().hex[:8]}"
            result["messages"] = [
                AIMessage(
                    content="",
                    tool_calls=[{
                        "id":   tc_id,
                        "name": check_staff_active.name,
                        "args": {"employee_id": employee_id, "email_id": email_id},
                    }],
                )
            ]
            logger.info(
                "_decide_tool: '%s...' → check_staff employee_id=%r email_id=%r",
                str(question)[:60], employee_id, email_id,
            )
        else:
            logger.info("_decide_tool: '%s...' → %s", str(question)[:60], next_node)

        return result

    def _route_from_decide(self, state: AgentState) -> str:
        return state.get("next_node", "agent")

    # ------------------------------------------------------------------
    # Graph nodes
    # ------------------------------------------------------------------

    async def _think(self, messages: list[AnyMessage]) -> str:
        """Produce a brief situation assessment from conversation history."""
        history_lines: list[str] = []
        for m in messages:
            if isinstance(m, HumanMessage):
                history_lines.append(f"User: {str(m.content)[:400]}")
            elif isinstance(m, AIMessage) and not getattr(m, "tool_calls", None):
                history_lines.append(f"Bot: {str(m.content)[:400]}")
            elif isinstance(m, ToolMessage):
                history_lines.append(f"Tool({m.tool_call_id[:8]}): {str(m.content)[:200]}")
        if not history_lines:
            return ""
        try:
            assessment = await self._llm_client.complete([
                {"role": "system", "content": THINK_PROMPT},
                {"role": "user",   "content": "\n".join(history_lines)},
            ])
            # logger.debug("_think assessment:\n%s", assessment)
            return assessment.strip()
        except Exception as exc:
            logger.warning("_think failed: %s", exc)
            return ""

    async def _call_agent(self, state: AgentState) -> dict:
        agent_turns = sum(1 for m in state["messages"] if isinstance(m, AIMessage))

        assessment = await self._think(state["messages"])
        messages_with_hint = list(state["messages"])
        if assessment:
            messages_with_hint.append(
                SystemMessage(content=f"[INTERNAL REASONING — not visible to user]\n{assessment}")
            )

        openai_msgs = _to_openai_messages(messages_with_hint)
        queue = _token_queue_var.get()

        # Stream the final answer directly with JSON format.
        # Tokens are pushed into the per-request queue so stream_answer can
        # forward them to the WebSocket without a separate Phase-2 LLM call.
        full_content = ""
        async for chunk in self._llm_client.stream_json(openai_msgs):
            if queue is not None:
                await queue.put(chunk)
            if isinstance(chunk, str):
                full_content += chunk

        response = AIMessage(content=full_content)
        logger.debug("_call_agent: turn=%d streamed %d chars", agent_turns, len(full_content))

        conversation_status = await self._evaluate_end_chat(state["messages"])
        return {"messages": [response], "conversation_status": conversation_status}

    async def _evaluate_end_chat(self, messages: list[AnyMessage]) -> int:
        """Return 2 if the conversation should end, 1 if it should continue."""
        history_lines: list[str] = []
        for m in messages:
            if isinstance(m, HumanMessage):
                history_lines.append(f"User: {str(m.content)[:300]}")
            elif isinstance(m, AIMessage) and not getattr(m, "tool_calls", None):
                history_lines.append(f"Bot: {str(m.content)[:300]}")
            elif isinstance(m, ToolMessage):
                history_lines.append(f"Tool result: {str(m.content)[:200]}")

        if not history_lines:
            return 1

        eval_messages = [
            {"role": "system", "content": END_CHAT_PROMPT},
            {"role": "user",   "content": "\n".join(history_lines)},
        ]
        try:
            raw = (await self._llm_client.complete(eval_messages)).strip().lower()
            result = 4 if "end" in raw else 1
            logger.debug("_evaluate_end_chat: %r → conversation_status=%d", raw, result)
            return result
        except Exception as exc:
            logger.warning("_evaluate_end_chat failed: %s", exc)
            return 1

    async def _call_check_staff(self, state: AgentState) -> dict:
        """Execute staff verification using tool_call args from the last AIMessage.

        The tool call is always present — either emitted by _decide_tool (direct route)
        or by the agent node. Calls staff_check_service.verify() directly.
        """
        last = state["messages"][-1]
        tool_calls = getattr(last, "tool_calls", None)
        if not tool_calls:
            logger.warning("_call_check_staff: no tool_calls on last message, skipping")
            return {"messages": []}

        tc = next((tc for tc in tool_calls if tc["name"] == check_staff_active.name), None)
        if not tc:
            return {"messages": []}

        employee_id = tc["args"].get("employee_id", "")
        email_id    = tc["args"].get("email_id", "")

        try:
            active = await staff_check_service.verify(employee_id, email_id)
        except Exception as exc:
            active = None
            logger.error("_call_check_staff: verify() failed: %s", exc)

        if active is True:
            content = (
                f"ACTIVE: Nhân viên {employee_id} tồn tại và đang hoạt động. "
                "Ghi nhận thông tin và tiến hành hỗ trợ."
            )
        elif active is False:
            content = (
                f"NOT_FOUND: Nhân viên {employee_id} không tìm thấy hoặc đã nghỉ việc. "
                "Yêu cầu người dùng kiểm tra lại."
            )
        else:
            content = (
                f"UNKNOWN: Không thể xác minh {employee_id} (lỗi kết nối). "
                "Xử lý như ACTIVE (fail open)."
            )

        logger.info("_call_check_staff: %s → %r", employee_id, content[:80])
        return {"messages": [ToolMessage(content=content, tool_call_id=tc["id"])]}

    async def _call_qdrant(self, state: AgentState) -> dict:
        """Execute KB search using tool_call args from the last AIMessage."""
        last = state["messages"][-1]
        results: list = []

        tool_calls = getattr(last, "tool_calls", None)
        if not tool_calls:
            # Direct route from decide_tool — synthesise tool call + result pair
            last_human = next(
                (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None
            )
            query = str(last_human.content if last_human else "")
            tc_id = f"call_{uuid.uuid4().hex[:8]}"
            results.append(AIMessage(
                content="",
                tool_calls=[{
                    "id":   tc_id,
                    "name": search_knowledge_base.name,
                    "args": {"query": query},
                }],
            ))
            content = await self._run_qdrant_search(query)
            results.append(ToolMessage(content=content, tool_call_id=tc_id))
            logger.info("_call_qdrant (direct): query=%r result_len=%d", query[:60], len(content))
        else:
            for tc in tool_calls:
                if tc["name"] == search_knowledge_base.name:
                    query = tc["args"].get("query", "")
                    content = await self._run_qdrant_search(query)
                    results.append(ToolMessage(content=content, tool_call_id=tc["id"]))
                    logger.info("_call_qdrant: query=%r result_len=%d", query[:60], len(content))

        return {"messages": results}

    async def _run_qdrant_search(self, query: str) -> str:
        if self._qdrant_search:
            try:
                return await self._qdrant_search(query)
            except Exception as exc:
                logger.error("_call_qdrant search failed: %s", exc)
                return f"(lỗi tìm kiếm KB: {exc})"
        return "(không có kết quả bổ sung — sử dụng ngữ cảnh hiện có)"

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _route(self, state: AgentState) -> str:
        last = state["messages"][-1]
        tool_calls = getattr(last, "tool_calls", None)
        if not tool_calls:
            return END

        agent_turns = sum(1 for m in state["messages"] if isinstance(m, AIMessage))
        if agent_turns >= _MAX_AGENT_TURNS:
            logger.warning("_route: max agent turns (%d) reached, forcing END", _MAX_AGENT_TURNS)
            return END

        return _TOOL_TO_NODE.get(tool_calls[0]["name"], END)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def stream_answer(self, openai_messages: list[dict]) -> AsyncIterator[str | dict]:
        """Run the graph as a background task; stream tokens from _call_agent in real-time.

        _call_agent now calls stream_json directly and pushes each token into a
        per-request asyncio.Queue (via _token_queue_var ContextVar).  This coroutine
        consumes that queue and yields tokens immediately — no second LLM call needed.
        """
        if not self.enabled:
            raise RuntimeError("AgentGraph not built — call agent_graph.build() in container.py")

        lc_messages = _to_lc_messages(openai_messages)
        token_queue: asyncio.Queue[str | dict | None] = asyncio.Queue()
        ctx_token = _token_queue_var.set(token_queue)

        async def _run_graph() -> dict:
            try:
                return await self._graph.ainvoke(
                    {"messages": lc_messages, "next_node": "agent", "conversation_status": 1}
                )
            except Exception as exc:
                logger.error("graph.ainvoke failed: %s", exc, exc_info=True)
                raise
            finally:
                await token_queue.put(None)  # sentinel — signals stream_answer to stop

        graph_task = asyncio.create_task(_run_graph())

        try:
            while True:
                chunk = await token_queue.get()
                if chunk is None:
                    break
                yield chunk  # str token or usage dict from stream_json
        except Exception:
            graph_task.cancel()
            raise
        finally:
            _token_queue_var.reset(ctx_token)

        final_state = await graph_task
        conv_status = final_state.get("conversation_status", 1)
        logger.debug("stream_answer: complete, conv_status_eval=%d", conv_status)

        # Authoritative evaluation result — rag.py overrides conversation_status with this.
        yield {"conv_status_eval": conv_status}


agent_graph = AgentGraph()
