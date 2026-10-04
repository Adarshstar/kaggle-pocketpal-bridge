# kaggle-pocketpal-bridge

Full context for humans and AI agents: see HANDOFF.md (and AGENTS.md).

OpenAI-compatible bridge for PocketPal: Kaggle free AI quota + free FOSS web search (SearXNG).

    PocketPal -> ngrok static domain -> [GitHub Actions: gateway + SearXNG] -> Kaggle notebook (LLM)

## Parts
- gateway/gateway.py - runs in GitHub Actions. OpenAI API, API-key check, web search + page extraction, forwards to Kaggle.
- searxng/settings.yml - SearXNG metasearch (many engines, JSON output), run in Docker by the workflow.
- gateway/run_tunnel.py - publishes the gateway on the ngrok domain and queues the next run for handover.
- kaggle/bridge.py - Kaggle notebook cell: LLM backend (kbench.llms), free Cloudflare tunnel, registers with the gateway every 15 s.
- tests/ - end-to-end checks with a fake backend.
- .github/workflows/live.yml - mode `test` (checks only) or `live` (publish on ngrok).

## Secrets (never commit values)
- GitHub repo secrets: NGROK_TOKEN, BRIDGE_KEY
- Kaggle (Add-ons -> Secrets): BRIDGE_KEY (same value)

## Use
1. Kaggle: stop any old cell that opens an ngrok tunnel. Run kaggle/bridge.py as one cell.
2. GitHub: Actions -> live -> Run workflow -> mode `live`.
3. PocketPal: Server Type OpenAI, URL https://scrubbed-calcium-subscript.ngrok-free.dev (no /v1), API key = BRIDGE_KEY.
4. Choose a model ending in `:web` (e.g. google/gemini-2.5-flash:web) for live web search.
   Models without `:web` skip search and save quota. Any model id works with `:web` appended.

## How web search works
Query = your last message (plus the previous one for very short follow-ups) -> SearXNG (general + news) ->
de-duplicated top 8 -> text extracted from the top 3 pages (trafilatura) -> injected as a system message
with today's date and [n] citation numbers.

## Limits
- A GitHub Actions job lasts at most 6 h. The workflow queues its own successor about 10 min before the end
  (expect a 1-2 min gap). Private repo on the free plan: 2000 Actions minutes per month (about 33 h);
  public repos are unlimited.
- Replies arrive all at once. Each message uses Kaggle quota (about $10/day, $100/month).
- Regenerate tokens that were pasted into chats.

## v2: thinking controls, streaming, search v2
Flags on any model id: `:web` search, `:think` / `:low` / `:medium` / `:high` thinking effort, `:nothink`, `:tags`.
E.g. `google/gemini-2.5-pro:web:high`. Thinking (and search progress) is streamed in `reasoning_content`, separate from the
answer; use `:tags` if PocketPal does not show it. Details: HANDOFF.md section 2b, CHANGELOG.md.
Deploy: `gh workflow run live.yml -f mode=live`; after changing kaggle/bridge.py run `gh workflow run kaggle-push.yml`.
