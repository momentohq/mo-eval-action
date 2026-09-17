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
from dataclasses import dataclass


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
    offline_artifacts: tuple[str, ...] = ()
    """Paths `offline_prepare` produces that the repository's own `.gitignore` will typically ignore
    (`vendor/`). They must be force-tracked in the start commit: mo-eval's snapshotter freezes
    tracked and non-ignored files only, so an ignored `vendor/` silently never reaches the
    workers — and the baseline "fails" on a module fetch instead of on the task."""
    failed_a_test: str = ""
    """Regex proving the NAMED test ran and FAILED, with `{name}` available. A line pattern, like
    `ran_a_test`, and matched the same two ways.

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
    scorer_preamble: str = ""
    """Shell lines the generated scorer runs first — environment the language's toolchain needs that a
    scoring shell may not provide. Kept per language rather than per repository: it is a fact about
    the toolchain, not the code under test."""


def _pattern(source: str) -> re.Pattern[str]:
    """Compile a contract pattern.

    MULTILINE matters: these patterns are applied both line by line (when naming the tests a change
    wrote) and against whole files (when detecting which framework a repository uses). Without it a
    leading `^` anchors to the start of the entire file, so every framework scores zero and detection
    silently falls back to the default — which is how Go kept being read as standard-library tests.
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
        ran_a_test=r"^test (\S+::)?{name}( - should panic)? \.\.\. ok$",
        failed_a_test=r"^test (\S+::)?{name}( - should panic)? \.\.\. FAILED$",
        package_scoped=True,
    ),
    "go": Language(
        name="go",
        source_suffixes=(".go",),
        test_path=_pattern(r"_test\.go$"),
        test_declaration=_pattern(r"^func\s+(?:Test|Fuzz|Example)\w*\("),
        test_name=_pattern(r"func\s+((?:Test|Fuzz|Example)\w*)\s*\("),
        inline_tests=False,
        test_command="go test",
        filter_template="-v -run ^{name}$ {package}",
        ran_a_test=r"--- PASS: {name}\b",
        failed_a_test=r"--- FAIL: {name}\b",
        # A login shell (`sh -lc`, which mo-eval's local-suite workers use) sources /etc/profile,
        # which resets PATH to the Debian default and drops /usr/local/go/bin — so `go` is "not
        # found" in the very image that ships it, and a baseline "fails" for a reason that is not
        # the task. Re-adding the standard Go locations is harmless where they are already present.
        scorer_preamble='command -v go >/dev/null 2>&1 || export PATH="$PATH:/usr/local/go/bin:/go/bin"',
        offline_prepare="go mod vendor",
        offline_artifacts=("vendor",),
    ),
    "java": Language(
        name="java",
        source_suffixes=(".java",),
        test_path=_pattern(r"(^|/)src/\w*test\w*/"),
        test_declaration=_pattern(r"@Test\b"),
        test_name=_pattern(r"(?:void|Object)\s+(\w+)\s*\("),
        inline_tests=False,
        test_command="./gradlew test",
        filter_template="--tests *{name}*",
        ran_a_test=r"BUILD SUCCESSFUL",
    ),
    "kotlin": Language(
        name="kotlin",
        source_suffixes=(".kt",),
        test_path=_pattern(r"(^|/)src/\w*test\w*/"),
        test_declaration=_pattern(r"@Test\b"),
        test_name=_pattern(r"fun\s+`?([^`(]+?)`?\s*\("),
        inline_tests=False,
        test_command="./gradlew allTests",
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
        ran_a_test=r"::{name}\b.*PASSED",
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
        ran_a_test=r"Tests:?\s+1 passed",
    ),
    "csharp": Language(
        name="csharp",
        source_suffixes=(".cs",),
        test_path=_pattern(r"(^|/)[^/]*\.Tests?/|Tests?\.cs$"),
        test_declaration=_pattern(r"\[(?:Fact|Test|Theory)\]"),
        test_name=_pattern(r"(?:void|Task|async\s+Task)\s+(\w+)\s*\("),
        inline_tests=False,
        test_command="dotnet test",
        filter_template="--filter FullyQualifiedName~{name}",
        ran_a_test=r"Passed:\s+[1-9]",
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
            ran_a_test=r"✓.*> {name}( [0-9.]+m?s)?$",
            failed_a_test=r"×.*> {name}( [0-9.]+m?s)?$",
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
            filter_template='-ginkgo.focus=(^|[[:space:]]){name}$ {package}',
            name_is_regex=True,
            ran_a_test=r"Ran 1 of",
            # A variant changes how tests are found and named, not what the toolchain needs: a
            # Ginkgo repository is a Go repository, and its bundles vendor and repair PATH the same way.
            scorer_preamble='command -v go >/dev/null 2>&1 || export PATH="$PATH:/usr/local/go/bin:/go/bin"',
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
