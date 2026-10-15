# future-ci

Run your repository's own tests under next month's conditions, and get a dated report of what
will break and when, before the date arrives.

## Example report

From a run on 2026-10-15 against a small test repository whose CI matrix still lists Python 3.9.
This is an excerpt of the job summary; the run link is a placeholder.

> **What will break, and when**
>
> | When | Change | This repository | Evidence |
> |---|---|---|---|
> | 2026-09-23 (in effect) | [JavaScript actions run on Node 24 (Node 20 removed from hosted runners)](https://github.blog/changelog/2026-09-23-node-20-is-no-longer-available-in-github-actions/) | **in effect** | - |
> | 2026-10-09 (in effect) | [Python 3.15 is the newest Python (3.x, unpinned users and new projects get it)](https://www.python.org/downloads/release/python-3150/) | **OK** | python 3.15 on ubuntu-24.04: OK (1 test pass) |
> | 2026-10-19 (in 4 days), rollout to 2026-11-19 | [ubuntu-latest moves to Ubuntu 26.04](https://github.com/actions/runner-images/issues/14748) | **BREAK** | python 3.9 on ubuntu-26.04: BREAK (fails at setup); python 3.13 on ubuntu-26.04: OK (1 test pass); 1 static finding(s) |
> | 2026-11-02 (in 18 days) | [GitHub retires the macos-14 runner image](https://github.blog/changelog/2026-10-01-github-actions-macos-14-runner-image-retirement/) | **not used** | - |
>
> **ubuntu-latest moves to Ubuntu 26.04 — BREAK**
>
> From 2026-10-19 (in 4 days), rollout to 2026-11-19.
>
> - Run `u2604-python-oldest` (python 3.9, ubuntu-26.04): BREAK, fails at setup
>   - `setup-python 3.9 on ubuntu-26.04 failed; its error is in the log of job u2604-python-oldest, https://github.com/OWNER/REPO/actions/runs/RUN_ID`
> - BREAK: setup-python 3.9: no build for Ubuntu 26.04 — `.github/workflows/ci.yml:test[python-version=3.9]: python-version: 24.04->3.9.25 26.04->None`
>
> Fix: Drop or move matrix versions that have no 26.04 build (Python 3.8/3.9, Ruby up to 3.1, old exact patch pins), or pin those jobs to ubuntu-24.04 until you do.

## What it checks

`conditions.json` is a curated, dated list. As of 2026-10-14:

| Date | Change | How it is checked |
|---|---|---|
| 2026-10-19, rollout to 11-19 | `ubuntu-latest` moves to Ubuntu 26.04 | Your tests run on `ubuntu-26.04` next to `ubuntu-24.04`, for the oldest and newest Python/Ruby in your matrix. A static read of every matrix entry flags versions with no 26.04 build. |
| 2026-10-09 (in effect) | Python 3.15 is the newest Python | Your tests run on 3.15 next to your newest Python. Static checks: a `requires-python` cap that excludes 3.15, imports of the removed `sre_*` modules, and dependencies with a 3.15 cap or no cp315 wheel. |
| 2026-11-02 | GitHub retires `macos-14` | Static: jobs that run on `macos-14`, `-large` or `-xlarge`. |
| 2026-09-23 (in effect) | JavaScript actions run on Node 24 | Report line only. |

Two more conditions are **experimental and opt-in**. They run only when you name them in
`conditions`:

| Date | Change | How it is checked |
|---|---|---|
| 2026-10-28 | Node.js 26 becomes LTS (`lts/*` moves to 26): `node-26-lts` | Your tests run on Node 26 next to your newest Node (Node projects only). |
| 2026-11-01 | Alberta, the NWT, Manitoba and BC stop falling back (tzdata 2026e); the US falls back the same day: `canada-tz-2026-11-01` | Your tests run with `TZ=America/Edmonton`, 2026e rules in the system zoneinfo and a clock set to 2026-11-02 09:30 (libfaketime), next to the same zone on today's clock. |

Every condition run is paired with a baseline run made the same day. Only a failure that the
baseline does not have counts as BREAK. A condition run that fails at an earlier phase than its
baseline (for example at setup, while the baseline installs and fails in a test) is a BREAK. In any
other case where the baseline fails, the condition is reported UNKNOWN, never BREAK. With pytest,
failing tests are compared one by one (JUnit XML). A timeout is UNKNOWN: it depends on runner load as
much as on the change.

## Use in GitHub Actions

```yaml
on:
  schedule: [{cron: "17 6 * * 1"}]
  workflow_dispatch:
jobs:
  future:
    uses: CyberNative-AI/future-ci/.github/workflows/future-ci.yml@<commit SHA> # v0
    with:
      tool-ref: <the same commit SHA>
      install: python -m pip install -e .[test]
      test: python -m pytest -q
```

Use the full commit SHA of the release you choose, in both places. Each release's SHA is on the
[releases page](https://github.com/CyberNative-AI/future-ci/releases). The workflow file and the tool
code it runs are fetched separately, so `tool-ref` is required and has no default.

Inputs:

- `test` (required): the command that runs your tests (bash).
- `install`: the command that installs dependencies, run before `test`.
- `apt`: extra apt packages, space separated.
- `env`: extra environment, one `NAME=value` per line.
- `language`: `python`, `ruby` or `node`. Default: from the root manifest.
- `conditions`: condition ids, space separated. Default: the default set. `default canada-tz-2026-11-01`
  adds an experimental condition to the default set.
- `fail-on`: `none` (report only, the default) or `break` (fail the run when a condition breaks).
- `timeout-minutes`: per phase (install, test). Default 30.
- `tool-ref` (required): the commit SHA the tool code is checked out at. Pass the SHA you pin the
  workflow to. A tag or branch also works, but then the tool code can change under you. The plan job
  resolves the ref to one commit, every later job runs that commit, and the report names it.

The report goes to the job summary and to the `futureci-report` artifact. Every job runs with
`contents: read` and checks out without persisting credentials.

The workflow is report-only unless you set `fail-on: break`. A run that cannot apply its condition
(an `apt` package fails to install, or the tz condition's clock check fails) is reported UNKNOWN
with a warning, and does not fail the workflow.

Every condition run and every baseline run is a separate hosted job. With the default conditions, a
Python project that tests two or more Python versions gets 7 jobs per workflow run: 5 test jobs
(oldest and newest Python on 24.04 and 26.04, and 3.15), one plan job and one report job. A project
that tests one Python version gets 3 test jobs. The opt-in tz condition adds 2 test jobs for each
of Python and Ruby (for Node when neither is present), and opt-in Node 26 adds 2 (the Node 26 run
and its baseline). All of them count against your Actions minutes like any other Linux job.

## What this is not

A hand-written matrix (`runs-on: ubuntu-26.04`, `python-version: "3.15"`, `TZ` plus libfaketime)
gives you the same raw signal for free, and a red CI explains itself on the day. What this adds is
the dated, kept-current list of conditions, picking the versions from your own workflows, the
baseline comparison, and one report that says which change breaks which job and when.

One difference in the raw signal: the libfaketime that `apt-get install faketime` gives you on
ubuntu-24.04 (0.9.10) breaks timed waits in Python under a fake clock (checked with Ubuntu 24.04's own
Python 3.12). With its default settings, lock, event, condition, queue and thread-join timeouts
hang. With the monotonic clock left real (`FAKETIME_DONT_FAKE_MONOTONIC=1`), `time.sleep` and
`subprocess` timeouts fail with EINVAL instead. Either way, a hand-written clock job reports breaks
that are not there. The tz condition here
builds libfaketime 0.9.13 from its pinned upstream source and, before your tests run, checks that a
process sees the condition's clock and that sleeps, lock timeouts and subprocess timeouts still work.
It builds with POSIX semaphores. Upstream's default locks with `flock()` on a descriptor libfaketime
opens at start-up, so a process that closes all its descriptors above 2 (daemons, `closefrom()`
callers, some test helpers) exits from its next `stat()`
([libfaketime#561](https://github.com/wolfcw/libfaketime/issues/561)). Upstream chose `flock()` because a 32-bit
child of a 64-bit process can hang on a POSIX semaphore; the tz condition accepts that tradeoff. The
check also closes its descriptors and confirms that a child process's clock carries on from its parent's.
If that check fails, the condition is reported as UNKNOWN, never as a break.

## How it was tested, and its limits

We ran it with the local engine (below) on 11 public projects, 6 Python and 5 Ruby, whose CI our
static checks had already flagged for Ubuntu 26.04 or Python 3.15. Running each project's own tests
reproduced the flagged break in all 11. After two harness fixes (both in libfaketime handling, both
now covered by the clock check above), none of the reported breaks was false.

What that does and does not show:

- The projects were picked because the static checks flagged them. So the test shows that the
  runs confirm those findings, not that they find breaks the static checks miss.
- Every Ubuntu 26.04 break was found at setup (no Python or Ruby build for 26.04 at that version),
  not in a test.
- The tz condition found no real break in these projects. Node 26 has not been run on a real Node
  project yet. Both are experimental for that reason.

The workflow itself is checked on GitHub-hosted runners against small fixture repositories: one per
default condition, the two experimental ones, a clean control that must stay green with
`fail-on: break`, and the failure paths above.

## Run it locally (Docker)

```sh
python3 futureci.py plan --repo PATH --out plan.json          # needs PyYAML
docker build -t futureci-runner:24.04 --build-arg BASE=ubuntu:24.04 local
docker build -t futureci-runner:26.04 --build-arg BASE=ubuntu:26.04 local
python3 futureci.py local --plan plan.json --repo PATH --out results \
  --install "pip install -e .[test]" --test "pytest -q" \
  --node NODE24_BINARY --actions DIR_WITH_SETUP_ACTION_CHECKOUTS --tzdir DIR_WITH_tzdata2026e.tar.gz
python3 futureci.py report --plan plan.json --results results
```

The local engine runs the released `actions/setup-python`, `ruby/setup-ruby` and
`actions/setup-node` code under Node 24 in `ubuntu:24.04` / `ubuntu:26.04` containers. It sets the
hosted runners' file and stack limits and adds the packages hosted images have that test suites
commonly need. It is close to a hosted job but not the same. Hosted images carry far more
preinstalled software, and they run an SSH server, which the local engine does not. A suite that
connects to `localhost` over SSH fails its baseline locally, so its conditions report UNKNOWN.

Use `--workdir` to put checkouts, caches and toolchains on a disk of your choice instead of the
container engine's own.

## Tests

```sh
python3 -m unittest discover -s tests
```

## Licence

MIT (`LICENSE`). `vendor/check2604.py` is our Ubuntu 26.04 setup checker (MIT, see
`vendor/LICENSE.check2604`). `data/top3000-*.tsv` come from our Python 3.15 install-fallback scan of
PyPI's 3,000 most downloaded projects (2026-10-11). `vendor/data/versions-manifest.json` (from
actions/python-versions, MIT) and `vendor/data/ruby-builder-versions.json` (from ruby/setup-ruby,
MIT) are copied unchanged; their notices are in `vendor/data/NOTICE`.
libfaketime (GPL-2.0) is not vendored: the tz condition downloads and builds it on the runner.

Prepared by [CyberNative AI LLC](https://cybernative.ai), an AI-run company, using AI agents.
Corrections: hello@cybernative.ai
