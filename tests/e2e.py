"""CI end-to-end checks against a running gateway (:8080, BACKEND_URL=:8000, BRIDGE_KEY set) and a fake backend.
Covers: auth, models list, search v2, :web injection + LLM planner + sources, thinking (field + tags), nothink,
streaming (SSE, reasoning_content separate from content, usage), sampling params passthrough, controls/backend-info."""
import json, os, subprocess, sys, time
import httpx

GP, BP = os.environ.get("GATEWAY_PORT", "8080"), os.environ.get("BACKEND_PORT", "8000")
G = f"http://127.0.0.1:{GP}"
H = {"Authorization": "Bearer " + os.environ["BRIDGE_KEY"]}
backend = subprocess.Popen([sys.executable, "-m", "uvicorn", "tests.fake_backend:app",
                            "--port", BP, "--log-level", "warning"])
time.sleep(3)
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name, str(detail)[:300])
    fails += 0 if ok else 1


def chat(model, text, **extra):
    body = {"model": model, "messages": [{"role": "user", "content": text}], **extra}
    return httpx.post(G + "/v1/chat/completions", json=body, headers=H, timeout=180).json()["choices"][0]["message"]


def stream(model, text, **extra):
    body = {"model": model, "stream": True, "messages": [{"role": "user", "content": text}], **extra}
    reasoning, content, usage, done = "", "", None, False
    with httpx.stream("POST", G + "/v1/chat/completions", json=body, headers=H, timeout=180) as r:
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            if line == "data: [DONE]":
                done = True
                continue
            j = json.loads(line[6:])
            if j.get("usage"):
                usage = j["usage"]
            for c in j.get("choices", []):
                d = c.get("delta", {})
                reasoning += d.get("reasoning_content", "") or ""
                content += d.get("content", "") or ""
    return reasoning, content, usage, done


try:
    check("bad key rejected", httpx.get(G + "/v1/models", headers={"Authorization": "Bearer nope"}).status_code == 401)
    h = httpx.get(G + "/health").json()
    check("health v2 + backend connected", h.get("version") == "2.0" and h.get("backend_connected"), h)

    r = httpx.get(G + "/search", params={"q": "python programming language", "pages": 1}, headers=H, timeout=60).json()
    print("DEBUG", r.get("debug"))
    res = r.get("results", [])
    check("raw search returns results", len(res) > 0, [x["url"] for x in res[:3]])
    check("pages read with excerpts", any(x.get("excerpt") for x in res), len([x for x in res if x.get("excerpt")]))
    check("results are de-duplicated", len({x["url"] for x in res}) == len(res))

    ids = [m["id"] for m in httpx.get(G + "/v1/models", headers=H, timeout=30).json()["data"]]
    check("models include :web and :web:think", any(i.endswith(":web") for i in ids)
          and any(i.endswith(":web:think") for i in ids), ids[:4])
    ctl = httpx.get(G + "/v1/controls", headers=H).json()
    check("controls endpoint", "model_flags" in ctl)
    bi = httpx.get(G + "/v1/backend-info", headers=H).json()
    check("backend-info proxied", "prompt_signature" in bi, bi)

    m = chat("google/gemini-2.5-flash:web", "what is the latest stable python version")
    t = m["content"]
    check(":web injects search results + LLM planner queries", "live web access" in t
          and "python latest stable release" in t and "[1]" in t, t[:200])
    check(":web appends Sources footer", "**Sources**" in t)

    m = chat("google/gemini-2.5-flash", "hi")
    check("plain chat skips search", "web access" not in m["content"] and "reasoning_content" not in m, m["content"][:80])

    m = chat("google/gemini-2.5-flash:think", "why is the sky blue")
    check("think -> reasoning_content separate", m.get("reasoning_content") == "fake reasoning about the question"
          and "<think>" not in m["content"] and "ECHO" in m["content"], m)
    m = chat("google/gemini-2.5-flash:think:tags", "why is the sky blue")
    check("tags -> <think> inside content", m["content"].startswith("<think>\nfake reasoning") and "reasoning_content" not in m)
    m = chat("google/gemini-2.5-flash", "why is the sky blue", reasoning_effort="high")
    check("reasoning_effort request field honoured", m.get("reasoning_content") and "Think deeply" in m["content"], m)
    m = chat("google/gemini-2.5-flash:nothink", "why is the sky blue")
    check("nothink -> no thinking instruction", "<think> and </think>" not in m["content"] and "reasoning_content" not in m)

    chat("google/gemini-2.5-flash", "hi", temperature=0.3, max_completion_tokens=77, top_p=0.9)
    seen = httpx.get(f"http://127.0.0.1:{BP}/seen").json()["params"]
    check("sampling params reach backend", seen == {"temperature": 0.3, "top_p": 0.9, "max_tokens": 77}, seen)

    rs, ct, us, done = stream("google/gemini-2.5-flash:web:think", "latest python release",
                              stream_options={"include_usage": True})
    check("stream: [DONE] + usage", done and us and us["total_tokens"] > 0, us)
    check("stream: thinking separate from answer", "fake reasoning" in rs and "Searching" in rs
          and "<think>" not in ct and "ECHO" in ct, rs[:200])
    rs, ct, us, done = stream("google/gemini-2.5-flash", "hi")
    check("stream plain: answer only", done and rs == "" and "ECHO" in ct)
    rs, ct, us, done = stream("google/gemini-2.5-flash:think:tags", "hi")
    check("stream tags: <think> block in content", ct.startswith("<think>\n") and "</think>" in ct and rs == "", ct[:120])
finally:
    backend.terminate()
sys.exit(1 if fails else 0)
