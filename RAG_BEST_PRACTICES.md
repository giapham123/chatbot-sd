# RAG — Giải pháp tốt nhất & Lộ trình lên production

Đánh giá dựa trên codebase hiện tại (Qdrant + OpenAI + FastAPI, kiến trúc ports/adapters).

---

## ✅ Đã triển khai (trong lần cập nhật này)

### 1. Contextualize query (rewrite multi-turn) — `app/services/rag.py`
Trước khi retrieve, dùng LLM rẻ (`OPENAI_ROUTER_MODEL` = `gpt-4o-mini`) viết lại câu hỏi
nối tiếp thành **câu hỏi độc lập** dựa trên lịch sử.
- Ví dụ: `"vẫn bị thì sao?"` + history → `"Máy tính bị treo phải làm sao?"`
- Không có history (lượt đầu) → bỏ qua, dùng nguyên câu.
- Lỗi rewrite → tự động fallback về câu gốc (không làm hỏng luồng).

### 2. Reranking — `app/services/rag.py`
- Lấy **nhiều candidate hơn** từ Qdrant (`RERANK_CANDIDATES=8`), lọc theo `min_score`.
- LLM rẻ chấm & sắp xếp lại theo độ liên quan thật, rồi lấy `top_k`.
- Lỗi rerank → giữ nguyên thứ tự vector (an toàn).

### Cấu hình mới (`.env`)
```
OPENAI_ROUTER_MODEL=gpt-4o-mini   # model rẻ cho rewrite + rerank
RERANK_CANDIDATES=8               # số hit lấy về trước khi rerank
```

### Chi phí / độ trễ
Mỗi lượt RAG giờ có tối đa 3 lần gọi LLM: **rewrite** (mini) + **rerank** (mini) +
**answer** (gpt-4o, streaming). Dùng model mini cho 2 bước đầu để tiết kiệm.

---

## 🔴 Nên làm tiếp (ưu tiên cao)

### 3. Tập đánh giá (eval set) — nền tảng để tối ưu
`min_score=0.30`, `top_k=3` đang là số đặt tay. Không đo thì không cải thiện được.
- Tạo ~50–100 cặp *(câu hỏi thực tế → doc_id đúng)*.
- Đo **recall@k**, tỉ lệ trả lời sai, tỉ lệ handoff.
- Tune ngưỡng/model theo số liệu (ví dụ hiện match đúng ~1.0, liên quan ~0.60–0.67 →
  ngưỡng 0.30 có thể cho lọt match yếu).

### 4. Hybrid search (dense + BM25/sparse)
Qdrant hỗ trợ. Giúp bắt đúng **từ khóa cứng**: mã lỗi, tên app, mã máy — thứ embedding hay bỏ sót.

### 5. Trích nguồn (citation)
Cho bot kèm `doc_id`/`source` để kiểm chứng, chống bịa.

---

## 🟢 Khi có nhiều business (scale & production)

### 6. ⚠️ State đang ở RAM (`MemorySaver`) → điểm chặn scale lớn nhất
Chạy **nhiều worker/instance** → mỗi process RAM riêng → cùng 1 user rơi vào worker khác
sẽ mất state (`greeted`, `awaiting_admin`).
- Giải pháp khi scale: chuyển state sang **Redis** (shared), hoặc làm stateless (client gửi state).
- Chạy 1 worker thì tạm ổn — nhưng đó là **trần scale**.

### 7. Qdrant production
Local Docker chỉ hợp dev → lên **Qdrant Cloud** hoặc cluster có replica + snapshot/backup.
Config sẵn `QDRANT_URL` / `QDRANT_API_KEY` để đổi.

### 8. Chi phí & tốc độ LLM
`gpt-4o` đắt. Volume lớn: dùng `gpt-4o-mini` cho phần lớn câu, chỉ escalate khi khó.

### 9. Caching
- Cache embedding của query lặp lại.
- **Semantic cache** cho câu trả lời (câu hỏi na ná → trả cache) → giảm cost + latency mạnh.

### 10. Độ bền & bảo vệ
- Retry/backoff cho OpenAI & Qdrant (rate limit khi đông).
- Rate-limit + auth cho endpoint `/chat`.

### 11. Observability
Đã có: log kết quả Qdrant (`app/services/vector_store.py`) + log rewrite/rerank (`rag.py`).
Nên thêm: theo dõi **tỉ lệ handoff**, điểm retrieval trung bình, latency, cost/ngày.
Công cụ: Langfuse / Phoenix. → Đây là cách đo "RAG có success không" bằng số liệu.

### 12. Quy trình cập nhật KB
Upsert incremental (chỉ re-embed doc thay đổi), versioning collection → mở rộng KB không downtime.

---

## Lộ trình tóm tắt

| Ưu tiên | Việc | Trạng thái |
|---|---|---|
| 🔴 | Contextualize query (multi-turn) | ✅ Đã làm |
| 🔴 | Reranking | ✅ Đã làm |
| 🔴 | Eval set + tune min_score/top_k | ⬜ Nên làm tiếp |
| 🟡 | Hybrid search | ⬜ |
| 🟡 | Citation nguồn | ⬜ |
| 🟢 | Redis state (đa worker) | ⬜ Khi scale |
| 🟢 | Qdrant Cloud + backup | ⬜ Khi scale |
| 🟢 | Semantic cache + router model | ⬜ Khi scale |
| 🟢 | Observability (Langfuse/Phoenix) | ⬜ |

## Khuyến nghị
Bước tiếp theo đáng giá nhất là **eval set** (mục #3): có số liệu rồi mới tune được ngưỡng và
đo được hiệu quả của rewrite/rerank vừa thêm. Phần scale (Redis/Qdrant Cloud) chỉ cần khi
traffic thật sự tăng — kiến trúc ports/adapters cho phép đổi từng phần mà không đập lại toàn bộ.