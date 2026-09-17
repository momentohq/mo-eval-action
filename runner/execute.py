"""Execute work orders. The runner's whole intelligence, and there is deliberately very little of it.

Three primitives — export a tree, apply a patch, probe with the repository's own test command — and
a report of how each exited. Nothing here knows what a task is, which patch is a scaffold, or which
way a probe is supposed to go. That is not an accident of the spike: it is what lets the service keep
its method while the work runs on a machine the customer controls.

The bound on a probe is structural. `Step.args` are appended to the `test_command` the repository
itself declares, so the service chooses which tests run and never what runs them.
"""

from __future__ import annotations

import codecs
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from wire import OrderResult, Step, StepResult, WorkOrder

_RUNNER_CREDENTIALS = (
    "MO_EVAL_TOKEN",                  # the bearer this runner authenticates to the service with
    "GH_TOKEN",                       # set by the Action so the forge can be queried
    "GITHUB_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_URL",   # together, these MINT an identity for the repository
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
)
"""What the runner itself brings into the job, and must take away again before running a test.

A deny-list rather than an allow-list, which is the opposite of the rule for forwarding request
headers — and deliberately so. A build needs an environment nobody can enumerate in advance
(`GOCACHE`, `HOME`, `TMPDIR`, a hundred toolchain variables), so an allow-list here would break real
repositories rather than protect them. What CAN be enumerated is what the runner introduced: its own
bearer, the forge token the Action sets, and the pair that lets any code in the job mint an OIDC
token as this repository — a privilege the workflow has only because our own snippet asked for it.
The repository's own secrets stay: its tests already run with those in every other workflow it has.

Half of a pair. This removes what is in the ENVIRONMENT; a credential on disk is untouched by it,
which is why `action/mo-eval-suite.yml` checks out with `persist-credentials: false`. A scrub that
holds only in memory reads as a boundary and is not one.
"""


def _without_the_runners_credentials(inherited: Mapping[str, str], declared: dict[str, str]) -> dict[str, str]:
    """The environment a repository's own test command is run with.

    The declared half is filtered too. `forward_env` names variables to carry over from the runner's
    own environment, so a repository that named one of these would otherwise hand itself the
    credential the inherited half just removed.
    """
    return {
        **_UNCOLOURED,
        **{
            name: value
            for source in (inherited, declared)
            for name, value in source.items()
            if name not in _RUNNER_CREDENTIALS
        },
    }


_UNCOLOURED = {"NO_COLOR": "1", "FORCE_COLOR": "0"}
"""Asks a test reporter not to colour its output.

The proof that a test ran is a pattern over that output, and an escape sequence sits between the
things a pattern anchors to. Vitest in a container prints the name and then `\x1b[32m 2ms`, so a
proof anchored to the end of the line stops matching a test that plainly passed — measured on
`hono` in `oven/bun:1`, where the same probe matches under `NO_COLOR` and does not without it.

First in the mapping, so a repository that declares either one keeps its own value: a repository
that wants colour is choosing a harder thing to match, not being overridden.
"""


_GROUP_EXIT_SECONDS = 30
"""How long a signalled process group is given to end before it is killed outright."""

_MAX_STEPS_PER_ORDER = 200
"""How many steps one order may carry. An order is four fixed steps plus two probes per graded
test, so this is far above any real task."""

_MAX_ORDER_SECONDS = 4 * 3600
_MAX_RUNNER_SECONDS = 12 * 3600
"""How long one order, and every order this runner carries out, may take in total.

The step count is not a bound on cost on its own: 200 steps at an hour each is over a week of a
customer's CI, and a suite is many orders. Each step's own deadline still applies; these two cut
whichever comes first, so a service composing cheap-looking steps cannot spend an unbounded amount
of someone else's CI in aggregate."""

_MAX_SPOOLED_BYTES = 2 * 1024**3
_OUTPUT_POLL_SECONDS = 5
_EXIT_POLL_SECONDS = 0.05
"""How much output one command may write, and how often that is checked. The tail is what gets
reported; it does not stop the writing. A test command producing at device speed fills a CI
runner's disk long before any per-step deadline."""

_REDACTION_OVERLAP_BYTES = 4096
"""How much beyond the reported tail is read, so a secret straddling the cut is still whole when it
is replaced. Longer than any credential; short enough that the bound still bounds."""

_REDACTED = "[redacted]"
"""What a forwarded value is replaced with on its way back to the service."""

_APPLY_TIMEOUT_SECONDS = 120
"""A bound on `git apply`, the step that processes content the service supplied."""

_LOCAL_COMMAND_SECONDS = 120
"""A bound on the runner's own short commands — `git init`, `rustc -vV`. They take milliseconds on
a working machine and hang indefinitely on a wedged filesystem or a stalled toolchain, and this
process is the customer's CI job."""

_ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
"""What an order id may be. The same shape the service enforces, checked again here because the
runner must hold its own boundary rather than trust the service to have held it."""

_COMMIT = re.compile(r"[0-9a-f]{7,64}")
"""What a commit the service names may be: a hexadecimal object name and nothing else."""

_OUTPUT_TAIL_BYTES = 4000
_EXPORT_TIMEOUT_SECONDS = 600
_PROBE_TIMEOUT_SECONDS = 3600
_SETUP_TIMEOUT_SECONDS = 1800


_SCAN_CHUNK_BYTES = 1024 * 1024
"""How much of a spooled output is read at once while looking for the proof pattern."""

_SCAN_LINE_CHARS = 4 * 1024 * 1024
"""How long one line may grow before it is searched without waiting for its end.

A proof is a line pattern, so the scan holds a partial line until the newline that completes it. A
command that writes no newline at all — a progress bar redrawing with a carriage return — would
otherwise make that "partial line" the whole output, which is the bound this module exists to hold.
"""

_SCAN_OVERLAP_CHARS = _SCAN_LINE_CHARS
"""How much of an over-long line is carried into the next read, so a match straddling the cut is
still found. The cap's worth, not a token amount: a proof may be as long as the line it sits in —
`::{name}\b.*PASSED` spans a whole parametrized node id — and carrying less would discard a match
for being longer than an arbitrary window. Only reached on the over-long path; a complete line is
carried whole and needs no overlap."""


def _scanned(sink, *patterns: str | None) -> tuple[bool | None, ...]:
    """Whether `pattern` occurs anywhere in the spooled output.

    Read a piece at a time, because the whole of a command's output is up to the spool cap and the
    caller is a CI job — and read at all because the evidence is usually nowhere near the end.

    Searched over COMPLETE LINES, newline included, and compiled `MULTILINE` so `^` and `$` mean
    the ends of a line — the same thing they mean to the `grep -E` the offline scorer matches the
    same pattern with. A regular expression reads the end of a string
    as a word boundary, so searching a piece cut mid-line would let `--- PASS: TestFoo` inside
    `--- PASS: TestFooBar` prove that `TestFoo` ran. Cutting only at a newline keeps every boundary
    the pattern sees a boundary the output really has. The cost is that a proof spanning lines is
    found only when those lines land in the same read; every language contract states a line.

    Leaves the position where it stopped, so a caller that still wants the tail records the end
    first. A pattern that does not compile is the service's mistake, not this runner's, and is
    reported as "not looked for" rather than ending the order.
    """
    compiled: list[re.Pattern[str] | None] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.MULTILINE) if pattern else None)
        except re.error:
            # Distinct from "there was no pattern": the service asked a question this runner could
            # not read, and answering `None` would let the service fall back to reading an exit
            # code as though it had asked nothing. Answered "not found", which is the safe way to
            # be wrong — it loses a task rather than inventing one.
            compiled.append(re.compile(r"(?!)"))
    found: list[bool | None] = [None if expression is None else False for expression in compiled]
    if not any(expression is not None for expression in compiled):
        return tuple(found)
    # Decoded across reads rather than per read: a multi-byte character split by the cut would
    # otherwise become two replacement characters, and a test name carrying one could never match.
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    sink.seek(0)
    carry = ""
    while True:
        chunk = sink.read(_SCAN_CHUNK_BYTES)
        text = carry + decoder.decode(chunk, final=not chunk)
        def look(where: str, whole: bool) -> None:
            """Record every pattern that matches `where`. `whole` says the edges of the string are
            edges the output really has, so a match touching one is genuine."""
            for index, expression in enumerate(compiled):
                if expression is None or found[index]:
                    continue
                match = expression.search(where)
                if match is not None and (whole or 0 < match.start() and match.end() < len(where)):
                    found[index] = True

        if not chunk:
            look(text, True)
            return tuple(found)
        settled, newline, rest = text.rpartition("\n")
        if newline:
            look(settled + newline, True)
            if all(state is not False for state in found):
                return tuple(found)
            carry = rest
        elif len(text) > _SCAN_LINE_CHARS:
            # One line past the cap, so there is no newline to cut at and the line cannot be held
            # whole. Searched where it stands, and a match touching either end is left for the next
            # read: at those ends the string's own edge stands in for a character the output has,
            # and `\b` reads an edge as a word boundary whether or not one is there.
            look(text, False)
            if all(state is not False for state in found):
                return tuple(found)
            carry = text[-_SCAN_OVERLAP_CHARS:]
        else:
            carry = text


def _bounded_output(argv: list[str], *, cwd, env: dict[str, str] | None, timeout: float,
                    proof: str | None = None,
                    counterproof: str | None = None) -> tuple[int, str, bool | None, bool | None]:
    """Run a command and return its exit code and the tail of what it wrote.

    The output is written to a temporary file and only the tail is read back. `capture_output`
    would hold all of it in this process first, so the tail would be a display limit and not a
    bound: a repository whose setup prints for half an hour has a CI runner's memory to fill
    before the timeout ever arrives.
    """
    with tempfile.TemporaryFile("w+b") as sink:
        # Its own process group, so the deadline takes the tree and not just its root. The command
        # is the repository's own — `cargo test`, `pytest`, `make` — and each of those is a parent
        # of compilers, test binaries and servers. Killing the root leaves those running on the CI
        # runner after the workspace they were using has already been deleted.
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdout=sink, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        group = os.getpgid(child.pid)
        try:
            _wait_within(child, sink, timeout)
        finally:
            # Always, not only after a timeout. A test command's children outlive it often enough,
            # and the workspace they hold open is deleted as soon as the order ends. The leader is
            # reaped after this, not before: while it is still a zombie its pid is reserved, so the
            # group id cannot have been recycled under a stranger by the time it is signalled.
            #
            # A command that ended on its own gets no grace period — it has already had its run,
            # and what is left in its group are orphans holding a workspace about to be deleted.
            # One that is being stopped gets the usual `SIGTERM` first.
            _end_now(group) if _exited(child) else _end_group(group)
            child.wait()
        if sink.tell() > _MAX_SPOOLED_BYTES:
            # Checked on the way out as well as while waiting: a command can write its whole flood
            # inside one polling interval and exit, and the disk is just as full either way.
            raise OrderError(f"the command wrote more than {_MAX_SPOOLED_BYTES} bytes of output")
        # Where the output ends, recorded before the scan below moves the position — taken after it,
        # a scan that stopped at its match would make the "tail" a slice out of the middle.
        end = sink.tell()
        # Searched over the WHOLE spool, before the tail is cut. The evidence that a test ran is
        # often far from the end — a coverage table, a workspace of test binaries — and once the
        # tail is taken it is gone.
        seen, failed = _scanned(sink, proof, counterproof)
        # Read a little more than the tail so the caller can redact across the cut before it
        # truncates: a credential straddling the boundary would otherwise keep its surviving half.
        sink.seek(max(0, end - (_OUTPUT_TAIL_BYTES + _REDACTION_OVERLAP_BYTES)))
        return child.returncode, sink.read().decode(errors="replace"), seen, failed


def _wait_within(child: subprocess.Popen, sink, timeout: float) -> None:
    """Wait for a child, ending it if it runs too long or writes more than the runner will hold.

    The tail is what gets reported; it is not backpressure. A command writing at device speed for
    an hour fills the CI runner's disk whatever the tail says, so the spool is watched as it grows.

    Raises:
        subprocess.TimeoutExpired: If the deadline passes.
        OrderError: If the command writes more output than this runner will spool.
    """
    deadline = time.monotonic() + timeout
    weighed_at = time.monotonic()
    while True:
        if _exited(child):
            return
        now = time.monotonic()
        if now - weighed_at >= _OUTPUT_POLL_SECONDS:
            if sink.tell() > _MAX_SPOOLED_BYTES:
                raise OrderError(f"the command wrote more than {_MAX_SPOOLED_BYTES} bytes of output")
            weighed_at = now
        remaining = deadline - now
        if remaining <= 0:
            raise subprocess.TimeoutExpired(child.args, timeout)
        # Two rates: the child is looked for often, so a command that finishes is not waited on
        # after it has, and the spool is weighed rarely, because its size is a bound on a runaway
        # rather than something that changes meaningfully between one look and the next.
        time.sleep(min(_EXIT_POLL_SECONDS, remaining))


def _exited(child: subprocess.Popen) -> bool:
    """Whether the child has finished, WITHOUT reaping it.

    `wait` would reap, and a reaped pid is free to be reused — including as the id of somebody
    else's process group, which is the thing the caller goes on to kill. Left as a zombie the pid
    stays reserved until the caller has finished with it.
    """
    try:
        return os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None
    except ChildProcessError:
        return True


def _end_now(group: int) -> None:
    """Kill a process group outright, with no grace period."""
    try:
        os.killpg(group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _end_group(group: int) -> None:
    """Signal a whole process group, and then kill it.

    `SIGKILL` follows unconditionally rather than only when the leader is still alive. A test
    command usually goes on `SIGTERM` while something it started does not, and waiting on the
    leader reports that the group ended when only its leader did.

    Takes the group rather than the process, and the caller reads it with `os.getpgid` while the
    child is still running. Looked up afterwards it can be wrong in the dangerous direction: once
    the leader is reaped its pid is free, and `getpgid` on a reused pid answers with a stranger's
    group — which this function would then kill.
    """
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, signal_number)
        except (ProcessLookupError, PermissionError):
            return
        if signal_number is signal.SIGTERM:
            deadline = time.monotonic() + _GROUP_EXIT_SECONDS
            while time.monotonic() < deadline and _group_alive(group):
                time.sleep(0.1)
            if not _group_alive(group):
                return


def _group_alive(group: int) -> bool:
    """Whether any process in the group is still there."""
    try:
        os.killpg(group, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class OrderError(RuntimeError):
    """The runner could not carry an order out — distinct from a step that ran and exited non-zero."""


def commit_or_refuse(value: str) -> str:
    """A commit the service named, confirmed to be a commit before git is asked to resolve it.

    `git archive` reads its tree-ish positionally, so a value beginning with a dash is read as an
    option instead: `--output=<path>` truncates that path before git notices the tree is missing,
    and `--remote=<url>` opens a connection out of the customer's CI. Every value the service names
    goes through here, so nothing but a hexadecimal object name ever reaches git.
    """
    if not _COMMIT.fullmatch(value):
        raise OrderError(f"the service named {value!r} as a commit, which is not one")
    return value


def _host_target() -> str:
    """Resolve this machine's Rust host triple.

    Environment knowledge, so the runner answers it. A repository whose test command needs the triple
    writes `{host_target}` in its declared command and the runner fills it in; the service never
    supplies it, because the service does not know what machine this is.
    """
    probe = subprocess.run(["rustc", "-vV"], check=True, capture_output=True, text=True,
                           timeout=_LOCAL_COMMAND_SECONDS)
    for line in probe.stdout.splitlines():
        if line.startswith("host: "):
            return line.removeprefix("host: ").strip()
    raise OrderError("rustc reported no host triple")


def _fills(part: str, argument: str) -> bool:
    """Whether `argument` is `part` with its placeholders filled and nothing else changed.

    A placeholder takes anything that is not another option. A test's name is nearly unconstrained
    — Ginkgo, Jest and RSpec all name tests in prose, spaces included — but nothing a test is
    called begins with `-`, and that is the character that turns a selector into an instruction.
    There is no shell here, so what an argument contains reaches the tool as one argument; what
    matters is whether the tool will read it as a flag.
    """
    if "{" not in part:
        return argument == part
    pattern = "".join(
        r"(?!-).*" if piece in ("{name}", "{package}") else re.escape(piece)
        for piece in re.split(r"(\{name\}|\{package\})", part)
    )
    if re.fullmatch(pattern, argument, re.DOTALL) is None:
        return False
    # A part that IS the package placeholder is a path into the exported tree, and a path is read by
    # more tools than a selector is. `pytest` treats `@file` as a list of further options and
    # selectors to apply, so a repository owning a file called `@opts.py` would be naming flags
    # rather than a file — and these same arguments are what the generated scorer runs in the lane,
    # which is shared. Absolute paths and `..` are refused for the same reason the order id is: the
    # runner holds its own boundary rather than trusting what composed the order.
    return part != "{package}" or _within_the_tree(argument)


def _within_the_tree(argument: str) -> bool:
    """Whether a path argument stays inside the exported tree and names a path rather than a flag."""
    if argument.startswith(("-", "@", "/", "~")):
        return False
    return not any(segment == ".." for segment in PurePosixPath(argument).parts)


def _expand(command: str) -> list[str]:
    """Split the declared test command, expanding the placeholders a runner is allowed to fill.

    `_host_target` shells out to `rustc`, so it is resolved only when a part actually carries the
    placeholder. Resolving it unconditionally would make every Go or Python probe depend on a Rust
    toolchain being installed.
    """
    parts = shlex.split(command)
    if not any("{host_target}" in part for part in parts):
        return parts
    triple = _host_target()
    return [part.replace("{host_target}", triple) for part in parts]


class Runner:
    """Executes orders against one checkout, materializing each order in its own directory."""

    def __init__(
        self,
        repo: Path,
        workspaces: Path,
        test_command: str,
        env: dict[str, str],
        setup_command: str | None = None,
        secret_names: tuple[str, ...] = (),
        filter_template: str = "",
    ) -> None:
        """
        Args:
            repo: The local checkout orders export trees from.
            workspaces: Directory each order's workspace is created beneath.
            test_command: The repository's declared command — the allow-list probes are bound to.
            setup_command: The repository's declared command for making a checkout testable, if any.
            env: Extra environment for probes (a shared `CARGO_TARGET_DIR` keeps builds incremental
                across candidates, which is the difference between minutes and hours).
            filter_template: The language contract's own argument template. Probe arguments are
                checked against it, so the service can only ask for a test by name. Empty means the
                repository declared a language with no filter, and no probe arguments are accepted.
            secret_names: Which names in `env` came from `forward_env` — the secret-bearing half of
                the declaration. Their values are taken out of every output tail before it crosses
                back to the service, whatever their length.
        """
        self.repo = repo
        self.workspaces = workspaces
        self.test_command = test_command
        self.setup_command = setup_command
        self.filter_template = filter_template
        self.env = env
        self._test_environment = _without_the_runners_credentials(os.environ, env)
        # The values of the names `forward_env` carried, at any length, and nothing else. Length was
        # the wrong test: `forward_env` is the secret-bearing half of the declaration, so a short
        # value there is a short credential, while a long one in `env` is a long build flag.
        # Longest first, so a value that contains another is replaced before its substring is.
        self._secrets = tuple(sorted(
            {env[name] for name in (secret_names or ()) if env.get(name)}, key=len, reverse=True))
        self._runner_deadline = time.monotonic() + _MAX_RUNNER_SECONDS
        self._deadline = self._runner_deadline

    def run(self, order: WorkOrder) -> OrderResult:
        """Carry out one order, reporting each step's exit code and nothing more.

        Returns:
            The step results, or an `OrderResult` carrying `error` when the order could not be
            carried out at all. A failure to run is never reported as a step verdict.
        """
        if not _ORDER_ID.match(order.order_id):
            # The order id names a directory this method creates and then deletes. An id like
            # `../something` would put that directory — and the deletion — outside the workspace
            # root, in the customer's own checkout. The service is not trusted to be well behaved.
            return OrderResult(order_id=order.order_id, steps=[], error="malformed order id")
        if len(order.steps) > _MAX_STEPS_PER_ORDER:
            # The runner's own bound on what one order may cost its CI. Each probe may run for an
            # hour, so the number of them is the number that matters, and the service composing
            # them is the party this runner does not assume is well behaved.
            return OrderResult(order_id=order.order_id, steps=[],
                               error=f"order carries {len(order.steps)} steps; at most {_MAX_STEPS_PER_ORDER}")
        workspace = self.workspaces / order.order_id
        results: list[StepResult] = []
        self._deadline = min(time.monotonic() + _MAX_ORDER_SECONDS, self._runner_deadline)
        try:
            for step in order.steps:
                results.append(self._step(step, workspace))
        except subprocess.TimeoutExpired as failure:
            # `run` promises an OrderResult carrying `error`. A timeout in export or setup would
            # otherwise leave the caller with an uncaught exception instead of a reported failure.
            if time.monotonic() >= self._deadline:
                # The step was cut short because the order's budget ran out, not because the step
                # itself was slow. Reporting its truncated deadline would name the wrong bound.
                return OrderResult(order_id=order.order_id, steps=results,
                                   error="the order ran out of time")
            return OrderResult(order_id=order.order_id, steps=results,
                               error=f"a step timed out after {failure.timeout:.0f}s")
        except OrderError as failure:
            return OrderResult(order_id=order.order_id, steps=results, error=str(failure))
        except OSError as failure:
            # A setup or test command naming an executable this runner does not have. `run` promises
            # an `OrderResult` carrying `error`; without this the caller gets a traceback and the
            # whole suite stops on one repository's misdeclared command.
            return OrderResult(order_id=order.order_id, steps=results,
                               error=f"a step could not be started: {failure}")
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        return OrderResult(order_id=order.order_id, steps=results)

    def _within_deadline(self, timeout: float) -> float:
        """This step's deadline, cut to what is left of the order's and the runner's.

        Raises:
            OrderError: If neither budget has any time left.
        """
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise OrderError("the order ran out of time before this step")
        return min(timeout, remaining)

    def _step(self, step: Step, workspace: Path) -> StepResult:
        started = time.monotonic()
        if step.op == "export":
            self._export(step, workspace)
            return self._result(step, 0, started, "")
        if step.op == "setup":
            return self._setup(step, workspace, started)
        if step.op == "apply":
            return self._apply(step, workspace, started)
        if step.op == "probe":
            return self._probe(step, workspace, started)
        raise OrderError(f"unknown step op {step.op!r}")

    def _export(self, step: Step, workspace: Path) -> None:
        """Materialize a commit's tree into a clean directory.

        A tree export, not a clone: the workspace gets no history and no remote, so nothing in it can
        be used to look up how the change was actually made. mo-eval's own t-suite materializer takes
        the same approach, for the same reason.
        """
        if step.commit is None:
            raise OrderError("export step carries no commit")
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True)
        _export_into(self.repo, commit_or_refuse(step.commit),
                     workspace, self._within_deadline(_EXPORT_TIMEOUT_SECONDS))
        _freshen(workspace)
        _own_repository(workspace, self._within_deadline(_LOCAL_COMMAND_SECONDS))

    def _setup(self, step: Step, workspace: Path, started: float) -> StepResult:
        """Make the exported checkout testable, using the command the REPOSITORY declared.

        The step carries no command and cannot: the service asks for setup and the runner decides
        what that means. A repository that declares none is assumed to build from its source, which
        is true of every compiled language here and of none of the interpreted ones.
        """
        if self.setup_command is None:
            return self._result(step, 0, started, "no setup command declared")
        exit_code, tail, _, _ = _bounded_output(
            shlex.split(self.setup_command), cwd=workspace,
            env=self._test_environment, timeout=self._within_deadline(_SETUP_TIMEOUT_SECONDS),
        )
        return self._result(step, exit_code, started, tail)

    def _apply(self, step: Step, workspace: Path, started: float) -> StepResult:
        if step.patch is None:
            raise OrderError("apply step carries no patch")
        patch_file = workspace / ".mo-eval-order.patch"
        # Written through O_NOFOLLOW after unlinking: the workspace holds an exported repository,
        # and a repository may contain a file of this name — as a symlink to anywhere. `write_text`
        # would follow it and overwrite the target.
        patch_file.unlink(missing_ok=True)
        try:
            descriptor = os.open(patch_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError as failure:
            raise OrderError(f"could not write the patch: {failure}") from failure
        with os.fdopen(descriptor, "w") as handle:
            handle.write(step.patch)
        completed = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch_file)],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=self._within_deadline(_APPLY_TIMEOUT_SECONDS),
        )
        patch_file.unlink(missing_ok=True)
        tail = (completed.stdout + completed.stderr)[-_OUTPUT_TAIL_BYTES:]
        return self._result(step, completed.returncode, started, tail)

    def _probe(self, step: Step, workspace: Path, started: float) -> StepResult:
        """Run the repository's declared test command with the service's arguments appended."""
        if step.args is None:
            raise OrderError("probe step carries no args")
        arguments = self._filter_or_refuse(step.args)
        directory = self._resolve(workspace, step.cwd)
        # Before `_expand`, which may shell out to `rustc` for the host triple: rendering the
        # command is work too, and an exhausted budget should stop it rather than fund one more
        # process.
        budget = self._within_deadline(_PROBE_TIMEOUT_SECONDS)
        command = [*_expand(self.test_command), *arguments]
        try:
            exit_code, tail, seen, failed = _bounded_output(
                command, cwd=directory, env=self._test_environment, timeout=budget,
                proof=step.proof, counterproof=step.counterproof,
            )
        except subprocess.TimeoutExpired:
            if time.monotonic() >= self._deadline:
                # The order's budget ran out mid-probe. Reported as a step that timed out, it would
                # read as a test that hung — a verdict about the repository rather than about this
                # runner having stopped, and `run` would report no error at all.
                raise
            # A probe that did not finish proves nothing, and says so rather than leaving the
            # service to read a timeout's exit code as a test that failed. Reported only where a
            # pattern was carried, so a runner asked nothing still answers nothing.
            return self._result(step, 124, started, "probe timed out",
                                proof_seen=False if step.proof else None,
                                counterproof_seen=False if step.counterproof else None)
        return self._result(step, exit_code, started, tail, seen, failed)

    def _filter_or_refuse(self, arguments: list[str]) -> list[str]:
        """Probe arguments, confirmed to be a test filter and not a different command.

        The module's claim is that the service chooses which tests run and never what runs them,
        and appending whatever it sent does not hold that: a test tool's arguments include
        execution controls as well as selectors — `go test -exec <program>` names the program that
        runs the test binary, in a CI job with the customer's credentials in it. So the arguments
        are matched against the shape this repository's own language contract produces: the literal
        parts have to be exactly the contract's, and the parts filled from a test's name or package
        may be anything that is not another option.

        Raises:
            OrderError: If the arguments are not ones this contract's filter could have produced.
        """
        expected = shlex.split(self.filter_template)
        scope = 2 if arguments[:1] == ["-p"] and len(arguments) == len(expected) + 2 else 0
        if scope:
            # Cargo's `-p <package>`, which the service prepends to the template's own arguments.
            if arguments[1].startswith("-"):
                raise OrderError(f"probe argument {arguments[1]!r} is not a package")
        if len(arguments) - scope != len(expected):
            raise OrderError(
                f"probe carries {len(arguments)} argument(s); this language's filter takes {len(expected)}")
        for argument, part in zip(arguments[scope:], expected):
            if not _fills(part, argument):
                raise OrderError(f"probe argument {argument!r} is not {part!r} filled in")
        return arguments

    def _resolve(self, workspace: Path, relative: str | None) -> Path:
        """Resolve a probe's working directory inside the workspace.

        A directory the service names is still a path the runner validates. Anything resolving
        outside the exported tree is refused rather than clamped, so a traversal is an error the
        operator sees instead of a silent relocation.

        Raises:
            OrderError: If the path escapes the workspace or does not exist.
        """
        if relative is None or relative == ".":
            return workspace
        resolved = (workspace / relative).resolve()
        if not resolved.is_relative_to(workspace.resolve()):
            raise OrderError(f"probe directory escapes the workspace: {relative!r}")
        if not resolved.is_dir():
            raise OrderError(f"probe directory does not exist: {relative!r}")
        return resolved

    def _result(self, step: Step, exit_code: int, started: float, tail: str,
                proof_seen: bool | None = None, counterproof_seen: bool | None = None) -> StepResult:
        return StepResult(
            step_id=step.step_id,
            exit_code=exit_code,
            duration_seconds=round(time.monotonic() - started, 2),
            output_tail=self._reported(tail),
            proof_seen=proof_seen,
            counterproof_seen=counterproof_seen,
        )

    def _reported(self, output: str) -> str:
        """The tail of a command's output, with the repository's forwarded secrets taken out.

        Redacted first and cut afterwards. Cutting first leaves whatever half of a value fell
        inside the window, and a credential with a known prefix is mostly its second half.
        """
        return self._redacted(output)[-_OUTPUT_TAIL_BYTES:]

    def _redacted(self, tail: str) -> str:
        """Command output with the repository's forwarded secrets taken out of it.

        `forward_env` carries a repository's own credentials into the environment its tests run in,
        which is the point — its tests need them. The output of those tests is the one thing that
        crosses back to the service, and a failing test that prints its environment, a stack trace
        that renders a client object, or a verbose HTTP log would carry the value with it. Every
        tail goes through here, so there is one place that has to be right rather than one per step.
        """
        for secret in self._secrets:
            tail = tail.replace(secret, _REDACTED)
        return tail


def _export_into(repo: Path, commit: str, workspace: Path, timeout: float) -> None:
    """Stream a commit's tree out of `repo` and unpack it into `workspace`.

    The producer is held in its own session so a deadline takes it and not only the extraction it
    feeds, and both its pipe and the process itself are released whether the extraction succeeded,
    failed or ran out of time.

    Raises:
        OrderError: If either side fails, or the producer does not finish.
        subprocess.TimeoutExpired: If the extraction does not finish.
    """
    archive = subprocess.Popen(["git", "archive", "--format=tar", "--", commit], cwd=repo,
                               stdout=subprocess.PIPE, start_new_session=True)
    group = os.getpgid(archive.pid)
    try:
        extract = subprocess.run(["tar", "-xf", "-", "-C", str(workspace)], stdin=archive.stdout,
                                 capture_output=True, text=True, timeout=timeout)
    finally:
        if archive.stdout is not None:
            archive.stdout.close()
        try:
            # The producer's own failure is reported below; here it is only made to stop. It is
            # writing into a pipe nobody reads any more, so it ends on its own or it is ended.
            archive.wait(timeout=_GROUP_EXIT_SECONDS)
        except subprocess.TimeoutExpired:
            _end_group(group)
            archive.wait()
    if archive.returncode != 0:
        raise OrderError(f"could not read {commit} out of the repository")
    if extract.returncode != 0:
        raise OrderError(f"could not export {commit}: {extract.stderr[-400:]}")


def _own_repository(workspace: Path, timeout: float = _LOCAL_COMMAND_SECONDS) -> None:
    """Make an exported tree its own Git repository, so `git apply` resolves paths against IT.

    Run inside a repository, `git apply` takes patch paths relative to that repository's root and
    silently ignores any that fall outside the current directory — exit 0, nothing applied. A
    workspace under `.mo-eval/out/` inside a customer's checkout is exactly that case: on GitHub
    Actions every scaffold "applied" with exit 0 and every graded test was then "not found" at the
    start state, which the judge read as "already passes". An empty repository in the workspace
    stops discovery at the workspace boundary.
    """
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True, capture_output=True,
                   timeout=timeout)


def _freshen(workspace: Path) -> None:
    """Stamp every exported file with the current time.

    `git archive` gives each file its COMMIT's timestamp, so an exported tree from last week looks
    older than any build artifact produced since. Cargo decides a path dependency is fresh by
    comparing mtimes, so it then skips recompiling and links this tree's crate against a `.rmeta`
    built from a different commit's source. The result is a compile error that belongs to neither
    tree — observed here as `missing fields ... in initializer of RecordedFact`, from a candidate
    whose reference state builds perfectly on its own.

    That failure is silent in the worst way: it does not look like a bug, it looks like a candidate
    that failed validation, so a real task is discarded and nothing anywhere reports a problem.

    The cost is a full rebuild per exported tree, which is the honest price of a correct verdict.
    Sharing one target directory across trees is the thing that was wrong, not the rebuild.
    """
    for path in workspace.rglob("*"):
        try:
            os.utime(path, None, follow_symlinks=False)
        except OSError:
            continue
