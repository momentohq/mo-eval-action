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
import itertools
import json
import logging
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any as JSONAny
from typing import Protocol, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import contextlib
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

from mo_eval_svc.languages import LANGUAGES, Language, detect_framework, offline_setup, resolve_contract
from mo_eval_svc.runner.client import (
    REQUEST_BUDGET_BYTES,
    RunClient,
    ServiceClient,
    ServiceError,
    client_for,
    plan_order_requests,
)
from mo_eval_svc.runner.collect import bounded_text, change_source, repo_facts, tracked_files
from mo_eval_svc.runner.config import (
    CONFIG_PATH,
    ConfigError,
    RunnerConfig,
    load_config,
    probe_environment,
)
from mo_eval_svc.runner.execute import (
    OrderError,
    Runner,
    _bounded_output,
    _export_into,
    _without_the_runners_credentials,
    commit_or_refuse,
)
from mo_eval_svc.wire import (
    CLIENT_VERSION_PATTERN,
    SCORER_IN_TREE,
    ChangeSource,
    ClientVersions,
    ConventionSources,
    OrderResult,
    OrdersResponse,
    OrderVerdict,
    RejectionSummary,
    RepoFacts,
    ReviewComment,
    RunRequest,
    SourceRequest,
    TaskPackage,
    UploadRequest,
    VerdictReport,
    Wire,
    WorkOrder,
)
from mo_eval_svc.worker_platform import DEFAULT_WORKER_PLATFORM, WorkerPlatform

_LOGGER = logging.getLogger(__name__)
"""Runner progress events. Entrypoints choose their rendering without coupling the runner to one."""

LoggingConfigurator = Callable[[bool], None]
"""Configures the CLI's logging handler for whether detailed output was requested."""


def configure_plain_logging(verbose: bool) -> None:
    """Configure concise standard-library progress logging for the vendored runner entrypoint.

    Args:
        verbose: Whether diagnostic progress should be included.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        force=True,
    )


def _safe_relative(name: str, into: Path) -> Path:
    """A service-supplied file name resolved under `into`, or a refusal.

    The service names the files a package is written as. It is not trusted to keep them inside the
    output directory, so `..`, an absolute path and a symlinked parent are all rejected here.

    Returns:
        The resolved destination inside `into`, after rejecting traversal and symlink
        components.

    Raises:
        ValueError: If the name is absolute, contains traversal, escapes the output directory,
            or passes through a symlink.
    """
    candidate = into / name
    if name.startswith("/") or ".." in Path(name).parts:
        raise ValueError(f"the service returned the path {name!r}, which leaves the output directory")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(into.resolve()):
        raise ValueError(
            f"the service returned the path {name!r}, which resolves outside the output directory"
        )
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
    handed; it does not import the service to do so.

    Returns:
        The directory containing the written task package.
    """
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


def _bounded(
    argv: list[str], *, cwd: Path, timeout: float = _GIT_SECONDS, env: dict[str, str] | None = None
) -> None:
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


def write_local_suite(
    package: TaskPackage,
    repo: Path,
    into: Path,
    config: RunnerConfig | None = None,
    cache: Path | None = None,
) -> Path | None:
    """Materialize `into/<task_id>/` as a one-task local-test-suite over the task's start tree.

    The start tree is rebuilt the way the validation order built it — the parent commit exported,
    the scaffold applied, one evaluator-owned commit — because mo-eval's snapshotter wants a Git
    worktree root and the order's workspace was deleted when the order finished. Returns `None`
    when the package carries no overlay (no worker image was declared).

    Returns:
        The committed, scoreable task start tree, or `None` when the package carries no local-
        suite overlay.

    Raises:
        OrderError: If a declared offline artifact is absent, or the task cannot be prepared and
            scored in its worker image.
    """
    if not package.local_suite:
        return None
    meta = json.loads(package.files["meta.json"])
    _refuse_a_bundle_no_worker_could_score(meta, config)
    root = _safe_relative(package.task_id, into)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    try:
        return _build_local_suite(package, repo, root, meta, config, cache)
    except BaseException:
        # A refused bundle must not survive its refusal. The score check runs AFTER the overlay is
        # written, so what it declines is a complete tree — manifest and all — and `_hand_off`
        # screens failed builds by asking whether the manifest is there. Left on disk, a refused
        # bundle passes that screen and ships: measured on `momentohq/hono`, where the check named
        # all four tasks, the run still reported `validated 4 of 4`, and the lane returned
        # `scorer_error` on every cell — the outcome the refusal had predicted in words.
        shutil.rmtree(root, ignore_errors=True)
        raise


def _build_local_suite(
    package: TaskPackage,
    repo: Path,
    root: Path,
    meta: dict[str, JSONAny],
    config: RunnerConfig | None,
    cache: Path | None,
) -> Path:
    """Fill a prepared bundle root with the task's start tree, overlay and start commit.

    Split from `write_local_suite` so one `except` there owns removing a tree that never shipped,
    rather than every failure path remembering to.

    Returns:
        The committed, scoreable task start tree.

    Raises:
        OrderError: If a declared offline artifact is absent, or the task cannot be prepared and
            scored in its worker image.
    """
    _export_into(repo, commit_or_refuse(meta["parent_commit"]), root, _EXPORT_SECONDS)
    for path in root.rglob("*"):
        os.utime(path, None, follow_symlinks=False)
    git = ["git", "-c", "user.email=mo-eval@example.invalid", "-c", "user.name=mo-eval"]
    # Initialized BEFORE the scaffold is applied: inside a customer's checkout, `git apply` would
    # otherwise resolve the patch against the enclosing repository and silently apply nothing.
    _bounded([*git, "init", "-q"], cwd=root)
    _write_without_following(root / ".mo-eval-scaffold.patch", package.files["scaffold.patch"])
    _bounded(
        ["git", "apply", "--whitespace=nowarn", ".mo-eval-scaffold.patch"], cwd=root, timeout=_APPLY_SECONDS
    )
    (root / ".mo-eval-scaffold.patch").unlink()
    _prepare_offline(root, meta, config=config, cache=cache)
    _refuse_unbundlable_links(root)
    _break_hard_links(root)
    _write_all(package.local_suite, root, "local suite")
    _bounded([*git, "add", "-A"], cwd=root)
    _stage_what_the_commit_carried(repo, commit_or_refuse(meta["parent_commit"]), root, git)
    for artifact in _offline_artifacts(meta, config):
        _require_within(root, artifact)
        if not (root / artifact).exists():
            # Declared and absent means the preparation did not produce what it promised — a typo, a
            # different path, a partial failure. Skipping it silently freezes a tree WITHOUT the
            # dependencies and ships a bundle that fails in a worker, which is the whole failure this
            # declaration exists to prevent.
            #
            # A language default is exempt, and the condition it must meet is narrower than
            # "it describes a typical repository rather than this one" — that says why absence is
            # POSSIBLE, not why absence is SAFE. What the skip needs is: for this language, a missing
            # artifact means THERE WAS NOTHING TO VENDOR, never that the dependencies went somewhere
            # else. Both defaults meet it, measured: `go mod vendor` and `cargo vendor` each write no
            # `vendor/` for a project with no dependencies. Yarn's Plug'n'Play is the shape that does
            # not — it writes no `node_modules` and resolves from a GLOBAL cache, so absence there
            # would mean the dependencies are outside the tree entirely, and this skip would freeze a
            # bundle no worker could score. That is why TypeScript ships no default (#4269).
            if config is not None and config.offline_prepare:
                raise OrderError(
                    f"`offline_prepare` did not produce the declared artifact {artifact!r}; the task's "
                    f"tree would be frozen without the dependencies a worker cannot fetch"
                )
            continue
        # Forced past .gitignore: the frozen snapshot takes tracked files, and an ignored
        # vendor/ would vanish between this tree and the worker that scores it.
        _bounded([*git, "add", "-f", artifact], cwd=root)
    _bounded(
        [*git, "commit", "-q", "--no-verify", "-m", f"mo-eval start state for {package.task_id}"], cwd=root
    )
    # AFTER the commit, deliberately: running a scorer leaves build artifacts, and the `add -A`
    # above would have frozen them into the start tree — the same defect #4128 fixed for capture.
    # The tree is restored to the committed state afterwards, so what ships is what was committed.
    #
    _refuse_a_task_its_own_image_cannot_score(root, meta, config)
    return root


_SCORE_CHECK_SECONDS = 1800
"""How long the packaging score-check may take.

A scorer compiles before it runs — `cargo test` on a cold target directory is minutes — and this is
the same work a worker does, so the bound is the worker's order of magnitude rather than a probe's.
"""

_SCORE_CHECK_TAIL_BYTES = 1200
"""How much of a refused scorer's output is quoted back."""

_TASK_SHAPED_EXIT = 1
"""What the generated scorer exits when the named test ran and did not pass — the only answer that
earns a bundle its place. `0` means the start state already passes and `3` means the test never ran;
both are refusals, and the third is the one this check exists for."""


class _ScoreCommand(Protocol):
    """Run a scorer, returning its exit code, output tail, and optional proof markers."""

    def __call__(
        self, argv: list[str], *, cwd: Path, timeout: float, env: dict[str, str] | None
    ) -> tuple[int, str, bool | None, bool | None]: ...


class _PrepareCommand(Protocol):
    """Run a preparation command, raising OrderError when it fails."""

    def __call__(self, argv: list[str], *, cwd: Path, timeout: float, env: dict[str, str] | None) -> None: ...


def _stage_what_the_commit_carried(repo: Path, commit: str, root: Path, git: list[str]) -> None:
    """Stage the exported commit's own files, past any ignore rule that would drop them.

    `git add -A` above re-decides what belongs from scratch, in a repository initialized moments
    ago where every file is untracked — so a `.gitignore` inside the exported tree applies to files
    that were tracked in the commit it came from. Git itself never does this: an ignore rule has no
    authority over a tracked file, which is why the file is in the commit at all.

    Unstaged, the file survives just long enough to be misleading: the score check runs against the
    working tree and passes, and `_restore_to_the_commit`'s `git clean -qfdx` then deletes it, so
    the bundle ships without it. Measured on valkey, whose vendored jemalloc carries a `.gitignore`
    excluding the generated `configure` that valkey commits anyway — five files vanished, the worker
    answered `./configure: not found`, no C task from the repository could be scored, and every one
    of them had validated.

    Scoped to what the commit carried rather than a blanket `add -f`: everything else in the tree is
    the preparation's output, and the ignore rules are how a vendoring step's incidental build
    artifacts stay out of the bundle. What a repository declares in `offline_artifacts` is forced in
    separately, for that reason.

    Three details are load-bearing, each measured rather than reasoned:

    - **`:(literal)`**. A pathspec is a glob, and `--` disables option parsing, not pathspec magic.
      A committed `pages/[id].tsx` — the ordinary spelling of a dynamic route, so this is common in
      JavaScript repositories — is a bracket expression that also matches a neighbouring `i.tsx`,
      and force-adds it past the very ignore rule that was keeping it out.
    - **Raw bytes**. A path on Linux is bytes, not text, and `text=True` decodes them as strict
      UTF-8; one `caf\xe9.txt` in a commit raises `UnicodeDecodeError`, which is a `ValueError`, so
      packaging reports "bundle was not created" for EVERY task of that repository.
    - **Not a directory**. `git add -f` on a directory recurses past every ignore rule beneath it.
      The path is a blob in the commit, so a directory there means the preparation replaced it —
      and a submodule's gitlink exports as an empty directory that `ls-tree -r` names.

    Raises:
        OrderError: If the commit's file list cannot be read, or the paths cannot be staged.
    """
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", commit],
        cwd=repo,
        capture_output=True,
        timeout=_GIT_SECONDS,
    )
    if listed.returncode != 0:
        detail = listed.stderr.decode("utf-8", "replace").strip()
        raise OrderError(f"could not list the files of {commit}: {detail}")
    carried = []
    for raw in listed.stdout.split(b"\0"):
        if not raw:
            continue
        where = root / os.fsdecode(raw)
        # `lexists`, so a symlink is judged as itself. A path the scaffold deleted is simply absent,
        # and not an error: the commit says what EXISTED, not what survived the scaffold.
        if not os.path.lexists(where) or (where.is_dir() and not where.is_symlink()):
            continue
        carried.append(b":(literal)" + raw)
    if not carried:
        return
    # Through stdin rather than argv: the list is a whole repository's worth of paths, and this has
    # no length to exceed and so needs no batching to get wrong.
    staged = subprocess.run(
        [*git, "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul"],
        cwd=root,
        input=b"\0".join(carried) + b"\0",
        capture_output=True,
        timeout=_GIT_SECONDS,
    )
    if staged.returncode != 0:
        detail = staged.stderr.decode("utf-8", "replace").strip()
        raise OrderError(f"could not stage the files {commit} carried: {detail}")


def resolved_worker_platform(config: RunnerConfig | None) -> WorkerPlatform:
    """Return a configured worker platform or the compatibility default.

    The optional configuration belongs to generated task metadata that predates platform
    declarations. Both runner Docker paths use this one resolution so they cannot silently choose
    different architectures for the same bundle.

    Args:
        config: Repository configuration when the bundle was created from a configured checkout.

    Returns:
        The declared platform, or Linux x86-64 for a legacy missing configuration.
    """
    if config is None:
        return DEFAULT_WORKER_PLATFORM
    return config.worker_platform


def _refuse_a_task_its_own_image_cannot_score(
    root: Path, meta: dict[str, JSONAny], config: RunnerConfig | None, run: _ScoreCommand | None = None
) -> None:
    """Run this task's own scorer in its own worker image, and refuse a bundle that cannot be graded.

    Validation ran the probes on THIS host. Scoring runs the generated scorer inside
    `worker_image`, against a frozen tree, with no network. Nothing compared the two until here, and
    a task can pass the first and be ungradeable in the second — measured on `sqlglot`, whose image
    had no pytest, where setup exited zero and the scorer reported "did not pass" for a test that
    could never run. Counted, and counted wrong.

    The scorer is already in the tree and carries its own patterns, so this needs no contract
    reading: the exit status alone separates the three answers. `1` is the task-shaped failure a
    start state owes; `3` is the scorer saying nothing ran, which is the environment rather than the
    task (#4139); `0` is a start state that already passes, which is no task at all.

    Runs with `--network none`, as a worker does. With one, an install could fetch what a worker
    cannot and the check would pass under an environment that will not exist at scoring time.

    Raises:
        OrderError: If the image cannot score the task, or the check cannot be carried out at all.
    """
    # `_bounded_output`, not `_bounded`: the scorer's exit code IS the answer here, and `_bounded`
    # raises on any non-zero — including `1`, the one status that means the bundle is good.
    run = run or _bounded_output
    image = config.worker_image if config is not None else None
    if not image:
        # Nothing to disagree with: a repository that declares no image is scored wherever a worker
        # happens to run, and this check has no second environment to compare against.
        #
        # Returning BEFORE the restore below is what makes the restore this function's own and not
        # the caller's: `git clean -qfdx` prunes the bundle, and on this path nothing would look at
        # what it pruned. Hoisted into the caller it silently dropped a scaffolded test matching an
        # ignore rule, and the `local-suite` overlay under a repository ignoring `.mo-eval/`.
        return
    if shutil.which("docker") is None:
        # The same refusal `_prepare_offline` makes, for the same reason: skipping the comparison
        # silently is what leaves a bundle that looks complete and cannot be scored.
        raise OrderError(
            "this repository declares a worker image, but there is no `docker` on this runner to "
            "score the task in it before shipping — a bundle no worker can grade would look complete"
        )
    contract = _contract_for(meta)
    setup = meta.get("setup_cmd")
    steps: list[str] = []
    if setup:
        if contract is None:
            raise OrderError("cannot prepare offline scoring for an unknown language contract")
        steps.append(offline_setup(setup, contract))
    steps.append(f"bash {shlex.quote(SCORER_IN_TREE)}")
    # Restored BEFORE the check as well as after, so it reads the tree that SHIPS rather than the
    # one that happens to be on disk. The two differ by whatever the export and the preparation left
    # unstaged, and the difference is not academic: valkey's `deps/jemalloc/configure` was present
    # while the check ran — so it passed — and removed by the restore afterwards, so every worker
    # then answered `./configure: not found`. A guard that reads a richer tree than it blesses
    # cannot see the failure it exists for.
    #
    # Here rather than at the top of this function: everything above refuses on what the task
    # DECLARES, and those refusals must read the same whether or not `root` is a git tree at all.
    # Restoring first turned an unknown-contract refusal into `not a git repository`.
    _restore_to_the_commit(root)
    container = f"mo-eval-score-check-{os.getpid()}-{root.name}"
    try:
        exit_code, tail, _, _ = run(
            [
                "docker",
                "run",
                "--platform",
                resolved_worker_platform(config).value,
                "--rm",
                "--name",
                container,
                # As a worker scores: with none. Given a network, a setup could fetch what a worker
                # cannot and the check would pass under an environment that will not exist later.
                "--network",
                "none",
                "--pids-limit",
                str(_OFFLINE_PREPARE_MAX_PIDS),
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-e",
                f"HOME={_PREPARE_HOME}",
                "-v",
                f"{root}:/mo-eval-tree",
                "-w",
                "/mo-eval-tree",
                "--entrypoint",
                "sh",
                image,
                "-c",
                "; ".join(steps),
            ],
            cwd=root,
            timeout=_SCORE_CHECK_SECONDS,
            env=_without_the_runners_credentials(os.environ, {}),
        )
    except BaseException:
        # Killing the CLI does not stop a container — it is the daemon's child, and `--rm` fires
        # only once it exits. Bounded like the preparation's kill: one that hangs would hold the CI
        # job open, which is what this is preventing.
        subprocess.run(["docker", "kill", container], capture_output=True, timeout=_GIT_SECONDS, check=False)
        _restore_to_the_commit(root, best_effort=True)
        raise
    _restore_to_the_commit(root)
    if exit_code == _TASK_SHAPED_EXIT:
        return
    # The scorer's own words, bounded: a refusal a person cannot act on is barely better than none.
    tail = (tail or "")[-_SCORE_CHECK_TAIL_BYTES:]
    if exit_code == 0:
        raise OrderError(
            f"{meta['task_id']}: the start state PASSES its own scorer inside {image} — there is no "
            f"task here to solve, whatever the probes on this host reported\n{tail}"
        )
    raise OrderError(
        f"{meta['task_id']}: this task's scorer exited {exit_code} inside {image}, not "
        f"{_TASK_SHAPED_EXIT} — the image cannot grade the task, so no worker could either. A bundle "
        f"shipped now would report every attempt as a failed task, including a correct one\n{tail}"
    )


def _restore_to_the_commit(root: Path, *, best_effort: bool = False) -> None:
    """Put the tree back to the start state that was committed, discarding what the scorer built.

    A scorer compiles and caches, and none of that is part of a task's start state. Restored to the
    commit rather than cleaned selectively, so a toolchain that writes somewhere unexpected cannot
    ship inside the bundle.

    `best_effort` while an exception is already in flight: raising from here would replace a refusal
    with a git error, so the reason a bundle was rejected would be the cleanup rather than the task.
    """
    git = ["git", "-c", "user.email=mo-eval@example.invalid", "-c", "user.name=mo-eval"]
    for argv in ([*git, "reset", "-q", "--hard"], [*git, "clean", "-qfdx"]):
        try:
            _bounded(argv, cwd=root, timeout=_GIT_SECONDS)
        except Exception:
            if not best_effort:
                raise
            return


def _discard_the_preparation_cache(cache: Path) -> None:
    """Remove the suite's shared cache once every bundle is built.

    Disk is the limit that actually binds a suite (#4277), and the cache is the one thing this
    change ADDS to it — 189 MB for `gin`, whose closure is small. The peak is not here but in
    hand-off, which holds every tree AND every archive at once, so the cache is discarded before
    that and the saving on downloads costs no headroom at all.

    A Go module cache is deliberately read-only, and it is the DIRECTORIES that block removal: a
    file is unlinked from its parent, so 0444 on the file costs nothing and 0555 on the directory
    holding it refuses every unlink inside it. Restoring write permission as errors arrive does not
    work for that reason — the path that raises is the file, and the path that must change is its
    parent. So the directories are made writable first, deliberately, and then the tree goes.

    Best effort: a cache that will not delete is not worth failing a suite over, since the job's
    temporary directory goes when the job does.
    """
    for directory, _, _ in os.walk(cache):
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
    shutil.rmtree(cache, ignore_errors=True)


def _cache_environment(cache: Path, contract: Language | None) -> tuple[str, ...]:
    """Docker arguments mounting the suite's shared preparation cache and pointing a toolchain at it.

    The mount alone shares only what a toolchain keeps under HOME, which for an official image is
    usually not the download. `prepare_cache_env` names the variable that moves the rest.

    Returns:
        Docker mount and environment arguments directing the toolchain to the shared preparation
        cache.
    """
    arguments = ["-v", f"{cache}:{_PREPARE_HOME}"]
    for name, relative in sorted((contract.prepare_cache_env if contract is not None else {}).items()):
        arguments += ["-e", f"{name}={PurePosixPath(_PREPARE_HOME) / relative}"]
    return tuple(arguments)


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


def _resolved_offline_prepare(meta: dict[str, JSONAny], config: RunnerConfig | None) -> str | None:
    """The preparation that will actually run: the repository's own, else the language's default.

    One resolution, because two callers ask this — the gate that refuses an unscorable bundle, and
    the step that runs it — and two spellings of the same question drift. The drift direction is the
    dangerous one: a gate that sees LESS than the preparation passes a bundle the preparation then
    does nothing for.

    Returns:
        The declared preparation command, otherwise the language default, or `None` when neither
        exists.
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


def _break_hard_links(root: Path) -> None:
    """Give every path in the start tree its own inode.

    `tarfile` writes the FIRST path to an inode as a regular file and every later one as a hard-link
    member, and the lane's allow-list refuses that member type. So a tree where two paths share an
    inode produces an archive the lane refuses, at the same late moment a symlink once did.

    Symlinks are the opposite case and are KEPT: the lane admits an in-tree relative one, and a link
    is semantics rather than a second name for the same bytes, so copying its target is lossy.
    Breaking a hard link is not — both paths keep the content they shared.

    Measured on `momentohq/hono`: one pair in 33,249 files, `node_modules/workerd/bin/workerd`, which
    bun installs by hard-linking from its own cache — and 131 MB, because the file it shares is a
    compiled binary. Rare and heavy, which is why the copy is made only for the second path and not
    for every file with a link count above one: a file hard-linked to something OUTSIDE the tree
    appears once here and is archived as itself.

    Runs after the link check, which refuses a tree the lane could not unpack; ordering is not
    load-bearing beyond that, since nothing before this creates or removes a path.
    """
    kept: set[tuple[int, int]] = set()
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        status = path.lstat()
        if status.st_nlink < 2:
            continue
        inode = (status.st_ino, status.st_dev)
        if inode not in kept:
            # The first path to this inode is the one the archive writes in full.
            kept.add(inode)
            continue
        # Copied to a neighbour and moved over the top, rather than unlinked and copied in place: an
        # unlink followed by a failed copy would leave the tree missing a file it had.
        spare = path.with_name(f"{path.name}.mo-eval-unlinked")
        shutil.copy2(path, spare)
        spare.replace(path)


def _refuse_unbundlable_links(root: Path) -> None:
    """Refuse a start tree holding a symlink the lane will not unpack.

    The links are KEPT. An earlier version copied each link's target over it, because the lane's
    allow-list admitted only regular files and directories — and that is lossy in a way that is easy
    to miss: a JavaScript bin shim is a link whose content is a relative import, and the import only
    resolves through the link. `node_modules/.bin/vitest` holds `import './dist/cli.js'`, which
    reaches `node_modules/vitest/dist/cli.js` through the link and `node_modules/.bin/dist/cli.js`
    once flattened. Every `.bin` entry broke that way, so no JavaScript repository could be scored;
    pnpm and workspace layouts, which are links all the way down, were worse.

    What remains here is the check, not the copy: a link the lane would refuse is refused now, while
    the funnel can still say which task and why, rather than four seconds into a lane as a tar flag.

    The predicate is `lane/dispatch.py::_link_stays_inside`, restated rather than imported — the lane
    runs the system python as a standalone copy and cannot import this package. `tests/
    test_bundle_symlinks.py` asserts both answer alike over one table of cases, so the two cannot
    drift apart silently.

    Raises:
        OrderError: If a link is absolute, leaves the tree, or points at nothing.
    """
    root = root.resolve()
    # Iterated, not materialized: the version that copied links had to sort them, because resolving
    # one rewrote the tree underneath the walk. Nothing is rewritten now, so order does not matter —
    # and sorting would hold every path in a `node_modules` in memory at once to no purpose.
    for link in root.rglob("*"):
        if not link.is_symlink():
            continue
        target = os.readlink(link)
        relative = link.relative_to(root)
        # `!r` on the target, as the lane does with the same value: it is the mined repository's
        # bytes, and an unquoted one puts whatever it contains into a log verbatim.
        if not link_stays_inside(relative.as_posix(), target):
            raise OrderError(
                f"{relative} points outside the task's tree ({target!r}); a worker has only the "
                f"tree, so there is nothing there to follow"
            )
        if not link.exists():
            # Inside the tree and still unfollowable. A bundle carrying it packages and unpacks, and
            # the failure lands on whatever reads through it — in a worker, with no network to fetch
            # what is missing.
            raise OrderError(
                f"{relative} is a link to nothing a worker could follow ({target!r}); "
                f"the task cannot be scored"
            )


def link_stays_inside(name: str, target: str) -> bool:
    """Whether a symlink at `name` pointing at `target` lands inside the tree that holds it.

    Text only, never the filesystem: this must answer the same question the lane answers about an
    archive it has not yet unpacked, and a check that followed real paths would answer about the
    machine it ran on instead. Normalizing rather than refusing every `..` is what keeps a real
    `node_modules` working — a bin shim is `../pkg/cli.js`, and the climb is what makes it correct.

    Args:
        name: The link's own path, relative to the tree root, in POSIX form.
        target: The link's target, exactly as stored.

    Returns:
        True when the target lands inside the tree.
    """
    if not target or PurePosixPath(target).is_absolute():
        return False
    landing = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    return landing != ".." and not landing.startswith("../")


def _refuse_a_bundle_no_worker_could_score(meta: dict[str, JSONAny], config: RunnerConfig | None) -> None:
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
    why = (
        "its tests resolve dependencies as they run"
        if fetches_when_testing
        else "its test runner needs an installed dependency tree"
        if needs_an_installed_tree
        else "it declares a `setup_command`"
    )
    raise OrderError(
        f"{why}, and nothing puts those dependencies into the task's tree — a worker scores with no "
        f"network. Declare `offline_prepare` and `offline_artifacts` in {CONFIG_PATH}, or the bundle "
        f"cannot be scored"
    )


def _prepare_offline(
    root: Path,
    meta: dict[str, JSONAny],
    run: _PrepareCommand | None = None,
    config: RunnerConfig | None = None,
    cache: Path | None = None,
) -> None:
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

    `cache` is a directory mounted as the container's HOME so the toolchain cache survives between
    tasks. Every task of a suite resolves the same dependency closure, and without it each one
    re-downloads the whole thing: measured on `gin`, 7s cold against 1s warm, and gin's closure is
    a small one. Scoped to the suite's own output directory and so to the CI job, deliberately —
    the redundancy is entirely within one invocation, and a cache outliving the job on a
    self-hosted runner would be reachable by a second repository.

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
        # fails offline much later. Prepared where it will be consumed, it is right by construction on
        # every axis the worker has: operating system and interpreter from the image, and CPU
        # architecture from `--platform`, which would otherwise follow this host on a multi-arch image.
        #
        # With a network, unlike scoring: this is the step whose whole job is fetching what scoring
        # will not be able to.
        # Named, so it can be stopped by name. Ending the deadline kills the `docker` CLI and the
        # process group it sits in, which is enough for every other command this runner runs — but a
        # container is a child of the daemon, not of the CLI. Kill the CLI alone and the container
        # keeps running, and `--rm` never fires because it removes a container only once it exits.
        container = f"mo-eval-prepare-{os.getpid()}-{root.name}"
        try:
            run(
                [
                    "docker",
                    "run",
                    "--platform",
                    resolved_worker_platform(config).value,
                    "--rm",
                    "--name",
                    container,
                    # A deadline bounds how LONG a preparation runs, not how fast it consumes. A fork
                    # bomb exhausts the host's process table long before the deadline fires, and killing
                    # the container afterwards cannot give back what was already taken. Far above any
                    # real vendoring — `pip wheel`, `cargo vendor` and `bun install` spawn tens of
                    # processes, not thousands — so it costs a legitimate repository nothing.
                    "--pids-limit",
                    str(_OFFLINE_PREPARE_MAX_PIDS),
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
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "-e",
                    f"HOME={_PREPARE_HOME}",
                    # Shared with the other tasks of this suite, so the closure is fetched once rather
                    # than once per task. Mounted only here: the score check below must keep starting
                    # cold, for the same reason it runs with `--network none`.
                    #
                    # The mount is not enough on its own. An official image sets the toolchain's own
                    # cache variable, and then the cache lands outside HOME however HOME is set —
                    # `golang:1-bookworm` ships `GOPATH=/go`, so the module cache ignored a shared HOME
                    # entirely and two tasks still fetched everything twice. The contract names what to
                    # point back, per language and measured.
                    *(_cache_environment(cache, contract) if cache is not None else ()),
                    "-v",
                    f"{root}:/mo-eval-tree",
                    "-w",
                    "/mo-eval-tree",
                    "--entrypoint",
                    "sh",
                    image,
                    "-c",
                    prepare,
                ],
                cwd=root,
                timeout=_OFFLINE_PREPARE_SECONDS,
                env=_without_the_runners_credentials(os.environ, {}),
            )
        except Exception:
            # Best effort and deliberately silent: the failure being reported is the preparation's,
            # and a cleanup that raises would replace it with one about cleaning up.
            subprocess.run(
                ["docker", "kill", container], capture_output=True, timeout=_GIT_SECONDS, check=False
            )
            raise
        return
    # Through a shell, the same as the in-image path above: vendoring is often a pipeline —
    # `cargo vendor >> .cargo/config.toml` writes the configuration that makes the vendor directory
    # take effect — and a declared command must mean one thing wherever it runs.
    run(
        ["sh", "-c", prepare],
        cwd=root,
        timeout=_OFFLINE_PREPARE_SECONDS,
        env=_without_the_runners_credentials(os.environ, {}),
    )


def _contract_for(meta: dict[str, JSONAny]) -> Language | None:
    """The contract a package was generated under, including a detected variant such as `go+ginkgo`,
    which `LANGUAGES` alone does not name.

    Returns:
        The package's language or detected framework contract, or `None` for an unknown name.
    """
    try:
        return resolve_contract(meta.get("generated", {}).get("language", ""))
    except KeyError:
        return None


def _offline_artifacts(meta: dict[str, JSONAny], config: RunnerConfig | None = None) -> tuple[str, ...]:
    """What the offline preparation leaves behind that must be tracked — the repository's own when
    it declared a preparation, the language's otherwise. Read from whichever declared the command,
    because artifacts that do not belong to the command that ran are artifacts that do not exist.

    Returns:
        Artifact paths belonging to the selected preparation command, or an empty tuple when no
        contract is known.
    """
    if config is not None and config.offline_prepare:
        return config.offline_artifacts
    contract = _contract_for(meta)
    return contract.offline_artifacts if contract is not None else ()


def _repo_name(repo: Path, declared: str | None) -> str:
    if declared:
        return declared
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=repo, capture_output=True, text=True, timeout=_GIT_SECONDS
    ).stdout.strip()
    tail = url.removesuffix(".git").replace(":", "/").rstrip("/")
    parts = tail.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else tail


def _branch(repo: Path, repo_name: str) -> str:
    """The branch merged pull requests target — the repository's DEFAULT branch, not whatever is
    checked out.

    A CI job checks out the pushed branch, and a developer often has a feature branch out; listing
    merged PRs against either returns nothing, silently.

    Returns:
        The forge's default branch, falling back to origin's default, the checked-out branch,
        then `main`.
    """
    # The forge first, and `origin/HEAD` after it. `repo_name` is what the config declares, which on
    # a fork is the UPSTREAM — and merged pull requests are listed against that repository. A fork
    # whose own default differs would otherwise set `--base` to its branch and list nothing, which
    # reads as an upstream with no merged work.
    forge = subprocess.run(
        [
            "gh",
            "repo",
            "view",
            "-R",
            repo_name,
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=_FORGE_SECONDS,
    ).stdout.strip()
    if forge:
        return forge
    # Then the clone's own default, then whatever is checked out.
    head = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=_GIT_SECONDS,
    ).stdout.strip()
    if head:
        return head.split("/", 1)[-1]
    return (
        subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=_GIT_SECONDS,
        ).stdout.strip()
        or "main"
    )


def _contract_name(repo: Path, language: str, test_command: str = "") -> str:
    """Which framework this repository actually uses, measured from its test files and from the
    command it declared it runs them with.

    Returns:
        The framework contract name detected from tracked tests and the declared test command.
    """
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

    Returns:
        Unique offered change IDs in service selection order, within the candidate limit.
    """
    offered = {change.change_id for change in facts.changes}
    unknown = [change_id for change_id in change_ids if change_id not in offered]
    if unknown:
        raise ServiceError(f"the service selected {len(unknown)} change(s) this repository did not offer")
    unique = list(dict.fromkeys(change_ids))
    if len(unique) > candidates:
        raise ServiceError(f"the service selected {len(unique)} changes for {candidates} candidate(s)")
    return unique


def verdicts_asked_for(report: VerdictReport, orders: int) -> VerdictReport:
    """The verdicts the service returned, confirmed to be one per order this runner actually ran.

    Each verdict with a task attached costs a package on disk, a tree export, a vendoring step and
    an archive held open until it uploads. Unbounded, a service can spend a CI runner's whole disk
    answering a request for eight.

    Raises:
        ServiceError: If the answer carries more verdicts than there were orders.

    Returns:
        The unchanged report after checking its verdict count against the number of executed
        orders.
    """
    if len(report.verdicts) > orders:
        raise ServiceError(f"the service returned {len(report.verdicts)} verdicts for {orders} order(s)")
    return report


def orders_asked_for(orders: list[WorkOrder], candidates: int) -> list[WorkOrder]:
    """The orders the service composed, confirmed to be no more work than was requested.

    Raises:
        ServiceError: If the answer carries more orders than candidates were asked for.

    Returns:
        The unchanged order list after enforcing the requested candidate limit.
    """
    if len(orders) > candidates:
        raise ServiceError(f"the service returned {len(orders)} orders for {candidates} candidate(s)")
    return orders


_MAX_MERGED_REASONS = 200
"""How many distinct rejection reasons one merged tally keeps.

The service composes these strings and the funnel prints the top few. Merging several responses is
what makes the bound worth having: without it a service grows the runner's tally by a whole
response's worth of distinct reasons per request, and packing decides how many requests there are.
"""

_MAX_HELD_ANSWER_CHARS = 256 * 1024 * 1024
"""How much service-composed text the runner holds while assembling several answers into one.

A count is not a size. `_MAX_MERGED_REASONS` bounds how many reasons are kept and
`orders_asked_for` bounds how many orders arrive, and a service fills either bound with maximally
large entries: `wire.MAX_STRING_CHARS` lets one string be 16 MiB, so two hundred of them is
gigabytes inside both counts.

Set to what one response could already hold (`client._MAX_RESPONSE_BYTES`), because that is the
bound this stopped being: while there was one answer, the per-answer ceiling bounded what was held
at once. Packing makes the number of answers follow the size of a repository's changes, so the same
ceiling has to be stated across them.
"""


def _held_chars(answer: OrdersResponse) -> int:
    """How much service-composed text one answer keeps alive.

    Counted field by field rather than by re-encoding the answer, which would allocate exactly the
    megabytes the bound exists to refuse.

    Returns:
        The total characters retained in rejection reasons, order IDs, step text, and probe
        arguments.
    """
    total = sum(len(summary.reason) for summary in answer.rejections)
    for order in answer.orders:
        total += len(order.order_id)
        for step in order.steps:
            total += sum(
                len(text)
                for text in (
                    step.op,
                    step.step_id,
                    step.commit or "",
                    step.patch or "",
                    step.cwd or "",
                    step.proof or "",
                    step.counterproof or "",
                )
            )
            total += sum(len(argument) for argument in step.args or ())
    return total


TOO_LARGE_TO_SEND = "source is larger than one request to the service carries"
"""Why a selected change was never asked about. A stable category with no size in it, so the funnel
tallies every such change into one row rather than one row each."""


def issue_orders(
    client: ServiceClient, wire: Wire, facts: RepoFacts, sources: list[ChangeSource], candidates: int
) -> OrdersResponse:
    """Ask the service for work orders and merge the answers into one response.

    In as many requests as `plan_order_requests` says the selection needs, tallied so the funnel
    reads them as it would have read one.

    Each request is recorded as its own crossing, because it is one: `wire/` holds what was sent,
    not what was assembled. A change no request can carry is named here instead of being sent.

    Raises:
        ServiceError: If the selection cannot be packed into any request, or a request fails.

    Returns:
        Combined work orders and rejection counts, including changes too large to send.
    """
    _LOGGER.info("Creating validation orders from %d selected change(s)", len(sources))
    planned = plan_order_requests(facts, sources, candidates)
    for change_id, cost in planned.oversized.items():
        _LOGGER.warning(
            "%s was not asked about: its source costs %d bytes, and one request carries %d",
            change_id[:9],
            cost,
            REQUEST_BUDGET_BYTES,
        )
    orders: list[WorkOrder] = []
    counts: Counter[str] = Counter()
    if planned.oversized:
        counts[TOO_LARGE_TO_SEND] = len(planned.oversized)
    held = 0
    for batch in planned.batches:
        wire.crossing("up", "change-source", batch)
        answer = wire.crossing("down", "work-orders", client.orders(facts, batch, candidates))
        orders.extend(answer.orders)
        # Both checked per request rather than only on the whole, because the whole is now assembled
        # from several answers: a service that overruns stops at the first one instead of after the
        # runner has held every batch's worth of it.
        orders_asked_for(orders, candidates)
        held += _held_chars(answer)
        if held > _MAX_HELD_ANSWER_CHARS:
            raise ServiceError(
                f"the service's answers hold {held:,} characters across "
                f"{len(orders)} order(s); at most {_MAX_HELD_ANSWER_CHARS:,}"
            )
        for summary in answer.rejections:
            if summary.reason in counts or len(counts) < _MAX_MERGED_REASONS:
                counts[summary.reason] += summary.count
    return OrdersResponse(
        orders=orders, rejections=[RejectionSummary(reason, count) for reason, count in counts.most_common()]
    )


def suite_exit(*, written: int, shipped: int, asked_to_run: bool) -> int:
    """What a finished `suite` run is worth as an exit code.

    A task is written BEFORE its containerized bundle is built, so `written` counts tasks that
    exist on disk — not tasks that are running. A suite whose every bundle failed therefore has a
    non-empty `written` and shipped nothing, and reporting success on that is the "validated N of N
    while shipping nothing" this module already warns about.

    Args:
        written: Tasks written to `--out`.
        shipped: Bundles the hand-off actually handed to the service; zero when none was asked for.
        asked_to_run: Whether `--run` named any route. Without it there is nothing to ship and
            `written` alone is the outcome.

    Returns:
        Zero when the run produced what it was asked for, three when it produced nothing runnable —
        the same three `submit` uses for a tree that handed off nothing.
    """
    if not written:
        return 3
    if asked_to_run and not shipped:
        return 3
    return 0


def client_version(value: str) -> str | None:
    """Parse a `--mo-version` / `--claude-code-version` / `--codex-version` value.

    Checked as the arguments are parsed, so a malformed one costs nothing: the service would refuse
    it too, but only at `/v1/runs`, after the suite was mined and its bundles uploaded.

    Args:
        value: The raw flag value. Empty is how the Action passes an input left unset.

    Returns:
        The version, or `None` for empty, which stages the lane's pin.

    Raises:
        argparse.ArgumentTypeError: If the value is not a release version.
    """
    if not value:
        return None
    if not CLIENT_VERSION_PATTERN.match(value):
        raise argparse.ArgumentTypeError(f"expected a release version like 1.2.3, got {value!r}")
    return value


def requested_client_versions(arguments: argparse.Namespace) -> ClientVersions | None:
    """The releases a run asks the lane to stage in place of its pins.

    Args:
        arguments: Parsed `suite` or `submit` arguments.

    Returns:
        The named versions, or `None` when none is named, so the request carries no key.

    Raises:
        ValueError: If a Claude Code or Codex release is named but no `cc` or `codex` harness would
            run it — refused here, before the suite is mined, rather than by the service afterwards.
    """
    versions = ClientVersions(
        mo=arguments.mo_version, claude_code=arguments.claude_code_version, codex=arguments.codex_version
    )
    if versions == ClientVersions():
        return None
    harnesses = arguments.harnesses or []
    if versions.claude_code is not None and "cc" not in harnesses:
        raise ValueError(f"Claude Code {versions.claude_code} is named, but no cc harness runs it")
    if versions.codex is not None and "codex" not in harnesses:
        raise ValueError(f"Codex {versions.codex} is named, but no codex harness runs it")
    return versions


def suite(arguments: argparse.Namespace) -> int:
    repo = arguments.repo.resolve()
    out = (arguments.out if arguments.out is not None else repo / ".mo-eval" / "out").resolve()
    _LOGGER.debug("Resolving repository and configuration from %s", repo)
    # Before anything can fail: `--out` may be reused, and a funnel record left by the previous
    # run would tell the Action's summary step that this run reported one.
    (out / FUNNEL_RECORD).unlink(missing_ok=True)
    try:
        client_versions = requested_client_versions(arguments)
    except ValueError as failure:
        _LOGGER.error("Configuration failed: %s", failure)
        return 2
    try:
        config = load_config(repo / arguments.config)
    except ConfigError as failure:
        _LOGGER.error("Configuration failed: %s", failure)
        return 2
    environment, missing = probe_environment(config, os.environ)
    if missing:
        _LOGGER.warning(
            "Configuration names missing environment variables %s; probes needing them will fail identically",
            missing,
        )
    try:
        client = client_for(arguments.service, arguments.token or os.environ.get("MO_EVAL_TOKEN"))
    except ServiceError as failure:
        _LOGGER.error("Service setup failed: %s", failure)
        return 2

    # Before anything is written into it. `_package_each_verdict` creates `tasks/` and
    # `local-suite/` well before a hand-off could look at them, so a PRE-EXISTING link at either
    # door would put this run's output outside `--out` and the refusal would come too late to
    # matter.
    try:
        refuse_a_linked_tree(out)
    except OrderError as failure:
        _LOGGER.error("Configuration failed: %s", failure)
        return 2
    wire = Wire(root=out / "wire")
    contract = _contract_name(repo, config.language, config.test_command)
    name = _repo_name(repo, config.repo)
    _LOGGER.info("mo-eval-runner · %s · %s", name, contract)

    branch = arguments.branch or _branch(repo, name)
    _LOGGER.info("Collecting merged changes from %s", branch)
    facts = repo_facts(
        repo,
        repo_name=name,
        language=contract,
        test_command=config.test_command,
        branch=branch,
        limit=arguments.history,
        language_contract=LANGUAGES[config.language],
        setup_command=config.setup_command,
    )
    _LOGGER.debug("Collected %d merged change(s) via %s", len(facts.changes), facts.source)
    # Recorded after the declaration is folded in, so the audit holds what was sent rather than an
    # earlier version of it.
    facts = wire.crossing(
        "up",
        "repo-facts",
        replace(
            facts,
            worker_image=config.worker_image,
        ),
    )
    try:
        _LOGGER.info("Selecting up to %d candidate change(s)", arguments.candidates)
        request = wire.crossing("down", "source-request", client.select(facts, arguments.candidates))
        request = replace(request, change_ids=asked_for(request.change_ids, facts, arguments.candidates))
        _LOGGER.debug("Selection retained %d change(s)", len(request.change_ids))
        sources = change_source(repo, facts, request.change_ids, resolve_contract(contract))
        response = issue_orders(client, wire, facts, sources, arguments.candidates)
    except ServiceError as failure:
        _LOGGER.error("Selection or order creation failed: %s", failure)
        return 1

    orders = response.orders[: arguments.validate] if arguments.validate else response.orders
    if arguments.dry_run:
        _LOGGER.info("Dry run: %d order(s) would be validated", len(orders))
        _funnel(
            facts,
            request,
            response,
            len(orders),
            VerdictReport([]),
            [],
            dry_run=True,
            record=out / FUNNEL_RECORD,
        )
        _report_written_outputs(out)
        return 0
    runner = Runner(
        repo=repo,
        workspaces=out / "workspaces",
        test_command=config.test_command,
        env=environment,
        setup_command=config.setup_command,
        # Which of those names carry a credential, so the runner can take their values
        # out of what crosses back. `env` alone cannot say: a build flag and a token
        # look the same once they are merged.
        secret_names=tuple(config.forward_env),
        # The shape a probe's arguments must have. Without it the service could append
        # anything the repository's test tool accepts, which is more than a filter.
        filter_template=resolve_contract(contract).filter_template,
    )
    results = []
    for index, order in enumerate(orders, start=1):
        _LOGGER.info("Validating order %d/%d: %s", index, len(orders), order.order_id)
        results.append(runner.run(order))
    wire.crossing("up", "order-results", results)
    try:
        report = verdicts_asked_for(wire.crossing("down", "verdicts", client.verdicts(results)), len(results))
    except ServiceError as failure:
        _LOGGER.error("Validation verdict request failed: %s", failure)
        return 1

    written: list[Path] = []
    # One toolchain cache for every task of this suite. A sibling of `local-suite/`, never inside a
    # bundle: `write_local_suite` force-adds the declared offline artifacts, and a cache under a
    # task's tree would be frozen into the start state and shipped to every worker.
    prepare_cache = out / ".prepare-cache"
    prepare_cache.mkdir(parents=True, exist_ok=True)
    _LOGGER.info("Packaging %d validation verdict(s)", len(report.verdicts))
    packaged, refusals = _package_each_verdict(report, repo, out, config, prepare_cache, written)
    # Before hand-off, which is where disk peaks: it holds every tree AND every archive at once.
    _discard_the_preparation_cache(prepare_cache)

    _funnel(
        facts,
        request,
        response,
        len(orders),
        report,
        written,
        evidence=_evidence(results, report),
        record=out / FUNNEL_RECORD,
        packaged=packaged,
        refusals=refusals,
    )
    shipped = 0
    if written and arguments.run:
        _LOGGER.info("Preparing hosted handoff for %d validated task(s)", len(written))
        _LOGGER.debug("Requested routes: %s", ", ".join(arguments.run))
        conventions = _conventions(repo, facts.repo, min(arguments.history, 120))
        try:
            shipped = _hand_off(
                client,
                facts.repo,
                out,
                routes=arguments.run,
                repeats=arguments.repeats,
                conventions=conventions,
                harnesses=arguments.harnesses,
                refusals=refusals,
                client_versions=client_versions,
            )
        except ServiceError as failure:
            # The validated tasks are already on disk, so this is recoverable: the same hand-off is
            # what `submit --out <dir>` does. Reported as a message and an exit code, not a traceback.
            _LOGGER.error(
                "Hosted handoff failed: %s. The validated tasks are in %s; retry with `submit --out %s`",
                failure,
                out,
                out,
            )
            _report_written_outputs(out)
            return 1
        if not shipped:
            _LOGGER.warning(
                "%d task(s) validated, but no bundle was handed off; nothing is running", len(written)
            )
    _report_written_outputs(out)
    return suite_exit(written=len(written), shipped=shipped, asked_to_run=bool(arguments.run))


def submit(arguments: argparse.Namespace) -> int:
    """Hand an already-validated `--out` tree to the service for evaluation.

    Returns:
        Zero after a hand-off that shipped at least one bundle; two for a refusal — a reported
        service error, or a tree this runner will not walk; three when the tree handed off nothing,
        so a caller can tell "nothing was there to run" from "the run was refused" — `suite` uses
        the same three for a suite that yielded no task.
    """
    out = Path(arguments.out)
    repo_name = arguments.repo_name
    try:
        client_versions = requested_client_versions(arguments)
    except ValueError as failure:
        _LOGGER.error("Hosted handoff refused: %s", failure)
        return 2
    _LOGGER.info("Preparing hosted handoff from %s", arguments.out)
    conventions = None
    if arguments.conventions_from is not None:
        conventions = _conventions(Path(arguments.conventions_from), repo_name, arguments.history)
    try:
        client = client_for(arguments.service, arguments.token or os.environ.get("MO_EVAL_TOKEN"))
        shipped = _hand_off(
            client,
            repo_name,
            out,
            routes=arguments.run,
            repeats=arguments.repeats,
            conventions=conventions,
            harnesses=arguments.harnesses,
            client_versions=client_versions,
        )
    except ServiceError as failure:
        # Reported the way `suite` reports it: a named service that cannot be reached is a message
        # and an exit code, not a traceback.
        _LOGGER.error("Hosted handoff failed: %s", failure)
        return 2
    except OrderError as failure:
        # `_hand_off` refuses a tree it will not read — a symlinked `local-suite`. On THIS path that
        # tree is the caller's, so the refusal is reachable, and reaching it must cost an exit code
        # rather than a traceback like every other refusal here.
        _LOGGER.error("Hosted handoff failed: %s", failure)
        return 2
    if not shipped:
        # `suite`'s code for "nothing survived". A hand-off that shipped no bundle recorded no run,
        # so reporting success would tell a workflow its arms are running when nothing is.
        _LOGGER.error("%s handed off no bundle; nothing is running", out)
        return 3
    return 0


def _conventions(repo: Path, repo_name: str, history: int) -> ConventionSources:
    """Collect what the conventions judge learns from. Review comments come from the most recently
    merged pull requests, not only the mined ones: a convention is stated wherever it was violated.

    Returns:
        Readable repository rules and substantive maintainer review comments for the conventions
        judge.
    """
    from mo_eval_svc.runner.collect import (  # ruff:ignore[import-outside-top-level]
        convention_files,
        review_comments,
    )

    numbers = _recent_merged(repo, repo_name, history)
    comments = [ReviewComment(**c) for c in review_comments(repo, repo_name, numbers)]
    files = convention_files(repo)
    _LOGGER.info(
        "Collected %d convention file(s) and %d review comment(s) from %d merged PRs",
        len(files),
        len(comments),
        len(numbers),
    )
    return ConventionSources(files=files, comments=comments)


def _recent_merged(repo: Path, repo_name: str, limit: int) -> list[int]:
    try:
        out = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "-R",
                repo_name,
                "--state",
                "merged",
                "--limit",
                str(limit),
                "--json",
                "number",
                "--jq",
                ".[].number",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    return [int(n) for n in out.split()]


_SUITE_MANIFEST = ".mo-eval/suite.yaml"
"""What makes a bundle scoreable. `local_suite_files` writes it alongside the prompt and the
acceptance script, and `mo-eval plan` refuses a bundle without it — so its absence is exactly the
thing the lane would discover, discovered here instead."""


_MAX_SUITE_BUNDLES = 512
"""How many bundles this runner will walk in a suite tree.

A suite is the handful of tasks a repository's merged pull requests yielded — `candidates` caps the
ask at a couple of dozen. On the `submit` path the tree is a cache or an artifact the CALLER named,
so the entry count is chosen by whoever wrote it rather than by this run; the bound is far above any
honest suite and far below a directory that would cost the runner its memory to list."""


def refuse_a_linked_tree(out: Path) -> None:
    """Refuse a suite tree whose own doors are links, before anything reads or writes through them.

    Called at both ends deliberately: `suite` writes into this tree, and a PRE-EXISTING
    `local-suite` or `tasks` link would have packaging create content outside `--out` long before a
    hand-off could notice; `submit` only reads, but the tree is the caller's. One check, run at
    whichever end comes first.

    Only these three. An ANCESTOR of `out` may legitimately be a link — `/tmp` is one on macOS, and
    a runner's temp path can be — so walking the ancestry would refuse honest trees. `suite` passes
    an already-resolved `out`, where the first test is a no-op and the children are the point;
    `submit` passes the path as given, where all three matter.

    Raises:
        OrderError: If `out` or either of the directories it owns is a symlink.
    """
    for named in (out, out / "local-suite", out / "tasks"):
        if named.is_symlink():
            raise OrderError(f"{named} is a symlink; a suite is a directory, not a link to one")


def confined_bundles(out: Path) -> list[Path]:
    """The bundle directories of the suite under `out`, or a refusal.

    The one place that decides what this runner may walk in a tree it did not necessarily produce:
    the doors are not links, the listing is bounded, and no entry is a link either. Everything else
    under `out` is reached through [`_safe_relative`], which refuses a link at any component.

    Returns:
        The bundle directories, sorted; empty when the suite directory is absent.

    Raises:
        OrderError: If a door is a link, or the listing is larger than this runner will walk.
    """
    refuse_a_linked_tree(out)
    suite_dir = out / "local-suite"
    if not suite_dir.is_dir():
        return []
    # Bounded BEFORE sorting: `sorted` materializes the whole listing, so a tree carrying a million
    # entries would cost that list before any of them was looked at. One more than the cap is read,
    # which is what makes "too many" distinguishable from "exactly the cap".
    entries = list(itertools.islice(suite_dir.iterdir(), _MAX_SUITE_BUNDLES + 1))
    if len(entries) > _MAX_SUITE_BUNDLES:
        raise OrderError(
            f"{suite_dir} holds more than {_MAX_SUITE_BUNDLES} entries; a suite is a handful of tasks"
        )
    return sorted(entry for entry in entries if entry.is_dir() and not entry.is_symlink())


def _hand_off(
    client: RunClient,
    repo_name: str,
    out: Path,
    routes: list[str],
    repeats: int,
    conventions: ConventionSources | None = None,
    harnesses: list[str] | None = None,
    refusals: list[str] | None = None,
    client_versions: ClientVersions | None = None,
) -> int:
    """Upload every validated bundle to the service's storage and record a run for the lane.

    `client_versions` names releases the lane stages in place of its pins; `None` sends no key.

    Returns:
        How many bundles were handed off. Zero is a real outcome — a suite whose bundles were all
        refused, or a tree with none — and it is returned rather than printed alone so a caller can
        refuse to report success for a run that shipped nothing.

    The bundles are tarred here and PUT to presigned URLs, so the service never receives the bytes
    (a bundle is a vendored start tree — far larger than a function is willing to carry). What is
    left on GitHub's side afterwards is nothing: the agent runs, the gateway key, and the spend all
    live on the service's side.

    Raises:
        ServiceError: If uploading a bundle fails or the service refuses the upload or run
            request.
        OrderError: If `local-suite` is a symlink rather than a directory — on the `submit` path
            that tree is the caller's, and a link there would have every bundle tarred through it.
    """

    _LOGGER.info("Preparing archives for hosted handoff")
    bundles = confined_bundles(out)
    if not bundles:
        if refusals:
            # Not a guess. A suite whose every bundle was refused HAS a worker image — refusing is
            # what that image just did — and telling the customer to check whether they declared one
            # sends them to the one place the answer is not.
            _LOGGER.warning(
                "Nothing to hand off: all %d bundle(s) were refused before shipping; see the refusals above",
                len(refusals),
            )
        else:
            _LOGGER.warning(
                "Nothing to hand off: no local-suite bundles (does the config declare worker_image?)"
            )
        return 0
    # A bundle whose build failed is still a DIRECTORY: `write_local_suite` creates the root, exports
    # the start tree into it, and only then lays the overlay over it — so a failure in between leaves
    # a tree with no `.mo-eval/`. Handed off, nothing downstream notices (the upload checks sizes, and
    # `/v1/runs` checks that each task has a validated reference, which it does), and the lane finds
    # out by claiming the job, exporting every bundle and failing at `mo-eval plan`. That reports a
    # failure in the customer's CI as a planning failure on our lane, minutes later, having spent a
    # claim. Checked here rather than in `suite` so `submit --out <dir>` is covered by the same rule.
    unscoreable = [bundle.name for bundle in bundles if not (bundle / _SUITE_MANIFEST).is_file()]
    if unscoreable:
        _LOGGER.warning(
            "Not handing off %d bundle(s) with no %s: %s. Their tasks remain runnable with "
            "`--service local`; bundle building failed above.",
            len(unscoreable),
            _SUITE_MANIFEST,
            ", ".join(unscoreable),
        )
        bundles = [bundle for bundle in bundles if bundle.name not in set(unscoreable)]
    if not bundles:
        _LOGGER.warning("Nothing to hand off: no bundle carries a suite to score")
        return 0
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
        _LOGGER.info("Requesting upload targets for %d bundle(s)", len(task_ids))
        targets = client.uploads(
            UploadRequest(repo=repo_name, suite_id=suite_id, task_ids=task_ids, sizes=archive_sizes)
        )
        _LOGGER.info("Uploading %d bundle(s) for suite %s", len(task_ids), suite_id)
        for name, archive in archives.items():
            url = targets.urls[name]
            if url.startswith("file://"):
                Path(url[7:]).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(archive, url[7:])
            elif not url.startswith("memory://"):
                with archive.open("rb") as body:
                    request = urllib.request.Request(
                        url,
                        data=body,
                        method="PUT",
                        headers={
                            "content-type": "application/gzip",
                            "content-length": str(archive_sizes[name]),
                        },
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
                        raise ServiceError(
                            f"uploading {name}: {getattr(failure, 'reason', failure)}"
                        ) from failure
            _LOGGER.info("Uploaded bundle %s (%.1f MB)", name, archive_sizes[name] / 1e6)
    finally:
        for archive in archives.values():
            archive.unlink(missing_ok=True)
    titles, categories, sizes = {}, {}, {}
    for task_id in task_ids:
        # Through the same door as the bundles: a cached `tasks/` or task directory that is a link
        # would otherwise read a meta from outside the tree and send its labels in the run request.
        try:
            meta = _safe_relative(f"tasks/{task_id}/meta.json", out)
        except ValueError as failure:
            _LOGGER.warning("%s: %s; its labels are omitted", task_id, failure)
            continue
        if not meta.is_file():
            continue
        # `submit` reaches here with a tree the CALLER named — a cache or an artifact, not this
        # run's own output. A truncated, non-object or unreadable meta is that tree's problem, not
        # a reason to end a hand-off that has already uploaded: the labels below are what a report
        # is titled with, and a run without them is a run with worse labels, not a failed one.
        try:
            record = json.loads(meta.read_text())
        except (OSError, ValueError) as failure:
            _LOGGER.warning("%s: meta.json unreadable (%s); its labels are omitted", task_id, failure)
            continue
        if not isinstance(record, dict):
            _LOGGER.warning("%s: meta.json is not an object; its labels are omitted", task_id)
            continue
        title = record.get("title")
        titles[task_id] = title if isinstance(title, str) else ""
        generated = record.get("generated")
        if not isinstance(generated, dict):
            continue
        # Checked, not merely truthy. `RunRequest.categories`/`.sizes` are `dict[str, str]`, and a
        # reused cache is not this run's output: a non-string here would serialize into the typed
        # request and the service would refuse `/v1/runs` AFTER the bundles were uploaded — which
        # is the opposite of what the comment above promises, a bad meta costing its labels alone.
        category, size = generated.get("category"), generated.get("size")
        if isinstance(category, str) and category:
            categories[task_id] = category
        if isinstance(size, str) and size:
            sizes[task_id] = size
    # No harness named stays unnamed all the way to the service, which is what decides what that
    # means — rather than a default here to drift from it, or a key in a request that predates it.
    _LOGGER.info("Recording hosted run")
    ticket = client.runs(
        RunRequest(
            repo=repo_name,
            suite_id=suite_id,
            task_ids=task_ids,
            arms=routes,
            repeats=repeats,
            titles=titles,
            conventions=conventions,
            categories=categories,
            sizes=sizes,
            harnesses=harnesses,
            client_versions=client_versions,
        )
    )
    _LOGGER.info(
        "Recorded run %s (%s); results will appear under %s",
        ticket.run_id,
        ticket.job_key,
        ticket.results_prefix,
    )
    return len(task_ids)


FUNNEL_RECORD = "funnel.txt"
"""Where under `--out` the funnel block is kept. The Action's summary step reads it to know the
runner got as far as reporting a funnel: `GITHUB_STEP_SUMMARY` is a different file for every step,
so the block the runner appended to its own is invisible to the step that follows."""


def _package_each_verdict(
    report: VerdictReport,
    repo: Path,
    out: Path,
    config: RunnerConfig | None,
    cache: Path,
    written: list[Path],
) -> tuple[list[Path], list[str]]:
    """Write each validated task and its containerized bundle, recording what could not be shipped.

    A function rather than a loop inside `suite` so the counts the funnel reports can be tested
    against the code that produces them. Asserting them on `_funnel` alone leaves the wiring
    untested: the tally renders correctly from arguments nothing fills in.

    Args:
        report: Verdicts from validation, carrying each task.
        repo: Checkout the start trees are exported from.
        out: Suite output root; tasks and bundles are written beneath it.
        config: Repository configuration, or `None` when it declares nothing.
        cache: Shared preparation cache for the run.
        written: Task package paths, appended to in order.

    Returns:
        The bundles written, and a message for each one that could not be.
    """
    packaged: list[Path] = []
    refusals: list[str] = []
    for verdict in report.verdicts:
        mark = "VALIDATED" if verdict.validated else "rejected "
        _LOGGER.info("%s %s: %s", mark, verdict.change_id[:9], verdict.detail)
        if verdict.task is None:
            continue
        written.append(write_package(verdict.task, out / "tasks"))
        try:
            bundle = write_local_suite(verdict.task, repo, out / "local-suite", config, cache=cache)
        except (OrderError, ValueError, OSError) as failure:
            # The task itself is already written and still valid. Only its containerized bundle
            # could not be built, and one task's bundle failing is not the suite's end. Counted as
            # well as printed: stderr is not where a customer reads whether their suite worked, and
            # a green run reporting `validated N of N` while shipping nothing is what this records.
            _LOGGER.warning("Bundle for %s was not created: %s", verdict.task.task_id, failure)
            refusals.append(str(failure))
            continue
        if bundle is not None:
            packaged.append(bundle)
            _LOGGER.info("Bundle for %s is ready at %s", verdict.task.task_id, bundle)
    return packaged, refusals


def _funnel(
    facts: RepoFacts,
    request: SourceRequest,
    response: OrdersResponse,
    ran: int,
    report: VerdictReport,
    written: list[Path],
    *,
    dry_run: bool = False,
    evidence: str = "",
    record: Path | None = None,
    packaged: list[Path] | None = None,
    refusals: list[str] | None = None,
) -> None:
    """Where every offered change went. The number a user needs is not how many tasks they got,
    but why they did not get more. `ran` is how many orders ran, or on a dry run how many would
    have, which `--validate` may cap below the orders issued.

    Printed, appended to the job summary where there is one (`GITHUB_STEP_SUMMARY`), since a suite
    that yielded nothing is exactly when the funnel has to be in front of the reader, and kept at
    `record` for the step that reads the summary after this one. `evidence` is `_evidence`'s
    report, placed after the funnel in all three.
    """
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
    elif facts.source_note:
        # The forge answered, but not from where the reader would assume — a repository that renamed
        # its default branch is mined from the branch its history actually targets, and saying so is
        # the difference between a number a reader can trust and one they cannot.
        notes.append(facts.source_note)
    suffix = f"  ({'; '.join(notes)})" if notes else ""
    lines = [
        f"offered    {offered:>4} merged changes via {facts.source}{suffix}",
        f"selected   {len(request.change_ids):>4}   " + _reasons(request.rejections),
        f"orders     {len(response.orders):>4}   " + _reasons(response.rejections),
    ]
    if dry_run:
        # Nothing ran, so nothing survived or failed to; saying so would misread a dry run as a
        # repository that yields nothing.
        lines.append(f"validated     —   (dry run: {ran} order(s) would run)")
        diagnosis = ""
    else:
        validated = sum(1 for v in report.verdicts if v.validated)
        lines.append(f"validated  {validated:>4} of {ran} run")
        lines += [f"           → {path}" for path in written]
        # A task validates on THIS host; a bundle also has to be scoreable in the declared worker
        # image. The two diverge exactly when those environments disagree, which is the failure the
        # packaging check exists to catch — so a run that validates tasks and packages none must
        # show the contradiction rather than report the first number and fall silent.
        attempted = len(packaged or []) + len(refusals or [])
        if attempted:
            lines.append(f"packaged   {len(packaged or []):>4} of {attempted}   " + _refusals(refusals or []))
        diagnosis = _diagnosis(request, response, report, ran=ran) if not written else ""
    if evidence:
        lines += ["", *evidence.rstrip("\n").splitlines()]
    if diagnosis:
        lines += ["", diagnosis]
    # Cleaned at the sink, every line alike, since this is the one place the block leaves. A
    # rejection's wording can carry a line of probe output: a reporter may have coloured it, and a
    # newline in it would put whatever follows at the start of a line of its own — where `::` is a
    # workflow command to Actions and three backticks would close a fence of three in the summary.
    lines = [_ANSI.sub("", line).replace("\n", " ") for line in lines]
    print()
    for line in lines:
        print(f"  {line}")
    block = "\n".join(["### funnel", "````", *lines, "````", ""])
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n" + block)
    if record is not None:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(block, encoding="utf-8")


def _report_written_outputs(out: Path) -> None:
    """Report the artifact categories produced by a completed suite command."""
    artifacts: list[str] = []
    wire_directory = out / "wire"
    wire_records = sorted(wire_directory.glob("*.json")) if wire_directory.is_dir() else []
    if wire_records:
        artifacts.append(f"protocol records ({len(wire_records)}): {wire_directory}")
    funnel = out / FUNNEL_RECORD
    if funnel.is_file():
        artifacts.append(f"funnel report: {funnel}")
    tasks_directory = out / "tasks"
    task_count = sum(path.is_dir() for path in tasks_directory.iterdir()) if tasks_directory.is_dir() else 0
    if task_count:
        artifacts.append(f"validated task packages ({task_count}): {tasks_directory}")
    bundles_directory = out / "local-suite"
    bundle_count = (
        sum(path.is_dir() for path in bundles_directory.iterdir()) if bundles_directory.is_dir() else 0
    )
    if bundle_count:
        artifacts.append(f"runnable task bundles ({bundle_count}): {bundles_directory}")
    if artifacts:
        _LOGGER.info("Wrote artifacts under %s", out)
        for artifact in artifacts:
            _LOGGER.info("  %s", artifact)


_ORDER_CHANGE = re.compile(r"^validate-([0-9a-f]{12})(?:-|$)")
"""The change an order validates, as the service spells an order id (`validate-<change>-<suffix>`);
a verdict names the change, and the two are joined here. An id of another shape joins nothing."""
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_COUNTS = re.compile(r"[0-9]+")
_EVIDENCE_LINES = 12
"""How much of a probe's tail the log shows for one rejection: enough for pytest's collection error
or Go's compile error and its summary, which is what a reader needs to place the failure."""


def _kind(verdict: OrderVerdict) -> str:
    """A rejection's wording with its counts taken out, so `2 graded test(s) did not run` and
    `4 graded test(s) did not run` are one kind of failure, which they are.

    Returns:
        The rejection detail with numeric counts normalized for grouping.
    """
    return _COUNTS.sub("N", verdict.detail)


def _evidence(results: list[OrderResult], report: VerdictReport) -> str:
    """One probe tail per kind of rejection and per way the tail ends: the first failing step's
    last lines, from the first order rejected that way.

    The tail is the evidence a verdict rests on, and it was readable only from the artifact.
    pydantic's eight identical rejections were two causes, a missing test dependency and a
    pydantic-core older than the commit expected, and the tail's last line is what tells them
    apart. Returned for `_funnel` to place.

    Returns:
        Formatted failing-step tails grouped by rejection kind and final line, or empty text
        when none are available.
    """
    by_order = {
        found.group(1): result for result in results if (found := _ORDER_CHANGE.match(result.order_id))
    }
    tails = []
    for verdict in report.verdicts:
        if verdict.validated:
            continue
        result = by_order.get(verdict.change_id[:12])
        step = next((step for step in (result.steps if result else []) if step.exit_code != 0), None)
        lines = _ANSI.sub("", step.output_tail).strip().splitlines() if step else []
        tails.append((verdict, step, lines))
    per_kind = Counter(_kind(verdict) for verdict, _, _ in tails)
    alike = Counter((_kind(verdict), lines[-1] if lines else "") for verdict, _, lines in tails)
    printed: list[str] = []
    seen: set[tuple[str, str]] = set()
    for verdict, step, lines in tails:
        key = (_kind(verdict), lines[-1] if lines else "")
        if step is None or key in seen:
            continue
        seen.add(key)
        share = (
            f" ({alike[key]} of {per_kind[key[0]]} rejected this way end like this)"
            if per_kind[key[0]] > 1
            else ""
        )
        printed.append(f"evidence  {verdict.change_id[:9]}  {step.step_id} exited {step.exit_code}{share}")
        printed += [f"           | {line}" for line in lines[-_EVIDENCE_LINES:]]
    return "\n".join(printed) + ("\n" if printed else "")


_HINTS = (
    (
        "merged more than",
        "The history window reaches past the age limit, so the repository merges rarely; "
        "recency is not fit. A repository with recent merged work, or a smaller --history.",
    ),
    (
        "touches no source file",
        "Most of what the repository merges is docs, CI or tests. If it plainly has "
        "source under an unusual layout, the language contract is not recognising it; "
        "survey.py reports the share it claims.",
    ),
    (
        "the reference state does not pass",
        "The repository's own tests do not run at the maintainers' own "
        "commit in this job: usually a test dependency or setup_command the "
        "job did not install, or a dependency the commit pins differently "
        "from the checkout. The evidence above says which.",
    ),
    (
        "did not run at the start state",
        "The graded tests were not seen running; the evidence above shows what the probe printed instead.",
    ),
    (
        "could not be set up",
        "The start tree could not be prepared in this job; the evidence above names the file.",
    ),
    ("does not build", "The start tree does not compile in this job; the evidence above names the file."),
    (
        "writes no test",
        "The selected changes carry no test of their own, so nothing states the task; a "
        "repository whose fixes land without tests yields little.",
    ),
    (
        TOO_LARGE_TO_SEND,
        "Those changes touch more file content than one request carries, so they were "
        "never asked about; the rest of the selection was. Too big to carry rather "
        "than unfit — a smaller --candidates does not help, since each is already "
        "weighed on its own.",
    ),
)
"""What a dominant rejection reason usually means, in words a reader can act on. Keyed by a phrase
of the reason, since that wording is the contract a reader already sees — the service's own for
every entry but the last, which the runner composes when a change cannot be sent at all."""


def _dominant(tally: list[RejectionSummary]) -> tuple[int, str]:
    top = max(tally, key=lambda rejection: rejection.count)
    return top.count, top.reason


def _diagnosis(request: SourceRequest, response: OrdersResponse, report: VerdictReport, *, ran: int) -> str:
    """Which stage emptied the funnel, its dominant reason, and what that usually means.

    Eight identical rejections are the signature of a missing dependency rather than of eight unfit
    changes, and 159 of 293 `touches no source file` is a repository that merges docs and CI; the
    reader is told that rather than left to infer it. Empty when something survived.

    Returns:
        The empty funnel's stage, dominant rejection, and available hint, or empty text when a
        task validated.
    """
    if any(verdict.validated for verdict in report.verdicts):
        return ""
    if not request.change_ids:
        if not request.rejections:
            return "nothing survived selection: the repository offered no merged changes to select from"
        stage, total, unit = "selection", sum(rejection.count for rejection in request.rejections), "offered"
        count, reason = _dominant(request.rejections)
    elif not response.orders:
        if not response.rejections:
            return "nothing survived the split, and the service gave no reason"
        stage, total, unit = "the split", len(request.change_ids), "selected"
        count, reason = _dominant(response.rejections)
    else:
        if not report.verdicts:
            return "nothing survived validation: no verdicts came back"
        stage, total, unit = "validation", ran, "run"
        kinds = Counter(_kind(verdict) for verdict in report.verdicts)
        kind, count = kinds.most_common(1)[0]
        reason = next(verdict.detail for verdict in report.verdicts if _kind(verdict) == kind)
    hint = next((hint for phrase, hint in _HINTS if phrase in reason), "")
    sentence = f"nothing survived {stage}: {count} of {total} {unit} {reason}"
    return f"{sentence}. {hint}" if hint else sentence


def _refusals(refusals: list[str]) -> str:
    """Tally why bundles were refused, one entry per distinct reason.

    Each message leads with the task id that could not be packaged, which makes every one unique —
    so the id is dropped before grouping, the way `_reasons` groups a rejection's wording rather
    than its subject. Bounded like the rest of the funnel: a refusal carries the scorer's own
    output, and the whole tail belongs in the evidence block, not in a one-line tally.

    Args:
        refusals: Messages from bundles that could not be packaged.

    Returns:
        A `refused: …` tally, or empty when nothing was refused.
    """
    if not refusals:
        return ""
    counts: dict[str, int] = {}
    for message in refusals:
        _, separator, reason = message.partition(": ")
        first = (reason if separator else message).splitlines()[0].strip()
        counts[first[:_REFUSAL_REASON_CHARS]] = counts.get(first[:_REFUSAL_REASON_CHARS], 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    top = ", ".join(f"{count} {reason}" for reason, count in ordered[:2])
    more = f", +{len(ordered) - 2} more" if len(ordered) > 2 else ""
    return f"refused: {top}{more}"


_REFUSAL_REASON_CHARS = 160
"""How much of a refusal survives into the one-line tally; the rest is the scorer's own output."""


def _reasons(rejections: list[RejectionSummary]) -> str:
    if not rejections:
        return ""
    top = ", ".join(f"{r.count} {r.reason}" for r in rejections[:3])
    more = f", +{len(rejections) - 3} more" if len(rejections) > 3 else ""
    return f"rejected: {top}{more}"


def _add_client_version_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the flags naming releases the lane stages in place of its pins.

    Args:
        parser: The `suite` or `submit` subcommand parser.
    """
    parser.add_argument(
        "--mo-version",
        type=client_version,
        default=None,
        metavar="X.Y.Z",
        help="published mo release every arm stages (default: the lane's pin)",
    )
    parser.add_argument(
        "--claude-code-version",
        type=client_version,
        default=None,
        metavar="X.Y.Z",
        help="published Claude Code release a cc arm runs (default: the lane's pin)",
    )
    parser.add_argument(
        "--codex-version",
        type=client_version,
        default=None,
        metavar="X.Y.Z",
        help="published Codex release a codex arm runs (default: the lane's pin)",
    )


def main(configure_logging: LoggingConfigurator | None = None) -> int:
    """Run the dependency-free runner CLI.

    Args:
        configure_logging: CLI-boundary logging configuration. The Poetry wrapper supplies Rich
            rendering; omitted, vendored runners use the standard-library formatter.

    Returns:
        The command's process exit code.
    """
    parser = argparse.ArgumentParser(prog="mo-eval-runner", description=__doc__)
    commands = parser.add_subparsers(required=True)
    suite_parser = commands.add_parser("suite", help="mine, validate, and write this repository's suite")
    suite_parser.add_argument("--repo", type=Path, default=Path("."))
    suite_parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="relative to --repo")
    suite_parser.add_argument("--service", default="local", help="'local' or the service base URL")
    suite_parser.add_argument("--token", default=None, help="bearer token, else $MO_EVAL_TOKEN")
    suite_parser.add_argument("--branch", default=None)
    suite_parser.add_argument("--history", type=int, default=300)
    suite_parser.add_argument("--candidates", type=int, default=8)
    suite_parser.add_argument("--validate", type=int, default=0, help="cap on orders to run (0 = all)")
    suite_parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output directory (default: <repo>/.mo-eval/out)",
    )
    suite_parser.add_argument(
        "--dry-run", action="store_true", help="stop after orders are issued; run nothing"
    )
    suite_parser.add_argument("--verbose", action="store_true", help="show detailed progress logging")
    suite_parser.add_argument(
        "--run",
        nargs="*",
        metavar="ROUTE",
        default=None,
        help=(
            "after validating, upload the bundles and ask the service to evaluate them on these model routes"
        ),
    )
    suite_parser.add_argument(
        "--harnesses",
        nargs="+",
        metavar="NAME",
        default=None,
        help="harnesses to compare against every route: mo, cc, pi, strands, codex (default: mo)",
    )
    suite_parser.add_argument("--repeats", type=int, default=1)
    _add_client_version_arguments(suite_parser)
    suite_parser.set_defaults(command_fn=suite)
    submit_parser = commands.add_parser(
        "submit", help="upload an already-validated --out tree and ask the service to evaluate it"
    )
    submit_parser.add_argument("--out", required=True)
    submit_parser.add_argument("--repo-name", required=True, help="owner/name the suite was mined from")
    submit_parser.add_argument("--service", required=True)
    submit_parser.add_argument("--token", default=None, help="bearer token, else $MO_EVAL_TOKEN")
    submit_parser.add_argument("--verbose", action="store_true", help="show detailed progress logging")
    submit_parser.add_argument(
        "--run",
        nargs="+",
        metavar="ROUTE",
        required=True,
        help="model routes, each paired with every harness",
    )
    submit_parser.add_argument(
        "--harnesses",
        nargs="+",
        metavar="NAME",
        default=None,
        help="harnesses to compare against every route: mo, cc, pi, strands, codex (default: mo)",
    )
    submit_parser.add_argument("--repeats", type=int, default=1)
    _add_client_version_arguments(submit_parser)
    submit_parser.add_argument(
        "--conventions-from",
        metavar="REPO",
        default=None,
        help=(
            "a checkout to collect convention sources from (contributing guide, lint config, review comments)"
        ),
    )
    submit_parser.add_argument(
        "--history", type=int, default=120, help="merged pull requests to read review comments from"
    )
    submit_parser.set_defaults(command_fn=submit)
    arguments = parser.parse_args()
    (configure_logging or configure_plain_logging)(arguments.verbose)
    command = cast(Callable[[argparse.Namespace], int], arguments.command_fn)
    return command(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
