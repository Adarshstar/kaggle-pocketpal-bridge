#!/usr/bin/env bash
# Start SearXNG (FOSS metasearch engine) in Docker on :8888 and wait until it answers JSON queries.
docker run -d --name searxng -p 8888:8080 \
  -e SEARXNG_SECRET="$(openssl rand -hex 24)" \
  -v "$PWD/searxng/settings.yml:/etc/searxng/settings.yml" \
  searxng/searxng:latest
for i in $(seq 1 40); do
  if curl -sf "http://127.0.0.1:8888/search?q=test&format=json" >/dev/null; then echo "searxng up"; exit 0; fi
  sleep 3
done
docker logs searxng | tail -40
exit 1
