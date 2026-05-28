from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_SLUG = "mysql-rest-console"
REPO_DIR = Path(__file__).resolve().parent.parent
STATUS_DIR = Path(os.getenv("MRS_CONSOLE_UPDATE_DIR", tempfile.gettempdir())) / APP_SLUG
STATUS_FILE = STATUS_DIR / "update-status.json"
LOG_FILE = STATUS_DIR / "update.log"
HTTP_SERVICE = f"{APP_SLUG}-http.service"
HTTPS_SERVICE = f"{APP_SLUG}-https.service"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _is_stale_running_state(status: dict[str, Any]) -> bool:
    if status.get("state") not in {"starting", "running", "restarting"}:
        return False
    raw_updated_at = str(status.get("updated_at") or "")
    if not raw_updated_at:
        return False
    try:
        updated_at = datetime.fromisoformat(raw_updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated_at).total_seconds() > 900


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_update_status(include_token: bool = False) -> dict[str, Any]:
    status = _read_json(STATUS_FILE)
    log_text = ""
    try:
        log_text = LOG_FILE.read_text(encoding="utf-8", errors="replace")[-20000:]
    except OSError:
        pass
    if not status:
        status = {
            "state": "idle",
            "step": "-",
            "message": "No update has been started.",
            "started_at": "",
            "updated_at": "",
            "finished_at": "",
            "service_names": [],
        }
    stale_running_state = _is_stale_running_state(status)
    if stale_running_state:
        status["message"] = f"Previous update status was stale at {status.get('state')}; a new update can be started."
    status["log_text"] = log_text
    status["can_start"] = stale_running_state or status.get("state") not in {"starting", "running", "restarting"}
    if not include_token:
        status.pop("poll_token", None)
    return status


def write_update_status(**updates: Any) -> dict[str, Any]:
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    status = _read_json(STATUS_FILE)
    status.update(updates)
    status["updated_at"] = utc_now()
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
    _chmod_private(tmp)
    tmp.replace(STATUS_FILE)
    _chmod_private(STATUS_FILE)
    return status


def append_update_log(message: str) -> None:
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(message.rstrip("\n") + "\n")
    _chmod_private(LOG_FILE)


def start_update_job() -> dict[str, Any]:
    current = read_update_status(include_token=True)
    if current.get("state") in {"starting", "running", "restarting"}:
        return current

    poll_token = secrets.token_urlsafe(24)
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text("", encoding="utf-8")
    _chmod_private(LOG_FILE)
    status = write_update_status(
        state="starting",
        step="queued",
        message="Update job has been queued.",
        started_at=utc_now(),
        finished_at="",
        poll_token=poll_token,
        service_names=[],
    )
    worker = REPO_DIR / "mysql_rest_console_update_worker.py"
    subprocess.Popen(
        [
            sys.executable,
            str(worker),
            "--repo-dir",
            str(REPO_DIR),
            "--status-file",
            str(STATUS_FILE),
            "--log-file",
            str(LOG_FILE),
        ],
        cwd=str(REPO_DIR),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return status


def poll_token_matches(token: str) -> bool:
    expected = str(_read_json(STATUS_FILE).get("poll_token", ""))
    return bool(expected and secrets.compare_digest(expected, str(token or "")))
