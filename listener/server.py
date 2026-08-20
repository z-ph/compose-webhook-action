#!/usr/bin/env python3
"""Local webhook: authenticated POST /hook runs docker compose (default: up --build -d)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse

PROXY_V2_SIG = b"\r\n\r\n\x00\r\nQUIT\n"

HERE = Path(__file__).resolve().parent
HOOK_ENV = HERE / ".env"
STATE_DIR = HERE / "var"

_lock = threading.Lock()
_job: dict[str, Any] | None = None
_pending: dict[str, Any] | None = None


def stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def require_token() -> str:
    token = os.environ.get("WEBHOOK_TOKEN", "").strip()
    if len(token) < 16:
        raise SystemExit("WEBHOOK_TOKEN missing or shorter than 16 chars")
    return token


def workdir() -> Path:
    raw = os.environ.get("COMPOSE_WORKDIR", "").strip()
    return Path(raw).resolve() if raw else Path.cwd()


def git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(workdir()),
            text=True,
            timeout=5,
        )
        return out.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def compose_cmd() -> list[str]:
    compose_file = os.environ.get("COMPOSE_FILE", "docker-compose.yml").strip() or "docker-compose.yml"
    compose_env = os.environ.get("COMPOSE_ENV", "").strip()
    args = os.environ.get("COMPOSE_ARGS", "up --build -d").split()
    cmd = ["docker", "compose", "-f", compose_file]
    if compose_env:
        cmd += ["--env-file", compose_env]
    cmd += args
    return cmd


def git_root(start: Path | None = None) -> Path | None:
    current = (start or workdir()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def pull_enabled() -> bool:
    return os.environ.get("GIT_PULL", "1").strip().lower() not in {"0", "false", "no", "off"}


def job_steps() -> list[list[str]]:
    steps: list[list[str]] = []
    if pull_enabled():
        root = git_root()
        if root is not None:
            steps.append(["git", "-C", str(root), "pull", "--ff-only"])
    steps.append(compose_cmd())
    return steps


def run_steps(
    steps: list[list[str]],
    *,
    log,
    cwd: Path,
    env: dict[str, str],
    dry_run: bool,
) -> int:
    for cmd in steps:
        log.write(f"$ {' '.join(cmd)}\n")
        log.flush()
        if dry_run:
            log.write("WEBHOOK_DRY_RUN=1 — not executed\n")
            log.flush()
            continue
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if proc.returncode != 0:
            return proc.returncode
        if cmd and cmd[0] == "git" and "pull" in cmd:
            env["GIT_SHA"] = git_sha()
    return 0


def extract_token(handler: BaseHTTPRequestHandler) -> str:
    auth = handler.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return handler.headers.get("X-Webhook-Token", "").strip()


def parse_proxy_v1_line(line: str) -> tuple[str, int] | None:
    parts = line.strip().split()
    if len(parts) >= 6 and parts[0] == "PROXY" and parts[1] in {"TCP4", "TCP6"}:
        return parts[2], int(parts[4])
    return None


def parse_proxy_v2_header(header: bytes, rest: bytes) -> tuple[str, int] | None:
    if len(header) < 16 or not header.startswith(PROXY_V2_SIG):
        return None
    ver_cmd = header[12]
    fam = header[13]
    length = struct.unpack("!H", header[14:16])[0]
    if (ver_cmd & 0xF0) != 0x20 or len(rest) < length:
        return None
    if ver_cmd & 0x0F == 0x00:
        return None
    if fam == 0x11 and length >= 12:
        src = socket.inet_ntoa(rest[0:4])
        port = struct.unpack("!H", rest[8:10])[0]
        return src, port
    if fam == 0x21 and length >= 36:
        src = socket.inet_ntop(socket.AF_INET6, rest[0:16])
        port = struct.unpack("!H", rest[32:34])[0]
        return src, port
    return None


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def accept_proxy_protocol(sock: socket.socket, addr: tuple[str, int]) -> tuple[str, int]:
    try:
        peek = sock.recv(16, socket.MSG_PEEK)
    except OSError:
        return addr
    if peek.startswith(b"PROXY "):
        raw = b""
        while b"\r\n" not in raw:
            chunk = sock.recv(1)
            if not chunk:
                break
            raw += chunk
        parsed = parse_proxy_v1_line(raw.decode("ascii", errors="replace"))
        return parsed if parsed else addr
    if peek.startswith(PROXY_V2_SIG):
        header = recv_exact(sock, 16)
        length = struct.unpack("!H", header[14:16])[0] if len(header) == 16 else 0
        rest = recv_exact(sock, length)
        parsed = parse_proxy_v2_header(header, rest)
        return parsed if parsed else addr
    return addr


class ProxyAwareHTTPServer(ThreadingHTTPServer):
    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        sock, addr = super().get_request()
        return sock, accept_proxy_protocol(sock, addr)


def json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def write_status(job: dict[str, Any] | None = None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "status.json").write_text(
        json.dumps({"job": job if job is not None else _job, "pending": _pending}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def reset_jobs() -> None:
    global _job, _pending
    with _lock:
        _job = None
        _pending = None


def current_job() -> dict[str, Any] | None:
    return _job


def pending_job() -> dict[str, Any] | None:
    return _pending


def new_job(dry_run: bool, status: str, hook_input: dict[str, Any] | None = None) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "status": status,
        "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dry_run": dry_run,
        "coalesced": 0,
    }
    if hook_input:
        if hook_input.get("feishu"):
            job["feishu"] = hook_input["feishu"]
        if hook_input.get("github"):
            job["github"] = hook_input["github"]
    return job


def set_running_job(job_id: str, dry_run: bool = True) -> dict[str, Any]:
    global _job
    with _lock:
        _job = {
            "id": job_id,
            "status": "running",
            "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "dry_run": dry_run,
            "coalesced": 0,
        }
        write_status(_job)
        return dict(_job)


def start_job(job_id: str, dry_run: bool) -> None:
    threading.Thread(target=run_job, args=(job_id, dry_run), daemon=True).start()


def accept_hook(dry_run: bool, hook_input: dict[str, Any] | None = None) -> dict[str, Any]:
    global _job, _pending
    with _lock:
        if _job and _job.get("status") in {"queued", "running"}:
            if _pending is None:
                _pending = new_job(dry_run, "waiting", hook_input)
                write_status(_job)
                return {
                    "accepted": True,
                    "queued": True,
                    "coalesced": False,
                    "job_id": _pending["id"],
                    "dry_run": dry_run,
                    "command": job_steps(),
                }
            _pending["coalesced"] = int(_pending.get("coalesced") or 0) + 1
            _pending["dry_run"] = dry_run
            _pending["last_accepted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            if hook_input:
                if hook_input.get("feishu"):
                    _pending["feishu"] = hook_input["feishu"]
                if hook_input.get("github"):
                    _pending["github"] = hook_input["github"]
            write_status(_job)
            return {
                "accepted": True,
                "queued": True,
                "coalesced": True,
                "job_id": _pending["id"],
                "dry_run": dry_run,
                "command": job_steps(),
            }
        job = new_job(dry_run, "queued", hook_input)
        _job = job
        write_status(job)
        start_job(job["id"], dry_run)
        return {
            "accepted": True,
            "queued": False,
            "coalesced": False,
            "job_id": job["id"],
            "dry_run": dry_run,
            "command": job_steps(),
        }


def finish_job(job_id: str, code: int, error: str | None = None) -> None:
    global _job, _pending
    nxt: dict[str, Any] | None = None
    finished: dict[str, Any] | None = None
    with _lock:
        if _job is None or _job.get("id") != job_id:
            return
        payload = {
            "status": "ok" if code == 0 else "fail",
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "returncode": code,
        }
        if error:
            payload["error"] = error
        _job.update(payload)
        write_status(_job)
        finished = dict(_job)
        if _pending is not None:
            nxt = _pending
            _pending = None
            nxt["status"] = "queued"
            _job = nxt
            write_status(_job)
    # 通知在锁外发送：网络 IO 不阻塞后续 /hook 接收与下一轮 compose 启动。
    # 失败只记日志，绝不影响 job 状态流转。
    if finished is not None:
        notify_feishu(finished)
    if nxt is not None:
        start_job(nxt["id"], bool(nxt.get("dry_run")))


def parse_hook_body(raw: bytes) -> dict[str, Any]:
    """从 POST /hook body 解析飞书通知配置与 GitHub 上下文（best-effort）。"""
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, Any] = {}
    feishu = data.get("feishu")
    if isinstance(feishu, dict):
        webhook = str(feishu.get("webhook") or "").strip()
        if webhook:
            result["feishu"] = {
                "webhook": webhook,
                "secret": str(feishu.get("secret") or "").strip(),
            }
    github = data.get("github")
    if isinstance(github, dict):
        result["github"] = {
            "repo": str(github.get("repo") or ""),
            "ref": str(github.get("ref") or ""),
            "sha": str(github.get("sha") or ""),
            "actor": str(github.get("actor") or ""),
            "run_url": str(github.get("run_url") or ""),
            "message": str(github.get("message") or ""),
        }
    return result


def short_sha(sha: str) -> str:
    return (sha or "")[:7]


def feishu_sign(timestamp: str, secret: str) -> str:
    # 飞书自定义机器人签名：HMAC-SHA256，key 为 `${timestamp}\n${secret}`，对空串签名后 base64。
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def build_feishu_payload(job: dict[str, Any]) -> dict[str, Any]:
    ok = job.get("status") == "ok"
    title = "✅ 部署成功" if ok else "❌ 部署失败"
    gh = job.get("github") or {}
    lines: list[list[dict[str, Any]]] = []

    def text(value: str) -> dict[str, Any]:
        return {"tag": "text", "text": value}

    def add(*elements: dict[str, Any]) -> None:
        lines.append(list(elements))

    if gh.get("repo"):
        add(text(f"仓库：{gh['repo']}"))
    if gh.get("ref"):
        add(text(f"分支：{gh['ref']}"))
    if gh.get("sha"):
        add(text(f"提交：{short_sha(gh['sha'])}"))
    if gh.get("actor"):
        add(text(f"触发者：{gh['actor']}"))
    if gh.get("message"):
        add(text(f"说明：{gh['message']}"))
    code = job.get("returncode")
    if code is not None:
        add(text(f"返回码：{code}"))
    if not ok and job.get("error"):
        add(text(f"错误：{job['error']}"))
    run_url = gh.get("run_url") or ""
    if run_url:
        add({"tag": "a", "text": "查看 GitHub Actions 运行", "href": run_url})

    return {"msg_type": "post", "content": {"post": {"zh_cn": {"title": title, "content": lines}}}}


def send_feishu(webhook: str, payload: dict[str, Any], secret: str) -> None:
    body = dict(payload)
    secret = (secret or "").strip()
    if secret:
        timestamp = str(int(time.time()))
        body["timestamp"] = timestamp
        body["sign"] = feishu_sign(timestamp, secret)
    data = json_bytes(body)
    req = urllib_request.Request(
        webhook,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as resp:
            resp_text = resp.read().decode("utf-8", errors="replace")
    except urllib_error.URLError as exc:
        raise RuntimeError(f"feishu request failed: {exc.reason}") from exc
    try:
        result = json.loads(resp_text)
    except json.JSONDecodeError:
        result = {}
    if not isinstance(result, dict) or result.get("code") != 0:
        raise RuntimeError(f"feishu rejected: {resp_text}")


def notify_feishu(job: dict[str, Any]) -> None:
    # /hook body 优先；listener .env 的 FEISHU_WEBHOOK/FEISHU_SECRET 作服务端兜底。
    feishu = job.get("feishu") or {}
    webhook = (feishu.get("webhook") or os.environ.get("FEISHU_WEBHOOK", "")).strip()
    if not webhook:
        return
    secret = (feishu.get("secret") or os.environ.get("FEISHU_SECRET", "")).strip()
    try:
        payload = build_feishu_payload(job)
        send_feishu(webhook, payload, secret)
        sys.stderr.write(f"{stamp()} feishu notify ok ({job.get('status')})\n")
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"{stamp()} feishu notify failed: {exc}\n")


def run_job(job_id: str, dry_run: bool) -> None:
    log_path = STATE_DIR / f"{job_id}.log"
    steps = job_steps()
    env = os.environ.copy()
    env.setdefault("GIT_SHA", git_sha())
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _lock:
        if _job is None or _job.get("id") != job_id:
            return
        _job.update({"status": "running", "started_at": started, "log": str(log_path), "command": steps})
        write_status(_job)
    try:
        with log_path.open("w", encoding="utf-8") as log:
            code = run_steps(steps, log=log, cwd=workdir(), env=env, dry_run=dry_run)
        finish_job(job_id, code)
    except Exception as exc:  # noqa: BLE001
        finish_job(job_id, -1, error=str(exc))


class Handler(BaseHTTPRequestHandler):
    server_version = "compose-webhook/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        xff = "-"
        if hasattr(self, "headers") and self.headers is not None:
            xff = self.headers.get("X-Forwarded-For", "-")
        sys.stderr.write("%s %s xff=%s - %s\n" % (stamp(), self.address_string(), xff, fmt % args))

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json_bytes(payload)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        if not secrets.compare_digest(extract_token(self), require_token()):
            self._send(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"ok": True})
            return
        if path == "/status":
            if not self._auth():
                return
            with _lock:
                self._send(200, {"job": _job, "pending": _pending})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/hook":
            self._send(404, {"error": "not found"})
            return
        if not self._auth():
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        hook_input = parse_hook_body(raw)
        dry_run = os.environ.get("WEBHOOK_DRY_RUN", "").strip() in {"1", "true", "yes"}
        result = accept_hook(dry_run, hook_input)
        self._send(202, result)


def self_test() -> int:
    os.environ.setdefault("WEBHOOK_TOKEN", "0" * 16)
    require_token()
    cmd = compose_cmd()
    assert cmd[:3] == ["docker", "compose", "-f"]
    assert cmd[-3:] == ["up", "--build", "-d"]
    assert parse_proxy_v1_line("PROXY TCP4 203.0.113.9 10.0.0.1 54321 19090") == ("203.0.113.9", 54321)
    v2 = (
        PROXY_V2_SIG
        + bytes([0x21, 0x11, 0x00, 0x0C])
        + socket.inet_aton("198.51.100.7")
        + socket.inet_aton("10.0.0.1")
        + struct.pack("!HH", 40000, 19090)
    )
    assert parse_proxy_v2_header(v2[:16], v2[16:]) == ("198.51.100.7", 40000)
    print("self-test ok")
    print("command:", " ".join(cmd))
    return 0


def main() -> int:
    load_env(HOOK_ENV)
    if "--self-test" in sys.argv:
        return self_test()
    require_token()
    cwd = workdir()
    compose_file = Path(os.environ.get("COMPOSE_FILE", "docker-compose.yml"))
    if not compose_file.is_absolute():
        compose_file = cwd / compose_file
    if not compose_file.is_file() and os.environ.get("WEBHOOK_DRY_RUN", "").strip() not in {"1", "true", "yes"}:
        raise SystemExit(f"compose file missing: {compose_file}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("WEBHOOK_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("WEBHOOK_PORT", "19090"))
    dry = os.environ.get("WEBHOOK_DRY_RUN", "").strip() in {"1", "true", "yes"}
    httpd = ProxyAwareHTTPServer((host, port), Handler)
    print(f"{stamp()} compose-webhook listen http://{host}:{port}  hook=POST /hook  dry_run={dry}")
    print(f"{stamp()} workdir: {cwd}")
    print(f"{stamp()} steps: {' && '.join(' '.join(step) for step in job_steps())}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print(f"\n{stamp()} stop")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
