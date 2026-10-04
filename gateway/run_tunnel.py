"""Publish the gateway (port 8080) on the static ngrok domain and keep it up, then hand over to a fresh run."""
import os, subprocess, time
from pyngrok import ngrok

DOMAIN = os.environ.get("NGROK_DOMAIN", "scrubbed-calcium-subscript.ngrok-free.dev")
LIFETIME = int(os.environ.get("LIFETIME_SECONDS", str(5 * 3600 + 40 * 60)))
HANDOVER = 10 * 60

ngrok.set_auth_token(os.environ["NGROK_TOKEN"])
t = ngrok.connect(8080, "http", domain=DOMAIN, pooling_enabled=True)
print("LIVE:", t.public_url, flush=True)

end = time.time() + LIFETIME
time.sleep(max(0, LIFETIME - HANDOVER))
print("Queueing next run for handover", flush=True)
subprocess.run(["gh", "workflow", "run", "live.yml", "-f", "mode=live"], check=False)
time.sleep(max(0, end - time.time()))
ngrok.kill()
