"""OpenAI-compatible gateway in front of the Kaggle backend (v2).

PocketPal -> (ngrok) -> this gateway -> [web search v2] -> Kaggle backend (LLM)

- Model id flags: ":shell" (model may run commands on the Render server, see shell_agent.py), ":web" (live search), ":think/:low/:medium/:high/:nothink" (thinking), ":tags" (<think> in content).
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
from . import shell_agent as SH

VERSION = "3.1"
KEY = os.environ.get("BRIDGE_KEY", "")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "google/gemini-2.5-flash")
REASONING_FORMAT = os.environ.get("REASONING_FORMAT", "tags")  # field | tags
WEB_MODELS = [m for m in os.environ.get(
    "WEB_MODELS",
    "google/gemini-2.5-flash,google/gemini-2.5-pro,anthropic/claude-sonnet-5@default,"
    "openai/gpt-5.4-mini-2026-03-17,deepseek-ai/deepseek-r1-0528,qwen/qwen3-235b-a22b-instruct-2507",
).split(",") if m]
BACKEND_TIMEOUT = int(os.environ.get("BACKEND_TIMEOUT", "300"))

backend = {"url": os.environ.get("BACKEND_URL", "").rstrip("/"), "seen": 0.0, "caps": None}
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


async def get_caps() -> list:
    """Backend capabilities ('stream', 'native_reasoning'); learned from /register or /v1/info (old notebooks: [])."""
    if backend["caps"] is None and backend["url"]:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{backend['url']}/v1/info", headers={"X-Backend-Key": KEY}, timeout=15)
                backend["caps"] = list(r.json().get("caps") or [])
        except Exception:
            return []
    return backend["caps"] or []


async def call_full(model: str, msgs: list, params: Optional[dict], effort: Optional[str]):
    """Non-streaming call that also returns the model's separate thoughts: (text, thoughts)."""
    if not backend["url"]:
        raise BackendError("The Kaggle notebook is not connected. Run the bridge cell in your notebook.")
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{backend['url']}/v1/chat/completions",
                json={"model": model, "messages": msgs, "stream": False, "params": params or {},
                      "reasoning": effort},
                headers={"X-Backend-Key": KEY, "User-Agent": "gateway"}, timeout=BACKEND_TIMEOUT)
            r.raise_for_status()
            m = r.json()["choices"][0]["message"]
            return m["content"], m.get("reasoning_content") or ""
    except Exception as e:
        raise BackendError(f"backend unreachable ({type(e).__name__}: {e}). Re-run the notebook cell.")


async def backend_stream(model: str, msgs: list, params: Optional[dict], effort: Optional[str]):
    """Real token stream from the Kaggle bridge. Yields ('delta'|'reasoning'|'error', text)."""
    async with httpx.AsyncClient() as client:
        async with client.stream(
                "POST", f"{backend['url']}/v1/chat/completions",
                json={"model": model, "messages": msgs, "stream": True, "params": params or {},
                      "reasoning": effort},
                headers={"X-Backend-Key": KEY, "User-Agent": "gateway"},
                timeout=httpx.Timeout(BACKEND_TIMEOUT, connect=15)) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                j = json.loads(line[6:])
                if j.get("done"):
                    return
                for k in ("delta", "reasoning", "error"):
                    if j.get(k):
                        yield k, j[k]


THINK_MODE = os.environ.get("THINK_MODE", "native")  # native: model's own thinking | prompt: <think> instruction


async def use_native() -> bool:
    return THINK_MODE == "native" and "native_reasoning" in await get_caps()


async def prepare(msgs: list, web: bool, effort: Optional[str], native: bool, progress):
    """Search (optional) + thinking instruction (only when not native). Returns (messages, search info)."""
    work, info = msgs, None
    if web:
        ctx, info = await S.build_context(msgs, ask=call_backend, progress=progress)
        work = [{"role": "system", "content": ctx}] + msgs
    if not native:
        work = R.with_instruction(work, R.thinking_instruction(effort))
    on = effort not in (None, "none")
    if web or on:
        progress("Asking the model" + (f" (thinking: {effort})" if on else ""))
    return work, info


async def pipeline(base: str, msgs: list, web: bool, effort: Optional[str], params: dict, progress):
    """Non-streaming: prepare -> backend -> split. Returns (reasoning, answer, info)."""
    native = await use_native()
    work, info = await prepare(msgs, web, effort, native, progress)
    try:
        text, thoughts = await call_full(base, work, params, effort if native else None)
    except BackendError as e:
        text, thoughts = f"[Bridge Error] {e}", ""
    tag_r, answer = R.split_reasoning(text)
    reasoning = "" if effort == "none" else (thoughts or tag_r)
    if not answer and reasoning:
        answer, reasoning = reasoning, ""
    if web and info:
        answer += S.sources_footer(answer, info)
    return reasoning, answer, info


async def _next(agen):
    try:
        return await agen.__anext__()
    except StopAsyncIteration:
        return None


@app.get("/health")
def health():
    return {"ok": True, "version": VERSION, "backend_connected": bool(backend["url"]), "backend_caps": backend["caps"],
            "backend_last_seen_s": round(time.time() - backend["seen"]) if backend["seen"] else None}


@app.post("/register")
async def register(req: Request, authorization: Optional[str] = Header(None)):
    check(authorization)
    url = (await req.json()).get("url", "").rstrip("/")
    if not url.startswith("https://"):
        raise HTTPException(400, "url must be https")
    body = await req.json()
    caps = body.get("caps")
    if url != backend["url"] or caps is not None:
        backend["caps"] = list(caps) if caps is not None else None
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
            "model_flags": {"web": "live web search", "shell": "model can run bash on the Render server (not with :web)", "think": "thinking, medium", "low": "thinking, brief",
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
    if SH.enabled():
        curated += [f"{m}:shell" for m in ok[:3]]
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in curated + ids]}


def _delta_chunk(cid, created, model, delta, finish=None):
    c = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return "data: " + json.dumps(c) + "\n\n"


async def shell_ask(base: str, params: dict, effort: Optional[str]):
    native = await use_native()

    async def ask(work: list) -> str:
        try:
            text, _ = await call_full(base, work, params, effort if native else None)
            return text
        except BackendError as e:
            return f"[Bridge Error] {e}"
    return ask


async def shell_stream(raw_model, base, msgs, effort, params, tags):
    """SSE for ':shell' models: each command + result goes into the thinking stream, the final answer is the content."""
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())
    q: asyncio.Queue = asyncio.Queue()
    ask = await shell_ask(base, params, effort)
    task = asyncio.create_task(SH.run_agent(ask, msgs, lambda s: q.put_nowait(s)))
    yield _delta_chunk(cid, created, raw_model, {"role": "assistant", "content": ""})
    opened, getter = False, None
    while True:
        if getter is None:
            getter = asyncio.ensure_future(q.get())
        done, _ = await asyncio.wait({getter, task}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            line = "- " + getter.result() + "\n"
            getter = None
            if tags:
                line, opened = ("" if opened else "<think>\n") + line, True
                yield _delta_chunk(cid, created, raw_model, {"content": line})
            else:
                yield _delta_chunk(cid, created, raw_model, {"reasoning_content": line})
            continue
        if task in done:
            break
        yield ": keepalive\n\n"
    if getter is not None and not getter.done():
        getter.cancel()
    while not q.empty():
        line = "- " + q.get_nowait() + "\n"
        key = "content" if tags else "reasoning_content"
        if tags and not opened:
            line, opened = "<think>\n" + line, True
        yield _delta_chunk(cid, created, raw_model, {key: line})
    try:
        answer = task.result()
    except Exception as e:
        answer = f"[Bridge Error] {type(e).__name__}: {e}"
    if tags and opened:
        answer = "\n</think>\n\n" + answer
    yield _delta_chunk(cid, created, raw_model, {"content": answer})
    yield _delta_chunk(cid, created, raw_model, {}, finish="stop")
    yield "data: [DONE]\n\n"


async def stream_response(raw_model, base, msgs, web, effort, params, tags, include_usage):
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())
    q: asyncio.Queue = asyncio.Queue()
    st = {"open": False, "answer": "", "mr": ""}

    def chunk(delta):
        return _delta_chunk(cid, created, raw_model, delta)

    def reason(text):
        if tags:
            pre = "" if st["open"] else "<think>\n"
            st["open"] = True
            return chunk({"content": pre + text})
        return chunk({"reasoning_content": text})

    def content(text):
        st["answer"] += text
        if tags and st["open"]:
            st["open"] = False
            text = "\n</think>\n\n" + text
        return chunk({"content": text})

    def emit(pairs):
        return [reason(t) if k == "r" else content(t) for k, t in pairs]

    yield chunk({"role": "assistant", "content": ""})
    native = await use_native()
    can_stream = "stream" in await get_caps()
    prep = asyncio.create_task(prepare(msgs, web, effort, native, lambda s: q.put_nowait(s)))
    getter = None
    while True:
        if getter is None:
            getter = asyncio.ensure_future(q.get())
        done, _ = await asyncio.wait({getter, prep}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            yield reason("- " + getter.result() + "\n")
            getter = None
            continue
        if prep in done:
            break
        yield ": keepalive\n\n"
    if getter is not None and not getter.done():
        getter.cancel()
    while not q.empty():
        yield reason("- " + q.get_nowait() + "\n")
    info = None
    try:
        work, info = prep.result()
    except Exception as e:
        work = None
        yield content(f"[Bridge Error] {type(e).__name__}: {e}")

    if work is not None:
        eff = effort if native else None
        sp = R.Splitter(enabled=effort != "none")
        got, streamed = False, False
        if can_stream:
            try:
                agen = backend_stream(base, work, params, eff)
                nxt = None
                while True:
                    if nxt is None:
                        nxt = asyncio.ensure_future(_next(agen))
                    done, _ = await asyncio.wait({nxt}, timeout=10)
                    if not done:
                        yield ": keepalive\n\n"
                        continue
                    item, nxt = nxt.result(), None
                    if item is None:
                        break
                    kind, txt = item
                    if kind == "error":
                        raise RuntimeError(txt)
                    got = True
                    if kind == "reasoning":
                        st["mr"] += txt
                        yield reason(txt)
                    else:
                        for k, t in sp.feed(txt):
                            st["mr"] += t if k == "r" else ""
                            yield reason(t) if k == "r" else content(t)
                for k, t in sp.finish():
                    st["mr"] += t if k == "r" else ""
                    yield reason(t) if k == "r" else content(t)
                streamed = True
            except Exception as e:
                if got:
                    yield content(f"\n\n[Bridge Error] {type(e).__name__}: {e}")
                    streamed = True
        if not streamed:
            try:
                text, thoughts = await call_full(base, work, params, eff)
            except BackendError as e:
                text, thoughts = f"[Bridge Error] {e}", ""
            tag_r, answer = R.split_reasoning(text)
            th = "" if effort == "none" else (thoughts or tag_r)
            if th:
                st["mr"] += th
                for piece in R.chunks(th):
                    yield reason(piece)
                    await asyncio.sleep(0.004)
            for piece in R.chunks(answer):
                yield content(piece)
                await asyncio.sleep(0.004)
        if not st["answer"].strip() and st["mr"].strip():
            yield content(st["mr"])  # model put everything inside the tags
        if web and info:
            foot = S.sources_footer(st["answer"], info)
            if foot:
                yield content(foot)
    if tags and st["open"]:
        st["open"] = False
        yield chunk({"content": "\n</think>\n\n"})
    yield _delta_chunk(cid, created, raw_model, {}, finish="stop")
    if include_usage:
        p = sum(R.approx_tokens(S.tt(m.get("content", ""))) for m in msgs)
        c = R.approx_tokens(st["answer"] + st["mr"])
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
    tags = ("tags" in flags or REASONING_FORMAT == "tags") and "field" not in flags
    effort = R.resolve_effort(body, flags)
    msgs = body.get("messages", [])
    params = R.sampling_params(body)
    if "shell" in flags:
        if web:
            return JSONResponse({"error": {"message": "':shell' cannot be combined with ':web' (web pages could inject commands)."}},
                                status_code=400)
        if body.get("stream"):
            return StreamingResponse(shell_stream(raw_model, base, msgs, effort, params, tags),
                                     media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        lines: list = []
        answer = await SH.run_agent(await shell_ask(base, params, effort), msgs, lines.append)
        content = "<think>\n" + "\n".join("- " + x for x in lines) + "\n</think>\n\n" + answer if (tags and lines) else answer
        msg = {"role": "assistant", "content": content}
        if lines and not tags:
            msg["reasoning_content"] = "\n".join("- " + x for x in lines)
        return JSONResponse({"id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion",
                             "created": int(time.time()), "model": raw_model,
                             "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
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
