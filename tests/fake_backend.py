"""Stand-in for the Kaggle backend (same API, echoes the prompt) used by CI."""
import os
from fastapi import FastAPI, Header, HTTPException
app = FastAPI()
KEY = os.environ.get("BRIDGE_KEY", "")

@app.get("/v1/models")
def models(x_backend_key: str = Header(None)):
    if x_backend_key != KEY:
        raise HTTPException(401)
    return {"object": "list", "data": [{"id": "google/gemini-2.5-flash", "object": "model"}]}

@app.post("/v1/chat/completions")
def chat(p: dict, x_backend_key: str = Header(None)):
    if x_backend_key != KEY:
        raise HTTPException(401)
    txt = "\n".join(f"{m['role']}: {m['content']}" for m in p["messages"])
    return {"choices": [{"message": {"role": "assistant", "content": "ECHO\n" + txt}}]}
