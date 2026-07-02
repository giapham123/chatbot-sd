# Chatbot SD — Overview & Architecture

MAFC Service Desk chatbot. It replies **like a person, grounded in a knowledge
base** (RAG), streams answers token-by-token, remembers the conversation, and
forwards to a human administrator when it can't help.

- **Runtime mode:** conversational **RAG** (LLM + KB + chat history). No scripted
  state machine.
- **Stack:** LangGraph · OpenAI (chat + embeddings) · FastAPI (SSE streaming) ·
  SQLite (durable sessions + transcripts) · in-memory vector store.

---

## 1. What it does (behavior)

Each user turn, the bot does exactly one of:

| Situation | Response |
|---|---|
| First message of a session | **Welcome** greeting |
| Small talk (`hi`, `cảm ơn`, `ok`…) | Natural, short reply (no KB lookup) |
| A question the KB covers | **RAG answer** — grounded, streamed token-by-token |
| A question the KB does **not** cover | Asks for **MSNV/email**, then forwards it + the unanswered question to the **administrator** |

Design rules:
- **Grounded, no hallucination** — answers come from retrieved KB context + history.
- **Never dead-ends** — the chat stays open; when stuck, it escalates to a human.
- **Multi-turn** — the last N messages are fed to the LLM for follow-ups.

---

## 2. High-level architecture

```
                 Browser / API client
                        │  POST /chat {session_id, message}
                        ▼
        ┌──────────────────────────────────────────┐
        │  app/main.py — FastAPI + SSE + demo UI     │   Web / transport layer
        └───────────────────┬──────────────────────┘
                            ▼
        ┌──────────────────────────────────────────┐
        │  ConversationService                       │   Orchestration:
        │  (app/orchestration/conversation.py)       │   run graph, stream events,
        └───────┬───────────────────────┬───────────┘   persist transcript, feed history
                │                       │
                ▼                       ▼
   ┌─────────────────────┐   ┌────────────────────────┐
   │ LangGraph graph      │   │ TranscriptStore (SQLite)│  history + audit
   │ single `respond` node│   └────────────────────────┘
   │ (orchestration/graph)│
   └───────┬─────────────┘
           │ decides: welcome / smalltalk / RAG / ask-MSNV→admin
           ▼
   ┌─────────────────────┐   ┌───────────────┐   ┌──────────────────┐
   │ RagService          │──▶│ EmbeddingClient│   │ ActionExecutor    │
   │ (services/rag.py)    │   │ + VectorStore  │   │ (notify admin)    │
   │  retrieve + prompt   │   └───────────────┘   └──────────────────┘
   │  + stream via LLM     │
   └───────┬─────────────┘
           ▼
     OpenAI (chat completions, streamed) / OpenAI embeddings
```

State (`greeted`, `awaiting_admin`, `unanswered`) is checkpointed per session in
SQLite, so it survives server restarts.

---

## 3. Layered design (SOLID)

The code is organized by concern; high-level policy depends only on abstractions
(ports) in `app/domain/interfaces.py`. Concrete adapters are injected in **one**
place: `app/container.py` (composition root).

```
app/
├── main.py                  # web layer: FastAPI, SSE frames, demo UI, debug entry
├── container.py             # composition root — wires adapters to ports (DIP)
├── config.py                # env-driven Settings (models, RAG params, DB paths)
│
├── domain/                  # the core — no framework/IO deps
│   ├── models.py            #   dataclasses: KBDoc, Emission, EmissionKind, …
│   └── interfaces.py        #   ports: LLMClient, EmbeddingClient, VectorStore,
│                            #          RagService, TranscriptStore, ActionExecutor,
│                            #          KnowledgeRepository, …
│
├── repositories/
│   └── csv_repositories.py  # CsvDataContext: loads data/*.csv into typed objects
│
├── services/                # capabilities behind the ports
│   ├── llm.py               #   OpenAI chat (streaming) + embeddings adapters
│   ├── vector_store.py      #   InMemoryVectorStore (cosine similarity, NumPy)
│   ├── rag.py               #   DefaultRagService: retrieve → prompt → stream
│   ├── actions.py           #   LoggingActionExecutor (forward-to-admin sink)
│   └── transcripts.py       #   SqliteTranscriptStore (persist + recent turns)
│
└── orchestration/
    ├── graph.py             # LangGraph: single conversational `respond` node
    └── conversation.py      # ConversationService: stream events + transcript + history
```


| SOLID | Where |
|---|---|
| **S**ingle responsibility | retrieval (`RagService`), streaming (`ConversationService`), persistence (`TranscriptStore`), web (`main.py`) are separate |
| **O**pen/closed | swap embeddings/LLM/vector store/notifier by adding an adapter — no change to policy |
| **L**iskov | every adapter honors its port contract and is interchangeable |
| **I**nterface segregation | small focused ports (`LLMClient`, `VectorStore`, `TranscriptStore`, …) |
| **D**ependency inversion | services/graph depend on `domain/interfaces`; concretes wired only in `container.py` |

---

## 4. Request lifecycle (one turn)

```
1. POST /chat {session_id, message}                         app/main.py
2. ConversationService.stream(session_id, message)          orchestration/conversation.py
     a. history = TranscriptStore.recent(session_id, N)     (prior turns)
     b. TranscriptStore.append(session_id, "user", message)
     c. state = graph.ainvoke({user_input, session_id})     orchestration/graph.py
          respond node decides:
            greet | smalltalk | RAG plan | ask-MSNV | forward-to-admin
     d. render emissions to SSE events:
            - TEXT        -> "message"        (scripted / greeting / ask)
            - RAG_ANSWER  -> "token"* + "message_end"  (streamed from LLM w/ history)
            - HANDOFF     -> "message"? + "handoff"     (forwarded to admin)
        each bot output is appended to the TranscriptStore
3. SSE stream returned to the client
```

**SSE events:** `message` · `token` · `message_end` · `handoff` · `error` · `done`.

---

## 5. Data

All under `data/` (generated by `scripts/build_data.py`).

| Sheet | Purpose |
|---|---|
| `knowledge_base.csv` | RAG corpus (105 docs: topic answers, paraphrases, FAQs). Embedded at startup. |
| `responses.csv` | Fixed messages: `welcome`, `fb_ask` (ask MSNV), `fb_done` (forwarded), `handoff_sd`. |
| `actions.csv` | `notify_admin` — forwards `{msnv_email, unanswered}` to the administrator. |
| `settings.csv` | Small key/value settings (e.g. `welcome_message_key`). |

Regenerate: `python scripts/build_data.py` (edit `PARAPHRASES` / `EXTRA_KB` to grow the KB).

---

## 6. RAG pipeline

```
startup:  KB rows ──embed──▶ InMemoryVectorStore   (services/rag.py build_index)

per query:
  message ─embed─▶ vector search (top_k, cosine) ─▶ score ≥ RAG_MIN_SCORE ?
        │                                                  │
        └── small talk ─▶ answer with empty context        ├─ yes ─▶ build prompt
                                                           │        (system + history + context)
                                                           │        ─▶ stream tokens from LLM
                                                           └─ no  ─▶ ask MSNV → notify_admin
```

- **Embeddings:** `text-embedding-3-small`. **Chat:** `gpt-4o` (streamed).
- **Vector store:** in-memory NumPy cosine (rebuilt each startup). Swap for
  Qdrant/Chroma by implementing `VectorStore` and wiring it in `container.py`.

---

## 7. Persistence & memory

| Concern | Backend | File |
|---|---|---|
| Durable session state (`greeted`, `awaiting_admin`, …) | LangGraph `AsyncSqliteSaver` | `var/checkpoints.sqlite` |
| Chat transcripts (audit) | `SqliteTranscriptStore` | `var/transcripts.sqlite` |
| Multi-turn memory | last `HISTORY_TURNS` messages fed to the LLM | (from transcripts) |

`var/` is gitignored. Connections open in the container, closed on FastAPI shutdown.

---

## 8. Configuration (`.env`)

```
OPENAI_API_KEY=...                 # required (keep out of source)
OPENAI_CHAT_MODEL=gpt-4o
OPENAI_EMBED_MODEL=text-embedding-3-small
RAG_TOP_K=3
RAG_MIN_SCORE=0.30                 # below this = "can't answer" -> forward to admin
HISTORY_TURNS=6
CHECKPOINTS_DB=var/checkpoints.sqlite
TRANSCRIPTS_DB=var/transcripts.sqlite
```

---

## 9. Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add OPENAI_API_KEY

uvicorn app.main:app --reload         # dev (hot reload)
python -m app.main                    # debug (breakpoints; no reload)
```

Open <http://localhost:8000> for the demo UI, or:

```bash
curl -N -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s1","message":"tôi không kết nối được vpn"}'
```

---

## 10. Extension points (change one adapter, nothing else)

| Want to… | Do this |
|---|---|
| Grow/curate answers | edit `knowledge_base.csv` (via `scripts/build_data.py`) |
| Actually email/Slack the admin | implement `ActionExecutor` (replace `LoggingActionExecutor`) in `container.py` |
| Persistent/scalable vectors | implement `VectorStore` (Qdrant/Chroma) in `container.py` |
| Different LLM / provider | implement `LLMClient` / `EmbeddingClient` in `container.py` |
| Store transcripts elsewhere | implement `TranscriptStore` (Postgres, etc.) |

---

## 11. Related docs

- [`README.md`](./README.md) — quick start & run.
- [`CHATBOT_SD_RULES.md`](./CHATBOT_SD_RULES.md) — original spec (historical).