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
permissions:
  contents: read
  pull-requests: read
jobs:
  suite:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-go@v5
        with:
          go-version-file: go.mod
      - uses: momentohq/mo-eval-action@v1
        with:
          token: ${{ secrets.MO_EVAL_TOKEN }}
          arms: ${{ inputs.arms }}
```

## What runs where

The Action is a thin runner. It reports facts about merged changes (paths and line counts, no
content), sends the source of the few the service selects, and executes the work orders the
service returns — export a tree, apply a patch, run the repository's declared test command with
arguments the service supplies. Which changes become tasks, how a change is split into a
specification and a withheld implementation, and how a task is judged all live on the service.

Validated suites are uploaded to the service's storage by presigned URL; with `arms` set, the
service records a run and evaluates it on its own hardware. The job artifact keeps only the task
packages and the audit log of every crossing.

## Inputs

| input | default | |
|---|---|---|
| `service` | the hosted service | Base URL of the mo-eval service; override for another deployment |
| `token` | — | Bearer token (a repository secret) |
| `arms` | `none` | Model routes to evaluate on; `none` builds the suite without running it |
| `history` | `300` | Merged pull requests offered as candidates |
| `candidates` | `12` | Candidates the service is asked to source |
| `config` | `.mo-eval/config.toml` | The repository's configuration |
| `out` | runner temp | Where the suite is written; nothing lands in the checkout |

The runner needs Python 3.11+ (present on GitHub's hosted runners) and `gh` (also present) and
nothing else. The repository's toolchain must be on `PATH` before this step.
