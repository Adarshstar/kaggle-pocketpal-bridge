"""CI check: SearXNG search works and ':web' models get search context.
Gateway must run on :8080 with BACKEND_URL=http://127.0.0.1:8000 and BRIDGE_KEY set."""
import os, subprocess, sys, time
import httpx

G = "http://127.0.0.1:8080"
H = {"Authorization": "Bearer " + os.environ["BRIDGE_KEY"]}
backend = subprocess.Popen([sys.executable, "-m", "uvicorn", "tests.fake_backend:app",
                            "--port", "8000", "--log-level", "warning"])
time.sleep(3)
fails = 0

def check(name, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name, detail)
    fails += 0 if ok else 1

try:
    r = httpx.get(G + "/v1/models", headers={"Authorization": "Bearer nope"})
    check("bad key rejected", r.status_code == 401, r.status_code)

    r = httpx.get(G + "/search", params={"q": "python programming language"}, headers=H, timeout=30)
    j = r.json()
    res = j.get("results", [])
    print("DEBUG", j.get("debug"))
    check("raw search returns results", len(res) > 0, f"{len(res)} results, first: {res[0]['url'] if res else None}")
    r2 = httpx.get(G + "/search", params={"q": "latest stable python release"}, headers=H, timeout=30).json()
    print("DEBUG2", r2.get("debug"))
    print([x["url"] for x in r2["results"]])
    check("non-news web results present", any("news" not in " ".join(x["engines"]) for x in r2["results"]))

    ids = [m["id"] for m in httpx.get(G + "/v1/models", headers=H, timeout=30).json()["data"]]
    check("models include :web variants", any(i.endswith(":web") for i in ids), ids[:3])

    def chat(model, text):
        body = {"model": model, "messages": [{"role": "user", "content": text}]}
        r = httpx.post(G + "/v1/chat/completions", json=body, headers=H, timeout=120)
        return r.json()["choices"][0]["message"]["content"]

    t = chat("google/gemini-2.5-flash:web", "what is the latest stable python version")
    print(t[:1500])
    check(":web chat injects search results", "Live web search results" in t and "[1]" in t)

    t = chat("google/gemini-2.5-flash", "hi")
    check("plain chat skips search", "Live web search" not in t)
finally:
    backend.terminate()
sys.exit(1 if fails else 0)
