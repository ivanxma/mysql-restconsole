# MySQL REST Console

Flask web application for administering and testing MySQL REST Service (MRS) endpoints through a myapp-style login workflow.

## Features

- local users, groups, profile assignments, and profiles stored in embedded MySQL database `configdb`
- first-level login with local console users
- second-level login to assigned DB profiles
- default bootstrap admin `localadmin` / `localadmin`, forced to change password on first login
- role-specific menus for:
  - Admin
  - General User
- REST service discovery, creation, exposure, and endpoint testing

## Layout

The code is split by responsibility:

- `app.py`: Flask app shell, template context, route registration
- `modules/page_routes.py`: page and form route orchestration
- `modules/mysql_service.py`: MySQL Shell and MRS DDL operations
- `modules/rest_service.py`: MRS REST endpoint discovery and execution
- `modules/profile_store.py`: `configdb.connection_profiles` profile storage
- `modules/local_auth.py`: `configdb` local users, groups, and profile assignments
- `modules/session_store.py`: role-aware menu/session helpers
- `modules/update_service.py`: Admin auto-update status and job launching

## Configuration

Run `setup.sh` to initialize the embedded socket-only MySQL instance and create `configdb`. The setup seeds one local admin user: `localadmin` with password `localadmin`; the first login forces a password change. Profiles and local user/group data are stored in `configdb`, not JSON files.

Connection passwords for target DB profiles are entered only during second-level profile login and are not stored. TLS material, SSH keys, `.runtime.env`, tokens, and credential files are ignored by git.

Useful environment variables:

```bash
export MRS_WEBAPP_SECRET_KEY='replace-me'
export MRS_WEBAPP_PORT=443
export LOCAL_MYSQL_ADMIN_PASSWORD='temporary-password-for-first-setup'
# Optional: required when setup must download an embedded MySQL Server tarball.
export MRS_CONSOLE_MYSQL_SERVER_URL_LINUX_X86='https://dev.mysql.com/get/Downloads/MySQL-9.7/mysql-9.7.0-linux-glibc2.28-x86_64.tar.xz'
# Optional: override embedded MySQL Shell 9.7+ download URL.
export MRS_CONSOLE_MYSQL_SHELL_URL_LINUX_X86='https://dev.mysql.com/get/Downloads/MySQL-Shell/mysql-shell-9.7.0-linux-glibc2.28-x86-64bit.tar.gz'
```

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./setup.sh ol9 https
```

Then open `https://<host>/login`.

## Oracle Linux 9 on OCI Compute

This repository includes a rerunnable `setup.sh` path for Oracle Linux 9. The short OCI Compute init script below clones or refreshes the app, runs setup as the `opc` user, and starts the HTTPS systemd service on port `443`.

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

APP_SLUG="mysql-rest-console"
APP_REPO_URL="https://github.com/ivanxma/mysql-restconsole.git"
APP_BRANCH="main"
APP_USER="opc"
APP_DIR="/home/${APP_USER}/${APP_SLUG}"
STATE_DIR="/var/lib/${APP_SLUG}-init"
INIT_LOG="/var/log/${APP_SLUG}-init.log"

mkdir -p "${STATE_DIR}"
echo installing > "${STATE_DIR}/state"
exec > >(tee -a "${INIT_LOG}") 2>&1
trap 'echo failed > "${STATE_DIR}/state"' ERR

dnf install -y git sudo
if [[ -d "${APP_DIR}/.git" ]]; then
  sudo -u "${APP_USER}" git -C "${APP_DIR}" fetch --all --prune
  sudo -u "${APP_USER}" git -C "${APP_DIR}" pull --ff-only
else
  sudo -u "${APP_USER}" git clone --branch "${APP_BRANCH}" "${APP_REPO_URL}" "${APP_DIR}"
fi

chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
sudo -u "${APP_USER}" env \
  APP_HOST="0.0.0.0" \
  HTTPS_PORT="443" \
  SERVICE_USER="${APP_USER}" \
  SERVICE_GROUP="${APP_USER}" \
  MRS_CONSOLE_UPDATE_ALLOWED_REMOTE_URL="${APP_REPO_URL}" \
  MRS_CONSOLE_UPDATE_ALLOWED_BRANCH="${APP_BRANCH}" \
  LOCAL_MYSQL_ADMIN_PASSWORD="${LOCAL_MYSQL_ADMIN_PASSWORD:-}" \
  MRS_CONSOLE_MYSQL_SERVER_URL_LINUX_X86="${MRS_CONSOLE_MYSQL_SERVER_URL_LINUX_X86:-}" \
  "${APP_DIR}/setup.sh" ol9 https

systemctl enable --now mysql-rest-console-https.service
echo installed > "${STATE_DIR}/state"
```

When `MRS_CONSOLE_MYSQL_SERVER_URL_LINUX_X86` is supplied, setup initializes the embedded socket-only MySQL store and creates `configdb`. `LOCAL_MYSQL_ADMIN_PASSWORD` defaults to `localadmin` for bootstrap and should be changed immediately through first-login password rotation.

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

- requires a clean worktree except local runtime files such as `.runtime.env`, `.cache/`, `.embedded/`, `.ssh-tunnels/`, and `tls/`
- verifies `origin` against `MRS_CONSOLE_UPDATE_ALLOWED_REMOTE_URL` when set
- verifies the current branch against `MRS_CONSOLE_UPDATE_ALLOWED_BRANCH`, defaulting to `main`
- runs `git fetch --all --prune` and `git pull --ff-only`
- reruns `./setup.sh <os-family> none`
- restarts active `mysql-rest-console-https.service` services when systemd is available
- writes status and logs under the OS temp directory in `mysql-rest-console/`

The web-triggered updater defaults to `SKIP_PRIVILEGED_SETUP=1`. This lets a restricted service refresh code and Python dependencies without changing system packages, firewall rules, or systemd units. After an update that includes privileged deployment changes, run this from an SSH shell:

```bash
cd /home/opc/mysql-rest-console
./setup.sh ol9 https
sudo systemctl restart mysql-rest-console-https.service
```

For full service-managed updates, grant the service user only the specific passwordless sudo commands you accept operationally, such as `systemctl restart mysql-rest-console-https.service`. Do not grant broad passwordless sudo unless that matches your host security policy.
