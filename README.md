# MySQL REST Console

Flask web application for administering and testing MySQL REST Service (MRS) endpoints through a myapp-style login workflow.

## Features

- named login profiles stored in `profiles.json`
- login using MySQL credentials against the selected profile
- role detection from the authenticated account grants
- Admin > Config page for DB and REST endpoint definitions
- role-specific menus for:
  - Admin
  - Rest Admin
  - Test User
- REST service discovery, creation, exposure, and endpoint testing

## Layout

The code is split by responsibility:

- `app.py`: Flask app shell, template context, route registration
- `modules/page_routes.py`: page and form route orchestration
- `modules/mysql_service.py`: MySQL Shell and MRS DDL operations
- `modules/rest_service.py`: MRS REST endpoint discovery and execution
- `modules/profile_store.py`: non-secret DB and REST endpoint profile storage
- `modules/session_store.py`: role-aware menu/session helpers
- `modules/update_service.py`: Admin auto-update status and job launching

## Configuration

Edit Admin > Config after login, or seed a local `profiles.json` from `profiles.example.json` with non-secret endpoint definitions:

```json
{
  "profiles": [
    {
      "name": "default",
      "label": "Local MySQL REST Service",
      "use_ssh_tunnel": false,
      "db_host": "127.0.0.1",
      "db_port": 3306,
      "api_host": "127.0.0.1",
      "api_port": 443,
      "local_db_port": 3306,
      "local_api_port": 8443,
      "ssh_key_path": "",
      "ssh_jump_host": "",
      "ssh_jump_user": "opc"
    }
  ]
}
```

Connection passwords are entered at login and are not stored in `profiles.json`. The real `profiles.json`, TLS material, SSH keys, `.runtime.env`, tokens, and credential files are ignored by git.

Useful environment variables:

```bash
export MRS_WEBAPP_SECRET_KEY='replace-me'
export MRS_WEBAPP_MYSQLSH=/path/to/mysqlsh
export MRS_WEBAPP_ADMIN_USER=admin
export MRS_WEBAPP_ADMIN_PASSWORD='change-me'
export MRS_WEBAPP_PORT=443
```

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
sudo ./setup.sh ol9 https
```

Then open `https://<host>/login`.

## Oracle Linux 9 on OCI Compute

This repository includes a rerunnable `setup.sh` path for Oracle Linux 9. The OCI Compute init script below clones or refreshes the app, runs setup as the `opc` user, installs an HTTPS systemd service on port `443`, opens firewalld, and writes a login banner that shows install progress.

Create the instance with these OCI values:

- Image: Oracle Linux 9
- Login user: `opc`
- Shape, compartment, VCN, subnet, and SSH key: use your tenancy-approved values
- Public IP: required if you want direct browser access
- Ingress rule: allow TCP `443` from your client network, or your chosen `HTTPS_PORT`
- Initialization script: paste the OL9 script in `Advanced options` > `Management` > `Initialization script`

Set `APP_REPO_URL` to the Git repository URL for this app before launching the instance.

```bash
#!/usr/bin/env bash
set -euo pipefail

APP_NAME="MySQL REST Console"
APP_SLUG="mysql-rest-console"
APP_REPO_URL="https://github.com/<owner>/<repo>.git"
APP_BRANCH="main"
APP_USER="opc"
APP_GROUP="opc"
APP_DIR="/home/${APP_USER}/${APP_SLUG}"
OS_FAMILY="ol9"
DEPLOY_MODE="https"
HTTPS_PORT="443"
APP_HOST="0.0.0.0"
STATE_DIR="/var/lib/${APP_SLUG}-init"
INIT_LOG="/var/log/${APP_SLUG}-init.log"
BANNER_FILE="/etc/profile.d/${APP_SLUG}-status.sh"

mkdir -p "${STATE_DIR}"
echo installing > "${STATE_DIR}/state"
touch "${INIT_LOG}"
chmod 0644 "${INIT_LOG}"

exec > >(tee -a "${INIT_LOG}") 2>&1

cat > "${BANNER_FILE}" <<'BANNER'
#!/usr/bin/env bash
[[ $- == *i* ]] || return 0
[[ "$(id -un)" == "opc" ]] || return 0
STATE_FILE="/var/lib/mysql-rest-console-init/state"
case "$(cat "${STATE_FILE}" 2>/dev/null || true)" in
  installing)
    echo "Please wait until installation to be completed."
    ;;
  installed)
    echo "MySQL REST Console setup has been completed."
    systemctl --no-pager --full status mysql-rest-console-https.service 2>/dev/null || true
    ;;
  failed)
    echo "MySQL REST Console setup failed. Review /var/log/mysql-rest-console-init.log."
    ;;
esac
BANNER
chmod 0644 "${BANNER_FILE}"

fail() {
  echo failed > "${STATE_DIR}/state"
  exit 1
}
trap fail ERR

dnf install -y git sudo

if [[ -d "${APP_DIR}/.git" ]]; then
  sudo -u "${APP_USER}" git -C "${APP_DIR}" fetch --all --prune
  sudo -u "${APP_USER}" git -C "${APP_DIR}" pull --ff-only
elif [[ -e "${APP_DIR}" ]]; then
  mv "${APP_DIR}" "${APP_DIR}.$(date +%Y%m%d%H%M%S).bak"
  sudo -u "${APP_USER}" git clone --branch "${APP_BRANCH}" "${APP_REPO_URL}" "${APP_DIR}"
else
  sudo -u "${APP_USER}" git clone --branch "${APP_BRANCH}" "${APP_REPO_URL}" "${APP_DIR}"
fi

chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"

sudo -u "${APP_USER}" env \
  APP_HOST="${APP_HOST}" \
  HTTPS_PORT="${HTTPS_PORT}" \
  SERVICE_USER="${APP_USER}" \
  SERVICE_GROUP="${APP_GROUP}" \
  MRS_CONSOLE_UPDATE_ALLOWED_REMOTE_URL="${APP_REPO_URL}" \
  MRS_CONSOLE_UPDATE_ALLOWED_BRANCH="${APP_BRANCH}" \
  "${APP_DIR}/setup.sh" "${OS_FAMILY}" "${DEPLOY_MODE}"

systemctl enable --now mysql-rest-console-https.service
systemctl --no-pager --full status mysql-rest-console-https.service || true
echo installed > "${STATE_DIR}/state"
```

Verification on Oracle Linux 9:

```bash
ssh opc@<public-ip>
sudo systemctl status mysql-rest-console-https.service
sudo tail -n 100 /var/log/mysql-rest-console-init.log
curl -sk -I https://<public-ip>/login
```

If you rerun the init script, it refreshes an existing Git checkout with `git fetch --all --prune` and `git pull --ff-only` instead of replacing it. Runtime files remain owned by `opc`.

## Auto-Update

Admin users can open `Admin > Update` to refresh the app from Git. The updater:

- requires a clean worktree except local runtime files such as `.runtime.env`, `profiles.json`, `.cache/`, `.embedded/`, `.ssh-tunnels/`, and `tls/`
- verifies `origin` against `MRS_CONSOLE_UPDATE_ALLOWED_REMOTE_URL` when set
- verifies the current branch against `MRS_CONSOLE_UPDATE_ALLOWED_BRANCH`, defaulting to `main`
- runs `git fetch --all --prune` and `git pull --ff-only`
- reruns `./setup.sh <os-family> none`
- restarts active `mysql-rest-console-http.service` or `mysql-rest-console-https.service` services when systemd is available
- writes status and logs under the OS temp directory in `mysql-rest-console/`

The web-triggered updater defaults to `SKIP_PRIVILEGED_SETUP=1`. This lets a restricted service refresh code and Python dependencies without changing system packages, firewall rules, or systemd units. After an update that includes privileged deployment changes, run this from an SSH shell:

```bash
cd /home/opc/mysql-rest-console
./setup.sh ol9 https
sudo systemctl restart mysql-rest-console-https.service
```

For full service-managed updates, grant the service user only the specific passwordless sudo commands you accept operationally, such as `systemctl restart mysql-rest-console-https.service`. Do not grant broad passwordless sudo unless that matches your host security policy.
