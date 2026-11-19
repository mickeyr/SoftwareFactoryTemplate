"""Tests for factory/bin/store and its use by factory/bin/job. Run from the repo root:

  python3 -m unittest discover -s factory/tests -v

They make no network call and run no real gh. Publishing and the live view
run real git against `Remote`, a bare repository in a temporary folder standing
in for origin, and the timeout tests put a `git` on PATH whose ls-remote or
fetch hangs. `ServeTest` runs `store serve` as a process of its own.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import http.client
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

JOB = Path(__file__).resolve().parents[1] / "bin" / "job"
_loader = importlib.machinery.SourceFileLoader("job", str(JOB))
job = importlib.util.module_from_spec(importlib.util.spec_from_loader("job", _loader))
_loader.exec_module(job)
store = job.store
STORE = JOB.parent / "store"
store_git = store.git  # the real one, before any test replaces it

BRANCH = "factory-state"


def replace(test, module, **values):
    """Set module globals for the length of one test."""
    for name, value in values.items():
        test.addCleanup(setattr, module, name, getattr(module, name))
        setattr(module, name, value)


def job_json(job_id, **fields):
    return {"id": job_id, "issue": 5, "title": "x", "issue_url": "https://github.com/o/r/issues/5",
            "pr": None, "pr_url": None, "branch": f"factory/{job_id}", "worktree": "/nonexistent",
            "stage": "build", "round": 1, "round_limit": 3, "intake": None, "agents": None, "gate": None,
            "reviews": {}, "cost_usd": 0.0, "started": "2026-01-01T00:00:00Z", "ended": None, **fields}


class Remote:
    """A bare repository standing in for origin."""

    def __init__(self, path):
        self.path = Path(path)
        subprocess.run(["git", "init", "-q", "--bare", str(self.path)], check=True)

    def git(self, *args, stdin=None):
        return subprocess.run(["git", "-C", str(self.path), *args], input=stdin, capture_output=True,
                              check=True).stdout

    def head(self, branch=BRANCH):
        return self.git("rev-parse", f"refs/heads/{branch}").decode().strip()

    def parents(self, sha):
        return self.git("rev-list", "--parents", "-n", "1", sha).decode().split()[1:]

    def commits(self, branch=BRANCH):
        return self.git("rev-list", f"refs/heads/{branch}").decode().split()

    def files(self, branch=BRANCH):
        paths = self.git("ls-tree", "-r", "-z", "--name-only", f"refs/heads/{branch}").decode().split("\0")
        return {p: self.git("cat-file", "blob", f"refs/heads/{branch}:{p}") for p in paths if p}

    def entry(self, path, branch=BRANCH):
        return self.git("rev-parse", f"refs/heads/{branch}:{path}").decode().strip()

    def seed(self, files, branch=BRANCH):
        """A branch with one commit holding `files` (path -> text)."""
        tree = store.write_tree(files, lambda *args, stdin=None: self.git(
            *args, stdin=stdin.encode() if stdin is not None else None).decode().strip())
        commit = self.git("commit-tree", tree, "-m", "seed").decode().strip()
        self.git("update-ref", f"refs/heads/{branch}", commit)
        return commit


def use_remote(test, folder):
    """Point the store at a new Remote under folder; commits get a fixed author."""
    remote = Remote(Path(folder) / "origin.git")
    replace(test, store, remote_url=lambda: str(remote.path), _failed=False)
    patcher = mock.patch.dict(os.environ, {
        "FACTORY_STATE_BRANCH": BRANCH, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    patcher.start()
    test.addCleanup(patcher.stop)
    return remote


class StoreTest(unittest.TestCase):
    """A throwaway factory/jobs with FACTORY_STATE_BRANCH set and a local origin.

    `calls` lists the git commands the store ran (`push`, `fetch`, ...), and
    `before` is called with each one's arguments first: it can raise to inject
    a failure or publish something else in between."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.jobs = Path(tmp.name) / "jobs"
        self.jobs.mkdir()
        self.remote = use_remote(self, tmp.name)
        self.calls = []
        self.before = None

        def git(args, cwd, deadline, stdin=None):
            self.calls.append(args[0])
            if self.before:
                self.before(args)
            return store_git(args, cwd, deadline, stdin)

        replace(self, job, JOBS=self.jobs, _repo="o/r", _locks={}, cost_usd=lambda j: 0.0)
        replace(self, store, git=git)

    def make_job(self, job_id):
        d = self.jobs / job_id
        (d / "round-1").mkdir(parents=True)
        (d / "job.json").write_text(json.dumps(job_json(job_id), indent=2) + "\n")
        (d / "issue.md").write_text(f"# Issue for {job_id}\n")
        (d / "intake.md").write_text("DECISION: ACCEPT\n")
        (d / "spec.md").write_text("# Spec\n")
        (d / "job.json.tmp").write_text("{}")
        (d / ".lock").write_text("")
        (d / "round-1" / "checks.md").write_text("GATE: PASS\n")
        (d / "round-1" / "checks.log").write_text("log\n")
        (d / "round-1" / "integration.log").write_text("log\n")
        return d

    def save(self, job_id):
        """job.save as the commands call it, returning stderr."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            job.save(json.loads((self.jobs / job_id / "job.json").read_text()))
        return err.getvalue()

    def sync(self, job_id):
        """`job sync <id>`: (stdout, stderr), raising if it exits."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            job.main(["sync", job_id])
        return out.getvalue(), err.getvalue()

    def publish_from_elsewhere(self, job_id):
        """Another machine publishes job_id: its own store repository, no shared lock."""
        before, self.before = self.before, None
        try:
            store.commit_jobs(str(self.remote.path), BRANCH, {job_id: store.job_files(self.jobs / job_id)},
                              self.jobs.parent / "elsewhere.git")
        finally:
            self.before = before


class OffTest(StoreTest):
    def test_unset_makes_no_store_call(self):
        """Acceptance 1."""
        self.make_job("001-a")
        for value in [None, ""]:
            with self.subTest(value=value):
                if value is None:
                    os.environ.pop("FACTORY_STATE_BRANCH")
                else:
                    os.environ["FACTORY_STATE_BRANCH"] = value
                out, err = self.sync("001-a")
                self.assertEqual(self.calls, [])
                self.assertEqual(err, "")
                self.assertFalse((self.jobs / "001-a" / ".unpublished").exists())
                self.assertFalse((self.jobs / ".store.git").exists())
                self.assertTrue(out.startswith("001-a: build round 1"))


class PublishTest(StoreTest):
    def test_first_save_creates_the_branch(self):
        """Acceptance 2."""
        self.make_job("001-a")
        out, err = self.sync("001-a")
        self.assertEqual(err, "")
        self.assertEqual(self.remote.parents(self.remote.head()), [])
        files = self.remote.files()
        self.assertEqual(sorted(files), [
            "001-a/intake.md", "001-a/issue.md", "001-a/job.json", "001-a/round-1/checks.md",
            "001-a/spec.md", "FACTORY-STATE.md"])
        self.assertIn(b"factory/bin/job", files["FACTORY-STATE.md"])
        self.assertEqual(files["001-a/job.json"], (self.jobs / "001-a" / "job.json").read_bytes())
        self.assertFalse((self.jobs / "001-a" / ".unpublished").exists())
        self.assertEqual(self.calls.count("push"), 1)

    def test_later_save_is_one_fast_forward_commit(self):
        self.make_job("001-a")
        self.save("001-a")
        first = self.remote.head()
        (self.jobs / "001-a" / "build.md").write_text("STATUS: READY\n")
        self.calls.clear()
        self.save("001-a")
        self.assertEqual(self.remote.parents(self.remote.head()), [first])
        self.assertEqual(self.remote.files()["001-a/build.md"], b"STATUS: READY\n")
        self.assertEqual(self.calls.count("push"), 1)
        self.assertNotIn("fetch", self.calls)  # our own last publish is already here

    def test_fetches_what_another_machine_published(self):
        self.make_job("001-a")
        self.make_job("002-b")
        self.publish_from_elsewhere("002-b")
        self.assertEqual(self.save("001-a"), "")
        self.assertIn("fetch", self.calls)
        self.assertEqual(len(self.remote.commits()), 2)
        self.assertIn("002-b/job.json", self.remote.files())

    def test_rename_leaves_no_old_file_and_other_jobs_untouched(self):
        """Acceptance 3."""
        self.make_job("001-a")
        self.make_job("002-b")
        self.save("001-a")
        self.save("002-b")
        other = self.remote.entry("002-b")
        other_files = {p: c for p, c in self.remote.files().items() if p.startswith("002-b/")}
        (self.jobs / "001-a" / "intake.md").rename(self.jobs / "001-a" / "intake-1.md")
        self.save("001-a")
        mine = sorted(p for p in self.remote.files() if p.startswith("001-a/"))
        self.assertIn("001-a/intake-1.md", mine)
        self.assertNotIn("001-a/intake.md", mine)
        self.assertEqual(self.remote.entry("002-b"), other)
        self.assertEqual({p: c for p, c in self.remote.files().items() if p.startswith("002-b/")}, other_files)

    @unittest.skipIf(os.name == "nt", "creating a symlink needs a privilege on Windows")
    def test_symlinks_and_other_files_are_not_published(self):
        d = self.make_job("001-a")
        outside = Path(self.jobs.parent) / "secret.md"
        outside.write_text("secret\n")
        (d / "leak.md").symlink_to(outside)
        (d / "round-2").symlink_to(self.jobs.parent)
        (d / "notes.txt").write_text("x")
        (d / "round-x").mkdir()
        (d / "round-x" / "a.md").write_text("x")
        (d / "round-1" / "deep").mkdir()
        (d / "round-1" / "deep" / "a.md").write_text("x")
        self.save("001-a")
        self.assertEqual(sorted(self.remote.files()), [
            "001-a/intake.md", "001-a/issue.md", "001-a/job.json", "001-a/round-1/checks.md",
            "001-a/spec.md", "FACTORY-STATE.md"])

    def test_interleaved_publishes_both_land(self):
        """Acceptance 4: B moves the branch between A's read and A's push."""
        self.make_job("001-a")
        self.make_job("002-b")
        self.make_job("003-c")
        self.save("003-c")
        self.calls.clear()
        fired = []

        def b_wins(args):
            if args[0] == "push" and not fired:
                fired.append(True)
                self.publish_from_elsewhere("002-b")

        self.before = b_wins
        self.assertEqual(self.save("001-a"), "")
        self.assertEqual(self.calls.count("push"), 3)  # A refused, B, A again
        self.assertEqual(len(self.remote.commits()), 3)
        files = self.remote.files()
        for job_id in ["001-a", "002-b", "003-c"]:
            self.assertIn(f"{job_id}/job.json", files)
            self.assertIn(f"{job_id}/round-1/checks.md", files)

    def test_racing_branch_creation_falls_back_to_update(self):
        self.make_job("001-a")
        self.make_job("002-b")
        fired = []

        def b_creates(args):
            if args[0] == "push" and not fired:
                fired.append(True)
                self.publish_from_elsewhere("002-b")

        self.before = b_creates
        self.assertEqual(self.save("001-a"), "")
        files = self.remote.files()
        self.assertIn("001-a/job.json", files)
        self.assertIn("002-b/job.json", files)
        self.assertEqual(len(self.remote.commits()), 2)

    def test_gives_up_after_five_lost_races(self):
        self.make_job("001-a")
        self.make_job("002-b")

        def always_lose(args):
            if args[0] == "push":
                (self.jobs / "002-b" / "build.md").write_text(f"{len(self.calls)}\n")
                self.publish_from_elsewhere("002-b")

        self.before = always_lose
        err = self.save("001-a")
        self.assertEqual(self.calls.count("push"), 10)  # five of A's, each after one from elsewhere
        self.assertIn("gave up after 5 attempts", err)
        self.assertNotIn("001-a/job.json", self.remote.files())
        self.assertTrue((self.jobs / "001-a" / ".unpublished").exists())


class CloseMergedTest(StoreTest):
    """store.close_merged: jobs on the branch that this machine doesn't hold."""

    FINISHED = {"merged", "rejected", "split"}

    def elsewhere(self, job_id, **fields):
        """A job another machine published and this one has no folder for."""
        d = self.make_job(job_id)
        (d / "job.json").write_text(json.dumps(job_json(job_id, **fields), indent=2) + "\n")
        self.publish_from_elsewhere(job_id)
        shutil.rmtree(d)

    def close(self, merged):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            closed = store.close_merged(self.jobs, "main", self.FINISHED, lambda pr: pr in merged,
                                        "2026-02-02T00:00:00Z")
        return closed, err.getvalue()

    def published(self, job_id):
        return json.loads(self.remote.files()[f"{job_id}/job.json"])

    def test_closes_only_unheld_jobs_whose_pr_is_merged(self):
        self.elsewhere("001-a", stage="needs-human", pr=7, ended="2026-01-01T01:00:00Z")
        self.elsewhere("002-b", stage="needs-human", pr=8)
        self.elsewhere("003-c", stage="review", pr=9)
        self.elsewhere("004-d", stage="needs-human", pr=None)
        self.elsewhere("005-e", stage="rejected", pr=10, ended="2026-01-01T01:00:00Z")
        self.make_job("006-f")  # this machine's: `job queue` closes it through its own folder
        (self.jobs / "006-f" / "job.json").write_text(json.dumps(job_json("006-f", stage="needs-human", pr=11)))
        self.save("006-f")
        before = self.remote.files()
        closed, err = self.close({7, 9, 10, 11})
        self.assertEqual((closed, err), (["001-a", "003-c"], ""))
        after = self.remote.files()
        for job_id in ["001-a", "003-c"]:
            self.assertEqual(self.published(job_id), {**json.loads(before[f"{job_id}/job.json"]),
                                                      "stage": "merged", "ended": "2026-02-02T00:00:00Z"})
        self.assertEqual({p: c for p, c in after.items() if p not in {"001-a/job.json", "003-c/job.json"}},
                         {p: c for p, c in before.items() if p not in {"001-a/job.json", "003-c/job.json"}})
        self.assertEqual(self.remote.parents(self.remote.head()), [self.remote.commits()[1]])

    def test_missing_jobs_folder_is_created_and_the_pass_runs(self):
        self.elsewhere("001-a", stage="needs-human", pr=7)
        shutil.rmtree(self.jobs)
        self.assertEqual(self.close({7}), (["001-a"], ""))
        self.assertEqual(self.published("001-a")["stage"], "merged")
        self.assertTrue(self.jobs.is_dir())

    def test_nothing_to_close_pushes_nothing(self):
        self.elsewhere("001-a", stage="needs-human", pr=7)
        head = self.remote.head()
        self.calls.clear()
        self.assertEqual(self.close(set()), ([], ""))
        self.assertNotIn("push", self.calls)
        self.assertEqual(self.remote.head(), head)

    def test_asks_about_each_pr_once_and_skips_bad_job_files(self):
        self.elsewhere("001-a", stage="needs-human", pr=7)
        self.remote.seed({**{p: c.decode() for p, c in self.remote.files().items()},
                          "002-b/job.json": "not json\n", "003-c/job.json": json.dumps({"id": "other"})})
        asked = []
        closed = store.close_merged(self.jobs, "main", self.FINISHED, lambda pr: asked.append(pr) or True, "t")
        self.assertEqual((closed, asked), (["001-a"], [7]))

    def test_off_no_branch_or_failed_store_does_nothing(self):
        self.assertEqual(self.close({7}), ([], ""))  # no branch yet
        self.assertEqual(self.remote.git("for-each-ref").strip(), b"")
        self.elsewhere("001-a", stage="needs-human", pr=7)
        self.calls.clear()
        os.environ["FACTORY_STATE_BRANCH"] = ""
        self.assertEqual(self.close({7}), ([], ""))
        os.environ["FACTORY_STATE_BRANCH"] = BRANCH
        store._failed = True
        self.assertEqual(self.close({7}), ([], ""))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.published("001-a")["stage"], "needs-human")

    def test_failure_is_one_warning(self):
        self.elsewhere("001-a", stage="needs-human", pr=7)
        self.before = failing(store.Failed("git ls-remote: Bad Gateway"))
        closed, err = self.close({7})
        self.assertEqual(closed, [])
        self.assertEqual(len(err.splitlines()), 1)
        self.assertTrue(err.startswith("job: store: git ls-remote: Bad Gateway; "))
        self.assertFalse(list(self.jobs.glob("*/.unpublished")))


def failing(error):
    """A `before` (StoreTest's) that fails every call."""
    def before(*args):
        raise error
    return before


class FailureTest(StoreTest):
    def test_failing_store_changes_nothing_but_warns_once(self):
        """Acceptance 5."""
        self.make_job("001-a")
        os.environ.pop("FACTORY_STATE_BRANCH")
        want_out, _ = self.sync("001-a")
        want_json = (self.jobs / "001-a" / "job.json").read_bytes()
        os.environ["FACTORY_STATE_BRANCH"] = BRANCH

        self.before = failing(store.Failed("git push: remote: Service Unavailable"))
        out, err = self.sync("001-a")
        self.assertEqual(out, want_out)
        self.assertEqual((self.jobs / "001-a" / "job.json").read_bytes(), want_json)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertTrue(err.startswith("job: store: "))
        self.assertTrue(err.endswith("; will retry on the next save\n"))
        self.assertIn("Service Unavailable", err)
        self.assertTrue((self.jobs / "001-a" / ".unpublished").exists())

    def test_refused_push_is_not_retried(self):
        self.make_job("001-a")

        def refuse(args):
            if args[0] == "push":
                raise store.Failed("git push: ! [remote rejected] x -> factory-state (protected branch hook declined)")

        self.before = refuse
        err = self.save("001-a")
        self.assertEqual(self.calls.count("push"), 1)
        self.assertIn("protected branch", err)
        self.assertTrue((self.jobs / "001-a" / ".unpublished").exists())

    def test_marked_job_goes_out_with_the_next_save(self):
        """Acceptance 6."""
        self.make_job("001-a")
        self.make_job("002-b")
        self.before = failing(store.Failed("git ls-remote: Bad Gateway"))
        self.sync("001-a")
        self.assertTrue((self.jobs / "001-a" / ".unpublished").exists())
        # A new process, with GitHub back.
        store._failed = False
        self.before = None
        self.calls.clear()
        out, err = self.sync("002-b")
        self.assertEqual(err, "")
        self.assertEqual(self.calls.count("commit-tree"), 1)
        files = self.remote.files()
        self.assertIn("001-a/job.json", files)
        self.assertIn("002-b/job.json", files)
        self.assertFalse((self.jobs / "001-a" / ".unpublished").exists())
        self.assertFalse((self.jobs / "002-b" / ".unpublished").exists())

    def test_after_a_failure_the_process_only_marks(self):
        self.make_job("001-a")
        self.make_job("002-b")
        self.before = failing(store.Failed("git ls-remote: Server Error"))
        first = self.save("001-a")
        calls = len(self.calls)
        second = self.save("002-b")
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(len(first.splitlines()), 1)
        self.assertEqual(second, "")
        self.assertTrue((self.jobs / "002-b" / ".unpublished").exists())

    def test_exception_in_store_code_is_a_warning(self):
        self.make_job("001-a")
        self.before = failing(KeyError("object"))
        err = self.save("001-a")
        self.assertTrue(err.startswith("job: store: "))
        self.assertTrue((self.jobs / "001-a" / ".unpublished").exists())

    def test_refused_branch_names_make_no_call(self):
        """Acceptance 8, names."""
        self.make_job("001-a")
        for name in ["main", "factory/001-x", "bad..name"]:
            with self.subTest(name=name):
                store._failed = False
                os.environ["FACTORY_STATE_BRANCH"] = name
                err = self.save("001-a")
                self.assertEqual(self.calls, [])
                self.assertTrue(err.startswith("job: store: "), err)
                self.assertIn(repr(name), err)

    def test_branch_without_marker_gets_no_write(self):
        """Acceptance 8, a code branch."""
        before = self.remote.seed({"README.md": "code\n"}, branch="main-copy")
        os.environ["FACTORY_STATE_BRANCH"] = "main-copy"
        self.make_job("001-a")
        err = self.save("001-a")
        self.assertNotIn("push", self.calls)
        self.assertEqual(self.remote.head("main-copy"), before)
        self.assertIn("has no FACTORY-STATE.md", err)


@unittest.skipIf(os.name == "nt", "fakes git with an sh script on PATH")
class TimeoutTest(StoreTest):
    """The real store.git against a `git` on PATH whose ls-remote never answers."""

    def setUp(self):
        super().setUp()
        bin_dir = self.jobs.parent / "bin"
        bin_dir.mkdir()
        self.log = self.jobs.parent / "git.log"
        (bin_dir / "git").write_text(
            f"#!/bin/sh\nif [ \"$1\" = ls-remote ]; then echo \"$*\" >> '{self.log}'; exec sleep 60; fi\n"
            f"exec '{shutil.which('git')}' \"$@\"\n")
        (bin_dir / "git").chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_hanging_call_gives_up_and_later_saves_make_no_call(self):
        """Acceptance 7, with the limits reduced."""
        replace(self, store, CALL_TIMEOUT=0.5, PUBLISH_LIMIT=3)
        self.make_job("001-a")
        self.make_job("002-b")
        start = time.monotonic()
        err = self.save("001-a")
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertTrue(err.startswith("job: store: "))
        self.assertIn("timed out", err)
        self.assertTrue((self.jobs / "001-a" / ".unpublished").exists())
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertEqual(self.save("002-b"), "")
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertTrue((self.jobs / "002-b" / ".unpublished").exists())

    def test_default_limits(self):
        self.assertLessEqual(store.PUBLISH_LIMIT, 20)
        self.assertLessEqual(store.CALL_TIMEOUT, store.PUBLISH_LIMIT)


def fake_git(bin_dir, script):
    """A `git` on PATH that runs `script` (sh) first, then the real git."""
    if os.name == "nt":
        raise unittest.SkipTest("fakes git with an sh script on PATH")
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "git").write_text(f"#!/bin/sh\n{script}exec '{shutil.which('git')}' \"$@\"\n")
    (bin_dir / "git").chmod(0o755)


def blob(data, mode="100644"):
    return mode, data


class LiveViewTest(unittest.TestCase):
    """LiveView.poll against a crafted branch of a local Remote.

    `calls` lists the git commands the view ran (`ls-remote`, `fetch`, ...)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Resolved, as the view's own paths are: macOS and Windows hand out a temp folder under another name.
        self.outer = Path(tmp.name).resolve()
        self.root = self.outer / "live"
        self.root.mkdir()
        self.remote = use_remote(self, self.outer)
        self.view = store.LiveView(self.root, self.outer / "view.git", BRANCH)
        self.calls = []
        real = self.view.git

        def git(*args, **kwargs):
            self.calls.append(args[0])
            return real(*args, **kwargs)

        self.view.git = git

    def status(self):
        return json.loads((self.root / "live.json").read_text())

    def copied(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def tree(self, entries):
        """A tree from {name: (mode, data) or {...}}, written raw, so names and
        modes git itself would refuse (`..`, slashes, a gitlink) get in."""
        raw = b""
        for name, value in sorted(entries.items()):
            if isinstance(value, dict):
                mode, sha = "40000", self.tree(value)
            else:
                mode, sha = value[0], self.remote.git("hash-object", "-w", "--stdin", stdin=value[1]).decode().strip()
            raw += f"{mode} {name}".encode() + b"\0" + bytes.fromhex(sha)
        return self.remote.git("hash-object", "-t", "tree", "--literally", "-w", "--stdin", stdin=raw).decode().strip()

    def branch(self, entries):
        """Point the branch at a new commit holding `entries` (see tree)."""
        parent = self.remote.git("for-each-ref", "--format=%(objectname)", f"refs/heads/{BRANCH}").decode().strip()
        commit = self.remote.git("commit-tree", self.tree(entries), "-m", "x",
                                 *(["-p", parent] if parent else [])).decode().strip()
        self.remote.git("update-ref", f"refs/heads/{BRANCH}", commit)
        return commit

    def good(self, job_id="001-a", **fields):
        return blob(json.dumps(job_json(job_id, **fields)).encode())

    def test_writes_only_valid_entries(self):
        """Acceptance 3."""
        self.branch({
            "..": {"evil.md": blob(b"evil")},
            "/abs.md": blob(b"evil"),
            "001-a/../../x.md": blob(b"evil"),
            "001-a": {
                "..": {"..": {"y.md": blob(b"evil")}},
                "link.md": blob(b"/etc/passwd", "120000"),
                "sub.md": blob(b"x", "160000"),
                "run.md": blob(b"x", "100755"),
                "big.md": blob(b"x" * (store.MAX_SIZE + 1)),
                "notes.txt": blob(b"x"),
                ".hidden.md": blob(b"x"),
                "round-x": {"checks.md": blob(b"x")},
                "round-1": {"checks.md": blob(b"GATE: PASS\n"), "sub": {"deep.md": blob(b"x")}},
                "job.json": self.good(),
                "spec.md": blob(b"# Spec\n"),
            },
            "bad id": {"job.json": self.good("bad id")},
            "002-b": {"job.json": self.good("003-c"), "spec.md": blob(b"# Spec\n")},
            "004-d": {"job.json": self.good("004-d", round=101)},
            "005-e": {"job.json": blob(b"[1, 2]")},
            "006-f": {"spec.md": blob(b"no job.json\n")},
            "007-g": {"job.json": self.good("007-g", issue="<img src=x onerror=alert(1)>")},
            "008-h": {"job.json": self.good("008-h", pr="<img src=x onerror=alert(1)>")},
            "009-i": {"job.json": self.good("009-i", pr=True)},
        })
        self.view.poll()
        self.assertEqual(sorted(self.copied()), [
            "jobs/001-a/job.json", "jobs/001-a/round-1/checks.md", "jobs/001-a/spec.md", "live.json"])
        self.assertEqual(sorted(p.name for p in self.outer.iterdir()), ["live", "origin.git", "view.git"])
        self.assertEqual([p.name for p in (self.root / "jobs").iterdir()], ["001-a"])
        self.assertTrue(self.status()["ok"])

    def test_listed_size_must_match_the_bytes(self):
        self.branch({"001-a": {"job.json": self.good(), "spec.md": blob(b"# Spec\n")}})
        real = self.view.listing

        def listing(commit):
            return [{**e, "size": e["size"] + 1} if e["path"].endswith("spec.md") else e for e in real(commit)]

        self.view.listing = listing
        self.view.poll()
        status = self.status()
        self.assertFalse(status["ok"])
        self.assertIn("isn't the", status["error"])
        self.assertEqual(sorted(self.copied()), ["live.json"])

    def test_failed_read_keeps_the_last_good_copy(self):
        """Acceptance 5: the remote moved away."""
        self.branch({"001-a": {"job.json": self.good(), "spec.md": blob(b"# Spec\n")}})
        self.view.poll()
        good = self.status()
        files = self.copied()
        self.assertEqual(good, {"ok": True, "loading": False, "branch": BRANCH, "last_good": good["last_good"],
                                "error": None})
        self.assertIsNotNone(good["last_good"])
        replace(self, store, remote_url=lambda: str(self.outer / "gone.git"))
        self.view.poll()
        bad = self.status()
        self.assertFalse(bad["ok"])
        self.assertFalse(bad["loading"])
        self.assertIn("git ls-remote", bad["error"])
        self.assertEqual(bad["last_good"], good["last_good"])
        files.pop("live.json")
        self.assertEqual({p: c for p, c in self.copied().items() if p != "live.json"}, files)

    def test_missing_branch_is_not_ok(self):
        """Acceptance 4."""
        self.remote.seed({"README.md": "code\n"}, branch="main")
        self.view.poll()
        self.assertEqual(self.status(), {"ok": False, "loading": False, "branch": BRANCH, "last_good": None,
                                         "error": f"branch {BRANCH} not found"})
        self.assertNotIn("fetch", self.calls)

    def test_unchanged_head_makes_no_fetch(self):
        """Acceptance 6."""
        self.branch({"001-a": {"job.json": self.good()}})
        self.view.poll()
        self.assertEqual(self.calls.count("fetch"), 1)
        first = self.status()["last_good"]
        for _ in range(3):
            self.calls.clear()
            self.view.poll()
            self.assertEqual(self.calls, ["ls-remote"])
            self.assertTrue(self.status()["ok"])
        self.assertGreaterEqual(self.status()["last_good"], first)

    def test_follows_changes_and_writes_only_what_changed(self):
        self.branch({"001-a": {"job.json": self.good(), "intake.md": blob(b"one\n")},
                     "002-b": {"job.json": self.good("002-b")}})
        self.view.poll()
        written = []
        real = store.write_atomic
        replace(self, store, write_atomic=lambda path, data: written.append(path) or real(path, data))
        self.calls.clear()
        self.branch({"001-a": {"job.json": self.good(), "intake-1.md": blob(b"one\n"),
                               "build.md": blob(b"STATUS: READY\n")}})
        self.view.poll()
        self.assertEqual(sorted(self.copied()), [
            "jobs/001-a/build.md", "jobs/001-a/intake-1.md", "jobs/001-a/job.json", "live.json"])
        self.assertEqual(self.calls.count("fetch"), 1)
        self.assertEqual(sorted(p.relative_to(self.root).as_posix() for p in written), [
            "jobs/001-a/build.md", "jobs/001-a/intake-1.md", "live.json"])
        self.assertFalse((self.root / "jobs" / "002-b").exists())

    def test_reads_what_the_publisher_wrote(self):
        jobs = self.outer / "jobs"
        (jobs / "001-a" / "round-1").mkdir(parents=True)
        (jobs / "001-a" / "job.json").write_text(json.dumps(job_json("001-a")))
        (jobs / "001-a" / "spec.md").write_text("# Spec\n")
        (jobs / "001-a" / "round-1" / "checks.md").write_text("GATE: PASS\n")
        store.publish(jobs, "001-a", "main")
        self.view.poll()
        self.assertTrue(self.status()["ok"])
        for name in ["job.json", "spec.md", "round-1/checks.md"]:
            self.assertEqual((self.root / "jobs" / "001-a" / name).read_bytes(),
                             (jobs / "001-a" / name).read_bytes())
        self.assertFalse((self.root / "jobs" / "FACTORY-STATE.md").exists())

    def test_updating_reaches_the_dashboard_copy(self):
        """`job update`'s `updating` key, which the dashboard shows as a marker."""
        jobs = self.outer / "jobs"
        (jobs / "001-a").mkdir(parents=True)
        (jobs / "001-a" / "job.json").write_text(
            json.dumps(job_json("001-a", stage="needs-human", updating="2026-01-01T00:05:00Z")))
        store.publish(jobs, "001-a", "main")
        self.view.poll()
        self.assertTrue(self.status()["ok"])
        copy = self.root / "jobs" / "001-a" / "job.json"
        self.assertEqual(copy.read_bytes(), (jobs / "001-a" / "job.json").read_bytes())
        self.assertEqual(json.loads(copy.read_text())["updating"], "2026-01-01T00:05:00Z")

    def test_updating_that_is_not_a_time_is_still_copied(self):
        """The dashboard, not the store, handles a bad `updating` value."""
        for value in [42, "<img src=x onerror=alert(1)>"]:
            with self.subTest(value=value):
                job = self.good(updating=value)
                self.branch({"001-a": {"job.json": job}})
                self.view.poll()
                self.assertTrue(self.status()["ok"])
                self.assertEqual(self.copied()["jobs/001-a/job.json"], job[1])
                self.assertEqual(json.loads(job[1])["updating"], value)

    def test_hanging_fetch_times_out_and_the_next_poll_tries_again(self):
        """Acceptance 7, with the limit reduced."""
        self.branch({"001-a": {"job.json": self.good()}})
        log = self.outer / "git.log"
        fake_git(self.outer / "bin", f"case \" $* \" in *' fetch '*) echo \"$*\" >> '{log}'; exec sleep 60;; esac\n")
        patcher = mock.patch.dict(os.environ, {"PATH": f"{self.outer / 'bin'}{os.pathsep}{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)
        replace(self, store, FETCH_TIMEOUT=0.5)
        for n in [1, 2]:
            start = time.monotonic()
            self.view.poll()
            self.assertLess(time.monotonic() - start, 5)
            status = self.status()
            self.assertFalse(status["ok"])
            self.assertIn("timed out", status["error"])
            self.assertEqual(len(log.read_text().splitlines()), n)

    def test_default_limits(self):
        self.assertEqual(store.FETCH_TIMEOUT, 120)
        self.assertEqual(store.CALL_TIMEOUT, 10)


class ServeTest(unittest.TestCase):
    """`store serve` as a process, with a `git` on PATH that answers
    `remote get-url origin` with a local Remote and logs every call."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.outer = Path(tmp.name)
        self.remote = use_remote(self, self.outer)
        self.tmp = self.outer / "tmp"
        self.tmp.mkdir()
        self.log = self.outer / "git.log"

    def git(self, hang=None):
        fake_git(self.outer / "bin", (
            f"echo \"$*\" >> '{self.log}'\n"
            f"if [ \"$3 $4 $5\" = 'remote get-url origin' ]; then echo '{self.remote.path}'; exit 0; fi\n"
            + (f"case \" $* \" in *' {hang} '*) exec sleep 60;; esac\n" if hang else "")))

    def start(self, port, branch=BRANCH):
        env = {**os.environ, "PATH": f"{self.outer / 'bin'}{os.pathsep}{os.environ['PATH']}",
               "FACTORY_STATE_BRANCH": branch, "TMPDIR": str(self.tmp)}
        proc = subprocess.Popen([sys.executable, str(STORE), "serve", "--port", str(port)], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def stop():
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
            proc.stdout.close()
            proc.stderr.close()

        self.addCleanup(stop)
        return proc

    def free_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def get(self, port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", path)
            res = conn.getresponse()
            return res.status, res.read()
        finally:
            conn.close()

    def url_line(self, proc):
        ready, _, _ = select.select([proc.stdout], [], [], 5)
        self.assertTrue(ready, "no URL line within 5 s")
        return proc.stdout.readline()

    def test_port_opens_before_the_first_load(self):
        """Acceptance 8: ls-remote never answers."""
        self.git(hang="ls-remote")
        port = self.free_port()
        proc = self.start(port)
        self.assertTrue(self.url_line(proc).startswith(f"http://127.0.0.1:{port}/dashboard.html"))
        status, page = self.get(port, "/dashboard.html")
        self.assertEqual(status, 200)
        self.assertEqual(page, (STORE.parents[1] / "dashboard.html").read_bytes())
        status, live = self.get(port, "/live.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(live), {"ok": False, "loading": True, "branch": BRANCH, "last_good": None,
                                            "error": None})
        self.assertIsNone(proc.poll())

    def test_repository_is_outside_the_served_root(self):
        self.remote.seed({"001-a/job.json": json.dumps(job_json("001-a")), "FACTORY-STATE.md": "x\n"})
        self.git()
        port = self.free_port()
        proc = self.start(port)
        self.url_line(proc)
        deadline = time.monotonic() + 10
        while not json.loads(self.get(port, "/live.json")[1]).get("ok"):
            self.assertLess(time.monotonic(), deadline, "no good read within 10 s")
            time.sleep(0.1)
        [folder] = self.tmp.iterdir()
        self.assertTrue(folder.name.startswith("factory-live-"))
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["repo.git", "site"])
        self.assertEqual(sorted(p.name for p in (folder / "site").iterdir()), ["dashboard.html", "jobs", "live.json"])
        self.assertTrue((folder / "repo.git" / "HEAD").is_file())
        self.assertEqual([p.name for p in (folder / "site" / "jobs").iterdir()], ["001-a"])
        for path in ["/repo.git/HEAD", "/../repo.git/HEAD", "/%2e%2e/repo.git/HEAD"]:
            self.assertEqual(self.get(port, path)[0], 404, path)
        proc.terminate()
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_port_in_use_fails_at_once(self):
        """Acceptance 9."""
        self.git()
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            proc = self.start(port)
            self.assertNotEqual(proc.wait(timeout=5), 0)
        err = proc.stderr.read()
        self.assertIn(f"store: can't serve on 127.0.0.1:{port}", err)
        self.assertNotIn("ls-remote", self.log.read_text() if self.log.exists() else "")
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_invalid_branch_name_binds_nothing(self):
        self.git()
        port = self.free_port()
        proc = self.start(port, branch="bad..name")
        self.assertNotEqual(proc.wait(timeout=5), 0)
        self.assertIn("isn't a valid branch name", proc.stderr.read())
        self.assertEqual(self.log.read_text().splitlines(), ["check-ref-format --branch bad..name"])
        self.assertEqual(list(self.tmp.iterdir()), [])
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))


if __name__ == "__main__":
    unittest.main()
