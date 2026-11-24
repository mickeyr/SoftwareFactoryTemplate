"""Tests for factory/bin/gitea.py against a local HTTP server standing in for Gitea,
and for the parts of factory/bin/job that differ on Gitea.

  python3 -m unittest discover -s factory/tests -v
"""

import contextlib
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import gitea  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("factory_job_gitea", str(BIN / "job"))
job = importlib.util.module_from_spec(importlib.util.spec_from_loader("factory_job_gitea", _loader))
_loader.exec_module(job)

REPO = "repos/o/r"


class FakeGitea(http.server.BaseHTTPRequestHandler):
    """Answers `METHOD /api/v1/<path>` from the server's `answers` (JSON data, or
    (status, data)); a list is cut into pages by `limit` and `page`. Records every call."""

    def handle_any(self):
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        path = url.path.removeprefix("/api/v1/")
        self.server.calls.append((self.command, path, query, body, self.headers.get("Authorization")))
        answer = self.server.answers.get(f"{self.command} {path}", (404, {"message": "not found"}))
        status, data = answer if isinstance(answer, tuple) else (200, answer)
        if isinstance(data, list) and "limit" in query:
            size, page = int(query["limit"]), int(query.get("page", 1))
            data = data[(page - 1) * size:page * size]
        payload = b"" if data is None else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = handle_any

    def log_message(self, *args):
        pass


class GiteaTest(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGitea)
        self.server.calls, self.server.answers = [], {}
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        patcher = mock.patch.dict(os.environ, {gitea.TOKEN_ENV: "secret"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def answer(self, **answers):
        self.server.answers.update({k.replace("_", " ", 1): v for k, v in answers.items()})

    def call(self, path, *args):
        out = gitea.call(self.url, path, *args)
        return json.loads(out) if out else None

    def calls(self):
        return [(method, path, query, body) for method, path, query, body, _ in self.server.calls]


class CallTest(GiteaTest):
    def test_sends_the_token_and_reads_the_user(self):
        self.server.answers["GET user"] = {"login": "op"}
        self.assertEqual(self.call("user"), {"login": "op"})
        self.assertEqual(self.server.calls[0][4], "token secret")

    def test_refuses_without_a_token(self):
        del os.environ[gitea.TOKEN_ENV]
        with self.assertRaisesRegex(gitea.Error, "set GITEA_TOKEN"):
            self.call("user")
        self.assertEqual(self.server.calls, [])

    def test_a_missing_thing_says_http_404(self):
        with self.assertRaisesRegex(gitea.Error, "HTTP 404"):
            self.call(f"{REPO}/issues/9")

    def test_an_unreachable_server_is_named(self):
        self.server.shutdown()
        self.server.server_close()
        with self.assertRaisesRegex(gitea.Error, "can't reach http://127.0.0.1"):
            gitea.call(self.url, "user")

    def test_issue_list_leaves_out_pull_requests_and_reads_as_githubs(self):
        self.server.answers[f"GET {REPO}/issues"] = [
            {"number": 1, "state": "open", "pull_request": None},
            {"number": 2, "state": "closed", "pull_request": None}]
        issues = self.call(f"{REPO}/issues?creator=op&labels=factory&state=all&per_page=100&page=1")
        self.assertEqual(issues, [{"number": 1, "state": "open", "state_reason": None},
                                  {"number": 2, "state": "closed", "state_reason": "completed"}])
        self.assertEqual(self.calls(), [("GET", f"{REPO}/issues", {
            "state": "all", "type": "issues", "labels": "factory", "created_by": "op", "limit": "50", "page": "1"}, None)])

    def test_a_long_list_is_paged_by_what_the_server_gives(self):
        """job stops at the first page shorter than its page size, so that size can't exceed the server's."""
        self.server.answers[f"GET {REPO}/issues"] = [{"number": n, "state": "open"} for n in range(70)]
        first = self.call(f"{REPO}/issues?state=open&per_page=100&page=1")
        second = self.call(f"{REPO}/issues?state=open&per_page=100&page=2")
        self.assertEqual((len(first), len(second)), (gitea.MAX_PAGE, 20))

    def test_open_pull_requests_are_filtered_by_head_branch(self):
        self.server.answers[f"GET {REPO}/pulls"] = (
            [{"number": n, "state": "open", "head": {"ref": f"other-{n}"}} for n in range(60)]
            + [{"number": 99, "state": "open", "head": {"ref": "factory/001-x", "repo": {"full_name": "o/r"}}}])
        found = self.call(f"{REPO}/pulls?head=o%3Afactory%2F001-x&state=open")
        self.assertEqual([pr["number"] for pr in found], [99])
        self.assertEqual([c[2]["page"] for c in self.calls()], ["1", "2"])

    def test_a_fork_branch_named_like_ours_is_not_our_pull_request(self):
        self.server.answers[f"GET {REPO}/pulls"] = [
            {"number": 7, "state": "open", "head": {"ref": "factory/001-x", "repo": {"full_name": "stranger/r"}}},
            {"number": 8, "state": "open", "head": {"ref": "factory/001-x", "repo": None}}]
        self.assertEqual(self.call(f"{REPO}/pulls?head=o%3Afactory%2F001-x&state=open"), [])

    def test_new_issue_takes_label_ids(self):
        self.answer(**{f"GET_{REPO}/labels": [{"id": 7, "name": "factory"}, {"id": 8, "name": "bug"}],
                       f"POST_{REPO}/issues": {"number": 3, "state": "open"}})
        made = self.call(f"{REPO}/issues", "-X", "POST", "-f", "title=T", "-f", "body=B", "-f", "labels[]=factory")
        self.assertEqual(made["number"], 3)
        self.assertEqual(self.calls()[-1], ("POST", f"{REPO}/issues", {}, {"title": "T", "body": "B", "labels": [7]}))

    def test_new_issue_with_an_unknown_label_is_refused(self):
        self.answer(**{f"GET_{REPO}/labels": []})
        with self.assertRaisesRegex(gitea.Error, "no label named factory"):
            self.call(f"{REPO}/issues", "-X", "POST", "-f", "title=T", "-f", "labels[]=factory")

    def test_adding_a_label_sends_its_name(self):
        self.answer(**{f"POST_{REPO}/issues/5/labels": []})
        self.call(f"{REPO}/issues/5/labels", "-X", "POST", "-f", "labels[]=factory:needs-info")
        self.assertEqual(self.calls(), [("POST", f"{REPO}/issues/5/labels", {}, {"labels": ["factory:needs-info"]})])

    def test_removing_a_label_goes_by_id(self):
        self.answer(**{f"GET_{REPO}/labels": [{"id": 7, "name": "factory:needs-info"}],
                       f"DELETE_{REPO}/issues/5/labels/7": (204, None)})
        self.assertIsNone(self.call(f"{REPO}/issues/5/labels/factory%3Aneeds-info", "-X", "DELETE"))
        self.assertEqual(self.calls()[-1][:2], ("DELETE", f"{REPO}/issues/5/labels/7"))

    def test_draft_pull_request_is_a_title_prefix(self):
        self.answer(**{f"POST_{REPO}/pulls": {"number": 4, "state": "open"}})
        self.call(f"{REPO}/pulls", "-X", "POST", "-f", "title=Add x", "-f", "head=b", "-f", "base=main",
                  "-F", "draft=true")
        self.assertEqual(self.calls()[-1][3], {"title": "WIP: Add x", "head": "b", "base": "main"})
        self.assertEqual(gitea.ready_title("WIP: Add x"), "Add x")
        self.assertEqual(gitea.ready_title("[wip] Add x"), "Add x")

    def test_merge_is_pinned_to_the_commit(self):
        self.answer(**{f"POST_{REPO}/pulls/4/merge": None})
        self.assertIsNone(self.call(f"{REPO}/pulls/4/merge", "-X", "PUT", "-f", "merge_method=squash", "-f", "sha=abc"))
        self.assertEqual(self.calls(), [("POST", f"{REPO}/pulls/4/merge", {}, {"Do": "squash", "head_commit_id": "abc"})])

    def test_closing_an_issue_drops_the_reason(self):
        self.answer(**{f"PATCH_{REPO}/issues/5": {"number": 5, "state": "closed"}})
        closed = self.call(f"{REPO}/issues/5", "-X", "PATCH", "-f", "state=closed", "-f", "state_reason=completed")
        self.assertEqual(self.calls()[-1][3], {"state": "closed"})
        self.assertEqual(closed["state_reason"], "completed")

    def test_checks_are_the_commit_statuses(self):
        self.answer(**{f"GET_{REPO}/commits/abc/status": {"state": "pending", "statuses": [
            {"context": "ci", "status": "success"}, {"context": "lint", "status": "pending"}]}})
        self.assertEqual(self.call(f"{REPO}/commits/abc/check-runs?per_page=50&page=1"),
                         {"total_count": 0, "check_runs": []})
        statuses = self.call(f"{REPO}/commits/abc/status?per_page=50")["statuses"]
        self.assertEqual([(s["context"], s["state"]) for s in statuses], [("ci", "success"), ("lint", "pending")])

    def test_comments_come_once(self):
        self.answer(**{f"GET_{REPO}/issues/5/comments": [{"id": n} for n in range(60)]})
        self.assertEqual(len(self.call(f"{REPO}/issues/5/comments?per_page=100&page=1")), 60)
        self.assertEqual(self.call(f"{REPO}/issues/5/comments?per_page=100&page=2"), [])

    def test_sub_issues_are_skipped(self):
        self.assertEqual(self.call(f"{REPO}/issues/5/sub_issues?per_page=100&page=1"), [])
        self.assertIsNone(self.call(f"{REPO}/issues/5/sub_issues", "-X", "POST", "-F", "sub_issue_id=9"))
        self.assertEqual(self.server.calls, [])


class JobOnGiteaTest(GiteaTest):
    """factory/bin/job with `forge.type` gitea, against the same server."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in {"GITEA": True, "FORGE": {"type": "gitea", "url": self.url + "/"}, "_repo": "o/r",
                            "_operator": None, "PAGE_SIZE": gitea.MAX_PAGE, "JOBS": Path(tmp.name)}.items():
            self.addCleanup(setattr, job, name, getattr(job, name))
            setattr(job, name, value)
        os.environ.pop("FACTORY_STATE_BRANCH", None)

    def make_job(self, **fields):
        j = {"id": "001-x", "issue": 5, "title": "Add x", "pr": None, "pr_url": None, "branch": "factory/001-x",
             "stage": "build", **fields}
        job.job_dir(j["id"]).mkdir()
        job.save(j)
        return j

    def test_links_go_to_the_gitea(self):
        self.assertEqual(job.file_url("factory-state", "001-x/decision.md"),
                         f"{self.url}/o/r/src/branch/factory-state/001-x/decision.md")
        self.assertEqual(job.file_url("factory-proof", "001-x/page.png", raw=True),
                         f"{self.url}/o/r/raw/branch/factory-proof/001-x/page.png")

    def test_operator_is_the_tokens_user(self):
        self.server.answers["GET user"] = {"login": "op"}
        self.assertEqual(job.operator(), "op")

    def test_a_failed_call_is_a_refusal(self):
        with self.assertRaisesRegex(job.Refused, "HTTP 404"):
            job.api_json("issues/9")

    def test_no_url_is_refused(self):
        job.FORGE = {"type": "gitea"}
        with self.assertRaisesRegex(job.Refused, "no `forge.url`"):
            job.api_json("issues/9")

    def test_pr_opens_a_draft_once(self):
        made = {"number": 4, "html_url": f"{self.url}/o/r/pulls/4", "state": "open", "head": {"ref": "factory/001-x"}}
        self.answer(**{f"GET_{REPO}/pulls": [], f"POST_{REPO}/pulls": made})
        self.make_job()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            job.cmd_pr(job.load("001-x"))
            job.unlock("001-x")
            job.cmd_pr(job.load("001-x"))
            job.unlock("001-x")
        self.assertEqual(out.getvalue(), f"4 {self.url}/o/r/pulls/4\n" * 2)
        posts = [c for c in self.calls() if c[0] == "POST"]
        self.assertEqual(posts, [("POST", f"{REPO}/pulls", {}, {
            "title": "WIP: Add x", "head": "factory/001-x", "base": job.BASE, "body": "Closes #5"})])

    def test_marking_ready_drops_the_draft_prefix(self):
        self.answer(**{f"GET_{REPO}/pulls/4": {"number": 4, "state": "open", "draft": True, "title": "WIP: Add x"},
                       f"PATCH_{REPO}/pulls/4": {"number": 4, "state": "open"}})
        job.mark_ready(4)
        self.assertEqual(self.calls()[-1], ("PATCH", f"{REPO}/pulls/4", {}, {"title": "Add x"}))


if __name__ == "__main__":
    unittest.main()
