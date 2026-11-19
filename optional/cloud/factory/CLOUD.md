# Running the factory in a Claude Code cloud session

How to set up a [Claude Code cloud environment](https://code.claude.com/docs/en/cloud-environments)
so cloud sessions (claude.ai/code, the Claude apps, `claude --cloud`) can build
and test this repository and run `/factory run`.

A cloud session runs in a fresh Ubuntu 24.04 VM with the repository cloned and
common toolchains installed. Whatever this repository needs beyond those comes
from the two scripts below.

## The pieces

| File | Where it runs | What it does |
|---|---|---|
| `scripts/cloud/setup.sh` | The environment's **setup script**: you copy it into the environment once. Runs as root before Claude Code starts, the first time a session starts in the environment. | Installs the toolchain and the packages. |
| `.claude/settings.json` | Committed. Read by every Claude Code session in this repository, local or cloud. | Registers `scripts/cloud/session-start.sh` as a `SessionStart` hook for `startup` and `resume`. |
| `scripts/cloud/session-start.sh` | The SessionStart hook. Runs after Claude Code starts, on every new or resumed session. | Exits at once unless `CLAUDE_CODE_REMOTE` is `true`, which only cloud sessions set. In the cloud it runs the install commands again (quick when the setup script already did the work) and logs to `/tmp/session-start.log`. |

Why two scripts:

- The setup script's result is cached. When it finishes in about five minutes,
  the VM's disk is snapshotted and later sessions start from the snapshot. The
  snapshot is rebuilt when the setup script or the allowed domains change, and
  after about seven days.
- The hook follows the branch. The environment keeps its own copy of the setup
  script, but the hook comes from the clone, so a branch that changes a
  toolchain pin or a lock file gets the right tools without anyone editing the
  environment.

Do not put secrets in the setup script or the environment variables: everyone
who can use the environment can read them. The setup script runs as root on
whatever the session's branch holds, and its result becomes the snapshot every
later session starts from, so start the first session (and the first one after
changing the setup script or the domain list) on the base branch.

## Set up the environment

1. Open [claude.ai/code](https://claude.ai/code), select this repository on its
   base branch, open the environment selector under the message box and choose
   **New environment**.
2. **Network access**: **Limited**, with **Allow package managers** checked.
   Add any other host the install or the tests need under **Allowed domains**;
   a blocked download names its host in the error.
3. **Setup script**: load `scripts/cloud/setup.sh` from a checkout of the base
   branch.
4. **Environment variables**:
   ```text
   BASH_DEFAULT_TIMEOUT_MS=600000
   FACTORY_STATE_BRANCH=factory-state
   ```
   The first makes 10 minutes the default wait for a command (it is 2 minutes
   otherwise, less than many builds). The second turns on the state store; see
   below. Both are plain variables, not secrets.
5. Save, then send a first message that exercises the setup, for example
   `Run bash factory/checks.sh and report the result.`

## What is different in a cloud session

The factory already handles each of these; they matter when you change it.

- **GitHub.** The session's GitHub proxy refuses GraphQL, which most `gh pr`
  and `gh issue` commands use. The factory and its agents reach GitHub only
  through `gh api` on REST paths (`repos/<owner>/<repo>/...`). Marking a PR
  ready has no REST call, so `factory/bin/job` uses the proxy's
  `ccr/ready_for_review` route there and `gh pr ready` elsewhere.
- **Other repositories.** The proxy serves only the repositories attached to
  the session. For another public repository, `git ls-remote --tags`, a shallow
  `git clone` and `raw.githubusercontent.com` work; `gh api repos/<other>/...`
  and `github.com/<other>/releases/...` return 403. Dependency update jobs read
  release notes through the routes that work
  (`.claude/agents/spec-writer.md`, `## Dependency update jobs`).
- **Job state.** `factory/jobs/` dies with the session. With
  `FACTORY_STATE_BRANCH` set, every save in `factory/bin/job` publishes the
  job's `job.json`, top-level `*.md` and `round-<N>/*.md` to that branch
  through `factory/bin/store` (never logs); anyone who can read the repository
  can read the branch. Follow it from your own machine with
  `FACTORY_STATE_BRANCH=factory-state factory/bin/store serve` and
  http://127.0.0.1:8001/dashboard.html. Restoring `factory/jobs/` from the
  branch in a new session isn't supported yet.
- **Docker.** `docker` is installed but `dockerd` is not running when a session
  starts, and `service docker start` fails (the session can't raise its
  open-file limit). With `integration.docker` true in `factory/config.json`,
  the gate starts `dockerd` itself when `docker info` fails, logging to
  `factory/jobs/dockerd.log`. By hand: `nohup dockerd > /tmp/dockerd.log 2>&1 &`.
  Docker Hub limits pulls per egress address, which sessions share; the gate
  retries an integration run that hit the limit.
- **Image builds.** The session proxy re-signs HTTPS with its own certificate
  authority (`/root/.ccr/ca-bundle.crt`, or `$SSL_CERT_FILE`). A `docker build`
  whose steps download packages doesn't trust it; pass the bundle to those
  steps as a build secret.
- **Browsers.** Playwright and its Chromium are pre-installed (`playwright` on
  `PATH`, browsers in `PLAYWRIGHT_BROWSERS_PATH`). Installing a browser or a
  browser tool at run time is refused, so the builder and the `ui` and `ux`
  reviewers use what is there.
- **Loopback calls.** `HTTPS_PROXY` is set in the session. A test that calls
  `127.0.0.1` may need `NO_PROXY=127.0.0.1,localhost`.

## Keeping it current

- After `scripts/cloud/setup.sh` changes on the base branch, load it into the
  environment again. Saving it rebuilds the snapshot.
- After a toolchain pin or lock file changes, nothing: the hook installs what
  the session's branch pins.

## Troubleshooting

- **The session fails while running the setup script.** It stops at the first
  failing command; a blocked download is the usual cause.
- **Claude reports `Toolchain setup failed; the output is in /tmp/session-start.log`.**
  Ask Claude to show the end of that log.
