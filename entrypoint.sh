#!/bin/sh
set -e

case "$1" in
  api)
    exec uvicorn eventgraph.main:app --host 0.0.0.0 --port 8000 --proxy-headers
    ;;
  worker)
    exec python -m eventgraph.worker
    ;;
  profile-gui)
    exec python -m gui --host 0.0.0.0 --port 8765
    ;;
  profile-prepare)
    shift
    exec python -m profile.db.prepare "$@"
    ;;
  test)
    exec pytest -q
    ;;
  *)
    exec "$@"
    ;;
esac
