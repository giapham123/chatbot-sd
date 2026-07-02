"""FastAPI app — SSE streaming endpoint for the SD chatbot.

    POST /chat   { "session_id": "...", "message": "..." }  -> text/event-stream

SSE frame format:  event: <type>\n data: <json>\n\n
Event types: message | token | message_end | handoff | end | error | done
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from .config import Settings
from .container import Container, build_container
from .orchestration.conversation import StreamEvent

_container: Container | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _container
    _container = await build_container(Settings.load())
    yield
    await _container.close()


app = FastAPI(title="Chatbot SD", lifespan=lifespan)


class HistoryTurn(BaseModel):
    role: str   # "user" | "bot"
    text: str


class ChatRequest(BaseModel):
    session_id: str
    message: str
    history: list[HistoryTurn] = []  # prior turns from the client's localStorage


def _sse(event: StreamEvent) -> str:
    return f"event: {event.event}\ndata: {json.dumps({'text': event.data}, ensure_ascii=False)}\n\n"


@app.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    assert _container is not None

    history = [(t.role, t.text) for t in req.history]

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in _container.conversation.stream(
                req.session_id, req.message, history
            ):
                yield _sse(event)
        except Exception as exc:  # surface errors to the client, keep server alive
            yield _sse(StreamEvent("error", str(exc)))
        finally:
            yield "event: done\ndata: {}\n\n"

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
        || { sid: 'sess-' + Math.random().toString(36).slice(2), msgs: [] };
const sid = store.sid;
const log = document.getElementById('log');
let busy = false;
function save(){ localStorage.setItem(KEY, JSON.stringify(store)); }
function line(cls, t){const d=document.createElement('div');d.className=cls;d.textContent=t;log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}

// Restore previous conversation on load.
for(const m of store.msgs){ line(m.role==='user'?'u':'b', (m.role==='user'?'Bạn: ':'Bot: ')+m.text); }

async function send(){
  if(busy)return;                       // guard against double-submit
  const inp=document.getElementById('msg');const text=inp.value.trim();if(!text)return;inp.value='';
  busy=true;
  // History = prior turns (before this message), sent to the server for context.
  const history=store.msgs.filter(m=>m.role==='user'||m.role==='bot');
  line('u','Bạn: '+text);
  const botMsgs=[];let cur=null;let curText='';
  try{
  const res=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({session_id:sid,message:text,history})});
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
    }
  }
  }finally{
    // Persist this turn to localStorage.
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
    # Silence very chatty third-party debug logs (OpenAI request dumps, HTTP internals).
    for noisy in ("openai", "httpx", "httpcore", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="debug" if debug else "info")


if __name__ == "__main__":
    main()