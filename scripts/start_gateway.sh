#!/usr/bin/env bash
# In test mode the gateway talks to the local fake backend; in live mode it waits for the Kaggle notebook to register.
if [ "$MODE" = "test" ]; then export BACKEND_URL=http://127.0.0.1:8000; fi
nohup python -m uvicorn gateway.gateway:app --host 127.0.0.1 --port 8080 --log-level warning > gateway.log 2>&1 &
for i in $(seq 1 20); do
  curl -sf http://127.0.0.1:8080/health && exit 0
  sleep 1
done
cat gateway.log
exit 1
