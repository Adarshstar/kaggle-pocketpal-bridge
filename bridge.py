# Kaggle -> PocketPal bridge (run in a Kaggle Benchmarks notebook)
# Setup: add NGROK_TOKEN under Add-ons -> Secrets (or set the env var), then run this cell.
import os, json, time, asyncio, threading, subprocess, sys
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "fastapi", "uvicorn", "pyngrok"], check=True)
import uvicorn
from typing import Any, List, Optional
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pyngrok import ngrok
import kaggle_benchmarks as kbench

def get_token():
    t = os.environ.get("NGROK_TOKEN")
    if t:
        return t
    try:
        from kaggle_secrets import UserSecretsClient
        return UserSecretsClient().get_secret("NGROK_TOKEN")
    except Exception:
        raise RuntimeError("Set NGROK_TOKEN (Kaggle Secrets or env var)")

NGROK_TOKEN = get_token()
DEFAULT = "google/gemini-2.5-flash"
print("Models:", len(kbench.llms))

app = FastAPI()

class M(BaseModel):
    role: str
    content: Any

class P(BaseModel):
    model: Optional[str] = None
    messages: List[M]
    stream: Optional[bool] = False

def tt(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return str(c)

@app.get("/v1/models")
def models():
    return {"object": "list",
            "data": [{"id": m, "object": "model"} for m in sorted(kbench.llms.keys())]}

@app.post("/v1/chat/completions")
def chat(p: P):
    model = p.model if p.model in kbench.llms else DEFAULT
    prompt = "\n".join(f"{m.role}: {tt(m.content)}" for m in p.messages)
    try:
        r = kbench.llms[model].prompt(prompt)
        text = r if isinstance(r, str) else getattr(r, "text", str(r))
    except Exception as e:
        text = f"[Proxy Error] {model}: {e}"
    ts = int(time.time())
    if p.stream:
        def gen():
            a = {"id": "c1", "object": "chat.completion.chunk", "created": ts, "model": model,
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}
            b = {"id": "c1", "object": "chat.completion.chunk", "created": ts, "model": model,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield "data: " + json.dumps(a) + "\n\n"
            yield "data: " + json.dumps(b) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    return {"id": "c1", "object": "chat.completion", "created": ts, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}

def serve():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=8000, log_level="warning")).serve())

threading.Thread(target=serve, daemon=True).start()
time.sleep(3)

ngrok.kill()
ngrok.set_auth_token(NGROK_TOKEN)
t = ngrok.connect(8000, pooling_enabled=True)
print("=" * 50)
print("PocketPal Server URL (no /v1):")
print(t.public_url)
print("=" * 50)
