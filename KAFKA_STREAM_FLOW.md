# Kafka → Streaming Response Flow

End-to-end trace from an incoming `chat_sd` Kafka message to token-by-token reply.

---

## Overview

> **LangGraph is NOT used in this flow.**
> `ConversationService.stream()` calls `rag.answer_stream()` directly.
> The graph (`build_conversation_graph`) is wired into `ConversationService` but
> never invoked from the Kafka path — it exists only for potential non-streaming callers.

```
Kafka (ai-agent-chat)
  └─► router.py          route by URI
        └─► worker.py    per-channel queue + merge
              └─► handler.py     WebSocket token events
                    └─► conversation.py   StreamEvent generator  (no graph)
                          └─► rag.py      answer_stream()
                                └─► llm.py   stream_json()
                                      └─► OpenAI (stream=True + json_object)
```

---

## Step 1 — Kafka consumer (`kafka/router.py`)

```
consume_loop()
  receives: { "uri": "api/v1/chat_sd", "data": { "channel_id": "...", ... } }
  uri == "api/v1/chat_sd"
    → worker.dispatch(raw, kafka_msg, consumer, producer, conversation, ...)
```

---

## Step 2 — Per-channel worker (`kafka/worker.py`)

```
dispatch()
  → put message into channel_id's asyncio.Queue
  → if no worker running → asyncio.create_task(channel_worker(...))

channel_worker() — runs one loop per batch:
  1. wait for first item from queue  (blocks up to CHANNEL_IDLE_TIMEOUT = 30 s)
  2. drain() — grab every other message already queued right now (non-blocking)
  3. merge_batch() — N simultaneous questions → 1 combined query
  4. inject _channel_history[channel_id] to replace stale client history
  5. await handle_chat_sd(data, conversation, message_id, ws_client)
  6. producer.send(output_topic, { "uri": "/api/v1/bot/chat/reply", "data": output })
  7. _channel_history[channel_id] = output["chat_history"]
  8. loop back to step 1 (worker stays alive for CHANNEL_IDLE_TIMEOUT)
```

---

## Step 3 — Handler (`kafka/handler.py`)

```
handle_chat_sd(data, conversation, message_id, ws_client)
  → ws_client.send(status="start")           (if WebSocket connected)
  → async for event in conversation.stream(...):
      "token"       → ws_client.send(status="processing", text=event.data)
      "message_end" → ws_client.send(status="done")
      "output"      → output = json.loads(event.data)
  → return output                             (worker sends this to bot-agent via Kafka)
```

---

## Step 4 — ConversationService (`orchestration/conversation.py`)

```
stream(channel_id, user_input, history, ...)
  → history = last history_turns pairs
  → async for item in rag.answer_stream(user_input, history, ...):
      str  → yield StreamEvent("token", item)     ← fires immediately per LLM chunk
      dict → save as result
  → yield StreamEvent("message_end")
  → build output dict  {channel_id, agent_id, answer, chat_history,
                         conversation_status, identify, error, ...}
  → yield StreamEvent("output", json.dumps(output))
```

---

## Step 5 — RAG service (`services/rag.py`)

```
answer_stream(query, history, conversation_status, error_email)

  _build_messages():
    1. _contextualize()      — router LLM rewrites follow-up into standalone query
    2. embedder.embed()      — embed standalone query
    3. vector_store.search() — Qdrant top-K candidates (up to rerank_candidates)
    4. filter by min_score
    5. _rerank()             — router LLM re-orders by true relevance
    6. assemble system prompt + history + KB context + user question

  async for chunk in llm.stream_json(messages):
    accumulate raw_parts (full JSON for parsing later)

    state machine — two phases:
      PHASE 1 (searching):
        append chunk to search_buf
        scan for pattern:  "response"\s*:\s*"
        when found → enter PHASE 2, process content after the opening quote

      PHASE 2 (inside response value):
        _extract_until_quote(chunk, escape_next):
          char-by-char:
            '\'  → set escape_next flag
            \n \t \r  → unescape and include
            '"'  → end of value, return to PHASE 1
            else → accumulate to output
        yield clean text chunk   ← only the response field, no JSON framing

  after stream ends:
    yield _parse_structured("".join(raw_parts))
      → json.loads full accumulated text
      → returns { response, conversation_status, identify, error_email }
```

---

## Step 6 — LLM client (`services/llm.py`)

```
stream_json(messages)
  → OpenAI chat.completions.create(
        model        = chat_model,
        temperature  = 0,
        response_format = {"type": "json_object"},
        stream       = True,
        messages     = messages,
    )
  → async for chunk in stream:
      delta = chunk.choices[0].delta.content
      if delta → yield delta          ← raw partial JSON text
```

---

## What each layer receives

| Layer | Receives |
|---|---|
| `llm.stream_json` | Raw partial JSON chunks, e.g. `{"response": "Xin `, `chào`, `anh/chị!"` |
| `rag.answer_stream` | Clean response text only, e.g. `"Xin "`, `"chào "`, `"anh/chị!"` |
| `conversation.stream` | `StreamEvent("token", "Xin ")`, `StreamEvent("token", "chào ")`, … then `StreamEvent("output", "{...}")` |
| `handler.py` | WebSocket `processing` event per token → frontend renders progressively |
| `worker.py` | Final `output` dict → sent to `bot-agent` topic as one Kafka message |

---

## Timing

```
t=0ms   Kafka message arrives
t~50ms  Embed + Qdrant search + rerank complete
t~600ms OpenAI starts yielding first token
t~600ms First "processing" WebSocket event fires (token visible in chat UI)
  ...   More tokens arrive every ~50-100ms
t~3s    Last token; "done" WebSocket event fires
t~3ms   "output" sent to bot-agent Kafka topic (full structured result)
```
