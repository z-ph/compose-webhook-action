#!/usr/bin/env python3
"""GET /health then POST /hook (and optionally poll /status) using stdlib only."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def hook_body() -> bytes:
    # 把飞书通知配置与 GitHub 上下文塞进 /hook body，listener 在 compose 完成/失败时据此发飞书。
    feishu_webhook = env("FEISHU_WEBHOOK")
    github = {
        "repo": env("GITHUB_REPOSITORY"),
        "ref": env("GITHUB_REF"),
        "sha": env("GITHUB_SHA"),
        "actor": env("GITHUB_ACTOR"),
        "run_url": env("GITHUB_SERVER_URL", "https://github.com")
        + f"/{env('GITHUB_REPOSITORY')}/actions/runs/{env('GITHUB_RUN_ID')}",
        "message": env("GITHUB_MESSAGE"),
    }
    feishu = {"webhook": feishu_webhook, "secret": env("FEISHU_SECRET")}
    payload = {"feishu": feishu, "github": github}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def resolve_urls(raw: str) -> tuple[str, str]:
    url = raw.strip().strip("'\"")
    if not url:
        print("::error::WEBHOOK_URL is empty")
        raise SystemExit(1)

    if "://" not in url:
        host = url.split("/", 1)[0]
        hostname = host.rsplit(":", 1)[0]
        has_port = ":" in host and (host.rsplit(":", 1)[-1].isdigit())
        use_http = has_port or all(part.isdigit() for part in hostname.split("."))
        url = f"{'http' if use_http else 'https'}://{url}"

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        print("::error::WEBHOOK_URL is not an http(s) URL")
        raise SystemExit(1)

    path = parsed.path.rstrip("/") or ""
    if path.endswith("/hook"):
        hook_path = path
        base_path = path[: -len("/hook")]
    else:
        hook_path = f"{path}/hook"
        base_path = path

    base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))
    hook = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, hook_path, "", ""))
    return hook, base.rstrip("/")


def hook_urls() -> tuple[str, str]:
    return resolve_urls(env("WEBHOOK_URL"))


def request(
    url: str,
    *,
    method: str,
    token: str,
    timeout: float,
    data: bytes | None = None,
    auth: bool = True,
) -> tuple[int, str]:
    headers: dict[str, str] = {}
    if auth:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
    except ValueError as exc:
        print("::error::WEBHOOK_URL is not a valid http(s) URL")
        raise SystemExit(1) from exc
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        print(f"::error::request failed: {exc.reason}")
        raise SystemExit(1) from exc


def append_output(**values: str) -> None:
    path = env("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in values.items():
            fh.write(f"{key}={value}\n")


def parse_object(body: str) -> dict:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def probe_health(base: str, timeout: float) -> int:
    health_url = f"{base}/health"
    http_code, body = request(health_url, method="GET", token="", timeout=timeout, auth=False)
    print(f"health HTTP {http_code}")
    if body:
        print(body)
    if http_code != 200:
        print(f"::error::webhook /health failed (HTTP {http_code})")
        return 1
    payload = parse_object(body)
    if payload and payload.get("ok") is False:
        print("::error::webhook /health returned ok=false")
        return 1
    return 0


def main() -> int:
    url, base = hook_urls()
    token = env("WEBHOOK_TOKEN")
    timeout = float(env("WEBHOOK_TIMEOUT", "30") or "30")
    wait = env("WEBHOOK_WAIT", "false") == "true"
    wait_timeout = int(env("WEBHOOK_WAIT_TIMEOUT", "1800") or "1800")

    if probe_health(base, timeout) != 0:
        return 1

    http_code, body = request(url, method="POST", token=token, timeout=timeout, data=hook_body())
    print(f"hook HTTP {http_code}")
    print(body)

    if http_code != 202:
        print(f"::error::webhook rejected request (HTTP {http_code})")
        return 1

    job_id = str(parse_object(body).get("job_id") or "")
    if not job_id:
        print("::error::response missing job_id")
        return 1

    append_output(**{"job-id": job_id, "http-code": str(http_code)})

    if not wait:
        print(f"accepted job {job_id} (not waiting)")
        append_output(status="accepted")
        return 0

    deadline = time.monotonic() + wait_timeout
    status = "unknown"
    while time.monotonic() < deadline:
        _, status_body = request(f"{base}/status", method="GET", token=token, timeout=15)
        job = parse_object(status_body).get("job") or {}
        if not isinstance(job, dict):
            job = {}
        status = str(job.get("status") or "")
        print(f"status={status}")
        if status == "ok":
            append_output(status="ok")
            return 0
        if status == "fail":
            print(f"::error::compose job {job_id} failed")
            append_output(status="fail")
            return 1
        time.sleep(5)

    print(
        f"::error::timed out waiting for job {job_id} after {wait_timeout}s "
        f"(last status={status})"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
