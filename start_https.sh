#!/usr/bin/env bash
set -euo pipefail

APP_ADDRESS="${APP_ADDRESS:-}"
APP_PORT="${APP_PORT:-}"
TLS_CERT="${TLS_CERT:-}"
TLS_KEY="${TLS_KEY:-}"
if [[ -f .runtime.env ]]; then
  set -a
  . ./.runtime.env
  set +a
fi
APP_ADDRESS="${APP_ADDRESS:-${APP_HOST:-127.0.0.1}}"
APP_PORT="${APP_PORT:-${HTTPS_PORT:-443}}"

if [[ -z "$TLS_CERT" || -z "$TLS_KEY" ]]; then
  printf 'TLS_CERT and TLS_KEY are required for HTTPS startup.\n' >&2
  exit 1
fi

PYTHON_BIN="${VENV_DIR:-.venv}/bin/python"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="python3"
exec "$PYTHON_BIN" -c "from app import app; app.run(debug=False, host='${APP_ADDRESS}', port=int('${APP_PORT}'), ssl_context=('${TLS_CERT}', '${TLS_KEY}'), threaded=True)"
