# Kaggle backend cell: runs the LLM server and registers itself with the GitHub-Actions gateway.
# Kaggle Secrets needed (Add-ons -> Secrets): BRIDGE_KEY   (same value as the GitHub secret BRIDGE_KEY)
# STOP any old cell that used ngrok before running this one (the gateway owns the ngrok domain now).
import os, re, json, time, asyncio, threading, subprocess, sys, stat, urllib.request
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "fastapi", "uvicorn", "requests"], check=True)
import uvicorn, requests
from typing import Any, List, Optional
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import kaggle_benchmarks as kbench

GATEWAY = os.environ.get("GATEWAY_URL", "https://scrubbed-calcium-subscript.ngrok-free.dev")
DEFAULT = "google/gemini-2.5-flash"

def get_key():
    k = os.environ.get("BRIDGE_KEY")
    if k:
        return k
    from kaggle_secrets import UserSecretsClient
    return UserSecretsClient().get_secret("BRIDGE_KEY")

KEY = get_key()
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

def auth(k):
    if k != KEY:
        raise HTTPException(401, "bad key")

@app.get("/v1/models")
def models(x_backend_key: Optional[str] = Header(None)):
    auth(x_backend_key)
    return {"object": "list",
            "data": [{"id": m, "object": "model"} for m in sorted(kbench.llms.keys())]}

@app.post("/v1/chat/completions")
def chat(p: P, x_backend_key: Optional[str] = Header(None)):
    auth(x_backend_key)
    model = p.model if p.model in kbench.llms else DEFAULT
    prompt = "\n".join(f"{m.role}: {tt(m.content)}" for m in p.messages)
    try:
        r = kbench.llms[model].prompt(prompt)
        text = r if isinstance(r, str) else getattr(r, "text", str(r))
    except Exception as e:
        text = f"[Proxy Error] {model}: {e}"
    return {"id": "c1", "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}

def serve():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=8000, log_level="warning")).serve())

threading.Thread(target=serve, daemon=True).start()
time.sleep(3)

# --- free Cloudflare quick tunnel (no account needed) ---
if not os.path.exists("cloudflared"):
    urllib.request.urlretrieve(
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
        "cloudflared")
    os.chmod("cloudflared", os.stat("cloudflared").st_mode | stat.S_IEXEC)
proc = subprocess.Popen(["./cloudflared", "tunnel", "--url", "http://127.0.0.1:8000", "--no-autoupdate"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
tunnel = None
for line in proc.stdout:
    m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
    if m:
        tunnel = m.group(0)
        break
assert tunnel, "cloudflared did not give a URL"
threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()

def heartbeat():
    while True:
        try:
            requests.post(GATEWAY + "/register", json={"url": tunnel}, timeout=10,
                          headers={"Authorization": f"Bearer {KEY}", "ngrok-skip-browser-warning": "1"})
        except Exception as e:
            print("register failed:", e)
        time.sleep(15)

threading.Thread(target=heartbeat, daemon=True).start()
print("=" * 50)
print("Backend tunnel:", tunnel)
print("PocketPal Server URL (no /v1):", GATEWAY)
print("PocketPal API key: your BRIDGE_KEY")
print("Models ending in :web use live web search")
print("=" * 50)
