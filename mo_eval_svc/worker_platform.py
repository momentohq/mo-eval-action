"""The OCI worker-platform contract shared by service delivery boundaries.

This module deliberately uses only the standard library. The hosted runner vendors it into an
Action and the lane deployment copies it as a standalone script beside ``judge.py``.
"""

from __future__ import annotations

from enum import Enum


class WorkerPlatformError(ValueError):
    """A raw worker-platform declaration is malformed or unsupported."""


class WorkerPlatform(str, Enum):
    """Canonical OCI platforms supported by local-suite workers.

    The lane runs on Python 3.9, whose standard library does not have `StrEnum`. Inheriting from
    `str` and `Enum` preserves the same member values as mo-eval while every boundary writes `.value`
    explicitly.
    """

    linux_amd64 = "linux/amd64"
    """Linux on x86-64."""
    linux_arm64 = "linux/arm64"
    """Linux on ARM64."""

    @classmethod
    def _missing_(cls, declared: object) -> None:
        """Reject a raw value outside the stable worker-platform vocabulary.

        Raises:
            WorkerPlatformError: If the supplied value is not one of the canonical platforms.
        """
        raise WorkerPlatformError(f"worker platform must be `linux/amd64` or `linux/arm64`, got {declared!r}")

    @classmethod
    def parse(cls, declared: object) -> WorkerPlatform | None:
        """Parse a raw boundary value without resolving a legacy omission.

        Args:
            declared: A string from TOML, YAML, or a stored record, or `None` when omitted.

        Returns:
            The matching platform, or `None` for an omitted legacy declaration.

        Raises:
            WorkerPlatformError: If the supplied value is not one of the canonical platforms.
        """
        if declared is None:
            return None
        if not isinstance(declared, str):
            raise WorkerPlatformError(
                f"worker platform must be `linux/amd64` or `linux/arm64`, got {declared!r}"
            )
        return cls(declared)

    @classmethod
    def effective(cls, declared: WorkerPlatform | None) -> WorkerPlatform:
        """Resolve a parsed platform through the compatibility default.

        Args:
            declared: A parsed platform, or `None` for an older manifest that omitted it.

        Returns:
            The platform every downstream worker must use.
        """
        if declared is None:
            return DEFAULT_WORKER_PLATFORM
        return declared


DEFAULT_WORKER_PLATFORM = WorkerPlatform.linux_amd64
"""Compatibility platform for declarations and suite artifacts written before this field existed."""
