"""Offline unit tests (no network): PYTHONPATH=. python tests/test_units.py"""
from gateway import reasoning as R
from gateway import search as S


def test_parse_model_flags():
    assert R.parse_model("google/gemini-2.5-pro:web:high") == ("google/gemini-2.5-pro", {"web", "high"})
    assert R.parse_model("anthropic/claude-sonnet-5@default") == ("anthropic/claude-sonnet-5@default", set())
    assert R.parse_model("x/y:think:tags")[1] == {"think", "tags"}


def test_effort_priority():
    assert R.resolve_effort({}, set()) is None
    assert R.resolve_effort({"reasoning_effort": "high"}, set()) == "high"
    assert R.resolve_effort({"reasoning_effort": "high"}, {"nothink"}) == "none"  # suffix wins
    assert R.resolve_effort({"reasoning": {"effort": "low"}}, set()) == "low"
    assert R.resolve_effort({"thinking": {"type": "enabled", "budget_tokens": 1000}}, set()) == "low"
    assert R.resolve_effort({"thinking": {"type": "enabled", "budget_tokens": 20000}}, set()) == "high"
    assert R.resolve_effort({"thinking": {"type": "disabled"}}, set()) == "none"
    assert R.resolve_effort({"chat_template_kwargs": {"enable_thinking": True}}, set()) == "medium"
    assert R.resolve_effort({"enable_thinking": False}, set()) == "none"


def test_split_reasoning():
    assert R.split_reasoning("<think>a b</think>\n\nAnswer") == ("a b", "Answer")
    assert R.split_reasoning("no tags here") == ("", "no tags here")
    assert R.split_reasoning("step one</think>Final") == ("step one", "Final")  # R1 style
    r, a = R.split_reasoning("<think>one</think>mid<think>two</think>end")
    assert r == "one\n\ntwo" and a == "midend"
    r, a = R.split_reasoning("<think>cut off")  # unclosed
    assert r == "cut off" and a == ""
    assert R.split_reasoning("<thinking>x</thinking>y") == ("x", "y")


def test_instruction_and_system_merge():
    assert R.thinking_instruction(None) == "" and R.thinking_instruction("none") == ""
    assert "<think>" in R.thinking_instruction("high")
    msgs = [{"role": "user", "content": "hi"}]
    out = R.with_instruction(msgs, "EXTRA")
    assert out[0] == {"role": "system", "content": "EXTRA"} and msgs == [{"role": "user", "content": "hi"}]
    out = R.with_instruction([{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}], "EXTRA")
    assert out[0]["content"] == "S\n\nEXTRA" and len(out) == 2


def test_stream_splitter():
    def run(parts):
        sp, out = R.Splitter(), []
        for c in parts:
            out += sp.feed(c)
        return out + sp.finish()
    o = run(["Hi <thi", "nk>abc ", "def</th", "ink>\n\nanswer 1 < 2 and <b>x</b>"])
    assert o == [("c", "Hi "), ("r", "abc "), ("r", "def"), ("c", "answer 1 < 2 and <b>x</b>")], o
    assert run(["plain text"]) == [("c", "plain text")]
    assert run(["<think>cut", " off"]) == [("r", "cut"), ("r", " off")]
    assert R.Splitter(enabled=False).feed("<think>x</think>") == [("c", "<think>x</think>")]
    assert R.parse_model("a/b:think:field")[1] == {"think", "field"}


def test_sampling_params():
    p = R.sampling_params({"temperature": 0.2, "max_completion_tokens": 50, "messages": [], "stream": True})
    assert p == {"temperature": 0.2, "max_tokens": 50}


def test_chunks_roundtrip():
    t = "hello  world\nthis is a longer text " * 20
    assert "".join(R.chunks(t)) == t


def test_parse_plan():
    p = S.parse_plan('noise {"search": true, "queries": ["a b", "c"], "recency": "week"} tail')
    assert p == {"search": True, "queries": ["a b", "c"], "recency": "week"}
    assert S.parse_plan('{"search": false, "queries": [], "recency": "none"}')["search"] is False
    assert S.parse_plan("not json") is None


def test_heuristic_plan():
    p = S.heuristic_plan([{"role": "user", "content": "latest python release today?"}])
    assert p["search"] and p["recency"] == "day"
    p = S.heuristic_plan([{"role": "user", "content": "who wrote Dune"},
                          {"role": "assistant", "content": "Frank Herbert"},
                          {"role": "user", "content": "and when?"}])
    assert "Dune" in p["queries"][0]


def test_fuse_dedup_and_rank():
    a = [{"url": "https://www.example.com/x/", "title": "A", "content": "short", "engines": ["ddgs"]},
         {"url": "https://pinterest.com/p", "title": "P", "content": "", "engines": ["ddgs"]}]
    b = [{"url": "https://example.com/x", "title": "A2", "content": "a longer snippet", "engines": ["searxng"]},
         {"url": "https://other.org/y", "title": "B", "content": "", "engines": ["searxng"]}]
    out = S.fuse([(1.0, a), (0.9, b)], 5)
    urls = [x["url"] for x in out]
    assert len(out) == 3 and urls[0].endswith("/x/")  # duplicate merged, in both lists -> top
    assert out[0]["snippet"] == "a longer snippet" and out[0]["engines"] == ["ddgs", "searxng"]
    assert urls[-1] == "https://pinterest.com/p"  # low-quality domain pushed down


def test_best_passages_prefers_relevant():
    filler = "\n".join(f"Unrelated paragraph number {i} about weather and cooking recipes and gardening." * 3
                       for i in range(30))
    text = "Intro line.\n" + filler + "\nThe Python 3.14 release adds template strings and deferred annotations.\n" + filler
    out = S.best_passages(text, "python 3.14 release template strings", 600)
    assert "template strings" in out and len(out) < 900


def test_sources_footer():
    info = {"sources": [{"n": 1, "title": "One", "url": "https://a"}, {"n": 2, "title": "Two", "url": "https://b"}]}
    assert "[2] Two" in S.sources_footer("see [2]", info) and "[1]" not in S.sources_footer("see [2]", info)
    assert "[1] One" in S.sources_footer("no cites", info)
    assert S.sources_footer("x", {}) == ""


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("PASS", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
