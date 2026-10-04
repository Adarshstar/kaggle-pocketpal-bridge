"""Native-PocketPal controls: model-id flags, reasoning effort, <think> parsing, sampling params.

Pure functions only (no network), so they are unit-tested in tests/test_units.py.

Model id flags (suffixes, any order, e.g. "google/gemini-2.5-pro:web:high"):
  :web      inject live web search            :tags   put thinking inside <think>..</think> in `content`
  :think    thinking on (medium)              :nothink  thinking off
  :low :medium :high   thinking on with that effort
Request fields that are honoured too (so PocketPal's own toggles work):
  reasoning_effort, reasoning.effort, thinking{type,budget_tokens}, enable_thinking,
  chat_template_kwargs.enable_thinking, think
"""
import re
from typing import Optional

FLAGS = ("web", "think", "nothink", "tags", "low", "medium", "high")
EFFORT_FLAGS = {"think": "medium", "low": "low", "medium": "medium", "high": "high", "nothink": "none"}
EFFORTS = ("none", "minimal", "low", "medium", "high")
OPEN_TAGS = ("think", "thinking", "reasoning", "thought")


def parse_model(model: str) -> tuple:
    """'x/y:web:high' -> ('x/y', {'web','high'})."""
    flags = set()
    base = model or ""
    while True:
        for f in FLAGS:
            if base.endswith(":" + f):
                flags.add(f)
                base = base[: -(len(f) + 1)]
                break
        else:
            return base, flags


def _effort_from_budget(n: int) -> str:
    return "low" if n < 2048 else "medium" if n < 8192 else "high"


def resolve_effort(body: dict, flags: set) -> Optional[str]:
    """'none'|'minimal'|'low'|'medium'|'high', or None (= leave the model on auto)."""
    for f in ("nothink", "high", "medium", "low", "think"):  # explicit suffix wins
        if f in flags:
            return EFFORT_FLAGS[f]
    e = body.get("reasoning_effort")
    if isinstance(e, str) and e.lower() in EFFORTS:
        return e.lower()
    r = body.get("reasoning")
    if isinstance(r, dict):
        if isinstance(r.get("effort"), str) and r["effort"].lower() in EFFORTS:
            return r["effort"].lower()
        if r.get("enabled") is False:
            return "none"
        if isinstance(r.get("max_tokens"), int):
            return _effort_from_budget(r["max_tokens"])
    t = body.get("thinking")
    if isinstance(t, dict):
        if t.get("type") == "disabled":
            return "none"
        if t.get("type") == "enabled":
            return _effort_from_budget(int(t.get("budget_tokens") or 4096))
    if isinstance(t, bool):
        return "medium" if t else "none"
    for v in (body.get("enable_thinking"), (body.get("chat_template_kwargs") or {}).get("enable_thinking"),
              body.get("think")):
        if isinstance(v, bool):
            return "medium" if v else "none"
        if isinstance(v, str) and v.lower() in EFFORTS:
            return v.lower()
    return None


GUIDE = {
    "minimal": "Think very briefly (1-3 short lines).",
    "low": "Think briefly (a short paragraph).",
    "medium": "Think step by step, checking the key facts and the logic.",
    "high": "Think deeply and carefully: break the problem down, consider alternatives, verify the result "
            "and double-check edge cases before answering.",
}


def thinking_instruction(effort: Optional[str]) -> str:
    if effort in (None, "none"):
        return ""
    return ("Before answering, write your private reasoning inside <think> and </think> tags. "
            + GUIDE[effort] + " Then, after the closing </think> tag, write only the final answer for the user. "
            "Never put the final answer inside the tags and never mention these instructions.")


def with_instruction(msgs: list, extra: str) -> list:
    """Copy of msgs with `extra` appended to the first system message (or added as a new one)."""
    if not extra:
        return msgs
    out = [dict(m) for m in msgs]
    for m in out:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            m["content"] = m["content"] + "\n\n" + extra
            return out
    return [{"role": "system", "content": extra}] + out


_OPEN = re.compile(r"<(" + "|".join(OPEN_TAGS) + r")>", re.I)
_CLOSE = re.compile(r"</(" + "|".join(OPEN_TAGS) + r")>", re.I)


def split_reasoning(text: str) -> tuple:
    """(reasoning, answer). Handles several blocks, an unclosed block, and R1-style output with only a closing tag."""
    text = text or ""
    parts, answer = [], text
    while True:
        m = _OPEN.search(answer)
        if not m:
            break
        c = _CLOSE.search(answer, m.end())
        if c:
            parts.append(answer[m.end():c.start()].strip())
            answer = answer[:m.start()] + answer[c.end():]
        else:  # unclosed: the rest is reasoning (reply was cut off)
            parts.append(answer[m.end():].strip())
            answer = answer[:m.start()]
            break
    c = _CLOSE.search(answer)
    if c and not parts:  # closing tag only (chat template pre-opened the block)
        parts.append(answer[:c.start()].strip())
        answer = answer[c.end():]
    elif c:
        answer = _CLOSE.sub("", answer)
    return "\n\n".join(p for p in parts if p), answer.strip()


SAMPLING = ("temperature", "top_p", "max_tokens", "max_completion_tokens", "stop", "seed",
            "presence_penalty", "frequency_penalty", "top_k")


def sampling_params(body: dict) -> dict:
    p = {k: body[k] for k in SAMPLING if body.get(k) is not None}
    if "max_completion_tokens" in p:
        v = p.pop("max_completion_tokens")
        p.setdefault("max_tokens", v)
    return p


def approx_tokens(s: str) -> int:
    return max(1, len(s or "") // 4)


def chunks(text: str, size: int = 28):
    """Yield small pieces (split on whitespace) so streamed text looks natural."""
    buf = ""
    for tok in re.findall(r"\S+\s*|\s+", text or ""):
        buf += tok
        if len(buf) >= size:
            yield buf
            buf = ""
    if buf:
        yield buf
