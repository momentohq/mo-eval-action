# mo-eval suite — GitHub Action

Turns a repository's own merged pull requests into an evaluation suite for coding agents, validates
every task with the repository's tests, and hands the suite to the mo-eval service to run models
against. What the repository keeps is small: this Action in a workflow, and one config file.

```toml
# .mo-eval/config.toml
language = "go"
test_command = "go test"
worker_image = "golang:1.25-bookworm@sha256:…"   # pinned by digest; a mutable tag is refused
# repo = "owner/name"   # only on a fork: whose merged pull requests are the record
```

```yaml
# .github/workflows/mo-eval.yml
name: mo-eval suite
on:
  workflow_dispatch:
    inputs:
      arms:
        description: Models to evaluate, space-separated ("none" = build the suite only)
        default: "anthropic/claude-opus-5 momento/zai-org/GLM-5.3"
      harnesses:
        description: Coding-agent harnesses, space-separated (mo, cc, pi)
        default: "mo"
permissions:
  contents: read
  pull-requests: read
  id-token: write   # the job authenticates to mo-eval as this repository; no secret to manage
jobs:
  suite:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          # The job runs the repository's own historical tests, as the same user in the same
          # workspace. Left on disk, the forge token is readable by them.
          persist-credentials: false
      - uses: actions/setup-go@v5
        with:
          go-version-file: go.mod
      - uses: momentohq/mo-eval-action@v1
        with:
          arms: ${{ inputs.arms }}
          harnesses: ${{ inputs.harnesses }}
```

## Before enrolling: is the repository a fit?

Recency is not fit. A repository that merges constantly can offer almost nothing to mine when most
of what it merges is docs, CI or tests, and one that merges rarely offers changes too old for their
dependencies to install. Run once with `dry-run: "true"` first: it mines and selects in seconds and
prints the funnel — how many changes were offered, how many selected, and the leading reasons the
rest were turned away — without validating anything. A full run also appends the funnel to the job
summary, and when nothing survives it shows the first failure of each kind and what the dominant
reason usually means.

## What runs where

The Action is a thin runner. It reports facts about merged changes (paths and line counts, no
content), sends the source of the few the service selects, and executes the work orders the
service returns — export a tree, apply a patch, run the repository's declared test command with
arguments the service supplies. Which changes become tasks, how a change is split into a
specification and a withheld implementation, and how a task is judged all live on the service.

Validated suites are uploaded to the service's storage by presigned URL; with `arms` set, the
service records a run and evaluates it on its own hardware. The job artifact keeps only the task
packages and the audit log of every crossing.

### Containment, and the runner to choose

Building a suite means running the repository's **own** declared commands — its setup, its test
command, its `offline_prepare`. Where each runs differs, and the difference is what decides which
`runs-on` is safe:

| stage | where | bounded by | network |
|---|---|---|---|
| probes — does the named test fail before the change and pass after | **the CI host itself** | a deadline and an output cap | the host's |
| `offline_prepare` | a container, in the image the repository declared | deadline, `--pids-limit`, the runner's own uid | yes — vendoring has to fetch |
| the score check | the same container shape | deadline, `--pids-limit`, the runner's own uid | `--network none` |
| scoring a task | a worker, on the service's hardware | deadline, `--memory`, `--pids-limit` | `--network none`; the agent's own phase reaches the gateway and nothing else |

The probes run first and always, and they run **uncontained**: no cgroup, no pids limit, no
filesystem isolation. What holds on every path is credentials — the runner's own bearer, the forge
token, and the pair that mints an OIDC token as this repository are all removed from the
environment before any repository command runs, and the workflow above checks out with
`persist-credentials: false` so none is left on disk either.

Uncontained is the right answer for the job this Action is built for, and the wrong one just
outside it:

- **A GitHub-hosted runner — the supported choice.** The host is disposable and belongs to the
  repository whose commands it runs. A repository that wants to exhaust its own ephemeral runner
  needs no help from us.
- **A self-hosted runner — not supported.** Those commands run on a host that outlives the job, and
  on a shared one, a host that another repository also uses. Nothing here sandboxes them.
- **`pull_request_target` — never.** It would run a contributor's declared commands, from their
  fork, with this workflow's secrets. A plain `pull_request` on a GitHub-hosted runner is no worse
  than any CI that runs a fork's tests; on a self-hosted runner it is the previous bullet with an
  untrusted author. The sample above avoids the question with `workflow_dispatch`.

## Inputs

| input | default | |
|---|---|---|
| `service` | the hosted service | Base URL of the mo-eval service; override for another deployment |
| `token` | — | Optional. Without it the job authenticates as the repository via GitHub's OIDC token (`permissions: id-token: write`); with it, a static bearer for the service |
| `arms` | `none` | Model routes to evaluate on; `none` builds the suite without running it |
| `harnesses` | `mo` | Space-separated `mo`, `cc`, `pi`; each runs against every model route. Set `mo pi` to compare Mo and Pi |
| `history` | `300` | Merged pull requests offered as candidates |
| `candidates` | `12` | Candidates the service is asked to source |
| `config` | `.mo-eval/config.toml` | The repository's configuration |
| `out` | runner temp | Where the suite is written; nothing lands in the checkout |
| `dry-run` | `false` | `"true"` mines and selects, prints the funnel, and runs nothing: a first look at fit in seconds |
| `repeats` | `1` | How many times to run each arm, so a comparison rests on more than one sample. **1 to 5.** The service **refuses** an over-cap request rather than running fewer — a run recorded as 5 when 9 was asked for would be a different benchmark reported as yours |
| `repo-name` | this repository | The `owner/name` the suite was mined as — the tenant the service proved its tasks under. Set it only when the config declares an upstream with `repo = "owner/name"`: on a fork that is the upstream, and the checkout's own name would be a different tenant |
| `reuse-suite` | — | Path to a suite a previous run already mined and validated. Set it to skip mining and validation and only run `arms`. The path must outlive the run that built it — use a **cache**, not this Action's artifact, which carries the task records and not the runnable bundles. Cannot be combined with `dry-run` (there is no mining pass to stop short of) and needs `arms` set |

The runner needs Python 3.11+ (present on GitHub's hosted runners) and `gh` (also present) and
nothing else. The repository's toolchain must be on `PATH` before this step.

## Running more arms without re-mining

Mining reads merged pull requests and validation runs the repository's own tests once per candidate.
Changing the model route or the harness changes neither, so a second comparison does not need a
second suite.

Build once, keeping the suite somewhere that outlives the job. A **cache**, not this Action's
artifact: the artifact carries the task records for reading (`tasks/`, `wire/`, `funnel.txt`)
and deliberately not the runnable bundles, so a suite restored from it has no `local-suite/`
and `reuse-suite` refuses it.

```yaml
- uses: momentohq/mo-eval-action@v1
  with:
    out: ${{ runner.temp }}/mo-eval
    arms: none            # build and validate, run nothing
- uses: actions/cache/save@v4
  with:
    path: ${{ runner.temp }}/mo-eval
    key: mo-eval-suite-${{ github.sha }}
```

Then run as many arm/harness combinations as you like against it:

```yaml
- uses: actions/cache/restore@v4
  with:
    path: ${{ runner.temp }}/mo-eval
    key: mo-eval-suite-${{ github.sha }}
- uses: momentohq/mo-eval-action@v1
  with:
    reuse-suite: ${{ runner.temp }}/mo-eval
    arms: momento/zai-org/GLM-5.2
    harnesses: mo cc
    repeats: "3"
```

The bundles are uploaded again on each run — a run names storage keys the service resolves, so the
transfer is what a run costs. That is the cheap half; what is skipped is the mining and the
validation. The step summary says which suite was reused, and that nothing was mined, so a reused
run is never read as a mining pass that found the same thing twice.
