# Giải thích LangGraph (trong dự án Chatbot SD) 🇻🇳

Tài liệu giải thích LangGraph được dùng như thế nào trong dự án này.
Xem thêm kiến trúc tổng thể ở [`ARCHITECTURE.md`](./ARCHITECTURE.md).

---

## 1. LangGraph là gì?

**LangGraph** là thư viện để xây dựng ứng dụng LLM dưới dạng **đồ thị (graph)** gồm các
**node** (nút xử lý) nối với nhau bằng **edge** (cạnh). Nó giúp:

- Quản lý **state** (trạng thái hội thoại) đi qua từng node.
- **Lưu state theo từng phiên** (checkpointer) → nhớ được giữa các lượt chat, sống sót
  qua khi restart server.
- Điều khiển luồng: rẽ nhánh, lặp, gọi tool… (dự án mình chỉ dùng 1 node nên rất đơn giản).

> Hình dung: **State = cuốn sổ ghi chú**, đi qua từng **node**; mỗi node đọc/ghi vào sổ
> rồi chuyển tiếp theo **edge**.

---

## 2. Các khái niệm chính (map với code)

Tất cả nằm trong `app/orchestration/graph.py`.

### a) State — trạng thái hội thoại
```python
class ConversationState(TypedDict, total=False):
    user_input: str        # tin nhắn user gửi lượt này
    session_id: str
    greeted: bool          # đã chào chưa?
    awaiting_admin: bool   # đang chờ user cung cấp MSNV/email để gửi admin?
    unanswered: str        # câu hỏi bot không trả lời được
    emissions: list[dict]  # kết quả bot "phát ra" lượt này
```
Đây là "cuốn sổ". Mỗi node nhận state, trả về **phần cần cập nhật** (dict),
LangGraph tự merge vào.

### b) Node — nút xử lý
Dự án chỉ có **1 node duy nhất tên `respond`** (vì đã bỏ flow, chuyển sang RAG hội thoại):
```python
async def respond(state):
    message = state.get("user_input", "")

    if not state.get("greeted"):          # 1) lần đầu → chào
        return {"greeted": True, "emissions": [câu chào]}

    if state.get("awaiting_admin"):       # 2) user vừa gửi MSNV → chuyển admin
        action_executor.execute(notify_action, ...)
        return {"awaiting_admin": False, "emissions": [xác nhận, HANDOFF]}

    if _is_smalltalk(message):            # 3) chào hỏi/cảm ơn → trả lời tự nhiên
        return {"emissions": [RAG_ANSWER context rỗng]}

    plan = await rag.plan_async(message)  # 4) tìm trong knowledge base
    if plan.kind == RAG_ANSWER:           #    có → trả lời dựa trên KB
        return {"emissions": [plan]}
    return {"awaiting_admin": True, ...}  #    không → hỏi MSNV để gửi admin
```
Node = một hàm: **nhận `state` → trả về `dict` cập nhật state**.

### c) Xây và biên dịch graph
```python
graph = StateGraph(ConversationState)   # tạo graph với kiểu state
graph.add_node("respond", respond)      # thêm node
graph.set_entry_point("respond")        # điểm bắt đầu
graph.add_edge("respond", END)          # respond → kết thúc
return graph.compile(checkpointer=...)  # biên dịch, gắn bộ nhớ
```
Luồng cực đơn giản: `START → respond → END`.

### d) Checkpointer — bộ nhớ theo phiên
```python
# trong app/container.py
checkpointer = AsyncSqliteSaver(cp_conn)   # lưu state vào SQLite
graph = build_conversation_graph(..., checkpointer=checkpointer)
```
Nhờ checkpointer, state (`greeted`, `awaiting_admin`…) được **lưu theo `session_id`** và
**không mất khi restart server**.

### e) Gọi graph mỗi lượt chat
```python
# trong app/orchestration/conversation.py
config = {"configurable": {"thread_id": session_id}}   # thread_id = mã phiên
state = await self._graph.ainvoke({"user_input": user_input}, config)
```
- `ainvoke` = chạy graph 1 lượt (async).
- `thread_id` giúp LangGraph **nạp đúng state cũ** của phiên đó, chạy `respond`,
  rồi **lưu lại** state mới.
- Kết quả trả về là state cuối → mình đọc `state["emissions"]` để stream ra cho user.

---

## 3. Một lượt chat chạy thế nào?

```
User gửi "không kết nối vpn"
        │
        ▼
ainvoke({user_input}, {thread_id: session_id})
        │  LangGraph nạp state cũ từ SQLite (đã greeted=True)
        ▼
   node "respond":
        - đã chào rồi, không phải smalltalk
        - rag.plan_async("không kết nối vpn") → tìm thấy trong KB
        - trả về emissions = [RAG_ANSWER]
        │
        ▼
LangGraph lưu state mới vào SQLite (checkpoint)
        │
        ▼
ConversationService đọc emissions → stream token ra SSE cho user
```

---

## 4. Tại sao dùng LangGraph ở đây (dù chỉ 1 node)?

Chủ yếu để tận dụng **2 điểm mạnh**:

1. **Quản lý state + checkpointer** → nhớ hội thoại theo phiên, sống qua restart
   (thay vì tự viết code lưu/nạp state).
2. **Dễ mở rộng** → sau này muốn thêm bước (ví dụ: node phân loại ý định, node kiểm
   duyệt nội dung, node gọi tool) chỉ cần `add_node` + `add_edge`, không phải viết lại
   logic điều phối.

> **Tóm gọn:** LangGraph trong dự án = **một node `respond`** đưa ra quyết định
> (chào / trò chuyện / trả lời từ KB / chuyển admin), cộng với **bộ nhớ SQLite theo
> phiên**. Phần "trả lời như người thật" là do **RAG + LLM** (trong `services/rag.py`),
> còn LangGraph lo phần **điều phối và ghi nhớ trạng thái**.

---

## 5. Muốn thêm 1 node mới thì làm sao? (ví dụ)

Giả sử muốn thêm node **kiểm duyệt nội dung** trước khi trả lời:

```python
def moderate(state):
    if _có_từ_ngữ_xấu(state["user_input"]):
        return {"emissions": [{"kind": "text", "text": "Vui lòng dùng từ ngữ phù hợp ạ."}],
                "route": "block"}
    return {"route": "ok"}

graph.add_node("moderate", moderate)
graph.set_entry_point("moderate")
graph.add_conditional_edges("moderate", lambda s: s["route"],
                            {"block": END, "ok": "respond"})
graph.add_edge("respond", END)
```

Chỉ cần thêm node + cạnh; `respond` giữ nguyên. Đó là lợi thế của LangGraph.