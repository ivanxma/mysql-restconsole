#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  manage_heatwave_tunnels.sh <start|stop|status>
    --ssh-key <path>
    --ssh-user <name>
    --ssh-host <host>
    --remote-db-host <host>
    [options]

Creates or removes SSH tunnels for HeatWave database access and the REST API.

Required:
  start|stop|status               Action to perform.
  --ssh-key <path>                SSH private key for the bastion host.
  --ssh-user <name>               Bastion SSH user.
  --ssh-host <host>               Bastion SSH host.
  --remote-db-host <host>         Private DB host behind the bastion.

Optional:
  --remote-db-port <port>         Remote MySQL port. Default: 3306
  --remote-api-host <host>        Remote REST API host. Default: remote DB host
  --remote-api-port <port>        Remote REST API port. Default: 443
  --local-db-port <port>          Local MySQL port. Default: 3306
  --local-api-port <port>         Local REST API port. Default: 8443
  --socket-dir <path>             Directory for SSH control socket. Default: script dir/.ssh-tunnels
  --socket-name <name>            Control socket file name. Default: heatwave-mrs.sock
  --help                          Show this help text.

Examples:
  manage_heatwave_tunnels.sh start --ssh-key ~/.ssh/id_rsa --ssh-user opc --ssh-host 1.2.3.4 --remote-db-host 10.0.0.10
  manage_heatwave_tunnels.sh stop --ssh-key ~/.ssh/id_rsa --ssh-user opc --ssh-host 1.2.3.4 --remote-db-host 10.0.0.10
EOF
}

die() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

wait_for_port() {
  local host=$1
  local port=$2
  local retries=20

  while (( retries > 0 )); do
    if (echo >"/dev/tcp/$host/$port") >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    ((retries--))
  done

  return 1
}

ssh_key=""
ssh_user=""
ssh_host=""
remote_db_host=""
remote_api_host=""
remote_db_port="3306"
remote_api_port="443"
local_db_port="3306"
local_api_port="8443"
socket_dir=""
socket_name="heatwave-mrs.sock"
action=""

if [[ $# -eq 0 ]]; then
  usage
  exit 1
fi

case "$1" in
  start|stop|status)
    action=$1
    shift
    ;;
  --help|-h)
    usage
    exit 0
    ;;
  *)
    die "first argument must be start, stop, or status"
    ;;
esac

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssh-key)
      [[ $# -ge 2 ]] || die "missing value for --ssh-key"
      ssh_key=$2
      shift 2
      ;;
    --ssh-user)
      [[ $# -ge 2 ]] || die "missing value for --ssh-user"
      ssh_user=$2
      shift 2
      ;;
    --ssh-host)
      [[ $# -ge 2 ]] || die "missing value for --ssh-host"
      ssh_host=$2
      shift 2
      ;;
    --remote-db-host)
      [[ $# -ge 2 ]] || die "missing value for --remote-db-host"
      remote_db_host=$2
      shift 2
      ;;
    --remote-db-port)
      [[ $# -ge 2 ]] || die "missing value for --remote-db-port"
      remote_db_port=$2
      shift 2
      ;;
    --remote-api-host)
      [[ $# -ge 2 ]] || die "missing value for --remote-api-host"
      remote_api_host=$2
      shift 2
      ;;
    --remote-api-port)
      [[ $# -ge 2 ]] || die "missing value for --remote-api-port"
      remote_api_port=$2
      shift 2
      ;;
    --local-db-port)
      [[ $# -ge 2 ]] || die "missing value for --local-db-port"
      local_db_port=$2
      shift 2
      ;;
    --local-api-port)
      [[ $# -ge 2 ]] || die "missing value for --local-api-port"
      local_api_port=$2
      shift 2
      ;;
    --socket-dir)
      [[ $# -ge 2 ]] || die "missing value for --socket-dir"
      socket_dir=$2
      shift 2
      ;;
    --socket-name)
      [[ $# -ge 2 ]] || die "missing value for --socket-name"
      socket_name=$2
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ -n "$ssh_key" ]] || die "--ssh-key is required"
[[ -n "$ssh_user" ]] || die "--ssh-user is required"
[[ -n "$ssh_host" ]] || die "--ssh-host is required"
[[ -n "$remote_db_host" ]] || die "--remote-db-host is required"
[[ -n "$remote_api_host" ]] || remote_api_host="$remote_db_host"

if ! command -v ssh >/dev/null 2>&1; then
  die "ssh binary not found"
fi

ssh_key=${ssh_key/#\~/$HOME}

if [[ -z "$socket_dir" ]]; then
  script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  socket_dir="${script_dir}/.ssh-tunnels"
fi

mkdir -p "$socket_dir"
control_socket="${socket_dir}/${socket_name}"

ssh_target="${ssh_user}@${ssh_host}"
ssh_common_args=(
  -i "$ssh_key"
  -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -o ControlMaster=yes
  -S "$control_socket"
  "$ssh_target"
)

case "$action" in
  start)
    if ssh -S "$control_socket" -O check "$ssh_target" >/dev/null 2>&1; then
      printf 'Tunnel already running.\n'
      exit 0
    fi

    printf 'Starting SSH tunnels through %s...\n' "$ssh_target"
    ssh -f -N \
      "${ssh_common_args[@]}" \
      -L "${local_db_port}:${remote_db_host}:${remote_db_port}" \
      -L "${local_api_port}:${remote_api_host}:${remote_api_port}"

    wait_for_port 127.0.0.1 "$local_db_port" || die "database tunnel did not become ready on localhost:${local_db_port}"
    wait_for_port 127.0.0.1 "$local_api_port" || die "REST API tunnel did not become ready on localhost:${local_api_port}"

    printf 'Database tunnel: localhost:%s -> %s:%s\n' "$local_db_port" "$remote_db_host" "$remote_db_port"
    printf 'REST API tunnel: localhost:%s -> %s:%s\n' "$local_api_port" "$remote_api_host" "$remote_api_port"
    ;;
  stop)
    if ! ssh -S "$control_socket" -O check "$ssh_target" >/dev/null 2>&1; then
      printf 'Tunnel is not running.\n'
      rm -f "$control_socket"
      exit 0
    fi

    printf 'Stopping SSH tunnels through %s...\n' "$ssh_target"
    ssh -S "$control_socket" -O exit "$ssh_target" >/dev/null
    rm -f "$control_socket"
    printf 'Stopped.\n'
    ;;
  status)
    if ssh -S "$control_socket" -O check "$ssh_target" >/dev/null 2>&1; then
      printf 'Tunnel is running.\n'
      printf 'Database tunnel: localhost:%s -> %s:%s\n' "$local_db_port" "$remote_db_host" "$remote_db_port"
      printf 'REST API tunnel: localhost:%s -> %s:%s\n' "$local_api_port" "$remote_api_host" "$remote_api_port"
    else
      printf 'Tunnel is not running.\n'
      exit 1
    fi
    ;;
esac
