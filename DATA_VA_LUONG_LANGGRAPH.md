# Giải thích folder `data/` và luồng chạy LangGraph

## 1. Các file trong folder `data/`

4 file CSV đóng vai trò "cơ sở dữ liệu" của chatbot (nạp qua `app/repositories/csv_repositories.py`).
Runtime thực tế chỉ dùng 3 file: `responses`, `actions`, `knowledge_base`.

### `knowledge_base.csv` — Kho tri thức (RAG corpus) 📚
Cột: `doc_id, topic_id, question, answer, source, tags` (~105 dòng).

- Mỗi dòng là 1 cặp Hỏi–Đáp về Service Desk (VD: app treo, internet chậm, màn hình không lên…).
- Là nguồn để bot **tìm kiếm ngữ nghĩa (semantic search)** và trả lời có căn cứ.
- Nạp bởi `CsvKnowledgeRepository`. Cũng chính là file mà `scripts/embed_to_qdrant.py` embed lên Qdrant.

### `responses.csv` — Câu trả lời cố định (scripted) 💬
Cột: `response_key, text, variables`. Các câu bot nói sẵn, **không** qua LLM:

| key | Dùng khi |
|---|---|
| `welcome` | Lời chào lần đầu |
| `fb_ask` | Bot không trả lời được → xin MSNV/email công ty |
| `fb_done` | Đã ghi nhận & chuyển cho admin |
| `handoff_sd` | Fallback chuyển cho nhân viên hỗ trợ trực tiếp |

### `actions.csv` — Định nghĩa hành động 🔔
Cột: `action_id, type, message_key, payload`.

- Chỉ có 1 action: `notify_admin` (type `handoff`) — chuyển thông tin cho quản trị viên.
- `payload = msnv_email, unanswered` là các biến được đính kèm khi thực thi.

### `settings.csv` — Cấu hình chung ⚙️
Cột: `key, value`. Hiện có `welcome_message_key=welcome`.

> ⚠️ File này **không** được `CsvDataContext` nạp trong code hiện tại — có vẻ để dành/legacy.

---

## 2. Luồng chạy trong LangGraph

### Kiến trúc tổng thể (wiring ở `app/container.py`)

```
CSV data ─┐
OpenAI ───┼─► DefaultRagService ─┐
Qdrant  ──┘   (embed query +     ├─► LangGraph (build_conversation_graph)
              search Qdrant)     │
                                    ConversationService (stream events → SSE)
```

> ✅ **App dùng Qdrant làm vector store** (`QdrantVectorStore` trong
> `app/services/vector_store.py`). Không còn embed KB vào RAM khi khởi động —
> mỗi lượt chỉ embed **câu hỏi** rồi search thẳng vào Qdrant.
>
> **Nạp dữ liệu trước khi chạy app:**
> ```bash
> .venv/bin/python scripts/embed_to_qdrant.py           # tạo/nạp collection
> .venv/bin/python scripts/embed_to_qdrant.py --recreate # nếu KB thay đổi
> ```
> Cấu hình qua `.env`: `QDRANT_URL`, `QDRANT_API_KEY` (cho Qdrant Cloud),
> `QDRANT_COLLECTION` (mặc định `chatbot_sd_kb`).

### Graph rất tối giản (1 node)
`app/orchestration/graph.py` chỉ có: `respond ──► END`.
Toàn bộ logic nằm trong hàm `respond`, state được **checkpoint theo `session_id`** (SQLite)
nên `greeted` / `awaiting_admin` sống sót qua nhiều request.

### Luồng xử lý mỗi lượt (hàm `respond`, theo thứ tự ưu tiên)

```
User gửi tin nhắn
      │
      ▼
┌─────────────────────────────────────────────┐
│ 1. Chưa chào (greeted=False)?                │──► Gửi welcome, set greeted=True
├─────────────────────────────────────────────┤
│ 2. Đang chờ MSNV (awaiting_admin=True)?      │──► Chạy action notify_admin
│                                              │    (gửi msnv_email + câu hỏi cũ)
│                                              │    → fb_done + HANDOFF
├─────────────────────────────────────────────┤
│ 3. Small talk? (mọi từ đều là xã giao)       │──► RAG_ANSWER không context
│    (_is_smalltalk: hi/chào/cảm ơn/ok…)       │    → LLM trả lời tự nhiên
├─────────────────────────────────────────────┤
│ 4. Còn lại → RAG (rag.plan_async)            │
│    • embed câu hỏi → search vector store     │
│    • score >= min_score → RAG_ANSWER         │──► LLM stream câu trả lời có căn cứ
│    • yếu/không có → HANDOFF                   │──► fb_ask (xin MSNV), set
│                                              │    awaiting_admin=True, lưu unanswered
└─────────────────────────────────────────────┘
```

### Sau graph: streaming (`app/orchestration/conversation.py`)
`ConversationService.stream()` biến các "emission" từ graph thành sự kiện gửi về client:

1. Nhận `history` (các lượt trước) **do client gửi kèm** từ `localStorage`, cắt còn `history_turns` (6) lượt gần nhất.
2. Gọi `graph.ainvoke(...)` với `thread_id = session_id`.
3. Duyệt `emissions`:
   - `TEXT` → sự kiện `message` (câu cố định).
   - `RAG_ANSWER` → gọi `rag.stream_answer()` phát từng `token`, kết bằng `message_end`.
     LLM nhận **system prompt** (đóng vai Trợ lý Service Desk MAFC) + lịch sử +
     `NGỮ CẢNH` (các KB doc trúng) + `CÂU HỎI`.
   - `HANDOFF` → gửi `message` + sự kiện `handoff`.
4. **Lưu trữ:** không còn SQLite. Lịch sử chat được **client lưu vào `localStorage`** và gửi lại
   mỗi request (bộ nhớ đa lượt). State graph (`greeted`/`awaiting_admin`) nằm trong **RAM**
   (`MemorySaver`), keyed theo `session_id` — mất khi restart server.

### Tóm tắt vòng đời "không trả lời được"

```
Hỏi câu ngoài KB → RAG fallback (fb_ask xin MSNV) → awaiting_admin=True
   → user gửi MSNV/email → action notify_admin gửi cho admin → fb_done + HANDOFF
```

---

## Điểm mấu chốt
- **Graph cực đơn giản** (1 node stateful).
- "Trí thông minh" nằm ở tầng RAG (embed query + search Qdrant + ngưỡng `min_score`) và LLM streaming.
- Vector store là **Qdrant** — nạp trước bằng `scripts/embed_to_qdrant.py`, app không giữ vector trong RAM.