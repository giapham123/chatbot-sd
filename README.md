# Chatbot SD

MAFC Service Desk chatbot — **LangGraph + OpenAI + RAG + streaming**.

The bot replies **like a person, grounded in the knowledge base** (`data/knowledge_base.csv`)
with multi-turn memory. Per turn: greet on first contact → answer small talk naturally →
answer from the KB when it can → otherwise ask for MSNV/email and forward it (with the
unanswered question) to the administrator. Answers stream token-by-token over SSE.

📐 **Full design: [`ARCHITECTURE.md`](./ARCHITECTURE.md)** (overview, diagrams, SOLID layering, RAG pipeline, persistence).

## Architecture (SOLID) — at a glance

```
app/
├── main.py                  # web layer: FastAPI + SSE + demo UI + debug entry
├── container.py             # composition root (wires adapters to ports)
├── config.py                # env settings
├── domain/                  # core: models.py, interfaces.py (ports)
├── repositories/            # csv_repositories.py (responses, actions, knowledge)
├── services/                # llm.py, vector_store.py, rag.py, actions.py, transcripts.py
└── orchestration/           # graph.py (LangGraph respond node), conversation.py (streaming)
```

```
 HTTP /chat ─► ConversationService ─► LangGraph `respond` ─► RagService ─► OpenAI
               (transcripts + history)   (greet/smalltalk/RAG/admin)   (embed + stream)
```

| Principle | Where |
|---|---|
| **S**ingle responsibility | `RagService` retrieves, `ConversationService` streams, `TranscriptStore` persists, `main.py` serves |
| **O**pen/closed | swap LLM / embeddings / vector store / notifier by adding an adapter |
| **L**iskov | every adapter honors its port contract, interchangeable |
| **I**nterface segregation | small ports in `domain/interfaces.py` (`LLMClient`, `VectorStore`, `TranscriptStore`, …) |
| **D**ependency inversion | policy depends on `domain/interfaces.py`; concretes wired only in `container.py` |

## Data (sheets)

`knowledge_base · responses · actions · settings` — all in `data/`, generated from
**`scripts/build_data.py`** (single source of truth). The KB has **105 docs** (topic
answers + paraphrases + FAQs) transcribed from the SD workbook. Regenerate with
`python scripts/build_data.py` (edit `PARAPHRASES` / `EXTRA_KB` to grow the KB).

## History & persistence

- **Transcripts** — every user/bot/system message is saved to SQLite
  (`var/transcripts.sqlite`) via `SqliteTranscriptStore`, keyed by `session_id`.
- **Durable sessions** — LangGraph state (`greeted`, `awaiting_admin`, …) is
  checkpointed to SQLite (`var/checkpoints.sqlite`), so a session survives a
  server restart instead of resetting.
- **Multi-turn memory** — the last `HISTORY_TURNS` messages (default 6) are fed to
  the LLM for RAG answers, so follow-up questions have context.

Config (`.env`): `CHECKPOINTS_DB`, `TRANSCRIPTS_DB`, `HISTORY_TURNS`. The `var/`
folder is gitignored. Note: the demo web UI generates a new `session_id` per page
load, so history continues within a page session (the backend persists per id).

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then put your OPENAI_API_KEY in .env

uvicorn app.main:app --reload
```

Open <http://localhost:8000> for the demo UI, or call the API:

```bash
curl -N -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s1","message":"tôi không kết nối được vpn"}'
```

### SSE events
`message` (fixed text) · `token` (streamed RAG answer) · `message_end` · `handoff`
(forwarded to admin) · `error` · `done`.

## Extending

- **Grow / curate answers** → edit `knowledge_base.csv` via `scripts/build_data.py`.
- **Actually notify the admin (email/webhook)** → implement `ActionExecutor` (replace `LoggingActionExecutor`) in `container.py`.
- **Production vector store** → implement `VectorStore` (e.g. Qdrant) in `container.py`.
- **Different LLM / provider** → implement `LLMClient` / `EmbeddingClient` in `container.py`.
- **Wording (welcome / ask-MSNV / forwarded)** → edit `data/responses.csv`.