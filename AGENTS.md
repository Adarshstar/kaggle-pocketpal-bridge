# Instructions for AI agents working in this repo
Read `HANDOFF.md` first: it has the architecture, how to operate, secrets (names only), known issues and next ideas.
Rules: never commit secret values (use GitHub secrets); run `gh workflow run live.yml -f mode=test` before going live;
check `/health` after redeploying; keep the README and HANDOFF.md up to date when you change something.
Also: run `PYTHONPATH=. python tests/test_units.py` and `scripts/dev_e2e.py` before pushing; record every change in CHANGELOG.md;
work on a branch, run the `test` workflow on it, then merge and run `live`. After changing kaggle/bridge.py run the `kaggle-push` workflow.
