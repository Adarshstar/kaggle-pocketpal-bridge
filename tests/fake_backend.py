"""Stand-in for the Kaggle backend (same API) used by CI. Echoes the prompt; fakes <think> output and the
search planner so the gateway's thinking/streaming/search paths can be tested without Kaggle."""
import json, os
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

app = FastAPI()
KEY = os.environ.get("BRIDGE_KEY", "")
SEEN = {"params": None, "reasoning": None, "stream": None}


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
    return {"version": "fake", "models": 1, "prompt_signature": "(prompt, temperature=None)", "llm_type": "Fake",
            "caps": ["stream", "native_reasoning"]}


@app.get("/seen")
def seen():
    return SEEN


@app.post("/v1/chat/completions")
def chat(p: dict, x_backend_key: str = Header(None)):
    _auth(x_backend_key)
    SEEN["params"] = p.get("params")
    SEEN["reasoning"] = p.get("reasoning")
    SEEN["stream"] = bool(p.get("stream"))
    txt = "\n".join(f"{m['role']}: {m['content']}" for m in p["messages"])
    shown = txt.replace("<think>", "[think]").replace("</think>", "[/think]")  # echo must not contain real tags
    native = p.get("reasoning") not in (None, "none")
    thoughts = ""
    if "You plan web searches" in txt:
        out = json.dumps({"search": True, "queries": ["python latest stable release", "python 3 release notes"],
                          "recency": "month"})
    elif "<think> and </think>" in txt:
        out = "<think>fake reasoning about the question</think>\n\nECHO\n" + shown
    else:
        out = "ECHO\n" + shown
        thoughts = "fake native thoughts" if native else ""
    if p.get("stream"):
        full = ("<think>" + thoughts + "</think>\n\n" if thoughts else "") + out

        def gen():
            for i in range(0, len(full), 7):  # 7-char pieces split the tags on purpose
                yield "data: " + json.dumps({"delta": full[i:i + 7]}) + "\n\n"
            yield "data: " + json.dumps({"done": True}) + "\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    msg = {"role": "assistant", "content": out}
    if thoughts:
        msg["reasoning_content"] = thoughts
    return {"choices": [{"message": msg}]}
