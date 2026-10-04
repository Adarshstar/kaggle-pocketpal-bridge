"""Stand-in for the Kaggle backend (same API) used by CI. Echoes the prompt; fakes <think> output and the
search planner so the gateway's thinking/streaming/search paths can be tested without Kaggle."""
import json, os
from fastapi import FastAPI, Header, HTTPException

app = FastAPI()
KEY = os.environ.get("BRIDGE_KEY", "")
SEEN = {"params": None}


def _auth(k):
    if k != KEY:
        raise HTTPException(401)


@app.get("/v1/models")
def models(x_backend_key: str = Header(None)):
    _auth(x_backend_key)
    return {"object": "list", "data": [{"id": "google/gemini-2.5-flash", "object": "model"}]}


@app.get("/v1/info")
def info(x_backend_key: str = Header(None)):
    _auth(x_backend_key)
    return {"version": "fake", "models": 1, "prompt_signature": "(prompt, temperature=None)", "llm_type": "Fake"}


@app.get("/seen")
def seen():
    return SEEN


@app.post("/v1/chat/completions")
def chat(p: dict, x_backend_key: str = Header(None)):
    _auth(x_backend_key)
    SEEN["params"] = p.get("params")
    txt = "\n".join(f"{m['role']}: {m['content']}" for m in p["messages"])
    shown = txt.replace("<think>", "[think]").replace("</think>", "[/think]")  # echo must not contain real tags
    if "You plan web searches" in txt:
        out = json.dumps({"search": True, "queries": ["python latest stable release", "python 3 release notes"],
                          "recency": "month"})
    elif "<think> and </think>" in txt:
        out = "<think>fake reasoning about the question</think>\n\nECHO\n" + shown
    else:
        out = "ECHO\n" + shown
    return {"choices": [{"message": {"role": "assistant", "content": out}}]}
