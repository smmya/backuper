from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parent.parent
VAR_DIR = APP_ROOT / "var"
CONFIG_DIR = VAR_DIR / "config"
LOG_DIR = VAR_DIR / "logs"
STATE_DIR = VAR_DIR / "state"
CERT_DIR = VAR_DIR / "certs"
TMP_DIR = VAR_DIR / "tmp"


def ensure_dirs() -> None:
    for path in (VAR_DIR, CONFIG_DIR, LOG_DIR, STATE_DIR, CERT_DIR, TMP_DIR):
        path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def encode_code(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"BKUP1:{token}"


def decode_code(code: str) -> dict[str, Any]:
    code = code.strip()
    if code.startswith("bkup://"):
        code = "BKUP1:" + code[len("bkup://") :]
    if not code.startswith("BKUP1:"):
        raise ValueError("配置码格式不正确，应以 BKUP1: 开头")
    token = code.split(":", 1)[1].strip()
    padded = token + "=" * (-len(token) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("v") != 1:
        raise ValueError("不支持的配置码版本")
    return payload


def prompt(text: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    try:
        value = input(f"{text}{suffix}: ").strip()
    except EOFError:
        return default if default is not None else "0"
    return default if value == "" and default is not None else value


def confirm(text: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"{text} [{hint}]: ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes", "是", "1")


def pause() -> None:
    try:
        input("\n按回车继续...")
    except EOFError:
        return


def random_secret() -> str:
    return secrets.token_urlsafe(32)


def run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def command_exists(name: str) -> bool:
    try:
        proc = run(["which", name])
        return proc.returncode == 0
    except OSError:
        return False


def public_ip(timeout: int = 3) -> str | None:
    services = (
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
    )
    for url in services:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                value = resp.read(128).decode("utf-8").strip()
                if value:
                    return value
        except Exception:
            continue
    return None


def local_ip() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def detect_connect_host() -> tuple[str, str]:
    ip = public_ip()
    if ip:
        return ip, "公网 IP"
    ip = local_ip()
    if ip:
        return ip, "本地出口 IP"
    return "127.0.0.1", "回环地址"


def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _pid_memory_bytes(pid: int) -> int | None:
    status = Path(f"/proc/{pid}/status")
    if not status.exists():
        return None
    try:
        for line in status.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) * 1024
    except OSError:
        return None
    return None


def systemd_service_status(service: str) -> dict[str, Any]:
    status: dict[str, Any] = {
        "available": False,
        "active": "unknown",
        "enabled": "unknown",
        "pid": 0,
        "memory": None,
    }
    if not is_linux() or not command_exists("systemctl"):
        return status
    status["available"] = True
    active_proc = run(["systemctl", "is-active", service])
    enabled_proc = run(["systemctl", "is-enabled", service])
    status["active"] = active_proc.stdout.strip() or "unknown"
    status["enabled"] = enabled_proc.stdout.strip() or "unknown"
    pid_proc = run(["systemctl", "show", service, "--property", "MainPID", "--value"])
    try:
        status["pid"] = int((pid_proc.stdout.strip() or "0").splitlines()[0])
    except (ValueError, IndexError):
        status["pid"] = 0
    if status["pid"]:
        status["memory"] = _pid_memory_bytes(status["pid"])
    return status


def service_status_lines(service: str, version: str) -> list[str]:
    status = systemd_service_status(service)
    if not status["available"]:
        return [
            f"当前版本: {version}",
            "后台运行: 当前系统不可读取 systemd 状态",
            "开机自启: 当前系统不可读取 systemd 状态",
            "内存占用: 不可用",
        ]
    active = "运行中" if status["active"] == "active" else f"未运行 ({status['active']})"
    enabled = "已启用" if status["enabled"] == "enabled" else f"未启用 ({status['enabled']})"
    memory = human_size(status["memory"]) if status["memory"] is not None else "不可用"
    pid = f" PID {status['pid']}" if status["pid"] else ""
    return [
        f"当前版本: {version}",
        f"后台运行: {active}{pid}",
        f"开机自启: {enabled}",
        f"内存占用: {memory}",
    ]


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def print_header(title: str) -> None:
    if os.name == "nt":
        os.system("cls")
    elif os.environ.get("TERM"):
        os.system("clear")
    print(title)
    print("=" * len(title))


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def now_ts() -> int:
    return int(time.time())
