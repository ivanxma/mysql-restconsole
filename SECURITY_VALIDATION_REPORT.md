# MySQL REST Console Security and Validation Report

Date: 2026-05-29

## Scope

This report covers the README OCI Compute init-script update, `setup.sh` deployment behavior, login layout, authentication flow, runtime security controls, dependency vulnerability checks, and deployment validation for the MySQL REST Console application.

## Summary

| Area | Result | Evidence |
| --- | --- | --- |
| OCI OL9 init script | Pass | README init script is 27 lines and delegates setup, TLS, firewall, systemd, embedded MySQL, and service start to `setup.sh`. |
| Local unit tests | Pass | `python3 -m unittest discover -s tests` ran 16 tests successfully. |
| Python compile | Pass | `python3 -m compileall app.py modules mysql_rest_console_update_worker.py` completed successfully. |
| Template parsing | Pass | All Flask/Jinja templates parsed successfully through the app Jinja environment. |
| Shell syntax | Pass | `bash -n setup.sh secured_connection_profile_setup.sh start_http.sh start_https.sh` completed successfully. |
| Whitespace validation | Pass | `git diff --check` completed successfully. |
| Dependency vulnerability audit | Pass | `python3 -m pip_audit -r requirements.txt` returned `No known vulnerabilities found` locally and on the OCI host after installing `pip-audit` into the remote virtualenv. |
| OCI deployment | Pass | Host `193.123.190.139` deployed through `./setup.sh ol9 https`; HTTPS service is active. |
| HTTPS probe | Pass | `https://193.123.190.139/` returns `302` to `/login`; `https://193.123.190.139/login` returns `200`. |

## Layout Validation

The login screen has been tightened to match the myapp-style centered authentication layout:

- `.main-panel-login` uses full viewport height.
- `.login-stage` uses a constrained responsive grid and centers its contents.
- `.login-card` uses full available form-column width without stretching the full page.
- Username and password inputs are stacked field labels in one HTML `<form>`, not row-based table inputs.
- The login form keeps browser autocomplete semantics: `autocomplete="username"` and `autocomplete="current-password"`.

Validated by rendering `/login` through Flask test client and checking for form, username input, password input, autocomplete attributes, and no rendered local socket/config password values.

## Logic Flow Validation

Validated expected unauthenticated behavior:

- `GET /login` returns `200`.
- `GET /dashboard/config` without login redirects to `/login`.
- Local login remains the first-level authentication.
- Admin users stay in local embedded administration.
- General users proceed to second-level profile login before REST API functions use the profile connection.
- Profile credentials are stored only in server memory by token for the active session and are cleared on logout/profile switch.

Existing automated tests cover SQL generation, REST service response handling, cache behavior, and REST metadata behavior.

## Security Validation

Security controls validated:

- Flask session cookies are configured with `HttpOnly=True`.
- Flask session cookies are configured with `SameSite=Lax`.
- HTTPS deployment writes `MRS_WEBAPP_SESSION_COOKIE_SECURE=1` to `.runtime.env`.
- When `.runtime.env` is sourced as the service start scripts do, Flask reports `SESSION_COOKIE_SECURE=True`.
- `setup.sh` preserves an existing `MRS_WEBAPP_SECRET_KEY` and generates a random `openssl rand -hex 32` key when one is missing.
- `.runtime.env` is mode `0600`.
- `.gitignore` excludes runtime env files, embedded downloads, data directories, TLS assets, SSH keys, profiles, logs, cache, token files, credential files, and secret-like files.
- The login page does not render local socket paths or config database passwords.
- Generated curl script behavior masks passwords and reads runtime credentials instead of embedding cleartext credentials.

Source scan notes:

- Expected bootstrap references remain for `localadmin` / `localadmin`, because first login forces password rotation.
- Expected password field names and password hashing code remain in authentication modules and templates.
- No private key material was found in the tracked source scan.

## Vulnerability Validation

Local audit:

```text
python3 -m pip_audit -r requirements.txt
No known vulnerabilities found
```

Remote OCI audit:

```text
.venv/bin/python -m pip_audit -r requirements.txt
No known vulnerabilities found
```

The audit checks Python package advisories for the dependencies in `requirements.txt`. It is not a full penetration test, container scan, host hardening audit, or MySQL privilege audit.

## Deployment Validation

Deployment target:

- Region: `uk-london-1`
- Host: `193.123.190.139`
- OS path: Oracle Linux 9
- Mode: HTTPS on port `443`
- App directory: `/home/opc/mysql-rest-console`
- Service: `mysql-rest-console-https.service`

Remote deployment command path:

```text
git pull --ff-only
./setup.sh ol9 https
systemctl restart mysql-rest-console-https.service
```

Remote validation results:

```text
python -m unittest discover -s tests: 16 tests OK
python -m compileall app.py modules mysql_rest_console_update_worker.py: OK
template parse check: templates parsed
bash -n setup/start scripts: OK
systemctl is-active mysql-rest-console-https.service: active
curl -sk -I https://193.123.190.139/: 302 Location: /login
curl -sk -I https://193.123.190.139/login: 200 OK
```

## Deployment Notes

- The README init script is intentionally short. It only installs Git/Sudo, clones or refreshes the repository, and runs `setup.sh ol9 https` with deployment environment values.
- `setup.sh` owns the long-running operational work: Python virtualenv, embedded MySQL Shell 9.7+, embedded socket-only MySQL/configdb, TLS asset generation, firewall opening, systemd unit installation, and service enable/start.
- Optional OL9 package handling was adjusted so missing `ncurses-compat-libs` does not print a misleading package-resolution error on images where the package is unavailable.
- SSH validation emitted an OpenSSH warning that the server connection is not using a post-quantum key exchange algorithm. This is host SSH configuration feedback, not an application validation failure.

## Residual Risk

- The self-signed TLS certificate is suitable for test deployment only. Production should provide managed TLS material through `TLS_CERT` and `TLS_KEY`.
- The bootstrap `localadmin` / `localadmin` account intentionally exists for first setup and must be changed on first login.
- `pip-audit` covers Python package vulnerabilities only; it does not validate OS package CVEs, MySQL account grants, network security list rules, or application authorization through a browser-driven penetration test.
