"""What a repository declares about itself: `.mo-eval/config.toml`, read by the runner.

Three lines make a repository mineable, and the fourth is the security-relevant one:

    language = "python"
    setup_command = "poetry install --no-interaction"
    test_command = ".venv/bin/python -m pytest -q"
    forward_env = ["MOMENTO_API_KEY"]

`forward_env` names the environment variables the repository's tests need. Names only. The values
come from the runner's own environment — the CI job's secrets — and are never written here, never
put on a command line, and never sent to the service, which has no field that could carry them.

TOML rather than YAML because the runner is meant to be thin: `tomllib` is in the standard library
and a YAML parser is not, and a thin runner with a dependency is a runner that needs an install step
before it can run one.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from mo_eval_svc.languages import LANGUAGES

CONFIG_PATH = Path(".mo-eval") / "config.toml"

_MAX_OFFLINE_ARTIFACTS = 16
"""How many paths a vendoring may name. One or two is the real shape; the bound is on the subprocess
count that follows, since each named path becomes its own `git add -f`."""

_KNOWN_KEYS = {
    "language",
    "setup_command",
    "test_command",
    "forward_env",
    "env",
    "repo",
    "worker_image",
    "offline_prepare",
    "offline_artifacts",
}


class ConfigError(ValueError):
    """The declaration is missing, malformed, or names something the runner cannot honour."""


@dataclass(frozen=True)
class RunnerConfig:
    """A repository's declaration, validated."""

    language: str
    """A contract name from the language table."""
    test_command: str
    """The allow-list: probes append arguments to this and can never replace it."""
    setup_command: str | None = None
    """What makes a fresh export testable, or `None` for a repository that builds from source."""
    forward_env: tuple[str, ...] = ()
    """Names of environment variables to pass from the runner's environment into setup and probes."""
    env: dict[str, str] = field(default_factory=dict)
    """Static, non-secret environment the tests need (`POETRY_VIRTUALENVS_IN_PROJECT = "true"`)."""
    repo: str | None = None
    """`owner/name` recorded in tasks; derived from the git remote when absent."""
    worker_image: str | None = None
    """An OCI image pinned by digest that can build and test this repository — what a containerized
    agent run executes in. Optional: without it, tasks are emitted for host-native t-suite only."""
    offline_prepare: str | None = None
    """A command that puts this repository's dependencies INTO the tree, run before a task is frozen.

    Workers score with no network, so whatever the tests import has to already be there. Declared by
    the repository rather than derived from the language, because only the repository knows how its
    dependencies install: `setup_command` here is `pip install -e .[dev] pytest`, and the vendoring
    that satisfies it is `pip wheel --wheel-dir .mo-eval-wheels .[dev] pytest` — a repository using
    Poetry, uv or a lockfile needs a different one entirely. Falls back to the language's own when
    absent, which is how Go repositories get `go mod vendor` without declaring anything."""
    offline_artifacts: tuple[str, ...] = ()
    """What `offline_prepare` leaves behind, force-tracked into the start commit.

    A repository's `.gitignore` ignores exactly these — `vendor/`, `node_modules/`, a wheel
    directory — and the snapshotter freezes tracked files only, so without naming them the
    preparation runs and its output never reaches a worker."""


def load_config(path: Path) -> RunnerConfig:
    """Read and validate a declaration.

    Raises:
        ConfigError: If the file is absent, is not TOML, omits a required key, names an unknown key
            (a typo like `test_comand` would otherwise silently leave the allow-list empty), names
            a language the table does not know, or gives a value the wrong shape.

    Returns:
        The validated runner declaration with optional settings and normalized command
        whitespace.
    """
    if not path.is_file():
        raise ConfigError(f"no declaration at {path}; a repository needs one to be mined")
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as failure:
        raise ConfigError(f"{path} is not valid TOML: {failure}") from failure

    unknown = set(raw) - _KNOWN_KEYS
    if unknown:
        raise ConfigError(f"{path} names unknown key(s) {sorted(unknown)}; known: {sorted(_KNOWN_KEYS)}")
    for required in ("language", "test_command"):
        if not isinstance(raw.get(required), str) or not raw[required].strip():
            raise ConfigError(f"{path} must set `{required}` to a non-empty string")
    if raw["language"] not in LANGUAGES:
        raise ConfigError(f"{path}: language {raw['language']!r} is not one of {sorted(LANGUAGES)}")

    forward = raw.get("forward_env", [])
    if not isinstance(forward, list) or not all(isinstance(name, str) and name for name in forward):
        raise ConfigError(f"{path}: `forward_env` must be a list of environment variable names")
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        raise ConfigError(f"{path}: `env` must be a table of string values")
    declared_repo = raw.get("repo")
    if declared_repo is not None and (not isinstance(declared_repo, str) or not declared_repo.strip()):
        # TOML is happy to put an integer, a list or a table here. A truthy one of those reaches
        # `gh repo view` as an argument and raises a `TypeError` out of the runner, rather than the
        # `ConfigError` this function promises for every other wrong-shaped value.
        raise ConfigError(f"{path}: `repo` must be a non-empty string, got {declared_repo!r}")
    image = raw.get("worker_image")
    if image is not None and not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", str(image)):
        raise ConfigError(
            f"{path}: `worker_image` must be pinned by digest (name@sha256:<64 hex>), got {image!r}"
        )
    setup = raw.get("setup_command")
    if setup is not None and (not isinstance(setup, str) or not setup.strip()):
        raise ConfigError(f"{path}: `setup_command`, when set, must be a non-empty string")
    prepare = raw.get("offline_prepare")
    if prepare is not None and (not isinstance(prepare, str) or not prepare.strip()):
        raise ConfigError(f"{path}: `offline_prepare`, when set, must be a non-empty string")
    artifacts = raw.get("offline_artifacts", [])
    if not isinstance(artifacts, list) or any(not isinstance(a, str) or not a.strip() for a in artifacts):
        raise ConfigError(f"{path}: `offline_artifacts` must be a list of non-empty strings")
    if len(artifacts) > _MAX_OFFLINE_ARTIFACTS:
        # Each becomes its own `git add -f`, so a list is a subprocess count. A vendoring leaves one
        # or two directories behind — `vendor` and `.cargo`, `node_modules`, a wheel directory — and
        # a repository naming hundreds is describing something other than what it vendored.
        raise ConfigError(
            f"{path}: at most {_MAX_OFFLINE_ARTIFACTS} `offline_artifacts` may be named, got {len(artifacts)}"
        )
    if prepare and not artifacts:
        # A preparation whose output is not named is a preparation that runs and reaches nobody: the
        # snapshotter freezes tracked files, and what this produces is exactly what a `.gitignore`
        # ignores. Refused rather than run, because the failure is otherwise a worker error much later.
        raise ConfigError(f"{path}: `offline_prepare` needs `offline_artifacts` naming what it leaves behind")

    return RunnerConfig(
        language=raw["language"],
        test_command=raw["test_command"].strip(),
        setup_command=setup.strip() if isinstance(setup, str) else None,
        forward_env=tuple(forward),
        env={str(key): value for key, value in env.items()},
        repo=raw.get("repo"),
        worker_image=image,
        offline_prepare=prepare.strip() if isinstance(prepare, str) else None,
        offline_artifacts=tuple(a.strip() for a in artifacts),
    )


def probe_environment(config: RunnerConfig, environ: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """Assemble the environment setup and probes run in, from the declaration and the runner's own.

    Returns:
        The environment to add, and the names in `forward_env` that the runner's environment did not
        have. A missing secret is reported, not ignored: a repository whose conftest reads it at
        import time fails every probe identically, which would otherwise read as a repository with
        no valid tasks.
    """
    forwarded = {name: environ[name] for name in config.forward_env if name in environ}
    missing = [name for name in config.forward_env if name not in environ]
    return {**config.env, **forwarded}, missing
