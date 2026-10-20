#!/bin/sh
set -e

case "$1" in
  api)
    exec uvicorn eventgraph.main:app --host 0.0.0.0 --port 8000 --proxy-headers
    ;;
  worker)
    exec python -m eventgraph.worker
    ;;
  test)
    exec pytest -q
    ;;
  *)
    exec "$@"
    ;;
esac
