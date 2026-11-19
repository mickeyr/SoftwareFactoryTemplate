#!/usr/bin/env python3
"""Print the factory tracking issue for one Dependabot pull request.

Reads `gh pr view <n> --json number,title,url,headRefName,commits` on stdin and
prints the issue title, a blank line, then the issue body. The body holds only
the PR link, the ecosystem and the version changes, taken from the last
commit's message. The PR body is never read: it carries third-party release
notes, and the factory's agents read this issue.
"""

import json
import re
import sys


def fail(message):
    print(f"dependabot-issue-text: {message}", file=sys.stderr)
    sys.exit(1)


def update_type(fields):
    match = re.fullmatch(r"version-update:semver-(major|minor|patch)", fields.get("update-type", ""))
    return match.group(1) if match else "unknown"


def old_versions(message, name, new):
    # Dependabot writes the old version only in the prose above the YAML block,
    # as `Bumps [name](url) from A to B.`, `Updates `name` from A to B` or
    # `Bumps name from A to B`.
    escaped = re.escape(name)
    pattern = (
        rf"^(?:Bumps \[{escaped}\]\(\S*\)|Updates `{escaped}`|Bumps {escaped})"
        rf" from (\S+) to {re.escape(new)}\.?$"
    )
    return list(dict.fromkeys(re.findall(pattern, message, re.M)))


def main():
    pr = json.load(sys.stdin)
    branch = pr["headRefName"]
    if not branch.startswith("dependabot/"):
        fail(f"PR #{pr['number']} is not a Dependabot PR: its branch {branch!r} doesn't start with 'dependabot/'.")
    ecosystem = branch.split("/")[1]

    commit = pr["commits"][-1]
    message = f"{commit['messageHeadline']}\n\n{commit['messageBody']}"
    block = re.search(r"^updated-dependencies:\n(.*?)^\.\.\.$", message, re.M | re.S)
    if not block:
        fail(f"the last commit of PR #{pr['number']} has no 'updated-dependencies' block, so its version changes are unknown.")

    rows = {}
    for entry in re.split(r"^- ", block.group(1), flags=re.M)[1:]:
        fields = {key: value.strip("\"'") for key, value in re.findall(r"^\s*([\w-]+): (.*)$", entry, re.M)}
        name = fields.get("dependency-name")
        new = fields.get("dependency-version")
        if not name or not new:
            fail(f"an 'updated-dependencies' entry in PR #{pr['number']} lacks a dependency-name or dependency-version.")
        olds = old_versions(message, name, new)
        if not olds:
            fail(f"PR #{pr['number']}'s commit message has no 'from <old> to {new}' line for {name}, so its old version is unknown.")
        for old in olds:
            rows.setdefault((name, old, new), update_type(fields))

    print(f"Dependabot #{pr['number']}: {pr['title']}")
    print()
    print(f"Dependency update job for Dependabot PR {pr['url']}")
    print()
    print(f"Ecosystem: `{ecosystem}`")
    print()
    print("| Dependency | Old version | New version | Update type |")
    print("| --- | --- | --- | --- |")
    for (name, old, new), kind in rows.items():
        print(f"| `{name}` | `{old}` | `{new}` | {kind} |")


if __name__ == "__main__":
    main()
