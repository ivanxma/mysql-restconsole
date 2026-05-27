#!/usr/bin/env bash
set -euo pipefail

APP_ADDRESS="${APP_ADDRESS:-}"
APP_PORT="${APP_PORT:-}"
if [[ -f .runtime.env ]]; then
  set -a
  . ./.runtime.env
  set +a
fi
APP_ADDRESS="${APP_ADDRESS:-${APP_HOST:-127.0.0.1}}"
APP_PORT="${APP_PORT:-${HTTP_PORT:-5000}}"

PYTHON_BIN="${VENV_DIR:-.venv}/bin/python"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="python3"
exec "$PYTHON_BIN" -c "from app import app; app.run(debug=False, host='${APP_ADDRESS}', port=int('${APP_PORT}'), threaded=True)"
