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


if __name__ == "__main__":
    unittest.main()
