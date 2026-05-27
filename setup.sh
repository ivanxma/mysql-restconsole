#!/usr/bin/env bash
set -euo pipefail

APP_SLUG="mysql-rest-console"
OS_FAMILY="${1:-${MRS_CONSOLE_OS_FAMILY:-}}"
DEPLOY_MODE="${2:-${DEPLOY_MODE:-https}}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-${APP_DIR}/.venv}"
APP_HOST="${APP_HOST:-0.0.0.0}"
HTTP_PORT="${HTTP_PORT:-5000}"
HTTPS_PORT="${HTTPS_PORT:-443}"
TLS_CERT="${TLS_CERT:-${APP_DIR}/tls/selfsigned.crt}"
TLS_KEY="${TLS_KEY:-${APP_DIR}/tls/selfsigned.key}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"
PYTHON_BIN="${MRS_CONSOLE_PYTHON_BIN:-}"
SKIP_PRIVILEGED_SETUP="${SKIP_PRIVILEGED_SETUP:-0}"

detect_os_family() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    printf 'macos\n'
    return
  fi
  if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    case "${ID:-}:${VERSION_ID%%.*}" in
      ol:9|oraclelinux:9) printf 'ol9\n' ;;
      ol:8|oraclelinux:8) printf 'ol8\n' ;;
      ubuntu:*) printf 'ubuntu\n' ;;
      *) printf 'unsupported\n' ;;
    esac
    return
  fi
  printf 'unsupported\n'
}

install_ol9_prereqs() {
  [[ "$SKIP_PRIVILEGED_SETUP" == "1" ]] && return
  if command -v sudo >/dev/null 2>&1; then
    sudo dnf install -y git curl python3.12 python3.12-pip python3.12-devel firewalld mysql-shell || true
  fi
}

select_python() {
  if [[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]]; then
    printf '%s\n' "$PYTHON_BIN"
    return
  fi
  for candidate in python3.12 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return
    fi
  done
  printf 'python3\n'
}

write_runtime_env() {
  cat > "${APP_DIR}/.runtime.env" <<EOF
OS_FAMILY=${OS_FAMILY}
DEPLOY_MODE=${DEPLOY_MODE}
APP_HOST=${APP_HOST}
HTTP_PORT=${HTTP_PORT}
HTTPS_PORT=${HTTPS_PORT}
TLS_CERT=${TLS_CERT}
TLS_KEY=${TLS_KEY}
SERVICE_USER=${SERVICE_USER}
SERVICE_GROUP=${SERVICE_GROUP}
MRS_CONSOLE_UPDATE_ALLOWED_BRANCH=${MRS_CONSOLE_UPDATE_ALLOWED_BRANCH:-main}
MRS_CONSOLE_UPDATE_ALLOWED_REMOTE_URL=${MRS_CONSOLE_UPDATE_ALLOWED_REMOTE_URL:-}
EOF
  chmod 600 "${APP_DIR}/.runtime.env" || true
}

ensure_tls_assets() {
  [[ "$DEPLOY_MODE" == "https" || "$DEPLOY_MODE" == "both" ]] || return
  if [[ -r "$TLS_CERT" && -r "$TLS_KEY" ]]; then
    return
  fi
  mkdir -p "$(dirname "$TLS_CERT")" "$(dirname "$TLS_KEY")"
  if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required to generate default HTTPS TLS assets." >&2
    exit 1
  fi
  openssl req -x509 -newkey rsa:3072 -nodes \
    -keyout "$TLS_KEY" \
    -out "$TLS_CERT" \
    -days 365 \
    -subj "/CN=mysql-rest-console" >/dev/null 2>&1
  chmod 600 "$TLS_KEY" "$TLS_CERT" || true
}

install_python_deps() {
  local selected_python="$1"
  "$selected_python" -m venv "$VENV_DIR"
  "${VENV_DIR}/bin/python" -m pip install --upgrade pip
  "${VENV_DIR}/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"
}

install_systemd_service() {
  local mode="$1"
  local service_name="$2"
  local start_script="$3"
  local port="$4"
  [[ "$SKIP_PRIVILEGED_SETUP" == "1" ]] && return
  command -v sudo >/dev/null 2>&1 || return
  [[ "$mode" == "http" || "$mode" == "https" || "$mode" == "both" ]] || return

  sudo tee "/etc/systemd/system/${service_name}" >/dev/null <<EOF
[Unit]
Description=MySQL REST Console ${mode}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
Environment=APP_ADDRESS=${APP_HOST}
Environment=APP_PORT=${port}
$(if [[ "$mode" == "https" ]]; then printf 'Environment=TLS_CERT=%s\nEnvironment=TLS_KEY=%s\n' "$TLS_CERT" "$TLS_KEY"; fi)
ExecStart=${APP_DIR}/${start_script}
Restart=on-failure
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
$(if [[ "$port" -lt 1024 ]]; then printf 'AmbientCapabilities=CAP_NET_BIND_SERVICE\n'; fi)

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
}

open_ol9_firewall() {
  local port="$1"
  [[ "$SKIP_PRIVILEGED_SETUP" == "1" ]] && return
  command -v sudo >/dev/null 2>&1 || return
  command -v firewall-cmd >/dev/null 2>&1 || return
  sudo systemctl enable --now firewalld || true
  local zone
  zone="$(sudo firewall-cmd --get-active-zones 2>/dev/null | awk 'NR==1 {print $1}')"
  zone="${zone:-$(sudo firewall-cmd --get-default-zone 2>/dev/null || true)}"
  zone="${zone:-public}"
  sudo firewall-cmd --zone="$zone" --permanent --add-port="${port}/tcp" || true
  sudo firewall-cmd --reload || true
}

if [[ -z "$OS_FAMILY" ]]; then
  OS_FAMILY="$(detect_os_family)"
fi
if [[ "$OS_FAMILY" == "unsupported" ]]; then
  echo "Unsupported OS family. Use ol9, ol8, ubuntu, or macos." >&2
  exit 1
fi

if [[ "$OS_FAMILY" == "ol9" ]]; then
  install_ol9_prereqs
fi

selected_python="$(select_python)"
install_python_deps "$selected_python"
ensure_tls_assets
write_runtime_env
chmod 600 profiles.json 2>/dev/null || true
chmod 700 .ssh-tunnels 2>/dev/null || true

case "$DEPLOY_MODE" in
  http)
    install_systemd_service http "${APP_SLUG}-http.service" start_http.sh "$HTTP_PORT"
    [[ "$OS_FAMILY" == "ol9" ]] && open_ol9_firewall "$HTTP_PORT"
    ;;
  https)
    install_systemd_service https "${APP_SLUG}-https.service" start_https.sh "$HTTPS_PORT"
    [[ "$OS_FAMILY" == "ol9" ]] && open_ol9_firewall "$HTTPS_PORT"
    ;;
  both)
    install_systemd_service http "${APP_SLUG}-http.service" start_http.sh "$HTTP_PORT"
    install_systemd_service https "${APP_SLUG}-https.service" start_https.sh "$HTTPS_PORT"
    [[ "$OS_FAMILY" == "ol9" ]] && open_ol9_firewall "$HTTP_PORT"
    [[ "$OS_FAMILY" == "ol9" ]] && open_ol9_firewall "$HTTPS_PORT"
    ;;
  none) ;;
  *) echo "Deploy mode must be http, https, both, or none." >&2; exit 1 ;;
esac

echo "Setup completed for ${OS_FAMILY} (${DEPLOY_MODE})."
