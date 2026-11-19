# Install the software factory

You are a coding agent installing this factory into the user's project. Copy
what's generic, adapt what's repo-specific, verify it, and report. Work in the
project's root. Nothing here is for the user to run: you do every step, with
whatever file and shell tools you have on this machine (Linux, macOS or
Windows), and ask the user only where a step says to.

`<template>` below is where this file is: a folder on this machine, or the URL
you were given, in which case fetch each file from the same base. Don't clone
the template into the project.

## 0. Check the project

Stop and tell the user if any of these fail:

- It's a git repo with an `origin` on GitHub, and `gh auth status` shows a user
  who can push to it and manage its labels.
- Its default branch (`gh api repos/<owner>/<repo> --jq .default_branch`)
  exists on `origin`.
- `claude --version` is 2.1.257 or later (the intake agent runs on Fable).
- `python3 --version` is 3.10 or later. The scripts use only the standard
  library.
- On Windows: Git for Windows is installed (`bash --version` answers in the
  shell Claude Code uses), and `python3` runs there. The agents start the
  scripts as `factory/bin/job ...`, which Git Bash runs through `python3`. A
  python.org install has only `python.exe`: have the user install Python from
  the Microsoft Store, or copy `python.exe` to `python3.exe` beside it.

## 1. Copy

Ask the user about the two optional sets if they haven't said:

- **cloud**: they want to run the factory in Claude Code cloud sessions.
- **dependabot**: the repo uses Dependabot and they want its PRs built by the
  factory.

Copy each file below to the same path under the project root, unchanged,
creating folders as needed. The source path is the set's folder plus the
path: `<template>/template/factory/bin/job` goes to `factory/bin/job`.

Before copying anything, check every destination. If one exists (other than
`.claude/settings.json`), copy nothing, show the user the clashes and ask.

From `<template>/template/` (always):

```
.claude/skills/factory/SKILL.md
.claude/agents/intake.md
.claude/agents/spec-writer.md
.claude/agents/builder.md
.claude/agents/reviewer-code.md
.claude/agents/reviewer-security.md
.claude/agents/reviewer-ux.md
.claude/agents/reviewer-ui.md
.claude/agents/approver.md
factory/config.json
factory/checks.sh
factory/dashboard.html
factory/bin/job
factory/bin/gate
factory/bin/route
factory/bin/store
factory/bin/portable.py
factory/tests/test_job.py
factory/tests/test_gate.py
factory/tests/test_store.py
factory/tests/test_portable.py
```

From `<template>/optional/cloud/` (cloud only):

```
scripts/cloud/setup.sh
scripts/cloud/session-start.sh
factory/CLOUD.md
.claude/settings.json
```

When the project already has a `.claude/settings.json`, don't replace it:
add the template's `SessionStart` hook entry to its `hooks`.

From `<template>/optional/dependabot/` (dependabot only):

```
.github/workflows/dependabot-tracking-issue.yml
.github/scripts/dependabot-issue-text.py
```

Copy bytes, not retyped text: `factory/bin/job` is 1,700 lines and one changed
character breaks it. Keep line endings as LF, on Windows too (the scripts
start with `#!` lines, which a CRLF ending breaks). When the project has no
`.gitattributes` rule that already keeps them LF, add one:

```gitattributes
/factory/** text eol=lf
/scripts/cloud/** text eol=lf
```

**Executable bit.** These run as commands: `factory/bin/job`,
`factory/bin/gate`, `factory/bin/route`, `factory/bin/store`,
`factory/checks.sh`, and with cloud `scripts/cloud/setup.sh` and
`scripts/cloud/session-start.sh`. Mark them in git, which works on every
platform and is what a later checkout on Linux or macOS gets:

```
git add <the files above>
git update-index --chmod=+x <the files above>
```

On Linux and macOS also `chmod +x` them in the working tree.

**`.gitignore`.** Add these two lines if they aren't there (create the file if
there is none):

```gitignore
/factory/jobs/
/.worktrees/
```

`factory/jobs/` is runtime state and `.worktrees/` holds each job's checkout;
neither belongs in the repo. Everything you copied does. Then run
`git check-ignore factory/bin/job`: if it prints the path, the project ignores
`bin/` folders (many .NET and Java ones do), so also add `!/factory/bin/`.

## 2. Adapt

Every placeholder is marked `ADAPT:`. List them:

```bash
grep -rn 'ADAPT:' .claude factory scripts .github 2>/dev/null
```

Replace each with what it asks for, from the project's code, `AGENTS.md` or
`CLAUDE.md`, and CI config, then delete the marker. Keep the agents' structure,
file names, first-line formats and one-line replies as they are. Name real
paths; don't invent areas the project doesn't have. Where a marker's subject
doesn't exist in the project (no pages, say), say so in one sentence instead of
leaving the placeholder.

- `intake.md`: what the project is and where its UI lives.
- `reviewer-security.md`: the project's hot spots.
- `reviewer-ui.md`: where the design tokens, theme and shared components live.
- `reviewer-ux.md`: the kinds of users the project has.
- `spec-writer.md`: the project's testing rules.
- `approver.md`: what a human must always see.

**`factory/checks.sh`.** Replace the placeholder with the project's real
checks: install dependencies if a fresh worktree needs them, then lint,
typecheck, tests and build, whichever the project has. Mirror the CI workflow
that gates pull requests. The script runs with a job's worktree as its working
directory. It must be non-interactive, must not rewrite files (no `--fix`, no
`--update-snapshots`), and must exit non-zero on any failure. Keep
`set -euo pipefail`. On Windows it runs in Git Bash, so use commands that
exist there. Several jobs run it at once, so turn off any build daemon
or shared compiler server in it (for .NET: `export MSBUILDDISABLENODEREUSE=1
DOTNET_CLI_USE_MSBUILD_SERVER=0 UseSharedCompilation=false`; for Gradle:
`--no-daemon`).

**Integration tests** (`integration` in `factory/config.json`). These are the
tests too slow or too heavy for `checks.sh`: they need Docker, a database, a
browser or a real service. After `checks.sh` passes, the gate runs them only
for the integration test files the branch added or changed, one job at a time
across the machine. Work out whether the project has such tests (a separate
test project, folder, marker or CI workflow) and set:

- `paths`: fnmatch patterns for the integration test files
  (`tests/integration/*`; `*` also matches `/`). A branch that changes no
  matching file runs no integration tests.
- `command`: the command as a list of arguments. It runs without a shell, in
  the job's worktree, on whatever machine the factory runs on, so it must not
  rely on shell syntax (`&&`, pipes, globs, `VAR=x cmd`). An argument that is
  exactly `{files}` is replaced by the matching files; leave it out and the
  command runs as written whenever a matching file changed. Use one list when
  the same command works on Linux, macOS and Windows:

  ```json
  "command": ["pytest", "{files}"]
  ```

  and one list per platform when it doesn't (`linux`, `macos`, `windows`, and
  `default` for any not named):

  ```json
  "command": {
    "windows": ["pwsh", "-File", "scripts/integration.ps1", "{files}"],
    "default": ["bash", "scripts/integration.sh", "{files}"]
  }
  ```

  Prefer the test runner itself (`pytest`, `dotnet test`, `go test`,
  `npm run test:integration --`) over a wrapper script: it is the same on
  every platform. When the runner selects tests by name rather than by file,
  write a small script in a language the project already uses on every
  platform (Python, Node) that maps the files to names, and add that script to
  `protected_paths`, so a branch can't change the command it is judged by.
- `docker`: `true` when the tests need Docker. The gate then checks
  `docker info` first, and starts `dockerd` itself in a cloud session.
- `timeout_seconds`: the limit for one run (default 1200).

When the project has no such tests, leave `paths` and `command` empty and say
so in the hand-over. Don't invent a test suite.

Try it before you rely on it: run the command by hand from the project root
with one real integration test file in place of `{files}`, on this machine.
You can only verify the platform you are on; write the others from the
project's CI config or docs and say they are unverified.

**`factory/config.json`.**

- `base_branch`: the default branch.
- `merge_method`: `merge`, `squash` or `rebase`, one the repo allows
  (`gh api repos/<owner>/<repo> --jq '{allow_merge_commit, allow_squash_merge, allow_rebase_merge}'`).
- `protected_paths`: keep the factory entries and don't add the project's
  tests. Features legitimately change existing tests, and a protected test
  blocks the job with no way through. The code reviewer catches weakened or
  special-cased tests instead. Add a path only when no job should ever change
  it. They are fnmatch patterns, so `*` also matches `/`.
- `ports`: move `base` if the project already uses ports 5100–5199.
- Leave `concurrency`, `max_rounds`, `routing`, `surface_reviewers` and
  `prices_usd_per_mtok` as they are unless the user asks.

**`AGENTS.md`** (or `CLAUDE.md`, whichever the project uses; create `AGENTS.md`
if neither exists). Every agent reads it. Make sure it says:

- how to set up a fresh checkout (install dependencies, env files to copy);
- how to start the dev server on a given port, if the project has one, and the
  canned preview (a preview server that fills pages with canned data and talks
  to no real service), if it has one;
- the validation commands, matching `factory/checks.sh`.

Then add a short section:

```markdown
## Software factory

`/factory run` builds every open GitHub issue labeled `factory` through
intake → spec → build → gate → review → approve, one PR per issue. State is in
`factory/jobs/` (gitignored); `factory/bin/job` is its only writer. Checks:
`factory/checks.sh`, run by the gate (`factory/bin/gate <job-id>`), which then
runs the integration tests for the branch's changed integration test files
(`integration.paths` and `integration.command` in `factory/config.json`; one
command for every platform, or one each for `linux`, `macos` and `windows`). Dashboard: `python3 -m http.server 8000 --bind 127.0.0.1 --directory factory`,
then open http://127.0.0.1:8000/dashboard.html. Tests of the factory itself:
`python3 -m unittest discover -s factory/tests`.

Each job works in its own git worktree, a full checkout of the repo at
`.worktrees/<job-id>` (gitignored). A tool run from the repo root that doesn't
read `.gitignore` sees those copies too; exclude `.worktrees/` in its config.

The factory and its agents reach GitHub only through `gh api` on REST paths,
because Claude Code cloud sessions refuse GraphQL. With
`FACTORY_STATE_BRANCH=factory-state` set, every save also publishes the job's
`job.json` and `*.md` files to that branch; follow a factory running elsewhere
with `FACTORY_STATE_BRANCH=factory-state factory/bin/store serve` and
http://127.0.0.1:8001/dashboard.html.
```

**Labels.** Create them on GitHub:

```bash
gh label create factory --color 5319E7 --description "Build this with the software factory"
gh label create factory:needs-info --color FBCA04 --description "The factory asked the author a question"
gh label create factory:needs-human --color D93F0B --description "The factory needs a human decision on the PR"
```

**With `--cloud`.** Fill the `ADAPT:` blocks in `scripts/cloud/setup.sh` and
`scripts/cloud/session-start.sh` with the fresh-checkout setup from
`AGENTS.md`. `factory/CLOUD.md` tells the user how to create the environment;
add the hosts the install needs beyond the default allowlist to its step 2 if
you know them.

**With `--dependabot`.** Set the workflow's `branches:` to the base branch and
pin its action the way the repo's other workflows do. The workflow needs
`.github/dependabot.yml` to exist; don't create one unasked.

## 3. Verify

1. `grep -rn 'ADAPT:' .claude factory scripts .github 2>/dev/null` prints
   nothing.
2. In the project root, on the current branch, `bash factory/checks.sh` exits 0.
   If it fails there, the factory can never pass a job: fix the script (not
   the project) or tell the user what's broken.
3. `python3 -m unittest discover -s factory/tests` passes.
   When `integration.command` is set, the hand run from step 2 passed on this
   machine, and every pattern in `integration.paths` matches at least one file
   (`git ls-files` shows them).
4. `factory/bin/job queue` runs without an error.
5. Each file in `.claude/agents/` has only these frontmatter keys: `name`,
   `description`, `tools`, `model`, and `hooks` on the builder. Claude Code
   ignores unknown keys without a warning, so a typo silently changes behaviour.
6. `python3 -m json.tool factory/config.json` succeeds.
7. `git check-ignore factory/jobs/x .worktrees/x` prints both paths, and
   `git status --porcelain --untracked-files=all` lists every copied file (the
   skill, the agents and `factory/`, `factory/bin/` included) as committable.

## 4. Hand over

Stay on the branch the user is on: don't create or check out another branch.
Commit the install there with a one-line message and show the user the diff.
Ask before pushing: the factory branches every job from `origin/<base_branch>`,
so the install must be on that branch there before the first run.

Then tell the user, in a few lines:

- Label one or two small issues `factory`, then run `/factory run` in Claude
  Code from the project root.
- Every job ends as a PR. The approver merges what it approves; the rest wait
  labeled `factory:needs-human`. Approve one by merging it on GitHub, or tell
  the factory what to change with `/factory <anything>`.
- Intake runs on Fable, once per issue. Depending on their plan, Fable can
  bill to usage credits rather than the plan's included usage.
- To start carefully, set `concurrency` to 1 in `factory/config.json` and read
  every PR until they trust it.
- With `--cloud`: follow `factory/CLOUD.md` to create the environment.

End with a section headed **Integration tests**, so the user knows what the
gate will and won't run:

- What you set: the `paths` patterns and the `command` for each platform, as
  they are in `factory/config.json`, and whether Docker is required. Or that
  the project has no integration tests and both are empty.
- What you verified: the platform you ran the command on, the file you ran it
  with and the result. Name each platform whose command you wrote but could
  not run.
- What it does not cover: integration tests in files the branch didn't touch
  don't run in the gate; say where they do run (a nightly CI workflow, say) or
  that nothing runs them.
- How to extend it, all in `factory/config.json` under `integration`:
  - another folder or kind of test: add a pattern to `paths`;
  - another platform: turn `command` into an object and add its key (`linux`,
    `macos`, `windows`, `default`);
  - different selection (by test name, tag or project): point `command` at a
    script that maps `{files}` to it, and add the script to `protected_paths`;
  - check a change with `factory/bin/gate <job-id> --integration` on a job
    whose branch touches an integration test file.

## Upgrading later

When the factory is already installed and the user asks to upgrade it, copy
only the engine from `<template>/template/`, overwriting what is there:

```
.claude/skills/factory/SKILL.md
factory/dashboard.html
factory/bin/job
factory/bin/gate
factory/bin/route
factory/bin/store
factory/bin/portable.py
factory/tests/test_job.py
factory/tests/test_gate.py
factory/tests/test_store.py
factory/tests/test_portable.py
```

Leave what the project adapted: `.claude/agents/`, `factory/checks.sh` and
`factory/config.json`. Then:

- Compare each agent with the template's and bring over changes outside the
  parts the project adapted (the old `ADAPT:` spots). Show the user anything
  you aren't sure about.
- Compare `factory/config.json` with the template's and add keys the template
  has gained, with the template's defaults.
- Set the executable bits again (step 1), run step 3, and commit.
