"""OpenAI-compatible gateway in front of the Kaggle backend (v2).

PocketPal -> (ngrok) -> this gateway -> [web search v2] -> Kaggle backend (LLM)

- Model id flags: ":web" (live search), ":think/:low/:medium/:high/:nothink" (thinking), ":tags" (<think> in content).
  See gateway/reasoning.py. Request fields reasoning_effort / thinking / enable_thinking are honoured too.
- Thinking is streamed in `delta.reasoning_content` (separate from the answer); search progress goes there too.
- Replies are streamed in small SSE chunks with keep-alives while the Kaggle model works.
- The Kaggle notebook registers its tunnel URL via POST /register (heartbeat).
"""
import os, json, time, asyncio, uuid
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import reasoning as R
from . import search as S

VERSION = "2.0"
KEY = os.environ.get("BRIDGE_KEY", "")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "google/gemini-2.5-flash")
REASONING_FORMAT = os.environ.get("REASONING_FORMAT", "field")  # field | tags
WEB_MODELS = [m for m in os.environ.get(
    "WEB_MODELS",
    "google/gemini-2.5-flash,google/gemini-2.5-pro,anthropic/claude-sonnet-5@default,"
    "openai/gpt-5.4-mini-2026-03-17,deepseek-ai/deepseek-r1-0528,qwen/qwen3-235b-a22b-instruct-2507",
).split(",") if m]
BACKEND_TIMEOUT = int(os.environ.get("BACKEND_TIMEOUT", "300"))

backend = {"url": os.environ.get("BACKEND_URL", "").rstrip("/"), "seen": 0.0}
app = FastAPI(title="kaggle-pocketpal-gateway", version=VERSION)


def check(authorization: Optional[str]):
    if KEY and authorization != f"Bearer {KEY}":
        raise HTTPException(status_code=401, detail="invalid api key")


class BackendError(Exception):
    pass


async def call_backend(model: str, msgs: list, params: Optional[dict] = None) -> str:
    """Raises BackendError when the Kaggle notebook is missing/unreachable."""
    if not backend["url"]:
        raise BackendError("The Kaggle notebook is not connected. Run the bridge cell in your notebook.")
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{backend['url']}/v1/chat/completions",
                json={"model": model, "messages": msgs, "stream": False, "params": params or {}},
                headers={"X-Backend-Key": KEY, "User-Agent": "gateway"}, timeout=BACKEND_TIMEOUT)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        raise BackendError(f"backend unreachable ({type(e).__name__}: {e}). Re-run the notebook cell.")


async def ask_text(model: str, msgs: list, params: Optional[dict] = None) -> str:
    """Like call_backend but turns failures into a readable reply."""
    try:
        return await call_backend(model, msgs, params)
    except BackendError as e:
        return f"[Bridge Error] {e}"


async def pipeline(base: str, msgs: list, web: bool, effort: Optional[str], params: dict, progress):
    """Search (optional) -> thinking instruction -> backend -> split. Returns (reasoning, answer, info)."""
    work, info = msgs, None
    if web:
        ctx, info = await S.build_context(msgs, ask=call_backend, progress=progress)
        work = [{"role": "system", "content": ctx}] + msgs
    work = R.with_instruction(work, R.thinking_instruction(effort))
    thinking_on = effort not in (None, "none")
    if web or thinking_on:  # plain chats stay silent: no thinking panel at all
        progress("Asking the model" + (f" (thinking: {effort})" if thinking_on else ""))
    text = await ask_text(base, work, params)
    reasoning, answer = R.split_reasoning(text)
    if effort == "none":
        reasoning = ""
    if not answer and reasoning:  # model put everything inside the tags
        answer, reasoning = reasoning, ""
    if web and info:
        answer += S.sources_footer(answer, info)
    return reasoning, answer, info


@app.get("/health")
def health():
    return {"ok": True, "version": VERSION, "backend_connected": bool(backend["url"]),
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
async def search(q: str, recency: str = "none", pages: int = 0, authorization: Optional[str] = Header(None)):
    """Debug endpoint: raw ranked results (pages=1 also reads the top pages)."""
    check(authorization)
    async with httpx.AsyncClient() as client:
        res = await S.search(client, [q], recency)
        if pages:
            txt = await asyncio.gather(*[S.page_text(client, r["url"]) for r in res[:S.FETCH_PAGES]])
            for r, t in zip(res, txt):
                r["excerpt"] = S.best_passages(t, q, S.PAGE_CHARS) if t else ""
    return {"query": q, "recency": recency, "results": res, "debug": S.DEBUG}


@app.get("/v1/controls")
async def controls(authorization: Optional[str] = Header(None)):
    check(authorization)
    return {"version": VERSION, "reasoning_format": REASONING_FORMAT,
            "model_flags": {"web": "live web search", "think": "thinking, medium", "low": "thinking, brief",
                            "medium": "thinking, step by step", "high": "thinking, deep", "nothink": "no thinking",
                            "tags": "send thinking inside <think> tags in content instead of reasoning_content"},
            "request_fields": ["reasoning_effort", "reasoning.effort", "thinking.type/budget_tokens",
                               "enable_thinking", "chat_template_kwargs.enable_thinking", "temperature", "top_p",
                               "max_tokens", "stop", "seed", "stream", "stream_options.include_usage"]}


@app.get("/v1/backend-info")
async def backend_info(authorization: Optional[str] = Header(None)):
    """What the Kaggle side supports (prompt() signature etc.); useful for the next agent."""
    check(authorization)
    if not backend["url"]:
        raise HTTPException(503, "backend not connected")
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{backend['url']}/v1/info", headers={"X-Backend-Key": KEY}, timeout=15)
        return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/v1/models")
async def models(authorization: Optional[str] = Header(None)):
    check(authorization)
    ids = []
    if backend["url"]:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{backend['url']}/v1/models", headers={"X-Backend-Key": KEY}, timeout=15)
                ids = [m["id"] for m in r.json().get("data", [])]
        except Exception:
            pass
    ok = [m for m in WEB_MODELS if not ids or m in ids]
    curated = []
    for m in ok:
        curated += [f"{m}:web", f"{m}:web:think"]
    curated += [f"{m}:think" for m in ok[:4]]
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in curated + ids]}


def _delta_chunk(cid, created, model, delta, finish=None):
    c = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return "data: " + json.dumps(c) + "\n\n"


async def stream_response(raw_model, base, msgs, web, effort, params, tags, include_usage):
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())
    q: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(pipeline(base, msgs, web, effort, params, lambda s: q.put_nowait(s)))
    state = {"open": False}

    def reason(text):  # one piece of thinking text, in the configured format
        if tags:
            pre = "" if state["open"] else "<think>\n"
            state["open"] = True
            return _delta_chunk(cid, created, raw_model, {"content": pre + text})
        return _delta_chunk(cid, created, raw_model, {"reasoning_content": text})

    yield _delta_chunk(cid, created, raw_model, {"role": "assistant", "content": ""})
    getter = None
    while True:
        if getter is None:
            getter = asyncio.ensure_future(q.get())
        done, _ = await asyncio.wait({getter, task}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            yield reason("- " + getter.result() + "\n")
            getter = None
            continue
        if task in done:
            break
        yield ": keepalive\n\n"
    if getter is not None and not getter.done():
        getter.cancel()
    while not q.empty():
        yield reason("- " + q.get_nowait() + "\n")
    try:
        thinking, answer, _ = task.result()
    except Exception as e:
        thinking, answer = "", f"[Bridge Error] {type(e).__name__}: {e}"
    if thinking:
        if state["open"] or not tags:
            yield reason("\n")
        for piece in R.chunks(thinking):
            yield reason(piece)
            await asyncio.sleep(0.004)
    if tags and state["open"]:
        yield _delta_chunk(cid, created, raw_model, {"content": "\n</think>\n\n"})
    for piece in R.chunks(answer):
        yield _delta_chunk(cid, created, raw_model, {"content": piece})
        await asyncio.sleep(0.004)
    yield _delta_chunk(cid, created, raw_model, {}, finish="stop")
    if include_usage:
        p = sum(R.approx_tokens(S.tt(m.get("content", ""))) for m in msgs)
        c = R.approx_tokens(answer + thinking)
        yield ("data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created,
                                      "model": raw_model, "choices": [],
                                      "usage": {"prompt_tokens": p, "completion_tokens": c,
                                                "total_tokens": p + c}}) + "\n\n")
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat(req: Request, authorization: Optional[str] = Header(None)):
    check(authorization)
    body = await req.json()
    raw_model = body.get("model") or DEFAULT_MODEL
    base, flags = R.parse_model(raw_model)
    web = "web" in flags
    tags = "tags" in flags or REASONING_FORMAT == "tags"
    effort = R.resolve_effort(body, flags)
    msgs = body.get("messages", [])
    params = R.sampling_params(body)
    if body.get("stream"):
        inc = bool((body.get("stream_options") or {}).get("include_usage"))
        return StreamingResponse(
            stream_response(raw_model, base, msgs, web, effort, params, tags, inc),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    thinking, answer, _ = await pipeline(base, msgs, web, effort, params, lambda s: None)
    msg = {"role": "assistant", "content": answer}
    if thinking:
        if tags:
            msg["content"] = f"<think>\n{thinking}\n</think>\n\n{answer}"
        else:
            msg["reasoning_content"] = thinking
    p = sum(R.approx_tokens(S.tt(m.get("content", ""))) for m in msgs)
    c = R.approx_tokens(answer + thinking)
    return JSONResponse({"id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion",
                         "created": int(time.time()), "model": raw_model,
                         "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}})
