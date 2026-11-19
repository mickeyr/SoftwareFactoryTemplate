"""Tests for factory/bin/gate's modes and integration run. Run from the repo root:

  python3 -m unittest discover -s factory/tests -v

Fake `docker` and `dockerd` scripts at the front of PATH stand in for the real
ones, a fake script for factory/checks.sh and a fake integration command, so
they make no Docker, build or network call.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock
import warnings
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "bin" / "gate"
_loader = importlib.machinery.SourceFileLoader("gate", str(GATE))
gate = importlib.util.module_from_spec(importlib.util.spec_from_loader("gate", _loader))
_loader.exec_module(gate)

TEST_PATH = "tests/integration/test_fake.py"
# Windows can't run a shebang script as `docker`, so the tests that need the fake daemon are Linux and macOS only.
sh_fakes = unittest.skipIf(os.name == "nt", "fakes docker and dockerd with sh scripts on PATH")

DOCKER = """#!/bin/sh
[ -e "{fake}/docker-up" ] && {{ echo "Server Version: fake"; exit 0; }}
echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock" >&2
exit 1
"""

# Records each start and its own open fds; `up` decides whether `docker info` then succeeds.
DOCKERD = """#!/bin/sh
echo started >> "{fake}/dockerd-runs"
ls -l /proc/$$/fd > "{fake}/dockerd-fds"
{up}
"""

# Counts its calls, then prints the output and exits with the code for that call.
INTEGRATION = """n=$(($(cat "{fake}/integration-calls" 2>/dev/null || echo 0) + 1))
echo $n > "{fake}/integration-calls"
echo "$@" > "{fake}/integration-args"
{body}
"""

PASSING = 'echo "4 passed"; exit 0'
FOREIGN_LINE = "/x/.worktrees/other-job/src/A.cs(1,1): error"
DOCKER_HUB_429 = ("unexpected status from HEAD request to https://registry-1.docker.io/v2/library/postgres/"
                  "manifests/sha256:0123: 429 Too Many Requests")


class GateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.fake = root / "fake"
        self.bin = root / "bin"
        self.fake.mkdir()
        self.bin.mkdir()
        # Laid out like a job's worktree under the main checkout, so the gate can tell its own name.
        self.main = root / "main"
        self.worktree = self.main / ".worktrees" / "test-job"
        self.make_repo()
        jobs = root / "factory" / "jobs"
        jobs.mkdir(parents=True)
        self.jobs = jobs
        self.log = jobs / "integration-test.log"
        for name, value in [("FACTORY", root / "factory"), ("INTEGRATION_LOCK", jobs / "integration.lock"),
                            ("DOCKERD_LOG", jobs / "dockerd.log"), ("DOCKERD_WAIT", 2), ("RATE_LIMIT_WAIT", 0.1),
                            ("CHECKS", root / "checks.sh"), ("INTEGRATION_PATHS", ["tests/integration/*"]),
                            ("INTEGRATION_COMMAND", [gate.portable.bash(), str(root / "integration.sh"), "{files}"]),
                            ("INTEGRATION_DOCKER", os.name != "nt"), ("PLATFORM", "linux")]:
            self.addCleanup(setattr, gate, name, getattr(gate, name))
            setattr(gate, name, value)
        self.set_env("PATH", f"{self.bin}{os.pathsep}{os.environ['PATH']}")
        self.set_env("CLAUDE_CODE_REMOTE", None)
        self.fake_bin("docker", DOCKER.format(fake=self.fake))
        self.fake_dockerd(up=True)
        self.fake_integration(PASSING)
        # The gate leaves dockerd running on purpose, so Popen warns when its object is collected.
        self.enterContext(warnings.catch_warnings())
        warnings.simplefilter("ignore", ResourceWarning)

    def set_env(self, name, value):
        self.addCleanup(self.restore_env, name, os.environ.get(name))
        self.restore_env(name, value)

    @staticmethod
    def restore_env(name, value):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def make_repo(self):
        path = TEST_PATH
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}

        def git(*args):
            subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=self.worktree, env=env,
                           check=True, capture_output=True)

        self.worktree.mkdir(parents=True)
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "--no-verify", "-m", "base")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        (self.worktree / path).parent.mkdir(parents=True)
        (self.worktree / path).write_text("def test_passes(): pass\n")
        git("add", path)
        git("commit", "-q", "--no-verify", "-m", "test")

    def fake_bin(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def fake_dockerd(self, up):
        self.fake_bin("dockerd", DOCKERD.format(fake=self.fake, up=f'touch "{self.fake}/docker-up"' if up else ""))

    def fake_integration(self, body):
        (Path(self.tmp.name) / "integration.sh").write_text(INTEGRATION.format(fake=self.fake, body=body))

    def docker_up(self):
        (self.fake / "docker-up").touch()

    def dockerd_runs(self):
        runs = self.fake / "dockerd-runs"
        return len(runs.read_text().splitlines()) if runs.exists() else 0

    def integration_calls(self):
        calls = self.fake / "integration-calls"
        return int(calls.read_text()) if calls.exists() else 0

    def run_gate(self):
        return gate.integration(str(self.worktree), "test-job", self.log)


class GateIntegrationTest(GateTestCase):

    def assert_docker_not_reachable(self, ok, result, report):
        self.assertFalse(ok)
        self.assertEqual(result, "integration tests failed: Docker not reachable")
        self.assertEqual(report, [
            f"Files: `{TEST_PATH}`",
            "Failed: Docker is not reachable (`docker info` failed), so integration tests could not run.",
            f"Full log: `{self.log}`",
        ])
        self.assertIn("Cannot connect to the Docker daemon", self.log.read_text())

    @sh_fakes
    def test_docker_up_does_not_start_dockerd(self):
        self.set_env("CLAUDE_CODE_REMOTE", "true")
        self.docker_up()
        ok, result, _ = self.run_gate()
        self.assertTrue(ok)
        self.assertRegex(result, r"^integration tests passed in \d+s$")
        self.assertEqual(self.dockerd_runs(), 0)

    @sh_fakes
    def test_docker_down_outside_cloud_session_starts_nothing(self):
        self.assert_docker_not_reachable(*self.run_gate())
        self.assertEqual(self.dockerd_runs(), 0)
        self.assertEqual(self.integration_calls(), 0)

    @sh_fakes
    def test_docker_down_with_remote_not_exactly_true_starts_nothing(self):
        for value in ["1", "TRUE", ""]:
            with self.subTest(CLAUDE_CODE_REMOTE=value):
                self.set_env("CLAUDE_CODE_REMOTE", value)
                # The gate holds the lock until its process exits, so each run needs its own.
                gate.INTEGRATION_LOCK = self.log.with_name(f"integration-{value}.lock")
                self.assert_docker_not_reachable(*self.run_gate())
                self.assertEqual(self.dockerd_runs(), 0)

    @sh_fakes
    def test_cloud_session_starts_dockerd_without_the_lock(self):
        self.set_env("CLAUDE_CODE_REMOTE", "true")
        ok, result, _ = self.run_gate()
        self.assertTrue(ok, result)
        self.assertEqual(self.dockerd_runs(), 1)
        self.assertEqual(self.integration_calls(), 1)
        fds = (self.fake / "dockerd-fds").read_text()
        self.assertIn(str(gate.DOCKERD_LOG), fds)
        self.assertNotIn(str(gate.INTEGRATION_LOCK), fds)

    @sh_fakes
    def test_cloud_session_dockerd_never_up_names_its_log(self):
        self.set_env("CLAUDE_CODE_REMOTE", "true")
        self.fake_dockerd(up=False)
        ok, result, report = self.run_gate()
        self.assertFalse(ok)
        self.assertEqual(result, "integration tests failed: Docker not reachable")
        self.assertEqual(self.dockerd_runs(), 1)
        self.assertEqual(self.integration_calls(), 0)
        self.assertIn(f"Started `dockerd`, but `docker info` still failed after 2s; its log is `{gate.DOCKERD_LOG}`.",
                      report)

    def test_rate_limited_class_passes_on_retry(self):
        self.docker_up()
        self.fake_integration(f'if [ $n -eq 1 ]; then '
                         f'echo "{DOCKER_HUB_429}"; exit 1; fi\n{PASSING}')
        ok, result, report = self.run_gate()
        self.assertTrue(ok, result)
        self.assertEqual(self.integration_calls(), 2)
        self.assertRegex(result, r"^integration tests passed in \d+s$")
        retries = [line for line in report if line.startswith("Retrying")]
        self.assertEqual(retries, ["Retrying after a Docker Hub rate limit (attempt 2 of 3)."])
        self.assertIn(retries[0], self.log.read_text())

    def test_rate_limit_that_persists_fails_after_three_attempts(self):
        self.docker_up()
        self.fake_integration('echo "toomanyrequests: You have reached your unauthenticated pull rate limit."; exit 1')
        ok, result, report = self.run_gate()
        self.assertFalse(ok)
        self.assertEqual(self.integration_calls(), 3)
        self.assertRegex(result, r"^integration tests failed: Docker Hub rate limit in \d+s$")
        self.assertEqual(len([line for line in report if line.startswith("Retrying")]), 2)
        self.assertIn("Failed: Docker Hub rate-limited image pulls after 3 attempts.", report)

    def test_project_too_many_requests_failure_is_not_retried(self):
        self.docker_up()
        self.fake_integration('echo "Expected TooManyRequests but was OK"; exit 1')
        ok, result, report = self.run_gate()
        self.assertFalse(ok)
        self.assertEqual(self.integration_calls(), 1)
        self.assertRegex(result, r"^integration tests failed in \d+s$")
        self.assertFalse([line for line in report if line.startswith("Retrying")])
        self.assertIn("Failed: the integration command exited 1.", report)

    def test_output_naming_another_worktree_fails_a_passing_run(self):
        self.docker_up()
        self.fake_integration(f'echo "{FOREIGN_LINE}"; {PASSING}')
        ok, result, report = self.run_gate()
        self.assertFalse(ok)
        self.assertRegex(result, r"^integration tests failed: output named another job's worktree in \d+s$")
        self.assertIn(f"- .worktrees/other-job: {FOREIGN_LINE}", "\n".join(report))

    def test_output_naming_own_worktree_passes(self):
        self.docker_up()
        self.fake_integration(f'echo "{self.worktree}/src/A.cs(1,1): warning"; {PASSING}')
        ok, result, _ = self.run_gate()
        self.assertTrue(ok, result)

    def test_changed_files_are_the_script_arguments(self):
        self.docker_up()
        ok, result, _ = self.run_gate()
        self.assertTrue(ok, result)
        self.assertEqual((self.fake / "integration-args").read_text().strip(), TEST_PATH)

    def test_no_matching_file_skips_without_docker(self):
        gate.INTEGRATION_PATHS = ["other/*"]
        ok, result, report = self.run_gate()
        self.assertTrue(ok)
        self.assertEqual(result, "integration tests skipped")
        self.assertEqual(report, ["Skipped: no integration test files changed."])
        self.assertEqual(self.integration_calls(), 0)

    def test_no_command_skips(self):
        for command in ([], {}):
            with self.subTest(command=command):
                gate.INTEGRATION_COMMAND = command
                ok, result, report = self.run_gate()
                self.assertTrue(ok)
                self.assertEqual(report, ["Skipped: config.json has no `integration.command`."])
        self.assertEqual(self.integration_calls(), 0)

    def test_files_token_is_replaced_where_it_stands(self):
        gate.INTEGRATION_COMMAND = ["run", "{files}", "--flag", "a{files}"]
        self.assertEqual(gate.integration_command(["x", "y"]), ["run", "x", "y", "--flag", "a{files}"])

    def test_command_for_this_platform_wins_over_default(self):
        script = str(Path(self.tmp.name) / "integration.sh")
        gate.INTEGRATION_COMMAND = {"windows": ["run", script, "win"], "default": ["run", script, "other"]}
        for platform, arg in [("windows", "win"), ("macos", "other")]:
            with self.subTest(platform=platform):
                gate.PLATFORM = platform
                self.assertEqual(gate.integration_command([]), ["run", script, arg])

    def test_platform_without_a_command_fails_and_names_it(self):
        self.docker_up()
        gate.INTEGRATION_COMMAND = {"windows": ["never-run"]}
        ok, result, report = self.run_gate()
        self.assertFalse(ok)
        self.assertEqual(result, "integration tests failed: no command for linux")
        self.assertIn("has no `linux` or `default` entry", report[0])

    def test_command_not_on_path_fails_and_names_it(self):
        self.docker_up()
        gate.INTEGRATION_COMMAND = ["no-such-test-runner", "{files}"]
        ok, result, report = self.run_gate()
        self.assertFalse(ok)
        self.assertEqual(result, "integration tests failed: command not found")
        self.assertIn("`no-such-test-runner`", report[0])

    def test_malformed_command_stops_the_gate(self):
        gate.INTEGRATION_COMMAND = "pytest {files}"
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as raised:
            self.run_gate()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("must be a list of strings", err.getvalue())

    def test_docker_not_needed_runs_with_docker_down(self):
        gate.INTEGRATION_DOCKER = False
        ok, result, _ = self.run_gate()
        self.assertTrue(ok, result)
        self.assertEqual(self.integration_calls(), 1)


class ForeignWorktreesTest(unittest.TestCase):
    OWN = "/home/u/repo/.worktrees/255-own"

    def test_own_worktree_is_not_foreign(self):
        output = f"/home/u/repo/src/A.cs ok\n{self.OWN}/src/A.cs(1,1): error\nin /home/u/repo/.worktrees/255-own\n"
        self.assertEqual(gate.foreign_worktrees(self.OWN, output), [])

    def test_other_worktree_is_named_with_its_first_line(self):
        output = f"{self.OWN}/a\n  /home/u/repo/.worktrees/163-other/src/A.cs: error\n/x/.worktrees/163-other/b\n"
        self.assertEqual(gate.foreign_worktrees(self.OWN, output),
                         [("163-other", "/home/u/repo/.worktrees/163-other/src/A.cs: error")])

    def test_name_extending_own_is_foreign(self):
        output = "/home/u/repo/.worktrees/255-own-other/obj/x.dll\n"
        self.assertEqual(gate.foreign_worktrees(self.OWN, output),
                         [("255-own-other", "/home/u/repo/.worktrees/255-own-other/obj/x.dll")])

    def test_worktree_outside_worktrees_folder_counts_every_match(self):
        output = "/home/u/repo/.worktrees/a/x\n/home/u/repo/.worktrees/b/y\n"
        self.assertEqual([name for name, _ in gate.foreign_worktrees("/home/u/checkout", output)], ["a", "b"])


class GateModesTest(GateTestCase):
    def setUp(self):
        super().setUp()
        self.job_dir = self.jobs / "test-job"
        (self.job_dir / "round-1").mkdir(parents=True)
        (self.job_dir / "job.json").write_text(json.dumps({"worktree": str(self.worktree), "round": 1}))
        self.docker_up()

    def fake_checks(self, line, code=0):
        script = Path(self.tmp.name) / "checks.sh"
        script.write_text(f'env > "{self.fake}/checks-env"\necho "{line}"\necho "to stderr" >&2\nexit {code}\n')

    def own_line(self):
        return f"{self.worktree}/src/A.cs(1,1): warning"

    def run_mode(self, fn, *args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            if stdin is not None:
                with unittest.mock.patch.object(gate.sys, "stdin", io.StringIO(stdin)):
                    fn(*args)
            else:
                fn(*args)
        return raised.exception.code, out.getvalue(), err.getvalue()

    def assert_foreign_gate_fails(self, mode):
        self.fake_checks(FOREIGN_LINE)
        code, out, _ = self.run_mode(gate.gate, "test-job", mode)
        self.assertEqual(code, 1)
        self.assertIn("GATE: FAIL", out)
        self.assertIn(".worktrees/other-job", out)
        text = (self.job_dir / "round-1" / ("merge-checks.md" if mode == "--merge" else "checks.md")).read_text()
        self.assertTrue(text.startswith("GATE: FAIL\nCOMMIT: "))
        summary = text.split("## Summary\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("- Foreign worktrees: .worktrees/other-job named in checks.sh output", summary)
        foreign = text.split("## Foreign worktrees\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(foreign, f"- .worktrees/other-job: {FOREIGN_LINE}")
        self.assertIn("Skipped: checks.sh output named another job's worktree.", text)
        self.assertEqual(self.integration_calls(), 0)

    def test_gate_fails_when_checks_output_names_another_worktree(self):
        self.assert_foreign_gate_fails("gate")

    def test_merge_gate_fails_when_checks_output_names_another_worktree(self):
        self.assert_foreign_gate_fails("--merge")

    def test_gate_passes_when_checks_output_names_only_own_worktree(self):
        self.fake_checks(self.own_line())
        code, out, _ = self.run_mode(gate.gate, "test-job", "gate")
        self.assertEqual(code, 0, out)
        text = (self.job_dir / "round-1" / "checks.md").read_text()
        self.assertTrue(text.startswith("GATE: PASS\n"))
        self.assertIn("## Foreign worktrees\nNone named.", text)
        self.assertEqual(self.integration_calls(), 1)

    def test_update_writes_only_update_checks(self):
        self.fake_checks(self.own_line())
        round_dir = self.job_dir / "round-1"
        (round_dir / "checks.md").write_text("GATE: PASS\nCOMMIT: old\n")
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.worktree, capture_output=True,
                                text=True, check=True).stdout.strip()
        code, out, _ = self.run_mode(gate.main, ["test-job", "--update"])
        self.assertEqual(code, 0, out)
        text = (round_dir / "update-checks.md").read_text()
        self.assertTrue(text.startswith(f"GATE: PASS\nCOMMIT: {commit}\n"))
        self.assertIn(f"- Commit {commit[:7]}, brought up to date with the latest main by job update\n", text)
        self.assertEqual((round_dir / "checks.md").read_text(), "GATE: PASS\nCOMMIT: old\n")
        self.assertEqual(sorted(p.name for p in round_dir.iterdir()),
                         ["checks.md", "update-checks.log", "update-checks.md", "update-integration.log"])

    def test_usage_lists_update(self):
        code, _, err = self.run_mode(gate.main, [])
        self.assertEqual(code, 2)
        self.assertIn("gate <job-id> --update", err)

    def test_checks_fails_when_output_names_another_worktree(self):
        self.fake_checks(FOREIGN_LINE)
        code, out, err = self.run_mode(gate.checks, "test-job")
        self.assertEqual(code, 1)
        self.assertIn(FOREIGN_LINE, out)
        self.assertIn("to stderr", out)
        self.assertIn(f"- .worktrees/other-job: {FOREIGN_LINE}", err)

    def test_checks_keeps_a_failing_exit_code(self):
        self.fake_checks(FOREIGN_LINE, code=3)
        code, _, err = self.run_mode(gate.checks, "test-job")
        self.assertEqual(code, 3)
        self.assertIn(".worktrees/other-job", err)

    def test_checks_passes_when_output_names_only_own_worktree(self):
        self.fake_checks(self.own_line())
        code, out, err = self.run_mode(gate.checks, "test-job")
        self.assertEqual(code, 0, err)
        self.assertIn(self.own_line(), out)

    def test_hook_fails_when_output_names_another_worktree(self):
        self.fake_checks(FOREIGN_LINE)
        transcript = Path(self.tmp.name) / "transcript.jsonl"
        transcript.write_text(json.dumps({"message": {"content": "Job: test-job. Job folder: x."}}) + "\n")
        code, _, err = self.run_mode(gate.hook, stdin=json.dumps({"agent_transcript_path": str(transcript)}))
        self.assertEqual(code, 2)
        self.assertIn(f"- .worktrees/other-job: {FOREIGN_LINE}", err)
        self.assertIn("factory/bin/gate test-job --checks", err)



if __name__ == "__main__":
    unittest.main()
