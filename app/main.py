"""FastAPI app — SSE streaming endpoint for the SD chatbot.

    POST /chat  (ChatSD schema)  ->  text/event-stream

SSE frame format:  event: <type>\n data: <json>\n\n
Event types:
  message      — scripted text ({"text": "..."})
  token        — streaming token ({"text": "..."})
  message_end  — signals end of token stream ({})
  handoff      — transferred to human SD ({"text": ""})
  end          — conversation ended ({"text": ""})
  output       — full OutputChatSD JSON (channel_id, answer, similarity, ...)
  error        — exception ({"text": "..."})
  done         — stream complete ({})
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Literal, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from .config import Settings
from .container import Container, build_container
from .kafka.router import consume_loop
from .orchestration.conversation import StreamEvent
from .services.langfuse_service import langfuse_service
from .services.minio_service import minio_service

_container: Container | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _container
    settings = Settings.load()
    _container = await build_container(settings)

    if _container.ws_client:
        try:
            await _container.ws_client.connect()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "WebSocket connect failed — running without WS: %s", exc
            )
            _container.ws_client = None

    kafka_task: asyncio.Task | None = None
    if _container.kafka_consumer and _container.kafka_producer:
        kafka_task = asyncio.create_task(
            consume_loop(
                _container.kafka_consumer,
                _container.kafka_producer,
                _container.conversation,
                settings.kafka_input_topic,
                settings.kafka_output_topic,
                ws_client=_container.ws_client,
            ),
            name="kafka-consume-loop",
        )

    yield

    if kafka_task is not None:
        kafka_task.cancel()
        await asyncio.gather(kafka_task, return_exceptions=True)
    if _container.ws_client:
        await _container.ws_client.disconnect()
    await _container.close()


app = FastAPI(title="Chatbot SD", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request / Response schemas  (mirrors ai-agent ChatSD / OutputChatSD)
# ---------------------------------------------------------------------------

class UserInfo(BaseModel):
    msnv: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None


class ChatSDRequest(BaseModel):
    mode: Literal["NORMAL", "ADVANCE"] = "NORMAL"
    first_session: Optional[bool] = None
    channel_id: Optional[str] = None
    agent_id: Optional[str] = None
    question: Optional[str] = None
    user: Optional[UserInfo] = None
    platform: Optional[Literal["WEB", "TEST", "ZALO", "GROUPWARE"]] = "WEB"
    chat_history: Optional[list[dict]] = None   # LangChain format: [{type, content}]
    tool_messages: Optional[list[dict]] = None
    recursion_count: Optional[int] = 0
    last_tool_name: Optional[str] = ""
    conversation_status: Literal[0, 1, 2, 3, 4] = 0
    error: Optional[dict] = None
    image_url: Optional[list[str]] = None


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _sse(event: StreamEvent) -> str:
    if event.event == "output":
        # data is already a JSON string (full OutputChatSD payload)
        return f"event: {event.event}\ndata: {event.data}\n\n"
    return f"event: {event.event}\ndata: {json.dumps({'text': event.data}, ensure_ascii=False)}\n\n"


def _lc_history_to_tuples(chat_history: list[dict] | None) -> list[tuple[str, str]]:
    """Convert LangChain message list to (role, text) tuples the graph expects."""
    result: list[tuple[str, str]] = []
    for msg in (chat_history or []):
        t = msg.get("type", "")
        content = msg.get("content", "")
        if t == "human":
            result.append(("user", content))
        elif t in ("ai", "assistant"):
            result.append(("bot", content))
    return result


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@app.post("/chat")
async def chat(req: ChatSDRequest) -> StreamingResponse:
    assert _container is not None

    history = _lc_history_to_tuples(req.chat_history)
    channel_id = req.channel_id or str(uuid.uuid4())

    platform = req.platform or "WEB"
    raw_keys = req.image_url or []
    image_detection = minio_service.get_image_urls(raw_keys, platform)
    image_b64: list[tuple[str, str]] = []
    for _key in raw_keys:
        _result = await minio_service.aget_image_b64(_key)
        if _result:
            image_b64.append(_result)

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in _container.conversation.stream(
                channel_id,
                req.question or "",
                history,
                agent_id=req.agent_id,
                platform=platform,
                conversation_status=req.conversation_status,
                error=req.error,        # error_email extracted inside stream()
                image_b64=image_b64,
            ):
                if event.event == "output" and image_detection:
                    output = json.loads(event.data)
                    output["image_detection"] = image_detection
                    event = StreamEvent("output", json.dumps(output, ensure_ascii=False))
                yield _sse(event)
        except Exception as exc:
            yield _sse(StreamEvent("error", str(exc)))
        finally:
            yield "event: done\ndata: {}\n\n"
            langfuse_service.flush()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _DEMO_HTML


_DEMO_HTML = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><title>Chatbot SD</title>
<style>
 body{font-family:sans-serif;max-width:640px;margin:2rem auto;padding:0 1rem}
 #log{border:1px solid #ccc;border-radius:8px;padding:1rem;height:420px;overflow:auto}
 .u{color:#0645ad}.b{color:#111}.sys{color:#888;font-style:italic}
 #row{display:flex;gap:.5rem;margin-top:.5rem}#msg{flex:1;padding:.5rem}
 button{padding:.5rem 1rem}
</style></head><body>
<h2>Chatbot SD (demo)</h2>
<div id="log"></div>
<div id="row"><input id="msg" placeholder="Nhập tin nhắn… vd: tôi muốn nâng cấp RAM"/>
<button onclick="send()">Gửi</button></div>
<script>
// Chat history lives in the browser (localStorage) — no server-side DB.
const KEY = 'chatbot_sd';
let store = JSON.parse(localStorage.getItem(KEY) || 'null')
        || { channel_id: 'ch-' + Math.random().toString(36).slice(2), msgs: [] };
const channel_id = store.channel_id;
const log = document.getElementById('log');
let busy = false;
function save(){ localStorage.setItem(KEY, JSON.stringify(store)); }
function line(cls, t){const d=document.createElement('div');d.className=cls;d.textContent=t;log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}

// Restore previous conversation on load.
for(const m of store.msgs){ line(m.role==='user'?'u':'b', (m.role==='user'?'Bạn: ':'Bot: ')+m.text); }

async function send(){
  if(busy)return;
  const inp=document.getElementById('msg');const text=inp.value.trim();if(!text)return;inp.value='';
  busy=true;
  // Build chat_history in LangChain format from stored messages.
  const chat_history=store.msgs.map(m=>({type:m.role==='user'?'human':'ai',content:m.text}));
  const conversation_status=store.msgs.length===0?0:1;
  line('u','Bạn: '+text);
  const botMsgs=[];let cur=null;let curText='';
  try{
  const res=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({channel_id,question:text,chat_history,platform:'WEB',conversation_status})});
  const reader=res.body.getReader();const dec=new TextDecoder();let buf='';
  while(true){const {value,done}=await reader.read();if(done)break;buf+=dec.decode(value,{stream:true});
    let parts=buf.split('\\n\\n');buf=parts.pop();
    for(const p of parts){
      const ev=(p.match(/event: (.*)/)||[])[1];const dm=p.match(/data: (.*)/);
      const data=dm?JSON.parse(dm[1]):{};
      if(ev==='message'){line('b','Bot: '+data.text);botMsgs.push(data.text);}
      else if(ev==='token'){if(!cur){cur=line('b','Bot: ');curText='';}cur.textContent+=data.text;curText+=data.text;}
      else if(ev==='message_end'){botMsgs.push(curText);cur=null;curText='';}
      else if(ev==='handoff'){line('sys','→ Đã chuyển cho nhân viên hỗ trợ (SD).');}
      else if(ev==='end'){line('sys','— Kết thúc cuộc trò chuyện —');}
      else if(ev==='error'){line('sys','Lỗi: '+data.text);}
      // 'output' event carries full OutputChatSD JSON — available for callers.
    }
  }
  }finally{
    store.msgs.push({role:'user',text});
    for(const b of botMsgs) store.msgs.push({role:'bot',text:b});
    save();
    busy=false;
  }
}
document.getElementById('msg').addEventListener('keydown',e=>{if(e.key==='Enter')send();});
</script></body></html>"""


def main() -> None:
    """Debug entry point.  Run with:  python -m app.main

    No --reload here so IDE breakpoints work (reload runs a subprocess the
    debugger can't attach to). For hot-reload dev use the CLI instead:
        uvicorn app.main:app --reload
    """
    import logging
    import os

    import uvicorn

    debug = os.getenv("DEBUG", "1") == "1"
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in (
        "openai", "httpx", "httpcore", "asyncio", "urllib3",
        "aiokafka", "websockets",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="debug" if debug else "info")


if __name__ == "__main__":
    main()
