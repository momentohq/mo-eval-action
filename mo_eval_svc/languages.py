"""What each language does with its tests. Shared by both sides, like `wire.py`.

The runner needs this to detect which framework a repository uses (it holds the test files) and to
name the contract it found; the service needs it to split, scope and grade. Neither side may reach
into the other, so the table lives at the boundary where both can see it. The contracts themselves
carry nothing sensitive — how `pytest` names a test is not a secret.

The miner needs four things from a language, and only the last one is hard: which files are
implementation, which files are wholly tests, how a test function announces itself, and — for the
languages that keep unit tests inside the file they test — how to find the boundary.

Only Rust needs that fourth answer, which is why it was built first. Everywhere else the split falls
out of the path, and `split.py` already has that branch.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Language:
    """How one language's repositories are shaped."""

    name: str
    source_suffixes: tuple[str, ...]
    """Extensions that count as implementation."""
    test_path: re.Pattern[str]
    """A path whose whole content is tests. The split for these is the path itself."""
    test_declaration: re.Pattern[str]
    """How a test function is introduced, for counting what a change actually added."""
    test_name: re.Pattern[str]
    """Matches a test declaration; the name is the FIRST GROUP THAT CAPTURED, searched from the
    declaration line forward.

    Not group 1 specifically: a language with more than one way to declare a test needs one
    alternative per form, and each brings its own group — Ruby has both `it "name"` and
    `def test_name`. Callers read `next(group for group in match.groups() if group)`.

    Separate from `test_declaration` because the two are not always on the same line: an annotation
    language puts `@Test` above the signature, while Go and Python name the test in the declaration
    itself. Searching a short window from the declaration covers both without a second table.
    """
    inline_tests: bool
    """Whether unit tests live inside the files they test, requiring a syntactic boundary."""
    test_command: str
    """What the repository would declare in `.mo-eval/config.toml` — the allow-list a probe appends to."""
    filter_template: str
    """Arguments that run exactly one named test. `{name}` is the test; `{package}` is the directory
    of the file that declares it, as a `./dir` path (`.` at the root) — for ecosystems where a test
    command otherwise compiles and runs every package to find one."""
    ran_a_test: str
    """Regex proving at least one test actually EXECUTED, with `{name}` available.

    A LINE pattern. Matched against the probe's output compiled `MULTILINE`, and against the same
    output by `grep -E` in the offline scorer, so `^` and `$` mean the ends of a line in both and a
    construct only one engine knows — a lookahead — passes validation and then fails every task in
    the lane.

    Load-bearing, and different in every ecosystem. `cargo test` and `go test` both exit **zero** when
    a filter matches nothing, so an oracle reading only the exit code would grade a task that cannot
    be failed. `pytest` exits 5 and Gradle fails outright, so there the exit code is enough — but the
    pattern is still declared, because "this language happens to be safe" is not something to leave
    implicit in a scorer.
    """
    name_is_regex: bool = False
    """Whether the runner reads `{name}` as a regular expression rather than as a literal.

    Ginkgo's `--ginkgo.focus` does; Go's `-run` does too but the template anchors it. A description
    containing `(`, `[`, `+` or `.` would otherwise match a different test, or none — and a probe
    that matches nothing is a task that cannot be failed."""
    package_scoped: bool = False
    """Whether the test command needs a build unit named (Cargo's `-p`), rather than running from the
    repository root."""
    offline_prepare: str | None = None
    """A command the runner executes in a task's start tree, before it is frozen, so that setup and
    scoring succeed with NO network: mo-eval's baseline and scorer workers run with none. For Go that
    is `go mod vendor`, after which `go test` reads `vendor/` by default. A language without such a
    command relies on the worker image to carry the dependencies."""
    declaration: re.Pattern[str] | None = None
    """How ANY top-level declaration is introduced — a superset of `test_declaration`.

    Where a test's body ends. Attribution walks a file assigning each line to the nearest preceding
    test, and without this it never stops: a `func Benchmark…` following a test is not a test
    declaration, so every line of it — and of everything after it, up to the next test — is charged
    to the test above. Measured on `gin`, that put five of eleven mined tasks' graded lists in the
    position of naming a test the change never touched, while the prompt told the agent each one
    failed on its tree.

    Empty for a language nobody has measured, which keeps exactly today's behaviour for it rather
    than guessing a pattern — the rule this table already follows for every other measured field.
    """
    offline_artifacts: tuple[str, ...] = ()
    """Paths `offline_prepare` produces that the repository's own `.gitignore` will typically ignore
    (`vendor/`). They must be force-tracked in the start commit: mo-eval's snapshotter freezes
    tracked and non-ignored files only, so an ignored `vendor/` silently never reaches the
    workers — and the baseline "fails" on a module fetch instead of on the task."""
    failed_a_test: str = ""
    """Regex that is evidence the NAMED test did not pass, with `{name}` available. A line pattern,
    like `ran_a_test`, and matched the same two ways.

    Read only alongside two other answers — the command failed, and `ran_a_test` did NOT match — so
    what it has to carry is the test's IDENTITY, not a verdict. A verdict line is the obvious way to
    carry one and is what most ecosystems give. It is not the only way: `c` declares gtest's
    `[ RUN      ]` START line, because a C test that segfaults never reaches a verdict and a
    contract watching for one would reject the flip. Whatever the line, it must name the test —
    anything weaker re-admits the case below, where a neighbour failed and this test never ran.

    The start state is where a task's claim lives: a test that fails there and passes at the
    reference is the flip the mining looks for. Read from an exit code alone that claim is wrong
    whenever the command failed for a reason that is not the test — a start tree that does not
    compile, or a neighbouring test the substring filter also selected. Both produce a non-zero exit
    with the named test never having run.

    `ran_a_test` cannot answer it: that pattern proves a test ran and PASSED, which is exactly what
    the start state should not do. Hence a second pattern, for what the ecosystem prints when the
    named test ran and failed.

    Empty for a contract whose failure wording has not been measured. The judge then reads the exit
    code, which is what it did before this existed — no better, and no worse.
    """
    vendoring_replaces_setup: bool = False
    """Whether vendoring makes the repository's setup command unnecessary rather than offline-able.

    `node_modules` IS the install: with it in the tree there is nothing left to do, and running
    `bun install --frozen-lockfile` offline fails outright — measured, `DNSResolveFailed downloading
    tarball color-name@1.1.3`, even with a complete `node_modules` present. Whereas `pip install -e .`
    also builds and links the project itself, so Python keeps its setup and is merely pointed at the
    wheels.

    Safe to decide from the contract alone: a bundle only exists for such a repository when something
    vendored, because a repository that declares a setup command and no vendoring is refused before a
    bundle is written.
    """
    resolves_dependencies_when_testing: bool = False
    """Whether running the tests itself reaches for dependencies, rather than a separate install step.

    `cargo test` and `go test` resolve and fetch on their own; `pytest` and `vitest` use what an
    install already put there. It decides whether a repository needs vendoring even when it declares
    no `setup_command` — `clap` declares none and still cannot be scored offline, because `cargo`
    goes to `index.crates.io` the moment the tests run.
    """
    offline_test_args: tuple[str, ...] = ()
    """Arguments that make the TEST command resolve from the vendored tree instead of a network.

    The mirror image of `offline_env`, and needed for the same reason by a different shape of
    toolchain: `offline_env` is prefixed to the SETUP command, which a language resolving its
    dependencies at install time always has. A language that resolves them as the tests run may
    declare no setup command at all — `clap` declares none — so there is nothing to prefix, and the
    telling has to ride on the command that does run.

    Cargo is the case, and arguments are what make it deliverable. The same setting exists in
    `.cargo/config.toml`, but a repository that already pins `[source.crates-io]` (a mirror, its own
    offline CI) cannot have a second one appended: the duplicate key is reported as a *manifest*
    error naming `Cargo.toml`, a file that is not at fault. It exists in the environment too, and a
    worker's manifest has nowhere to carry environment. An argument has neither problem, and takes
    precedence over the file when both are present — measured on a repository pinning an unreachable
    mirror, where the argument form runs the tests and the appended stanza fails to parse.
    """
    prepare_cache_env: dict[str, str] = field(default_factory=dict)
    """Where this toolchain keeps its download cache, relative to the preparation container's HOME.

    Every task of a suite resolves the same dependency closure, so the preparation container is
    given one shared directory as HOME and the closure is fetched once rather than once per task.
    HOME alone does not achieve that: an official image usually sets the toolchain's own variable,
    and then the cache lands outside HOME whatever HOME is. Measured in `golang:1-bookworm`, whose
    `GOPATH=/go` puts the module cache at `/go/pkg/mod` and leaves only the build cache under HOME:

        HOME=/tmp  go env GOMODCACHE -> /go/pkg/mod

    So each entry names the variable and a path under HOME for it to point at. Empty for a language
    nobody has measured, which costs only the sharing — the preparation still runs.
    """
    offline_env: dict[str, str] = field(default_factory=dict)
    """Environment that makes this toolchain resolve dependencies from the tree instead of a network.

    Prefixed to the setup command a worker runs, because workers score with no network and the
    dependencies are vendored into the start tree by then. Language knowledge rather than repository
    knowledge: `pip` is told with `PIP_NO_INDEX` and `PIP_FIND_LINKS` whatever a repository's install
    command happens to be, while WHICH command vendors them is the repository's to declare.

    Go needs none — `go test` reads `vendor/` on its own, which is why Go worked before any of this
    existed and why its absence here went unnoticed.
    """
    scope_is_test_file: bool = False
    """Whether `{package}` means the test's own FILE rather than the directory holding it.

    `pytest` collects everything it is pointed at before it filters, so a probe that names no path
    imports the whole suite to run one test. One unrelated file that cannot be imported then fails
    every task in the repository — measured on `sqlglot`, where three test modules needing an
    optional dependency ended the run at `3 errors during collection` while the task's own test was
    fine. Naming the file also narrows what `-k` can reach: the same probe collected three matching
    tests across the suite and two within the file.
    """
    command_marker: str = ""
    """A word in the repository's declared test command that selects this contract over its siblings.

    Detection is otherwise by counting test declarations, which cannot separate two frameworks that
    write tests the same way and differ only in what runs them: Jest and Vitest are both `it(` and
    `test(`, and a repository says which it uses by declaring `npx vitest run`. A contract naming a
    marker is chosen only when the marker is in that command, and never by counting."""
    runner_reported: str = ""
    """Regex proving the test RUNNER executed and reported, whatever any test's outcome was.

    Weaker than `failed_a_test` and available where that is not. The question the scorer needs
    answered on a non-zero exit is whether anything ran at all, and a by-name failure is one way to
    know — but only two contracts can print one. A runner's own summary line answers the same
    question without naming a test, so a contract with no failure wording can still tell a broken
    environment from a failed task.

    No `{name}`: this is about the runner, not about one test. A toolchain that compiled the
    repository's own code and refused it has reported — Go's `[build failed]`, cargo's `could not
    compile` — which is what lets the scorer grade a tree the agent left uncompilable as a failed
    task rather than an environment that could not run the test.

    Measured, not guessed, because these encode what a tool PRINTS. Vitest reports
    `Test Files  1 failed (1)` for a file whose import could not be resolved — nothing ran, and a
    pattern over that line would have claimed it did; its `Tests` line reads `no tests` in the same
    output, which is why that is the one named here. pytest prints `1 error in 0.07s` for a
    collection failure and `1 failed, 1 passed in 0.01s` when tests ran, and both a failing test and
    an absent pytest exit 1 — the exit code cannot separate them at all.

    Empty for a contract whose summary wording has not been measured; those keep grading on the exit
    code, as they always did."""
    build_failed_at: str = ""
    """Regex whose first group captures the repository path of a file the compiler refused, relative
    to where the test command ran. Read by the judge over the whole of a probe's tail under
    `MULTILINE`, so a diagnostic that spans lines can be anchored to its header; a newline in it is
    written as `\\n`, never reached through `\\s`, which would cross into the line before.

    A test that names what the change adds cannot run until the change exists, and in a compiled
    language that is a compile error rather than a failure by name. The path says whose failure it
    is. A file the scaffold wrote names the task; a source file, a dependency or a manifest names a
    tree that does not build for a reason no agent is asked to fix.

    Only the path, not the reason. Go's wording for a missing symbol and for a missing module has
    the same `file:line:col:` shape; `build_failed_summary` separates them, because only the first
    ends in `[build failed]`.

    Empty where compile errors have not been measured, and for an interpreted language, where the
    question does not arise: the test runs and fails by name. Declared together with
    `build_failed_summary`, never alone.
    """
    build_failed_summary: str = ""
    """Regex for the line the toolchain prints when it compiled the repository's own code and
    refused it. A line pattern, read by the judge line by line.

    The other half of `build_failed_at`. Go's wording for a missing symbol and for a missing module
    has the same `file:line:col:` shape, and only the summary tells them apart: `FAIL pkg [build
    failed]` is a compiler refusing this code, `FAIL pkg [setup failed]` is a module it could not
    fetch or a file it could not parse. `runner_reported` cannot stand in for it, because a
    repository declaring `go test ./...` prints `ok` for the packages that did build on the same
    run, and that says the toolchain ran, not that it compiled and refused the file at hand.
    """
    scorer_preamble: str = ""
    """Shell lines the generated scorer runs first — environment the language's toolchain needs that a
    scoring shell may not provide. Kept per language rather than per repository: it is a fact about
    the toolchain, not the code under test."""
    build_cache_variable: str = ""
    """Environment variable pointing the toolchain's build cache at a directory, rendered into a
    bundle's manifest so mo-eval seeds it once per task and hands every grading worker a private
    writable clone of it (mo-eval 0.15.2+; older images reject the field). Empty for a toolchain
    nobody has measured, which then compiles in each worker as before.

    Only for a cache addressed by its inputs, where a stale or foreign entry is a miss and never a
    wrong answer. Go's `GOCACHE` is; a Cargo `target/` directory is not, and is left out."""
    build_cache_seed: str = ""
    """Command that fills that cache from the start tree: compiles the code and its tests and runs
    none. Running any would store results a grading worker could read back. Declared together with
    `build_cache_variable`, never alone."""


def is_source_path(path: str, language: Language) -> bool:
    """Whether a changed file is one the splitter will look at.

    The runner asks this before it reads a blob, and the service asks it again before it diffs one.
    Spelled once because those two answers must be the same: a runner that sent less than the
    service keeps would withhold part of a change from its own split, and one that sent more would
    ship a customer's files to a service that discards them unread.

    Path and contract only — no content — so the runner can apply it to the facts it already holds.

    Returns:
        Whether the path matches a source suffix or test path in the language contract.
    """
    return path.endswith(language.source_suffixes) or bool(language.test_path.search(path))


def offline_test(test_command: str, language: Language) -> str:
    """A test command as a worker must run it: told to resolve from the tree, not from a network.

    The same contract-supplied telling `offline_setup` gives a setup command, for a toolchain that
    resolves when the tests run. Appended rather than prefixed because what it supplies is arguments,
    and the repository's own command is not rewritten — only extended.

    Lives here beside `offline_setup` because three renderings of one bundle need the same string —
    the scorer, the acceptance lines a prompt quotes, and the whole-suite command the conventions
    judge runs — and a bundle whose scorer resolves offline while its prompt's commands do not is a
    task an agent cannot work on.

    Empty for a contract that needs no telling, which is every one but Rust today.

    Returns:
        The test command with offline arguments appended when the contract requires them.
    """
    if not language.offline_test_args:
        return test_command
    return " ".join([test_command, *(shlex.quote(argument) for argument in language.offline_test_args)])


def offline_setup(setup_command: str, language: Language) -> str:
    """A setup command as a worker must run it: told to resolve from the tree, not from a network.

    A worker scores with `--network none` and the dependencies were vendored into the start tree
    before it was frozen, so the install has to be pointed at them. Which variables do that is a
    fact about the toolchain, so it comes from the contract; the command itself is the repository's
    and is not rewritten — only prefixed, so what runs is still what the repository declared.

    Lives here rather than beside the manifest it is written into, because two callers need the same
    string: the service, rendering a bundle's `setup:` line, and the runner, running that bundle's
    scorer in its own image before shipping it. A second spelling would let the check pass under an
    environment the worker will not have.

    Empty for a contract that needs no telling. `go test` reads `vendor/` by itself, which is why Go
    scored offline long before any of this was written.

    Returns:
        The setup command prefixed with exports for the contract's offline environment, or
        unchanged when none are required.
    """
    if not language.offline_env:
        return setup_command
    # `export …;` rather than a `VAR=x command` prefix. A prefix binds to ONE command, and a setup
    # command is not always one: `cd sub && pip install -e .` would apply the variables to `cd` and
    # leave `pip` reaching for a network that is not there. A manifest's setup runs under `/bin/sh
    # -lc`, so an export reaches every part of whatever the repository declared.
    exported = " ".join(
        f"{name}={shlex.quote(value)}" for name, value in sorted(language.offline_env.items())
    )
    return f"export {exported}; {setup_command}"


def _pattern(source: str) -> re.Pattern[str]:
    """Compile a contract pattern.

    MULTILINE matters: these patterns are applied both line by line (when naming the tests a change
    wrote) and against whole files (when detecting which framework a repository uses). Without it a
    leading `^` anchors to the start of the entire file, so every framework scores zero and detection
    silently falls back to the default — which is how Go kept being read as standard-library tests.

    Returns:
        A case-insensitive pattern whose anchors match individual lines.
    """
    return re.compile(source, re.IGNORECASE | re.MULTILINE)


LANGUAGES: dict[str, Language] = {
    "rust": Language(
        name="rust",
        source_suffixes=(".rs",),
        # A Rust test lives in `tests/`, in a file named for tests (`src/parser_tests.rs`,
        # `src/tests.rs`), or inline behind `#[cfg(test)]`. A file of the middle kind carries no
        # inner test module, so a contract naming only the directory reads its changes as
        # implementation and then reports that the change wrote no test at all.
        test_path=_pattern(r"(^|/)tests?/|(^|/)tests\.rs$|_tests?\.rs$"),
        test_declaration=_pattern(r"#\[(?:\w+::)*test\]"),
        test_name=_pattern(r"fn\s+(\w+)"),
        inline_tests=True,
        test_command="cargo test --target {host_target}",
        # `cargo test <name>` filters by substring and cargo names no exact-match flag that works on
        # a bare function name, so the filter stays a substring and the PROOF carries the identity:
        # libtest prints one line per test, and `test roff::required_group ... ok` names which of
        # the six tests that substring ran. Counting could not — `required_group` also selects
        # `required_group_with_required_option`, so `clap` reported two passes and the probe read as
        # a test that never ran. The optional path segment is what libtest prefixes for a test in a
        # module, and `- should panic` is what libtest appends for a `#[should_panic]` test that
        # passed. Written `(\S+::)?` rather than a bracket class: `[^ ]` includes a newline in
        # Python and not in `grep`, and the offline scorer matches this same pattern with `grep -E`
        # — which also has no `(?:`, so the group is a capturing one.
        #
        # Two things it does not settle. A same-named test in another module of the same package
        # satisfies it, because a bare name is not an identity (#3973); the count it replaces
        # refused that case instead of grading it, so this trades a lost task for a possibly
        # misattributed one, and the identity work is what closes it. And a repository that declares
        # `--nocapture` interleaves a test's own output between the name and its status, which no
        # line pattern can follow.
        filter_template="{name}",
        resolves_dependencies_when_testing=True,
        # `cargo vendor` alone, and the stanza it prints written ONLY when the repository has no
        # cargo config of its own. Appending to one that exists is what breaks: a repository already
        # pinning `[source.crates-io]` gets a duplicate key, which cargo reports as a manifest error
        # pointing at `Cargo.toml` — the wrong file, so the reader looks in the wrong place.
        #
        # Where the file is written, an agent's own `cargo test` resolves from the tree too, which a
        # coding agent needs: it iterates by running the tests. Where it is not, `offline_test_args`
        # still carries the scorer, the acceptance commands and the conventions judge, so the bundle
        # grades correctly and only an ad-hoc invocation reaches for a network.
        #
        # `cargo vendor` ignores the repository's own source replacement, measured: it vendored from
        # crates.io on a repository pinning an unreachable mirror, where `--respect-source-config`
        # spent its retries on `Could not resolve host`.
        #
        # A crate with no dependencies is the case the missing-artifact exemption turns on, and this
        # survives it: cargo writes no `vendor/`, and prints "There is no dependency to vendor in
        # this project." to STDERR — so the config this redirects stdout into is left empty, which is
        # valid TOML and leaves cargo working. Absence here means there was nothing to vendor, which
        # is the condition that exemption requires.
        offline_prepare=(
            "if [ -e .cargo/config.toml ] || [ -e .cargo/config ]; then cargo vendor >/dev/null; "
            "else mkdir -p .cargo && cargo vendor > .cargo/config.toml; fi"
        ),
        # `.cargo` as well as `vendor`, because the config written above is what makes the vendored
        # directory findable, and a repository ignoring either would ship a tree that has the crates
        # and cannot resolve them.
        offline_artifacts=("vendor", ".cargo"),
        offline_test_args=(
            "--config",
            'source.crates-io.replace-with="vendored-sources"',
            "--config",
            'source.vendored-sources.directory="vendor"',
        ),
        # The same login-shell trap Go hits: `sh -lc` sources /etc/profile, which resets PATH to the
        # Debian default and drops `/usr/local/cargo/bin` — so `rustc` and `cargo` are "not found"
        # in the very image that ships them, and a task fails for a reason that is not the task.
        # Measured in `rust:1.94-bookworm`: `rustc: command not found`, scorer exit 127.
        scorer_preamble=(
            "command -v cargo >/dev/null 2>&1 || export "
            'PATH="$PATH:/usr/local/cargo/bin:/usr/local/rustup/bin"'
        ),
        ran_a_test=r"^test (\S+::)?{name}( - should panic)? \.\.\. ok$",
        failed_a_test=r"^test (\S+::)?{name}( - should panic)? \.\.\. FAILED$",
        # Measured on a scratch crate. `test result: ok.` / `test result: FAILED.` once tests ran;
        # `error: could not compile `crate` (lib test) due to 1 previous error` when the test
        # target did not build, which for an additive change is the start state's own failure
        # (#4224). `error: no matching package named` and `cargo: command not found` name the
        # environment and match neither. What this cannot separate is a compiler that fails for the
        # environment's sake — a missing linker also ends in `could not compile` — and #4022's
        # packaging-time scorer run is the check for that.
        runner_reported=r"^(test result: |error: could not compile )",
        # `error[E0425]: …` then `--> tests/it.rs:1:52` for an integration test, `--> src/lib.rs:4:49`
        # for an inline one. Anchored to the `error` header on the line before, because rustc puts
        # the same `-->` under a warning, and a parent with an unused import warns on every run.
        # `--> Cargo.toml:7:2` (a manifest error) is deliberately not a `.rs` file.
        build_failed_at=r"^error(?:\[E[0-9]+\])?: [^\n]*\n[ \t]*--> ([^\n:]+\.rs):[0-9]+:[0-9]+",
        build_failed_summary=r"^error: could not compile ",
        package_scoped=True,
    ),
    "go": Language(
        name="go",
        source_suffixes=(".go",),
        test_path=_pattern(r"_test\.go$"),
        test_declaration=_pattern(r"^func\s+(?:Test|Fuzz|Example)\w*\("),
        # Every Go top-level declaration, so a test's attribution stops at the next `func` rather
        # than running through an adjacent `Benchmark`/helper into the following test.
        declaration=_pattern(r"^func\s"),
        test_name=_pattern(r"func\s+((?:Test|Fuzz|Example)\w*)\s*\("),
        inline_tests=False,
        test_command="go test",
        resolves_dependencies_when_testing=True,
        filter_template="-v -run ^{name}$ {package}",
        ran_a_test=r"--- PASS: {name}\b",
        failed_a_test=r"--- FAIL: {name}\b",
        # Measured on gin (go1.25 on Actions, go1.27 locally). `ok pkg 0.4s`, `ok pkg 0.4s [no tests
        # to run]`, `FAIL pkg 0.4s` and `FAIL pkg [build failed]` all say `go test` ran on the
        # repository's own code — the last is a test binary it compiled and refused, which for an
        # additive change is the start state doing exactly what it should (#4224). `FAIL pkg
        # [setup failed]` is a module it could not resolve or a file it could not parse, and is left
        # out on purpose: that is the environment, not the task.
        runner_reported=r"^(ok|FAIL)\s+\S+\s+([0-9.]+s|\[build failed\])",
        # `./x_test.go:4:37: undefined: f` at the root; `sub/x_test.go:3:35: …` in a nested package,
        # relative to where `go test` ran.
        # A path begins in column one: `go test -v` indents what a test itself logs by four spaces.
        build_failed_at=r"^(?:\./)?([^ \t\n:][^\n:]*\.go):[0-9]+:[0-9]+: ",
        build_failed_summary=r"^FAIL\s+\S+\s+\[build failed\]",
        # A login shell (`sh -lc`, which mo-eval's local-suite workers use) sources /etc/profile,
        # which resets PATH to the Debian default and drops /usr/local/go/bin — so `go` is "not
        # found" in the very image that ships it, and a baseline "fails" for a reason that is not
        # the task. Re-adding the standard Go locations is harmless where they are already present.
        scorer_preamble='command -v go >/dev/null 2>&1 || export PATH="$PATH:/usr/local/go/bin:/go/bin"',
        # Measured on gin in `golang:1.26-bookworm` as uid 1000: `go test ./...` takes 33s cold and 3s
        # against a cache this seed filled, with no test result served from it.
        build_cache_variable="GOCACHE",
        build_cache_seed="go test -run '^$' ./...",
        offline_prepare="go mod vendor",
        offline_artifacts=("vendor",),
        # `GOPATH` moves the module cache, which is the download; `GOCACHE` already follows HOME.
        prepare_cache_env={"GOPATH": "go"},
    ),
    "java": Language(
        name="java",
        source_suffixes=(".java",),
        test_path=_pattern(r"(^|/)src/\w*test\w*/"),
        test_declaration=_pattern(r"@Test\b"),
        test_name=_pattern(r"(?:void|Object)\s+(\w+)\s*\("),
        inline_tests=False,
        test_command="./gradlew test",
        resolves_dependencies_when_testing=True,
        # `--tests` matches the WHOLE fully-qualified name, so `*.{name}` is anchored where
        # `*{name}*` is a substring: measured on a project holding `testParsesHeader` and
        # `testParsesHeaderWithCharset`, the old form ran both and the new one runs exactly one.
        # That mattered in the false-RED direction here rather than the false-green one #4209
        # opens with: `BUILD SUCCESSFUL` already needs every selected test to pass, so a neighbour
        # could not carry a failing task — but a failing NEIGHBOUR could fail a run whose named
        # test passed, and validation reads that as the task's own test failing.
        #
        # `--rerun` (the task-scoped one, Gradle 7.6+) because the proof is the build's verdict:
        # a second run over unchanged inputs prints `Task :test UP-TO-DATE` and `BUILD SUCCESSFUL`
        # having executed nothing at all. An older wrapper rejects the flag loudly, which costs a
        # task rather than grading one wrongly.
        filter_template="--rerun --tests *.{name}",
        # Sound only WITH the filter above, and this is the pairing #4209 asks for: Gradle prints
        # no per-test line for a pass, so the verdict is all there is — but the filter selects only
        # the named test, a filter matching nothing fails with `No tests found for given includes`
        # rather than succeeding vacuously, and `--rerun` denies it the up-to-date shortcut.
        ran_a_test=r"BUILD SUCCESSFUL",
        # A failure IS named, with no reporter flag to ask for it: `HeaderTest > testParsesHeader()
        # FAILED`. The leading `\b` is what refuses a suffix neighbour — there is no word boundary
        # inside `testParsesHeader` of `testParsesHeaderWithCharset` — and the parentheses are
        # optional because JUnit 5 prints them and JUnit 4 does not.
        failed_a_test=r"\b{name}(\(\))? FAILED",
        # `1 test completed, 1 failed` / `2 tests completed, 1 failed`. A compile failure prints
        # neither this nor a named FAILED line, which is what keeps a broken environment out of
        # both.
        runner_reported=r"[0-9]+ tests? completed",
    ),
    "kotlin": Language(
        name="kotlin",
        source_suffixes=(".kt",),
        test_path=_pattern(r"(^|/)src/\w*test\w*/"),
        test_declaration=_pattern(r"@Test\b"),
        test_name=_pattern(r"fun\s+`?([^`(]+?)`?\s*\("),
        inline_tests=False,
        test_command="./gradlew allTests",
        resolves_dependencies_when_testing=True,
        filter_template="--tests *{name}*",
        ran_a_test=r"BUILD SUCCESSFUL",
    ),
    "python": Language(
        name="python",
        source_suffixes=(".py",),
        test_path=_pattern(r"(^|/)tests?/|(^|/)test_[^/]*\.py$|_test\.py$"),
        test_declaration=_pattern(r"^\s*(?:async\s+)?def\s+test_\w+"),
        test_name=_pattern(r"def\s+(test_\w+)"),
        inline_tests=False,
        test_command="python -m pytest",
        # `-v` so pytest prints one node id per test, and the proof below names the node rather
        # than counting passes. `-k test_foo` also selects `test_foobar`, so an aggregate count
        # would let a task whose own test was deleted pass on its neighbour's result. The `\b`
        # is what separates them: `::test_foo` does not match `::test_foobar`.
        filter_template="-v {package} -k {name}",
        scope_is_test_file=True,
        # Verified in `python:3.12-bookworm` with `--network none`: with wheels vendored into the
        # tree, `pip install -e .[dev] pytest` resolves from them and the suite runs. Without it the
        # same command reaches for `setuptools` and the worker fails before an agent starts.
        offline_env={"PIP_NO_INDEX": "1", "PIP_FIND_LINKS": ".mo-eval-wheels"},
        ran_a_test=r"::{name}\b.*PASSED",
        # Unanchored on purpose: `-q` prints `1 failed, 1 passed in 0.01s` while the default
        # reporter wraps the same counts in `=====`, and the repository's own command chooses which.
        runner_reported=r"[0-9]+ (passed|failed)",
        # No failure wording, so the start state is read from the exit code. `pytest` reports a
        # unittest subtest failure on its own lines and still prints `::{name} PASSED` for the test
        # that owns them, so both halves of a by-name reading are wrong here at once: nothing says
        # the test failed, and the line that says it passed is not true. Measured on `sqlglot`'s
        # `5dea55713`, whose `test_identity` fails fifteen subtests at the start state — a real task
        # that a by-name reading refuses. Tracked as #4057.
        failed_a_test="",
    ),
    "typescript": Language(
        name="typescript",
        source_suffixes=(".ts", ".tsx"),
        test_path=_pattern(r"\.(test|spec)\.tsx?$|(^|/)__tests__/|(^|/)test/"),
        test_declaration=_pattern(r"^\s*(?:it|test)\s*\("),
        test_name=_pattern(r"""(?:it|test)\s*\(\s*['"`]([^'"`]+)"""),
        inline_tests=False,
        test_command="npx jest",
        vendoring_replaces_setup=True,
        # `-t` is a regular expression over a test's full name — its `describe` blocks joined to
        # its own by spaces — so the name is escaped and anchored for the same reason Ginkgo's is:
        # unanchored, a task about `renders a list` is also graded by `renders a list of two`, and
        # a task whose own test is gone passes on its neighbour. Quoted in the template because it
        # is `shlex.split` before it is filled, and shlex would otherwise eat the backslash.
        filter_template=r"-t '(^|\s){name}$'",
        name_is_regex=True,
        # Both wordings: Jest prints `Tests:       1 passed, 1 total` and Vitest prints
        # `Tests  1 passed | 5192 skipped (5193)` — same filter flag, same anchoring, different
        # summary line. Measured against hono, which is Vitest; the colon alone made every probe
        # read as "no test ran", which rejects a whole repository for its reporter's punctuation.
        #
        # Reporter-agnostic on purpose, and it has to stay that way: the Vitest contract is an
        # ALTERNATE chosen only when the repository's test command contains the word `vitest`, so a
        # project running Vitest through `npm test` lands HERE. A Jest-shaped proof, or a Jest-only
        # flag in the filter, would break exactly those repositories to fix the others.
        #
        # `.*` before the count because `-t` does not SELECT tests in Jest — it skips the rest and
        # still counts them — so a filtered run reads `Tests: 2 skipped, 1 passed, 3 total` and the
        # count is not what follows the label. Anchored on `1 passed` as a whole word: it still
        # refuses `3 passed` (a filter that matched three tests proves nothing about one) and is not
        # satisfied by `11 passed` (#4197).
        ran_a_test=r"Tests:?\s.*\b1 passed\b",
        # Reads for both reporters too: Jest's `Tests: 0 total` and Vitest's `Tests  no tests` both
        # carry no passed/failed count, which is what says the runner reported nothing. Never
        # `Test Suites:`/`Test Files`, which report a failure for a suite that ran nothing.
        runner_reported=r"Tests:?\s.*[0-9]+ (passed|failed)",
    ),
    "c": Language(
        name="c",
        source_suffixes=(".c", ".h"),
        # A C project's unit tests are C++ files, because GoogleTest is: valkey's live in
        # `src/unit/test_*.cpp` and exercise `src/*.c`. The suffixes stay C on purpose — what a
        # change did to the C is what is graded, and a `.cpp` under the test path is a test by its
        # path rather than by its extension.
        # THREE shapes. A gtest file under `src/unit/` is a test by its directory; so is every C
        # file under a `tests/` tree, which is where a C project keeps the fixtures its integration
        # suite loads — valkey has fifty `tests/modules/*.c`, and a rule reading only the `.cpp`
        # shape charges each of them to the implementation half. The suffix is the third, and the
        # only one not measured here: valkey names its files `test_*.cpp` while Abseil and much of
        # Google's own C++ names them `*_test.cc`, and a repository of that shape whose tests were
        # read as implementation would report "change writes no test" for every candidate.
        #
        # Deliberately path-led rather than gtest-shaped, and that is load-bearing for the future:
        # `detect_framework` counts a variant's `test_declaration` over the files the BASE row's
        # `test_path` selected, so narrowing this to gtest's own spellings would make a Unity or
        # CMocka variant uncountable before it could be added.
        #
        # But SUFFIXED, unlike every other row's bare `(^|/)tests?/`, and that is what C costs.
        # `is_source_path` is "a source suffix OR this pattern", so a bare directory shape claims a
        # path whatever its extension, and `change_source` then reads the parent AND child blob of
        # each one and ships both — which is the saving that predicate exists for. Measured on
        # valkey: the bare shape claims 690 of 2043 tracked paths and 412 of those are neither C nor
        # C++ (272 `.tcl`, 66 `.sh`, 19 `.lua`, 12 binary `.rdb`), because a C repository keeps a
        # large script suite under `tests/` and vendors whole foreign test trees under `deps/`.
        # Across 300 commits that is ~146 KB per candidate read and sent for files nothing mines —
        # the Tcl suite is not gradeable today, so a candidate carrying only those is rejected
        # downstream for writing no test, after the bytes have already crossed.
        test_path=_pattern(r"(^|/)(unit|tests?)/.*\.(c|h|cpp|cc|cxx|hpp)$|_tests?\.(cpp|cc|cxx)$"),
        test_declaration=_pattern(r"^\s*TEST(?:_F|_P)?\s*\("),
        # A gtest test's identity is `Suite.Name`: two arguments of one macro, where this table can
        # carry exactly one captured group. The METHOD half is the one that is nearly unique —
        # measured over valkey's 897 tests, one method name is shared with another suite — so the
        # filter below re-attaches the suite with a `*` and the proof carries the whole identity.
        test_name=_pattern(r"TEST(?:_[FP])?\s*\(\s*\w+\s*,\s*(\w+)\s*\)"),
        inline_tests=False,
        # A skeleton rather than a command, because C has no ecosystem-wide one: there is no
        # `go test` here, and what builds a C project is the project's. Two parts of the shape are
        # the contract's, though, and both are load-bearing — which is why this is not left empty.
        #
        # The BUILD is inside it. An additive change's start state is a tree that does not compile,
        # and a build moved into `setup_command` reports that as an environment that could not be
        # prepared rather than as the task.
        #
        # And it is an `sh -c '…' $0`, not a script in the repository. A workspace is an export of
        # the candidate's PARENT commit and nothing overlays `.mo-eval/` into it, so a wrapper
        # committed on HEAD is absent and every order is refused with `[Errno 2] No such file or
        # directory` (#4568). SINGLE-quoted: the generated scorer pastes this into a bash script
        # verbatim (`service/build.py::_ACCEPTANCE_SCRIPT`), so a double-quoted body would have THAT
        # shell expand `$(…)` and `"$@"` before `sh` saw them, while the probe path — which execs
        # with no shell at all — would be unaffected. The probe's filter arrives as `"$@"` and
        # cannot reach the script text, which is what keeps the declaration an allow-list.
        test_command="sh -c 'set -e; make; exec ./build/unit-tests \"$@\"' mo-eval",
        # `--gtest_color=no` first, and from the contract rather than the repository: gtest colours
        # its own report when it believes it is being watched, and the escape lands INSIDE the
        # brackets — `\x1b[0;31m[  FAILED  ] \x1b[m` — where no line pattern below can see it.
        # Measured: `make test-unit` (which runs the binary under `gtest-parallel`) prints exactly
        # that, and the same binary run directly prints none.
        #
        # `*.` because the name is the method half. It is a glob, not a substring: gtest matches a
        # filter against the WHOLE `Suite.Name`, so `*.ParseSubnetIpv4` cannot also select
        # `ParseSubnetIpv4Extra` the way an unanchored substring would — only another suite's
        # identically named method, which the proof then separates.
        #
        # Nothing in `{name}` can be a filter metacharacter: `test_name` captures `(\w+)`, and
        # gtest reads `*`, `?`, `:` and a leading `-` (negation). `_fills` refuses a leading `-`
        # independently.
        #
        # A PARAMETERIZED test is not selectable and is not meant to be. `TEST_P` instantiates as
        # `Prefix/Suite.Method/0`, which `*.Method` does not match — measured, the filter selects
        # zero tests — so its probe proves nothing and the candidate is refused at validation
        # rather than graded. Selecting it would need `:*.{name}/*` in the filter AND a proof that
        # tolerates the `/0` suffix; with only the first, tests would run and prove nothing, which
        # is worse. valkey has 7 such instantiations against 897 tests.
        filter_template="--gtest_color=no --gtest_filter=*.{name}",
        # `[       OK ] AnetSubnetTest.ParseSubnetIpv4 (0 ms)`. Load-bearing, not decoration: a
        # filter matching nothing exits ZERO, printing `[==========] 0 tests from 0 test suites
        # ran.` — so an oracle reading the exit code alone would grade a task that cannot be failed.
        #
        # `( \(|$)` rather than a bare `\(`, because the duration is OPTIONAL: a repository
        # declaring `--gtest_print_time=0` prints `[       OK ] ExampleTest.TestAssertions` with
        # nothing after it (measured), and a pattern demanding the duration reports that a test
        # which plainly passed never ran — rejecting every task in the repository for a reporter
        # flag. The alternation still refuses a longer neighbour: after `{name}` in
        # `…] Suite.NameExtra (0 ms)` comes `E`, which is neither a space nor the end of the line.
        ran_a_test=r"^\[\s+OK\s+\] \S+\.{name}( \(|$)",
        # The START of the test, not gtest's `[  FAILED  ]` line — because in C a failing test
        # very often does not reach one. Measured on valkey-io/valkey#4312, whose task is a signed-overflow
        # fix: at the start state the test SEGFAULTS, gtest's last output is `[ RUN      ]
        # VsetTest.TestVsetLargeExpiryBucketOverflow` and the command exits 139. A pattern over
        # `[  FAILED  ]` sees nothing there and rejects a flip that is exactly the one mining looks
        # for — and a crash is the normal shape of a C bug's test, not an edge case.
        #
        # Sound because of the three things `_failed_a_test` requires together: the command failed,
        # THIS pattern appeared, and `ran_a_test` did NOT. A test that started and did not pass, on
        # a command that failed, failed — whether it printed a verdict, crashed, or was killed. The
        # `$` is what stops `…] Suite.NameExtra` satisfying it, and an assertion failure satisfies
        # it too, since gtest prints `[ RUN      ]` before the test body either way.
        #
        # A compile failure has no `[ RUN      ]` line at all, which is the case this cannot admit
        # and `build_failed_at` below would — see there.
        failed_a_test=r"^\[\s+RUN\s+\] \S+\.{name}$",
        # `[==========] 1 test from 1 test suite ran. (0 ms total)` — printed for zero tests too,
        # and never printed at all when the build failed before the binary existed. That is the
        # question this answers: did the runner get as far as reporting.
        runner_reported=r"^\[==========\] [0-9]+ tests? from [0-9]+ test suites? ran\.",
        # The same colour suppression as `filter_template`, by the OTHER lever, because the flag
        # reaches only the renderings that append a filter. `Oracle.runnable_test_command` appends
        # none — it is the whole-suite command the conventions judge and the lane's litter probe
        # run — so without this those two read a coloured report with no pattern able to see it.
        # The scorer already treats colour as a general hazard and exports `NO_COLOR`/`FORCE_COLOR`
        # for Vitest; gtest reads neither.
        #
        # Measured, with a real pty and `TERM` set, because without either gtest does not colour at
        # all and every answer looks the same: unset it colours, `GTEST_COLOR=no` suppresses,
        # `--gtest_color=no` suppresses, and `--gtest_color=yes` BEATS `GTEST_COLOR=no` — which is
        # why the flag is declared too rather than this replacing it.
        scorer_preamble="export GTEST_COLOR=no",
        # No `build_failed_at`/`build_failed_summary`, and not for want of measuring. gcc's wording
        # is `test_sds.cpp:709:18: error: 'x' was not declared in this scope` and make's is
        # `make: *** [Makefile:226: test_sds.o] Error 1`, but neither path resolves. gcc spells it
        # relative to the directory make compiled in (`src/unit`) and ld spells it absolute, while
        # `_compiler_refusals` joins what it captures to `workspace_root` — `.` for every contract
        # that is not package scoped. Nothing matches a scaffolded path, and the rule then refuses
        # exactly the tasks it exists to admit.
        #
        # So an additive C change is not mined: its scaffolded test cannot compile, this contract's
        # `failed_a_test` needs a `[ RUN      ]` line the binary never printed, and `_failed_a_test`
        # reads the exit code only when `counterproof_seen is None`, which the runner sets only for
        # a contract carrying no counterproof at all. `judge` refuses it as a graded test that did
        # not run. Measured: three of four valkey validation orders ended there, every one a link
        # error. #4567 carries the fix.
    ),
    "csharp": Language(
        name="csharp",
        source_suffixes=(".cs",),
        test_path=_pattern(r"(^|/)[^/]*\.Tests?/|Tests?\.cs$"),
        test_declaration=_pattern(r"\[(?:Fact|Test|Theory)\]"),
        test_name=_pattern(r"(?:void|Task|async\s+Task)\s+(\w+)\s*\("),
        inline_tests=False,
        test_command="dotnet test",
        # `--logger console;verbosity=detailed` is what makes `dotnet test` print a line PER TEST.
        # Quoted because the template is `shlex.split` before it is filled and the semicolon would
        # otherwise end the argument.
        #
        # `~` is CONTAINS, and vstest offers no anchored form for a bare method name — an exact
        # `FullyQualifiedName=` needs the namespace and class, which a task's test name does not
        # carry. So the filter stays a substring and the PROOF carries the identity, exactly as
        # Rust's does for the same reason.
        filter_template="--logger 'console;verbosity=detailed' --filter FullyQualifiedName~{name}",
        # The test's own line, terminated by the duration the logger appends. Measured: a task named
        # `ParsesHeader` also selects `ParsesHeaderWithCharset`, and the count this replaced could
        # not tell them apart — so deleting the task's own test and leaving the neighbour passing
        # exited ZERO with `Passed: 1` and was graded resolved (#4209). The ` \[` is what stops
        # `ParsesHeader` being proven by `ParsesHeaderWithCharset`.
        ran_a_test=r"Passed ([^ ]*\.)?{name} \[",
        failed_a_test=r"Failed ([^ ]*\.)?{name} \[",
        # The run summary, which a filter matching nothing does not print at all: it exits zero
        # saying `No test matches the given testcase filter` and reports no counts.
        runner_reported=r"(Failed|Passed):\s+[0-9]+",
    ),
    "ruby": Language(
        name="ruby",
        source_suffixes=(".rb",),
        test_path=_pattern(r"(^|/)spec/|_spec\.rb$|(^|/)test/|_test\.rb$"),
        test_declaration=_pattern(r"^\s*(?:it\s+['\"]|def\s+test_)"),
        test_name=_pattern(r"""it\s+['"]([^'"]+)|def\s+(test_\w+)"""),
        inline_tests=False,
        test_command="bundle exec rspec",
        filter_template="-e {name}",
        ran_a_test=r"[1-9][0-9]* examples?, 0 failures",
    ),
    "php": Language(
        name="php",
        source_suffixes=(".php",),
        test_path=_pattern(r"(^|/)tests?/|Test\.php$"),
        test_declaration=_pattern(r"^\s*(?:public\s+)?function\s+test\w+|@test\b"),
        test_name=_pattern(r"function\s+(\w+)\s*\("),
        inline_tests=False,
        test_command="vendor/bin/phpunit",
        filter_template="--filter {name}",
        ran_a_test=r"OK \([1-9]",
    ),
    "dart": Language(
        name="dart",
        source_suffixes=(".dart",),
        test_path=_pattern(r"(^|/)test/|_test\.dart$"),
        test_declaration=_pattern(r"^\s*test\s*\("),
        test_name=_pattern(r"""test\s*\(\s*['"]([^'"]+)"""),
        inline_tests=False,
        test_command="dart test",
        filter_template="--plain-name {name}",
        ran_a_test=r"All tests passed",
    ),
    "swift": Language(
        name="swift",
        source_suffixes=(".swift",),
        test_path=_pattern(r"(^|/)Tests/|Tests?\.swift$"),
        test_declaration=_pattern(r"^\s*func\s+test\w+|@Test\b"),
        test_name=_pattern(r"func\s+(test\w+)\s*\("),
        inline_tests=False,
        test_command="swift test",
        filter_template="--filter {name}",
        ran_a_test=r"Executed [1-9][0-9]* test",
    ),
    "elixir": Language(
        name="elixir",
        source_suffixes=(".ex",),
        test_path=_pattern(r"(^|/)test/|_test\.exs$"),
        test_declaration=_pattern(r'^\s*test\s+["\']'),
        test_name=_pattern(r'test\s+"([^"]+)"'),
        inline_tests=False,
        test_command="mix test",
        # `mix test --only` filters TAGS, not descriptions: `--only handles invalid input` selects
        # nothing, so every probe would prove nothing and every Elixir task would be rejected for a
        # reason that names the wrong thing. ExUnit selects a test by `file:line`, which needs a
        # test's identity to carry where it is declared (#3973). Refused until it does.
        filter_template="",
        ran_a_test=r"[1-9][0-9]* tests?, 0 failures",
    ),
}


# --- frameworks -----------------------------------------------------------------------------------
#
# The language is not the unit of adaptation; the test framework is. `client-sdk-go` declares no
# `func TestXxx` worth mining — it uses Ginkgo, where a test is `It("name", func(){…})` and the
# runner is filtered with `-ginkgo.focus` rather than `-run`. Assuming the standard library there
# produced one proposal out of eight, and the seven it discarded were real.
#
# Which framework a repository uses is measured rather than declared: count how many of its test
# files each candidate contract actually matches and take the winner. A manifest can lie, be absent,
# or list a framework used by one legacy directory; the files cannot.

ALTERNATES: dict[str, tuple[Language, ...]] = {
    "typescript": (
        Language(
            name="typescript+vitest",
            source_suffixes=(".ts", ".tsx"),
            test_path=_pattern(r"\.(test|spec)\.tsx?$|(^|/)__tests__/|(^|/)test/"),
            test_declaration=_pattern(r"^\s*(?:it|test)\s*\("),
            test_name=_pattern(r"""(?:it|test)\s*\(\s*['"`]([^'"`]+)"""),
            inline_tests=False,
            test_command="npx vitest run",
            command_marker="vitest",
            vendoring_replaces_setup=True,
            # `--reporter=verbose` so Vitest prints one line per test, and the proof below names the
            # test rather than counting passes. A count cannot tell "three tests matched" from "one
            # test, three times": a Vitest config may declare several projects, and `hono` declares
            # three, so its one selected test reports `Tests  3 passed` and read as a count says
            # nothing ran. The `✓` is what makes the line a pass rather than a listing, and the
            # lookahead is what stops `renders a list` matching `renders a list of two` — written as
            # Anchored at the end of the line, past the duration the reporter appends, because
            # `renders a list` is otherwise proven by `renders a list of two` — a space follows the
            # name either way. Not a lookahead: the offline scorer matches this same pattern with
            # `grep -E`, which has none.
            filter_template=r"--reporter=verbose -t '(^|\s){name}$'",
            name_is_regex=True,
            # `Tests`, never `Test Files`: the latter reads `1 failed (1)` for a file that never
            # ran a thing, so it proves the opposite of what it looks like.
            runner_reported=r"Tests\s+[0-9]",
            ran_a_test=r"✓.*> {name}( [0-9.]+m?s)?$",
            failed_a_test=r"\u00d7.*> {name}( [0-9.]+m?s)?$",
        ),
    ),
    "go": (
        Language(
            name="go+ginkgo",
            source_suffixes=(".go",),
            test_path=_pattern(r"_test\.go$"),
            test_declaration=_pattern(r'^\s*(?:It|Entry|Specify)\s*\(\s*"'),
            test_name=_pattern(r'^\s*(?:It|Entry|Specify)\s*\(\s*"([^"]+)"'),
            inline_tests=False,
            test_command="go test",
            # Anchored at both ends, and proven to have run ONE spec. `--ginkgo.focus` is an
            # unanchored regex over a spec's full text — container descriptions joined to the
            # leaf's by spaces — so a bare `handles input` also runs `handles input errors`, and a
            # trailing `$` alone still matches `mishandles input`. A task whose own spec is missing
            # would then be graded by its neighbour. The leaf is the tail of that text and either
            # begins it or follows a space, which is what this says. Written `[[:space:]]` rather
            # than a backslash class because the template is `shlex.split` before it is filled, and
            # shlex reads a backslash as its own escape — the class would reach Ginkgo as a letter.
            filter_template="-ginkgo.focus=(^|[[:space:]]){name}$ {package}",
            name_is_regex=True,
            ran_a_test=r"Ran 1 of",
            # A variant changes how tests are found and named, not what the toolchain needs: a
            # Ginkgo repository is a Go repository, and its bundles vendor and repair PATH the same way.
            scorer_preamble='command -v go >/dev/null 2>&1 || export PATH="$PATH:/usr/local/go/bin:/go/bin"',
            # Measured on gin in `golang:1.26-bookworm` as uid 1000: `go test ./...` takes 33s cold and 3s
            # against a cache this seed filled, with no test result served from it.
            build_cache_variable="GOCACHE",
            build_cache_seed="go test -run '^$' ./...",
            offline_prepare="go mod vendor",
            offline_artifacts=("vendor",),
        ),
    ),
}


def detect_framework(test_file_contents: list[str], language: Language, test_command: str = "") -> Language:
    """Pick the contract that actually matches this repository's tests.

    Args:
        test_file_contents: Contents of the repository's test files.
        language: The language's default contract.
        test_command: What the repository declared it runs its tests with. Read by a contract that
            names a `command_marker`, because two frameworks can write tests identically.

    Returns:
        Whichever candidate contract matches the most test files, defaulting to `language` when
        nothing matches — a repository with no recognizable tests should report zero yield under its
        declared contract, not under one guessed for it.
    """
    candidates = (language, *ALTERNATES.get(language.name, ()))
    named = [c for c in candidates if c.command_marker and c.command_marker in test_command]
    if named:
        return named[0]
    best, best_score = language, 0
    for candidate in candidates:
        if candidate.command_marker:
            # Its own command did not name it, and counting declarations cannot reach it — the
            # sibling it would be confused with writes tests identically.
            continue
        score = sum(1 for content in test_file_contents if candidate.test_declaration.search(content))
        if score > best_score:
            best, best_score = candidate, score
    return best


def resolve_contract(name: str) -> Language:
    """Look a contract up by the name a runner reported — a language, or a detected variant.

    Raises:
        KeyError: If nothing is called that.

    Returns:
        The named language or framework contract capable of selecting an individual test.
    """
    if name in LANGUAGES:
        contract = LANGUAGES[name]
        if not contract.filter_template:
            # A row with no way to run one test by name cannot make a task: every probe would run
            # the whole suite, and a probe that does not single out its test proves nothing.
            raise KeyError(f"{name}: this contract cannot run a single test by name yet")
        return contract
    for variants in ALTERNATES.values():
        for variant in variants:
            if variant.name == name:
                return variant
    raise KeyError(name)
