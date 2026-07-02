# Tại sao dùng LangGraph? Có thể dùng LangChain thay không?

Trả lời ngắn gọn: **không bắt buộc phải dùng LangGraph**, và LangChain là một công cụ
*khác loại* chứ không phải bản thay thế 1-đổi-1.

---

## 1. LangGraph đang thực sự làm gì trong app này?

Nhìn vào `app/orchestration/graph.py`, "graph" chỉ có **1 node**:

```
respond ──► END
```

Nó **không** dùng sức mạnh thật của LangGraph (nhiều node, rẽ nhánh có điều kiện, vòng lặp,
agent, human-in-the-loop). Thứ duy nhất LangGraph mang lại giá trị ở đây là:

> **Checkpointer** (`MemorySaver`, keyed theo `session_id`) — lưu state per-session giữa
> các lượt: `greeted`, `awaiting_admin`, `unanswered`.

Tức là LangGraph đang bị dùng gần như chỉ để làm **"bộ nhớ state của hội thoại"**, chứ
không phải để điều phối luồng phức tạp.

---

## 2. LangChain vs LangGraph — khác gì?

| | LangChain | LangGraph |
|---|---|---|
| Vai trò | Ghép nối các bước LLM: prompt template, retriever, RAG chain, output parser… | Điều phối **stateful, nhiều bước, có vòng lặp** (agent, workflow), kèm checkpoint/persistence |
| Hợp với | "Chuỗi" tuyến tính: input → retrieve → prompt → LLM → output | Luồng có nhánh/vòng, cần nhớ state qua nhiều lượt, human-in-loop |
| Trong app này | Có thể thay phần RAG chain | Đang lo phần state per-session |

Lưu ý quan trọng: **app hiện KHÔNG dùng LangChain chút nào.** Code gọi thẳng OpenAI SDK
(`OpenAILLMClient`), Qdrant client, đọc CSV trực tiếp — theo kiến trúc ports/adapters sạch
(interfaces + adapter). LangGraph là dependency LLM-framework *duy nhất*.

---

## 3. Có thay được không? → Được. 2 lựa chọn:

### Lựa chọn A — Plain Python (khuyến nghị cho app này)
Bỏ hẳn LangGraph. Thay checkpointer bằng một dict state theo `session_id`, giữ nguyên logic
if/elif trong `respond`.

- ✅ Bớt 1 dependency lớn, code minh bạch hơn, hợp với style "gọi SDK trực tiếp".
- ✅ History đã ở localStorage + state đã ở RAM → thay `MemorySaver` bằng `dict` là xong.
- ❌ Nếu sau này cần luồng phức tạp (nhiều node, agent), phải tự viết.

### Lựa chọn B — LangChain
Dùng LangChain cho RAG chain + memory (VD `RunnableWithMessageHistory`, retriever Qdrant).

- ✅ Idiomatic nếu quen hệ sinh thái LangChain, có sẵn nhiều tích hợp.
- ❌ Thêm dependency nặng, và **trùng lặp** với các adapter đã tự viết (đang gọi OpenAI/Qdrant
  trực tiếp) → phải viết lại kha khá.

---

## Khuyến nghị

Vì app đang ở style "clean architecture + gọi SDK trực tiếp", và đang liên tục **cắt bớt
dependency** (numpy, sqlite): nếu muốn bỏ LangGraph thì nên đi **Plain Python**, chứ không
nên thêm LangChain — LangChain sẽ là một lớp trừu tượng thừa chồng lên thứ đã có.

Chỉ nên giữ/đổi sang framework khi dự định luồng phức tạp hơn (router nhiều intent, gọi tool,
nhiều bước suy luận) — lúc đó **LangGraph** mới đáng giá hơn LangChain.

---

## Tóm tắt 1 dòng
- **Giữ LangGraph:** khi sắp có luồng nhiều node/agent/vòng lặp.
- **Plain Python:** khi muốn nhẹ, sạch, ít dependency (phù hợp app hiện tại).
- **LangChain:** khi muốn tận dụng hệ sinh thái chain/retriever/memory của LangChain.
