# Changelog

## 3.0 - real token streaming + native thinking (branch feat/native-stream)
- Kaggle bridge (kaggle/bridge.py v3): each request runs in its own `kbench.chats.new()` chat (no history leak);
  calls `llm.prompt(reasoning=..., seed, temperature, extra_api_params={max_tokens, top_p, stop, ...})`; returns the
  model's own thoughts via `kbench.chats.last_reasoning_traces()` as `reasoning_content`. Streaming
  (`stream:true`) uses `llm.invoke()` with `stream_responses=True` -> real token SSE with keep-alives.
  Falls back (richest -> plainest params) if a model rejects `reasoning` or extras. Registers `caps` with the gateway.
- Gateway v3: THINK_MODE=native (default; sends `reasoning` effort instead of the prompt trick, falls back to prompt
  mode on old notebooks without caps) | prompt. Incremental `<think>` splitter (tags may be split across chunks) turns
  streamed text into reasoning/answer live. Old (non-stream) notebooks still work via the simulated stream.
- PocketPal: default REASONING_FORMAT is now `tags` (`<think>..</think>` in content, which PocketPal parses itself);
  add `:field` to a model id for `reasoning_content` instead.
- Tests: tests/test_bridge.py runs the bridge against the real kaggle_benchmarks package with a fake LLM.
- Known kbench limit: while streaming, Gemini thought parts are dropped by the library, so streamed thinking only appears for
  models whose proxy embeds `<think>` in content; use a non-stream (`:think` on a gateway without caps) for others.

## 2.0 - native PocketPal controls, streaming, search v2 (branch feat/native-controls)
- Thinking/reasoning controls. Model-id flags `:think :low :medium :high :nothink :tags`, plus request fields
  `reasoning_effort`, `reasoning.effort`, `thinking{type,budget_tokens}`, `enable_thinking`,
  `chat_template_kwargs.enable_thinking`. The gateway adds a `<think>` instruction, then splits the reply into
  `reasoning_content` (separate field, shown in its own place by clients that support it) and `content`.
  `:tags` (or env REASONING_FORMAT=tags) sends `<think>...</think>` inside `content` instead.
- Streaming: real SSE chunks (reasoning first, then answer), keep-alive comments while Kaggle works, `usage`
  (estimated) when `stream_options.include_usage`. NOTE: the Kaggle call itself is still non-streaming; the gateway
  replays the finished text in small chunks (kbench exposes no token stream).
- Sampling params (temperature, top_p, max_tokens, stop, seed...) are forwarded to the Kaggle backend, which only
  passes those that `llm.prompt()` declares in its signature (see GET /v1/backend-info to inspect it).
- Search v2 (gateway/search.py): LLM query planner (decides if search is needed, 1-3 self-contained queries,
  recency), ddgs text + news + SearXNG merged with reciprocal-rank fusion, low-quality domain demotion, top-5 pages
  read and cut to the most relevant passages, pasted URLs are read directly, caches, search progress streamed into
  the thinking panel, "Sources" footer with the cited results.
- Fix: lxml/trafilatura crashed natively ("free(): invalid pointer") when pages were parsed in parallel threads ->
  all parsing now runs in one worker thread. requirements.txt gained lxml_html_clean.
- New: GET /v1/controls, GET /v1/backend-info, scripts/kaggle_push.py + workflow kaggle-push.yml (re-push the
  notebook from Actions), tests/test_units.py (offline), scripts/dev_e2e.py (local e2e), richer tests/e2e.py.
- SearXNG: engines that are always blocked from runner IPs are disabled.
