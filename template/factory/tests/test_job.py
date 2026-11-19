"""Tests for factory/bin/job. Run from the repo root:

  python3 -m unittest discover -s factory/tests -v

They make no gh or network call: `StubGh` stands in for `job.gh` and
returns REST-shaped JSON. Most stub git too; `IntakeWorktreeTest` runs local
git against a bare "origin" repository in a temp directory.
"""

import contextlib
import copy
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

JOB = Path(__file__).resolve().parents[1] / "bin" / "job"
_loader = importlib.machinery.SourceFileLoader("job", str(JOB))
job = importlib.util.module_from_spec(importlib.util.spec_from_loader("job", _loader))
_loader.exec_module(job)

URL = "https://github.com/o/r/issues/"
NOT_FOUND = job.Refused("`gh api` failed: gh: Not Found (HTTP 404)")


def open_issue(n):
    return {"number": n, "state": "open", "state_reason": None, "html_url": f"{URL}{n}"}


def closed_issue(n, reason="completed"):
    return {"number": n, "state": "closed", "state_reason": reason, "html_url": f"{URL}{n}"}


def pull_request(n):
    return {"number": n, "state": "closed", "state_reason": None,
            "html_url": f"https://github.com/o/r/pull/{n}", "pull_request": {}}


def issue(n, author="alice", state="open", body="", labels=("factory",)):
    return {"number": n, "title": f"Issue {n}", "body": body, "state": state, "state_reason": None,
            "html_url": f"{URL}{n}", "user": {"login": author, "type": "User"},
            "labels": [{"name": name} for name in labels], "comments": 0}


def comment_json(author, created_at, body):
    return {"user": {"login": author}, "created_at": created_at, "body": body}


def replace(test, **values):
    """Set module globals of job for the length of one test."""
    for name, value in values.items():
        test.addCleanup(setattr, job, name, getattr(job, name))
        setattr(job, name, value)


class StubGh:
    """Replaces job.gh. Records every argument list and answers by `METHOD path`
    for `gh api` calls, or by the first two words otherwise. An answer is JSON
    data, a job.Refused to raise, or an iterator giving one answer per call."""

    def __init__(self, test, answers=None):
        self.calls = []
        self.answers = answers or {}
        replace(test, gh=self, _repo="o/r", _operator="op", _issue_states={})

    @staticmethod
    def key(args):
        if args[0] != "api":
            return " ".join(args[:2])
        method = args[args.index("-X") + 1] if "-X" in args else "GET"
        return f"{method} {next(a for a in args[1:] if a.startswith('repos/'))}"

    def keys(self):
        """Each call's key, without the page number of a paged list."""
        return [self.key(c).partition("&page=")[0] for c in self.calls]

    def __call__(self, *args):
        self.calls.append(list(args))
        key, _, page = self.key(args).partition("&page=")
        answer = self.answers.get(key, {})
        if page and isinstance(answer, list):  # a paged list: the answer is its pages, cut again by job.PAGE_SIZE
            items = [item for part in answer for item in part]
            answer = items[(int(page) - 1) * job.PAGE_SIZE:int(page) * job.PAGE_SIZE]
        if hasattr(answer, "__next__"):
            answer = next(answer)
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer)


class DependenciesTest(unittest.TestCase):
    def test_single_number(self):
        self.assertEqual(job.dependencies("Depends on #12"), [12])

    def test_comma_separated_list(self):
        self.assertEqual(job.dependencies("Depends on #12, #3 and #45"), [3, 12, 45])

    def test_lower_case(self):
        self.assertEqual(job.dependencies("depends on #7"), [7])

    def test_upper_case(self):
        self.assertEqual(job.dependencies("DEPENDS ON #7"), [7])

    def test_mixed_case(self):
        self.assertEqual(job.dependencies("dePends On #7"), [7])

    def test_leading_whitespace(self):
        self.assertEqual(job.dependencies("Intro\n   \tDepends on #8"), [8])

    def test_crlf_line_endings(self):
        self.assertEqual(job.dependencies("Intro\r\nDepends on #9\r\nMore #10\r\n"), [9])

    def test_several_lines(self):
        self.assertEqual(job.dependencies("Depends on #2\ntext\nDepends on #1"), [1, 2])

    def test_duplicates(self):
        self.assertEqual(job.dependencies("Depends on #4, #4\nDepends on #4"), [4])

    def test_other_repo_references_ignored(self):
        self.assertEqual(job.dependencies("Depends on owner/repo#5, other#6, #7"), [7])

    def test_no_dependency_line(self):
        self.assertEqual(job.dependencies("Fix the thing.\nSee #3."), [])

    def test_empty_body(self):
        self.assertEqual(job.dependencies(""), [])

    def test_none_body(self):
        self.assertEqual(job.dependencies(None), [])

    def test_reference_on_other_line_ignored(self):
        self.assertEqual(job.dependencies("This depends on #3\nDepends on #4"), [4])

    def test_leading_zeros(self):
        self.assertEqual(job.dependencies("Depends on #00000000000012"), [12])

    def test_overlong_number_reads_first_ten_digits(self):
        self.assertEqual(job.dependencies("Depends on #9223372036854775808, #5"), [5, 9223372036])

    def test_thousands_of_digits_parse(self):
        self.assertEqual(job.dependencies("Depends on #" + "9" * 5000), [9999999999])


class DependencyActionTest(unittest.TestCase):
    def test_no_dependencies_is_new(self):
        self.assertEqual(job.dependency_action(20, [], {}), ("new", ""))

    def test_all_closed_as_completed_is_new(self):
        states = {1: closed_issue(1), 2: closed_issue(2)}
        self.assertEqual(job.dependency_action(20, [1, 2], states), ("new", ""))

    def test_one_open_holds(self):
        self.assertEqual(job.dependency_action(20, [12], {12: open_issue(12)}),
                         ("hold", "waiting for #12"))

    def test_open_and_completed_holds_on_open_only(self):
        states = {11: closed_issue(11), 12: open_issue(12), 13: open_issue(13)}
        self.assertEqual(job.dependency_action(20, [11, 12, 13], states),
                         ("hold", "waiting for #12, #13"))

    def test_closed_as_not_planned_waits(self):
        action, note = job.dependency_action(20, [12], {12: closed_issue(12, "not_planned")})
        self.assertEqual(action, "wait")
        self.assertIn("#12 closed as not planned", note)
        self.assertIn("Depends on", note)

    def test_closed_as_duplicate_waits(self):
        action, note = job.dependency_action(20, [12], {12: closed_issue(12, "duplicate")})
        self.assertEqual(action, "wait")
        self.assertIn("#12 closed as duplicate", note)

    def test_closed_without_reason_waits(self):
        action, note = job.dependency_action(20, [12], {12: closed_issue(12, None)})
        self.assertEqual(action, "wait")
        self.assertIn("#12 closed without a reason", note)

    def test_missing_waits(self):
        action, note = job.dependency_action(20, [99], {99: None})
        self.assertEqual(action, "wait")
        self.assertIn("#99 does not exist", note)

    def test_pull_request_waits(self):
        action, note = job.dependency_action(20, [143], {143: pull_request(143)})
        self.assertEqual(action, "wait")
        self.assertIn("#143 is a pull request", note)

    def test_self_dependency_waits(self):
        action, note = job.dependency_action(170, [170], {})
        self.assertEqual(action, "wait")
        self.assertIn("#170 is this issue", note)

    def test_open_and_not_planned_waits(self):
        states = {12: open_issue(12), 13: closed_issue(13, "not_planned")}
        action, note = job.dependency_action(20, [12, 13], states)
        self.assertEqual(action, "wait")
        self.assertIn("#13 closed as not planned", note)
        self.assertNotIn("#12", note)

    def test_number_too_large_waits(self):
        action, note = job.dependency_action(20, [9223372036], {})
        self.assertEqual(action, "wait")
        self.assertIn("#9223372036 is not a valid issue number", note)

    def test_zero_waits(self):
        action, note = job.dependency_action(20, [0], {})
        self.assertEqual(action, "wait")
        self.assertIn("#0 is not a valid issue number", note)

    def test_mutual_dependencies_each_hold(self):
        self.assertEqual(job.dependency_action(30, [31], {31: open_issue(31)}),
                         ("hold", "waiting for #31"))
        self.assertEqual(job.dependency_action(31, [30], {30: open_issue(30)}),
                         ("hold", "waiting for #30"))


class DependencyStatesTest(unittest.TestCase):
    """issue_state calls gh, so it is replaced to record which numbers get looked up."""

    def setUp(self):
        self.looked_up = []
        real = job.issue_state
        job.issue_state = lambda n: self.looked_up.append(n) or open_issue(n)
        self.addCleanup(setattr, job, "issue_state", real)

    def test_skips_self_and_invalid_numbers(self):
        states = job.dependency_states(20, [0, 5, 20, 999_999_999, 1_000_000_000])
        self.assertEqual(self.looked_up, [5, 999_999_999])
        self.assertEqual(sorted(states), [5, 999_999_999])

    def test_invalid_reference_only_affects_its_issue(self):
        body = "Depends on #9223372036854775808"
        deps = job.dependencies(body)
        action, note = job.dependency_action(20, deps, job.dependency_states(20, deps))
        self.assertEqual(self.looked_up, [])
        self.assertEqual(action, "wait")
        self.assertIn("is not a valid issue number", note)


class RepoNameTest(unittest.TestCase):
    def test_ssh(self):
        self.assertEqual(job.parse_repo("git@github.com:acme/widgets.git\n"),
                         "acme/widgets")

    def test_https_with_git(self):
        self.assertEqual(job.parse_repo("https://github.com/o/r.git"), "o/r")

    def test_https_without_git(self):
        self.assertEqual(job.parse_repo("https://github.com/o/r"), "o/r")

    def test_https_trailing_slash(self):
        self.assertEqual(job.parse_repo("https://github.com/o/r/"), "o/r")

    def test_ssh_url_form(self):
        self.assertEqual(job.parse_repo("ssh://git@github.com:22/o/my.repo_1-x.git"), "o/my.repo_1-x")

    def test_proxy(self):
        self.assertEqual(job.parse_repo("http://local_proxy@127.0.0.1:41235/git/acme/widgets"),
                         "acme/widgets")

    def test_unparseable_refused(self):
        for url in ["", "origin", "https://github.com/o/bad name", "https://github.com/o/r?x=1",
                    "https://github.com/o/..", "https://github.com/o/.git", "https://github.com/o/r\n/",
                    "https://github.com/o\t/r", "https://github.com/o/r\n.git"]:
            with self.subTest(url=url), self.assertRaises(job.Refused) as e:
                job.parse_repo(url)
            self.assertIn("expected a URL whose path ends in <owner>/<repo>", str(e.exception))
            self.assertIn(repr(url.strip()), str(e.exception))

    def test_read_once_from_origin(self):
        calls = []
        replace(self, _repo=None, run=lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(
            cmd, 0, "https://github.com/o/r.git\n", ""))
        self.assertEqual((job.repo(), job.repo()), ("o/r", "o/r"))
        self.assertEqual(calls, [["git", "remote", "get-url", "origin"]])


class CostTest(unittest.TestCase):
    """The real cost_usd against a throwaway transcripts folder."""

    PRICES = {"test-model": {"input": 1.0, "output": 10.0, "cache_write_5m": 2.0,
                             "cache_write_1h": 4.0, "cache_read": 0.1}}

    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        config = tempfile.TemporaryDirectory()
        self.addCleanup(config.cleanup)
        os.environ["CLAUDE_CONFIG_DIR"] = config.name
        jobs = tempfile.TemporaryDirectory()
        self.addCleanup(jobs.cleanup)
        replace(self, JOBS=Path(jobs.name), CONFIG={"prices_usd_per_mtok": self.PRICES})
        self.job = {"id": "001-x"}
        self.prompt = json.dumps({"type": "user", "message": {"role": "user", "content": (
            f"Job: 001-x. Job folder: {job.job_dir('001-x')}/. Build round 1.")}})

    @staticmethod
    def usage(message_id, **tokens):
        return json.dumps({"type": "assistant", "message": {"id": message_id, "model": "test-model",
                                                              "usage": tokens}})

    def transcript(self, name, data):
        path = job.transcripts_dir() / "session" / "subagents" / f"agent-{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)

    def test_damaged_and_cut_off_lines_skipped(self):
        cut_off = self.usage("c", output_tokens=500000)[:40]
        self.transcript("a", "\n".join([
            self.prompt,
            self.usage("a", input_tokens=1000000, output_tokens=100000),  # 1.00 + 1.00
            '{"type": "assistant", "message": not json',
            self.usage("b", input_tokens=230000),  # 0.23
            cut_off,
        ]))
        self.assertEqual(job.cost_usd(self.job), 2.23)

    def test_damaged_first_line_skips_only_that_transcript(self):
        self.transcript("a", "\n".join([
            self.prompt[:-5],
            self.usage("a", output_tokens=111000),  # 1.11 if it were counted
        ]) + "\n")
        self.transcript("b", "\n".join([
            self.prompt,
            self.usage("b", output_tokens=37000),  # 0.37
        ]) + "\n")
        self.assertEqual(job.cost_usd(self.job), 0.37)

    def test_wrong_shapes_skipped(self):
        no_id = json.dumps({"type": "assistant", "message": {"model": "test-model",
                                                               "usage": {"output_tokens": 900000}}})
        no_model = json.dumps({"type": "assistant", "message": {"id": "m",
                                                                  "usage": {"output_tokens": 900000}}})
        usage_not_object = json.dumps({"type": "assistant", "message": {"id": "u", "model": "test-model",
                                                                          "usage": [1, 2]}})
        self.transcript("a", "\n".join([
            self.prompt,
            "[1, 2]",
            "7",
            no_id,
            no_model,
            usage_not_object,
            self.usage("a", output_tokens=42000),  # 0.42
        ]) + "\n")
        self.assertEqual(job.cost_usd(self.job), 0.42)

    def test_invalid_utf8_does_not_stop_reading(self):
        self.transcript("a", b"\n".join([
            self.prompt.encode(),
            b'{"type": "assistant", "message": \xff\xfe broken}',
            self.usage("a", input_tokens=150000, output_tokens=20000).encode(),  # 0.15 + 0.20
        ]) + b"\n")
        self.assertEqual(job.cost_usd(self.job), 0.35)

    def test_shapeless_first_lines_tie_to_no_job(self):
        for line in ["not json", "[1, 2]", '"text"', '{"message": 5}', '{"message": {"content": 5}}',
                     '{"message": {}}']:
            with self.subTest(line=line):
                self.assertEqual(job.prompt_text(line), "")


class JobFilesTest(unittest.TestCase):
    """A throwaway factory/jobs, with gh, git and cost stubbed."""

    def setUp(self):
        self.gh = StubGh(self)
        # save() publishes when the shell sets it; these tests are about the local files.
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("FACTORY_STATE_BRANCH", None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runs = []

        def fake_run(cmd, cwd=None, check=True):
            self.runs.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "abc123\n" if cmd[:2] == ["git", "rev-parse"] else "", "")

        replace(self, JOBS=Path(tmp.name), run=fake_run, _locks={}, cost_usd=lambda j: 0.0,
                transcripts_dir=lambda: Path(tmp.name) / ".no-transcripts",
                git=lambda j, *args, check=True: fake_run(["git", *args]))

    def make_job(self, **fields):
        j = {"id": "001-x", "issue": 5, "title": "x", "issue_url": f"{URL}5", "pr": None, "pr_url": None,
             "branch": "factory/001-x", "worktree": str(job.JOBS / "no-worktree"), "stage": "merging",
             "round": 1, "round_limit": 3, "intake": None, "agents": None, "gate": None, "reviews": {},
             "cost_usd": 0.0, "started": "2026-01-01T00:00:00Z", "ended": None, **fields}
        job.job_dir(j["id"]).mkdir()
        job.save(j)
        return j

    def read(self, name):
        return (job.job_dir("001-x") / name).read_text()


class IssueFieldsTest(JobFilesTest):
    def answer_issue(self, data, comments=()):
        self.gh.answers["GET repos/o/r/issues/5"] = data
        self.gh.answers["GET repos/o/r/issues/5/comments?per_page=100"] = [list(comments)]

    def test_create_refuses_closed_issue(self):
        self.answer_issue(issue(5, state="closed"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            job.cmd_create("5")
        self.assertIn("issue #5 is closed", err.getvalue())

    def test_job_id_is_the_issue_number_and_a_title_slug(self):
        (job.JOBS / "001-older-job").mkdir(parents=True)
        for number, want in [(7, "007-issue-7"), (1234, "1234-issue-1234")]:
            self.gh.answers[f"GET repos/o/r/issues/{number}"] = issue(number)
            self.gh.answers[f"GET repos/o/r/issues/{number}/comments?per_page=100"] = [[]]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                job.cmd_create(str(number))
            self.assertEqual(out.getvalue().strip(), want)
            self.assertEqual(json.loads((job.JOBS / want / "job.json").read_text())["branch"], f"factory/{want}")

    def test_create_maps_rest_fields(self):
        self.answer_issue(issue(5, author="github-actions[bot]"),
                          [comment_json("github-actions[bot]", "2026-11-01T10:00:00Z", "More")])
        with contextlib.redirect_stdout(io.StringIO()):
            job.cmd_create("5")
        created = json.loads((job.JOBS / "005-issue-5" / "job.json").read_text())
        self.assertEqual(created["issue_url"], f"{URL}5")
        issue_md = (job.JOBS / "005-issue-5" / "issue.md").read_text()
        self.assertIn(f"Opened by @github-actions[bot]. {URL}5\n", issue_md)
        self.assertIn("### @github-actions[bot], 2026-11-01T10:00:00Z", issue_md)


DEPENDABOT_PR = {"number": 9, "html_url": "https://github.com/o/r/pull/9", "state": "open",
                 "user": {"login": "dependabot[bot]", "type": "Bot"}, "base": {"ref": "main"},
                 "head": {"ref": "dependabot/npm/x-2.0.0", "repo": {"full_name": "O/r"}}}


def tracking_issue(**fields):
    return {**issue(5), "title": "Dependabot #9: Bump x from 1.0.0 to 2.0.0",
            "user": {"login": "github-actions[bot]", "type": "Bot"},
            "body": "Dependency update job for Dependabot PR https://github.com/o/r/pull/9\n", **fields}


class DependabotCreateTest(JobFilesTest):
    """`job create` for a tracking issue: the job works in Dependabot's PR only
    when the workflow opened the issue and Dependabot opened the PR it links."""

    def create(self, issue_data, pr_data):
        self.gh.answers["GET repos/o/r/issues/5"] = issue_data
        self.gh.answers["GET repos/o/r/issues/5/comments?per_page=100"] = [[]]
        self.gh.answers["GET repos/o/r/pulls/9"] = pr_data
        with contextlib.redirect_stdout(io.StringIO()):
            job.cmd_create("5")
        return json.loads(next(job.JOBS.glob("005-*/job.json")).read_text())

    def test_job_takes_dependabots_pr_and_branch(self):
        created = self.create(tracking_issue(), DEPENDABOT_PR)
        self.assertEqual((created["id"], created["pr"], created["pr_url"], created["branch"], created["dependabot"]),
                         ("005-dependabot-9-bump-x", 9, "https://github.com/o/r/pull/9",
                          "dependabot/npm/x-2.0.0", 9))

    def test_anything_else_is_an_ordinary_job(self):
        fork = {**DEPENDABOT_PR, "head": {"ref": "dependabot/npm/x-2.0.0", "repo": {"full_name": "eve/r"}}}
        cases = {
            "issue by a person": (tracking_issue(user={"login": "alice", "type": "User"}), DEPENDABOT_PR),
            "issue by an app posing as the bot": (tracking_issue(user={"login": "github-actions[bot]", "type": "User"}),
                                                  DEPENDABOT_PR),
            "PR by a person": (tracking_issue(), {**DEPENDABOT_PR, "user": {"login": "eve", "type": "User"}}),
            "PR closed": (tracking_issue(), {**DEPENDABOT_PR, "state": "closed"}),
            "PR against another branch": (tracking_issue(), {**DEPENDABOT_PR, "base": {"ref": "release"}}),
            "PR from a fork": (tracking_issue(), fork),
            "PR whose head repository is gone": (tracking_issue(), {**DEPENDABOT_PR, "head": {"ref": "x", "repo": None}}),
            "issue that doesn't link the PR": (tracking_issue(body="Bump x"), DEPENDABOT_PR),
            "PR that doesn't exist": (tracking_issue(), NOT_FOUND),
            "title without a PR number": (tracking_issue(title="Dependabot: bump x"), DEPENDABOT_PR),
        }
        for name, (issue_data, pr_data) in cases.items():
            with self.subTest(name):
                created = self.create(issue_data, pr_data)
                self.assertEqual((created["pr"], created["branch"]), (None, f"factory/{created['id']}"))
                self.assertNotIn("dependabot", created)
                shutil.rmtree(job.JOBS / created["id"])


class IssueStateTest(unittest.TestCase):
    def test_not_found_does_not_exist(self):
        gh = StubGh(self, {"GET repos/o/r/issues/99": NOT_FOUND})
        self.assertIsNone(job.issue_state(99))
        self.assertEqual(gh.keys(), ["GET repos/o/r/issues/99"])
        action, note = job.dependency_action(20, [99], job.dependency_states(20, [99]))
        self.assertEqual(action, "wait")
        self.assertIn("#99 does not exist", note)

    def test_other_error_raises(self):
        StubGh(self, {"GET repos/o/r/issues/99": job.Refused("gh: Bad credentials (HTTP 401)")})
        with self.assertRaises(job.Refused):
            job.issue_state(99)

    def test_rest_pull_request(self):
        StubGh(self, {"GET repos/o/r/issues/143": pull_request(143)})
        _, note = job.dependency_action(20, [143], job.dependency_states(20, [143]))
        self.assertIn("#143 is a pull request", note)


class QueueTest(JobFilesTest):
    def test_lists_every_page_without_pull_requests(self):
        self.gh.answers["GET repos/o/r/issues?labels=factory&state=open&per_page=100"] = [
            [issue(1), {**issue(2), "pull_request": {}}], [issue(3)]]
        replace(self, PAGE_SIZE=2)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            job.cmd_queue()
        self.assertEqual(out.getvalue(), "new\t1\t-\t-\tIssue 1\nnew\t3\t-\t-\tIssue 3\n")
        # Asked for by page number: a cloud session's proxy refuses the
        # `repositories/<id>/...` next-page link that `gh api --paginate` follows.
        self.assertEqual(self.gh.calls, [
            ["api", "repos/o/r/issues?labels=factory&state=open&per_page=100&page=1"],
            ["api", "repos/o/r/issues?labels=factory&state=open&per_page=100&page=2"]])

    def test_a_full_last_page_is_followed_by_one_empty_page(self):
        self.gh.answers["GET repos/o/r/issues?labels=factory&state=open&per_page=100"] = [
            [issue(1), issue(2)]]
        replace(self, PAGE_SIZE=2)
        with contextlib.redirect_stdout(io.StringIO()):
            job.cmd_queue()
        self.assertEqual([c[-1][-7:] for c in self.gh.calls], ["&page=1", "&page=2"])


    def test_closes_merged_jobs_of_other_machines(self):
        self.gh.answers["GET repos/o/r/issues?labels=factory&state=open&per_page=100"] = [[]]
        self.gh.answers["GET repos/o/r/pulls/7"] = {"state": "closed", "merged": True}
        seen = []

        def close_merged(jobs, base, finished, is_merged, ended):
            seen.append((jobs, base, finished, is_merged(7)))
            return ["009-x"]

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(job.store, "close_merged", close_merged), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            job.cmd_queue()
        self.assertEqual(seen, [(job.JOBS, job.BASE, job.TERMINAL, True)])
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "job: closed 009-x on the state branch: its PR is merged\n")


class CommentPagingTest(unittest.TestCase):
    def fetch(self, pages):
        StubGh(self, {"GET repos/o/r/issues/5": issue(5, author="alice"),
                      "GET repos/o/r/issues/5/comments?per_page=100": pages})
        return job.fetch_issue(5)

    def test_reply_on_second_page_counts(self):
        question = comment_json("op", "2026-11-01T10:00:00Z", f"{job.MARKER}\nWhich one?")
        reply = comment_json("alice", "2026-11-02T10:00:00Z", "This one.")
        self.assertTrue(job.replied_since_question(self.fetch([[question], [reply]])))

    def test_transferred_issue_refused(self):
        for answer in [issue(6), {**issue(5), "html_url": "https://github.com/other/r/issues/5"}]:
            with self.subTest(answer=answer["html_url"]):
                StubGh(self, {"GET repos/o/r/issues/5": answer})
                with self.assertRaises(job.Refused) as e:
                    job.fetch_issue(5)
                self.assertIn("probably transferred", str(e.exception))

    def test_question_on_second_page_is_unanswered(self):
        reply = comment_json("alice", "2026-11-01T10:00:00Z", "Early words.")
        question = comment_json("op", "2026-11-02T10:00:00Z", f"{job.MARKER}\nWhich one?")
        self.assertFalse(job.replied_since_question(self.fetch([[reply], [question]])))


class PullRequestTest(unittest.TestCase):
    def test_merged_comes_from_merged(self):
        StubGh(self, {"GET repos/o/r/pulls/7": {"state": "closed", "merged": True}})
        self.assertTrue(job.pr_merged({"pr": 7}))
        StubGh(self, {"GET repos/o/r/pulls/7": {"state": "closed", "merged": False}})
        self.assertFalse(job.pr_merged({"pr": 7}))

    def test_open_pr_lookup_encodes_head(self):
        gh = StubGh(self)
        job.open_prs("factory/030-x")
        self.assertEqual(gh.keys(), ["GET repos/o/r/pulls?head=o%3Afactory%2F030-x&state=open"])


class CommentAndLabelTest(unittest.TestCase):
    def test_comment_body_is_literal(self):
        gh = StubGh(self)
        job.comment(7, "@/etc/passwd")
        self.assertEqual(gh.calls, [["api", "repos/o/r/issues/7/comments", "-X", "POST",
                                     "-f", f"body={job.MARKER}\n@/etc/passwd"]])

    def test_label_add(self):
        gh = StubGh(self)
        job.label(5, add="factory:needs-human")
        self.assertEqual(gh.calls, [["api", "repos/o/r/issues/5/labels", "-X", "POST",
                                     "-f", "labels[]=factory:needs-human"]])

    def test_label_remove_absent_is_ignored(self):
        gh = StubGh(self, {"DELETE repos/o/r/issues/5/labels/factory%3Aneeds-info": NOT_FOUND})
        job.label(5, remove=["factory:needs-info", "factory"])
        self.assertEqual(gh.keys(), ["DELETE repos/o/r/issues/5/labels/factory%3Aneeds-info",
                                     "DELETE repos/o/r/issues/5/labels/factory"])

    def test_label_remove_other_error_raises(self):
        StubGh(self, {"DELETE repos/o/r/issues/5/labels/factory": job.Refused("gh: Forbidden (HTTP 403)")})
        with self.assertRaises(job.Refused):
            job.label(5, remove=["factory"])


class MarkReadyTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDE_CODE_REMOTE", None)

    def test_cloud_session_uses_proxy_route(self):
        os.environ["CLAUDE_CODE_REMOTE"] = "true"
        gh = StubGh(self, {"GET repos/o/r/pulls/7": {"draft": True}})
        job.mark_ready(7)
        self.assertEqual(gh.calls[1], ["api", "repos/o/r/pulls/7/ccr/ready_for_review", "-X", "POST"])
        self.assertEqual(len(gh.calls), 2)

    def test_elsewhere_uses_gh_pr_ready(self):
        os.environ["CLAUDE_CODE_REMOTE"] = "false"
        gh = StubGh(self, {"GET repos/o/r/pulls/7": {"draft": True}})
        job.mark_ready(7)
        self.assertEqual(gh.calls, [["api", "repos/o/r/pulls/7"], ["pr", "ready", "7", "--repo", "o/r"]])

    def test_not_draft_does_nothing(self):
        os.environ["CLAUDE_CODE_REMOTE"] = "true"
        gh = StubGh(self, {"GET repos/o/r/pulls/7": {"draft": False}})
        job.mark_ready(7)
        self.assertEqual(gh.keys(), ["GET repos/o/r/pulls/7"])


class RejectTest(JobFilesTest):
    def test_reject_closes_pr_and_removes_each_label(self):
        j = self.make_job(stage="needs-human", pr=7)
        (job.job_dir(j["id"]) / "rejection.md").write_text("REJECTED\n\nNot wanted.\n")
        job.advance(j, "rejected")
        self.assertEqual(self.gh.keys(), ["POST repos/o/r/issues/5/comments",
                                          "DELETE repos/o/r/issues/5/labels/factory",
                                          "DELETE repos/o/r/issues/5/labels/factory%3Aneeds-human",
                                          "PATCH repos/o/r/pulls/7"])
        self.assertEqual(self.gh.calls[-1], ["api", "repos/o/r/pulls/7", "-X", "PATCH", "-f", "state=closed"])
        self.assertEqual(j["stage"], "rejected")


def intake_md(decision="ACCEPT", follow_ups=None, slices=None, author="We will add X."):
    text = (f"DECISION: {decision}\nRISK: low\nSURFACES: none\nRESCOPED: yes\n\n"
            "## Summary\n- Accepted a slice.\n\n## Scope\nSecret scope text\n")
    if author is not None:
        text += f"\n## For the author\n{author}\n"
    if follow_ups is not None:
        text += f"\n## Follow-ups\n{follow_ups}\n"
    if slices is not None:
        text += f"\n## Slices\n{slices}\n"
    return text + "\n## Why\nIt fits.\n"


TWO_FOLLOW_UPS = ("### 1. Add the export endpoint\nExport every consumer as CSV.\n\n"
                  "### 2. Schedule the export\nNeeds: 1\nRun the export nightly.\n")


def entries_text(n):
    return "".join(f"### {k}. Entry {k}\nBuild part {k}.\n\n" for k in range(1, n + 1))


class FollowUpsTest(unittest.TestCase):
    def parse(self, follow_ups):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intake.md"
            path.write_text(intake_md(follow_ups=follow_ups))
            return job.entries(path, "Follow-ups")

    def refused(self, follow_ups):
        with self.assertRaises(job.Refused) as e:
            self.parse(follow_ups)
        return str(e.exception)

    def test_two_entries_with_needs(self):
        self.assertEqual(self.parse(TWO_FOLLOW_UPS), [
            {"k": 1, "title": "Add the export endpoint", "needs": [], "body": "Export every consumer as CSV."},
            {"k": 2, "title": "Schedule the export", "needs": [1], "body": "Run the export nightly."}])

    def test_no_section_or_blank_section_is_no_entries(self):
        self.assertEqual(self.parse(None), [])
        self.assertEqual(self.parse("\n  \n"), [])

    def test_five_entries_pass(self):
        self.assertEqual(len(self.parse(entries_text(5))), 5)

    def test_more_than_five_refused(self):
        self.assertIn("the limit is 5", self.refused(entries_text(6)))

    def test_stray_text_refused(self):
        self.assertIn("text before the first entry", self.refused("Some notes.\n\n" + entries_text(1)))

    def test_bad_numbering_refused(self):
        for text in ["### 2. Skipped one\nBody.\n", "### 1. One\nBody.\n### 3. Three\nBody.\n",
                     "### 1 No dot\nBody.\n"]:
            with self.subTest(text=text):
                self.assertIn("should be `### ", self.refused(text))

    def test_empty_title_refused(self):
        self.assertIn("entry 1: the title is empty", self.refused("### 1. \nBody.\n"))

    def test_overlong_title_refused(self):
        self.assertIn("the limit is 120", self.refused(f"### 1. {'x' * 121}\nBody.\n"))

    def test_empty_body_refused(self):
        self.assertIn("entry 1: the body is empty", self.refused("### 1. Title\n\n### 2. Other\nBody.\n"))
        self.assertIn("entry 2: the body is empty", self.refused(entries_text(1) + "### 2. Title\nNeeds: 1\n"))

    def test_bad_needs_refused(self):
        for needs, why in [("1, 3", "must name an earlier entry"), ("2", "names the entry itself"),
                           ("9", "doesn't exist"), ("0", "doesn't exist"), ("one", "entry numbers")]:
            with self.subTest(needs=needs):
                text = entries_text(1) + f"### 2. Two\nNeeds: {needs}\nBody.\n\n### 3. Three\nBody.\n"
                self.assertIn(why, self.refused(text))

    def test_depends_on_refused(self):
        for text in ["### 1. Title\nBody.\n  depends ON #9\n", "### 1. Depends on #9\nBody.\n"]:
            with self.subTest(text=text):
                self.assertIn("`Depends on` line", self.refused(text))

    def test_factory_marker_refused(self):
        text = "### 1. Title\nBody.\n<!-- factory follow-up of #5, entry 1 -->\n"
        self.assertIn("`<!-- factory`", self.refused(text))


def filed_body(text, source, k, depends):
    return (f"{text}\n\nThis was part of the request in #{source}, left out of that change to keep it small. "
            f"The factory will pick it up once #{source} is closed."
            f"\n\nDepends on {depends}\n\n<!-- factory follow-up of #{source}, entry {k} -->")


class IntakeFollowUpsTest(JobFilesTest):
    LIST = "GET repos/o/r/issues?creator=op&state=all&per_page=100"
    CREATE = "POST repos/o/r/issues"

    def accept(self, follow_ups=TWO_FOLLOW_UPS, title="x", decision="ACCEPT", stage="spec"):
        j = self.make_job(stage="intake", round=0, title=title, agents={"reviewers": ["code"]})
        (job.job_dir(j["id"]) / "intake.md").write_text(intake_md(decision, follow_ups))
        self.gh.answers.setdefault("GET repos/o/r/issues/5", issue(5))
        self.gh.answers.setdefault(self.CREATE, iter([{"number": 41}, {"number": 42}]))
        return job.advance(job.sync(j), stage)

    def intake_comment(self):
        calls = [c for c in self.gh.calls if self.gh.key(c) == "POST repos/o/r/issues/5/comments"]
        self.assertEqual(len(calls), 1)
        return calls[0][-1].removeprefix("body=")

    def test_files_each_entry_then_comments(self):
        self.gh.answers[self.LIST] = [[]]
        self.accept()
        creates = [c for c in self.gh.calls if self.gh.key(c) == self.CREATE]
        self.assertEqual(creates, [
            ["api", "repos/o/r/issues", "-X", "POST", "-f", "title=Add the export endpoint",
             "-f", "body=" + filed_body("Export every consumer as CSV.", 5, 1, "#5"), "-f", "labels[]=factory"],
            ["api", "repos/o/r/issues", "-X", "POST", "-f", "title=Schedule the export",
             "-f", "body=" + filed_body("Run the export nightly.", 5, 2, "#5, #41"), "-f", "labels[]=factory"]])
        self.assertEqual(self.gh.calls[1], ["api", "repos/o/r/issues?creator=op&state=all&per_page=100&page=1"])
        keys = self.gh.keys()
        self.assertLess(max(i for i, k in enumerate(keys) if k == self.CREATE),
                        keys.index("POST repos/o/r/issues/5/comments"))
        self.assertTrue(self.intake_comment().endswith(
            "### Follow-up issues\n\n- #41 Add the export endpoint\n- #42 Schedule the export"))
        self.assertIn(["git", "worktree", "add", "-b", "factory/001-x", str(job.JOBS / "no-worktree"),
                       "origin/main"], self.runs)

    def test_retry_reuses_filed_issue(self):
        filed = issue(41, author="op", body=filed_body("Export every consumer as CSV.", 5, 1, "#5"))
        self.gh.answers[self.LIST] = [[issue(30, author="op")], [{**issue(39, author="op"), "pull_request": {}},
                                                                 filed]]
        self.gh.answers[self.CREATE] = iter([{"number": 42}])
        self.accept()
        creates = [c for c in self.gh.calls if self.gh.key(c) == self.CREATE]
        self.assertEqual(len(creates), 1)
        self.assertIn("-f", creates[0])
        self.assertIn("body=" + filed_body("Run the export nightly.", 5, 2, "#5, #41"), creates[0])
        self.assertIn("- #41 Add the export endpoint\n- #42 Schedule the export", self.intake_comment())

    def test_marker_from_another_author_is_not_reused(self):
        planted = issue(40, author="mallory", body=filed_body("Planted.", 5, 1, "#5"))
        self.gh.answers[self.LIST] = [[planted]]
        self.accept()
        self.assertEqual(self.gh.keys().count(self.CREATE), 2)
        self.assertIn("- #41 Add the export endpoint", self.intake_comment())

    def test_malformed_section_refuses_before_any_call(self):
        with self.assertRaises(job.Refused) as e:
            self.accept(follow_ups=entries_text(6))
        self.assertIn("the limit is 5", str(e.exception))
        self.assertFalse([k for k in self.gh.keys() if k.startswith("POST")])
        self.assertFalse([c for c in self.runs if c[:3] == ["git", "worktree", "add"]])
        self.assertEqual(json.loads(self.read("job.json"))["stage"], "intake")

    def test_dependabot_job_files_nothing(self):
        self.accept(title="Dependabot #12: Bump x from 1 to 2")
        self.assertNotIn(self.LIST, self.gh.keys())
        self.assertNotIn(self.CREATE, self.gh.keys())
        self.assertNotIn("### Follow-up issues", self.intake_comment())

    def test_follow_up_source_files_nothing(self):
        self.gh.answers["GET repos/o/r/issues/5"] = issue(
            5, author="op", body="Work.\n\nDepends on #3\n\n<!-- factory follow-up of #3, entry 1 -->")
        self.accept()
        self.assertNotIn(self.LIST, self.gh.keys())
        self.assertNotIn(self.CREATE, self.gh.keys())
        self.assertIn("### Follow-up issues\n\nThe work left out of this change is not filed as new issues, "
                      "because the factory filed this issue itself as a follow-up of another issue. A person can "
                      "file any of it that is worth doing.", self.intake_comment())

    def test_slice_source_files_nothing(self):
        self.gh.answers["GET repos/o/r/issues/5"] = issue(
            5, author="op", body="Work.\n\nPart of #3.\n\n<!-- factory slice of #3, entry 2 -->")
        self.accept()
        self.assertNotIn(self.LIST, self.gh.keys())
        self.assertNotIn(self.CREATE, self.gh.keys())
        self.assertIn("because the factory filed this issue itself as one part of a larger issue",
                      self.intake_comment())

    def test_marker_from_another_author_does_not_make_a_follow_up(self):
        self.gh.answers["GET repos/o/r/issues/5"] = issue(5, body="<!-- factory follow-up of #3, entry 1 -->")
        self.gh.answers[self.LIST] = [[]]
        self.accept()
        self.assertEqual(self.gh.keys().count(self.CREATE), 2)

    def test_no_section_files_nothing(self):
        self.accept(follow_ups=None)
        self.assertEqual(self.gh.keys(), ["POST repos/o/r/issues/5/comments"])

    def test_needs_info_files_nothing(self):
        self.accept(follow_ups=entries_text(6), decision="NEEDS-INFO", stage="needs-info")
        self.assertEqual(self.gh.keys(), ["POST repos/o/r/issues/5/comments", "POST repos/o/r/issues/5/labels"])

    def test_reject_files_nothing(self):
        self.accept(follow_ups=entries_text(6), decision="REJECT", stage="rejected")
        self.assertEqual(self.gh.keys(), ["POST repos/o/r/issues/5/comments",
                                          "DELETE repos/o/r/issues/5/labels/factory"])

    def test_filed_body_reads_back_as_a_follow_up_with_its_dependencies(self):
        body = filed_body("<body>", 5, 1, "#5")
        self.assertEqual(body, "<body>\n\nThis was part of the request in #5, left out of that change to keep it "
                               "small. The factory will pick it up once #5 is closed.\n\nDepends on #5\n\n"
                               "<!-- factory follow-up of #5, entry 1 -->")
        self.assertEqual(job.factory_filed(issue(41, author="op", body=body)), "follow-up")
        self.assertEqual(job.dependencies(filed_body("Run it.", 5, 2, "#5, #41")), [5, 41])


SECRETS = ["Secret scope text", "It fits.", "Accepted a slice.", "risk", "RISK:", "SURFACES", "RESCOPED"]
INTAKE_LINK = "Full notes: [`intake.md`](https://github.com/o/r/blob/factory-state/001-x/intake.md)"


class DependabotIntakeTest(JobFilesTest):
    def test_accept_comments_on_the_pr_and_checks_out_dependabots_branch(self):
        j = self.make_job(stage="intake", round=0, title="Dependabot #9: Bump x", agents={"reviewers": ["code"]},
                          pr=9, pr_url="https://github.com/o/r/pull/9", branch="dependabot/npm/x-2.0.0", dependabot=9)
        (job.job_dir(j["id"]) / "intake.md").write_text(intake_md("ACCEPT"))
        job.advance(job.sync(j), "spec")
        keys = self.gh.keys()
        self.assertIn("POST repos/o/r/issues/9/comments", keys)
        self.assertNotIn("POST repos/o/r/issues/5/comments", keys)
        self.assertIn(["git", "fetch", "origin",
                       "+refs/heads/dependabot/npm/x-2.0.0:refs/remotes/origin/dependabot/npm/x-2.0.0"], self.runs)
        self.assertIn(["git", "worktree", "add", "-B", "dependabot/npm/x-2.0.0", str(job.JOBS / "no-worktree"),
                       "origin/dependabot/npm/x-2.0.0"], self.runs)

    def test_needs_info_still_asks_on_the_issue(self):
        j = self.make_job(stage="intake", round=0, title="Dependabot #9: Bump x", pr=9, dependabot=9)
        (job.job_dir(j["id"]) / "intake.md").write_text(intake_md("NEEDS-INFO"))
        job.advance(job.sync(j), "needs-info")
        self.assertIn("POST repos/o/r/issues/5/comments", self.gh.keys())
        self.assertNotIn("POST repos/o/r/issues/9/comments", self.gh.keys())


class IntakeCommentTest(JobFilesTest):
    def advance(self, decision, stage, author="We will add X."):
        j = self.make_job(stage="intake", round=0, agents={"reviewers": ["code"]})
        (job.job_dir(j["id"]) / "intake.md").write_text(intake_md(decision, author=author))
        return job.advance(job.sync(j), stage)

    def posted(self):
        calls = [c for c in self.gh.calls if self.gh.key(c) == "POST repos/o/r/issues/5/comments"]
        self.assertEqual(len(calls), 1)
        return calls[0][-1].removeprefix(f"body={job.MARKER}\n")

    def test_each_decision_posts_only_the_author_section(self):
        for decision, stage in [("ACCEPT", "spec"), ("NEEDS-INFO", "needs-info"), ("REJECT", "rejected")]:
            with self.subTest(decision=decision):
                self.gh.calls.clear()
                self.advance(decision, stage, author="1. What should X do?")
                text = self.posted()
                self.assertEqual(text, f"### Factory intake: {decision}\n\n1. What should X do?")
                for secret in SECRETS:
                    self.assertNotIn(secret, text)
                shutil.rmtree(job.job_dir("001-x"))

    def test_accept_comment_starts_with_the_author_section(self):
        self.advance("ACCEPT", "spec")
        text = self.posted()
        self.assertTrue(text.startswith("### Factory intake: ACCEPT\n\nWe will add X."))
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_missing_or_empty_section_refused_before_any_call(self):
        for decision, stage in [("ACCEPT", "spec"), ("NEEDS-INFO", "needs-info"), ("REJECT", "rejected")]:
            for author in [None, "", "  \n"]:
                with self.subTest(decision=decision, author=author):
                    self.gh.calls.clear()
                    with self.assertRaises(job.Refused) as e:
                        self.advance(decision, stage, author=author)
                    self.assertIn("## For the author", str(e.exception))
                    self.assertIn("intake.md", str(e.exception))
                    self.assertEqual(self.gh.calls, [])
                    self.assertEqual(json.loads(self.read("job.json"))["stage"], "intake")
                    shutil.rmtree(job.job_dir("001-x"))

    def test_link_ends_each_comment_when_the_state_branch_is_on(self):
        publish = mock.patch.object(job.store, "publish").start()
        self.addCleanup(mock.patch.stopall)
        os.environ["FACTORY_STATE_BRANCH"] = "factory-state"
        for decision, stage in [("ACCEPT", "spec"), ("NEEDS-INFO", "needs-info"), ("REJECT", "rejected")]:
            with self.subTest(decision=decision):
                self.gh.calls.clear()
                self.advance(decision, stage)
                text = self.posted()
                self.assertTrue(text.endswith("We will add X.\n\n" + INTAKE_LINK))
                self.assertEqual(text.splitlines()[-1], INTAKE_LINK)
                shutil.rmtree(job.job_dir("001-x"))
        publish.assert_called()

    def test_no_link_when_the_state_branch_is_unset_or_empty(self):
        os.environ["FACTORY_STATE_BRANCH"] = ""
        self.advance("ACCEPT", "spec")
        self.assertNotIn("Full notes:", self.posted())

    def test_link_quotes_the_branch_keeping_slashes(self):
        os.environ["FACTORY_STATE_BRANCH"] = "state/a b"
        self.assertEqual(job.full_notes({"id": "001-x"}, "intake.md"),
                         "\n\nFull notes: [`intake.md`](https://github.com/o/r/blob/state/a%20b/001-x/intake.md)")


TWO_SLICES = ("### 1. Add the export endpoint\nToday nothing exports consumers. Add a CSV export.\n\n"
              "### 2. Schedule the export\nNeeds: 1\nThe export only runs by hand. Run it nightly.\n")


def slice_body(text, parent, k, depends=None):
    body = f"{text}\n\nThis is one part of #{parent}, which was too large to build as one change."
    if depends:
        body += f"\n\nDepends on {depends}"
    return f"{body}\n\n<!-- factory slice of #{parent}, entry {k} -->"


def sub_issue(n):
    return {**issue(n, author="op"), "id": 1000 + n}


class IntakeSplitTest(JobFilesTest):
    LIST = "GET repos/o/r/issues?creator=op&state=all&per_page=100"
    CREATE = "POST repos/o/r/issues"
    SUBS = "GET repos/o/r/issues/5/sub_issues?per_page=100"
    LINK = "POST repos/o/r/issues/5/sub_issues"
    COMMENT = "POST repos/o/r/issues/5/comments"

    def split(self, slices=TWO_SLICES, title="x", decision="SPLIT", follow_ups=None):
        j = self.make_job(stage="intake", round=0, title=title)
        (job.job_dir(j["id"]) / "intake.md").write_text(intake_md(decision, follow_ups, slices))
        self.gh.answers.setdefault("GET repos/o/r/issues/5", issue(5))
        self.gh.answers.setdefault(self.CREATE, iter([{"number": 41, "id": 1041}, {"number": 42, "id": 1042}]))
        job.save(job.advance(job.sync(j), "split"))
        return json.loads(self.read("job.json"))

    def refused(self, **kwargs):
        with self.assertRaises(job.Refused) as e:
            self.split(**kwargs)
        self.assertEqual(json.loads(self.read("job.json"))["stage"], "intake")
        return str(e.exception)

    def creates(self):
        return [c for c in self.gh.calls if self.gh.key(c) == self.CREATE]

    def links(self):
        return [c for c in self.gh.calls if self.gh.key(c) == self.LINK]

    def test_files_links_comments_and_ends(self):
        saved = self.split()
        self.assertEqual(self.creates(), [
            ["api", "repos/o/r/issues", "-X", "POST", "-f", "title=Add the export endpoint",
             "-f", "body=" + slice_body("Today nothing exports consumers. Add a CSV export.", 5, 1),
             "-f", "labels[]=factory"],
            ["api", "repos/o/r/issues", "-X", "POST", "-f", "title=Schedule the export",
             "-f", "body=" + slice_body("The export only runs by hand. Run it nightly.", 5, 2, "#41"),
             "-f", "labels[]=factory"]])
        bodies = [c[c.index("-f", 5) + 1] for c in self.creates()]
        self.assertNotIn("Depends on", bodies[0])
        self.assertTrue(all("Depends on #5" not in b for b in bodies))
        self.assertEqual(self.links(), [
            ["api", "repos/o/r/issues/5/sub_issues", "-X", "POST", "-F", "sub_issue_id=1041"],
            ["api", "repos/o/r/issues/5/sub_issues", "-X", "POST", "-F", "sub_issue_id=1042"]])
        comments = [c for c in self.gh.calls if self.gh.key(c) == self.COMMENT]
        self.assertEqual(comments, [["api", "repos/o/r/issues/5/comments", "-X", "POST", "-f",
                                     f"body={job.MARKER}\n### Factory intake: SPLIT\n\n"
                                     "- #41 Add the export endpoint\n- #42 Schedule the export"]])
        keys = self.gh.keys()
        self.assertLess(max(i for i, k in enumerate(keys) if k == self.LINK), keys.index(self.COMMENT))
        self.assertEqual((saved["stage"], saved["slices"]), ("split", [41, 42]))
        self.assertIsNotNone(saved["ended"])
        self.assertFalse([c for c in self.runs if c[:3] == ["git", "worktree", "add"]])
        self.assertNotIn("PATCH repos/o/r/issues/5", keys)
        self.assertNotIn("DELETE repos/o/r/issues/5/labels/factory", keys)

    def test_comment_ends_with_link_when_the_state_branch_is_on(self):
        mock.patch.object(job.store, "publish").start()
        self.addCleanup(mock.patch.stopall)
        os.environ["FACTORY_STATE_BRANCH"] = "factory-state"
        self.split()
        comment = [c for c in self.gh.calls if self.gh.key(c) == self.COMMENT][0][-1]
        self.assertTrue(comment.endswith("- #42 Schedule the export\n\n" + INTAKE_LINK))

    def test_filed_slice_reads_back_as_a_slice(self):
        body = slice_body("<body>", 5, 1)
        self.assertEqual(body, "<body>\n\nThis is one part of #5, which was too large to build as one change.\n\n"
                               "<!-- factory slice of #5, entry 1 -->")
        self.assertEqual(job.factory_filed(issue(41, author="op", body=body)), "slice")
        self.assertEqual(job.dependencies(slice_body("Run it.", 5, 2, "#41")), [41])

    def test_retry_with_every_slice_filed_and_linked_does_nothing_new(self):
        one = {**sub_issue(41), "body": slice_body("Today nothing exports consumers. Add a CSV export.", 5, 1)}
        two = {**sub_issue(42), "body": slice_body("The export only runs by hand. Run it nightly.", 5, 2, "#41")}
        self.gh.answers[self.LIST] = [[issue(30, author="op"), one], [two]]
        self.gh.answers[self.SUBS] = [[one, two]]
        saved = self.split()
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.links(), [])
        self.assertEqual((saved["stage"], saved["slices"]), ("split", [41, 42]))

    def test_retry_files_and_links_only_the_missing_slice(self):
        one = {**sub_issue(41), "body": slice_body("Today nothing exports consumers. Add a CSV export.", 5, 1)}
        self.gh.answers[self.LIST] = [[one]]
        self.gh.answers[self.SUBS] = [[one]]
        self.gh.answers[self.CREATE] = iter([{"number": 43, "id": 1043}])
        saved = self.split()
        self.assertEqual(len(self.creates()), 1)
        self.assertIn("body=" + slice_body("The export only runs by hand. Run it nightly.", 5, 2, "#41"),
                      self.creates()[0])
        self.assertEqual(self.links(), [["api", "repos/o/r/issues/5/sub_issues", "-X", "POST",
                                         "-F", "sub_issue_id=1043"]])
        self.assertEqual(saved["slices"], [41, 43])

    def test_lowest_numbered_marked_issue_is_reused(self):
        body = slice_body("Today nothing exports consumers. Add a CSV export.", 5, 1)
        self.gh.answers[self.LIST] = [[{**sub_issue(44), "body": body}, {**sub_issue(41), "body": body}]]
        self.gh.answers[self.CREATE] = iter([{"number": 45, "id": 1045}])
        self.assertEqual(self.split()["slices"], [41, 45])

    def test_marker_from_another_author_is_not_reused(self):
        planted = {**sub_issue(40), "user": {"login": "mallory", "type": "User"},
                   "body": slice_body("Planted.", 5, 1)}
        self.gh.answers[self.LIST] = [[planted]]
        self.gh.answers[self.SUBS] = [[planted]]
        saved = self.split()
        self.assertEqual(len(self.creates()), 2)
        self.assertEqual(saved["slices"], [41, 42])

    def test_factory_filed_issue_is_never_split(self):
        for marker, kind in [("<!-- factory slice of #3, entry 1 -->", "slice"),
                             ("<!-- factory follow-up of #3, entry 1 -->", "follow-up")]:
            with self.subTest(kind=kind):
                self.gh.calls.clear()
                self.gh.answers["GET repos/o/r/issues/5"] = issue(5, author="op", body=f"Work.\n\n{marker}")
                why = self.refused()
                self.assertIn(f"is a {kind} the factory filed, so it can't be split", why)
                self.assertIn("NEEDS-INFO", why)
                self.assertEqual(self.gh.keys(), ["GET repos/o/r/issues/5"])
                shutil.rmtree(job.job_dir("001-x"))

    def test_marker_from_another_author_does_not_block_a_split(self):
        self.gh.answers["GET repos/o/r/issues/5"] = issue(5, body="<!-- factory slice of #3, entry 1 -->")
        self.assertEqual(self.split()["stage"], "split")

    def test_dependabot_job_refused_before_any_call(self):
        why = self.refused(title="Dependabot #12: Bump x from 1 to 2")
        self.assertIn("Dependabot jobs are never split", why)
        self.assertEqual(self.gh.calls, [])

    def test_bad_section_refused_before_any_call(self):
        for slices, why in [(entries_text(1), "a split needs at least 2"), (entries_text(6), "the limit is 5"),
                            (None, "has 0 entries"),
                            ("### 1. One\nBody.\n### 3. Three\nBody.\n", "should be `### 2. <title>`"),
                            (entries_text(1) + "### 2. Two\nBody.\nDepends on #9\n", "`Depends on` line"),
                            (entries_text(1) + "### 2. Two\n<!-- factory slice of #5, entry 1 -->\n",
                             "`<!-- factory`")]:
            with self.subTest(why=why):
                self.gh.calls.clear()
                message = self.refused(slices=slices)
                self.assertIn(why, message)
                self.assertIn("## Slices", message)
                self.assertEqual(self.gh.calls, [])
                shutil.rmtree(job.job_dir("001-x"))

    def test_follow_ups_with_a_split_refused_before_any_call(self):
        why = self.refused(follow_ups=entries_text(1))
        self.assertIn("a split files only slices", why)
        self.assertEqual(self.gh.calls, [])

    def test_other_decision_refused_before_any_call(self):
        why = self.refused(decision="ACCEPT")
        self.assertIn("DECISION: ACCEPT, not SPLIT", why)
        self.assertEqual(self.gh.calls, [])

    def test_split_holds_no_slot(self):
        j = self.make_job(stage="split", slices=[41, 42])
        self.assertEqual(job.running([j]), [])
        replace(self, CONCURRENCY=1)
        job.require_slot(None)


class SplitQueueTest(JobFilesTest):
    def queue(self, *slices):
        self.make_job(stage="split", slices=[41, 42], ended="2026-01-01T01:00:00Z")
        self.gh.answers["GET repos/o/r/issues?labels=factory&state=open&per_page=100"] = [[issue(5)]]
        for s in slices:
            self.gh.answers[f"GET repos/o/r/issues/{s['number']}"] = s
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            job.cmd_queue()
        rows = [line.split("\t") for line in out.getvalue().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertNotIn(rows[0][0], {"resume", "new"})
        return rows[0]

    def patches(self):
        return [c for c in self.gh.calls if self.gh.key(c) == "PATCH repos/o/r/issues/5"]

    def test_every_slice_completed_closes_the_parent(self):
        row = self.queue(closed_issue(41), closed_issue(42))
        self.assertEqual(self.patches(), [["api", "repos/o/r/issues/5", "-X", "PATCH",
                                           "-f", "state=closed", "-f", "state_reason=completed"]])
        self.assertEqual(row[:4], ["skip", "5", "001-x", "split"])
        self.assertIn("every slice is closed as completed", row[4])

    def test_open_slice_waits_without_writing(self):
        row = self.queue(closed_issue(41), open_issue(42))
        self.assertEqual(self.patches(), [])
        self.assertEqual(row[0], "skip")
        self.assertIn("waiting for #42", row[4])
        self.assertNotIn("#41", row[4])

    def test_slice_closed_otherwise_needs_a_person(self):
        row = self.queue(closed_issue(41), closed_issue(42, "not_planned"))
        self.assertEqual(self.patches(), [])
        self.assertEqual(row[0], "wait")
        self.assertIn("#42 closed as not planned", row[4])
        self.assertIn("a person must close", row[4])

    def test_missing_slice_needs_a_person(self):
        self.gh.answers["GET repos/o/r/issues/42"] = NOT_FOUND
        row = self.queue(closed_issue(41))
        self.assertEqual(self.patches(), [])
        self.assertEqual(row[0], "wait")
        self.assertIn("#42 does not exist", row[4])

    def test_no_recorded_slices_is_never_closed(self):
        self.make_job(stage="split", ended="2026-01-01T01:00:00Z")
        self.gh.answers["GET repos/o/r/issues?labels=factory&state=open&per_page=100"] = [[issue(5)]]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            job.cmd_queue()
        self.assertEqual(self.patches(), [])
        self.assertTrue(out.getvalue().startswith("wait\t5\t001-x\tsplit\t"))


PR_URL = "https://github.com/o/r/pull/7"
PROSE = ("## Background\nThe gateway forwards calls.\n\n"
         "## The problem\nA bad header | breaks it.\n\n"
         "## The change\n- Reject the header.\n\n"
         "## Testing\n`HeaderTests` covers it.\n\n"
         "## Proof\n`curl -H 'X: bad' /call` answered 400.\n\n")
DIFF = "M\tsrc/a.py\nA\tsrc/b c.py\nD\tsrc/gone.py\nR100\tsrc/old.py\tsrc/new.py\n"


def pr_md(*entries, prose=PROSE):
    return prose + "## Where to look\n" + "".join(f"{e}\n" for e in entries)


def anchor(path):
    return f"{PR_URL}/files#diff-{hashlib.sha256(path.encode()).hexdigest()}"


class PrBodyTest(JobFilesTest):
    """The build → gate move: pr.md is checked, then rendered into the PR body."""

    PATCH = "PATCH repos/o/r/pulls/7"

    def setUp(self):
        super().setUp()
        self.gh.answers["GET repos/o/r/pulls?head=o%3Afactory%2F001-x&state=open"] = [
            {"number": 7, "html_url": PR_URL}]
        answers = {"status": "", "rev-list": "1\n", "rev-parse": "abc123\n", "-c": DIFF}

        def fake_git(j, *args, check=True):
            self.runs.append(["git", *args])
            return subprocess.CompletedProcess(args, 0, answers.get(args[0], ""), "")

        replace(self, git=fake_git)

    def gate(self, text, **fields):
        j = self.make_job(**{"stage": "build", **fields})
        (job.job_dir(j["id"]) / "build.md").write_text("STATUS: READY\n\n# Build\n")
        if text is not None:
            (job.job_dir(j["id"]) / "pr.md").write_text(text)
        (job.job_dir(j["id"]) / "proof").mkdir()
        for name, data in getattr(self, "proof_files", {}).items():
            (job.job_dir(j["id"]) / "proof" / name).write_bytes(data)
        job.save(job.advance(job.sync(j), "gate"))
        return json.loads(self.read("job.json"))

    def body(self):
        patches = [c for c in self.gh.calls if self.gh.key(c) == self.PATCH]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0][:5], ["api", "repos/o/r/pulls/7", "-X", "PATCH", "-f"])
        return patches[0][5].removeprefix("body=")

    def refused(self, text):
        with self.assertRaises(job.Refused) as e:
            self.gate(text)
        self.assertNotIn(self.PATCH, self.gh.keys())
        self.assertEqual(json.loads(self.read("job.json"))["stage"], "build")
        return str(e.exception)

    def test_writes_body_with_table_of_links(self):
        saved = self.gate(pr_md("- `src/a.py:10-20` Rejects the header.",
                                "- `src/b c.py:1-1` New check.",
                                "- `src/new.py:3-9` Moved helper."))
        self.assertEqual((saved["stage"], saved["pr"], saved["pr_url"]), ("gate", 7, PR_URL))
        self.assertIn(["git", "-c", "core.quotePath=false", "diff", "--name-status", "origin/main...HEAD"],
                      self.runs)
        self.assertEqual(self.body(), "Closes #5\n\n" + PROSE + "## Where to look\n\n"
                         "| Where | What |\n| --- | --- |\n"
                         f"| [`src/a.py:10-20`]({anchor('src/a.py')}R10-R20) | Rejects the header. |\n"
                         f"| [`src/b c.py:1-1`]({anchor('src/b c.py')}R1-R1) | New check. |\n"
                         f"| [`src/new.py:3-9`]({anchor('src/new.py')}R3-R9) | Moved helper. |\n")

    def test_dependabot_job_replaces_its_pr(self):
        self.gate(pr_md("- `src/a.py:1-2` Bump."), title="Dependabot #77: Bump x from 1 to 2")
        self.assertTrue(self.body().startswith("Closes #5\nReplaces #77\n\n## Background\n"))

    def test_other_job_has_no_replaces_line(self):
        self.gate(pr_md("- `src/a.py:1-2` Fix."), title="Fix Dependabot #77 notes")
        self.assertNotIn("Replaces", self.body())

    def test_deleted_file_links_to_the_file(self):
        self.gate(pr_md("- `src/gone.py:1-40` Removed the old parser."))
        self.assertIn(f"| [`src/gone.py:1-40`]({anchor('src/gone.py')}) | Removed the old parser. |", self.body())

    def test_renamed_file_new_path_accepted(self):
        self.gate(pr_md("- `src/new.py:1-2` Renamed."))
        self.assertIn(f"({anchor('src/new.py')}R1-R2)", self.body())

    def test_eight_entries_accepted(self):
        self.gate(pr_md(*[f"- `src/a.py:{k}-{k}` Piece {k}." for k in range(1, 9)]))
        self.assertEqual(self.body().count("| [`src/a.py:"), 8)

    def test_pipe_in_description_escaped(self):
        self.gate(pr_md("- `src/a.py:1-2` Splits a | b."))
        self.assertIn(" | Splits a \\| b. |\n", self.body())

    def test_body_rewritten_on_a_later_round(self):
        self.gate(pr_md("- `src/a.py:1-2` Fix."), pr=7, pr_url=PR_URL, round=2)
        self.assertEqual(self.gh.keys(), [self.PATCH])
        self.assertIn(f"({anchor('src/a.py')}R1-R2)", self.body())

    def test_refuses_missing_proof_section(self):
        msg = self.refused(pr_md("- `src/a.py:1-2` Fix.", prose=PROSE[:PROSE.index("## Proof")]))
        self.assertIn("missing `## Proof`", msg)

    def test_refuses_co_author_line(self):
        prose = PROSE + "Co-Authored-By: Claude <noreply@anthropic.com>\n\n"
        self.assertIn("attribution line", self.refused(pr_md("- `src/a.py:1-2` Fix.", prose=prose)))

    def test_refuses_generated_with_line(self):
        prose = PROSE + "Generated with [Claude Code](https://x)\n\n"
        self.assertIn("attribution line", self.refused(pr_md("- `src/a.py:1-2` Fix.", prose=prose)))

    def tmp_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def proof(self, link, files=None):
        """pr.md whose Proof section shows `link`, with `files` already in the job folder's proof/."""
        self.proof_files = files or {}
        return pr_md("- `src/a.py:1-2` Fix.", prose=PROSE + f"![The page]({link})\n\n")

    def test_proof_files_are_pushed_and_linked(self):
        remote = self.tmp_dir()
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        os.environ.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
        self.addCleanup(setattr, job.store, "remote_url", job.store.remote_url)
        job.store.remote_url = lambda: str(remote)
        png = b"\x89PNG\r\n\x1a\n\x00binary\r\n"
        self.gate(self.proof("proof/page.png", {"page.png": png, "unlinked.png": b"x"}))
        self.assertIn("![The page](https://github.com/o/r/blob/factory-proof/001-x/page.png?raw=true)", self.body())
        tree = subprocess.run(["git", f"--git-dir={remote}", "ls-tree", "-r", "factory-proof"],
                              capture_output=True, text=True, check=True).stdout.splitlines()
        blob = hashlib.sha1(b"blob %d\0" % len(png) + png).hexdigest()
        self.assertEqual([t.split("\t")[1] for t in tree], ["001-x/page.png", "FACTORY-STATE.md"])
        self.assertEqual(tree[0], f"100644 blob {blob}\t001-x/page.png")

    def test_refuses_proof_link_without_its_file(self):
        self.assertIn("isn't a file", self.refused(self.proof("proof/page.png")))

    @unittest.skipIf(os.name == "nt", "making a symlink needs a privilege there")
    def test_refuses_proof_folder_that_is_a_link(self):
        outside = self.tmp_dir()
        (outside / "page.png").write_bytes(b"x")
        j = self.make_job(stage="build")
        (job.job_dir("001-x") / "build.md").write_text("STATUS: READY\n\n# Build\n")
        (job.job_dir("001-x") / "pr.md").write_text(self.proof("proof/page.png"))
        (job.job_dir("001-x") / "proof").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(job.Refused) as e:
            job.advance(job.sync(j), "gate")
        self.assertIn("must be a real folder", str(e.exception))

    def test_refuses_proof_file_of_another_kind(self):
        self.assertIn("png, jpg, gif, webm or mp4", self.refused(self.proof("proof/../job.json")))

    def test_refuses_when_proof_cannot_be_pushed(self):
        self.addCleanup(setattr, job.store, "remote_url", job.store.remote_url)
        missing = str(self.tmp_dir() / "missing")
        job.store.remote_url = lambda: missing
        self.assertIn("couldn't push", self.refused(self.proof("proof/page.png", {"page.png": b"x"})))

    def test_refuses_missing_pr_md(self):
        self.assertIn("pr.md doesn't exist", self.refused(None))

    def test_refuses_missing_heading(self):
        msg = self.refused(pr_md("- `src/a.py:1-2` Fix.", prose=PROSE.replace("## Testing\n", "")))
        self.assertIn("missing `## Testing`", msg)

    def test_refuses_headings_out_of_order(self):
        prose = ("## The problem\nA.\n\n## Background\nB.\n\n## The change\nC.\n\n## Testing\nD.\n\n## Proof\nE.\n\n")
        self.assertIn("out of order", self.refused(pr_md("- `src/a.py:1-2` Fix.", prose=prose)))

    def test_refuses_empty_section(self):
        msg = self.refused(pr_md("- `src/a.py:1-2` Fix.", prose=PROSE.replace("`HeaderTests` covers it.", "")))
        self.assertIn("`## Testing` section is empty", msg)

    def test_refuses_text_before_first_heading(self):
        self.assertIn("text before", self.refused("# Title\n\n" + pr_md("- `src/a.py:1-2` Fix.")))

    def test_refuses_extra_heading(self):
        msg = self.refused(pr_md("- `src/a.py:1-2` Fix.", prose=PROSE + "## Notes\nMore.\n\n"))
        self.assertIn("`## Notes`", msg)

    def test_refuses_malformed_where_line(self):
        self.assertIn("'- src/a.py:1-2 Fix.' should be", self.refused(pr_md("- src/a.py:1-2 Fix.")))

    def test_refuses_start_after_end(self):
        self.assertIn("should be", self.refused(pr_md("- `src/a.py:9-2` Fix.")))

    def test_refuses_zero_entries(self):
        self.assertIn("`## Where to look` section is empty", self.refused(pr_md()))

    def test_refuses_nine_entries(self):
        msg = self.refused(pr_md(*[f"- `src/a.py:{k}-{k}` Piece {k}." for k in range(1, 10)]))
        self.assertIn("has 9 entries; the limit is 8", msg)

    def test_refuses_path_not_in_diff(self):
        msg = self.refused(pr_md("- `src/old.py:1-2` Fix."))
        self.assertIn("names `src/old.py`, which isn't in", msg)


class DependabotPrNotesTest(PrBodyTest):
    """A job in Dependabot's PR leaves the PR body alone and keeps one comment."""

    COMMENTS = "GET repos/o/r/issues/7/comments?per_page=100"
    TEXT = pr_md("- `src/a.py:10-20` Rejects the header.")

    def gate_in_pr(self):
        return self.gate(self.TEXT, pr=7, pr_url=PR_URL, branch="dependabot/npm/x-2.0.0", dependabot=7,
                         title="Dependabot #7: Bump x")

    def test_first_round_posts_one_comment(self):
        self.gh.answers[self.COMMENTS] = [[comment_json("op", "2026-11-01T10:00:00Z", f"{job.MARKER}\n### Factory intake: ACCEPT")]]
        self.assertEqual(self.gate_in_pr()["stage"], "gate")
        self.assertNotIn(self.PATCH, self.gh.keys())
        posts = [c for c in self.gh.calls if self.gh.key(c) == "POST repos/o/r/issues/7/comments"]
        self.assertEqual(len(posts), 1)
        body = posts[0][-1].removeprefix("body=")
        self.assertTrue(body.startswith(f"{job.MARKER}\n### Factory: about this update\n\n## Background\n"), body)
        self.assertNotIn("Closes #", body)
        self.assertIn(f"| [`src/a.py:10-20`]({anchor('src/a.py')}R10-R20) | Rejects the header. |", body)

    def test_later_rounds_rewrite_that_comment(self):
        mine = {**comment_json("op", "2026-11-01T10:00:00Z", f"{job.MARKER}\n### Factory: about this update\n\nold"),
                "id": 55}
        other = {**comment_json("eve", "2026-11-01T11:00:00Z", f"{job.MARKER}\n### Factory: about this update\n\nfake"),
                 "id": 56}
        self.gh.answers[self.COMMENTS] = [[mine, other]]
        self.gate_in_pr()
        self.assertNotIn("POST repos/o/r/issues/7/comments", self.gh.keys())
        patches = [c for c in self.gh.calls if self.gh.key(c) == "PATCH repos/o/r/issues/comments/55"]
        self.assertEqual(len(patches), 1)
        self.assertTrue(patches[0][-1].startswith(f"body={job.MARKER}\n### Factory: about this update\n\n## Background"))


class MergeTest(JobFilesTest):
    RUNS = "GET repos/o/r/commits/abc123/check-runs?per_page=100"
    STATUS = "GET repos/o/r/commits/abc123/status?per_page=100"
    MERGE = "PUT repos/o/r/pulls/7/merge"

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(job.subprocess, "run",
                                    return_value=subprocess.CompletedProcess([], 0, "PASS", ""))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gh.answers[self.RUNS] = check_runs(("build", "completed", "success"))
        self.gh.answers[self.STATUS] = {"state": "pending", "statuses": []}
        # A fake clock: sleeping moves it on, and nothing really waits.
        self.clock = 0.0
        self.sleeps = []

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.clock += seconds

        for name, value in [("sleep", sleep), ("monotonic", lambda: self.clock)]:
            patcher = mock.patch.object(job.time, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def merge(self):
        """Run `job merge` on a job with PR 7; the printed line."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            job.cmd_merge(self.make_job(pr=7))
        return out.getvalue().strip()

    def merged_on_merge(self):
        """PR 7 reads as merged once `job merge` has merged it."""
        self.gh.answers["GET repos/o/r/pulls/7"] = iter([{"merged": False, "draft": False},
                                                         {"merged": False, "draft": False},
                                                         {"merged": True, "draft": False}])

    def stage(self):
        return json.loads(self.read("job.json"))["stage"]

    def test_waits_for_running_check_then_merges(self):
        self.merged_on_merge()
        self.gh.answers[self.RUNS] = iter([check_runs(("build", "in_progress", None)),
                                           check_runs(("build", "completed", "success"))])
        self.merge()
        self.assertIn(["api", "repos/o/r/pulls/7/merge", "-X", "PUT", "-f", "merge_method=squash",
                       "-f", "sha=abc123"], self.gh.calls)
        self.assertEqual(self.sleeps, [30])
        keys = self.gh.keys()
        self.assertLess(max(i for i, k in enumerate(keys) if k == self.RUNS), keys.index(self.MERGE))
        self.assertEqual(self.stage(), "merged")

    def test_failed_check_run_escalates_without_merging(self):
        self.gh.answers["GET repos/o/r/pulls/7"] = {"merged": False, "draft": False}
        self.gh.answers[self.RUNS] = check_runs(("lint", "completed", "success"),
                                                ("build", "completed", "failure"))
        line = self.merge()
        self.assertEqual(self.stage(), "needs-human")
        self.assertNotIn(self.MERGE, self.gh.keys())
        self.assertIn("needs-human: Checks on `abc123` failed: build (failure)", line)
        self.assertNotIn("lint", line)
        self.assertIn("`build` (failure) https://github.com/o/r/runs/build", self.read("decision.md"))
        text = posted(self.gh, 7)
        self.assertIn("build (failure)", text)
        self.assertIn("factory/001-x", text)
        self.assertNotIn(".md", text)
        self.assertNotIn("round-", text)

    def test_check_still_running_after_limit_escalates(self):
        self.gh.answers["GET repos/o/r/pulls/7"] = {"merged": False, "draft": False}
        self.gh.answers[self.RUNS] = check_runs(("CodeQL / Analyze", "in_progress", None))
        line = self.merge()
        self.assertEqual(self.stage(), "needs-human")
        self.assertNotIn(self.MERGE, self.gh.keys())
        self.assertIn("still running after 60 minutes: CodeQL / Analyze (in_progress)", line)
        self.assertEqual(self.clock, 3600)
        self.assertNotIn(".md", posted(self.gh, 7))

    def test_failed_commit_status_escalates_without_merging(self):
        self.gh.answers["GET repos/o/r/pulls/7"] = {"merged": False, "draft": False}
        self.gh.answers[self.STATUS] = {"state": "failure", "statuses": [
            {"context": "ci/x", "state": "failure", "target_url": "https://ci.example/x", "description": "Broke"}]}
        line = self.merge()
        self.assertEqual(self.stage(), "needs-human")
        self.assertNotIn(self.MERGE, self.gh.keys())
        self.assertIn("ci/x (failure)", line)
        self.assertIn("`ci/x` (failure): Broke https://ci.example/x", self.read("decision.md"))

    def test_pending_status_is_waited_for(self):
        self.merged_on_merge()
        self.gh.answers[self.STATUS] = iter([{"state": state, "statuses": [{"context": "ci/x", "state": state}]}
                                             for state in ["pending", "success"]])
        self.merge()
        self.assertEqual(self.sleeps, [30])
        self.assertIn(self.MERGE, self.gh.keys())

    def test_no_checks_merges_after_settling(self):
        self.merged_on_merge()
        self.gh.answers[self.RUNS] = {"total_count": 0, "check_runs": []}
        self.merge()
        self.assertGreaterEqual(self.clock, job.CHECKS_SETTLE_SECONDS)
        self.assertEqual(self.clock, 120)
        self.assertIn(self.MERGE, self.gh.keys())
        self.assertEqual(self.stage(), "merged")

    def test_check_runs_read_page_by_page(self):
        self.gh.answers["GET repos/o/r/pulls/7"] = {"merged": False, "draft": False}
        first = check_runs(*[(f"job {k}", "completed", "success") for k in range(100)])
        self.gh.answers[self.RUNS] = iter([{**first, "total_count": 101},
                                           {**check_runs(("late", "completed", "failure")), "total_count": 101}])
        line = self.merge()
        self.assertIn(["api", "repos/o/r/commits/abc123/check-runs?per_page=100&page=2"], self.gh.calls)
        self.assertIn("late (failure)", line)
        self.assertNotIn(self.MERGE, self.gh.keys())

    def test_refused_merge_prints_githubs_reason(self):
        self.gh.answers["GET repos/o/r/pulls/7"] = {"merged": False, "draft": False}
        self.gh.answers[self.MERGE] = job.Refused(
            "`gh api repos/o/r/pulls/7/merge -X PUT` failed: gh: Pull Request is not mergeable (HTTP 405)")
        line = self.merge()
        self.assertIn("needs-human: GitHub refused to merge pull request #7: "
                      "gh: Pull Request is not mergeable (HTTP 405)", line)

    def test_refused_merge_without_reason_names_mergeable_state(self):
        self.gh.answers["GET repos/o/r/pulls/7"] = {"merged": False, "draft": False, "mergeable": True,
                                                    "mergeable_state": "unstable"}
        self.gh.answers[self.MERGE] = job.Refused("`gh api repos/o/r/pulls/7/merge -X PUT` failed: ")
        line = self.merge()
        self.assertIn("needs-human: GitHub refused to merge pull request #7 and gave no reason "
                      "(mergeable: True, mergeable_state: unstable)", line)
        self.assertIn("mergeable_state: unstable", self.read("decision.md"))

    def test_merge_sends_configured_method(self):
        self.assertEqual(job.CONFIG["merge_method"], "squash")
        self.gh.answers["GET repos/o/r/pulls/7"] = iter([{"merged": False, "draft": False},
                                                         {"merged": False, "draft": False},
                                                         {"merged": True, "draft": False}])
        with contextlib.redirect_stdout(io.StringIO()):
            job.cmd_merge(self.make_job(pr=7))
        self.assertIn(["api", "repos/o/r/pulls/7/merge", "-X", "PUT", "-f", "merge_method=squash",
                       "-f", "sha=abc123"], self.gh.calls)
        self.assertEqual(json.loads(self.read("job.json"))["stage"], "merged")

    def test_merge_failure_goes_to_needs_human(self):
        self.gh.answers["GET repos/o/r/pulls/7"] = {"merged": False, "draft": False}
        self.gh.answers["PUT repos/o/r/pulls/7/merge"] = job.Refused("gh: Pull Request is not mergeable (HTTP 405)")
        with contextlib.redirect_stdout(io.StringIO()):
            job.cmd_merge(self.make_job(pr=7))
        self.assertEqual(json.loads(self.read("job.json"))["stage"], "needs-human")
        self.assertIn("HTTP 405", self.read("decision.md"))
        self.assertIn("POST repos/o/r/issues/7/comments", self.gh.keys())
        self.assertIn("POST repos/o/r/issues/5/labels", self.gh.keys())
        text = posted(self.gh, 7)
        self.assertIn("GitHub refused to merge the pull request:\n\n```\n", text)
        self.assertNotIn("round-", text)
        self.assertNotIn(".md", text)

    def test_failed_gate_names_no_job_file(self):
        self.gh.answers["GET repos/o/r/pulls/7"] = {"merged": False, "draft": False}
        replace(self, git=lambda j, *args, check=True: subprocess.CompletedProcess(args, 0, "", ""))
        job.subprocess.run.return_value = subprocess.CompletedProcess([], 1, "GATE: FAIL (001-x round 1)", "")
        with contextlib.redirect_stdout(io.StringIO()):
            job.cmd_merge(self.make_job(pr=7))
        text = posted(self.gh, 7)
        self.assertIn("The checks said:", text)
        self.assertIn("GATE: FAIL (001-x round 1)", text)
        self.assertIn("```", text)
        self.assertNotIn("round-", text)
        self.assertNotIn(".md", text)
        self.assertEqual(self.stage(), "needs-human")

    def conflict(self, diff="src/a.py\n", **fields):
        """`job merge` on an approved job with PR 7 whose merge of origin/main conflicts; returns
        (exit code, stdout, stderr)."""
        self.gh.answers["GET repos/o/r/pulls/7"] = {"merged": False, "draft": False}

        def fake_git(j, *args, check=True):
            self.runs.append(["git", *args])
            code = 1 if args[0] == "merge" and args[1] != "--abort" else 0
            out = {"merge": "CONFLICT (content): Merge conflict in src/a.py", "diff": diff,
                   "rev-parse": "abc123\n"}.get(args[0], "")
            return subprocess.CompletedProcess(["git", *args], code, out, "")

        replace(self, git=fake_git, require_slot=lambda job_id: self.fail("require_slot called"))
        self.addCleanup(lambda: [f.close() for f in job._locks.values()])
        self.make_job(**{"stage": "approve", "pr": 7, **fields})
        decision = job.job_dir("001-x") / "decision.md"
        decision.write_text("APPROVE\n\n## Summary\n- Fine.\n")
        os.utime(decision, (0, 0))
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                job.main(["merge", "001-x"])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def feedback_files(self):
        return sorted(p.name for p in job.job_dir("001-x").glob("feedback-*.md"))

    def assert_sent_to_build(self, code, out, err, round):
        self.assertEqual(code, 0, err)
        self.assertEqual(out, f"001-x: conflicts with main in src/a.py; sent to build round {round} to resolve "
                              "them (feedback-1.md)\n")
        diff = ["git", "diff", "--name-only", "--diff-filter=U"]
        self.assertLess(self.runs.index(diff), self.runs.index(["git", "merge", "--abort"]))
        self.assertEqual(job.subprocess.run.call_args_list, [])
        self.assertNotIn("push", [c[1] for c in self.runs])
        self.assertEqual(self.read("decision.md"), "APPROVE\n\n## Summary\n- Fine.\n")
        self.assertEqual([k for k in self.gh.keys() if not k.startswith("GET ")], [])
        self.assertNotIn("ready", " ".join(" ".join(c) for c in self.gh.calls))
        feedback = self.read("feedback-1.md")
        self.assertIn("From `job merge`, not from a person", feedback)
        self.assertIn("The job was approved and its merge found the conflict", feedback)
        self.assertIn("Merge `origin/main` into the branch", feedback)
        self.assertIn("- `src/a.py`", feedback)

    def test_conflict_goes_to_build_to_resolve_it(self):
        code, out, err = self.conflict(round=2, round_limit=3)
        self.assert_sent_to_build(code, out, err, 3)
        saved = json.loads(self.read("job.json"))
        self.assertEqual((saved["stage"], saved["round"], saved["round_limit"], saved["merge_conflicts"]),
                         ("build", 3, 3, 1))
        self.assertEqual((saved["gate"], saved["reviews"], saved["ended"]), (None, {}, None))
        self.assertTrue((job.job_dir("001-x") / "round-3").is_dir())

    def test_conflict_goes_to_build_with_every_slot_taken(self):
        for k in range(job.CONCURRENCY):
            self.make_job(id=f"00{k + 2}-busy", issue=10 + k, branch=f"factory/00{k + 2}-busy", stage="build")
        self.assertEqual(len(job.running(job.all_jobs())), job.CONCURRENCY)
        code, out, err = self.conflict(round=2, round_limit=3)
        self.assert_sent_to_build(code, out, err, 3)
        self.assertEqual(self.stage(), "build")

    def test_conflict_in_the_last_round_gets_one_more_round_and_no_more(self):
        code, out, err = self.conflict(round=3, round_limit=3)
        self.assert_sent_to_build(code, out, err, 4)
        saved = json.loads(self.read("job.json"))
        self.assertEqual((saved["stage"], saved["round"], saved["round_limit"]), ("build", 4, 4))
        checks = job.round_dir(saved, 4) / "checks.md"
        checks.write_text("GATE: FAIL\nCOMMIT: abc123\n\n## Summary\nBroken.\n")
        saved["stage"] = "gate"
        with self.assertRaisesRegex(job.Refused, "round 4 of 4: go to needs-human"):
            job.advance(copy.deepcopy(saved), "build")
        self.assertEqual(job.advance(saved, "needs-human")["stage"], "needs-human")

    def assert_escalated_as_today(self, err):
        self.assertEqual(self.stage(), "needs-human")
        self.assertEqual(self.feedback_files(), [])
        text = posted(self.gh, 7)
        self.assertIn("The branch `factory/001-x` changes the same lines as `main` changed since, so git can't "
                      "combine them and the pull request was not merged.", text)
        self.assertIn("Git said:\n\n```\nCONFLICT (content): Merge conflict in src/a.py\n```", text)
        self.assertNotIn(".md", text)
        self.assertNotIn("round-", text)
        self.assertIn("The branch `factory/001-x` changes the same lines", self.read("decision.md"))
        self.assertIn("POST repos/o/r/issues/5/labels", self.gh.keys())

    def test_conflict_after_two_hand_offs_goes_to_a_person(self):
        code, out, err = self.conflict(round=4, round_limit=4, merge_conflicts=2)
        self.assertEqual(code, 0)
        self.assert_escalated_as_today(err)
        self.assertIn("001-x: `job merge` already sent the branch to build 2 times", err)
        self.assertIn("The factory already sent the branch back to its builder 2 times to resolve conflicts with "
                      "`main`, and it conflicted again.", posted(self.gh, 7))
        saved = json.loads(self.read("job.json"))
        self.assertEqual((saved["merge_conflicts"], saved["round"]), (2, 4))

    def test_conflict_without_conflicting_files_goes_to_a_person(self):
        code, out, err = self.conflict(diff="", round=2)
        self.assert_escalated_as_today(err)
        self.assertNotIn("conflicted again", posted(self.gh, 7))
        self.assertNotIn("merge_conflicts", json.loads(self.read("job.json")))

    def test_conflict_goes_to_a_person_when_the_move_is_refused(self):
        def refuse(j, stage, real=job.advance):
            if stage == "build":
                raise job.Refused("001-x: the move was refused")
            return real(j, stage)

        replace(self, advance=refuse)
        code, out, err = self.conflict(round=2)
        self.assert_escalated_as_today(err)
        self.assertIn("job: 001-x: the move was refused", err)
        self.assertNotIn("conflicted again", posted(self.gh, 7))

    def test_dependabot_conflict_goes_to_build_on_its_branch(self):
        code, out, err = self.conflict(round=2, branch="dependabot/npm/x-1.2.3", dependabot=True)
        self.assert_sent_to_build(code, out, err, 3)
        self.assertIn(["git", "merge", "--no-edit", "origin/main"], self.runs)


# Runs factory/bin/job's main() in a process of its own, against a throwaway jobs
# folder, with git stubbed and a gate that marks its start and takes a while.
UPDATE_CHILD = r"""
import importlib.machinery, importlib.util, json, subprocess, sys, time
from pathlib import Path

path, jobs, argv = sys.argv[1], Path(sys.argv[2]), json.loads(sys.argv[3])
loader = importlib.machinery.SourceFileLoader("job", path)
job = importlib.util.module_from_spec(importlib.util.spec_from_loader("job", loader))
loader.exec_module(job)
job.JOBS = jobs


def git(j, *args, check=True):
    out = "abc123\n" if args[0] == "rev-parse" else ""
    return subprocess.CompletedProcess(["git", *args], 1 if args[0] == "merge-base" else 0, out, "")


def gate(cmd, **kw):
    (jobs / f"gating-{cmd[2]}").touch()
    time.sleep(1.5)
    return subprocess.CompletedProcess(cmd, 0, "GATE: PASS", "")


job.git = git
job.subprocess.run = gate
job.main(argv)
"""


class UpdateTest(JobFilesTest):
    """`job update`, with git and the gate stubbed. By default origin/main is ahead of the
    branch, the merge is clean, the gate passes and the push succeeds."""

    BEFORE = "abc123"

    def setUp(self):
        super().setUp()
        self.codes = {"merge-base": 1}
        self.outs = {"rev-parse": self.BEFORE + "\n"}
        self.errs = {}

        def fake_git(j, *args, check=True):
            cmd = ["git", *args]
            self.runs.append(cmd)
            res = subprocess.CompletedProcess(cmd, self.codes.get(args[0], 0), self.outs.get(args[0], ""),
                                              self.errs.get(args[0], ""))
            if check and res.returncode != 0:
                raise job.Refused(f"`{' '.join(cmd)}` failed")
            return res

        replace(self, git=fake_git, require_slot=lambda job_id: self.fail("require_slot called"))
        self.addCleanup(self.close_locks)
        patcher = mock.patch.object(job.subprocess, "run",
                                    return_value=subprocess.CompletedProcess([], 0, "GATE: PASS (001-x round 1)", ""))
        self.gate = patcher.start()
        self.addCleanup(patcher.stop)

    def make_job(self, **fields):
        return super().make_job(**{"stage": "approve", "pr": 7, **fields})

    @staticmethod
    def close_locks():
        for f in job._locks.values():
            f.close()

    def update(self, argv):
        """Run `job.main(argv)`; returns (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                job.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def git_commands(self):
        return [c[1] for c in self.runs if c[0] == "git"]

    def changing(self):
        return [c for c in self.runs if c[0] == "git" and c[1] in {"fetch", "merge", "push", "reset"}]

    def gate_calls(self):
        return [c.args[0] for c in self.gate.call_args_list]

    def test_already_up_to_date(self):
        self.codes["merge-base"] = 0
        self.make_job()
        before = self.read("job.json")
        code, out, _ = self.update(["update", "001-x"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "001-x: already up to date with main\n")
        self.assertEqual(self.gate_calls(), [])
        self.assertNotIn("merge", self.git_commands())
        self.assertNotIn("push", self.git_commands())
        self.assertEqual(self.read("job.json"), before)

    def test_clean_merge_passing_gate_pushes(self):
        for stage in ["approve", "needs-human"]:
            with self.subTest(stage=stage):
                self.runs.clear()
                self.gate.reset_mock()
                self.make_job(stage=stage)
                before = self.read("job.json")
                code, out, err = self.update(["update", "5"])
                self.assertEqual(code, 0, err)
                self.assertEqual(out, "001-x: merged main into factory/001-x, gate PASS, pushed abc123\n")
                self.assertIn(["git", "fetch", "origin", "main"], self.runs)
                self.assertIn(["git", "merge", "--no-ff", "--no-edit", "origin/main"], self.runs)
                self.assertNotIn("rebase", self.git_commands())
                self.assertEqual(self.gate_calls(), [[sys.executable, str(job.BIN / "gate"), "001-x", "--update"]])
                pushes = [c for c in self.runs if c[1] == "push"]
                self.assertEqual(pushes, [["git", "push", "origin", "factory/001-x"]])
                self.assertNotIn("reset", self.git_commands())
                self.assertEqual(self.read("job.json"), before)
                job.unlock("001-x")
                shutil.rmtree(job.job_dir("001-x"))

    def test_dependabot_job_pushes_its_branch(self):
        self.make_job(branch="dependabot/npm/x-1.2.3", dependabot=True)
        code, _, err = self.update(["update", "001-x"])
        self.assertEqual(code, 0, err)
        self.assertIn(["git", "push", "origin", "dependabot/npm/x-1.2.3"], self.runs)

    def test_conflict_aborts_and_names_the_files(self):
        self.codes["merge"] = 1
        self.outs["diff"] = "src/a.py\ndocs/b c.md\n"
        self.make_job()
        before = self.read("job.json")
        code, _, err = self.update(["update", "001-x"])
        self.assertEqual(code, 1)
        self.assertIn(["git", "diff", "--name-only", "--diff-filter=U"], self.runs)
        self.assertIn(["git", "merge", "--abort"], self.runs)
        self.assertLess(self.runs.index(["git", "diff", "--name-only", "--diff-filter=U"]),
                        self.runs.index(["git", "merge", "--abort"]))
        self.assertIn("- src/a.py\n- docs/b c.md", err)
        self.assertEqual(self.gate_calls(), [])
        self.assertNotIn("push", self.git_commands())
        self.assertEqual(self.read("job.json"), before)

    def test_gate_failure_resets_and_pushes_nothing(self):
        self.gate.return_value = subprocess.CompletedProcess([], 1, "GATE: FAIL (001-x round 1)", "gate stderr")
        self.make_job(stage="needs-human")
        before = self.read("job.json")
        code, out, err = self.update(["update", "001-x"])
        self.assertEqual(code, 1)
        self.assertNotIn("push", self.git_commands())
        self.assertIn(["git", "reset", "--hard", self.BEFORE], self.runs)
        self.assertIn("GATE: FAIL (001-x round 1)", err)
        self.assertIn("gate stderr", err)
        self.assertEqual(out, "")
        self.assertEqual(self.read("job.json"), before)

    def test_push_failure_resets(self):
        self.codes["push"] = 1
        self.errs["push"] = "! [rejected] factory/001-x -> factory/001-x (non-fast-forward)"
        self.make_job()
        before = self.read("job.json")
        code, _, err = self.update(["update", "001-x"])
        self.assertEqual(code, 1)
        self.assertEqual(self.runs[-1], ["git", "reset", "--hard", self.BEFORE])
        self.assertIn("non-fast-forward", err)
        self.assertEqual(self.read("job.json"), before)

    def assert_refused(self, says, **fields):
        self.runs.clear()
        self.make_job(**fields)
        before = self.read("job.json")
        code, out, err = self.update(["update", "001-x"])
        self.assertEqual(code, 1)
        self.assertIn("001-x", err)
        self.assertIn(says, err)
        self.assertEqual(out, "")
        self.assertEqual(self.changing(), [])
        self.assertEqual(self.gate_calls(), [])
        self.assertEqual(self.read("job.json"), before)
        job.unlock("001-x")
        shutil.rmtree(job.job_dir("001-x"))

    def test_refuses_without_a_pull_request(self):
        for stage in ["approve", "needs-human", "needs-info"]:
            with self.subTest(stage=stage):
                self.assert_refused("has no pull request", stage=stage, pr=None)

    def test_refuses_finished_jobs(self):
        for stage in ["merged", "rejected", "split"]:
            with self.subTest(stage=stage):
                self.assert_refused(f"is {stage}: the job is finished", stage=stage)

    def test_refuses_jobs_an_agent_may_be_working_on(self):
        for stage in ["intake", "spec", "build", "gate", "review", "merging"]:
            with self.subTest(stage=stage):
                self.assert_refused(f"is in {stage}, not approve or needs-human", stage=stage)
        self.assertFalse((job.JOBS / "update.lock").exists())

    def test_refuses_a_dirty_worktree(self):
        self.outs["status"] = " M src/a.py\n?? notes.txt\n"
        self.assert_refused("has uncommitted changes:\n M src/a.py\n?? notes.txt")

    def test_needs_no_slot(self):
        base = job.JOBS
        for stage in ["approve", "needs-human"]:
            with self.subTest(stage=stage):
                replace(self, JOBS=base / stage, _locks={})
                self.addCleanup(self.close_locks)
                job.JOBS.mkdir()
                for k in range(job.CONCURRENCY):
                    self.make_job(id=f"00{k + 2}-busy", issue=10 + k, stage="build")
                self.make_job(stage=stage)
                busy = len(job.running(job.all_jobs()))
                # An approve job holds a slot of its own; a needs-human one doesn't.
                self.assertEqual(busy, job.CONCURRENCY + (stage == "approve"))
                code, _, err = self.update(["update", "001-x"])
                self.assertEqual(code, 0, err)
                self.assertEqual(len(job.running(job.all_jobs())), busy)

    def marker(self):
        return json.loads(self.read("job.json")).get("updating", "absent")

    def test_marker_is_set_while_the_gate_runs(self):
        seen = []

        def gate(cmd, **kw):
            seen.append(self.marker())
            return subprocess.CompletedProcess(cmd, 0, "GATE: PASS", "")

        self.gate.side_effect = gate
        self.make_job()
        self.assertEqual(self.update(["update", "001-x"])[0], 0)
        self.assertRegex(seen[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.marker(), "absent")

    def test_marker_is_removed_after_failure_and_exception(self):
        self.gate.return_value = subprocess.CompletedProcess([], 1, "GATE: FAIL", "")
        j = self.make_job()
        before = self.read("job.json")
        self.assertEqual(self.update(["update", "001-x"])[0], 1)
        self.assertEqual(self.marker(), "absent")
        self.gate.side_effect = RuntimeError("gate crashed")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            job.cmd_update("001-x")
        self.assertEqual(self.marker(), "absent")
        self.assertEqual(self.read("job.json"), before)

    def test_waits_for_the_update_lock(self):
        j = self.make_job()
        lock_path = job.JOBS / "update.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(lambda: os.close(fd) if fd_open[0] else None)
        fd_open = [True]
        job.portable.lock(fd)
        os.write(fd, b"999-other (pid 1)")
        err = io.StringIO()
        result = {}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            thread = threading.Thread(target=lambda: result.setdefault("code", self.update_code(j)))
            thread.start()
            deadline = time.monotonic() + 10
            while "waiting" not in err.getvalue() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn(f"job: waiting for {lock_path}, held by 999-other (pid 1)", err.getvalue())
            self.assertEqual(self.changing(), [])
            os.close(fd)
            fd_open[0] = False
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["code"], 0)
        self.assertIn(["git", "fetch", "origin", "main"], self.runs)
        self.assertIn(["git", "push", "origin", "factory/001-x"], self.runs)

    def update_code(self, j):
        try:
            job.main(["update", j["id"]])
        except SystemExit as e:
            return e.code
        return 0

    def test_needs_human_conflict_goes_to_build_to_resolve_it(self):
        self.codes["merge"] = 1
        self.outs["diff"] = "src/a.py\n"
        j = self.make_job(stage="needs-human", round=2, round_limit=2)
        (job.job_dir(j["id"]) / "decision.md").write_text("ESCALATE\n\n## For the human\nLook.\n")
        os.utime(job.job_dir(j["id"]) / "decision.md", (0, 0))
        code, out, err = self.update(["update", "001-x"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "001-x: conflicts with main in src/a.py; sent to build round 3 to resolve them "
                              "(feedback-1.md)\n")
        self.assertIn(["git", "merge", "--abort"], self.runs)
        self.assertEqual(self.gate_calls(), [])
        self.assertNotIn("push", self.git_commands())
        self.assertNotIn("reset", self.git_commands())
        saved = json.loads(self.read("job.json"))
        self.assertEqual((saved["stage"], saved["round"], saved["round_limit"]), ("build", 3, 2 + job.CONFIG["max_rounds"]))
        self.assertNotIn("updating", saved)
        feedback = self.read("feedback-1.md")
        self.assertIn("From `job update`, not from a person", feedback)
        self.assertIn("Merge `origin/main` into the branch", feedback)
        self.assertIn("- `src/a.py`", feedback)
        self.assertIn("DELETE repos/o/r/issues/5/labels/factory%3Aneeds-human", self.gh.keys())

    def escalated(self, id="001-x", issue=5, **fields):
        """A needs-human job whose decision.md is older than any feedback written now."""
        j = self.make_job(id=id, issue=issue, branch=f"factory/{id}", stage="needs-human", round=2, round_limit=2,
                          **fields)
        decision = job.job_dir(id) / "decision.md"
        decision.write_text("ESCALATE\n\n## For the human\nLook.\n")
        os.utime(decision, (0, 0))
        return j

    def snapshot(self, id="001-x"):
        d = job.job_dir(id)
        return {str(p.relative_to(d)): p.read_bytes() if p.is_file() else None
                for p in sorted(d.rglob("*")) if p.name != ".lock"}

    def test_conflict_left_as_it_was_when_the_move_to_build_fails(self):
        self.codes["merge"] = 1
        self.outs["diff"] = "src/a.py\n"
        self.escalated()
        self.gh.answers["DELETE repos/o/r/issues/5/labels/factory%3Aneeds-human"] = job.Refused(
            "`gh api` failed: gh: Server Error (HTTP 500)")
        before = self.snapshot()
        code, out, err = self.update(["update", "001-x"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 500", err)
        self.assertEqual(out, "")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((job.job_dir("001-x") / "round-3").exists())
        self.assertFalse((job.job_dir("001-x") / "feedback-1.md").exists())

    def test_conflict_left_in_needs_human_when_no_slot_is_free(self):
        self.codes["merge"] = 1
        self.outs["diff"] = "src/a.py\n"
        for k in range(job.CONCURRENCY):
            self.make_other(f"00{k + 2}-busy", 10 + k, stage="build")
        self.escalated()
        before = self.snapshot()
        code, out, err = self.update(["update", "001-x"])
        self.assertEqual(code, 1)
        self.assertIn(f"001-x: conflicts with main in src/a.py; left in needs-human because concurrency "
                      f"{job.CONCURRENCY} is reached (running: 002-busy, 003-busy, 004-busy)", err)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn("DELETE", " ".join(self.gh.keys()))

    def test_all_sends_only_as_many_conflicts_to_build_as_there_are_free_slots(self):
        self.codes["merge"] = 1
        self.outs["diff"] = "src/a.py\n"
        for k in range(job.CONCURRENCY - 1):
            self.make_other(f"01{k}-busy", 10 + k, stage="review")
        self.escalated()
        self.escalated(id="002-y", issue=6)
        code, out, err = self.update(["update", "--all"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "001-x: conflicts with main in src/a.py; sent to build round 3 to resolve them "
                              "(feedback-1.md)\n")
        self.assertIn("002-y: conflicts with main in src/a.py; left in needs-human because concurrency", err)
        self.assertIn("job: not brought up to date: 002-y", err)
        stages = {j["id"]: j["stage"] for j in job.peek_jobs()}
        self.assertEqual((stages["001-x"], stages["002-y"]), ("build", "needs-human"))
        self.assertEqual(len(job.running(job.peek_jobs())), job.CONCURRENCY)

    def free(self, id):
        """Whether another process could take job `id`'s lock now."""
        fd = os.open(job.job_dir(id) / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            job.portable.lock(fd, wait=False)
            return True
        except BlockingIOError:
            return False
        finally:
            os.close(fd)

    def test_all_locks_one_job_at_a_time(self):
        self.make_job(stage="needs-human")
        self.make_other("002-y", 6, stage="needs-human")
        self.make_other("003-build", 7, stage="build")
        seen = []

        def gate(cmd, **kw):
            seen.append((cmd[2], {id: self.free(id) for id in ["001-x", "002-y", "003-build"]}))
            return subprocess.CompletedProcess(cmd, 0, "GATE: PASS", "")

        self.gate.side_effect = gate
        code, _, err = self.update(["update", "--all"])
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, [("001-x", {"001-x": False, "002-y": True, "003-build": True}),
                                ("002-y", {"001-x": True, "002-y": False, "003-build": True})])

    def test_all_skips_a_job_that_moved_on_and_carries_on_past_an_error(self):
        self.make_job(stage="needs-human")
        self.make_other("002-y", 6, stage="needs-human")
        self.make_other("003-z", 7, stage="needs-human")
        real_load = job.load

        def load(job_id):
            j = real_load(job_id)
            if job_id == "001-x":
                j["stage"] = "build"
            return j

        replace(self, load=load)
        self.gate.side_effect = [OSError("no gate"), subprocess.CompletedProcess([], 0, "GATE: PASS", "")]
        code, out, err = self.update(["update", "--all"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "001-x: skipped, it is now in build\n"
                              "003-z: merged main into factory/003-z, gate PASS, pushed abc123\n")
        self.assertIn("job: 002-y: OSError: no gate", err)
        self.assertIn("job: not brought up to date: 002-y", err)

    def test_single_update_waits_behind_all_without_deadlock(self):
        self.make_job(stage="needs-human")
        self.make_other("002-y", 6, stage="needs-human")
        env = {k: v for k, v in os.environ.items() if k != "FACTORY_STATE_BRANCH"}
        procs = []

        def start(argv):
            proc = subprocess.Popen([sys.executable, "-c", UPDATE_CHILD, str(JOB), str(job.JOBS), json.dumps(argv)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
            procs.append(proc)

        start(["update", "--all"])
        deadline = time.monotonic() + 10
        while not (job.JOBS / "gating-001-x").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue((job.JOBS / "gating-001-x").exists())
        # 002-y is later in --all's list, so the old lock order deadlocked here.
        start(["update", "002-y"])
        (every_out, every_err), (single_out, single_err) = [p.communicate(timeout=30) for p in procs]
        self.assertEqual(procs[0].returncode, 0, every_err)
        self.assertEqual(procs[1].returncode, 0, single_err)
        self.assertEqual(every_out, "001-x: merged main into factory/001-x, gate PASS, pushed abc123\n"
                                    "002-y: merged main into factory/002-y, gate PASS, pushed abc123\n")
        self.assertIn(f"job: waiting for {job.JOBS / 'update.lock'}, held by update --all", single_err)
        self.assertEqual(single_out, "002-y: merged main into factory/002-y, gate PASS, pushed abc123\n")

    def test_refused_single_update_does_not_wait_for_the_update_lock(self):
        self.make_job(stage="build")
        fd = os.open(job.JOBS / "update.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        job.portable.lock(fd)
        code, _, err = self.update(["update", "5"])
        self.assertEqual(code, 1)
        self.assertIn("001-x is in build, not approve or needs-human", err)
        self.assertNotIn("waiting", err)

    def test_job_that_moved_on_is_refused_once_locked(self):
        self.make_job(stage="needs-human")
        real_load = job.load

        def load(job_id):
            j = real_load(job_id)
            j["stage"] = "build"
            return j

        replace(self, load=load)
        code, _, err = self.update(["update", "001-x"])
        self.assertEqual(code, 1)
        self.assertIn("001-x is in build, not approve or needs-human", err)
        self.assertEqual(self.changing(), [])

    def test_tenth_feedback_is_the_newest(self):
        j = self.escalated()
        d = job.job_dir(j["id"])
        for k in range(1, 10):
            (d / f"feedback-{k}.md").write_text("old\n")
            os.utime(d / f"feedback-{k}.md", (0, 0))
        os.utime(d / "decision.md", (1, 1))
        (d / "feedback-10.md").write_text("new\n")
        self.assertEqual(job.advance(j, "build")["stage"], "build")

    def test_issue_number_locks_only_that_job(self):
        self.make_job()
        self.make_other("002-y", 6, stage="build")
        seen = []

        def gate(cmd, **kw):
            seen.append((self.free("001-x"), self.free("002-y")))
            return subprocess.CompletedProcess(cmd, 0, "GATE: PASS", "")

        self.gate.side_effect = gate
        code, _, err = self.update(["update", "5"])
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, [(False, True)])

    def make_other(self, id, issue, **fields):
        j = {"id": id, "issue": issue, "branch": f"factory/{id}", **fields}
        return self.make_job(**j)

    def test_all_updates_every_needs_human_job_with_a_pull_request(self):
        self.make_job(stage="needs-human")
        self.make_other("002-y", 6, stage="needs-human")
        self.make_other("003-approve", 7, stage="approve")
        self.make_other("004-no-pr", 8, stage="needs-human", pr=None)
        self.make_other("005-build", 9, stage="build")
        code, out, err = self.update(["update", "--all"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "001-x: merged main into factory/001-x, gate PASS, pushed abc123\n"
                              "002-y: merged main into factory/002-y, gate PASS, pushed abc123\n")
        self.assertEqual([c[2] for c in self.gate_calls()], ["001-x", "002-y"])

    def test_all_carries_on_past_a_failure_and_names_it(self):
        self.make_job(stage="needs-human")
        self.make_other("002-y", 6, stage="needs-human")
        self.gate.side_effect = [subprocess.CompletedProcess([], 1, "GATE: FAIL (001-x round 1)", ""),
                                 subprocess.CompletedProcess([], 0, "GATE: PASS", "")]
        code, out, err = self.update(["update", "--all"])
        self.assertEqual(code, 1)
        self.assertIn("GATE: FAIL (001-x round 1)", err)
        self.assertIn("job: not brought up to date: 001-x", err)
        self.assertEqual(out, "002-y: merged main into factory/002-y, gate PASS, pushed abc123\n")
        self.assertIn(["git", "reset", "--hard", self.BEFORE], self.runs)

    def test_all_with_nothing_to_update(self):
        self.make_job(stage="approve")
        code, out, _ = self.update(["update", "--all"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "no needs-human job with a pull request\n")
        self.assertEqual(self.changing(), [])


def check_runs(*runs):
    """The check-runs answer for (name, status, conclusion) triples."""
    return {"total_count": len(runs),
            "check_runs": [{"name": name, "status": status, "conclusion": conclusion,
                            "html_url": f"https://github.com/o/r/runs/{name}"} for name, status, conclusion in runs]}


def posted(gh, number):
    """The text of the one comment posted on issue or PR `number`."""
    calls = [c for c in gh.calls if gh.key(c) == f"POST repos/o/r/issues/{number}/comments"]
    assert len(calls) == 1, calls
    return calls[0][-1].removeprefix(f"body={job.MARKER}\n")


DECISION = ("ESCALATE\n\n## Summary\n- Summary text.\n\n## Why\nWhy text.\n\n"
            "## Acceptance criteria\n1. [x] Criteria text.\n\n## For the human\n"
            "Decide whether `App:Timeout` may change. Look at `appsettings.json` on the PR.\n")
HUMAN = "Decide whether `App:Timeout` may change. Look at `appsettings.json` on the PR."
DECISION_LINK = "Full notes: [`decision.md`](https://github.com/o/r/blob/factory-state/001-x/decision.md)"


class EscalationCommentTest(JobFilesTest):
    def setUp(self):
        super().setUp()
        self.gh.answers["GET repos/o/r/pulls/7"] = {"merged": False, "draft": False}

    def escalate(self, decision=DECISION):
        j = self.make_job(stage="approve", pr=7)
        (job.job_dir(j["id"]) / "decision.md").write_text(decision)
        return job.advance(j, "needs-human")

    def test_posts_only_the_human_section(self):
        self.escalate()
        self.assertEqual(posted(self.gh, 7), f"### Factory: needs a human\n\n{HUMAN}")

    def test_link_ends_the_comment_when_the_state_branch_is_on(self):
        mock.patch.object(job.store, "publish").start()
        self.addCleanup(mock.patch.stopall)
        os.environ["FACTORY_STATE_BRANCH"] = "factory-state"
        self.escalate()
        self.assertEqual(posted(self.gh, 7), f"### Factory: needs a human\n\n{HUMAN}\n\n{DECISION_LINK}")

    def test_missing_or_empty_section_refused_before_any_call(self):
        for decision in [DECISION.split("## For the human")[0], DECISION.split("## For the human")[0]
                         + "## For the human\n\n"]:
            with self.subTest(decision=decision):
                self.gh.calls.clear()
                with self.assertRaises(job.Refused) as e:
                    self.escalate(decision)
                self.assertIn("## For the human", str(e.exception))
                self.assertEqual(self.gh.calls, [])
                shutil.rmtree(job.job_dir("001-x"))

    def assert_for_a_person(self, text):
        self.assertTrue(text.startswith("### Factory: needs a human\n\n"))
        self.assertNotIn("round-", text)
        self.assertNotIn(".md", text)

    def test_builder_blocked(self):
        j = self.make_job(stage="build")
        (job.job_dir(j["id"]) / "build.md").write_text("STATUS: BLOCKED\n\n## Blocked\nDocker is unreachable.\n")
        job.advance(j, "needs-human")
        text = posted(self.gh, 5)
        self.assert_for_a_person(text)
        self.assertIn("stopped before finishing this change and needs a person. What it said:\n\n"
                      "Docker is unreachable.", text)

    def test_builder_blocked_without_a_reason(self):
        j = self.make_job(stage="build")
        (job.job_dir(j["id"]) / "build.md").write_text("STATUS: BLOCKED\n")
        job.advance(j, "needs-human")
        self.assertIn("(it gave no reason)", posted(self.gh, 5))

    def test_gate_failed_in_the_last_round(self):
        j = self.make_job(stage="gate", round=3, round_limit=3)
        rd = job.round_dir(j)
        rd.mkdir()
        (rd / "checks.md").write_text("GATE: FAIL\nCOMMIT: abc123\n\n## Summary\n"
                                      "- checks.sh failed with exit 1 in 9s\n- Protected paths: none changed\n\n"
                                      "## checks.sh\nlog\n")
        job.advance(j, "needs-human")
        text = posted(self.gh, 5)
        self.assert_for_a_person(text)
        self.assertIn("`factory/001-x`", text)
        self.assertIn("after round 3, the last round the factory allows", text)
        self.assertIn("- checks.sh failed with exit 1 in 9s\n- Protected paths: none changed", text)

    def test_review_rounds_used_up(self):
        j = self.make_job(stage="review", round=3, round_limit=3, agents={"reviewers": ["code", "security"]})
        rd = job.round_dir(j)
        rd.mkdir()
        (rd / "review-code.md").write_text("VERDICT: CHANGES\n")
        (rd / "review-security.md").write_text("VERDICT: PASS\n")
        job.advance(job.sync(j), "needs-human")
        text = posted(self.gh, 5)
        self.assert_for_a_person(text)
        self.assertIn("after round 3, the last round the factory allows. Still asking for changes: code.", text)


class ReviewerModelsTest(JobFilesTest):
    """`sync` records in agents.ran_on the model each reviewer's transcript ran on."""

    ROUTED = {"code": "opus", "security": "claude-mythos-5-2"}

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.transcripts = Path(tmp.name)
        replace(self, transcripts_dir=lambda: self.transcripts)
        self.count = 0

    def transcript(self, model, reviewer=None, review_round=1, job_id="001-x", timestamp="2026-01-01T00:00:00Z",
                   meta=True, extra=(), first=None):
        """Write agent-<k>.jsonl (and its meta file unless meta is False); returns its path."""
        self.count += 1
        d = self.transcripts / "session" / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"agent-{self.count}.jsonl"
        prompt = (f"Job: {job_id}. Job folder: {job.job_dir(job_id)}/. Issue: #5. "
                  f"Scratch: /scratch/{job_id}/reviewer-{reviewer}/. Review round {review_round}.")
        head = {"type": "user", "message": {"role": "user", "content": prompt}}
        if timestamp is not None:
            head["timestamp"] = timestamp
        lines = [first if first is not None else json.dumps(head), *extra,
                 json.dumps({"type": "assistant", "message": {"id": "m1", "model": model, "content": []}}),
                 json.dumps({"type": "assistant", "message": {"id": "m2", "model": "<synthetic>", "content": []}})]
        path.write_text("\n".join(lines) + "\n")
        if meta:
            path.with_name(f"agent-{self.count}.meta.json").write_text(
                json.dumps({"agentType": f"reviewer-{reviewer}", "model": "fable"}))
        return path

    def synced(self, **agents):
        j = self.make_job(stage="review", agents={"reviewers": dict(self.ROUTED), **agents})
        job.save(job.sync(j))
        return json.loads(self.read("job.json"))["agents"]

    def test_match_records_the_model_the_routed_alias_ran_on(self):
        self.transcript("claude-opus-5-5", reviewer="code")
        self.assertEqual(self.synced()["ran_on"]["code"], "claude-opus-5-5")

    def test_mismatch_records_the_substitute_and_keeps_the_routed_model(self):
        self.transcript("claude-fable-5-2", reviewer="security")
        agents = self.synced()
        self.assertEqual(agents["ran_on"], {"code": None, "security": "claude-fable-5-2"})
        self.assertEqual(agents["reviewers"], self.ROUTED)

    def test_no_transcript_gives_null_and_keeps_a_recorded_model(self):
        self.assertEqual(self.synced()["ran_on"], {"code": None, "security": None})
        shutil.rmtree(job.job_dir("001-x"))
        agents = self.synced(ran_on={"code": "claude-opus-5-5", "gone": "x"})
        self.assertEqual(agents["ran_on"], {"code": "claude-opus-5-5", "security": None})

    def test_missing_transcripts_folder_gives_null(self):
        replace(self, transcripts_dir=lambda: self.transcripts / "missing")
        self.assertEqual(self.synced()["ran_on"], {"code": None, "security": None})

    def test_damaged_lines_are_skipped(self):
        path = self.transcript("claude-fable-5-2", reviewer="security",
                               extra=["not json", "[1, 2]", '{"type": "assistant"}'])
        with path.open("ab") as f:
            f.write(b"\xff\xfe not utf-8\n")
        self.transcript("claude-opus-5-5", reviewer="code", first="not json")
        self.assertEqual(self.synced()["ran_on"], {"code": None, "security": "claude-fable-5-2"})

    def test_attribution_by_scratch_path_without_a_meta_file(self):
        self.transcript("claude-opus-5-5", reviewer="code", meta=False)
        self.assertEqual(self.synced()["ran_on"]["code"], "claude-opus-5-5")

    def test_meta_file_of_another_agent_is_ignored(self):
        path = self.transcript("claude-opus-5-5", reviewer="code")
        path.with_name(f"{path.stem}.meta.json").write_text(json.dumps({"agentType": "builder"}))
        self.assertEqual(self.synced()["ran_on"]["code"], None)

    def test_latest_round_wins(self):
        self.transcript("claude-fable-5-2", reviewer="code", review_round=2, timestamp="2026-01-01T00:00:00Z")
        self.transcript("claude-opus-5-5", reviewer="code", review_round=1, timestamp="2026-01-02T00:00:00Z")
        self.assertEqual(self.synced()["ran_on"]["code"], "claude-fable-5-2")

    def test_same_round_goes_to_the_latest_timestamp(self):
        self.transcript("claude-fable-5-2", reviewer="code", timestamp="2026-01-02T00:00:00Z")
        self.transcript("claude-opus-5-5", reviewer="code", timestamp=None)
        self.transcript("claude-sonnet-5-5", reviewer="code", timestamp="2026-01-01T00:00:00Z")
        self.assertEqual(self.synced()["ran_on"]["code"], "claude-fable-5-2")

    def test_another_jobs_transcript_is_ignored(self):
        self.transcript("claude-opus-5-5", reviewer="code", job_id="002-y")
        self.assertEqual(self.synced()["ran_on"]["code"], None)

    def test_no_agents_adds_nothing(self):
        self.transcript("claude-opus-5-5", reviewer="code")
        j = self.make_job(stage="build")
        self.assertIsNone(job.sync(j)["agents"])

    def test_sync_command_records_ran_on(self):
        self.transcript("claude-fable-5-2", reviewer="security", extra=["not json"])
        self.make_job(stage="review", agents={"reviewers": dict(self.ROUTED)})
        with contextlib.redirect_stdout(io.StringIO()):
            job.main(["sync", "001-x"])
        for f in job._locks.values():
            f.close()
        agents = json.loads(self.read("job.json"))["agents"]
        self.assertEqual(agents["reviewers"]["security"], "claude-mythos-5-2")
        self.assertEqual(agents["ran_on"], {"code": None, "security": "claude-fable-5-2"})


class IntakeWorktreeTest(unittest.TestCase):
    """Real git with no network: a bare "origin" with `main`, and a clone of it as
    the main checkout, on an older local branch with an uncommitted change.
    Only gh is stubbed."""

    ID = "005-issue-5"

    def setUp(self):
        self.gh = StubGh(self)
        self.gh.answers["GET repos/o/r/issues/5"] = issue(5)
        self.gh.answers["GET repos/o/r/issues/5/comments?per_page=100"] = [[]]
        patcher = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("FACTORY_STATE_BRANCH", None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.upstream, self.main = root / "upstream", root / "main"
        self.sh(root, "init", "-q", "-b", "main", str(self.upstream))
        (self.upstream / ".gitignore").write_text(".worktrees/\n")
        (self.upstream / "README").write_text("one\n")
        self.sh(self.upstream, "add", ".gitignore", "README")
        self.sh(self.upstream, "commit", "-q", "-m", "one")
        self.sh(root, "clone", "-q", "--bare", str(self.upstream), str(root / "origin.git"))
        self.sh(self.upstream, "remote", "add", "origin", str(root / "origin.git"))
        self.sh(root, "clone", "-q", str(root / "origin.git"), str(self.main))
        self.sh(self.main, "checkout", "-q", "-b", "older")
        (self.main / "README").write_text("local edit\n")
        self.worktree = self.main / ".worktrees" / self.ID
        replace(self, REPO=self.main, JOBS=root / "jobs", _locks={}, cost_usd=lambda j: 0.0)

    @staticmethod
    def sh(cwd, *args, check=True):
        res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if check and res.returncode != 0:
            raise AssertionError(f"git {' '.join(args)}: {res.stderr}")
        return res

    def out(self, cwd, *args):
        return self.sh(cwd, *args).stdout.strip()

    def push(self, name):
        """A new commit on origin's main, adding file `name`; returns its hash."""
        (self.upstream / name).write_text(name + "\n")
        self.sh(self.upstream, "add", name)
        self.sh(self.upstream, "commit", "-q", "-m", name)
        self.sh(self.upstream, "push", "-q", "origin", "main")
        return self.out(self.upstream, "rev-parse", "HEAD")

    def main_state(self):
        return [self.out(self.main, *args) for args in
                [("rev-parse", "HEAD"), ("branch", "--show-current"), ("status", "--porcelain")]]

    def create(self):
        with contextlib.redirect_stdout(io.StringIO()):
            job.cmd_create("5")
        return job.load(self.ID)

    def move(self, j, stage, decision=None, **kwargs):
        if decision:
            (job.job_dir(self.ID) / "intake.md").write_text(intake_md(decision, **kwargs))
        j = job.advance(job.sync(j), stage)
        job.save(j)
        return j

    def worktrees(self):
        return [line for line in self.out(self.main, "worktree", "list", "--porcelain").splitlines()
                if line.startswith("worktree ")]

    def assert_tree_gone(self):
        self.assertFalse(self.worktree.exists())
        self.assertEqual(self.worktrees(), [f"worktree {self.main.as_posix()}"])
        self.assertEqual(self.out(self.main, "branch", "--list", f"factory/{self.ID}"), "")

    def test_create_checks_out_origin_main_detached_and_leaves_the_main_checkout_alone(self):
        newest = self.push("only-on-origin.txt")
        before = self.main_state()
        self.assertNotEqual(before[0], newest)
        j = self.create()
        self.assertEqual(j["worktree"], self.worktree.as_posix())
        head = self.out(self.worktree, "rev-parse", "HEAD")
        self.assertEqual(head, self.out(self.main, "rev-parse", "origin/main"))
        self.assertEqual(head, newest)
        self.sh(self.main, "merge-base", "--is-ancestor", before[0], head)
        self.assertTrue((self.worktree / "only-on-origin.txt").exists())
        self.assertEqual((self.worktree / "README").read_text(), "one\n")
        self.assertNotEqual(self.sh(self.worktree, "symbolic-ref", "-q", "HEAD", check=False).returncode, 0)
        self.assertEqual(self.main_state(), before)
        self.assertEqual(before[1:], ["older", "M README"])

    def test_needs_info_drops_the_tree_and_re_intake_makes_it_at_the_new_origin_main(self):
        j = self.create()
        newer = self.push("second.txt")
        j = self.move(j, "needs-info", "NEEDS-INFO")
        self.assertFalse(self.worktree.exists())
        self.assertNotIn(f"worktree {self.worktree}", self.worktrees())
        j = self.move(j, "intake")
        self.assertEqual(self.out(self.worktree, "rev-parse", "HEAD"), newer)
        self.assertTrue((self.worktree / "second.txt").exists())
        self.assertTrue((job.job_dir(self.ID) / "intake-1.md").exists())

    def test_spec_turns_the_tree_into_the_job_branch_at_the_newest_origin_main(self):
        before = self.main_state()
        j = self.create()
        newest = self.push("third.txt")
        j["agents"] = {"reviewers": ["code"]}
        j = self.move(j, "spec", "ACCEPT")
        self.assertEqual(self.out(self.worktree, "branch", "--show-current"), f"factory/{self.ID}")
        self.assertEqual(self.out(self.worktree, "rev-parse", "HEAD"), newest)
        self.assertEqual(self.main_state(), before)

    def test_rejected_from_intake_removes_the_tree(self):
        j = self.create()
        self.assertTrue(self.worktree.exists())
        self.move(j, "rejected", "REJECT")
        self.assert_tree_gone()

    def test_split_removes_the_tree(self):
        j = self.create()
        self.gh.answers["POST repos/o/r/issues"] = iter([{"number": 41, "id": 1041}, {"number": 42, "id": 1042}])
        self.assertEqual(self.move(j, "split", "SPLIT", slices=TWO_SLICES)["stage"], "split")
        self.assert_tree_gone()

    def test_rejected_from_needs_info_removes_the_tree(self):
        j = self.move(self.create(), "needs-info", "NEEDS-INFO")
        with contextlib.redirect_stdout(io.StringIO()):
            job.cmd_reject(job.load(self.ID), "Out of scope.")
        self.assertEqual(json.loads((job.job_dir(self.ID) / "job.json").read_text())["stage"], "rejected")
        self.assert_tree_gone()

    def test_failed_fetch_refuses_create_and_makes_no_job(self):
        self.sh(self.main, "remote", "set-url", "origin", str(self.main.parent / "missing.git"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            job.cmd_create("5")
        self.assertIn("`git fetch origin main` failed: ", err.getvalue())
        self.assertIn("missing.git", err.getvalue())
        self.assertFalse(job.job_dir(self.ID).exists())
        self.assertFalse(self.worktree.exists())

    def test_failed_fetch_refuses_re_intake_and_changes_nothing(self):
        j = self.move(self.create(), "needs-info", "NEEDS-INFO")
        self.sh(self.main, "remote", "set-url", "origin", str(self.main.parent / "missing.git"))
        self.gh.calls.clear()
        with self.assertRaises(job.Refused) as e:
            job.advance(job.sync(j), "intake")
        self.assertIn("`git fetch origin main` failed: ", str(e.exception))
        self.assertEqual(json.loads((job.job_dir(self.ID) / "job.json").read_text())["stage"], "needs-info")
        self.assertTrue((job.job_dir(self.ID) / "intake.md").exists())
        self.assertFalse((job.job_dir(self.ID) / "intake-1.md").exists())
        self.assertFalse([k for k in self.gh.keys() if k.startswith("DELETE")])
        self.assertFalse(self.worktree.exists())


if __name__ == "__main__":
    unittest.main()
