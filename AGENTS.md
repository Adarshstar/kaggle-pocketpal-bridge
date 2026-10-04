# Instructions for AI agents working in this repo
Read `HANDOFF.md` first: it has the architecture, how to operate, secrets (names only), known issues and next ideas.
Rules: never commit secret values (use GitHub secrets); run `gh workflow run live.yml -f mode=test` before going live;
check `/health` after redeploying; keep the README and HANDOFF.md up to date when you change something.
