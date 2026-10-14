from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


HOST = "127.0.0.1"
PORT = 8000
APP_URL = f"http://{HOST}:{PORT}"
HEALTH_URL = f"{APP_URL}/api/health"


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def show_error(message: str) -> None:
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, "InferTree 启动失败", 0x10)
    else:
        print(message, file=sys.stderr)


def server_is_ready(timeout: float = 0.8) -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            return payload.get("service") == "infertree-agent"
    except (OSError, ValueError, UnicodeError, urllib.error.URLError):
        return False


def start_server(root: Path) -> None:
    python = root / ".venv" / "Scripts" / "pythonw.exe"
    if not python.exists():
        raise RuntimeError(
            "未找到项目虚拟环境。请确认 .venv 与 InferTree.exe 位于同一项目目录。"
        )
    if not (root / "config.local.toml").exists():
        raise RuntimeError("未找到 config.local.toml，请先创建并填写模型配置。")

    log_dir = root / "data"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "launcher.log"
    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    child_env = os.environ.copy()
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"

    with log_path.open("ab") as log:
        subprocess.Popen(
            [
                str(python),
                "-m",
                "uvicorn",
                "backend.main:app",
                "--host",
                HOST,
                "--port",
                str(PORT),
            ],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=creation_flags,
            close_fds=True,
            env=child_env,
        )


def main() -> int:
    root = project_root()
    try:
        if not server_is_ready():
            start_server(root)
            for _ in range(40):
                if server_is_ready():
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError(
                    "Agent 服务未能在 10 秒内启动。请查看 data\\launcher.log。"
                )

        if os.environ.get("INFERTREE_NO_BROWSER") != "1":
            webbrowser.open(APP_URL)
        return 0
    except Exception as exc:
        show_error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
