#!/usr/bin/env python3
"""The runner. What a customer's CI job runs; what the Action wraps.

    mo-eval-runner suite --service https://… --token …     # against the hosted service
    mo-eval-runner suite --service local                    # everything in this process

Reads the repository's own `.mo-eval/config.toml`, collects facts, lets the service choose, sends
source for the chosen few, carries out the orders that come back, reports exit codes, and writes
whatever the service says survived. It ends with a funnel — how many changes were offered, and where
the rest went — because a suite that comes back empty must say which stage emptied it.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from languages import LANGUAGES, Language, detect_framework, resolve_contract  # noqa: E402
from runner.client import ServiceError, client_for  # noqa: E402
from runner.collect import bounded_text, change_source, repo_facts, tracked_files  # noqa: E402
from runner.config import CONFIG_PATH, ConfigError, RunnerConfig, load_config, probe_environment  # noqa: E402
from runner.execute import (OrderError, Runner, _bounded_output, _end_group,  # noqa: E402
                            _export_into, _without_the_runners_credentials, commit_or_refuse)
from wire import (ConventionSources, RepoFacts, ReviewComment, RunRequest, TaskPackage, UploadRequest,  # noqa: E402
                  VerdictReport, Wire)


def _safe_relative(name: str, into: Path) -> Path:
    """A service-supplied file name resolved under `into`, or a refusal.

    The service names the files a package is written as. It is not trusted to keep them inside the
    output directory, so `..`, an absolute path and a symlinked parent are all rejected here.
    """
    candidate = (into / name)
    if name.startswith("/") or ".." in Path(name).parts:
        raise ValueError(f"the service returned the path {name!r}, which leaves the output directory")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(into.resolve()):
        raise ValueError(f"the service returned the path {name!r}, which resolves outside the output directory")
    # And no component of it is a link. `resolve()` follows them, so a path whose ancestor points
    # somewhere else INSIDE the tree still lands under `into` and passes the check above — a
    # checked-in `.mo-eval -> src` would put these writes in the repository's own source and change
    # the start tree the oracle was proven against. `O_NOFOLLOW` guards only the last component.
    walked = into
    for part in Path(name).parts:
        walked = walked / part
        if walked.is_symlink():
            raise ValueError(f"the path {name!r} passes through the link {walked.name!r}")
    return resolved


_MAX_WRITTEN_FILES = 2_000
_MAX_WRITTEN_BYTES = 64 * 1024 * 1024
"""How much of one package this runner will put on the customer's disk. A task is a handful of
files: a manifest, a prompt, a patch, a scorer. The decoder's own bound admits a hundred thousand
strings of sixteen megabytes each, which is a disk the runner does not own."""


def _write_all(files: Mapping[str, str], into: Path, kind: str) -> None:
    """Write a service-supplied map of paths to contents under `into`.

    Counted before anything is written, so a refusal leaves no half-written tree behind.

    Raises:
        ValueError: If the map carries more files, or more bytes, than this runner will write — or
            a path that leaves `into`.
    """
    if len(files) > _MAX_WRITTEN_FILES:
        raise ValueError(f"the service returned {len(files)} {kind} files; at most {_MAX_WRITTEN_FILES}")
    total = sum(len(content.encode()) for content in files.values())
    if total > _MAX_WRITTEN_BYTES:
        raise ValueError(f"the service returned {total} bytes of {kind}; at most {_MAX_WRITTEN_BYTES}")
    # Every path resolved before the first is written. Checked as it went, a map whose last entry
    # escapes the directory would be refused with the rest of it already on disk.
    resolved = [(_safe_relative(name, into), content) for name, content in files.items()]
    for path, content in resolved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def write_package(package: TaskPackage, into: Path) -> Path:
    """Write a package the service returned as `into/<task_id>/`. The runner writes what it is
    handed; it does not import the service to do so."""
    directory = _safe_relative(package.task_id, into)
    directory.mkdir(parents=True, exist_ok=True)
    _write_all(package.files, directory, "package")
    return directory


def _write_without_following(path: Path, content: str) -> None:
    """Write into an exported, repository-controlled tree without following a symlink already there.

    The tree came from the repository, so a file of this name may be checked in as a link to
    anywhere the CI user can write. `O_NOFOLLOW | O_EXCL` after unlinking writes the path itself.
    """
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(content)


_OFFLINE_PREPARE_SECONDS = 1800
"""A deadline on vendoring. It resolves a dependency graph from a tree whose commit the service
chose, which is long work but not endless work."""

_GIT_SECONDS = 600
_APPLY_SECONDS = 120
_EXPORT_SECONDS = 600
"""Deadlines for the commands that build a bundle. The commit they run over is named by the
service and the patch is composed by it, so none of them is a wait this runner should make
without an end — the same bounds `runner/execute.py` puts on the identical operations."""

_FORGE_SECONDS = 120
"""A bound on the one command here that leaves the machine. `gh` retries and follows redirects, and
an unreachable forge would otherwise hold a CI job open until the job's own limit ended it."""


def _bounded(argv: list[str], *, cwd: Path, timeout: float = _GIT_SECONDS,
             env: dict[str, str] | None = None) -> None:
    """Run one command to completion under a deadline, or raise `OrderError`.

    Through the same wait a probe gets, and for the same reasons: its output is spooled rather than
    held in this process, the whole process group is ended whether it finished or not, and the
    leader is reaped only after the group has been signalled — so a package manager that prints for
    an hour cannot exhaust the runner, and a recycled pid cannot receive the kill.

    Args:
        env: The whole environment for the child, or `None` to inherit this process's. Name one for
            anything the repository chose, which is not the same as anything this runner runs.

    Raises:
        OrderError: If the command fails, does not finish, or writes more than the runner spools.
            The runner's callers report that as a message and an exit code; a `CalledProcessError`
            would be a traceback.
    """
    named = " ".join(argv[:2])
    try:
        exit_code, tail, _, _ = _bounded_output(argv, cwd=cwd, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        raise OrderError(f"{named} did not finish within {timeout:.0f}s") from expired
    if exit_code != 0:
        raise OrderError(f"{named} failed: {tail[-400:]!r}")


def write_local_suite(package: TaskPackage, repo: Path, into: Path,
                      config: RunnerConfig | None = None) -> Path | None:
    """Materialize `into/<task_id>/` as a one-task local-test-suite over the task's start tree.

    The start tree is rebuilt the way the validation order built it — the parent commit exported,
    the scaffold applied, one evaluator-owned commit — because mo-eval's snapshotter wants a Git
    worktree root and the order's workspace was deleted when the order finished. Returns `None`
    when the package carries no overlay (no worker image was declared).
    """
    if not package.local_suite:
        return None
    meta = json.loads(package.files["meta.json"])
    _refuse_a_bundle_no_worker_could_score(meta, config)
    root = _safe_relative(package.task_id, into)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    _export_into(repo, commit_or_refuse(meta["parent_commit"]), root, _EXPORT_SECONDS)
    for path in root.rglob("*"):
        os.utime(path, None, follow_symlinks=False)
    git = ["git", "-c", "user.email=mo-eval@example.invalid", "-c", "user.name=mo-eval"]
    # Initialized BEFORE the scaffold is applied: inside a customer's checkout, `git apply` would
    # otherwise resolve the patch against the enclosing repository and silently apply nothing.
    _bounded(git + ["init", "-q"], cwd=root)
    _write_without_following(root / ".mo-eval-scaffold.patch", package.files["scaffold.patch"])
    _bounded(["git", "apply", "--whitespace=nowarn", ".mo-eval-scaffold.patch"], cwd=root, timeout=_APPLY_SECONDS)
    (root / ".mo-eval-scaffold.patch").unlink()
    _prepare_offline(root, meta, config=config)
    _write_all(package.local_suite, root, "local suite")
    _bounded(git + ["add", "-A"], cwd=root)
    for artifact in _offline_artifacts(meta, config):
        _require_within(root, artifact)
        if not (root / artifact).exists():
            # Declared and absent means the preparation did not produce what it promised — a typo, a
            # different path, a partial failure. Skipping it silently freezes a tree WITHOUT the
            # dependencies and ships a bundle that fails in a worker, which is the whole failure this
            # declaration exists to prevent. The exception is a language default, which describes a
            # typical repository rather than this one: `go mod vendor` writes no `vendor/` for a
            # module with no dependencies, and that tree is complete as it stands.
            if config is not None and config.offline_prepare:
                raise OrderError(
                    f"`offline_prepare` did not produce the declared artifact {artifact!r}; the task's "
                    f"tree would be frozen without the dependencies a worker cannot fetch"
                )
            continue
        # Forced past .gitignore: the frozen snapshot takes tracked files, and an ignored
        # vendor/ would vanish between this tree and the worker that scores it.
        _bounded(git + ["add", "-f", artifact], cwd=root)
    _bounded(git + ["commit", "-q", "--no-verify", "-m", f"mo-eval start state for {package.task_id}"], cwd=root)
    return root


_PREPARE_HOME = "/tmp"
"""HOME inside the preparation container: somewhere the runner's uid may actually write.

Deliberately NOT under the mounted tree — a toolchain cache is not part of a task's start state, and
anything left there would be frozen into the bundle and shipped to every worker.
"""

_OFFLINE_PREPARE_MAX_PIDS = 2048
"""How many processes a preparation may have at once.

No memory or CPU ceiling sits beside it, deliberately: a Rust or TypeScript vendoring legitimately
uses several gigabytes, so a cap low enough to bound a hostile one would refuse real work, and a cap
high enough for real work bounds nothing on a CI runner that has less than the cap. The process table
is different — the gap between what a vendoring needs and what an attack needs is three orders of
magnitude, so there is a number that separates them.
"""


def _resolved_offline_prepare(meta: dict, config: RunnerConfig | None) -> str | None:
    """The preparation that will actually run: the repository's own, else the language's default.

    One resolution, because two callers ask this — the gate that refuses an unscorable bundle, and
    the step that runs it — and two spellings of the same question drift. The drift direction is the
    dangerous one: a gate that sees LESS than the preparation passes a bundle the preparation then
    does nothing for.
    """
    declared = config.offline_prepare if config is not None else None
    if declared:
        return declared
    contract = _contract_for(meta)
    return contract.offline_prepare if contract is not None else None


def _require_within(root: Path, artifact: str) -> None:
    """Refuse an artifact path that names anything outside the task's own tree.

    A repository declares these, and they are handed to `git add -f` in a directory this runner
    created. An absolute path or one climbing out of the tree would be asking the runner to reach
    somewhere it has no business reaching — refused for the same reason an order id is checked
    before a directory is made from it.

    Raises:
        OrderError: If the path is absolute, climbs out, or is empty.
    """
    if not artifact or artifact.startswith(("/", "~")) or PurePosixPath(artifact).is_absolute():
        raise OrderError(f"offline artifact {artifact!r} must be a path inside the repository")
    if any(segment == ".." for segment in PurePosixPath(artifact).parts):
        raise OrderError(f"offline artifact {artifact!r} climbs out of the repository")


def _refuse_a_bundle_no_worker_could_score(meta: dict, config: RunnerConfig | None) -> None:
    """Refuse to write a bundle whose dependencies nothing puts into its tree.

    A worker scores with no network. A repository that declares a `setup_command` is saying its tests
    need something installed, and unless something vendors that into the start tree the bundle mines,
    validates, packages and uploads correctly and then fails in the worker before an agent starts —
    `No matching distribution found`, reported as `launch_error` after a lane has claimed the job and
    pulled an image.

    Refused here, at the step that produces the bundle, because whether the dependencies are present
    is a property of the bundle rather than of the lane that later consumes it. Loud and early beats
    correct-looking and unrunnable.

    Raises:
        OrderError: If the repository needs dependencies and nothing declares how to vendor them.
    """
    contract = _contract_for(meta)
    fetches_when_testing = contract is not None and contract.resolves_dependencies_when_testing
    # A contract whose vendoring REPLACES the install needs the installed tree present whether or not
    # the repository named a setup command: `bunx vitest` cannot run without `node_modules`, and such
    # a repository declaring nothing would otherwise ship with neither a setup line nor the tree.
    needs_an_installed_tree = contract is not None and contract.vendoring_replaces_setup
    if config is None or not (config.setup_command or fetches_when_testing or needs_an_installed_tree):
        # Nothing to install and a toolchain that fetches nothing when the tests run, so there is
        # nothing to vendor. A suite whose tests import only what the image already has scores
        # offline today, which is how a synthetic one-file suite passes.
        return
    if _resolved_offline_prepare(meta, config):
        return
    why = ("its tests resolve dependencies as they run" if fetches_when_testing
           else "its test runner needs an installed dependency tree" if needs_an_installed_tree
           else "it declares a `setup_command`")
    raise OrderError(
        f"{why}, and nothing puts those dependencies into the task's tree — a worker scores with no "
        f"network. Declare `offline_prepare` and `offline_artifacts` in {CONFIG_PATH}, or the bundle "
        f"cannot be scored"
    )


def _prepare_offline(root: Path, meta: dict, run=None, config: RunnerConfig | None = None) -> None:
    """Make the start tree scorable with no network, the way its language does that.

    mo-eval's baseline and scorer workers have no network at all — the first live run failed on
    `dial tcp: lookup proxy.golang.org` from inside `go test`. The dependencies have to be IN the
    frozen tree. Runs before the evaluator-owned commit so they are part of the start state the agent
    receives and the scorer restores, not part of the agent's diff.

    Vendoring is `go mod vendor`, `npm ci` and their kind — parents of resolvers and compilers, so
    it goes through `_bounded` and its deadline takes the whole tree rather than only its root.

    The command is the repository's, and what it runs is the repository's too: a Gradle preparation
    executes the checked-out build script, at a commit the service chose. So it is given the same
    environment a probe gets — this runner's own credentials removed — rather than the job's whole
    environment, which carries the forge token and the pair that mints an OIDC identity.

    Raises:
        OrderError: If the preparation fails or does not finish. A bundle that cannot be scored
            offline must not be written as though it could.
    """
    run = run or _bounded
    contract = _contract_for(meta)
    # The repository's own, when it declared one: only it knows how its dependencies install, and
    # the language's default is a default rather than an answer. Go needs nothing declared because
    # `go mod vendor` IS the answer for every Go repository; `pip install -e .[dev] pytest` is not.
    prepare = _resolved_offline_prepare(meta, config)
    if not prepare:
        return
    image = config.worker_image if config is not None else None
    if image and shutil.which("docker") is None:
        # Refused rather than prepared here instead. Falling back to this host would vendor for THIS
        # platform and interpreter, which is the failure the image exists to avoid — and it would do
        # it silently, leaving a bundle that looks complete and cannot be scored.
        raise OrderError(
            "this repository declares a worker image and an offline preparation, but there is no "
            "`docker` on this runner to prepare in it. Preparing on the runner instead would vendor "
            "for the runner's own platform, which the worker cannot use"
        )
    if image:
        # IN the worker image, not on this host. A vendored dependency is often a binary built for
        # one platform and one interpreter: preparing `sqlglot` on macOS produced
        # `duckdb-1.5.5-cp313-cp313-macosx_11_0_arm64.whl`, which a linux/amd64 worker running
        # Python 3.12 cannot use — and the preparation SUCCEEDS, so the bundle looks complete and
        # fails offline much later. Prepared where it will be consumed, it is right by construction.
        #
        # With a network, unlike scoring: this is the step whose whole job is fetching what scoring
        # will not be able to.
        # Named, so it can be stopped by name. Ending the deadline kills the `docker` CLI and the
        # process group it sits in, which is enough for every other command this runner runs — but a
        # container is a child of the daemon, not of the CLI. Kill the CLI alone and the container
        # keeps running, and `--rm` never fires because it removes a container only once it exits.
        container = f"mo-eval-prepare-{os.getpid()}-{root.name}"
        try:
            run(["docker", "run", "--rm", "--name", container,
                 # A deadline bounds how LONG a preparation runs, not how fast it consumes. A fork
                 # bomb exhausts the host's process table long before the deadline fires, and killing
                 # the container afterwards cannot give back what was already taken. Far above any
                 # real vendoring — `pip wheel`, `cargo vendor` and `bun install` spawn tens of
                 # processes, not thousands — so it costs a legitimate repository nothing.
                 "--pids-limit", str(_OFFLINE_PREPARE_MAX_PIDS),
                 # The runner's own uid, so what is vendored is owned by whoever has to read it
                 # afterwards rather than by root. That uid has no entry in the image's passwd file,
                 # so HOME resolves to `/` — which nothing may write — and every toolchain puts its
                 # cache under HOME: Go's build cache, `$CARGO_HOME`, pip's, npm's, bun's. Go is
                 # simply the one that refuses to start without it:
                 #
                 #     failed to initialize build cache at /.cache/go-build: mkdir /.cache: denied
                 #
                 # So every Go bundle failed to build on any runner that is not root, which is every
                 # GitHub Actions runner (#4137). One writable HOME answers all of them at once.
                 "--user", f"{os.getuid()}:{os.getgid()}",
                 "-e", f"HOME={_PREPARE_HOME}",
                 "-v", f"{root}:/mo-eval-tree", "-w", "/mo-eval-tree", "--entrypoint", "sh",
                 image, "-c", prepare],
                cwd=root, timeout=_OFFLINE_PREPARE_SECONDS,
                env=_without_the_runners_credentials(os.environ, {}))
        except Exception:
            # Best effort and deliberately silent: the failure being reported is the preparation's,
            # and a cleanup that raises would replace it with one about cleaning up.
            subprocess.run(["docker", "kill", container], capture_output=True, timeout=_GIT_SECONDS,
                           check=False)
            raise
        return
    # Through a shell, the same as the in-image path above: vendoring is often a pipeline —
    # `cargo vendor >> .cargo/config.toml` writes the configuration that makes the vendor directory
    # take effect — and a declared command must mean one thing wherever it runs.
    run(["sh", "-c", prepare], cwd=root, timeout=_OFFLINE_PREPARE_SECONDS,
        env=_without_the_runners_credentials(os.environ, {}))


def _contract_for(meta: dict) -> Language | None:
    """The contract a package was generated under, including a detected variant such as `go+ginkgo`,
    which `LANGUAGES` alone does not name."""
    try:
        return resolve_contract(meta.get("generated", {}).get("language", ""))
    except KeyError:
        return None


def _offline_artifacts(meta: dict, config: RunnerConfig | None = None) -> tuple[str, ...]:
    """What the offline preparation leaves behind that must be tracked — the repository's own when
    it declared a preparation, the language's otherwise. Read from whichever declared the command,
    because artifacts that do not belong to the command that ran are artifacts that do not exist."""
    if config is not None and config.offline_prepare:
        return config.offline_artifacts
    contract = _contract_for(meta)
    return contract.offline_artifacts if contract is not None else ()


def _repo_name(repo: Path, declared: str | None) -> str:
    if declared:
        return declared
    url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo, capture_output=True, text=True,
                         timeout=_GIT_SECONDS).stdout.strip()
    tail = url.removesuffix(".git").replace(":", "/").rstrip("/")
    parts = tail.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else tail


def _branch(repo: Path, repo_name: str) -> str:
    """The branch merged pull requests target — the repository's DEFAULT branch, not whatever is
    checked out.

    A CI job checks out the pushed branch, and a developer often has a feature branch out; listing
    merged PRs against either returns nothing, silently.
    """
    # The forge first, and `origin/HEAD` after it. `repo_name` is what the config declares, which on
    # a fork is the UPSTREAM — and merged pull requests are listed against that repository. A fork
    # whose own default differs would otherwise set `--base` to its branch and list nothing, which
    # reads as an upstream with no merged work.
    forge = subprocess.run(["gh", "repo", "view", "-R", repo_name, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
                           cwd=repo, capture_output=True, text=True, timeout=_FORGE_SECONDS).stdout.strip()
    if forge:
        return forge
    # Then the clone's own default, then whatever is checked out.
    head = subprocess.run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo,
                          capture_output=True, text=True, timeout=_GIT_SECONDS).stdout.strip()
    if head:
        return head.split("/", 1)[-1]
    return subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo, capture_output=True,
                          text=True, timeout=_GIT_SECONDS).stdout.strip() or "main"


def _contract_name(repo: Path, language: str, test_command: str = "") -> str:
    """Which framework this repository actually uses, measured from its test files and from the
    command it declared it runs them with."""
    base = LANGUAGES[language]
    contents = []
    for path in tracked_files(repo, lambda candidate: base.test_path.search(candidate) is not None)[:200]:
        text = bounded_text(repo / path)
        if text is not None:
            contents.append(text)
    return detect_framework(contents, base, test_command).name


def asked_for(change_ids: list[str], facts: RepoFacts, candidates: int) -> list[str]:
    """The changes the service selected, confirmed to be changes this runner offered.

    The runner reads a blob per file of every id here and then runs an order for each. A service
    that answered with more ids than were asked for — or with ids from somewhere else — would spend
    a customer's CI on work they did not request, and the decoder's own bound is a hundred thousand.

    Raises:
        ServiceError: If the answer names more than was asked for, or a change that was not offered.
    """
    offered = {change.change_id for change in facts.changes}
    unknown = [change_id for change_id in change_ids if change_id not in offered]
    if unknown:
        raise ServiceError(f"the service selected {len(unknown)} change(s) this repository did not offer")
    unique = list(dict.fromkeys(change_ids))
    if len(unique) > candidates:
        raise ServiceError(f"the service selected {len(unique)} changes for {candidates} candidate(s)")
    return unique


def verdicts_asked_for(report, orders: int):
    """The verdicts the service returned, confirmed to be one per order this runner actually ran.

    Each verdict with a task attached costs a package on disk, a tree export, a vendoring step and
    an archive held open until it uploads. Unbounded, a service can spend a CI runner's whole disk
    answering a request for eight.

    Raises:
        ServiceError: If the answer carries more verdicts than there were orders.
    """
    if len(report.verdicts) > orders:
        raise ServiceError(f"the service returned {len(report.verdicts)} verdicts for {orders} order(s)")
    return report


def orders_asked_for(orders: list, candidates: int) -> list:
    """The orders the service composed, confirmed to be no more work than was requested.

    Raises:
        ServiceError: If the answer carries more orders than candidates were asked for.
    """
    if len(orders) > candidates:
        raise ServiceError(f"the service returned {len(orders)} orders for {candidates} candidate(s)")
    return orders


def suite(arguments: argparse.Namespace) -> int:
    repo = arguments.repo.resolve()
    try:
        config = load_config(repo / arguments.config)
    except ConfigError as failure:
        print(f"config: {failure}", file=sys.stderr)
        return 2
    environment, missing = probe_environment(config, os.environ)
    if missing:
        print(f"config: forward_env names {missing} but the environment does not have them; "
              f"probes needing them will fail identically", file=sys.stderr)
    try:
        client = client_for(arguments.service, arguments.token or os.environ.get("MO_EVAL_TOKEN"))
    except ServiceError as failure:
        print(f"service: {failure}", file=sys.stderr)
        return 2

    out = arguments.out.resolve()
    wire = Wire(root=out / "wire")
    contract = _contract_name(repo, config.language, config.test_command)
    name = _repo_name(repo, config.repo)
    print(f"mo-eval-runner · {name} · {contract}")

    facts = repo_facts(
        repo, repo_name=name, language=contract, test_command=config.test_command,
        branch=arguments.branch or _branch(repo, name), limit=arguments.history,
        language_contract=LANGUAGES[config.language], setup_command=config.setup_command,
    )
    # Recorded after the declaration is folded in, so the audit holds what was sent rather than an
    # earlier version of it.
    facts = wire.crossing("up", "repo-facts", replace(facts, worker_image=config.worker_image))
    try:
        request = wire.crossing("down", "source-request", client.select(facts, arguments.candidates))
        request = replace(request, change_ids=asked_for(request.change_ids, facts, arguments.candidates))
        sources = wire.crossing("up", "change-source", change_source(repo, facts, request.change_ids))
        response = wire.crossing("down", "work-orders", client.orders(facts, sources, arguments.candidates))
        response = replace(response, orders=orders_asked_for(response.orders, arguments.candidates))
    except ServiceError as failure:
        print(f"service: {failure}", file=sys.stderr)
        return 1

    orders = response.orders[: arguments.validate] if arguments.validate else response.orders
    if arguments.dry_run:
        print(f"  --dry-run: {len(orders)} order(s) issued, none run")
        _funnel(facts, request, response, 0, VerdictReport([]), [], dry_run=True)
        return 0
    runner = Runner(repo=repo, workspaces=out / "workspaces", test_command=config.test_command,
                    env=environment, setup_command=config.setup_command,
                    # Which of those names carry a credential, so the runner can take their values
                    # out of what crosses back. `env` alone cannot say: a build flag and a token
                    # look the same once they are merged.
                    secret_names=tuple(config.forward_env),
                    # The shape a probe's arguments must have. Without it the service could append
                    # anything the repository's test tool accepts, which is more than a filter.
                    filter_template=resolve_contract(contract).filter_template)
    results = []
    for order in orders:
        print(f"  running {order.order_id} …", flush=True)
        results.append(runner.run(order))
    wire.crossing("up", "order-results", results)
    try:
        report = verdicts_asked_for(wire.crossing("down", "verdicts", client.verdicts(results)),
                                    len(results))
    except ServiceError as failure:
        print(f"service: {failure}", file=sys.stderr)
        return 1

    written = []
    for verdict in report.verdicts:
        mark = "VALIDATED" if verdict.validated else "rejected "
        print(f"  {mark} {verdict.change_id[:9]}  {verdict.detail}")
        if verdict.task is not None:
            written.append(write_package(verdict.task, out / "tasks"))
            try:
                bundle = write_local_suite(verdict.task, repo, out / "local-suite", config)
            except (OrderError, ValueError, OSError) as failure:
                # The task itself is already written and still valid. Only its containerized
                # bundle could not be built, and one task's bundle failing is not the suite's end.
                print(f"             local-suite bundle failed: {failure}", file=sys.stderr)
                continue
            if bundle is not None:
                print(f"             local-suite bundle → {bundle}")

    _funnel(facts, request, response, len(orders), report, written)
    if written and arguments.run:
        conventions = _conventions(repo, facts.repo, min(arguments.history, 120))
        try:
            _hand_off(client, facts.repo, out, routes=arguments.run, repeats=arguments.repeats,
                      conventions=conventions, harnesses=arguments.harnesses)
        except ServiceError as failure:
            # The validated tasks are already on disk, so this is recoverable: the same hand-off is
            # what `submit --out <dir>` does. Reported as a message and an exit code, not a traceback.
            print(f"service: {failure}", file=sys.stderr)
            print(f"the validated tasks are in {out}; retry the hand-off with `submit --out {out}`", file=sys.stderr)
            return 1
    return 0 if written else 3


def submit(arguments) -> int:
    """Hand an already-validated `--out` tree to the service for evaluation."""
    conventions = None
    if arguments.conventions_from is not None:
        conventions = _conventions(Path(arguments.conventions_from), arguments.repo_name, arguments.history)
    try:
        client = client_for(arguments.service, arguments.token or os.environ.get("MO_EVAL_TOKEN"))
        _hand_off(client, arguments.repo_name, Path(arguments.out), routes=arguments.run,
                  repeats=arguments.repeats, conventions=conventions, harnesses=arguments.harnesses)
    except ServiceError as failure:
        # Reported the way `suite` reports it: a named service that cannot be reached is a message
        # and an exit code, not a traceback.
        print(f"service: {failure}", file=sys.stderr)
        return 2
    return 0


def _conventions(repo: Path, repo_name: str, history: int) -> ConventionSources:
    """Collect what the conventions judge learns from. Review comments come from the most recently
    merged pull requests, not only the mined ones: a convention is stated wherever it was violated."""
    from runner.collect import convention_files, review_comments  # noqa: PLC0415
    numbers = _recent_merged(repo, repo_name, history)
    comments = [ReviewComment(**c) for c in review_comments(repo, repo_name, numbers)]
    files = convention_files(repo)
    print(f"  conventions: {len(files)} file(s), {len(comments)} review comment(s) from {len(numbers)} merged PRs")
    return ConventionSources(files=files, comments=comments)


def _recent_merged(repo: Path, repo_name: str, limit: int) -> list[int]:
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "-R", repo_name, "--state", "merged", "--limit", str(limit), "--json", "number",
             "--jq", ".[].number"],
            cwd=repo, capture_output=True, text=True, timeout=120, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    return [int(n) for n in out.split()]


def _hand_off(client, repo_name: str, out: Path, routes: list[str], repeats: int,
              conventions: ConventionSources | None = None,
              harnesses: list[str] | None = None) -> None:
    """Upload every validated bundle to the service's storage and record a run for the lane.

    The bundles are tarred here and PUT to presigned URLs, so the service never receives the bytes
    (a bundle is a vendored start tree — far larger than a function is willing to carry). What is
    left on GitHub's side afterwards is nothing: the agent runs, the gateway key, and the spend all
    live on the service's side.
    """
    import tarfile, tempfile, urllib.error, urllib.request, time
    suite_dir = out / "local-suite"
    bundles = sorted(p for p in suite_dir.iterdir() if p.is_dir()) if suite_dir.is_dir() else []
    if not bundles:
        print("  nothing to hand off: no local-suite bundles (does the config declare worker_image?)")
        return
    task_ids = [p.name for p in bundles]
    suite_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{task_ids[0][-8:]}"
    # Archived before the URLs are asked for, so the request can say how large each bundle is and
    # the service can sign that size into the upload it authorizes. Each one goes to a file rather
    # than a buffer: a bundle is a vendored start tree, and building it in memory costs its
    # compressed size and then copies that — on a CI runner, the archive is what runs out.
    archives: dict[str, Path] = {}
    try:
        for bundle in bundles:
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as handle:
                archives[bundle.name] = Path(handle.name)
            with tarfile.open(archives[bundle.name], mode="w:gz") as tar:
                tar.add(bundle, arcname=bundle.name)
        archive_sizes = {name: archive.stat().st_size for name, archive in archives.items()}
        targets = client.uploads(UploadRequest(repo=repo_name, suite_id=suite_id, task_ids=task_ids, sizes=archive_sizes))
        print(f"\n  uploading {len(task_ids)} bundle(s) for suite {suite_id}")
        for name, archive in archives.items():
            url = targets.urls[name]
            if url.startswith("file://"):
                Path(url[7:]).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(archive, url[7:])
            elif not url.startswith("memory://"):
                with archive.open("rb") as body:
                    request = urllib.request.Request(
                        url, data=body, method="PUT",
                        headers={"content-type": "application/gzip", "content-length": str(archive_sizes[name])},
                    )
                    try:
                        with urllib.request.urlopen(request, timeout=300) as response:
                            response.read()
                    except (urllib.error.URLError, OSError) as failure:
                        # Storage is a different host from the service, and its failures arrive as
                        # their own exception type — including a bare `TimeoutError` when the
                        # response times out, which is not a `URLError`. Raised as a `ServiceError`
                        # so both callers report a hand-off that did not happen the way they report
                        # every other one.
                        raise ServiceError(f"uploading {name}: {getattr(failure, 'reason', failure)}") from failure
            print(f"    {name}  {archive_sizes[name]/1e6:.1f} MB")
    finally:
        for archive in archives.values():
            archive.unlink(missing_ok=True)
    titles, categories, sizes = {}, {}, {}
    for task_id in task_ids:
        meta = out / "tasks" / task_id / "meta.json"
        if meta.is_file():
            record = json.loads(meta.read_text())
            titles[task_id] = record.get("title", "")
            generated = record.get("generated", {})
            if generated.get("category"):
                categories[task_id] = generated["category"]
            if generated.get("size"):
                sizes[task_id] = generated["size"]
    # No harness named stays unnamed all the way to the service, which is what decides what that
    # means — rather than a default here to drift from it, or a key in a request that predates it.
    ticket = client.runs(RunRequest(repo=repo_name, suite_id=suite_id, task_ids=task_ids, arms=routes,
                                    repeats=repeats, titles=titles, conventions=conventions,
                                    categories=categories, sizes=sizes, harnesses=harnesses))
    print(f"  run {ticket.run_id} recorded ({ticket.job_key}); results will appear under {ticket.results_prefix}")


def _funnel(facts, request, response, ran: int, report, written: list[Path], *, dry_run: bool = False) -> None:
    """Where every offered change went. The number a user needs is not how many tasks they got,
    but why they did not get more."""
    offered = len(facts.changes)
    notes = []
    if facts.unresolved_changes:
        notes.append(f"{facts.unresolved_changes} PRs unresolvable to a parent/reference pair")
    if facts.unreadable_changes:
        notes.append(f"{facts.unreadable_changes} unreadable")
    if facts.renaming_changes:
        notes.append(f"{facts.renaming_changes} rename a source file, which cannot be scaffolded yet")
    if facts.source != "github-prs":
        notes.append(f"read from git log, squash merges only: {facts.source_note or 'forge not consulted'}")
    suffix = f"  ({'; '.join(notes)})" if notes else ""
    print(f"\n  offered    {offered:>4} merged changes via {facts.source}{suffix}")
    print(f"  selected   {len(request.change_ids):>4}   " + _reasons(request.rejections))
    print(f"  orders     {len(response.orders):>4}   " + _reasons(response.rejections))
    if dry_run:
        # Nothing ran, so nothing survived or failed to; saying so would misread a dry run as a
        # repository that yields nothing.
        print(f"  validated     —   (dry run: {len(response.orders)} order(s) would run)")
        return
    validated = sum(1 for v in report.verdicts if v.validated)
    print(f"  validated  {validated:>4} of {ran} run")
    for path in written:
        print(f"             → {path}")
    if not written:
        print("\n  no task survived; the stage that emptied the funnel is the one to look at")


def _reasons(rejections) -> str:
    if not rejections:
        return ""
    top = ", ".join(f"{r.count} {r.reason}" for r in rejections[:3])
    more = f", +{len(rejections) - 3} more" if len(rejections) > 3 else ""
    return f"rejected: {top}{more}"


def main() -> int:
    parser = argparse.ArgumentParser(prog="mo-eval-runner", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    s = commands.add_parser("suite", help="mine, validate, and write this repository's suite")
    s.add_argument("--repo", type=Path, default=Path("."))
    s.add_argument("--config", type=Path, default=CONFIG_PATH, help="relative to --repo")
    s.add_argument("--service", default="local", help="'local' or the service base URL")
    s.add_argument("--token", default=None, help="bearer token, else $MO_EVAL_TOKEN")
    s.add_argument("--branch", default=None)
    s.add_argument("--history", type=int, default=300)
    s.add_argument("--candidates", type=int, default=8)
    s.add_argument("--validate", type=int, default=0, help="cap on orders to run (0 = all)")
    s.add_argument("--out", type=Path, default=Path(".mo-eval") / "out")
    s.add_argument("--dry-run", action="store_true", help="stop after orders are issued; run nothing")
    s.add_argument("--run", nargs="*", metavar="ROUTE", default=None,
                   help="after validating, upload the bundles and ask the service to evaluate them on these model routes")
    s.add_argument("--harnesses", nargs="+", metavar="NAME", default=None,
                   help="client harnesses to compare, each against every route: mo, cc, or both (default: mo)")
    s.add_argument("--repeats", type=int, default=1)
    s.set_defaults(command_fn=suite)
    m = commands.add_parser("submit", help="upload an already-validated --out tree and ask the service to evaluate it")
    m.add_argument("--out", required=True)
    m.add_argument("--repo-name", required=True, help="owner/name the suite was mined from")
    m.add_argument("--service", required=True)
    m.add_argument("--token", default=None, help="bearer token, else $MO_EVAL_TOKEN")
    m.add_argument("--run", nargs="+", metavar="ROUTE", required=True,
                   help="model routes, each paired with every harness")
    m.add_argument("--harnesses", nargs="+", metavar="NAME", default=None,
                   help="client harnesses to compare, each against every route: mo, cc, or both (default: mo)")
    m.add_argument("--repeats", type=int, default=1)
    m.add_argument("--conventions-from", metavar="REPO", default=None,
                   help="a checkout to collect convention sources from (contributing guide, lint config, review comments)")
    m.add_argument("--history", type=int, default=120, help="merged pull requests to read review comments from")
    m.set_defaults(command_fn=submit)
    arguments = parser.parse_args()
    return arguments.command_fn(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
