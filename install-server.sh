#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_URL="${BACKUPER_REPO_URL:-https://github.com/smmya/backuper.git}"
BRANCH="${BACKUPER_BRANCH:-main}"
INSTALL_BASE_DIR="${BACKUPER_BASE_DIR:-$(pwd)}"
INSTALL_DIR="${BACKUPER_INSTALL_DIR:-$INSTALL_BASE_DIR/backuper}"
SERVICE_NAME="${BACKUPER_SERVICE_NAME:-backuper-server.service}"
AUTO_START="${BACKUPER_AUTO_START:-1}"
INSTALL_RCLONE="${BACKUPER_INSTALL_RCLONE:-1}"

log() {
  printf '\n[Backuper] %s\n' "$*" >&2
}

die() {
  printf '\n[Backuper] ERROR: %s\n' "$*" >&2
  exit 1
}

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "请使用 root 执行，或使用: sudo BACKUPER_REPO_URL=... bash install-server.sh"
  fi
}

validate_install_dir() {
  case "$INSTALL_DIR" in
    /*) ;;
    *) die "BACKUPER_INSTALL_DIR 必须是绝对路径" ;;
  esac
  case "$INSTALL_DIR" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
      die "安装目录过于宽泛或危险: $INSTALL_DIR"
      ;;
  esac
}

install_packages() {
  log "安装系统依赖"
  local packages
  packages="git python3 openssl ca-certificates curl"
  if [ "$INSTALL_RCLONE" = "1" ]; then
    packages="$packages rclone"
  fi

  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y $packages
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y $packages
  elif command -v yum >/dev/null 2>&1; then
    yum install -y $packages
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install $packages
  elif command -v pacman >/dev/null 2>&1; then
    pacman -Sy --noconfirm --needed $packages
  else
    die "无法识别包管理器，请先手动安装: $packages"
  fi
}

check_python() {
  python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Backuper 需要 Python 3.10 或更高版本")
PY
}

resolve_source() {
  if [ -d "$SCRIPT_DIR/backuper" ] && [ -f "$SCRIPT_DIR/server" ] && [ -f "$SCRIPT_DIR/client" ]; then
    log "使用脚本同目录程序: $SCRIPT_DIR"
    printf '%s\n' "$SCRIPT_DIR"
    return
  fi

  local tmpdir
  tmpdir="$(mktemp -d)"
  trap 'rm -rf "$tmpdir"' EXIT
  log "从 GitHub 拉取程序: $REPO_URL ($BRANCH)"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$tmpdir/repo"
  printf '%s\n' "$tmpdir/repo"
}

install_files() {
  local src="$1"
  log "安装到 $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"

  local src_real install_real
  src_real="$(cd "$src" && pwd)"
  install_real="$(cd "$INSTALL_DIR" && pwd)"
  if [ "$src_real" = "$install_real" ]; then
    die "源码目录和安装目录相同。请在上级目录执行脚本，或设置 BACKUPER_INSTALL_DIR。"
  fi
  case "$install_real/" in
    "$src_real/"*) die "安装目录不能位于源码目录内部: $install_real" ;;
  esac

  rm -rf "$INSTALL_DIR/backuper"
  cp -a "$src/backuper" "$INSTALL_DIR/backuper"
  cp -f "$src/server" "$INSTALL_DIR/server"
  cp -f "$src/client" "$INSTALL_DIR/client"
  cp -f "$src/README.md" "$INSTALL_DIR/README.md"
  chmod +x "$INSTALL_DIR/server" "$INSTALL_DIR/client"

  mkdir -p "$INSTALL_DIR/var" "$INSTALL_DIR/storage"
}

write_launchers() {
  log "生成命令入口"
  cat >/usr/local/bin/backuper-server <<EOF
#!/usr/bin/env bash
cd "$INSTALL_DIR"
exec /usr/bin/env python3 ./server "\$@"
EOF
  cat >/usr/local/bin/backuper-client <<EOF
#!/usr/bin/env bash
cd "$INSTALL_DIR"
exec /usr/bin/env python3 ./client "\$@"
EOF
  chmod +x /usr/local/bin/backuper-server /usr/local/bin/backuper-client
}

write_service() {
  log "生成 systemd 服务: $SERVICE_NAME"
  cat >"/etc/systemd/system/$SERVICE_NAME" <<EOF
[Unit]
Description=Backuper Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/env python3 $INSTALL_DIR/server serve
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
}

start_service() {
  if [ "$AUTO_START" = "1" ]; then
    log "启用并重启后台服务"
    systemctl enable "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"
  else
    log "已跳过自动启动，可稍后执行: systemctl enable --now $SERVICE_NAME"
  fi
}

print_done() {
  cat <<EOF

[Backuper] 安装完成

安装目录:
  $INSTALL_DIR

常用命令:
  cd $INSTALL_DIR && ./server
  backuper-server
  systemctl status $SERVICE_NAME
  journalctl -u $SERVICE_NAME -f

下一步:
  1. 执行: cd $INSTALL_DIR && ./server
  2. 进入“基础设置”确认 HTTP / HTTPS 监听端口
  3. 进入“账号管理”创建账号并生成客户端配置码

EOF
}

main() {
  need_root
  validate_install_dir
  install_packages
  check_python
  local src
  src="$(resolve_source)"
  install_files "$src"
  "$INSTALL_DIR/server" init
  write_launchers
  write_service
  start_service
  print_done
}

main "$@"
