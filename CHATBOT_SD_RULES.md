# Chatbot SD — Build Rules & Architecture

Rules and reference spec for building the **MAFC Service Desk (SD) chatbot** using
**LangGraph + OpenAI + RAG + Streaming**.

> Domain language stays Vietnamese (bot talks to MAFC staff in Vietnamese).
> Code, config, and this document are in English.

---

## 1. What we are building

A **hybrid chatbot**:

- **Guided flows (state machine)** — for known SD topics (e.g. *Yêu cầu nâng cấp RAM*),
  the bot follows a scripted decision tree: collect info → validate → confirm → transfer to SD.
- **RAG fallback (retrieval)** — for free-form questions not covered by a scripted flow,
  the bot retrieves from the SD knowledge base and answers, or transfers to a human if it can't.

Both run inside **one LangGraph graph** and stream tokens to the frontend.

---

## 2. Recommended stack

| Concern | Tool | Notes |
|---|---|---|
| Orchestration / state machine | **LangGraph** | Nodes = steps; edges = conditions; `MemorySaver`/`checkpointer` for per-session state |
| LLM | **OpenAI** `gpt-4o` (quality) / `gpt-4o-mini` (cheap, routing & validation) | Use mini for classification/validation, 4o for final answers |
| Embeddings | **OpenAI** `text-embedding-3-small` | Cheap, good enough for FAQ retrieval |
| Vector store | **Qdrant** (prod) or **Chroma/FAISS** (dev) | Store SD knowledge base chunks |
| Backend API | **FastAPI** | `/chat` endpoint with **SSE** streaming |
| Streaming | **SSE** (Server-Sent Events) | Simpler than WebSocket for one-way token stream |
| Session store | Redis (prod) / in-memory (dev) | Holds LangGraph checkpoints + retry counters |

---

## 3. Data model — split the single CSV into these sheets

Your current file is **one flat CSV = one topic**. That does not scale. Split it into the
sheets below. Each sheet maps to a specific tool responsibility.

### 3.1 `topics.csv` — topic catalog (routing)
Used by the **router** to decide which flow to enter.

| Column | Example | Purpose |
|---|---|---|
| `topic_id` | `ram_upgrade` | Stable key |
| `topic_name` | Yêu cầu nâng cấp máy tính-bộ nhớ | Display |
| `keywords` | nâng cấp, RAM, bộ nhớ, chậm, lag | Intent matching / router hints |
| `entry_node` | `ram_ask_id` | First node of the flow |
| `enabled` | `true` | Toggle a flow on/off |

### 3.2 `flows.csv` — conversation nodes (drives LangGraph)
This is the **heart** of the state machine. One row = one node.

| Column | Example | Purpose |
|---|---|---|
| `node_id` | `ram_ask_ram` | Stable key |
| `topic_id` | `ram_upgrade` | FK → topics |
| `node_type` | `ask` \| `validate` \| `message` \| `action` \| `end` | Node behavior |
| `bot_message_key` | `ask_ram_size` | FK → responses |
| `collect_slot` | `ram_size` | FK → slots (for `ask` nodes) |
| `validate_rule` | `email_mafc` | FK → validation_rules |
| `on_success` | `ram_confirm` | Next node id |
| `on_fail` | `ram_retry` | Next node id |
| `action` | `transfer_to_sd` | FK → actions (for `action`/`end` nodes) |

### 3.3 `responses.csv` — message templates
All bot text lives here (single source of truth, easy to edit without code).

| Column | Example |
|---|---|
| `response_key` | `ask_msnv` |
| `text` | Anh/chị vui lòng cung cấp MSNV/email? |
| `variables` | `{name}` (optional placeholders) |

### 3.4 `slots.csv` — entities to collect
| Column | Example |
|---|---|
| `slot_id` | `msnv_email` |
| `type` | `email` \| `choice` \| `text` |
| `choices` | `4GB,8GB,16GB` (for `choice`) |
| `required` | `true` |

### 3.5 `validation_rules.csv` — input validation + retry policy
| Column | Example | Purpose |
|---|---|---|
| `rule_id` | `email_mafc` | Key |
| `regex` | `^[\w.\-]+@mafc\.com\.vn$` | Format check |
| `max_retries` | `3` | Retry limit |
| `on_max_retries` | `end_chat_invalid` | Node when limit hit |

### 3.6 `actions.csv` — terminal / side-effect actions
| Column | Example |
|---|---|
| `action_id` | `transfer_to_sd` |
| `type` | `handoff` \| `end` |
| `message_key` | `transferred_to_sd` |
| `payload` | ticket fields sent to SD system |

### 3.7 `knowledge_base.csv` — RAG corpus (fallback answers)
Everything the bot can answer *without* a scripted flow. This is what gets **embedded**.

| Column | Example |
|---|---|
| `doc_id` | `kb_001` |
| `topic_id` | `ram_upgrade` (nullable) |
| `question` | Máy tôi chạy chậm phải làm sao? |
| `answer` | ... |
| `source` | SD policy v2 |
| `tags` | hardware, performance |

> **Rule:** scripted flows (`flows.csv`) take priority. RAG (`knowledge_base.csv`) only
> runs when the router finds no matching topic OR a flow explicitly hands off to RAG.

---

## 4. Flow of your RAM example, re-modeled

Your CSV becomes these `flows.csv` rows:

```
node_id        type      message_key       collect_slot  validate_rule  on_success     on_fail
ram_ask_id     ask       ask_msnv          msnv_email    email_mafc     ram_ask_ram    ram_retry_id
ram_retry_id   validate  invalid_info      msnv_email    email_mafc     ram_ask_ram    end_invalid
ram_ask_ram    ask       ask_ram_size      ram_size      -              ram_done       ram_ask_ram
ram_done       action    transferred_ok    -             -              -              (transfer_to_sd)
end_invalid    end       end_invalid_3x    -             -             -              (end)
```

Retry counter for `email_mafc` = **3** (`max_retries`), then → `end_invalid`.

---

## 5. LangGraph rules

1. **State object** (typed dict) carries: `messages`, `current_topic`, `current_node`,
   `slots` (collected values), `retry_counts`, `handoff` flag.
2. **One node function per `node_type`**, not per topic. The graph is *data-driven* —
   it reads `flows.csv`, it is not hard-coded per topic.
3. **Router node** at the top: classify user message → `topic_id` (use `keywords` +
   `gpt-4o-mini` classifier). No match → RAG node.
4. **Conditional edges** read `on_success` / `on_fail` from the flow row.
5. **Checkpointer** (`MemorySaver` dev / Redis prod) keyed by `session_id` so multi-turn
   state + retry counts survive between requests.
6. **Always** have a terminal path: every flow ends in `action` (handoff) or `end`.

---

## 6. RAG rules

1. Chunk `knowledge_base.csv` rows (Q+A per chunk is usually enough — small docs).
2. Embed with `text-embedding-3-small`; store in Qdrant/Chroma.
3. Retrieve top-k (k=3–4), pass as context to `gpt-4o`.
4. **Grounding rule:** if retrieval score is low / no relevant doc → do NOT hallucinate.
   Respond with the standard handoff message ("...đã được chuyển cho nhân viên hỗ trợ...")
   and trigger `transfer_to_sd`. This matches your CSV row *"2. Chatbot không trả lời được"*.
5. Cite `source` internally (for logging), not necessarily to the user.

---

## 7. Streaming rules

1. Backend: FastAPI endpoint returns `StreamingResponse` (SSE, `text/event-stream`).
2. Use LangGraph `.astream_events()` / `.astream()` to stream LLM tokens as they arrive.
3. Stream **only** LLM-generated text (RAG answers). Scripted `responses.csv` messages are
   fixed strings — send them as a single chunk (no need to fake token streaming).
4. Emit event types so the frontend can react: `token`, `handoff`, `end`, `error`.
5. On handoff/end, send a final event and close the stream.

---

## 8. Hard rules (do / don't)

- ✅ Keep all bot text in `responses.csv` — never hard-code Vietnamese strings in code.
- ✅ Email must match `@mafc.com.vn` before proceeding; enforce the 3-retry limit.
- ✅ Log every conversation (session_id, topic, slots, outcome) for auditing.
- ✅ Default to **human handoff** when unsure — never guess SD policy.
- ❌ Don't let RAG override a scripted flow that's already in progress.
- ❌ Don't store PII (MSNV/email) beyond the session/ticket without a retention policy.
- ❌ Don't call `gpt-4o` for validation — use regex + `gpt-4o-mini`.

---

## 9. Suggested project layout

```
chatbot-sd/
├── data/
│   ├── topics.csv
│   ├── flows.csv
│   ├── responses.csv
│   ├── slots.csv
│   ├── validation_rules.csv
│   ├── actions.csv
│   └── knowledge_base.csv
├── app/
│   ├── graph.py         # LangGraph: nodes, edges, state
│   ├── router.py        # topic classification
│   ├── rag.py           # embed + retrieve + answer
│   ├── loader.py        # load csv sheets into memory
│   ├── validators.py    # regex + retry logic
│   └── main.py          # FastAPI + SSE streaming
├── .env                 # OPENAI_API_KEY, etc.
└── CHATBOT_SD_RULES.md
```

---

## 10. Build order (milestones)

1. Split the CSV → the 7 sheets in `data/`.
2. `loader.py` — load sheets into typed objects.
3. `graph.py` — data-driven LangGraph over `flows.csv` (RAM topic first).
4. `validators.py` — email + retry.
5. `main.py` — FastAPI `/chat` with SSE streaming (scripted flow only).
6. `rag.py` — embed `knowledge_base.csv`, add RAG fallback node.
7. Router — auto-pick topic vs RAG.
8. Logging + human handoff integration with the real SD system.