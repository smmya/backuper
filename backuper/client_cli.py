from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import __version__
from .common import (
    APP_ROOT,
    CONFIG_DIR,
    LOG_DIR,
    STATE_DIR,
    TMP_DIR,
    confirm,
    decode_code,
    ensure_dirs,
    now_ts,
    pause,
    print_header,
    prompt,
    read_json,
    service_status_lines,
    write_json,
)


CLIENT_CONFIG = CONFIG_DIR / "client.json"
CLIENT_STATE = STATE_DIR / "client-state.json"


def default_config() -> dict:
    return {
        "servers": [],
        "schedule": {"type": "manual", "value": ""},
        "transfer": {
            "bwlimit": "",
            "retries": 5,
            "low_level_retries": 20,
            "timeout": "5m",
            "transfers": 2,
            "checkers": 4,
        },
        "failure_policy": "continue",
        "jobs": [],
    }


def load_config() -> dict:
    ensure_dirs()
    cfg = default_config()
    data = read_json(CLIENT_CONFIG, {})
    cfg.update(data)
    if cfg.get("server") and not cfg.get("servers"):
        server = cfg.pop("server")
        server.setdefault("name", server.get("server_name") or f"{server.get('username', 'user')}@{server.get('host', 'server')}:{server.get('port', '')}")
        cfg["servers"] = [server]
        for job in cfg.get("jobs", []):
            job.setdefault("server", server["name"])
    cfg.pop("server", None)
    return cfg


def save_config(config: dict) -> None:
    write_json(CLIENT_CONFIG, config)


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    with (LOG_DIR / "client.log").open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {message}\n")


def server_display_name(server: dict) -> str:
    return server.get("name") or f"{server.get('username', '-') }@{server.get('host', '-') }:{server.get('port', '-')}"


def make_server_name(server: dict, cfg: dict) -> str:
    base = server.get("name") or server.get("server_name") or f"{server.get('username', 'user')}@{server.get('host', 'server')}:{server.get('port', '')}"
    base = base.strip() or "server"
    names = {item.get("name") for item in cfg.get("servers", [])}
    if base not in names:
        return base
    idx = 2
    while f"{base}-{idx}" in names:
        idx += 1
    return f"{base}-{idx}"


def server_rows(cfg: dict) -> list[dict]:
    return cfg.get("servers", [])


def print_server_table(cfg: dict) -> None:
    rows = server_rows(cfg)
    if not rows:
        print("现有服务器: 暂无")
        return
    print("现有服务器:")
    for idx, server in enumerate(rows, 1):
        print(f"{idx}. {server_display_name(server)} | {server.get('mode', 'https')} | {server.get('host')}:{server.get('port')} | {server.get('username')}")


def select_server(cfg: dict) -> tuple[int, dict] | tuple[None, None]:
    rows = server_rows(cfg)
    if not rows:
        print("暂无服务器连接。")
        pause()
        return None, None
    print_server_table(cfg)
    choice = prompt("请选择服务器序号，或输入连接名")
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(rows):
            return idx, rows[idx]
    except ValueError:
        pass
    for idx, server in enumerate(rows):
        if server.get("name") == choice:
            return idx, server
    print("服务器连接不存在。")
    pause()
    return None, None


def import_server_code() -> None:
    code = prompt("请粘贴服务端配置码")
    try:
        server = server_from_code(code)
    except Exception as exc:
        print(f"解析失败: {exc}")
        pause()
        return
    cfg = load_config()
    server["name"] = prompt("本地连接名称", make_server_name(server, cfg))
    edit_server_config(server, "Backuper Client - 导入服务器配置", None)


def server_from_code(code: str) -> dict:
    payload = decode_code(code)
    return {
        "protocol": payload.get("protocol", "webdav"),
        "mode": payload.get("mode", "https"),
        "host": payload.get("host", ""),
        "port": payload.get("port", 443),
        "username": payload.get("username", ""),
        "password": payload.get("password", ""),
        "remote_path": payload.get("remote_path", "/"),
        "server_name": payload.get("server_name", ""),
        "program_access_token": payload.get("program_access_token", ""),
        "tls": payload.get("tls", {}),
    }


def refresh_server_from_code(idx: int, current: dict) -> None:
    code = prompt("请粘贴新的服务端配置码")
    try:
        incoming = server_from_code(code)
    except Exception as exc:
        print(f"解析失败: {exc}")
        pause()
        return
    incoming["name"] = current.get("name") or incoming.get("name")
    print_header("Backuper Client - 刷新服务器配置")
    print("当前配置:")
    show_server_config_inline(current)
    print("")
    print("配置码内容:")
    show_server_config_inline(incoming)
    print("")
    if confirm("是否用配置码刷新该服务器连接", True):
        edit_server_config(incoming, "Backuper Client - 刷新服务器配置", idx)


def save_server_config(server: dict, idx: int | None = None) -> None:
    cfg = load_config()
    if not server.get("name"):
        server["name"] = make_server_name(server, cfg)
    if any(item.get("name") == server["name"] for i, item in enumerate(cfg.get("servers", [])) if idx is None or i != idx):
        print("连接名称已存在，请换一个名称。")
        pause()
        return
    if idx is None:
        cfg.setdefault("servers", []).append(server)
    else:
        old_name = cfg["servers"][idx].get("name")
        cfg["servers"][idx] = server
        if old_name and old_name != server["name"]:
            for job in cfg.get("jobs", []):
                if job.get("server") == old_name:
                    job["server"] = server["name"]
    save_config(cfg)


def edit_server_config(server: dict, title: str, idx: int | None = None) -> None:
    while True:
        print_header(title)
        print(f"连接名称: {server.get('name') or '-'}")
        print(f"服务器名称: {server.get('server_name') or '-'}")
        print(f"协议: {server.get('protocol', 'webdav')}")
        print(f"传输模式: {server.get('mode', 'https')}")
        print(f"地址: {server.get('host') or '-'}")
        print(f"端口: {server.get('port') or '-'}")
        print(f"账号: {server.get('username') or '-'}")
        print(f"远端路径: {server.get('remote_path') or '/'}")
        print(f"包含密钥: {'是' if server.get('password') else '否'}")
        print(f"程序访问令牌: {'已配置' if server.get('program_access_token') else '未配置'}")
        print(f"TLS 校验: {'启用' if server.get('tls', {}).get('verify', True) else '关闭'}")
        print(f"CA 证书: {'已配置' if server.get('tls', {}).get('ca_cert') else '未配置'}")
        print("1. 确认保存")
        print("2. 修改连接名称")
        print("3. 修改服务器地址")
        print("4. 修改端口")
        print("5. 修改传输模式")
        print("6. 修改账号")
        print("7. 修改密码/密钥")
        print("8. 修改远端路径")
        print("9. 修改程序访问令牌")
        print("10. 设置 TLS 校验/CA 证书")
        print("11. 测试连接")
        print("0. 取消")
        choice = prompt("请选择")
        if choice == "1":
            save_server_config(server, idx)
            print("服务器配置已保存。")
            pause()
            return
        if choice == "2":
            server["name"] = prompt("连接名称", server.get("name", ""))
        elif choice == "3":
            server["host"] = prompt("服务器地址", server.get("host", ""))
        elif choice == "4":
            server["port"] = int(prompt("端口", str(server.get("port", 8443))))
        elif choice == "5":
            mode = prompt("传输模式 http/https", server.get("mode", "https")).lower()
            if mode in ("http", "https"):
                server["mode"] = mode
        elif choice == "6":
            server["username"] = prompt("账号", server.get("username", ""))
        elif choice == "7":
            server["password"] = getpass.getpass("请输入密码/密钥: ")
        elif choice == "8":
            server["remote_path"] = prompt("远端路径", server.get("remote_path", "/"))
        elif choice == "9":
            server["program_access_token"] = getpass.getpass("请输入程序访问令牌；留空表示不使用: ")
        elif choice == "10":
            tls = server.setdefault("tls", {})
            tls["verify"] = confirm("是否启用 TLS 证书校验", bool(tls.get("verify", server.get("mode") == "https")))
            if tls["verify"]:
                ca_path = prompt("CA 证书文件路径；留空使用系统证书", "")
                if ca_path:
                    try:
                        tls["ca_cert"] = Path(ca_path).expanduser().read_text(encoding="utf-8")
                    except OSError as exc:
                        print(f"读取 CA 证书失败: {exc}")
                        pause()
            else:
                tls["ca_cert"] = ""
        elif choice == "11":
            test_server(server)
            pause()
        elif choice == "0":
            return


def manual_server_config() -> None:
    mode = prompt("传输模式 http/https", "https").lower()
    if mode not in ("http", "https"):
        mode = "https"
    server = {
        "protocol": "webdav",
        "mode": mode,
        "host": prompt("服务器地址"),
        "port": int(prompt("端口", "8443" if mode == "https" else "8080")),
        "username": prompt("账号"),
        "password": getpass.getpass("请输入密码/密钥: "),
        "remote_path": prompt("远端路径", "/"),
        "server_name": prompt("服务器名称，可留空", ""),
        "program_access_token": getpass.getpass("请输入程序访问令牌；留空表示不使用: "),
        "tls": {"verify": True, "ca_cert": ""},
    }
    cfg = load_config()
    server["name"] = prompt("本地连接名称", make_server_name(server, cfg))
    if server["mode"] == "https":
        server["tls"]["verify"] = confirm("是否启用 TLS 证书校验", True)
        if server["tls"]["verify"]:
            ca_path = prompt("CA 证书文件路径；留空使用系统证书", "")
            if ca_path:
                try:
                    server["tls"]["ca_cert"] = Path(ca_path).expanduser().read_text(encoding="utf-8")
                except OSError as exc:
                    print(f"读取 CA 证书失败: {exc}")
                    pause()
    edit_server_config(server, "Backuper Client - 手动服务器配置", None)


def show_server_config(server: dict) -> None:
    show_server_config_inline(server)
    pause()


def show_server_config_inline(server: dict) -> None:
    print(f"连接名称: {server.get('name') or '-'}")
    print(f"服务器名称: {server.get('server_name') or '-'}")
    print(f"协议: {server.get('protocol', 'webdav')}")
    print(f"传输模式: {server.get('mode', 'https')}")
    print(f"地址: {server.get('host')}")
    print(f"端口: {server.get('port')}")
    print(f"账号: {server.get('username')}")
    print(f"远端路径: {server.get('remote_path', '/')}")
    print(f"包含密钥: {'是' if server.get('password') else '否'}")
    print(f"程序访问令牌: {'已配置' if server.get('program_access_token') else '未配置'}")
    print(f"TLS 校验: {'启用' if server.get('tls', {}).get('verify', True) else '关闭'}")
    print(f"CA 证书: {'已配置' if server.get('tls', {}).get('ca_cert') else '未配置'}")


def manage_existing_server() -> None:
    cfg = load_config()
    idx, server = select_server(cfg)
    if server is None:
        return
    while True:
        cfg = load_config()
        if idx >= len(cfg.get("servers", [])):
            return
        server = cfg["servers"][idx]
        print_header(f"Backuper Client - 服务器 {server_display_name(server)}")
        print(f"地址: {server.get('host')}:{server.get('port')}")
        print(f"账号: {server.get('username')}")
        print("1. 修改配置")
        print("2. 查看配置")
        print("3. 测试连接")
        print("4. 通过配置码刷新证书/参数")
        print("5. 删除连接")
        print("0. 返回")
        choice = prompt("请选择")
        if choice == "1":
            edit_server_config(dict(server), "Backuper Client - 修改服务器配置", idx)
        elif choice == "2":
            show_server_config(server)
        elif choice == "3":
            test_server(server)
            pause()
        elif choice == "4":
            refresh_server_from_code(idx, dict(server))
        elif choice == "5":
            if any(job.get("server") == server.get("name") for job in cfg.get("jobs", [])):
                print("该服务器仍被任务使用，不能删除。")
                pause()
            elif confirm("确认删除该服务器连接", False):
                cfg["servers"].pop(idx)
                save_config(cfg)
                return
        elif choice == "0":
            return


def server_connection_menu() -> None:
    while True:
        cfg = load_config()
        print_header("Backuper Client - 服务器连接配置")
        print_server_table(cfg)
        print("")
        print("1. 导入服务端配置码")
        print("2. 手动添加服务器")
        print("3. 管理现有服务器")
        print("4. 测试某个服务器")
        print("0. 返回")
        choice = prompt("请选择")
        if choice == "1":
            import_server_code()
        elif choice == "2":
            manual_server_config()
        elif choice == "3":
            manage_existing_server()
        elif choice == "4":
            _, server = select_server(load_config())
            if server:
                test_server(server)
                pause()
        elif choice == "0":
            return


def write_ca_cert(server: dict) -> Path | None:
    ca = server.get("tls", {}).get("ca_cert")
    if not ca:
        return None
    path = CONFIG_DIR / f"ca-{server.get('username', 'server')}.pem"
    path.write_text(ca, encoding="utf-8")
    return path


def rclone_obscure(password: str) -> str:
    try:
        proc = subprocess.run(["rclone", "obscure", password], text=True, capture_output=True)
        if proc.returncode == 0:
            return proc.stdout.strip()
    except OSError:
        pass
    return password


def make_rclone_config(server: dict, temp_dir: Path) -> tuple[Path, list[str]]:
    url = f"{server.get('mode', 'https')}://{server['host']}:{server['port']}{server.get('remote_path', '/')}"
    cfg_path = temp_dir / "rclone.conf"
    pass_value = rclone_obscure(server.get("password", ""))
    cfg_path.write_text(
        "\n".join(
            [
                "[backuper]",
                "type = webdav",
                f"url = {url}",
                "vendor = other",
                f"user = {server.get('username', '')}",
                f"pass = {pass_value}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    extra: list[str] = []
    ca = server.get("tls", {}).get("ca_cert")
    if ca:
        ca_path = temp_dir / "server-ca.pem"
        ca_path.write_text(ca, encoding="utf-8")
        extra.extend(["--ca-cert", str(ca_path)])
    if server.get("mode") == "https" and not server.get("tls", {}).get("verify", True):
        extra.append("--no-check-certificate")
    return cfg_path, extra


def rclone_base_args(cfg: dict, temp_dir: Path, server: dict) -> list[str]:
    rcfg, extra = make_rclone_config(server, temp_dir)
    transfer = cfg.get("transfer", {})
    args = [
        "rclone",
        "--config",
        str(rcfg),
        "--retries",
        str(transfer.get("retries", 5)),
        "--low-level-retries",
        str(transfer.get("low_level_retries", 20)),
        "--timeout",
        str(transfer.get("timeout", "5m")),
        "--transfers",
        str(transfer.get("transfers", 2)),
        "--checkers",
        str(transfer.get("checkers", 4)),
    ]
    if transfer.get("bwlimit"):
        args.extend(["--bwlimit", transfer["bwlimit"]])
    if server.get("program_access_token"):
        args.extend(["--header", f"X-Backuper-Access: {server['program_access_token']}"])
    args.extend(extra)
    return args


def test_server(server: dict | None = None) -> bool:
    cfg = load_config()
    if server is None:
        if not cfg.get("servers"):
            print("尚未配置服务器。")
            return False
        server = cfg["servers"][0]
    if not server:
        print("尚未配置服务器。")
        return False
    with tempfile.TemporaryDirectory(dir=TMP_DIR) as td:
        args = rclone_base_args(cfg, Path(td), server) + ["lsd", "backuper:"]
        proc = subprocess.run(args)
        print("连接测试成功。" if proc.returncode == 0 else "连接测试失败。")
        return proc.returncode == 0


def schedule_menu() -> None:
    cfg = load_config()
    while True:
        print_header("Backuper Client - 全局备份计划")
        print(f"当前计划: {format_schedule(cfg['schedule'])}")
        print("1. 仅手动执行")
        print("2. 每隔一段时间执行")
        print("3. 每天固定时间执行")
        print("4. 每周固定时间执行")
        print("5. 每月固定时间执行")
        print("0. 返回")
        choice = prompt("请选择")
        if choice == "1":
            cfg["schedule"] = {"type": "manual", "value": ""}
        elif choice == "2":
            cfg["schedule"] = {"type": "interval", "value": prompt("间隔，例如 6h / 30m / 1d", "6h")}
        elif choice == "3":
            cfg["schedule"] = {"type": "daily", "value": prompt("时间，例如 02:30", "02:30")}
        elif choice == "4":
            cfg["schedule"] = {"type": "weekly", "value": prompt("星期和时间，例如 sun 03:00", "sun 03:00")}
        elif choice == "5":
            cfg["schedule"] = {"type": "monthly", "value": prompt("日期和时间，例如 1 04:00", "1 04:00")}
        elif choice == "0":
            save_config(cfg)
            return
        save_config(cfg)


def format_schedule(schedule: dict) -> str:
    t = schedule.get("type", "manual")
    v = schedule.get("value", "")
    return {
        "manual": "仅手动执行",
        "interval": f"每隔 {v}",
        "daily": f"每天 {v}",
        "weekly": f"每周 {v}",
        "monthly": f"每月 {v}",
    }.get(t, f"{t} {v}")


def jobs_menu() -> None:
    while True:
        cfg = load_config()
        print_header("Backuper Client - 备份目录管理")
        print_job_table(cfg)
        print("")
        print("1. 添加备份目录")
        print("2. 管理现有目录")
        print("3. 调整执行顺序")
        print("4. 刷新列表")
        print("0. 返回")
        choice = prompt("请选择")
        if choice == "1":
            add_job()
        elif choice == "2":
            manage_existing_job()
        elif choice == "3":
            reorder_jobs()
        elif choice == "4":
            continue
        elif choice == "0":
            return


def list_jobs() -> None:
    cfg = load_config()
    print_job_table(cfg)
    pause()


def job_state() -> dict:
    return read_json(CLIENT_STATE, {}).get("jobs", {})


def job_remote_root(job: dict) -> str:
    return str(job["name"]).strip("/")


def should_ignore_path(path: Path, root: Path, job: dict) -> bool:
    rel = path.relative_to(root) if path != root else Path(".")
    parts = rel.parts
    if parts and parts[0] in set(job.get("ignore_dirs", [])):
        return True
    suffixes = tuple(job.get("ignore_suffixes", []))
    return bool(suffixes and path.is_file() and path.name.endswith(suffixes))


def tar_exclude_args(job: dict, root_name: str) -> list[str]:
    args: list[str] = []
    for dirname in job.get("ignore_dirs", []):
        args.extend(["--exclude", f"{root_name}/{dirname}"])
    for suffix in job.get("ignore_suffixes", []):
        args.extend(["--exclude", f"*{suffix}"])
    return args


def rclone_filter_args(job: dict) -> list[str]:
    args: list[str] = []
    for dirname in job.get("ignore_dirs", []):
        args.extend(["--exclude", f"/{dirname}/**"])
    for suffix in job.get("ignore_suffixes", []):
        args.extend(["--exclude", f"*{suffix}"])
    return args


def validate_new_job_name(cfg: dict, name: str) -> bool:
    if not name:
        print("任务名称不能为空。")
        return False
    if "/" in name or "\\" in name:
        print("任务名称不能包含路径分隔符。")
        return False
    if any(job["name"] == name for job in cfg.get("jobs", [])):
        print("任务名称已存在，请换一个名称。")
        return False
    return True


def job_last_summary(job: dict, state: dict | None = None) -> str:
    state = state if state is not None else job_state()
    item = state.get(job["name"], {})
    if not item:
        return "从未备份"
    status = "成功" if item.get("status") == "success" else "失败"
    at = item.get("finished_at") or item.get("started_at") or "-"
    message = item.get("message") or ""
    if message:
        return f"{status} | {at} | {message[:60]}"
    return f"{status} | {at}"


def print_job_table(cfg: dict) -> None:
    if not cfg["jobs"]:
        print("现有备份目录: 暂无")
        return
    state = job_state()
    print("现有备份目录:")
    for idx, job in enumerate(cfg["jobs"], 1):
        print(
            f"{idx}. {job['name']} | {'启用' if job.get('enabled') else '关闭'} | "
            f"{job['mode']} | {job.get('server', '未绑定服务器')} | {job['path']} -> {job_remote_root(job)}"
        )
        print(f"   上次备份: {job_last_summary(job, state)}")


def add_job() -> None:
    cfg = load_config()
    _, server = select_server(cfg)
    if server is None:
        return
    name = prompt("任务名称")
    if not validate_new_job_name(cfg, name):
        pause()
        return
    path = prompt("本地目录")
    print("备份模式: 1. 打包上传  2. 同步")
    mode = "archive" if prompt("请选择", "1") == "1" else "sync"
    job = {
        "name": name,
        "path": path,
        "enabled": confirm("是否启用该目录", True),
        "mode": mode,
        "server": server["name"],
        "ignore_suffixes": [],
        "ignore_dirs": [],
    }
    if mode == "archive":
        job["archive_format"] = prompt("打包格式 tar.gz/tar.zst", "tar.gz")
        job["retention"] = int(prompt("保留最近几份备份", "7"))
    else:
        job["sync_delete"] = confirm("是否删除远端多余文件（完全镜像）", False)
    cfg["jobs"].append(job)
    save_config(cfg)
    print("备份目录已添加。")
    pause()


def select_job(cfg: dict) -> tuple[int, dict] | tuple[None, None]:
    if not cfg["jobs"]:
        print("暂无任务。")
        pause()
        return None, None
    print_job_table(cfg)
    choice = prompt("请选择任务序号，或输入任务名")
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(cfg["jobs"]):
            return idx, cfg["jobs"][idx]
    except ValueError:
        pass
    for idx, job in enumerate(cfg["jobs"]):
        if job["name"] == choice:
            return idx, job
    print("任务不存在。")
    pause()
    return None, None


def manage_existing_job() -> None:
    cfg = load_config()
    idx, job = select_job(cfg)
    if job is None:
        return
    name = job["name"]
    while True:
        cfg = load_config()
        found = next(((i, item) for i, item in enumerate(cfg["jobs"]) if item["name"] == name), None)
        if not found:
            print("任务已不存在。")
            pause()
            return
        idx, job = found
        print_header(f"Backuper Client - 任务 {job['name']}")
        print(f"状态: {'启用' if job.get('enabled') else '关闭'}")
        print(f"模式: {job['mode']}")
        print(f"服务器: {job.get('server', '未绑定服务器')}")
        print(f"本地目录: {job['path']}")
        print(f"远端目录: {job_remote_root(job)}")
        print(f"上次备份: {job_last_summary(job)}")
        print("1. 修改任务")
        print("2. 启用任务")
        print("3. 关闭任务")
        print("4. 删除任务")
        print("5. 立即执行该任务")
        print("6. 忽略规则")
        print("0. 返回")
        choice = prompt("请选择")
        if choice == "1":
            edit_job(idx)
            cfg = load_config()
            if idx < len(cfg["jobs"]):
                name = cfg["jobs"][idx]["name"]
        elif choice == "2":
            set_job_enabled(True, idx)
        elif choice == "3":
            set_job_enabled(False, idx)
        elif choice == "4":
            delete_job(idx)
            return
        elif choice == "5":
            run_job(cfg, job)
            pause()
        elif choice == "6":
            ignore_rules_menu(job["name"])
        elif choice == "0":
            return


def edit_job(idx: int | None = None) -> None:
    cfg = load_config()
    if idx is None:
        idx, job = select_job(cfg)
    else:
        job = cfg["jobs"][idx] if 0 <= idx < len(cfg["jobs"]) else None
    if job is None:
        return
    old_name = job["name"]
    new_name = prompt("任务名称", job["name"])
    if new_name != old_name and not validate_new_job_name(cfg, new_name):
        pause()
        return
    job["name"] = new_name
    job["path"] = prompt("本地目录", job["path"])
    if confirm("是否修改该任务绑定的服务器", False):
        _, server = select_server(cfg)
        if server is not None:
            job["server"] = server["name"]
    if job["mode"] == "archive":
        job["archive_format"] = prompt("打包格式", job.get("archive_format", "tar.gz"))
        job["retention"] = int(prompt("保留份数", str(job.get("retention", 7))))
    else:
        job["sync_delete"] = confirm("是否删除远端多余文件", bool(job.get("sync_delete", False)))
    cfg["jobs"][idx] = job
    save_config(cfg)
    if old_name != job["name"]:
        state = read_json(CLIENT_STATE, {})
        jobs = state.get("jobs", {})
        if old_name in jobs and job["name"] not in jobs:
            jobs[job["name"]] = jobs.pop(old_name)
            write_json(CLIENT_STATE, state)


def reorder_jobs() -> None:
    cfg = load_config()
    idx, job = select_job(cfg)
    if job is None:
        return
    new_pos = int(prompt("移动到第几个位置", str(idx + 1))) - 1
    new_pos = max(0, min(new_pos, len(cfg["jobs"]) - 1))
    cfg["jobs"].pop(idx)
    cfg["jobs"].insert(new_pos, job)
    save_config(cfg)


def set_job_enabled(enabled: bool, idx: int | None = None) -> None:
    cfg = load_config()
    if idx is None:
        _, job = select_job(cfg)
    else:
        job = cfg["jobs"][idx] if 0 <= idx < len(cfg["jobs"]) else None
    if job is None:
        return
    job["enabled"] = enabled
    save_config(cfg)


def delete_job(idx: int | None = None) -> None:
    cfg = load_config()
    if idx is None:
        idx, job = select_job(cfg)
    else:
        job = cfg["jobs"][idx] if 0 <= idx < len(cfg["jobs"]) else None
    if job is None:
        return
    if confirm(f"确认删除任务 {job['name']}", False):
        name = job["name"]
        cfg["jobs"].pop(idx)
        save_config(cfg)
        state = read_json(CLIENT_STATE, {})
        if state.get("jobs", {}).pop(name, None) is not None:
            write_json(CLIENT_STATE, state)


def ignore_rules_menu(job_name: str) -> None:
    while True:
        cfg = load_config()
        job = next((item for item in cfg.get("jobs", []) if item["name"] == job_name), None)
        if not job:
            print("任务不存在。")
            pause()
            return
        job.setdefault("ignore_suffixes", [])
        job.setdefault("ignore_dirs", [])
        print_header(f"Backuper Client - 忽略规则 {job_name}")
        print("后缀忽略:")
        print_rule_list(job["ignore_suffixes"])
        print("顶层目录忽略:")
        print_rule_list(job["ignore_dirs"])
        print("")
        print("1. 管理后缀忽略")
        print("2. 管理顶层目录忽略")
        print("0. 返回")
        choice = prompt("请选择")
        if choice == "1":
            manage_rule_list(job_name, "ignore_suffixes", "后缀，例如 .tmp 或 .log")
        elif choice == "2":
            manage_rule_list(job_name, "ignore_dirs", "顶层目录名，例如 cache")
        elif choice == "0":
            return


def print_rule_list(values: list[str]) -> None:
    if not values:
        print("  暂无")
        return
    for idx, value in enumerate(values, 1):
        print(f"  {idx}. {value}")


def manage_rule_list(job_name: str, key: str, prompt_text: str) -> None:
    while True:
        cfg = load_config()
        job = next((item for item in cfg.get("jobs", []) if item["name"] == job_name), None)
        if not job:
            return
        values = job.setdefault(key, [])
        print_header(f"Backuper Client - {prompt_text}")
        print_rule_list(values)
        print("")
        print("1. 添加规则")
        print("2. 删除规则")
        print("0. 返回")
        choice = prompt("请选择")
        if choice == "1":
            value = prompt(prompt_text).strip()
            if value and value not in values:
                values.append(value)
                save_config(cfg)
        elif choice == "2":
            if not values:
                pause()
                continue
            try:
                idx = int(prompt("请选择要删除的规则序号")) - 1
            except ValueError:
                continue
            if 0 <= idx < len(values):
                values.pop(idx)
                save_config(cfg)
        elif choice == "0":
            return


def transfer_menu() -> None:
    cfg = load_config()
    t = cfg["transfer"]
    print_header("Backuper Client - 传输参数")
    t["bwlimit"] = prompt("限速，例如 5M；留空不限速", t.get("bwlimit", ""))
    t["retries"] = int(prompt("重试次数", str(t.get("retries", 5))))
    t["low_level_retries"] = int(prompt("底层重试次数", str(t.get("low_level_retries", 20))))
    t["timeout"] = prompt("超时时间", t.get("timeout", "5m"))
    t["transfers"] = int(prompt("并发传输数", str(t.get("transfers", 2))))
    t["checkers"] = int(prompt("检查并发数", str(t.get("checkers", 4))))
    cfg["failure_policy"] = "stop" if confirm("任务失败时是否停止本轮备份", False) else "continue"
    save_config(cfg)


def export_config() -> None:
    cfg = load_config()
    data = {
        "version": 1,
        "schedule": cfg.get("schedule", {}),
        "transfer": cfg.get("transfer", {}),
        "failure_policy": cfg.get("failure_policy", "continue"),
        "jobs": cfg.get("jobs", []),
    }
    path = Path(prompt("导出文件路径", str(APP_ROOT / "client-export.json"))).expanduser()
    write_json(path, data)
    print(f"已导出: {path}")
    pause()


def import_config() -> None:
    path = Path(prompt("导入文件路径")).expanduser()
    data = read_json(path, None)
    if not data:
        print("配置文件读取失败。")
        pause()
        return
    print(f"检测到 {len(data.get('jobs', []))} 个备份任务。")
    for job in data.get("jobs", []):
        print(f"- {job.get('name')} | {job.get('mode')} | {job.get('path')}")
    if not confirm("确认导入这些非敏感配置", True):
        return
    seen: set[str] = set()
    normalized_jobs = []
    for job in data.get("jobs", []):
        name = job.get("name", "")
        if not name or "/" in name or "\\" in name or name in seen:
            print(f"导入失败，任务名称无效或重复: {name}")
            pause()
            return
        seen.add(name)
        normalized_jobs.append(dict(job))
    cfg = load_config()
    valid_servers = {server.get("name") for server in cfg.get("servers", [])}
    needs_rebind = any(job.get("server") not in valid_servers for job in normalized_jobs)
    if needs_rebind:
        if cfg.get("servers"):
            print("导入任务中的服务器绑定在当前客户端不存在，请选择这些任务要绑定的服务器。")
            _, server = select_server(cfg)
            if server is None:
                return
            for job in normalized_jobs:
                job["server"] = server["name"]
        else:
            print("当前尚未配置服务器。请先添加服务器连接，再导入任务配置。")
            pause()
            return
    cfg["schedule"] = data.get("schedule", cfg["schedule"])
    cfg["transfer"] = data.get("transfer", cfg["transfer"])
    cfg["failure_policy"] = data.get("failure_policy", cfg.get("failure_policy", "continue"))
    cfg["jobs"] = normalized_jobs
    save_config(cfg)
    print("已导入。服务器账号和密钥未被修改。")
    pause()


def config_menu() -> None:
    while True:
        print_header("Backuper Client - 导入/导出配置")
        print("1. 导出非敏感配置")
        print("2. 导入非敏感配置")
        print("0. 返回")
        choice = prompt("请选择")
        if choice == "1":
            export_config()
        elif choice == "2":
            import_config()
        elif choice == "0":
            return


def run_one_job_menu() -> None:
    cfg = load_config()
    _, job = select_job(cfg)
    if job:
        run_job(cfg, job)
        pause()


def run_all_jobs() -> bool:
    cfg = load_config()
    jobs = [job for job in cfg.get("jobs", []) if job.get("enabled", True)]
    if not jobs:
        log("没有启用的备份任务。")
        print("没有启用的备份任务。")
        return True
    ok = True
    for job in jobs:
        result = run_job(cfg, job)
        ok = ok and result
        if not result and cfg.get("failure_policy") == "stop":
            break
    return ok


def update_job_result(job: dict, status: str, started_at: str, message: str = "") -> None:
    state = read_json(CLIENT_STATE, {})
    jobs = state.setdefault("jobs", {})
    jobs[job["name"]] = {
        "status": status,
        "started_at": started_at,
        "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
        "message": message,
    }
    write_json(CLIENT_STATE, state)


def run_job(cfg: dict, job: dict) -> bool:
    started_at = dt.datetime.now().isoformat(timespec="seconds")
    server = server_for_job(cfg, job)
    if server is None:
        print("任务尚未绑定有效服务器。")
        update_job_result(job, "failed", started_at, "任务尚未绑定有效服务器")
        return False
    path = Path(job["path"]).expanduser()
    if not path.exists():
        log(f"任务 {job['name']} 失败: 路径不存在 {path}")
        print(f"路径不存在: {path}")
        update_job_result(job, "failed", started_at, f"路径不存在: {path}")
        return False
    log(f"开始任务 {job['name']}")
    try:
        with tempfile.TemporaryDirectory(dir=TMP_DIR) as td:
            temp_dir = Path(td)
            if job["mode"] == "archive":
                result = run_archive_job(cfg, job, path, temp_dir, server)
            else:
                result = run_sync_job(cfg, job, path, temp_dir, server)
    except Exception as exc:
        log(f"任务 {job['name']} 失败: {exc}")
        update_job_result(job, "failed", started_at, str(exc))
        return False
    log(f"任务 {job['name']} {'成功' if result else '失败'}")
    update_job_result(job, "success" if result else "failed", started_at)
    return result


def server_for_job(cfg: dict, job: dict) -> dict | None:
    wanted = job.get("server")
    for server in cfg.get("servers", []):
        if server.get("name") == wanted:
            return server
    if not wanted and len(cfg.get("servers", [])) == 1:
        return cfg["servers"][0]
    return None


def run_archive_job(cfg: dict, job: dict, path: Path, temp_dir: Path, server: dict) -> bool:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    ext = "tar.gz" if job.get("archive_format", "tar.gz") == "tar.gz" else "tar.zst"
    archive = temp_dir / f"{job['name']}-{stamp}.{ext}"
    excludes = tar_exclude_args(job, path.name)
    if ext == "tar.zst":
        cmd = [
            "tar",
            "--numeric-owner",
            "--xattrs",
            "--acls",
            *excludes,
            "-I",
            "zstd -19 -T0",
            "-cf",
            str(archive),
            "-C",
            str(path.parent),
            path.name,
        ]
    else:
        cmd = [
            "tar",
            "--numeric-owner",
            "--xattrs",
            "--acls",
            *excludes,
            "-I",
            "gzip -9",
            "-cf",
            str(archive),
            "-C",
            str(path.parent),
            path.name,
        ]
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        return False
    if not verify_archive(archive, ext):
        log(f"任务 {job['name']} 失败: 归档校验失败 {archive}")
        return False
    remote = f"backuper:{job_remote_root(job)}"
    args = rclone_base_args(cfg, temp_dir, server) + ["copy", str(archive), remote]
    proc = subprocess.run(args)
    if proc.returncode == 0:
        enforce_retention(cfg, job, temp_dir, server)
    return proc.returncode == 0


def enforce_retention(cfg: dict, job: dict, temp_dir: Path, server: dict) -> None:
    retention = int(job.get("retention", 0))
    if retention <= 0:
        return
    remote = f"backuper:{job_remote_root(job)}"
    args = rclone_base_args(cfg, temp_dir, server) + ["lsf", remote, "--files-only"]
    proc = subprocess.run(args, text=True, capture_output=True)
    if proc.returncode != 0:
        return
    files = sorted([line.strip() for line in proc.stdout.splitlines() if line.strip()])
    for name in files[:-retention]:
        subprocess.run(rclone_base_args(cfg, temp_dir, server) + ["deletefile", f"{remote}/{name}"])


def verify_archive(archive: Path, ext: str) -> bool:
    if ext == "tar.zst":
        cmd = ["tar", "-I", "zstd -T0", "-tf", str(archive)]
    else:
        cmd = ["tar", "-tzf", str(archive)]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        if proc.stderr:
            print(proc.stderr.strip())
        return False
    return True


def run_sync_job(cfg: dict, job: dict, path: Path, temp_dir: Path, server: dict) -> bool:
    remote = f"backuper:{job_remote_root(job)}"
    action = "sync" if job.get("sync_delete", False) else "copy"
    proc = subprocess.run(rclone_base_args(cfg, temp_dir, server) + rclone_filter_args(job) + [action, str(path), remote])
    if proc.returncode != 0:
        return False
    manifest = write_sync_metadata_manifest(job, path, temp_dir)
    meta_remote = "backuper:.backuper/metadata"
    meta_proc = subprocess.run(rclone_base_args(cfg, temp_dir, server) + ["copy", str(manifest), meta_remote])
    return meta_proc.returncode == 0


def write_sync_metadata_manifest(job: dict, path: Path, temp_dir: Path) -> Path:
    manifest = {
        "version": 1,
        "job": job["name"],
        "source": str(path),
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "entries": [],
    }
    seen: set[str] = set()
    for root, dirs, files in os.walk(path):
        current = Path(root)
        dirs[:] = [name for name in dirs if not should_ignore_path(current / name, path, job)]
        for item in [current, *[current / name for name in dirs], *[current / name for name in files]]:
            if should_ignore_path(item, path, job):
                continue
            try:
                stat_result = item.lstat()
            except OSError as exc:
                manifest["entries"].append({"path": str(item.relative_to(path)), "error": str(exc)})
                continue
            rel = "." if item == path else item.relative_to(path).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            entry = {
                "path": rel,
                "type": "dir" if item.is_dir() else "symlink" if item.is_symlink() else "file",
                "uid": stat_result.st_uid,
                "gid": stat_result.st_gid,
                "mode": oct(stat_result.st_mode & 0o7777),
                "mtime": int(stat_result.st_mtime),
                "size": stat_result.st_size if item.is_file() else 0,
            }
            entry["user"] = username_for_uid(stat_result.st_uid)
            entry["group"] = groupname_for_gid(stat_result.st_gid)
            manifest["entries"].append(entry)
    out = temp_dir / f"{job['name']}-metadata.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def username_for_uid(uid: int) -> str:
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except Exception:
        return ""


def groupname_for_gid(gid: int) -> str:
    try:
        import grp

        return grp.getgrgid(gid).gr_name
    except Exception:
        return ""


def show_log() -> None:
    log_path = LOG_DIR / "client.log"
    if not log_path.exists():
        print("暂无日志。")
    else:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-200:]:
            print(line)
    pause()


def service_name() -> str:
    return "backuper-client.service"


def install_service() -> None:
    unit = f"""[Unit]
Description=Backuper Client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={APP_ROOT}
Environment=HOME=/root
ExecStart=/usr/bin/env python3 {APP_ROOT / "client"} daemon
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""
    target = Path("/etc/systemd/system") / service_name()
    tmp = APP_ROOT / "var" / service_name()
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(unit, encoding="utf-8")
    commands = [
        privileged_args(["cp", str(tmp), str(target)]),
        privileged_args(["systemctl", "daemon-reload"]),
        privileged_args(["systemctl", "enable", "--now", service_name()]),
    ]
    for cmd in commands:
        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            print("开机自启配置失败。")
            return
    print("已安装并启用开机自启，客户端后台服务已启动。")


def systemctl(action: str) -> None:
    proc = subprocess.run(privileged_args(["systemctl", action, service_name()]))
    if proc.returncode != 0:
        print("systemctl 执行失败；如果尚未安装服务，请先选择“生成开机自启配置”。")


def privileged_args(args: list[str]) -> list[str]:
    if os.name == "nt":
        return args
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return args
    return ["sudo", *args]


def remove_service() -> None:
    target = Path("/etc/systemd/system") / service_name()
    commands = [
        privileged_args(["systemctl", "disable", "--now", service_name()]),
        privileged_args(["rm", "-f", str(target)]),
        privileged_args(["systemctl", "daemon-reload"]),
    ]
    for cmd in commands:
        proc = subprocess.run(cmd)
        if proc.returncode != 0 and "disable" not in cmd:
            print("删除开机自启配置失败。")
            return
    print("已删除开机自启配置，并停止客户端后台服务。")


def background_service_menu() -> None:
    while True:
        print_header("Backuper Client - 后台服务")
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


def due_to_run(cfg: dict, state: dict) -> bool:
    schedule = cfg.get("schedule", {"type": "manual"})
    stype = schedule.get("type", "manual")
    if stype == "manual":
        return False
    last_run = int(state.get("last_run", 0))
    now = dt.datetime.now()
    if stype == "interval":
        return now_ts() - last_run >= parse_interval(schedule.get("value", "1d"))
    if state.get("last_date") == now.strftime("%Y-%m-%d"):
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


def daemon() -> None:
    ensure_dirs()
    log("客户端调度器启动。")
    while True:
        cfg = load_config()
        state = read_json(CLIENT_STATE, {})
        try:
            if due_to_run(cfg, state):
                run_all_jobs()
                state["last_run"] = now_ts()
                state["last_date"] = dt.datetime.now().strftime("%Y-%m-%d")
                write_json(CLIENT_STATE, state)
        except Exception as exc:
            log(f"调度器错误: {exc}")
        time.sleep(30)


def interactive() -> None:
    ensure_dirs()
    while True:
        cfg = load_config()
        print_header("Backuper Client")
        for line in service_status_lines(service_name(), __version__):
            print(line)
        print("")
        print(f"服务器数量: {len(cfg.get('servers', []))}")
        print(f"全局计划: {format_schedule(cfg['schedule'])}")
        print("1. 立即执行一轮备份")
        print("2. 后台服务/开机自启")
        print("3. 服务器连接配置")
        print("4. 全局备份计划")
        print("5. 备份目录管理")
        print("6. 传输参数设置")
        print("7. 导入/导出配置")
        print("8. 查看备份日志")
        print("0. 退出")
        choice = prompt("请选择")
        if choice == "1":
            run_all_jobs()
            pause()
        elif choice == "2":
            background_service_menu()
        elif choice == "3":
            server_connection_menu()
        elif choice == "4":
            schedule_menu()
        elif choice == "5":
            jobs_menu()
        elif choice == "6":
            transfer_menu()
        elif choice == "7":
            config_menu()
        elif choice == "8":
            show_log()
        elif choice == "0":
            return


def main() -> None:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("command", nargs="?", choices=["daemon", "run", "test"])
    args = parser.parse_args()
    if args.command == "daemon":
        daemon()
    elif args.command == "run":
        raise SystemExit(0 if run_all_jobs() else 1)
    elif args.command == "test":
        raise SystemExit(0 if test_server() else 1)
    else:
        interactive()
