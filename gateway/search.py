"""Web search v2: LLM query planning, multi-source search (ddgs text+news, SearXNG), reciprocal-rank fusion,
relevant-passage extraction from pages, caching, and direct reading of URLs the user pasted.

build_context() returns (system_context_text, info). `info` has queries/sources for the UI + debugging.
"""
import os, re, json, time, math, asyncio, datetime
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
import trafilatura

SEARX = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
TOP_RESULTS = int(os.environ.get("TOP_RESULTS", "8"))
FETCH_PAGES = int(os.environ.get("FETCH_PAGES", "5"))
PAGE_CHARS = int(os.environ.get("PAGE_CHARS", "2200"))
USER_URL_CHARS = int(os.environ.get("USER_URL_CHARS", "6000"))
SEARCH_REWRITE = os.environ.get("SEARCH_REWRITE", "1") != "0"
SEARCH_MODEL = os.environ.get("SEARCH_MODEL", "google/gemini-2.5-flash")
SEARCH_BUDGET_S = int(os.environ.get("SEARCH_BUDGET_S", "45"))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

DEBUG: dict = {"unresponsive": [], "searx": 0, "ddgs": 0, "ddgs_news": 0, "ddgs_error": "", "plan": "heuristic"}

STOP = set("""a an the and or of to in on at for from by with about as is are was were be been being it its this that
these those i you he she we they me my your our their what which who whom how why when where do does did can could should
would will shall may might must not no yes please tell give show find search look latest new current today now""".split())

LOW_QUALITY = ("pinterest.", "facebook.com", "instagram.com", "tiktok.com", "twitter.com", "x.com", "quora.com",
               "linkedin.com")
RECENCY_WORDS = {
    "day": ("today", "tonight", "right now", "this morning", "live score", "breaking"),
    "week": ("this week", "yesterday", "latest news", "news", "recent", "just released", "just announced"),
    "month": ("this month", "latest", "newest", "current", "new release", "update", "released"),
}
TIMELIMIT = {"day": "d", "week": "w", "month": "m"}


class TTLCache:
    def __init__(self, ttl: int, size: int):
        self.ttl, self.size, self.d = ttl, size, OrderedDict()

    def get(self, k):
        v = self.d.get(k)
        if not v or time.time() - v[0] > self.ttl:
            self.d.pop(k, None)
            return None
        return v[1]

    def put(self, k, val):
        self.d[k] = (time.time(), val)
        self.d.move_to_end(k)
        while len(self.d) > self.size:
            self.d.popitem(last=False)


# lxml/trafilatura crashed natively (free(): invalid pointer) when several pages were parsed in parallel threads,
# so all page parsing goes through ONE worker thread (parsing is fast; the slow part, fetching, stays concurrent).
EXTRACT_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="extract")
PAGE_CACHE = TTLCache(900, 80)
SEARCH_CACHE = TTLCache(300, 60)


def tt(c: Any) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return str(c)


def toks(s: str) -> list:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9.+#_-]*", (s or "").lower()) if len(w) > 1 and w not in STOP]


# ---------------------------------------------------------------- planning
def guess_recency(q: str) -> str:
    ql = q.lower()
    for level in ("day", "week", "month"):
        if any(w in ql for w in RECENCY_WORDS[level]):
            return level
    return "none"


def heuristic_plan(msgs: list) -> dict:
    users = [tt(m.get("content", "")).strip() for m in msgs if m.get("role") == "user"]
    users = [u for u in users if u]
    if not users:
        return {"search": False, "queries": [], "recency": "none"}
    q = users[-1]
    if len(q) < 40 and len(users) > 1:  # short follow-up needs the previous question
        q = users[-2][-200:] + " " + q
    q = re.sub(r"https?://\S+", " ", q)
    q = re.sub(r"\s+", " ", q).strip()[-300:]
    return {"search": bool(q), "queries": [q] if q else [], "recency": guess_recency(q)}


def _plan_prompt(msgs: list, today: str) -> str:
    convo = []
    for m in msgs[-8:]:
        if m.get("role") in ("user", "assistant"):
            convo.append(f"{m['role']}: {tt(m.get('content', ''))[:500]}")
    return (
        f"You plan web searches for a chat assistant. Today is {today}.\n"
        "Conversation (latest last):\n" + "\n".join(convo) + "\n\n"
        "Decide whether fresh information from the web is needed to answer the LAST user message. "
        'Reply with ONLY compact JSON: {"search": true|false, "queries": ["..."], "recency": "day|week|month|year|none"}\n'
        "Rules: 1-3 short keyword-style queries, each fully self-contained (resolve pronouns and follow-ups using "
        "earlier turns), different angles of the question. Use recency day/week/month only when the user wants "
        "the most recent info. search=false only for greetings, thanks, or tasks that need no facts "
        "(pure maths, rewriting text, coding with no external info)."
    )


def parse_plan(text: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
    except Exception:
        return None
    qs = [re.sub(r"\s+", " ", str(q)).strip()[:200] for q in (j.get("queries") or []) if str(q).strip()][:3]
    rec = str(j.get("recency", "none")).lower()
    return {"search": bool(j.get("search", True)) and bool(qs), "queries": qs,
            "recency": rec if rec in ("day", "week", "month", "year", "none") else "none"}


async def make_plan(msgs: list, ask, today: str) -> dict:
    """ask(model, msgs, params) -> str may raise; any failure falls back to the heuristic plan."""
    base = heuristic_plan(msgs)
    if not SEARCH_REWRITE or ask is None or not base["queries"]:
        DEBUG["plan"] = "heuristic"
        return base
    try:
        out = await asyncio.wait_for(
            ask(SEARCH_MODEL, [{"role": "user", "content": _plan_prompt(msgs, today)}], {"temperature": 0}), 30)
        plan = parse_plan(out)
        if plan:
            DEBUG["plan"] = "llm"
            if plan["search"] and plan["recency"] == "none":
                plan["recency"] = base["recency"]
            return plan
    except Exception as e:
        DEBUG["plan"] = f"heuristic ({type(e).__name__})"
        return base
    DEBUG["plan"] = "heuristic (bad plan)"
    return base


# ---------------------------------------------------------------- sources
def _ddgs(q: str, recency: str, news: bool) -> list:
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        kw = {"max_results": 8 if news else 10}
        if recency in TIMELIMIT:
            kw["timelimit"] = TIMELIMIT[recency]
        d = DDGS()
        rows = (d.news(q, **kw) if news else d.text(q, **kw)) or []
        DEBUG["ddgs_error"] = ""
        return [{"title": x.get("title", ""), "url": x.get("href") or x.get("url", ""),
                 "content": x.get("body", ""), "engines": ["ddgs-news" if news else "ddgs"],
                 "date": x.get("date", "")} for x in rows]
    except Exception as e:
        DEBUG["ddgs_error"] = f"{type(e).__name__}: {e}"[:200]
        return []


async def _searx(client: httpx.AsyncClient, q: str, recency: str) -> list:
    params = {"q": q, "format": "json", "language": "auto", "categories": "general"}
    if recency in ("day", "week", "month", "year"):
        params["time_range"] = recency
    try:
        r = await client.get(f"{SEARX}/search", params=params, timeout=20)
        r.raise_for_status()
        j = r.json()
        DEBUG["unresponsive"] = j.get("unresponsive_engines", [])
        return [{"title": x.get("title", ""), "url": x.get("url", ""), "content": x.get("content", ""),
                 "engines": x.get("engines", ["searxng"]), "date": x.get("publishedDate", "") or ""}
                for x in j.get("results", [])]
    except Exception as e:
        DEBUG["unresponsive"] = [["searxng", str(e)[:100]]]
        return []


def _key(u: str):
    p = urlparse(u)
    return (p.netloc.lower().removeprefix("www."), p.path.rstrip("/"))


def fuse(lists: list, limit: int) -> list:
    """Reciprocal-rank fusion over (weight, results) lists, de-duplicated by host+path."""
    agg = {}
    for weight, rows in lists:
        for rank, x in enumerate(rows):
            u = x.get("url", "")
            if not u.startswith("http"):
                continue
            e = agg.setdefault(_key(u), {"title": x.get("title", ""), "url": u, "snippet": "",
                                         "engines": set(), "date": "", "score": 0.0})
            e["score"] += weight / (60 + rank)
            e["engines"].update(x.get("engines", []))
            if len(x.get("content", "") or "") > len(e["snippet"]):
                e["snippet"] = (x.get("content") or "").strip()
            e["date"] = e["date"] or x.get("date", "")
    for e in agg.values():
        host = urlparse(e["url"]).netloc.lower()
        if any(b in host for b in LOW_QUALITY):
            e["score"] *= 0.5
        if len(e["engines"]) > 1:
            e["score"] *= 1.15
    out = sorted(agg.values(), key=lambda e: -e["score"])[:limit]
    for e in out:
        e["engines"] = sorted(e["engines"])
    return out


async def search_one(client: httpx.AsyncClient, q: str, recency: str) -> list:
    ck = (q, recency)
    hit = SEARCH_CACHE.get(ck)
    if hit is not None:
        return hit
    want_news = recency in ("day", "week", "month")
    jobs = [asyncio.to_thread(_ddgs, q, recency, False), _searx(client, q, recency)]
    if want_news:
        jobs.append(asyncio.to_thread(_ddgs, q, recency, True))
    res = await asyncio.gather(*jobs)
    web, sx = res[0], res[1]
    news = res[2] if want_news else []
    DEBUG["ddgs"], DEBUG["searx"], DEBUG["ddgs_news"] = len(web), len(sx), len(news)
    lists = [(1.0, web), (0.9, sx), (0.8, news)]
    if web or sx or news:  # never cache an empty (probably blocked) result
        SEARCH_CACHE.put(ck, lists)
    return lists


async def search(client: httpx.AsyncClient, queries: list, recency: str, limit: int = TOP_RESULTS) -> list:
    per = await asyncio.gather(*[search_one(client, q, recency) for q in queries])
    return fuse([x for p in per for x in p], limit)


# ---------------------------------------------------------------- pages
async def fetch_html(client: httpx.AsyncClient, url: str, cap: int = 700_000) -> str:
    async with client.stream("GET", url, headers={"User-Agent": UA, "Accept-Language": "en"},
                             timeout=8, follow_redirects=True) as r:
        ct = r.headers.get("content-type", "text/html")
        if r.status_code >= 400 or ("html" not in ct and "text" not in ct):
            return ""
        buf = b""
        async for c in r.aiter_bytes():
            buf += c
            if len(buf) > cap:
                break
        return buf.decode(r.encoding or "utf-8", errors="ignore")


async def page_text(client: httpx.AsyncClient, url: str) -> str:
    hit = PAGE_CACHE.get(url)
    if hit is not None:
        return hit
    try:
        html = await asyncio.wait_for(fetch_html(client, url), 12)
        text = await asyncio.get_running_loop().run_in_executor(
            EXTRACT_POOL, lambda: trafilatura.extract(html, include_comments=False, include_tables=True)) or ""
    except Exception:
        text = ""
    text = text[:40_000]
    if text:
        PAGE_CACHE.put(url, text)
    return text


def best_passages(text: str, query: str, budget: int) -> str:
    """Pick the paragraphs most relevant to the query (idf-weighted overlap), keep original order."""
    if len(text) <= budget:
        return text
    paras, buf = [], ""
    for line in re.split(r"\n+", text):
        line = line.strip()
        if not line:
            continue
        if len(buf) + len(line) < 450:
            buf = (buf + " " + line).strip()
        else:
            if buf:
                paras.append(buf)
            buf = line
    if buf:
        paras.append(buf)
    q = set(toks(query))
    if not q or not paras:
        return text[:budget]
    n = len(paras)
    df = {w: sum(1 for p in paras if w in p.lower()) for w in q}
    scored = []
    for i, p in enumerate(paras):
        pl = p.lower()
        s = sum(math.log(1 + n / (1 + df[w])) for w in q if w in pl) / (len(p) ** 0.25)
        if i == 0:
            s += 0.5  # the lede usually says what the page is
        scored.append((s, i))
    keep, used = set(), 0
    for s, i in sorted(scored, reverse=True):
        if used + len(paras[i]) > budget and keep:
            continue
        keep.add(i)
        used += len(paras[i])
        if used >= budget:
            break
    return " … ".join(paras[i][:budget] for i in sorted(keep))[:budget + 200]


# ---------------------------------------------------------------- context
async def build_context(msgs: list, ask=None, progress=None) -> tuple:
    """Returns (context_text, info). Never raises: on failure the context says the search failed."""
    say = progress or (lambda s: None)
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%A, %d %B %Y")
    info = {"queries": [], "sources": [], "pages": 0, "recency": "none", "searched": False, "user_urls": []}
    try:
        return await asyncio.wait_for(_build(msgs, ask, say, today, info), SEARCH_BUDGET_S)
    except Exception as e:
        say(f"Search problem ({type(e).__name__}); answering from own knowledge")
        return (f"Current date: {today} (UTC). Web search failed ({type(e).__name__}); answer from your own "
                "knowledge and say it may be outdated."), info


async def _build(msgs, ask, say, today, info):
    last_user = next((tt(m.get("content", "")) for m in reversed(msgs) if m.get("role") == "user"), "")
    urls = list(dict.fromkeys(re.findall(r"https?://[^\s<>()\"']+", last_user)))[:3]
    say("Planning search")
    plan = await make_plan(msgs, ask, today)
    info["recency"] = plan["recency"]
    lines = [f"Current date: {today} (UTC)."]
    async with httpx.AsyncClient() as client:
        user_pages = []
        if urls:
            say("Reading linked page" + ("s" if len(urls) > 1 else ""))
            texts = await asyncio.gather(*[page_text(client, u) for u in urls])
            for u, t in zip(urls, texts):
                if t:
                    user_pages.append((u, best_passages(t, last_user, USER_URL_CHARS)))
                    info["user_urls"].append(u)
        results = []
        if plan["search"]:
            info["queries"] = plan["queries"]
            info["searched"] = True
            rec = f" (recent: {plan['recency']})" if plan["recency"] != "none" else ""
            say("Searching: " + " | ".join(plan["queries"]) + rec)
            results = await search(client, plan["queries"], plan["recency"])
            say(f"Found {len(results)} results, reading the best pages")
            qtext = " ".join(plan["queries"])
            pages = await asyncio.gather(*[page_text(client, r["url"]) for r in results[:FETCH_PAGES]])
            for r, p in zip(results, pages):
                r["excerpt"] = best_passages(p, qtext, PAGE_CHARS) if p else ""
            info["pages"] = sum(1 for r in results if r.get("excerpt"))
            say(f"Read {info['pages']} pages")
        if results:
            lines.append("You have live web access. Search queries used: " + "; ".join(plan["queries"]) + ".")
            lines.append("")
            for i, r in enumerate(results, 1):
                d = f" ({r['date'][:10]})" if r.get("date") else ""
                lines.append(f"[{i}] {r['title']}{d} - {r['url']}")
                if r["snippet"]:
                    lines.append("    " + r["snippet"][:400])
                if r.get("excerpt"):
                    lines.append("    Relevant excerpt: " + r["excerpt"])
                lines.append("")
            info["sources"] = [{"n": i, "title": r["title"], "url": r["url"]} for i, r in enumerate(results, 1)]
        elif plan["search"]:
            lines.append("Web search returned no results; answer from your own knowledge and say it may be outdated.")
        for u, t in user_pages:
            lines += ["", f"Page the user linked ({u}):", t]
        if results:
            lines.append("Answer using these results. Cite sources inline like [1]. Prefer newer, more "
                         "authoritative sources, mention when sources disagree, and say so plainly if the results "
                         "do not answer the question instead of guessing.")
    return "\n".join(lines), info


def sources_footer(answer: str, info: dict, limit: int = 5) -> str:
    """'Sources' list of the results the answer cited (or the top ones if it cited none)."""
    src = info.get("sources") or []
    if not src:
        return ""
    cited = {int(n) for n in re.findall(r"\[(\d{1,2})\]", answer)}
    pick = [s for s in src if s["n"] in cited] or src[:3]
    return "\n\n**Sources**\n" + "\n".join(f"[{s['n']}] {s['title'] or s['url']} - {s['url']}"
                                          for s in pick[:limit])
