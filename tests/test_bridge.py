"""Tests kaggle/bridge.py's request handling against the REAL kaggle_benchmarks package (pip install kaggle-benchmarks)
with a fake LLM, so the thinking / streaming / isolation code paths are exercised without Kaggle's model proxy."""
import json, os, re, sys
os.environ["BRIDGE_KEY"] = "k"
import kaggle_benchmarks as kbench
from kaggle_benchmarks.actors.llms import LLMChat, LLMResponse

CALLS = []


class FakeLLM(LLMChat):
    def invoke(self, messages, system=None, reasoning=None, tools=None, **kw):
        CALLS.append({"n": len(messages), "system": system, "reasoning": reasoning, "kw": kw,
                      "text": [str(m.content) for m in messages]})
        if kw.get("bad"):
            raise ValueError("unsupported param")
        if self.stream_responses:
            return iter([LLMResponse(content=c) for c in ["Hel", "lo ", "world"]])
        return LLMResponse(content="Hello world", reasoning_traces="because" if reasoning not in (None, "none") else None)


kbench.llms = {"fake/m": FakeLLM(name="fake")}
src = open("kaggle/bridge.py").read()
a, b = src.index("app = FastAPI()"), src.index("def serve():")
ns = {"kbench": kbench, "KEY": "k", "DEFAULT": "fake/m", "os": os, "re": re, "json": json, "time": __import__("time"),
      "threading": __import__("threading"), "inspect": __import__("inspect")}
head = src[:src.index("import kaggle_benchmarks as kbench")]
exec("from typing import Any, List, Optional\nfrom fastapi import FastAPI, Header, HTTPException\n"
     "from fastapi.responses import StreamingResponse\nfrom pydantic import BaseModel\n" + src[a:b], ns)
from fastapi.testclient import TestClient
c = TestClient(ns["app"])
H = {"X-Backend-Key": "k"}
fails = 0


def check(n, ok, d=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n, str(d)[:200])
    fails += 0 if ok else 1


def post(**body):
    return c.post("/v1/chat/completions", json={"model": "fake/m", "messages": [{"role": "user", "content": "hi"}], **body}, headers=H)


r = post().json()["choices"][0]["message"]
check("plain call", r["content"] == "Hello world" and "reasoning_content" not in r, r)
r = post(reasoning="high").json()["choices"][0]["message"]
check("native thoughts returned separately", r.get("reasoning_content") == "because", r)
check("effort reached the model", CALLS[-1]["reasoning"] == "high", CALLS[-1])
CALLS.clear()
post(messages=[{"role": "system", "content": "SYS"}, {"role": "user", "content": "one"}])
post(messages=[{"role": "user", "content": "two"}])
check("each request is an isolated chat (no history leak)", all("one" not in t for t in CALLS[-1]["text"]) and CALLS[-1]["n"] == 1, CALLS[-1])
check("system message is passed", any("SYS" in t for t in CALLS[0]["text"]) or CALLS[0]["system"] == "SYS", CALLS[0])
r = post(params={"max_tokens": 50, "top_p": 0.5, "top_k": 3, "seed": 7}).json()["choices"][0]["message"]
check("sampling params forwarded, unsupported dropped", CALLS[-1]["kw"].get("max_tokens") == 50
      and CALLS[-1]["kw"].get("top_p") == 0.5 and "top_k" not in CALLS[-1]["kw"] and CALLS[-1]["kw"].get("seed") == 7, CALLS[-1]["kw"])
r = c.post("/v1/chat/completions", json={"model": "fake/m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}, headers=H)
lines = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
check("stream yields real chunks then done", [x.get("delta") for x in lines[:3]] == ["Hel", "lo ", "world"] and lines[-1].get("done"), lines)
check("bad key rejected", c.post("/v1/chat/completions", json={"messages": []}, headers={"X-Backend-Key": "x"}).status_code == 401)
check("info reports caps", c.get("/v1/info", headers=H).json().get("caps") == ["stream", "native_reasoning"])
sys.exit(1 if fails else 0)
