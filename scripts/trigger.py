#!/usr/bin/env python3
"""POST /hook (and optionally poll /status) using stdlib only."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def hook_urls() -> tuple[str, str]:
    url = env("WEBHOOK_URL").rstrip("/")
    if not url:
        print("::error::WEBHOOK_URL is empty")
        raise SystemExit(1)
    if not url.endswith("/hook"):
        url = f"{url}/hook"
    return url, url[: -len("/hook")]


def request(
    url: str,
    *,
    method: str,
    token: str,
    timeout: float,
    data: bytes | None = None,
) -> tuple[int, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
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


def main() -> int:
    url, base = hook_urls()
    token = env("WEBHOOK_TOKEN")
    timeout = float(env("WEBHOOK_TIMEOUT", "30") or "30")
    wait = env("WEBHOOK_WAIT", "false") == "true"
    wait_timeout = int(env("WEBHOOK_WAIT_TIMEOUT", "1800") or "1800")

    http_code, body = request(url, method="POST", token=token, timeout=timeout, data=b"{}")
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
