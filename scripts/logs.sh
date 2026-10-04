#!/usr/bin/env bash
tail -50 gateway.log || true
docker logs searxng 2>&1 | tail -50 || true
