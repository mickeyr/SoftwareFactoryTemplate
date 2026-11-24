"""Gitea (and Forgejo) for factory/bin/job, which speaks GitHub's REST API.

`call` takes what `gh api` takes (a path under the API root, `-X`, `-f`, `-F`)
and answers as GitHub would, so job has one set of calls for both. Most of
Gitea's API matches GitHub's; what differs is turned here, each with its reason.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

TOKEN_ENV = "GITEA_TOKEN"
MAX_PAGE = 50  # Gitea's default cap on a page (MAX_RESPONSE_ITEMS); asking for more silently gives this many
DRAFT_PREFIX = "WIP: "  # Gitea has no draft flag: a title that starts like this is a draft
TIMEOUT = 60


class Error(Exception):
    """A refused or failed call. Says `HTTP <status>`, as gh's errors do."""


def parse(args):
    """(method, fields) from gh's flags. `-f` values are text, `-F` values are typed, `name[]` collects a list."""
    method, fields, args = None, {}, list(args)
    while args:
        flag = args.pop(0)
        if flag == "-X":
            method = args.pop(0)
        elif flag in {"-f", "-F"}:
            key, _, value = args.pop(0).partition("=")
            if flag == "-F":
                value = {"true": True, "false": False}.get(value, int(value) if value.isdigit() else value)
            if key.endswith("[]"):
                fields.setdefault(key[:-2], []).append(value)
            else:
                fields[key] = value
        else:
            raise Error(f"gitea: unsupported argument {flag!r}")
    return method or ("POST" if fields else "GET"), fields


def request(url, method, path, body=None):
    """One call. Returns the parsed JSON, or None for an empty answer."""
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        raise Error(f"gitea: set {TOKEN_ENV} to an access token for {url} that can read and write "
                    "this repository's issues and pull requests")
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/v1/{path}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"token {token}", "Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            text = res.read().decode()
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace").strip()
        raise Error(f"gitea: {method} {path}: HTTP {e.code} {e.reason}: {detail[:500]}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise Error(f"gitea: {method} {path}: can't reach {url}: {e}") from None
    return json.loads(text) if text.strip() else None


def pages(url, path):
    """Every item of a list endpoint; `path` already has a query string."""
    items = []
    for number in range(1, 1000):
        page = request(url, "GET", f"{path}&limit={MAX_PAGE}&page={number}")
        items += page
        if len(page) < MAX_PAGE:
            break
    return items


def label_ids(url, repo, names):
    """Label names to ids, which creating an issue and removing a label take."""
    known = {label["name"]: label["id"] for label in pages(url, f"{repo}/labels?")}
    missing = [n for n in names if n not in known]
    if missing:
        raise Error(f"gitea: HTTP 404: no label named {', '.join(missing)} in this repository; create it first")
    return [known[n] for n in names]


def as_github(data):
    """An issue or a list of them, shaped as GitHub's. Gitea gives every issue a
    `pull_request` key, null for a real issue; GitHub leaves it out, and job
    tells the two apart by that."""
    for item in data if isinstance(data, list) else [data]:
        if isinstance(item, dict) and item.get("pull_request", "") is None:
            del item["pull_request"]
        # Gitea records no reason for closing an issue, and calling every close "completed" would
        # start what depends on an issue closed as not planned; job reads its completed label instead.
        if isinstance(item, dict) and "number" in item and "state" in item:
            item.setdefault("state_reason", None)
    return data


def call(url, path, *args):
    """What `gh api <path> <args>` prints, from the Gitea at `url`."""
    method, fields = parse(args)
    m = re.fullmatch(r"(repos/[^/]+/[^/]+)/(.*)", path)
    if not m:
        return json.dumps(request(url, method, path, fields or None))
    repo, rest = m.groups()
    rest, _, query = rest.partition("?")
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    page = int(params.pop("page", ["1"])[0])

    def send(method, rest, body=None, **query):
        q = urllib.parse.urlencode(query)
        return request(url, method, f"{repo}/{rest}" + (f"?{q}" if q else ""), body)

    def listing(rest, **query):
        """One page of a list, in job's page size: job stops at the first page shorter than the size it asked for."""
        size = min(int(params.get("per_page", [MAX_PAGE])[0]), MAX_PAGE)
        return as_github(send("GET", rest, limit=size, page=page, **query))

    # Sub-issues: Gitea has none. The slices still name their parent in their text.
    if re.fullmatch(r"issues/\d+/sub_issues", rest):
        return "[]" if method == "GET" else ""
    # Check runs: Gitea reports every check, its own Actions included, as a commit status.
    if re.fullmatch(r"commits/[^/]+/check-runs", rest):
        return json.dumps({"total_count": 0, "check_runs": []})
    if re.fullmatch(r"commits/[^/]+/status", rest):
        combined = send("GET", rest, limit=MAX_PAGE)
        for status in combined.get("statuses") or []:
            status["state"] = status.get("status")  # the same values under another name
        return json.dumps(combined)
    # Comments come all at once, whatever page is asked for.
    if method == "GET" and re.fullmatch(r"issues/\d+/comments", rest):
        return json.dumps(send("GET", rest) if page == 1 else [])
    if method == "GET" and rest == "issues":
        query = {"state": params.get("state", ["open"])[0], "type": "issues"}  # without `type`, pull requests too
        if "labels" in params:
            query["labels"] = params["labels"][0]
        if "creator" in params:
            query["created_by"] = params["creator"][0]
        return json.dumps(listing(rest, **query))
    if method == "GET" and rest == "pulls":
        # No filter by head branch, so it is applied here to every open pull request.
        found = as_github(pages(url, f"{repo}/pulls?state={params.get('state', ['open'])[0]}"))
        if "head" in params:
            # Only a branch of this repository: a fork can name its branch like a job's, and
            # job would then take the fork's pull request for its own and merge it.
            branch, ours = params["head"][0].partition(":")[2], repo.removeprefix("repos/").lower()
            found = [pr for pr in found if pr["head"]["ref"] == branch
                     and ((pr["head"].get("repo") or {}).get("full_name") or "").lower() == ours]
        return json.dumps(found if page == 1 else [])
    if method == "POST" and rest == "issues":
        fields["labels"] = label_ids(url, repo, fields.get("labels", []))
    if method == "POST" and rest == "pulls" and fields.pop("draft", False):
        fields["title"] = DRAFT_PREFIX + fields["title"]
    m = re.fullmatch(r"(issues/\d+/labels)/(.+)", rest)
    if method == "DELETE" and m:
        [label] = label_ids(url, repo, [urllib.parse.unquote(m.group(2))])
        rest = f"{m.group(1)}/{label}"
    if method == "PUT" and re.fullmatch(r"pulls/\d+/merge", rest):
        # `head_commit_id` refuses the merge when the branch moved, as GitHub's `sha` does.
        method, fields = "POST", {"Do": fields["merge_method"], "head_commit_id": fields["sha"]}
    if method == "PATCH" and re.fullmatch(r"issues/\d+", rest):
        fields.pop("state_reason", None)  # a closed issue has no reason here
    answer = send(method, rest, fields or None)
    return "" if answer is None else json.dumps(as_github(answer))


def ready_title(title):
    """A draft's title without what makes it a draft."""
    return re.sub(r"^\s*(?:WIP:|\[WIP\])\s*", "", title, flags=re.I)
