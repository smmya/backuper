from __future__ import annotations

import base64
import html
import json
import os
import shutil
import ssl
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from .common import LOG_DIR, ensure_dirs


ConfigLoader = Callable[[], dict]
FAIL_WINDOW_SECONDS = 15 * 60
DELAY_AFTER_FAILURES = 5
BAN_AFTER_FAILURES = 10
BAN_SECONDS = 10 * 60
MAX_AUTH_DELAY_SECONDS = 5

AUTH_LOCK = threading.Lock()
AUTH_FAILURES: dict[str, dict[str, float | int]] = {}


def _http_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _xml_escape(value: str) -> str:
    return html.escape(value, quote=True)


def _auth_log(message: str) -> None:
    ensure_dirs()
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with (LOG_DIR / "server-auth.log").open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {message}\n")


def _failure_state(ip: str) -> dict[str, float | int]:
    now = time.time()
    state = AUTH_FAILURES.setdefault(ip, {"count": 0, "last": 0.0, "banned_until": 0.0})
    if now - float(state.get("last", 0.0)) > FAIL_WINDOW_SECONDS and float(state.get("banned_until", 0.0)) <= now:
        state["count"] = 0
        state["banned_until"] = 0.0
    return state


def _ban_remaining(ip: str) -> int:
    with AUTH_LOCK:
        state = _failure_state(ip)
        remaining = int(float(state.get("banned_until", 0.0)) - time.time())
        return max(0, remaining)


def _record_auth_failure(ip: str, username: str, reason: str, path: str) -> int:
    with AUTH_LOCK:
        state = _failure_state(ip)
        count = int(state.get("count", 0)) + 1
        state["count"] = count
        state["last"] = time.time()
        if count >= BAN_AFTER_FAILURES:
            state["banned_until"] = time.time() + BAN_SECONDS
        delay = 0
        if count >= DELAY_AFTER_FAILURES:
            delay = min(count - DELAY_AFTER_FAILURES + 1, MAX_AUTH_DELAY_SECONDS)
        banned = int(max(0, float(state.get("banned_until", 0.0)) - time.time()))
    safe_user = username or "-"
    _auth_log(
        f"failure ip={ip} username={safe_user} reason={reason} "
        f"count={count} delay={delay}s banned_for={banned}s path={path}"
    )
    return delay


def _record_auth_success(ip: str) -> None:
    with AUTH_LOCK:
        AUTH_FAILURES.pop(ip, None)


def make_handler(load_config: ConfigLoader):
    class WebDAVHandler(BaseHTTPRequestHandler):
        server_version = "BackuperWebDAV/0.1"

        def log_message(self, fmt: str, *args) -> None:
            print(f"[webdav] {self.address_string()} - {fmt % args}")

        def do_OPTIONS(self) -> None:
            if not self._program_access_allowed():
                self.send_error(HTTPStatus.FORBIDDEN, "Backuper client header required")
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("DAV", "1, 2")
            self.send_header("Allow", "OPTIONS, PROPFIND, GET, HEAD, PUT, DELETE, MKCOL, MOVE")
            self.end_headers()

        def do_HEAD(self) -> None:
            path = self._authorized_path()
            if path is None:
                return
            if not path.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_stat_headers(path)
            self.end_headers()

        def do_GET(self) -> None:
            if not self._program_access_allowed():
                self.send_error(HTTPStatus.FORBIDDEN, "Backuper client header required")
                return
            if not load_config().get("allow_download", False):
                if not self._authenticate():
                    self.send_response(HTTPStatus.UNAUTHORIZED)
                    self.send_header("WWW-Authenticate", 'Basic realm="Backuper"')
                    self.end_headers()
                else:
                    self.send_error(HTTPStatus.FORBIDDEN, "download disabled")
                return
            path = self._authorized_path()
            if path is None:
                return
            if not path.exists() or path.is_dir():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_stat_headers(path)
            with path.open("rb") as f:
                shutil.copyfileobj(f, self.wfile)

        def do_PROPFIND(self) -> None:
            path = self._authorized_path()
            if path is None:
                return
            if not path.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            depth = self.headers.get("Depth", "0")
            entries = [path]
            if path.is_dir() and depth != "0":
                try:
                    entries.extend(sorted(path.iterdir(), key=lambda p: p.name))
                except OSError:
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
            body = self._propfind_xml(path, entries)
            data = body.encode("utf-8")
            self.send_response(207, "Multi-Status")
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_MKCOL(self) -> None:
            path = self._authorized_path()
            if path is None:
                return
            if path.exists():
                self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
                return
            path.mkdir(parents=True, exist_ok=False)
            self.send_response(HTTPStatus.CREATED)
            self.end_headers()

        def do_PUT(self) -> None:
            path = self._authorized_path()
            if path is None:
                return
            length = self.headers.get("Content-Length")
            if length is None:
                self.send_error(HTTPStatus.LENGTH_REQUIRED)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            created = not path.exists()
            remaining = int(length)
            tmp = path.with_name(path.name + ".uploading")
            with tmp.open("wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            if remaining != 0:
                tmp.unlink(missing_ok=True)
                self.send_error(HTTPStatus.BAD_REQUEST, "incomplete upload")
                return
            tmp.replace(path)
            self._apply_sync_metadata_if_needed(path)
            self.send_response(HTTPStatus.CREATED if created else HTTPStatus.NO_CONTENT)
            self.end_headers()

        def do_DELETE(self) -> None:
            path = self._authorized_path()
            if path is None:
                return
            if not path.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()

        def do_MOVE(self) -> None:
            src = self._authorized_path()
            if src is None:
                return
            dest_header = self.headers.get("Destination")
            if not dest_header:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            dest_path = urllib.parse.urlparse(dest_header).path
            dest = self._path_for_url(dest_path)
            if dest is None:
                return
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dest)
            self.send_response(HTTPStatus.CREATED)
            self.end_headers()

        def _authorized_path(self) -> Path | None:
            if not self._program_access_allowed():
                self.send_error(HTTPStatus.FORBIDDEN, "Backuper client header required")
                return None
            if not self._authenticate():
                if getattr(self, "auth_rate_limited", False):
                    self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
                    self.send_header("Retry-After", str(getattr(self, "auth_retry_after", BAN_SECONDS)))
                else:
                    self.send_response(HTTPStatus.UNAUTHORIZED)
                    self.send_header("WWW-Authenticate", 'Basic realm="Backuper"')
                self.end_headers()
                return None
            return self._path_for_url(self.path)

        def _program_access_allowed(self) -> bool:
            config = load_config()
            if not config.get("require_program_access", True):
                return True
            expected = config.get("program_access_token", "")
            provided = self.headers.get("X-Backuper-Access", "")
            return bool(expected) and provided == expected

        def _authenticate(self) -> bool:
            self.auth_rate_limited = False
            self.auth_retry_after = 0
            config = load_config()
            ip = self.client_address[0]
            banned_for = _ban_remaining(ip)
            if banned_for:
                self.auth_rate_limited = True
                self.auth_retry_after = banned_for
                _auth_log(f"blocked ip={ip} banned_for={banned_for}s path={self.path}")
                return False
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                _auth_log(f"missing-auth ip={ip} path={self.path}")
                return False
            username = ""
            try:
                raw = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
                username, password = raw.split(":", 1)
            except Exception:
                delay = _record_auth_failure(ip, username, "malformed-basic-auth", self.path)
                if delay:
                    time.sleep(delay)
                return False
            user = config.get("users", {}).get(username)
            if not user or not user.get("enabled", True):
                delay = _record_auth_failure(ip, username, "unknown-or-disabled-user", self.path)
                if delay:
                    time.sleep(delay)
                return False
            if user.get("password") != password:
                delay = _record_auth_failure(ip, username, "bad-password", self.path)
                if delay:
                    time.sleep(delay)
                return False
            _record_auth_success(ip)
            self.username = username
            return True

        def _path_for_url(self, url_path: str) -> Path | None:
            config = load_config()
            storage_root = Path(config["storage_root"]).resolve()
            user_root = (storage_root / self.username).resolve()
            rel = urllib.parse.unquote(urllib.parse.urlparse(url_path).path).lstrip("/")
            candidate = (user_root / rel).resolve()
            if user_root != candidate and user_root not in candidate.parents:
                self.send_error(HTTPStatus.FORBIDDEN)
                return None
            return candidate

        def _send_stat_headers(self, path: Path) -> None:
            stat = path.stat()
            self.send_response(HTTPStatus.OK)
            self.send_header("Last-Modified", _http_date(stat.st_mtime))
            self.send_header("Content-Length", "0" if path.is_dir() else str(stat.st_size))
            self.send_header("Content-Type", "application/octet-stream")

        def _apply_sync_metadata_if_needed(self, path: Path) -> None:
            if path.parent.name != "metadata" or path.parent.parent.name != ".backuper" or not path.name.endswith("-metadata.json"):
                return
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[webdav] metadata apply skipped: {exc}")
                return
            job_name = str(data.get("job") or path.name[: -len("-metadata.json")]).strip("/")
            if not job_name or "/" in job_name or "\\" in job_name:
                print(f"[webdav] metadata apply skipped: invalid job name {job_name}")
                return
            user_root = path.parent.parent.parent.resolve()
            sync_root = (user_root / job_name).resolve()
            if user_root != sync_root and user_root not in sync_root.parents:
                return
            if not sync_root.exists():
                return
            applied = 0
            for entry in data.get("entries", []):
                rel = entry.get("path", "")
                if not rel:
                    continue
                target = sync_root if rel == "." else (sync_root / rel).resolve()
                if sync_root != target and sync_root not in target.parents:
                    continue
                if not target.exists() and not target.is_symlink():
                    continue
                uid = entry.get("uid")
                gid = entry.get("gid")
                mode = entry.get("mode")
                try:
                    if uid is not None and gid is not None:
                        os.chown(target, int(uid), int(gid), follow_symlinks=False)
                    if mode and not target.is_symlink():
                        os.chmod(target, int(str(mode), 8))
                    applied += 1
                except PermissionError:
                    print(f"[webdav] metadata apply permission denied: {target}")
                except OSError as exc:
                    print(f"[webdav] metadata apply failed: {target}: {exc}")
            if applied:
                print(f"[webdav] applied sync metadata entries={applied} root={sync_root}")

        def _href_for(self, base: Path, path: Path) -> str:
            rel = "" if path == base else path.relative_to(base).as_posix()
            href = "/" + urllib.parse.quote(rel)
            if path.is_dir() and not href.endswith("/"):
                href += "/"
            return href

        def _propfind_xml(self, base: Path, entries: list[Path]) -> str:
            user_root = Path(load_config()["storage_root"]).resolve() / self.username
            parts = ['<?xml version="1.0" encoding="utf-8"?>', '<D:multistatus xmlns:D="DAV:">']
            for entry in entries:
                stat = entry.stat()
                href = self._href_for(user_root.resolve(), entry)
                resource_type = "<D:collection/>" if entry.is_dir() else ""
                length = 0 if entry.is_dir() else stat.st_size
                parts.append(
                    "<D:response>"
                    f"<D:href>{_xml_escape(href)}</D:href>"
                    "<D:propstat><D:prop>"
                    f"<D:resourcetype>{resource_type}</D:resourcetype>"
                    f"<D:getcontentlength>{length}</D:getcontentlength>"
                    f"<D:getlastmodified>{_http_date(stat.st_mtime)}</D:getlastmodified>"
                    "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
                    "</D:response>"
                )
            parts.append("</D:multistatus>")
            return "".join(parts)

    return WebDAVHandler


def serve(load_config: ConfigLoader, host: str, port: int, cert: Path | None = None, key: Path | None = None) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(load_config))
    if cert and key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    scheme = "https" if cert and key else "http"
    print(f"Backuper WebDAV listening on {scheme}://{host}:{port}")
    httpd.serve_forever()
