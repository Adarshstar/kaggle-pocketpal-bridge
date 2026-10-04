# HANDOFF - full context for the next AI agent

Written 2026-10-04. Owner: GitHub/Kaggle user `Adarshstar` (Kaggle username `adarshstar`).
Goal of the project: use the free Kaggle Benchmarks AI quota as an OpenAI-compatible API for the
**PocketPal** Android app, with free FOSS web search added. Everything is live and working as of this note.

NO SECRET VALUES ARE IN THIS REPO. Secrets are referred to by name only (see "Secrets").

## 1. Architecture

    PocketPal app
        |  HTTPS, OpenAI API, Bearer BRIDGE_KEY
        v
    ngrok static domain  https://scrubbed-calcium-subscript.ngrok-free.dev   (free ngrok account, pooling_enabled)
        |
        v
    GitHub Actions job (workflow "live")  -- runs on ubuntu-latest, max ~6 h, chains itself
        |-- gateway/gateway.py   FastAPI on 127.0.0.1:8080  (OpenAI API, auth, web-search injection)
        |-- SearXNG (Docker, port 8888 -> container 8080, config searxng/settings.yml)
        |-- ddgs python package (second search source, fallback/merge)
        |-- gateway/run_tunnel.py (pyngrok publishes :8080 on the static domain)
        v
    Kaggle notebook  adarshstar/new-benchmark-task-68ca6   (kaggle/bridge.py is its only cell)
        |-- FastAPI on 127.0.0.1:8000 (OpenAI API using kaggle_benchmarks `kbench.llms[model].prompt(...)`)
        |-- cloudflared quick tunnel (random https://*.trycloudflare.com URL, no account)
        |-- heartbeat thread: POST <gateway>/register {"url": tunnel} every 15 s with Bearer BRIDGE_KEY
        v
    Kaggle free AI quota (about $10/day, $100/month) -> Gemini, Claude, GPT, Grok, Qwen, DeepSeek, GLM ...

Why this shape: `kaggle_benchmarks` only works inside a Kaggle notebook, so the LLM call must happen there.
GitHub Actions hosts the search engine and the stable public URL. The notebook finds the gateway through the
stable ngrok domain and registers its own changing tunnel URL (notebook -> gateway direction), so nothing needs
to be re-entered in PocketPal when a tunnel restarts.

## 2. Repo map
- `gateway/gateway.py`   OpenAI-compatible gateway. Endpoints: GET /health (no auth), POST /register,
  GET /search?q= (returns results + `debug`), GET /v1/models, POST /v1/chat/completions.
  Model ids ending in `:web` get web-search context injected (system message with date + results + [n] citations);
  plain ids go straight to Kaggle. Non-streaming backend call; if client asked stream it replays the full text as one SSE chunk.
- `gateway/run_tunnel.py`  pyngrok tunnel on NGROK_DOMAIN; ~10 min before LIFETIME it runs `gh workflow run live.yml -f mode=live` (handover).
- `gateway/requirements.txt`  fastapi uvicorn httpx trafilatura pyngrok ddgs
- `searxng/settings.yml`  json format enabled, limiter off, short timeouts.
- `kaggle/bridge.py`  the Kaggle cell (backend). Reads BRIDGE_KEY from env or Kaggle Secrets.
- `tests/e2e.py`, `tests/fake_backend.py`  CI end-to-end test with a fake LLM backend (test mode only).
- `scripts/*.sh`  start SearXNG, start gateway, dump logs.
- `.github/workflows/live.yml`  workflow_dispatch with input `mode`: `test` (checks only, no tunnel) or `live` (publish on ngrok).
  concurrency group `run-<mode>`, cancel-in-progress false, so a queued run waits for the running one.

## 3. Secrets (names only)
GitHub repo Actions secrets: `NGROK_TOKEN` (ngrok authtoken), `BRIDGE_KEY` (shared key: PocketPal API key, gateway auth,
notebook->gateway registration, gateway->notebook header `X-Backend-Key`), `KAGGLE_API_TOKEN` (Kaggle CLI token, KGAT_...).
Kaggle side: the notebook cell sets `BRIDGE_KEY` inline via os.environ.setdefault (Kaggle CLI cannot create Kaggle Secrets).
The notebook is private. SECURITY: NGROK_TOKEN, KAGGLE_API_TOKEN and BRIDGE_KEY were all pasted into an AI chat session;
the user was told to regenerate them. After regenerating: update the GitHub secrets, re-push the notebook (BRIDGE_KEY),
and give the user the new BRIDGE_KEY for PocketPal. Older notebook versions on Kaggle may still contain the old ngrok token.
The current gateway pool also has no way to tell if ngrok free session limits are hit; see Known issues.

## 4. How to operate (commands an agent can run)
Tools used so far: GitHub CLI `gh` (through a "Server MCP" connector that is authenticated as Adarshstar), Kaggle CLI.
- Run `gh auth setup-git` in a fresh shell before `git push` (otherwise: could not read Username).
- Start/stop the live gateway: `gh workflow run live.yml -f mode=live` ; cancel old run first with `gh run cancel <id>`
  (queued run waits for the running one because cancel-in-progress is false).
- Run checks only: `gh workflow run live.yml -f mode=test` (does not touch ngrok, safe while live is running).
- Health: `GET https://scrubbed-calcium-subscript.ngrok-free.dev/health` with header `ngrok-skip-browser-warning: 1`
  -> `{"ok":true,"backend_connected":true,...}`. `backend_connected:false` means the Kaggle notebook is not running/registered.
- Search debug: `GET /search?q=...` with `Authorization: Bearer <BRIDGE_KEY>` returns `debug` (which SearXNG engines failed).
- Kaggle CLI: `python3 -m venv /tmp/kv && /tmp/kv/bin/pip install kaggle`, then `export KAGGLE_API_TOKEN=<token>`;
  `kaggle kernels list --mine`; `kaggle kernels pull adarshstar/new-benchmark-task-68ca6 -p dir -m`;
  edit the .ipynb; `kaggle kernels push -p dir`. `kaggle kernels status` returned HTTP 404 (API quirk), so verify via gateway /health.
- Re-pushing the notebook: take `kaggle/bridge.py`, prepend `import os; os.environ.setdefault("BRIDGE_KEY","<key>")`,
  append `while True: time.sleep(3600)` (keeps the Kaggle run alive), put it as the single code cell, push.
- Kaggle notebooks of the user: `adarshstar/new-benchmark-task-68ca6` (standard image, internet on, runs the bridge) and
  `adarshstar/new-benchmark-task-5d166` (uses the special `personal-benchmarks-new` docker image; its .ipynb pulled as non-JSON).
- Connector quirk: very large heredoc commands through the MCP terminal sometimes fail with "connector returned an error";
  write big files in several smaller commands. Terminal sessions can die; recreate with terminal_session_create.

## 5. PocketPal settings (user-facing)
Server Type OpenAI; URL `https://scrubbed-calcium-subscript.ngrok-free.dev` (no /v1); API key = BRIDGE_KEY.
Pick a model ending in `:web` for live search (curated list at top: gemini-2.5-flash, gemini-2.5-pro, claude-sonnet-5@default,
gpt-5.4-mini, deepseek-r1, qwen3-235b). Any model id plus `:web` works. Plain ids skip search and save quota.

## 6. Known models (from /v1/models, 2026-10-04, 41 ids)
anthropic/claude-haiku-4-5, opus-4-5/4-6/4-7/4-8/5, sonnet-4-5/4-6/5; google/gemini-2.5-flash/pro, 3-flash-preview,
3.1-flash-lite/pro-preview, 3.5-flash(+lite), 3.6/3.7/3.8-flash, gemma-4-26b/31b; openai/gpt-5.4(+mini/nano), gpt-5.5,
gpt-5.6-luna/sol/terra, gpt-6-astra, gpt-oss-120b/20b; deepseek-ai/deepseek-r1-0528; qwen3-235b/coder-480b/next-80b;
xai/grok-4.20(non-reasoning/reasoning), grok-4.5, grok-4.6; zai/glm-5. Start with gemini-2.5-flash or claude-sonnet-5;
use big models (Opus, GPT-6, Pro) sparingly because of the quota.

## 7. History / decisions (why things are the way they are)
1. Repo `kaggle-pocketpal-bridge` (private) was created empty. First version: the user's original single notebook cell
   (FastAPI + pyngrok inside Kaggle, no search). That worked and served PocketPal on the ngrok domain.
2. User asked for a free FOSS "advanced" web search made live from GitHub Actions. Chosen: SearXNG (FOSS metasearch) +
   trafilatura page extraction, run in Actions. A free ngrok account allows one agent session and the notebook already used it,
   so the roles were split: gateway (Actions) owns the ngrok domain; notebook uses a free cloudflared quick tunnel and registers.
3. Bugs found and fixed: (a) live mode ran the fake-backend e2e test and failed before the tunnel step -> e2e is test-mode only;
   (b) search returned old news only / empty: SearXNG engines DuckDuckGo (CAPTCHA), Brave (rate limit), Google News (access denied)
   are blocked from GitHub runner IPs and news results outranked web results -> web results are ranked first, `ddgs` added as a
   second source, `/search` exposes `debug`. After the fix "latest Python version" correctly returned 3.14.x.
4. The user's Kaggle API token was used to push the bridge into notebook 68ca6 (replacing the old ngrok cell).

## 8. Known issues / limits
- GitHub Actions: 6 h per job. run_tunnel.py queues a successor ~10 min before the end; expect a 1-2 min gap. Free private-repo
  quota is 2000 min/month (about 33 h). A public repo has unlimited minutes (no secrets are stored in the repo).
- Kaggle notebook runs have a session time limit; when it ends `/health` shows backend_connected:false. Fix: re-push the notebook.
- Search runs from datacenter IPs: DuckDuckGo/Brave/Google News blocked, quality varies between queries. Ideas: add engines in
  searxng/settings.yml, a free API search (Tavily/Brave API keys as GitHub secrets), better query rewriting with an LLM call.
- Replies are not streamed (full answer arrives at once).
- Old notebook sessions that still hold the ngrok pool (pooling_enabled) can steal requests; make sure no old cell is running.
- If ngrok complains about simultaneous sessions, make sure only one agent (the Actions job) uses NGROK_TOKEN.
- The gateway keeps the backend URL in memory only; it relearns it from the 15 s heartbeat after a restart.

## 9. Ideas for next work
- Real streaming (SSE chunks) from the Kaggle side.
- Smarter search: LLM query rewriting, `time_range`, more engines, source-quality ranking, caching, page-fetch timeouts.
- Auto re-launch of the Kaggle notebook (scheduled `kaggle kernels push` from a GitHub Action using KAGGLE_API_TOKEN).
- Quota guard: count requests and warn/limit expensive models.
- Optional public-repo switch for unlimited Actions minutes.
