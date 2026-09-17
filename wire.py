"""The runner/service protocol: every type that crosses the boundary, and the audit log of crossings.

The split is the product decision. Candidate selection, diff splitting, prompt authoring and oracle
construction never leave the service, because none of them need the customer's toolchain. Everything
that *does* need the toolchain — collecting git facts, running a test suite, running an agent — is
dumb enough to hand to a runner in the customer's CI.

So a runner never receives a "task". It receives `WorkOrder`s: check out this commit, apply this
patch, run this command, report the exit code. It is not told which patch is a scaffold and which is
a gold fix, which candidate survived validation, or why any commit was chosen. The judgment stays
server-side; the runner reports exit codes.

Every value defined here is JSON, and `Wire.crossing` records each one to disk. That makes the egress
claim auditable rather than asserted: after a run, `wire/` *is* the list of what left the repository.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from types import NoneType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

# --- phase 1: facts, carrying no source content ------------------------------------------------


@dataclass(frozen=True)
class FileFacts:
    """One file touched by one change, described without any of its content."""

    path: str
    """Repository-relative path."""
    insertions: int
    """Lines added, as the forge reports them."""
    deletions: int
    """Lines removed."""
    package: str | None
    """Build unit owning the path (the nearest Cargo.toml's name), or `None` when outside one.

    Supplied by the runner because it requires the checkout; used by the service to compose a
    scoped test command. Environment knowledge, not policy.
    """
    workspace_root: str | None
    """Repository-relative directory of the workspace that package belongs to, or `None`.

    A repository is not always one workspace. This one holds several — the root workspace builds for
    wasm, and `agent-platform/` is its own native workspace — so a test command run from the
    repository root cannot see a package in another. The runner reports where each package is built
    from because only the checkout knows; the service puts probes there.
    """


@dataclass(frozen=True)
class ChangeFacts:
    """One merged change, described well enough to *select* it and no better.

    Deliberately carries no diff and no prose body: phase 1 is meant to be cheap to produce, cheap to
    send, and uninteresting if intercepted. Only the changes that survive selection have their source
    requested in phase 2.
    """

    change_id: str
    """Forge-native identity of the merged change (here, the squashed commit sha)."""
    parent: str
    """The commit the change was applied to — a task's `parent_commit`."""
    title: str
    """Subject line. Prose, but one line of it, and it is what makes selection legible in a log."""
    merged_at: str
    """ISO-8601 merge timestamp, for recency-weighted selection."""
    files: list[FileFacts]
    """Every path the change touched."""
    number: int | None = None
    """The forge's pull-request number, when the change was collected as one."""
    labels: list[str] = field(default_factory=list)
    """The pull request's labels — the cheapest available category signal."""


@dataclass(frozen=True)
class RepoFacts:
    """What a runner reports about its repository before any source is requested."""

    repo: str
    """`owner/name`, the identity a generated task records."""
    forge: str
    """Which adapter produced this (`github`, later `gitlab`, …). The only host-specific field."""
    language: str
    """Declared in the repository's own `.mo-eval/config.toml`."""
    test_command: str
    """The customer's declared test command. Orders may fill in ARGUMENTS to this and nothing else —
    an allow-list, so the service cannot ask the runner to execute a command of its choosing."""
    changes: list[ChangeFacts]
    """Merged changes offered as candidates, newest first."""
    setup_command: str | None = None
    """The customer's declared command for making a fresh checkout testable — `poetry install`,
    `npm ci`, `bundle install`.

    Declared by the repository and run by the runner. An order can ask for setup; it cannot say what
    setup is, which keeps the same allow-list as `test_command`: the service never names a command.

    A compiled language rarely needs one — `cargo test` builds from the exported source — which is
    why its absence went unnoticed until an interpreted language was tried. Without it an
    interpreted repository is tested against whatever happens to be installed in the runner's
    ambient environment, which is the environment of some OTHER commit."""
    source: str = "git-log"
    """How the merged record was read: `github-prs` (the forge's pull requests, any merge strategy)
    or `git-log` (first-parent history, which only sees squash merges). Reported because the two
    see different repositories, and a suite mined from the wrong one is thin for a reason the user
    cannot otherwise discover."""
    worker_image: str | None = None
    """The repository's declared worker image, pinned by digest, for containerized agent runs."""
    source_note: str = ""
    """Why the record was read the way it was, when that was not the first choice — the forge's own
    error text, trimmed. A fallback that does not say why it fell back is a failure that reads as a
    thin repository."""
    unresolved_changes: int = 0
    """Pull requests whose merged unit could not be pinned to a (parent, reference) pair whose diff
    matches the PR's own file list. Counted, not dropped silently."""
    unreadable_changes: int = 0
    """How many merged changes the runner could not describe, and therefore never offered.

    Reported rather than swallowed. A repository with a damaged object, a rewritten history or a
    partial clone will refuse to show some commits, and silently skipping them would shrink the
    candidate pool with no trace — the same shape as every other under-collection failure here. A
    non-zero value means the offered list is not the whole history.
    """
    protocol: int = 0
    """Which revision of this protocol the runner speaks; 0 is a runner that does not say.

    Read by the service to decide whether a field it could send is one this runner can receive. The
    published Action vendors its own copy of these types and `from_json` refuses an unknown key, so
    a new field in an order breaks every runner published before it — not at the field, but at the
    whole order. The service learns the value before any runner sends it: deploy the service, then
    publish the Action, then move the pin.
    """
    renaming_changes: int = 0
    """Merged changes skipped because they rename a source file. A forge reports only the
    destination path, so the scaffold would add the new file without removing the old one — and the
    start state would then fail to compile, which the flip rule reads as a task."""


# --- phase 2: source for selected changes only ---------------------------------------------------


@dataclass(frozen=True)
class RejectionSummary:
    """How many changes one reason turned away. Coarse on purpose: enough for a user to learn why a
    repository yields little, not enough to reconstruct the selection model."""

    reason: str
    count: int


@dataclass(frozen=True)
class SourceRequest:
    """The service naming which changes it wants source for. The selection model stays server-side;
    only its output crosses — plus a tally of why the rest did not qualify, because a repository that
    yields nothing must be able to tell "nothing qualified" from "the service did not understand me"."""

    change_ids: list[str]
    contract: str = ""
    """The language/framework contract the service resolved for this repository, e.g. `go+ginkgo`."""
    rejections: list[RejectionSummary] = field(default_factory=list)
    """Selection rejections, most common first."""


@dataclass(frozen=True)
class FileSource:
    """Both sides of one changed file. Sending whole files rather than a diff keeps the split exact;
    a production runner can send hunks with context when egress volume matters."""

    path: str
    parent_content: str | None
    """Content at the parent commit, or `None` when the change added the file."""
    child_content: str | None
    """Content after the change, or `None` when the change deleted it."""


@dataclass(frozen=True)
class ChangeSource:
    """The source and prose for one selected change. This is the only payload carrying repository
    content, and it covers the selected changes alone."""

    change_id: str
    files: list[FileSource]
    prose: dict[str, str]
    """Free-form human context (change title, linked issue title/body) used to author a prompt."""


# --- phase 3: work orders, and what comes back ---------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One primitive a runner knows how to perform.

    `export` materializes a commit's tree into a clean directory; `apply` applies a patch to it;
    `probe` runs the declared test command with service-supplied arguments and reports how it exited.
    A probe carries no expectation — the runner is never told which way it is supposed to go.
    """

    op: str
    """`export`, `setup`, `apply`, or `probe`."""
    step_id: str
    """Identifies this step's result in the report."""
    commit: str | None = None
    """`export`: the tree to materialize."""
    patch: str | None = None
    """`apply`: a unified diff."""
    cwd: str | None = None
    """`probe`: repository-relative directory to run in, for a repository of several workspaces.

    A path inside the exported tree, which the runner enforces; it is not a way to reach a command
    elsewhere on the machine.
    """
    proof: str | None = None
    """`probe`: a regular expression whose presence in the output means the named test really ran.

    Sent because only the runner sees the whole of a command's output. The tail that comes back is
    bounded, and a reporter that prints more than that after its summary — a coverage table, a
    workspace of test binaries — pushes the evidence out of it, so the service would read a test that
    ran and passed as one that never ran at all.

    It tells the runner nothing it could use: the pattern says what "ran" looks like, never which way
    the probe is supposed to go. Sent only to a runner whose `RepoFacts.protocol` says it can read
    one; an older runner gets an order without the key and the service falls back to the tail.
    """
    counterproof: str | None = None
    """`probe`: a regular expression whose presence in the output means the named test ran and FAILED.

    The other half of `proof`, and sent for the other half of the question. A start state earns a
    task by FAILING, and read from an exit code alone that is true of a tree that did not compile
    and of a neighbouring test the filter also caught. This says what the named test failing looks
    like, and — like `proof` — says nothing about which way the probe is supposed to go.

    Sent only to a runner whose `RepoFacts.protocol` is high enough to receive one.
    """
    args: list[str] | None = None
    """`probe`: arguments APPENDED to the repository's own declared `test_command`.

    Not a script, and not a command. The service can say which tests to run; it cannot say what to
    run them with. That makes the bound an allow-list the runner enforces structurally rather than a
    deny-list of things a script must not contain — the same posture this repository already takes
    with request-header forwarding.
    """


@dataclass(frozen=True)
class WorkOrder:
    """A unit of work for the runner, opaque as to purpose."""

    order_id: str
    """Correlates the result. The service keeps the mapping from order to candidate; the runner
    cannot reconstruct it."""
    steps: list[Step]


@dataclass(frozen=True)
class StepResult:
    """What one step did, with no interpretation attached."""

    step_id: str
    exit_code: int
    duration_seconds: float
    output_tail: str
    """Bounded trailing output. Diagnostic for the operator; the verdict is the exit code."""
    proof_seen: bool | None = None
    """Whether `Step.proof` was found anywhere in the output, not only in the tail above.

    `None` when the step carried no pattern — and therefore whenever the step came from a runner
    that predates it, since the service does not send a pattern to one. Left out of the JSON
    entirely when it is `None`, so a result from a newer runner still decodes against an older
    service. The service then falls back to searching the tail, as it did before this field existed.
    """
    counterproof_seen: bool | None = None
    """Whether `Step.counterproof` was found anywhere in the output, on the same terms as
    `proof_seen`. `None` when the step carried no such pattern, and the service then reads the
    step's exit code, which is what it did before this field existed."""


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    steps: list[StepResult]
    error: str | None = None
    """Set when the runner could not carry the order out at all, as distinct from a step that ran
    and exited non-zero. Conflating the two would let an infrastructure failure read as a verdict."""


@dataclass(frozen=True)
class OrdersResponse:
    """The orders to run, and a tally of the selected changes that could not become one — so a user
    sees "7 selected, 5 split away for X" rather than a silently shorter list."""

    orders: list[WorkOrder]
    rejections: list[RejectionSummary] = field(default_factory=list)


# --- phase 4: verdicts, and the task packages that earned one -----------------------------------


@dataclass(frozen=True)
class TaskPackage:
    """A validated task as files, in the corpus layout mo-eval already reads."""

    task_id: str
    files: dict[str, str]
    """`meta.json`, `prompt.txt`, `scaffold.patch`, `acceptance.sh` — by name, as text."""
    local_suite: dict[str, str] = field(default_factory=dict)
    """Files to lay over the task's start tree to make it a one-task `local-test-suite` bundle
    (`.mo-eval/suite.yaml`, `.mo-eval/prompt.md`, `.mo-eval/acceptance/acceptance.sh`). Empty when
    the repository declared no worker image."""


@dataclass(frozen=True)
class OrderVerdict:
    """What one order's exit codes meant, decided server-side."""

    change_id: str
    validated: bool
    detail: str
    task: TaskPackage | None = None
    """Present exactly when `validated`."""


@dataclass(frozen=True)
class VerdictReport:
    verdicts: list[OrderVerdict]


# --- phase 5: handing validated bundles to the service's storage, and asking for a run ----------


@dataclass(frozen=True)
class UploadRequest:
    """The runner naming the bundles it holds for one suite. The service answers with one presigned
    PUT per bundle, so the bytes go to storage directly and never through the service itself."""

    repo: str
    suite_id: str
    """Identifies this mining run of this repository; the runner chooses it (a timestamp + sha)."""
    task_ids: list[str]
    sizes: dict[str, int] = field(default_factory=dict)
    """Bytes of each bundle, by task id. The service signs the size into the upload, so the PUT that
    URL authorizes is the one the runner said it would make and not an arbitrary one."""


@dataclass(frozen=True)
class UploadTargets:
    urls: dict[str, str]
    """Presigned PUT URL per task id, short-lived."""
    keys: dict[str, str]
    """Where each bundle will live, so a run can name it."""


@dataclass(frozen=True)
class ReviewComment:
    """One inline review comment a person left on a merged change — the repository's conventions,
    stated in its own words at the moment they mattered."""

    number: int
    path: str
    author: str
    body: str


@dataclass(frozen=True)
class ConventionSources:
    """What the conventions judge learns from: the repository's written rules and its review
    history. Collected by the runner, distilled on the service's side."""

    files: dict[str, str] = field(default_factory=dict)
    """Contributing guide, PR template, lint and format configuration, agent instructions — by path."""
    comments: list[ReviewComment] = field(default_factory=list)


@dataclass(frozen=True)
class RunRequest:
    """Ask the service to evaluate a suite: which arms, how many repeats. Recorded as a job; a lane
    picks it up. The runner never talks to the lane."""

    repo: str
    suite_id: str
    task_ids: list[str]
    arms: list[str]
    """Model routes (`anthropic/claude-opus-5`, `momento/zai-org/GLM-5.3`), each paired with every
    harness named below."""
    repeats: int = 1
    harnesses: list[str] | None = None
    """Client harnesses to compare: `mo`, `cc`, or both — the service refuses a name it cannot run.

    `None` rather than a default of `["mo"]`, so a caller who names no harness sends no value to
    distinguish: `to_json` omits a `None` field, an omitted key and a null one decode alike, and the
    service decides what naming none means. A default here would instead put the key in every
    request a new runner sends, which a service predating the field refuses whole."""
    titles: dict[str, str] = field(default_factory=dict)
    """Task id -> human title, carried so results can be read without the packages."""
    conventions: ConventionSources | None = None
    """Absent: correctness only, no conventions score."""
    categories: dict[str, str] = field(default_factory=dict)
    """Task id -> bug-fix | feature | refactor | performance | test-infra."""
    sizes: dict[str, str] = field(default_factory=dict)
    """Task id -> S | M | L."""


@dataclass(frozen=True)
class RunTicket:
    run_id: str
    job_key: str
    results_prefix: str
    """Where `cells.json` and the reports will appear when the lane is done."""


# --- phase 6: reading results back ----------------------------------------------------------------


@dataclass(frozen=True)
class ResultsRequest:
    repo: str


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    suite_id: str
    state: str
    """pending | running | done | failed — where the job record sits."""
    arms: list[str]
    task_count: int
    cells_url: str | None
    """Short-lived GET for the suite-level cells.json, present once the lane has written it."""
    suite_url: str | None
    rubric_url: str | None


@dataclass(frozen=True)
class ResultsResponse:
    repo: str
    runs: list[RunSummary]


# --- phase 7: sharing a run as a link -------------------------------------------------------------


@dataclass(frozen=True)
class ShareRequest:
    repo: str
    run_id: str
    ttl_hours: int = 72


@dataclass(frozen=True)
class ShareResponse:
    url: str
    """The hosted dashboard for one run: anyone with the link can read that run, nothing else,
    until it expires. Signed with the service's own token; the token never appears in it."""
    expires_at: str


# --- decoding ------------------------------------------------------------------------------------


MAX_CROSSINGS = 10_000
"""How many exchanges one run records. A mined suite is a few hundred; past this the log has
already said what it is for, and it shares a disk with the export it is recording."""

MAX_STRING_CHARS = 16 * 1024 * 1024
"""How long one decoded string may be. Above every field that carries a whole file, a patch or a
rendered package, and below what costs a decoder its memory."""

MAX_COLLECTION_ENTRIES = 100_000
"""How many entries one list or object in a decoded message may carry.

Counted, not sized. A per-item bound and a cap on the whole body stop neither of the shapes that
matter here: a million minimal objects fit comfortably inside a few megabytes of JSON and become a
million dataclasses with their own lists and dicts hanging off them. Above every real message —
the largest suite mined so far offered 299 changes — and far below what would cost a decoder its
memory.
"""


PROTOCOL = 2
"""The revision of this protocol the copy of these types in THIS tree speaks.

Sent by the runner as `RepoFacts.protocol` so the service knows which fields it may put in an order.
Raised when a field is added that an older runner would refuse to decode — which is any field at
all, since unknown keys are refused. The published Action vendors this file, so the number it sends
is the number that shipped with it.

1. `Step.proof` and `StepResult.proof_seen`: the proof that a test ran, searched over the whole of
   a probe's output rather than the tail that comes back.
2. `Step.counterproof` and `StepResult.counterproof_seen`: the same for a test that ran and FAILED,
   which is what a start state has to do to earn a task.
"""

MAX_DECODED_ENTRIES = 1_000_000
"""How many collection entries one message may decode to in total, across every level.

The per-collection bound is per LEVEL, and levels multiply. `RepoFacts.changes` may hold a hundred
thousand changes and each `ChangeFacts.files` another hundred thousand — ten billion dataclasses,
from a body small enough to send, every one of them inside its own level's bound. Counting the whole
decode is what turns the per-level bound into a bound on the message."""


class _Budget:
    """What is left of one message's decode, shared by every level of it."""

    __slots__ = ("left",)

    def __init__(self, left: int) -> None:
        self.left = left

    def spend(self, count: int, kind: object) -> None:
        """Take `count` entries out of the budget.

        Raises:
            ValueError: If this message has now decoded more than one message may.
        """
        self.left -= count
        if self.left < 0:
            raise ValueError(f"decoding {kind} passes {MAX_DECODED_ENTRIES} entries for one message")


def _bounded(count: int, kind: object) -> None:
    """Refuse a collection larger than one message may carry."""
    if count > MAX_COLLECTION_ENTRIES:
        raise ValueError(f"{count} entries for {kind}; at most {MAX_COLLECTION_ENTRIES}")


def _bounded_text(value: str, kind: object) -> str:
    """Refuse a string larger than one field may carry.

    A separate axis from the count. One entry holding a multi-gigabyte string is a message the
    count bound never sees — and several fields here carry whole files, whole patches and whole
    task packages, so a generous ceiling is still a ceiling.
    """
    if len(value) > MAX_STRING_CHARS:
        raise ValueError(f"{len(value)} characters for {kind}; at most {MAX_STRING_CHARS}")
    return value


def from_json(kind: type, data: Any, _budget: _Budget | None = None) -> Any:
    """Rebuild a boundary type from the JSON `to_json` produced.

    Only what the wire uses: dataclasses, lists and dicts of them, optionals, and scalars. Unknown
    keys are refused rather than ignored — a runner and a service that disagree about a field should
    fail at the boundary, where the message names the field, not three stages later.

    Args:
        _budget: What is left of this message's decode. Allocated by the outermost call and passed
            down, so nesting spends one allowance rather than a fresh one per level.

    Raises:
        ValueError: If `data` does not fit `kind`, or the whole of it decodes to more than one
            message may carry.
    """
    budget = _budget if _budget is not None else _Budget(MAX_DECODED_ENTRIES)
    origin = get_origin(kind)
    if origin is Union or origin is UnionType:
        members = [member for member in get_args(kind) if member is not NoneType]
        if data is None:
            return None
        if len(members) != 1:
            raise ValueError(f"cannot decode into {kind}")
        return from_json(members[0], data, budget)
    if origin is list:
        (item,) = get_args(kind)
        if not isinstance(data, list):
            raise ValueError(f"expected a list for {kind}, got {type(data).__name__}")
        _bounded(len(data), kind)
        budget.spend(len(data), kind)
        return [from_json(item, entry, budget) for entry in data]
    if origin is dict:
        _, value = get_args(kind)
        if not isinstance(data, dict):
            raise ValueError(f"expected an object for {kind}, got {type(data).__name__}")
        _bounded(len(data), kind)
        budget.spend(len(data), kind)
        return {str(key): from_json(value, entry, budget) for key, entry in data.items()}
    if is_dataclass(kind) and isinstance(kind, type):
        if not isinstance(data, dict):
            raise ValueError(f"expected an object for {kind.__name__}, got {type(data).__name__}")
        hints = get_type_hints(kind)
        known = {f.name for f in fields(kind)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"{kind.__name__} does not have field(s) {sorted(unknown)}")
        values = {}
        for f in fields(kind):
            if f.name in data:
                values[f.name] = from_json(hints[f.name], data[f.name], budget)
            elif f.default is MISSING and f.default_factory is MISSING:
                raise ValueError(f"{kind.__name__} is missing required field {f.name!r}")
        return kind(**values)
    if isinstance(data, str) and kind in (str, Any):
        return _bounded_text(data, kind)
    if kind is Any or isinstance(data, kind):
        return data
    if kind is float and isinstance(data, int):
        return float(data)
    raise ValueError(f"expected {getattr(kind, '__name__', kind)}, got {type(data).__name__}")


# --- the audit log -------------------------------------------------------------------------------

_DIRECTIONS = {"up": "runner->service", "down": "service->runner"}


@dataclass
class Wire:
    """Records every payload that crosses the boundary, in order, as JSON on disk.

    The point is not debugging. A customer asking "what leaves our repository?" gets a directory of
    the literal answer instead of a paragraph of reassurance.
    """

    root: Path
    sequence: int = field(default=0)

    def crossing(self, direction: str, name: str, payload: Any) -> Any:
        """Record one payload and return it unchanged.

        Args:
            direction: `up` for runner->service, `down` for service->runner.
            name: Short label for the payload, used in the filename.
            payload: Any dataclass, or list of them, defined in this module.

        Returns:
            `payload`, so a call site can wrap the value it was already passing.

        Raises:
            ValueError: If `direction` is not a known direction.
        """
        if direction not in _DIRECTIONS:
            raise ValueError(f"unknown direction {direction!r}")
        if self.sequence >= MAX_CROSSINGS:
            # One file per exchange, and the number of exchanges follows the size of the work. The
            # log is for reading afterwards, so it stops rather than fills the disk it shares with
            # the export it is recording.
            return payload
        self.sequence += 1
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{self.sequence:02d}-{direction}-{name}.json"
        path.write_text(json.dumps(to_json(payload), indent=2, sort_keys=True) + "\n")
        return payload

    def manifest(self) -> str:
        """Render the recorded crossings as a table, newest last."""
        rows = []
        for path in sorted(itertools.islice(self.root.glob("*.json"), MAX_CROSSINGS)):
            _, direction, name = path.stem.split("-", 2)
            rows.append(f"  {_DIRECTIONS[direction]:>18}  {name:<22} {path.stat().st_size:>9,} bytes")
        return "\n".join(rows)


def to_json(value: Any) -> Any:
    """Convert dataclasses (and containers of them) to plain JSON-compatible values.

    A field holding `None` is left out — but only where its default is `None`, which is exactly when
    leaving it out and sending it as null decode the same. A field typed `X | None` with no default
    is still REQUIRED, and omitting it would make the message unreadable rather than compatible:
    `FileFacts.package` is one.

    Left out at all because `from_json` refuses a key it does not know, so every field this side
    adds is otherwise a key the other side has never heard of — and the other side here is a GitHub
    Action, published and pinned by version, running in a customer's CI.
    """
    if is_dataclass(value) and not isinstance(value, type):
        # Field by field rather than `asdict`, which converts the whole tree in one go: it would
        # turn a nested dataclass into a dict before anything here could look at it, and the
        # omission below would then apply only to the outermost object. Production sends nested
        # ones — a `WorkOrder` of `Step`s, an `OrderResult` of `StepResult`s — so an `asdict` here
        # leaves every new field in every nested object on the wire as a null.
        return {field.name: to_json(getattr(value, field.name))
                for field in fields(value)
                if getattr(value, field.name) is not None or field.default is not None}
    if isinstance(value, list):
        return [to_json(item) for item in value]
    if isinstance(value, dict):
        return {key: to_json(item) for key, item in value.items()}
    return value
