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
LOCAL_MYSQL_PROFILE_NAME="${LOCAL_MYSQL_PROFILE_NAME:-local-admin-profile}"
LOCAL_MYSQL_ADMIN_USER="${LOCAL_MYSQL_ADMIN_USER:-localadmin}"
LOCAL_MYSQL_ADMIN_PASSWORD_WAS_SET="${LOCAL_MYSQL_ADMIN_PASSWORD+x}"
LOCAL_MYSQL_ADMIN_PASSWORD="${LOCAL_MYSQL_ADMIN_PASSWORD:-localadmin}"
LOCAL_MYSQL_SOCKET="${LOCAL_MYSQL_SOCKET:-${APP_DIR}/.data/run/mysql.sock}"
LOCAL_MYSQL_DATABASE="${LOCAL_MYSQL_DATABASE:-mysql}"
MYSQL_SERVER_VERSION="${MRS_CONSOLE_MYSQL_SERVER_VERSION:-9.7.0}"
MYSQL_SERVER_URL_LINUX_X86="${MRS_CONSOLE_MYSQL_SERVER_URL_LINUX_X86:-}"
CONFIGDB_NAME="${MRS_CONSOLE_CONFIGDB_NAME:-configdb}"
CONFIGDB_USER="${MRS_CONSOLE_CONFIGDB_USER:-mysql_rest_console_config}"
CONFIGDB_PASSWORD="${MRS_CONSOLE_CONFIGDB_PASSWORD:-}"

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
    sudo dnf install -y git curl xz libaio openssl python3.12 python3.12-pip python3.12-devel firewalld
    sudo dnf install -y ncurses-compat-libs mysql-shell || true
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
  if [[ -z "$CONFIGDB_PASSWORD" ]]; then
    CONFIGDB_PASSWORD="$(openssl rand -base64 32 | tr -d '\n')"
  fi
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
MRS_CONSOLE_CONFIGDB_NAME=${CONFIGDB_NAME}
MRS_CONSOLE_CONFIGDB_USER=${CONFIGDB_USER}
MRS_CONSOLE_CONFIGDB_PASSWORD=${CONFIGDB_PASSWORD}
MRS_CONSOLE_CONFIGDB_SOCKET=${LOCAL_MYSQL_SOCKET}
LOCAL_MYSQL_SOCKET=${LOCAL_MYSQL_SOCKET}
EOF
  chmod 600 "${APP_DIR}/.runtime.env" || true
}

ensure_tls_assets() {
  [[ "$DEPLOY_MODE" == "https" || "$DEPLOY_MODE" == "both" ]] || return 0
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

setup_local_admin_profile_only() {
  echo "Embedded configdb is not available; using built-in bootstrap profiles only." >&2
}

start_embedded_mysql_if_needed() {
  if [[ -S "$LOCAL_MYSQL_SOCKET" ]]; then
    return
  fi
  "${APP_DIR}/.embedded/mysql-server/current/bin/mysqld" --defaults-file="${APP_DIR}/etc/my.cnf" --daemonize
  for _ in {1..30}; do
    [[ -S "$LOCAL_MYSQL_SOCKET" ]] && return
    sleep 1
  done
  echo "Embedded MySQL did not create socket: $LOCAL_MYSQL_SOCKET" >&2
  exit 1
}

bootstrap_embedded_mysql() {
  mkdir -p "${APP_DIR}/.embedded/mysql-server" "${APP_DIR}/.data/run" "${APP_DIR}/.data/log" "${APP_DIR}/.data/tmp" "${APP_DIR}/etc"
  if [[ -z "$MYSQL_SERVER_URL_LINUX_X86" ]]; then
    echo "MRS_CONSOLE_MYSQL_SERVER_URL_LINUX_X86 is not set; skipping embedded MySQL download." >&2
    setup_local_admin_profile_only
    return
  fi
  if [[ ! -x "${APP_DIR}/.embedded/mysql-server/current/bin/mysqld" ]]; then
    local archive="${APP_DIR}/.embedded/mysql-server/mysql-${MYSQL_SERVER_VERSION}.tar.xz"
    curl -L --fail -o "$archive" "$MYSQL_SERVER_URL_LINUX_X86"
    tar -xf "$archive" -C "${APP_DIR}/.embedded/mysql-server"
    local extracted
    extracted="$(find "${APP_DIR}/.embedded/mysql-server" -maxdepth 1 -type d -name 'mysql-*' | sort | tail -n 1)"
    ln -sfn "$extracted" "${APP_DIR}/.embedded/mysql-server/current"
  fi
  cat > "${APP_DIR}/etc/my.cnf" <<EOF
[mysqld]
basedir=${APP_DIR}/.embedded/mysql-server/current
datadir=${APP_DIR}/.data/mysql
socket=${LOCAL_MYSQL_SOCKET}
pid-file=${APP_DIR}/.data/run/mysqld.pid
log-error=${APP_DIR}/.data/log/mysqld.err
tmpdir=${APP_DIR}/.data/tmp
skip-networking
mysqlx=0
EOF
  if [[ ! -d "${APP_DIR}/.data/mysql/mysql" ]]; then
    [[ -n "$LOCAL_MYSQL_ADMIN_PASSWORD" ]] || {
      echo "LOCAL_MYSQL_ADMIN_PASSWORD is required to initialize embedded local admin MySQL." >&2
      setup_local_admin_profile_only
      return
    }
    "${APP_DIR}/.embedded/mysql-server/current/bin/mysqld" --defaults-file="${APP_DIR}/etc/my.cnf" --initialize
    local tmp_password
    tmp_password="$(awk '/temporary password/ {print $NF}' "${APP_DIR}/.data/log/mysqld.err" | tail -n 1)"
    start_embedded_mysql_if_needed
    "${APP_DIR}/.embedded/mysql-server/current/bin/mysql" --protocol=socket --socket="$LOCAL_MYSQL_SOCKET" -uroot -p"${tmp_password}" --connect-expired-password <<SQL
ALTER USER 'root'@'localhost' IDENTIFIED BY '${LOCAL_MYSQL_ADMIN_PASSWORD}';
RENAME USER 'root'@'localhost' TO '${LOCAL_MYSQL_ADMIN_USER}'@'localhost';
GRANT ALL PRIVILEGES ON *.* TO '${LOCAL_MYSQL_ADMIN_USER}'@'localhost' WITH GRANT OPTION;
FLUSH PRIVILEGES;
SQL
  else
    start_embedded_mysql_if_needed
    if [[ -z "$LOCAL_MYSQL_ADMIN_PASSWORD_WAS_SET" ]]; then
      echo "Embedded MySQL already initialized; skipping admin bootstrap because LOCAL_MYSQL_ADMIN_PASSWORD was not supplied."
      return
    fi
  fi
  LOCAL_PROFILE_NAME="$LOCAL_MYSQL_PROFILE_NAME" \
  LOCAL_MYSQL_SOCKET="$LOCAL_MYSQL_SOCKET" \
  LOCAL_MYSQL_ADMIN_USER="$LOCAL_MYSQL_ADMIN_USER" \
  LOCAL_MYSQL_ADMIN_PASSWORD="$LOCAL_MYSQL_ADMIN_PASSWORD" \
  LOCAL_MYSQL_DATABASE="$LOCAL_MYSQL_DATABASE" \
  MRS_CONSOLE_CONFIGDB_NAME="$CONFIGDB_NAME" \
  MRS_CONSOLE_CONFIGDB_USER="$CONFIGDB_USER" \
  MRS_CONSOLE_CONFIGDB_PASSWORD="$CONFIGDB_PASSWORD" \
  MYSQL_BIN="${APP_DIR}/.embedded/mysql-server/current/bin/mysql" \
  "${APP_DIR}/secured_connection_profile_setup.sh"
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
ExecStart=/usr/bin/bash ${APP_DIR}/${start_script}
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
bootstrap_embedded_mysql
chmod 700 .ssh-tunnels 2>/dev/null || true
chmod 700 profile_ssh_keys 2>/dev/null || true

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
