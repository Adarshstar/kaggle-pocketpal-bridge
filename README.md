# kaggle-pocketpal-bridge

OpenAI-compatible bridge: Kaggle free AI quota (+ web search, planned) -> PocketPal (via ngrok).

## How it works
- A Kaggle **Benchmarks** notebook runs `bridge.py`: a FastAPI server exposing
  `GET /v1/models` and `POST /v1/chat/completions` (OpenAI format).
- Requests are forwarded to `kaggle_benchmarks.llms[model].prompt(...)`, which uses the
  free Kaggle AI quota.
- `pyngrok` publishes port 8000 as a public URL that PocketPal connects to.

## Setup
1. Open your Kaggle Benchmarks notebook.
2. Add your ngrok authtoken as a secret named `NGROK_TOKEN` (Add-ons -> Secrets), or set the env var.
3. Paste/run `bridge.py`. It prints the **PocketPal Server URL** (no `/v1`).
4. In PocketPal: add a remote server, **Server Type: OpenAI**, paste the URL, any API key,
   then pick a model from the list.

Last known URL (changes whenever the tunnel restarts):
`https://scrubbed-calcium-subscript.ngrok-free.dev`

## Notes
- Keep the Kaggle notebook open and running; if the session stops, the link goes offline.
- Replies arrive all at once, not word by word (the full answer is sent as one chunk).
- Cost: each message uses the Kaggle AI quota (about $10/day and $100/month). Check it in the
  notebook's right-side panel -> Benchmark Task -> Daily/Monthly AI Quota (tap refresh).
- Start with `google/gemini-2.5-flash` or `anthropic/claude-sonnet-5@default`; use bigger
  models (Opus, GPT-6, Pro) sparingly.
- Models don't know today's date or recent events. Add a system prompt such as
  "Your knowledge may be outdated; say so when unsure about recent events."
- Real current info needs a web-search step (e.g. Tavily free tier API key): search first,
  then pass the results to the model. Not implemented yet.
- Regenerate the Kaggle and ngrok tokens once everything is stable (they were shared in chat),
  then update the `NGROK_TOKEN` secret and re-run the cell.
- Never commit tokens to this repo.
