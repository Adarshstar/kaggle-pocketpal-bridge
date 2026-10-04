"""Run the e2e suite locally: starts the gateway on :18080 (backend :18000, no SearXNG), runs tests/e2e.py, stops it.
Usage: PYTHONPATH=. python scripts/dev_e2e.py   (needs gateway/requirements.txt installed)"""
import os, subprocess, sys, time, urllib.request

env = dict(os.environ, BRIDGE_KEY="localtest", PYTHONPATH=".", BACKEND_URL="http://127.0.0.1:18000",
           SEARXNG_URL="http://127.0.0.1:1", GATEWAY_PORT="18080", BACKEND_PORT="18000")
gw = subprocess.Popen([sys.executable, "-m", "uvicorn", "gateway.gateway:app", "--host", "127.0.0.1",
                       "--port", "18080", "--log-level", "warning"], env=env,
                      stdout=open("/tmp/gw-local.log", "w"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
try:
    for _ in range(20):
        try:
            urllib.request.urlopen("http://127.0.0.1:18080/health", timeout=2)
            break
        except Exception:
            time.sleep(1)
    rc = subprocess.run([sys.executable, "tests/e2e.py"], env=env, timeout=280).returncode
finally:
    gw.terminate()
sys.exit(rc)
