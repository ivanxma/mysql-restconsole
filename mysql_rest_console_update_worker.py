#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


APP_SLUG = "mysql-rest-console"
HTTP_SERVICE = f"{APP_SLUG}-http.service"
HTTPS_SERVICE = f"{APP_SLUG}-https.service"
DEFAULT_ALLOWED_BRANCH = "main"
ALLOWED_DIR_PREFIXES = (
    ".cache/",
    ".embedded/",
    ".ssh-tunnels/",
    "tls/",
)
ALLOWED_FILES = {
    ".runtime.env",
    "profiles.json",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_git_url(value: str) -> str:
    url = str(value or "").strip()
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url[len("git@github.com:") :]
    if url.endswith(".git"):
        url = url[:-4]
    return url.rstrip("/")


class Worker:
    def __init__(self, repo_dir: Path, status_file: Path, log_file: Path):
        self.repo_dir = repo_dir.resolve()
        self.status_file = status_file.resolve()
        self.log_file = log_file.resolve()
        self.status = self.load_status()

    def load_status(self) -> dict:
        try:
            payload = json.loads(self.status_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def write_status(self, **updates) -> None:
        self.status.update(updates)
        self.status["updated_at"] = utc_now()
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.status_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.status, indent=2), encoding="utf-8")
        self.chmod_private(tmp)
        tmp.replace(self.status_file)
        self.chmod_private(self.status_file)

    def log(self, message: str) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a", encoding="utf-8") as handle:
            handle.write(message.rstrip("\n") + "\n")
        self.chmod_private(self.log_file)

    @staticmethod
    def chmod_private(path: Path) -> None:
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def step(self, step: str, message: str) -> None:
        self.write_status(state="running", step=step, message=message)
        self.log(f"[{utc_now()}] {message}")

    def run(self, command: list[str], env: dict[str, str] | None = None) -> str:
        self.log("$ " + shlex.join(command))
        proc = subprocess.Popen(
            command,
            cwd=str(self.repo_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        output = []
        assert proc.stdout is not None
        for line in proc.stdout:
            output.append(line)
            self.log(line.rstrip("\n"))
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"Command failed with exit code {rc}: {shlex.join(command)}")
        return "".join(output)

    def capture(self, command: list[str]) -> str:
        result = subprocess.run(command, cwd=str(self.repo_dir), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or shlex.join(command)).strip())
        return result.stdout.strip()

    def runtime_env(self) -> dict[str, str]:
        env_file = self.repo_dir / ".runtime.env"
        env = {}
        if not env_file.exists():
            return env
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
        return env

    def detect_os_family(self, runtime_env: dict[str, str]) -> str:
        for key in ("MRS_CONSOLE_OS_FAMILY", "OS_FAMILY"):
            value = os.environ.get(key) or runtime_env.get(key, "")
            if value:
                return value.lower()
        if platform.system() == "Darwin":
            return "macos"
        fields = {}
        release = Path("/etc/os-release")
        if release.exists():
            for line in release.read_text(encoding="utf-8", errors="replace").splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    fields[key] = value.strip().strip('"').lower()
        distro = fields.get("ID", "")
        major = fields.get("VERSION_ID", "").split(".", 1)[0]
        if distro in {"ol", "oraclelinux"} and major == "9":
            return "ol9"
        if distro in {"ol", "oraclelinux"} and major == "8":
            return "ol8"
        if distro == "ubuntu":
            return "ubuntu"
        raise RuntimeError("Unable to detect supported OS family.")

    def verify_clean_worktree(self) -> None:
        output = self.capture(["git", "status", "--porcelain"])
        blocked = []
        for line in output.splitlines():
            path = line[3:].strip()
            if path in ALLOWED_FILES or any(path.startswith(prefix) for prefix in ALLOWED_DIR_PREFIXES):
                continue
            blocked.append(line)
        if blocked:
            raise RuntimeError("Git worktree has uncommitted application changes:\n" + "\n".join(blocked))

    def verify_source(self, runtime_env: dict[str, str]) -> None:
        branch = self.capture(["git", "branch", "--show-current"])
        origin = self.capture(["git", "remote", "get-url", "origin"])
        allowed_remote = os.environ.get("MRS_CONSOLE_UPDATE_ALLOWED_REMOTE_URL") or runtime_env.get("MRS_CONSOLE_UPDATE_ALLOWED_REMOTE_URL", "")
        allowed_branch = os.environ.get("MRS_CONSOLE_UPDATE_ALLOWED_BRANCH") or runtime_env.get("MRS_CONSOLE_UPDATE_ALLOWED_BRANCH", DEFAULT_ALLOWED_BRANCH)
        if allowed_remote and normalize_git_url(origin) != normalize_git_url(allowed_remote):
            raise RuntimeError(f"Update remote mismatch: origin={origin!r}, expected={allowed_remote!r}.")
        if allowed_branch and branch != allowed_branch:
            raise RuntimeError(f"Update branch mismatch: current={branch!r}, expected={allowed_branch!r}.")
        self.log(f"Verified update source {origin} on branch {branch}.")

    def active_services(self) -> list[str]:
        if not shutil.which("systemctl"):
            return []
        services = []
        for service in (HTTP_SERVICE, HTTPS_SERVICE):
            result = subprocess.run(["systemctl", "is-active", "--quiet", service])
            if result.returncode == 0:
                services.append(service)
        return services

    def restart_services(self, services: list[str]) -> None:
        if not services or not shutil.which("systemctl"):
            self.log("No active systemd service detected; restart skipped.")
            return
        self.write_status(state="restarting", step="restart", message="Restarting active services.", service_names=services)
        for service in services:
            self.run(["sudo", "systemctl", "restart", service])

    def main(self) -> None:
        try:
            self.write_status(state="running", step="start", message="Starting update worker.")
            runtime_env = self.runtime_env()
            os_family = self.detect_os_family(runtime_env)
            services = self.active_services()
            self.write_status(service_names=services)
            self.step("validate", "Validating update source and worktree.")
            self.verify_source(runtime_env)
            self.verify_clean_worktree()
            self.step("fetch", "Fetching repository updates.")
            self.run(["git", "fetch", "--all", "--prune"])
            self.step("pull", "Pulling repository changes with fast-forward only.")
            self.run(["git", "pull", "--ff-only"])
            self.step("setup", "Running setup.sh after repository update.")
            env = os.environ.copy()
            env.update(runtime_env)
            env.setdefault("SKIP_PRIVILEGED_SETUP", "1")
            self.run(["./setup.sh", os_family, "none"], env=env)
            self.restart_services(services)
            self.write_status(state="completed", step="done", message="Update completed.", finished_at=utc_now())
        except Exception as exc:
            self.log(f"ERROR: {exc}")
            self.write_status(state="error", step="failed", message=str(exc), finished_at=utc_now())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--log-file", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    Worker(Path(args.repo_dir), Path(args.status_file), Path(args.log_file)).main()
