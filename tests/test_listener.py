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


if __name__ == "__main__":
    unittest.main()
