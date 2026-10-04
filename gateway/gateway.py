"""OpenAI-compatible gateway with web search (SearXNG) in front of the Kaggle backend.

PocketPal -> (ngrok) -> this gateway -> [SearXNG search + page extraction] -> Kaggle backend (LLM)

- Model ids ending in ":web" get web search context injected; plain ids go straight through.
- The Kaggle notebook registers its tunnel URL via POST /register (heartbeat).
"""
import os, re, json, time, asyncio, datetime
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
import trafilatura
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

SEARX = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
KEY = os.environ.get("BRIDGE_KEY", "")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "google/gemini-2.5-flash")
WEB_MODELS = [m for m in os.environ.get(
    "WEB_MODELS",
    "google/gemini-2.5-flash,google/gemini-2.5-pro,anthropic/claude-sonnet-5@default,"
    "openai/gpt-5.4-mini-2026-03-17,deepseek-ai/deepseek-r1-0528,qwen/qwen3-235b-a22b-instruct-2507",
).split(",") if m]
TOP_RESULTS = int(os.environ.get("TOP_RESULTS", "8"))
FETCH_PAGES = int(os.environ.get("FETCH_PAGES", "3"))
PAGE_CHARS = int(os.environ.get("PAGE_CHARS", "1800"))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

backend = {"url": os.environ.get("BACKEND_URL", "").rstrip("/"), "seen": 0.0}
app = FastAPI(title="kaggle-pocketpal-gateway")


def check(authorization: Optional[str]):
    if KEY and authorization != f"Bearer {KEY}":
        raise HTTPException(status_code=401, detail="invalid api key")


def tt(c: Any) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return str(c)


def make_query(msgs: list) -> str:
    users = [tt(m.get("content", "")).strip() for m in msgs if m.get("role") == "user"]
    users = [u for u in users if u]
    if not users:
        return ""
    q = users[-1]
    # short follow-ups ("and in 2020?") need the previous question for context
    if len(q) < 40 and len(users) > 1:
        q = users[-2][-200:] + " " + q
    return re.sub(r"\s+", " ", q)[-300:]


LAST = {"unresponsive": [], "searx": 0, "ddgs": 0, "ddgs_error": ""}


def ddgs_search(q: str) -> list:
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        rows = DDGS().text(q, max_results=10) or []
        LAST["ddgs_error"] = ""
        return [{"title": x.get("title", ""), "url": x.get("href") or x.get("url", ""),
                 "content": x.get("body", ""), "engines": ["ddgs"]} for x in rows]
    except Exception as e:
        LAST["ddgs_error"] = f"{type(e).__name__}: {e}"[:200]
        return []


async def searx(client: httpx.AsyncClient, q: str) -> list:
    raw = []
    try:
        r = await client.get(f"{SEARX}/search", params={
            "q": q, "format": "json", "language": "auto", "categories": "general,news"}, timeout=20)
        r.raise_for_status()
        j = r.json()
        raw = j.get("results", [])
        LAST["unresponsive"] = j.get("unresponsive_engines", [])
    except Exception as e:
        LAST["unresponsive"] = [["searxng", str(e)[:100]]]
    web = await asyncio.to_thread(ddgs_search, q)
    LAST["searx"], LAST["ddgs"] = len(raw), len(web)

    def is_news(x):
        return bool(x.get("engines")) and all("news" in e for e in x["engines"])

    raw = web + [x for x in raw if not is_news(x)] + [x for x in raw if is_news(x)]
    seen, out = set(), []
    for x in raw:
        u = x.get("url", "")
        p = urlparse(u)
        k = (p.netloc.lower().removeprefix("www."), p.path.rstrip("/"))
        if not u or k in seen:
            continue
        seen.add(k)
        out.append({"title": x.get("title", ""), "url": u,
                    "snippet": (x.get("content") or "").strip(),
                    "engines": x.get("engines", [])})
        if len(out) >= TOP_RESULTS:
            break
    return out


async def extract(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, headers={"User-Agent": UA}, timeout=8, follow_redirects=True)
        if "text/html" not in r.headers.get("content-type", "text/html"):
            return ""
        text = await asyncio.to_thread(trafilatura.extract, r.text, include_comments=False,
                                       include_tables=False)
        return (text or "")[:PAGE_CHARS]
    except Exception:
        return ""


async def web_context(msgs: list) -> str:
    q = make_query(msgs)
    today = datetime.datetime.utcnow().strftime("%A, %d %B %Y")
    if not q:
        return f"Current date: {today}."
    async with httpx.AsyncClient() as client:
        try:
            results = await searx(client, q)
        except Exception as e:
            return f"Current date: {today}. Web search failed ({e}); answer from your own knowledge and say it may be outdated."
        pages = await asyncio.gather(*[extract(client, r["url"]) for r in results[:FETCH_PAGES]])
    lines = [f"Current date: {today} (UTC). Live web search results for: {q}", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']} - {r['url']}")
        if r["snippet"]:
            lines.append(f"    {r['snippet']}")
        if i <= len(pages) and pages[i - 1]:
            lines.append(f"    Page excerpt: {pages[i - 1]}")
        lines.append("")
    lines.append("Use these results to answer with up-to-date facts. Cite sources like [1]. "
                 "If the results are irrelevant or empty, say so instead of guessing.")
    return "\n".join(lines)


async def ask_backend(model: str, msgs: list) -> str:
    if not backend["url"]:
        return "[Bridge] The Kaggle notebook is not connected. Run the bridge cell in your notebook."
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{backend['url']}/v1/chat/completions",
                json={"model": model, "messages": msgs, "stream": False},
                headers={"X-Backend-Key": KEY, "User-Agent": "gateway"}, timeout=300)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[Bridge Error] backend unreachable ({type(e).__name__}: {e}). Re-run the notebook cell."


@app.get("/health")
def health():
    return {"ok": True, "backend_connected": bool(backend["url"]),
            "backend_last_seen_s": round(time.time() - backend["seen"]) if backend["seen"] else None}


@app.post("/register")
async def register(req: Request, authorization: Optional[str] = Header(None)):
    check(authorization)
    url = (await req.json()).get("url", "").rstrip("/")
    if not url.startswith("https://"):
        raise HTTPException(400, "url must be https")
    backend.update(url=url, seen=time.time())
    return {"ok": True}


@app.get("/search")
async def search(q: str, authorization: Optional[str] = Header(None)):
    check(authorization)
    async with httpx.AsyncClient() as client:
        res = await searx(client, q)
    return {"query": q, "results": res, "debug": LAST}


@app.get("/v1/models")
async def models(authorization: Optional[str] = Header(None)):
    check(authorization)
    ids = []
    if backend["url"]:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{backend['url']}/v1/models",
                                     headers={"X-Backend-Key": KEY}, timeout=15)
                ids = [m["id"] for m in r.json().get("data", [])]
        except Exception:
            pass
    web = [f"{m}:web" for m in WEB_MODELS if not ids or m in ids]
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in web + ids]}


@app.post("/v1/chat/completions")
async def chat(req: Request, authorization: Optional[str] = Header(None)):
    check(authorization)
    body = await req.json()
    model = body.get("model") or DEFAULT_MODEL
    web = model.endswith(":web")
    base = model[:-4] if web else model
    msgs = body.get("messages", [])
    if web:
        msgs = [{"role": "system", "content": await web_context(msgs)}] + msgs
    text = await ask_backend(base, msgs)
    ts = int(time.time())
    if body.get("stream"):
        def gen():
            a = {"id": "c1", "object": "chat.completion.chunk", "created": ts, "model": model,
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}
            b = {"id": "c1", "object": "chat.completion.chunk", "created": ts, "model": model,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield "data: " + json.dumps(a) + "\n\n"
            yield "data: " + json.dumps(b) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    return JSONResponse({"id": "c1", "object": "chat.completion", "created": ts, "model": model,
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                      "finish_reason": "stop"}]})
