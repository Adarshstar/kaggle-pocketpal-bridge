# Kaggle backend cell: runs the LLM server and registers itself with the GitHub-Actions gateway.
# Kaggle Secrets needed (Add-ons -> Secrets): BRIDGE_KEY   (same value as the GitHub secret BRIDGE_KEY)
# STOP any old cell that used ngrok before running this one (the gateway owns the ngrok domain now).
import os, re, json, time, asyncio, threading, subprocess, sys, stat, urllib.request, inspect
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "fastapi", "uvicorn", "requests"], check=True)
import uvicorn, requests
from typing import Any, List, Optional
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
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
    params: Optional[dict] = None
    reasoning: Optional[str] = None  # sampling params from the gateway (temperature, max_tokens, ...)

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

def result_text(r):
    if isinstance(r, str):
        return r
    return str(getattr(r, "text", None) or getattr(r, "content", None) or r)

CAPS = ["stream", "native_reasoning"]
LEVELS = ("none", "low", "medium", "high")
EXTRA_OK = ("max_tokens", "top_p", "stop", "presence_penalty", "frequency_penalty")

def norm_effort(e):
    if not e:
        return None
    e = str(e).lower()
    e = "low" if e == "minimal" else e
    return e if e in LEVELS else None

def split_msgs(msgs):
    system = "\n\n".join(tt(m.content) for m in msgs if m.role == "system") or None
    rest = [m for m in msgs if m.role != "system"]
    return system, rest

def flat_prompt(rest):
    if len(rest) == 1:
        return tt(rest[0].content)
    return "\n".join(f"{m.role}: {tt(m.content)}" for m in rest)

def split_params(params):
    p = dict(params or {})
    seed = int(p.pop("seed", 0) or 0)
    temp = p.pop("temperature", None)
    extra = {k: v for k, v in p.items() if k in EXTRA_OK}
    return seed, temp, extra

def ladder(effort, extra):
    """Attempts from richest to plainest; later ones run only if an earlier call raised."""
    out = [(effort, extra)]
    if extra:
        out.append((effort, {}))
    if effort:
        out.append((None, {}))
    return out

def run_blocking(model, p):
    llm = kbench.llms[model]
    system, rest = split_msgs(p.messages)
    seed, temp, extra = split_params(p.params)
    effort = norm_effort(p.reasoning)
    err = None
    for eff, ex in ladder(effort, extra):
        try:
            kw = {"seed": seed}
            if temp is not None:
                kw["temperature"] = temp
            if eff:
                kw["reasoning"] = eff
            if ex:
                kw["extra_api_params"] = ex
            with kbench.chats.new("req", system_instructions=system, orphan=True):
                r = llm.prompt(flat_prompt(rest), **kw)
                return result_text(r), kbench.chats.last_reasoning_traces()
        except Exception as e:
            err = e
    raise err

def sse(obj):
    return "data: " + json.dumps(obj) + "\n\n"

def stream_worker(model, p, q):
    """Runs in a thread: pushes ('d', text) / ('r', thoughts) / ('e', msg) / ('x', None) into q."""
    import copy
    from kaggle_benchmarks import actors as kactors, messages as kmsgs
    try:
        llm0 = kbench.llms[model]
        llm = copy.copy(llm0)
        llm.stream_responses = True
        system, rest = split_msgs(p.messages)
        seed, temp, extra = split_params(p.params)
        effort = norm_effort(p.reasoning)
        msgs = [kmsgs.Message(sender=(kactors.user if m.role == "user" else llm0), content=tt(m.content))
                for m in rest]
        err, got = None, False
        for eff, ex in ladder(effort, extra):
            try:
                kw = {"seed": seed, **ex}
                if temp is not None and getattr(llm, "support_temperature", False):
                    kw["temperature"] = temp
                resp = llm.invoke(msgs, system=system, reasoning=eff, **kw)
                if hasattr(resp, "__next__") or (hasattr(resp, "__iter__") and not hasattr(resp, "content")):
                    for ch in resp:
                        c = getattr(ch, "content", ch if isinstance(ch, str) else "") or ""
                        if c:
                            got = True
                            q.put(("d", c))
                else:
                    t = getattr(resp, "reasoning_traces", None)
                    if t:
                        q.put(("r", t))
                    c = getattr(resp, "content", "") or ""
                    got = bool(c)
                    q.put(("d", c))
                err = None
                break
            except Exception as e:
                err = e
                if got:
                    break
        if err:
            q.put(("e", f"{type(err).__name__}: {err}"))
    except Exception as e:
        q.put(("e", f"{type(e).__name__}: {e}"))
    finally:
        q.put(("x", None))

def stream_gen(model, p):
    import queue
    q = queue.Queue()
    threading.Thread(target=stream_worker, args=(model, p, q), daemon=True).start()
    while True:
        try:
            kind, val = q.get(timeout=10)
        except queue.Empty:
            yield ": keepalive\n\n"
            continue
        if kind == "x":
            yield sse({"done": True})
            return
        yield sse({{"d": "delta", "r": "reasoning", "e": "error"}[kind]: val})

@app.get("/v1/info")
def info(x_backend_key: Optional[str] = Header(None)):
    auth(x_backend_key)
    llm = kbench.llms[DEFAULT]
    try:
        sig = str(inspect.signature(llm.prompt))
    except Exception as e:
        sig = f"unavailable: {e}"
    return {"version": "3.0", "caps": CAPS, "models": len(kbench.llms), "prompt_signature": sig,
            "llm_type": type(llm).__name__,
            "llm_attrs": [a for a in dir(llm) if not a.startswith("_")][:60]}

@app.post("/v1/chat/completions")
def chat(p: P, x_backend_key: Optional[str] = Header(None)):
    auth(x_backend_key)
    model = p.model if p.model in kbench.llms else DEFAULT
    if p.stream:
        return StreamingResponse(stream_gen(model, p), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    thoughts = None
    try:
        text, thoughts = run_blocking(model, p)
    except Exception as e:
        text = f"[Proxy Error] {model}: {e}"
    msg = {"role": "assistant", "content": text}
    if thoughts:
        msg["reasoning_content"] = thoughts
    return {"id": "c1", "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]}

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
            requests.post(GATEWAY + "/register", json={"url": tunnel, "caps": CAPS, "v": "3.0"}, timeout=10,
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
