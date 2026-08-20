#!/usr/bin/env python3
"""Regression tests for trigger URL normalization and /health-before-/hook."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("trigger", ROOT / "scripts" / "trigger.py")
assert SPEC and SPEC.loader
trigger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trigger)


class ResolveUrlsTests(unittest.TestCase):
    def test_scheme_less_domain_becomes_https_hook(self) -> None:
        hook, base = trigger.resolve_urls("webhook.zphhpz.top")
        self.assertEqual(hook, "https://webhook.zphhpz.top/hook")
        self.assertEqual(base, "https://webhook.zphhpz.top")

    def test_scheme_less_host_port_becomes_http_hook(self) -> None:
        hook, base = trigger.resolve_urls("203.0.113.9:19090")
        self.assertEqual(hook, "http://203.0.113.9:19090/hook")
        self.assertEqual(base, "http://203.0.113.9:19090")

    def test_https_base_appends_hook(self) -> None:
        hook, base = trigger.resolve_urls("https://webhook.zphhpz.top")
        self.assertEqual(hook, "https://webhook.zphhpz.top/hook")
        self.assertEqual(base, "https://webhook.zphhpz.top")

    def test_full_hook_url_kept(self) -> None:
        hook, base = trigger.resolve_urls("https://webhook.zphhpz.top/hook")
        self.assertEqual(hook, "https://webhook.zphhpz.top/hook")
        self.assertEqual(base, "https://webhook.zphhpz.top")

    def test_quoted_and_whitespace_stripped(self) -> None:
        hook, _base = trigger.resolve_urls("  'https://webhook.zphhpz.top/hook'  ")
        self.assertEqual(hook, "https://webhook.zphhpz.top/hook")

    def test_empty_url_exits(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            trigger.resolve_urls("   ")
        self.assertEqual(ctx.exception.code, 1)


class HookBodyTests(unittest.TestCase):
    def test_includes_feishu_and_github(self) -> None:
        env = {
            "FEISHU_WEBHOOK": "https://open.feishu.cn/h/x",
            "FEISHU_SECRET": "s3cr3t",
            "GITHUB_REPOSITORY": "z-ph/zb",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": "abcdef0",
            "GITHUB_ACTOR": "z-ph",
            "GITHUB_RUN_ID": "123",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_MESSAGE": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            body = json.loads(trigger.hook_body().decode("utf-8"))
        self.assertEqual(body["feishu"]["webhook"], "https://open.feishu.cn/h/x")
        self.assertEqual(body["feishu"]["secret"], "s3cr3t")
        self.assertEqual(body["github"]["repo"], "z-ph/zb")
        self.assertEqual(body["github"]["sha"], "abcdef0")
        self.assertEqual(body["github"]["run_url"], "https://github.com/z-ph/zb/actions/runs/123")

    def test_empty_feishu_still_serializes(self) -> None:
        with mock.patch.dict(os.environ, {"FEISHU_WEBHOOK": "", "FEISHU_SECRET": ""}, clear=False):
            body = json.loads(trigger.hook_body().decode("utf-8"))
        self.assertEqual(body["feishu"]["webhook"], "")


class _Recorder:
    def __init__(self) -> None:
        self.paths: list[str] = []
        self.health_code = 200
        self.hook_code = 202

    def handler(self) -> type[BaseHTTPRequestHandler]:
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                recorder.paths.append(f"GET {self.path}")
                body = b'{"ok": true}' if recorder.health_code == 200 else b'{"ok": false}'
                self.send_response(recorder.health_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                recorder.paths.append(f"POST {self.path}")
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length:
                    self.rfile.read(length)
                payload = json.dumps({"accepted": True, "job_id": "abc123"}).encode("utf-8")
                self.send_response(recorder.hook_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler


class HealthBeforeHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recorder = _Recorder()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.recorder.handler())
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _run(self, url: str | None = None) -> int:
        env = {
            "WEBHOOK_URL": url or self.base,
            "WEBHOOK_TOKEN": "test-token-16xxxx",
            "WEBHOOK_TIMEOUT": "2",
            "WEBHOOK_WAIT": "false",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return trigger.main()

    def test_health_ok_then_posts_hook(self) -> None:
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.recorder.paths, ["GET /health", "POST /hook"])

    def test_unhealthy_skips_hook(self) -> None:
        self.recorder.health_code = 503
        self.assertEqual(self._run(), 1)
        self.assertEqual(self.recorder.paths, ["GET /health"])

    def test_scheme_less_localhost_port_reaches_health(self) -> None:
        host, port = self.httpd.server_address[:2]
        self.assertEqual(self._run(f"{host}:{port}"), 0)
        self.assertEqual(self.recorder.paths, ["GET /health", "POST /hook"])


if __name__ == "__main__":
    sys.exit(unittest.main())
