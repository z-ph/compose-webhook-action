#!/usr/bin/env python3
"""Listener job should git pull before docker compose."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("listener", ROOT / "listener" / "server.py")
assert SPEC and SPEC.loader
listener = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(listener)


class JobStepsTests(unittest.TestCase):
    def test_pull_then_compose(self) -> None:
        with mock.patch.object(listener, "git_root", return_value=Path("/repo")):
            steps = listener.job_steps()
        self.assertEqual(steps[0], ["git", "-C", "/repo", "pull", "--ff-only"])
        self.assertEqual(steps[-1][:3], ["docker", "compose", "-f"])
        self.assertEqual(steps[-1][-3:], ["up", "--build", "-d"])

    def test_skip_pull_when_disabled(self) -> None:
        with mock.patch.dict(os.environ, {"GIT_PULL": "0"}, clear=False):
            steps = listener.job_steps()
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0][:3], ["docker", "compose", "-f"])

    def test_skip_pull_when_not_a_git_repo(self) -> None:
        with mock.patch.object(listener, "git_root", return_value=None):
            steps = listener.job_steps()
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0][:3], ["docker", "compose", "-f"])


class RunStepsTests(unittest.TestCase):
    def test_failed_pull_skips_compose(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
            calls.append(list(cmd))
            result = mock.Mock()
            result.returncode = 1 if cmd[0] == "git" else 0
            return result

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "job.log"
            with log.open("w", encoding="utf-8") as fh, mock.patch.object(
                listener.subprocess, "run", side_effect=fake_run
            ):
                code = listener.run_steps(
                    [
                        ["git", "-C", "/repo", "pull", "--ff-only"],
                        ["docker", "compose", "-f", "docker-compose.yml", "up", "--build", "-d"],
                    ],
                    log=fh,
                    cwd=Path(tmp),
                    env={},
                    dry_run=False,
                )
            text = log.read_text(encoding="utf-8")
        self.assertEqual(code, 1)
        self.assertEqual(calls, [["git", "-C", "/repo", "pull", "--ff-only"]])
        self.assertIn("git -C /repo pull --ff-only", text)
        self.assertNotIn("docker compose", text)

    def test_dry_run_records_both_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "job.log"
            with log.open("w", encoding="utf-8") as fh:
                code = listener.run_steps(
                    [
                        ["git", "-C", "/repo", "pull", "--ff-only"],
                        ["docker", "compose", "-f", "docker-compose.yml", "up", "--build", "-d"],
                    ],
                    log=fh,
                    cwd=Path(tmp),
                    env={},
                    dry_run=True,
                )
            text = log.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("git -C /repo pull --ff-only", text)
        self.assertIn("docker compose", text)
        self.assertIn("WEBHOOK_DRY_RUN=1", text)


class SingleSlotQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        listener.reset_jobs()

    def tearDown(self) -> None:
        listener.reset_jobs()

    def test_idle_hook_starts_immediately(self) -> None:
        with mock.patch.object(listener.threading.Thread, "start"):
            result = listener.accept_hook(dry_run=True)
        self.assertTrue(result["accepted"])
        self.assertFalse(result["queued"])
        self.assertEqual(listener.current_job()["status"], "queued")
        self.assertIsNone(listener.pending_job())

    def test_busy_hook_waits_in_single_slot(self) -> None:
        listener.set_running_job("run1")
        result = listener.accept_hook(dry_run=True)
        self.assertTrue(result["accepted"])
        self.assertTrue(result["queued"])
        pending = listener.pending_job()
        self.assertIsNotNone(pending)
        self.assertEqual(result["job_id"], pending["id"])
        self.assertEqual(listener.current_job()["id"], "run1")

    def test_one_hundred_hooks_keep_single_waiter(self) -> None:
        listener.set_running_job("run1")
        first = listener.accept_hook(dry_run=True)
        ids = {first["job_id"]}
        coalesced = 0
        for _ in range(99):
            result = listener.accept_hook(dry_run=True)
            self.assertTrue(result["accepted"])
            self.assertTrue(result["queued"])
            ids.add(result["job_id"])
            if result.get("coalesced"):
                coalesced += 1
        self.assertEqual(ids, {first["job_id"]})
        self.assertEqual(coalesced, 99)
        self.assertEqual(listener.pending_job()["id"], first["job_id"])
        self.assertEqual(listener.pending_job().get("coalesced"), 99)

    def test_finished_job_starts_pending(self) -> None:
        listener.set_running_job("run1")
        waiting = listener.accept_hook(dry_run=True)
        started: list[str] = []
        with mock.patch.object(listener, "start_job", side_effect=lambda job_id, dry_run: started.append(job_id)):
            listener.finish_job("run1", code=0)
        self.assertEqual(started, [waiting["job_id"]])
        self.assertEqual(listener.current_job()["id"], waiting["job_id"])
        self.assertIsNone(listener.pending_job())


class ParseHookBodyTests(unittest.TestCase):
    def test_empty_body_returns_empty(self) -> None:
        self.assertEqual(listener.parse_hook_body(b""), {})

    def test_invalid_json_returns_empty(self) -> None:
        self.assertEqual(listener.parse_hook_body(b"not-json"), {})

    def test_extracts_feishu_and_github(self) -> None:
        raw = (
            b'{"feishu": {"webhook": "https://open.feishu.cn/h/x", '
            b'"secret": "s3cr3t"}, "github": {"repo": "z-ph/zb", '
            b'"ref": "refs/heads/main", "sha": "abcdef0", "actor": "z-ph", '
            b'"run_url": "https://github.com/z-ph/zb/actions/runs/1", '
            b'"message": "hello"}}'
        )
        out = listener.parse_hook_body(raw)
        self.assertEqual(out["feishu"], {"webhook": "https://open.feishu.cn/h/x", "secret": "s3cr3t"})
        self.assertEqual(out["github"]["repo"], "z-ph/zb")
        self.assertEqual(out["github"]["sha"], "abcdef0")

    def test_empty_webhook_drops_feishu(self) -> None:
        out = listener.parse_hook_body(b'{"feishu": {"webhook": ""}}')
        self.assertNotIn("feishu", out)


class FeishuPayloadTests(unittest.TestCase):
    def test_success_payload(self) -> None:
        job = {
            "status": "ok",
            "returncode": 0,
            "github": {"repo": "z-ph/zb", "ref": "refs/heads/main", "sha": "abcdef0", "actor": "z-ph"},
        }
        payload = listener.build_feishu_payload(job)
        self.assertEqual(payload["msg_type"], "post")
        self.assertEqual(payload["content"]["post"]["zh_cn"]["title"], "✅ 部署成功")
        content = payload["content"]["post"]["zh_cn"]["content"]
        flat = [el.get("text", el.get("href")) for line in content for el in line]
        self.assertTrue(any("z-ph/zb" in t for t in flat))
        self.assertTrue(any("abcdef0" in t for t in flat))
        self.assertTrue(any("返回码：0" in t for t in flat))

    def test_failure_payload_includes_error_and_link(self) -> None:
        job = {
            "status": "fail",
            "returncode": 1,
            "error": "boom",
            "github": {"repo": "z-ph/zb", "run_url": "https://github.com/z-ph/zb/actions/runs/2"},
        }
        payload = listener.build_feishu_payload(job)
        self.assertEqual(payload["content"]["post"]["zh_cn"]["title"], "❌ 部署失败")
        content = payload["content"]["post"]["zh_cn"]["content"]
        link_line = [el for line in content for el in line if el.get("tag") == "a"]
        self.assertEqual(link_line[0]["href"], "https://github.com/z-ph/zb/actions/runs/2")
        flat = [el.get("text") for line in content for el in line if el.get("tag") == "text"]
        self.assertTrue(any("boom" in t for t in flat))


class FeishuSignTests(unittest.TestCase):
    def test_sign_matches_algorithm(self) -> None:
        timestamp, secret = "1599368391", "abc"
        # 独立复现官方算法：HMAC-SHA256(key=`${ts}\n${secret}`, msg=b"") → base64
        digest = __import__("hmac").new(
            f"{timestamp}\n{secret}".encode("utf-8"), b"", __import__("hashlib").sha256
        ).digest()
        expected = __import__("base64").b64encode(digest).decode("ascii")
        self.assertEqual(listener.feishu_sign(timestamp, secret), expected)

    def test_sign_changes_with_secret(self) -> None:
        ts = "1700000000"
        self.assertNotEqual(listener.feishu_sign(ts, "a"), listener.feishu_sign(ts, "b"))


class FinishJobNotifyTests(unittest.TestCase):
    def setUp(self) -> None:
        listener.reset_jobs()

    def tearDown(self) -> None:
        listener.reset_jobs()

    def test_finish_job_sends_feishu(self) -> None:
        listener.set_running_job("run1")
        listener._job["feishu"] = {"webhook": "https://open.feishu.cn/h/x", "secret": ""}
        listener._job["github"] = {"repo": "z-ph/zb", "sha": "abcdef0"}
        sent: list[tuple] = []

        def fake_send(webhook, payload, secret):
            sent.append((webhook, payload, secret))

        with mock.patch.object(listener, "start_job"), mock.patch.object(listener, "send_feishu", side_effect=fake_send):
            listener.finish_job("run1", code=0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "https://open.feishu.cn/h/x")
        self.assertEqual(sent[0][1]["content"]["post"]["zh_cn"]["title"], "✅ 部署成功")
        self.assertEqual(sent[0][2], "")

    def test_finish_job_skips_when_no_webhook(self) -> None:
        listener.set_running_job("run1")  # 无 feishu 配置
        sent: list = []

        def fake_send(*args, **kwargs):
            sent.append(args)

        with mock.patch.object(listener, "start_job"), mock.patch.object(listener, "send_feishu", side_effect=fake_send):
            listener.finish_job("run1", code=1)
        # 无 webhook 时 notify_feishu 早退，send_feishu 不被调用
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
