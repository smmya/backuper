from __future__ import annotations

import argparse
import datetime as dt
import getpass
import ipaddress
import os
import subprocess
import threading
import time
from pathlib import Path

from . import __version__
from .common import (
    APP_ROOT,
    CERT_DIR,
    CONFIG_DIR,
    confirm,
    detect_connect_host,
    dir_size,
    encode_code,
    ensure_dirs,
    human_size,
    now_ts,
    pause,
    print_header,
    prompt,
    random_secret,
    read_json,
    run,
    service_status_lines,
    systemd_service_status,
    write_json,
)
from .webdav_server import serve as webdav_serve


SERVER_CONFIG = CONFIG_DIR / "server.json"
CLOUD_SYNC_LOCK = threading.Lock()


def default_config() -> dict:
    return {
        "server_name": "backuper-server",
        "storage_root": str(APP_ROOT / "storage"),
        "bind_host": "0.0.0.0",
        "http_enabled": False,
        "http_port": 8080,
        "https_enabled": True,
        "https_port": 8443,
        "transport_mode": "https",
        "allow_download": False,
        "require_program_access": True,
        "program_access_token": "",
        "users": {},
        "cloud_sync": cloud_defaults(),
    }


def cloud_defaults() -> dict:
    return {
        "enabled": False,
        "remote_name": "",
        "remote_path": "",
        "target": "",
        "rclone_config": "",
        "schedule": {"type": "manual", "value": ""},
        "last_run": None,
        "last_run_ts": 0,
        "last_date": "",
        "last_status": "never",
        "last_message": "",
    }


def load_config() -> dict:
    ensure_dirs()
    cfg = default_config()
    data = read_json(SERVER_CONFIG, {})
    cfg.update(data)
    changed = False
    if not data.get("program_access_token"):
        cfg["program_access_token"] = random_secret()
        changed = True
    cloud = cloud_defaults()
    cloud.update(cfg.get("cloud_sync", {}))
    if isinstance(cloud.get("schedule"), str):
        cloud["schedule"] = {"type": cloud.get("schedule") or "manual", "value": ""}
    if cloud.get("remote") and not cloud.get("target"):
        cloud["target"] = cloud.get("remote")
    cfg["cloud_sync"] = cloud
    if changed and SERVER_CONFIG.exists():
        write_json(SERVER_CONFIG, cfg)
    return cfg


def save_config(config: dict) -> None:
    write_json(SERVER_CONFIG, config)


def ensure_initialized() -> dict:
    cfg = load_config()
    changed = False
    if not cfg.get("program_access_token"):
        cfg["program_access_token"] = random_secret()
        changed = True
    Path(cfg["storage_root"]).mkdir(parents=True, exist_ok=True)
    if changed or not SERVER_CONFIG.exists():
        save_config(cfg)
    return cfg


def cert_paths() -> tuple[Path, Path]:
    return CERT_DIR / "server.crt", CERT_DIR / "server.key"


def cert_meta_path() -> Path:
    return CERT_DIR / "server-cert.json"


def _san_for_host(host: str) -> str:
    try:
        ipaddress.ip_address(host)
        return f"IP:{host}"
    except ValueError:
        return f"DNS:{host}"


def ensure_self_signed_cert(host: str | None = None) -> bool:
    cert, key = cert_paths()
    requested_hosts = ["localhost", "127.0.0.1"]
    if host and host not in requested_hosts:
        requested_hosts.append(host)
    meta = read_json(cert_meta_path(), {})
    existing_hosts = set(meta.get("hosts", []))
    if cert.exists() and key.exists() and set(requested_hosts).issubset(existing_hosts):
        return True
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    san = ",".join(_san_for_host(item) for item in requested_hosts)
    cmd = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "825",
        "-subj",
        f"/CN={host or 'backuper-local'}",
        "-addext",
        f"subjectAltName={san}",
        "-keyout",
        str(key),
        "-out",
        str(cert),
    ]
    try:
        proc = run(cmd)
        ok = proc.returncode == 0 and cert.exists() and key.exists()
        if ok:
            write_json(cert_meta_path(), {"hosts": requested_hosts})
        return ok
    except OSError:
        return False


def init_server() -> None:
    cfg = ensure_initialized()
    ok = ensure_self_signed_cert("localhost")
    print("初始化完成。")
    print(f"配置文件: {SERVER_CONFIG}")
    print(f"数据根目录: {cfg['storage_root']}")
    print("HTTPS 自签证书: 已生成" if ok else "HTTPS 自签证书: 生成失败，请安装 openssl 或切换 HTTP")


def run_server() -> None:
    cfg = ensure_initialized()
    start_cloud_scheduler()
    listeners: list[tuple[str, int, Path | None, Path | None]] = []
    if cfg.get("https_enabled", True):
        if not ensure_self_signed_cert("localhost"):
            raise SystemExit("无法生成 HTTPS 证书，请安装 openssl 或关闭 HTTPS。")
        cert, key = cert_paths()
        listeners.append(("https", int(cfg["https_port"]), cert, key))
    if cfg.get("http_enabled", False):
        listeners.append(("http", int(cfg["http_port"]), None, None))
    if not listeners:
        raise SystemExit("HTTP 和 HTTPS 都已关闭，至少需要启用一个监听端口。")
    for _, port, cert, key in listeners[:-1]:
        threading.Thread(
            target=webdav_serve,
            args=(load_config, cfg["bind_host"], port, cert, key),
            daemon=True,
        ).start()
    _, port, cert, key = listeners[-1]
    webdav_serve(load_config, cfg["bind_host"], port, cert, key)


def service_name() -> str:
    return "backuper-server.service"


def install_service() -> None:
    unit = f"""[Unit]
Description=Backuper Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={APP_ROOT}
ExecStart=/usr/bin/env python3 {APP_ROOT / "server"} serve
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""
    target = Path("/etc/systemd/system") / service_name()
    tmp = APP_ROOT / "var" / service_name()
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(unit, encoding="utf-8")
    for cmd in [
        privileged_args(["cp", str(tmp), str(target)]),
        privileged_args(["systemctl", "daemon-reload"]),
        privileged_args(["systemctl", "enable", "--now", service_name()]),
    ]:
        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            print("开机自启配置失败。")
            return
    print("已安装并启用开机自启，服务端后台服务已启动。")


def systemctl(action: str) -> None:
    proc = subprocess.run(privileged_args(["systemctl", action, service_name()]))
    if proc.returncode != 0:
        print("systemctl 执行失败；如果尚未安装服务，请先选择“生成开机自启配置”。")


def restart_service_if_running(reason: str) -> None:
    status = systemd_service_status(service_name())
    if not status.get("available"):
        print(f"{reason}已保存；当前系统不可读取 systemd 状态，请按需手动重启服务端。")
        return
    if status.get("active") != "active":
        print(f"{reason}已保存；后台服务当前未运行，无需自动重启。")
        return
    proc = subprocess.run(privileged_args(["systemctl", "restart", service_name()]))
    if proc.returncode == 0:
        print(f"{reason}已保存，服务端后台服务已自动重启。")
    else:
        print(f"{reason}已保存，但自动重启失败，请手动重启服务端。")


def privileged_args(args: list[str]) -> list[str]:
    if os.name == "nt":
        return args
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return args
    return ["sudo", *args]


def remove_service() -> None:
    target = Path("/etc/systemd/system") / service_name()
    for cmd in [
        privileged_args(["systemctl", "disable", "--now", service_name()]),
        privileged_args(["rm", "-f", str(target)]),
        privileged_args(["systemctl", "daemon-reload"]),
    ]:
        proc = subprocess.run(cmd)
        if proc.returncode != 0 and "disable" not in cmd:
            print("删除开机自启配置失败。")
            return
    print("已删除开机自启配置，并停止服务端后台服务。")


def background_service_menu() -> None:
    while True:
        print_header("Backuper Server - 后台服务")
        for line in service_status_lines(service_name(), __version__):
            print(line)
        print("")
        print("1. 安装/修复开机自启并启动")
        print("2. 启动后台服务")
        print("3. 停止后台服务")
        print("4. 重启后台服务")
        print("5. 删除开机自启并停止服务")
        print("0. 返回")
        choice = prompt("请选择")
        if choice == "1":
            install_service()
            pause()
        elif choice == "2":
            systemctl("start")
            pause()
        elif choice == "3":
            systemctl("stop")
            pause()
        elif choice == "4":
            systemctl("restart")
            pause()
        elif choice == "5":
            if confirm("确认删除开机自启并停止服务", False):
                remove_service()
                pause()
        elif choice == "0":
            return


def foreground_run_server() -> None:
    status = systemd_service_status(service_name())
    if status.get("active") == "active":
        print("后台服务已经在运行。请先停止后台服务，或继续使用后台服务。")
        pause()
        return
    run_server()


def user_menu() -> None:
    while True:
        print_header("Backuper Server - 账号管理")
        print_user_table(load_config())
        print("")
        print("1. 创建账号")
        print("2. 管理现有账号")
        print("3. 刷新列表")
        print("0. 返回")
        choice = prompt("请选择")
        if choice == "1":
            create_user()
        elif choice == "2":
            manage_existing_user()
        elif choice == "3":
            continue
        elif choice == "0":
            return


def create_user() -> None:
    cfg = load_config()
    username = prompt("用户名")
    if not username:
        return
    if username in cfg["users"]:
        print("账号已存在。")
        pause()
        return
    if confirm("是否自动生成随机密钥", True):
        password = random_secret()
    else:
        password = prompt_user_password()
        if not password:
            print("密码/密钥不能为空。")
            pause()
            return
    cfg["users"][username] = {"password": password, "enabled": True}
    (Path(cfg["storage_root"]) / username).mkdir(parents=True, exist_ok=True)
    save_config(cfg)
    print("账号已创建。")
    pause()


def prompt_user_password() -> str:
    first = getpass.getpass("请输入密码/密钥: ")
    second = getpass.getpass("请再次输入密码/密钥: ")
    if first != second:
        print("两次输入不一致。")
        return ""
    return first


def list_users() -> None:
    cfg = load_config()
    print_user_table(cfg)
    pause()


def user_rows(cfg: dict) -> list[tuple[str, dict]]:
    return sorted(cfg["users"].items(), key=lambda item: item[0])


def print_user_table(cfg: dict) -> None:
    root = Path(cfg["storage_root"])
    if not cfg["users"]:
        print("现有账号: 暂无")
        return
    print("现有账号:")
    for idx, (username, info) in enumerate(user_rows(cfg), 1):
        size = dir_size(root / username)
        status = "启用" if info.get("enabled", True) else "禁用"
        print(f"{idx}. {username} | {status} | {human_size(size)} | {root / username}")


def select_user(cfg: dict) -> str | None:
    rows = user_rows(cfg)
    if not rows:
        print("暂无账号。")
        pause()
        return None
    print_user_table(cfg)
    choice = prompt("请选择账号序号，或输入账号名")
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(rows):
            return rows[idx][0]
    except ValueError:
        pass
    if choice in cfg["users"]:
        return choice
    print("账号不存在。")
    pause()
    return None


def manage_existing_user() -> None:
    cfg = load_config()
    username = select_user(cfg)
    if not username:
        return
    while True:
        cfg = load_config()
        if username not in cfg["users"]:
            print("账号已不存在。")
            pause()
            return
        root = Path(cfg["storage_root"])
        info = cfg["users"][username]
        print_header(f"Backuper Server - 账号 {username}")
        print(f"状态: {'启用' if info.get('enabled', True) else '禁用'}")
        print(f"空间占用: {human_size(dir_size(root / username))}")
        print(f"保存目录: {root / username}")
        print("1. 启用账号")
        print("2. 禁用账号")
        print("3. 删除账号")
        print("4. 重新生成随机密钥")
        print("5. 手动设置密码/密钥")
        print("6. 生成客户端配置码")
        print("0. 返回")
        choice = prompt("请选择")
        if choice == "1":
            set_user_enabled(True, username)
        elif choice == "2":
            set_user_enabled(False, username)
        elif choice == "3":
            delete_user(username)
            return
        elif choice == "4":
            rotate_secret(username)
        elif choice == "5":
            set_user_password(username)
        elif choice == "6":
            generate_client_code(username)
        elif choice == "0":
            return


def set_user_enabled(enabled: bool, username: str | None = None) -> None:
    cfg = load_config()
    username = username or select_user(cfg)
    if not username:
        return
    cfg["users"][username]["enabled"] = enabled
    save_config(cfg)
    print("已更新账号状态。")
    pause()


def delete_user(username: str | None = None) -> None:
    cfg = load_config()
    username = username or select_user(cfg)
    if not username:
        return
    delete_data = confirm("是否同时删除该用户的数据", False)
    del cfg["users"][username]
    if delete_data:
        import shutil

        shutil.rmtree(Path(cfg["storage_root"]) / username, ignore_errors=True)
    save_config(cfg)
    print("账号已删除。")
    pause()


def rotate_secret(username: str | None = None) -> None:
    cfg = load_config()
    username = username or select_user(cfg)
    if not username:
        return
    cfg["users"][username]["password"] = random_secret()
    save_config(cfg)
    print("账号密钥已重新生成，旧配置码将失效。")
    pause()


def set_user_password(username: str | None = None) -> None:
    cfg = load_config()
    username = username or select_user(cfg)
    if not username:
        return
    password = prompt_user_password()
    if not password:
        pause()
        return
    cfg["users"][username]["password"] = password
    save_config(cfg)
    print("账号密码/密钥已更新，旧配置码将失效。")
    pause()


def generate_client_code(username: str | None = None) -> None:
    cfg = load_config()
    username = username or select_user(cfg)
    if not username:
        return
    host, source = detect_connect_host()
    print(f"检测到连接地址: {host} ({source})")
    if not confirm("是否使用该地址", True):
        host = prompt("请输入服务器地址", host)
    mode = "https" if cfg.get("https_enabled", True) else "http"
    port = int(cfg["https_port"] if mode == "https" else cfg["http_port"])
    ca_cert = ""
    if mode == "https":
        before = read_json(cert_meta_path(), {})
        if not ensure_self_signed_cert(host):
            print("无法为该地址生成 HTTPS 证书，请安装 openssl 或切换 HTTP 模式。")
            pause()
            return
        after = read_json(cert_meta_path(), {})
        if before and before != after:
            print("提示: HTTPS 证书已按新地址重新生成；如果接收服务正在运行，请重启服务。")
        cert, _ = cert_paths()
        if cert.exists():
            ca_cert = cert.read_text(encoding="utf-8")
    payload = {
        "v": 1,
        "protocol": "webdav",
        "mode": mode,
        "host": host,
        "port": port,
        "username": username,
        "password": cfg["users"][username]["password"],
        "remote_path": "/",
        "server_name": cfg.get("server_name", "backuper-server"),
        "program_access_token": cfg.get("program_access_token", ""),
        "tls": {"verify": mode == "https", "ca_cert": ca_cert},
    }
    print("\n客户端配置码:")
    print(encode_code(payload))
    print("\n警告: 该配置码包含账号密钥，请只通过可信渠道发送。")
    pause()


def settings_menu() -> None:
    cfg = load_config()
    before_listener = (
        bool(cfg.get("http_enabled", False)),
        int(cfg.get("http_port", 8080)),
        bool(cfg.get("https_enabled", True)),
        int(cfg.get("https_port", 8443)),
    )
    print_header("Backuper Server - 基础设置")
    print(f"1. 修改数据根目录: {cfg['storage_root']}")
    print(f"2. 切换 HTTP 监听: {'启用' if cfg.get('http_enabled', False) else '关闭'}")
    print(f"3. 修改 HTTP 端口: {cfg['http_port']}")
    print(f"4. 切换 HTTPS 监听: {'启用' if cfg.get('https_enabled', True) else '关闭'}")
    print(f"5. 修改 HTTPS 端口: {cfg['https_port']}")
    print(f"6. 修改客户端下载权限: {'允许' if cfg.get('allow_download', False) else '禁止'}")
    print(f"7. 修改普通请求访问限制: {'仅允许 Backuper 客户端' if cfg.get('require_program_access', True) else '允许普通 WebDAV 请求'}")
    print("0. 返回")
    choice = prompt("请选择")
    if choice == "1":
        cfg["storage_root"] = str(Path(prompt("新的数据根目录", cfg["storage_root"])).expanduser())
        Path(cfg["storage_root"]).mkdir(parents=True, exist_ok=True)
    elif choice == "2":
        cfg["http_enabled"] = not bool(cfg.get("http_enabled", False))
    elif choice == "3":
        cfg["http_port"] = int(prompt("HTTP 端口", str(cfg["http_port"])))
    elif choice == "4":
        cfg["https_enabled"] = not bool(cfg.get("https_enabled", True))
    elif choice == "5":
        cfg["https_port"] = int(prompt("HTTPS 端口", str(cfg["https_port"])))
    elif choice == "6":
        cfg["allow_download"] = confirm("是否允许客户端下载文件内容", bool(cfg.get("allow_download", False)))
    elif choice == "7":
        cfg["require_program_access"] = confirm("是否要求 Backuper 程序访问令牌", bool(cfg.get("require_program_access", True)))
    if not cfg.get("http_enabled", False) and not cfg.get("https_enabled", True):
        print("HTTP 和 HTTPS 不能同时关闭，已重新启用 HTTPS。")
        cfg["https_enabled"] = True
    save_config(cfg)
    after_listener = (
        bool(cfg.get("http_enabled", False)),
        int(cfg.get("http_port", 8080)),
        bool(cfg.get("https_enabled", True)),
        int(cfg.get("https_port", 8443)),
    )
    if choice in {"2", "3", "4", "5"} and before_listener != after_listener:
        restart_service_if_running("监听端口配置")


def usage() -> None:
    cfg = load_config()
    root = Path(cfg["storage_root"])
    print(f"总占用: {human_size(dir_size(root))}")
    for username in cfg["users"]:
        print(f"- {username}: {human_size(dir_size(root / username))}")
    pause()


def rclone_config_file() -> str:
    try:
        proc = subprocess.run(["rclone", "config", "file"], text=True, capture_output=True)
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.endswith(".conf") or "/" in line:
            return line
    return ""


def rclone_cloud_args(cloud: dict) -> list[str]:
    args = ["rclone"]
    if cloud.get("rclone_config"):
        args.extend(["--config", cloud["rclone_config"]])
    return args


def list_rclone_remotes(cloud: dict | None = None) -> list[str]:
    cloud = cloud or {}
    try:
        proc = subprocess.run(rclone_cloud_args(cloud) + ["listremotes"], text=True, capture_output=True)
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def cloud_target(remote_name: str, remote_path: str) -> str:
    remote_name = remote_name.strip()
    if remote_name and not remote_name.endswith(":"):
        remote_name += ":"
    rel = remote_path.strip()
    if rel.startswith("/"):
        return remote_name + rel
    rel = rel.strip("/")
    return remote_name + rel if rel else remote_name


def format_cloud_schedule(schedule: dict) -> str:
    stype = schedule.get("type", "manual")
    value = schedule.get("value", "")
    return {
        "manual": "仅手动同步",
        "interval": f"每隔 {value}",
        "daily": f"每天 {value}",
        "weekly": f"每周 {value}",
        "monthly": f"每月 {value}",
    }.get(stype, f"{stype} {value}")


def parse_interval(value: str) -> int:
    value = value.strip().lower()
    number = int(value[:-1])
    unit = value[-1]
    if unit == "m":
        return number * 60
    if unit == "h":
        return number * 3600
    if unit == "d":
        return number * 86400
    raise ValueError("间隔格式应为 30m / 6h / 1d")


def cloud_due_to_run(cloud: dict) -> bool:
    if not cloud.get("enabled") or not cloud.get("target"):
        return False
    schedule = cloud.get("schedule", {"type": "manual"})
    stype = schedule.get("type", "manual")
    if stype == "manual":
        return False
    last_run = int(cloud.get("last_run_ts", 0))
    now = dt.datetime.now()
    if stype == "interval":
        return now_ts() - last_run >= parse_interval(schedule.get("value", "1d"))
    if cloud.get("last_date") == now.strftime("%Y-%m-%d"):
        return False
    if stype == "daily":
        hh, mm = map(int, schedule.get("value", "02:00").split(":"))
        return now.hour > hh or (now.hour == hh and now.minute >= mm)
    if stype == "weekly":
        day, value = schedule.get("value", "sun 02:00").split()
        weekdays = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        if now.weekday() != weekdays.get(day.lower(), 6):
            return False
        hh, mm = map(int, value.split(":"))
        return now.hour > hh or (now.hour == hh and now.minute >= mm)
    if stype == "monthly":
        day, value = schedule.get("value", "1 02:00").split()
        if now.day != int(day):
            return False
        hh, mm = map(int, value.split(":"))
        return now.hour > hh or (now.hour == hh and now.minute >= mm)
    return False


def run_cloud_sync_once(config: dict | None = None, quiet: bool = False) -> bool:
    cfg = config or load_config()
    cloud = cfg.setdefault("cloud_sync", cloud_defaults())
    target = cloud.get("target") or cloud_target(cloud.get("remote_name", ""), cloud.get("remote_path", ""))
    if not target:
        if not quiet:
            print("尚未设置云同步目标。")
        return False
    if not CLOUD_SYNC_LOCK.acquire(blocking=False):
        if not quiet:
            print("已有云同步正在运行。")
        return False
    try:
        args = rclone_cloud_args(cloud) + ["sync", cfg["storage_root"], target]
        proc = subprocess.run(args, text=True, capture_output=True)
        cfg = load_config()
        cloud = cfg.setdefault("cloud_sync", cloud_defaults())
        now_text = dt.datetime.now().isoformat(timespec="seconds")
        cloud["last_run"] = now_text
        cloud["last_run_ts"] = now_ts()
        cloud["last_date"] = dt.datetime.now().strftime("%Y-%m-%d")
        cloud["last_status"] = "success" if proc.returncode == 0 else "failed"
        cloud["last_message"] = (proc.stderr or proc.stdout).strip()[-1000:]
        save_config(cfg)
        if not quiet:
            print("云同步成功。" if proc.returncode == 0 else "云同步失败。")
            if cloud["last_message"]:
                print(cloud["last_message"])
        return proc.returncode == 0
    finally:
        CLOUD_SYNC_LOCK.release()


def cloud_scheduler_loop() -> None:
    while True:
        try:
            cfg = load_config()
            cloud = cfg.get("cloud_sync", {})
            if cloud_due_to_run(cloud):
                run_cloud_sync_once(cfg, quiet=True)
        except Exception as exc:
            cfg = load_config()
            cloud = cfg.setdefault("cloud_sync", cloud_defaults())
            cloud["last_status"] = "failed"
            cloud["last_message"] = f"调度器错误: {exc}"
            save_config(cfg)
        time.sleep(30)


def start_cloud_scheduler() -> None:
    thread = threading.Thread(target=cloud_scheduler_loop, name="cloud-sync", daemon=True)
    thread.start()


def set_cloud_target(cloud: dict) -> None:
    detected_config = rclone_config_file()
    if detected_config:
        print(f"检测到 rclone 配置文件: {detected_config}")
        if confirm("是否让后台服务使用这个 rclone 配置文件", True):
            cloud["rclone_config"] = detected_config
    elif not cloud.get("rclone_config"):
        print("未检测到 rclone 配置文件；如后台服务使用 root 运行，建议手动指定配置文件路径。")
    if confirm("是否手动指定/修改 rclone 配置文件路径", False):
        cloud["rclone_config"] = prompt("rclone 配置文件路径", cloud.get("rclone_config", ""))

    remotes = list_rclone_remotes(cloud)
    if remotes:
        print("\n当前 rclone 目标:")
        for idx, remote in enumerate(remotes, 1):
            print(f"{idx}. {remote}")
        choice = prompt("请选择目标编号，或直接输入 remote 名称")
        try:
            remote_name = remotes[int(choice) - 1]
        except (ValueError, IndexError):
            remote_name = choice
    else:
        print("没有读取到 rclone remote，请先运行 rclone config，或手动输入 remote 名称。")
        remote_name = prompt("remote 名称，例如 myremote:")
    remote_path = prompt("云端相对路径，例如 backuper/server1；留空表示 remote 根目录", cloud.get("remote_path", ""))
    cloud["remote_name"] = remote_name if remote_name.endswith(":") else remote_name + ":"
    cloud["remote_path"] = remote_path.strip()
    cloud["target"] = cloud_target(cloud["remote_name"], cloud["remote_path"])
    cloud["enabled"] = bool(cloud["target"])


def set_cloud_schedule(cloud: dict) -> None:
    print("1. 仅手动同步")
    print("2. 每隔一段时间同步")
    print("3. 每天固定时间同步")
    print("4. 每周固定时间同步")
    print("5. 每月固定时间同步")
    choice = prompt("请选择")
    if choice == "1":
        cloud["schedule"] = {"type": "manual", "value": ""}
    elif choice == "2":
        cloud["schedule"] = {"type": "interval", "value": prompt("间隔，例如 6h / 30m / 1d", "6h")}
    elif choice == "3":
        cloud["schedule"] = {"type": "daily", "value": prompt("时间，例如 02:30", "02:30")}
    elif choice == "4":
        cloud["schedule"] = {"type": "weekly", "value": prompt("星期和时间，例如 sun 03:00", "sun 03:00")}
    elif choice == "5":
        cloud["schedule"] = {"type": "monthly", "value": prompt("日期和时间，例如 1 04:00", "1 04:00")}


def cloud_menu() -> None:
    while True:
        cfg = load_config()
        cloud = cfg.setdefault("cloud_sync", cloud_defaults())
        print_header("Backuper Server - 云同步")
        print(f"当前目标: {cloud.get('target') or '未设置'}")
        print(f"rclone 配置: {cloud.get('rclone_config') or '默认'}")
        print(f"自动同步: {'启用' if cloud.get('enabled') else '关闭'}")
        print(f"同步计划: {format_cloud_schedule(cloud.get('schedule', {}))}")
        print(f"最近同步: {cloud.get('last_run') or '无'}")
        print(f"最近状态: {cloud.get('last_status') or 'never'}")
        if cloud.get("last_message"):
            print(f"最近信息: {cloud['last_message'][:200]}")
        print("1. 从本机 rclone 配置选择云目标")
        print("2. 设置自动同步计划")
        print("3. 启用自动同步")
        print("4. 关闭自动同步")
        print("5. 立即同步")
        print("6. 查看当前 rclone 目标列表")
        print("0. 返回")
        choice = prompt("请选择")
        if choice == "1":
            set_cloud_target(cloud)
            save_config(cfg)
        elif choice == "2":
            set_cloud_schedule(cloud)
            save_config(cfg)
        elif choice == "3":
            cloud["enabled"] = True
            save_config(cfg)
        elif choice == "4":
            cloud["enabled"] = False
            save_config(cfg)
        elif choice == "5":
            run_cloud_sync_once(cfg)
            pause()
        elif choice == "6":
            remotes = list_rclone_remotes(cloud)
            if remotes:
                for remote in remotes:
                    print(f"- {remote}")
            else:
                print("没有读取到 rclone remote。")
            pause()
        elif choice == "0":
            return


def interactive() -> None:
    ensure_initialized()
    while True:
        cfg = load_config()
        print_header("Backuper Server")
        for line in service_status_lines(service_name(), __version__):
            print(line)
        print("")
        print(f"数据根目录: {cfg['storage_root']}")
        http_state = f"HTTP:{cfg['http_port']}" if cfg.get("http_enabled", False) else "HTTP:关闭"
        https_state = f"HTTPS:{cfg['https_port']}" if cfg.get("https_enabled", True) else "HTTPS:关闭"
        print(f"监听端口: {http_state} / {https_state}")
        print("1. 初始化/修复配置")
        print("2. 前台启动接收服务")
        print("3. 后台服务/开机自启")
        print("4. 基础设置")
        print("5. 账号管理")
        print("6. 查看空间占用")
        print("7. 云同步设置")
        print("0. 退出")
        choice = prompt("请选择")
        if choice == "1":
            init_server()
            pause()
        elif choice == "2":
            foreground_run_server()
        elif choice == "3":
            background_service_menu()
        elif choice == "4":
            settings_menu()
        elif choice == "5":
            user_menu()
        elif choice == "6":
            usage()
        elif choice == "7":
            cloud_menu()
        elif choice == "0":
            return


def main() -> None:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("command", nargs="?", choices=["serve", "init"])
    args = parser.parse_args()
    if args.command == "serve":
        run_server()
    elif args.command == "init":
        init_server()
    else:
        interactive()
