#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Exception types used across Operon."""

from __future__ import annotations

from typing import Any


class OperonError(Exception):
    """Base class for all Operon errors."""


class ConfigError(OperonError):
    """Configuration file is missing or invalid."""


class ValidationError(OperonError):
    """Metadata does not conform to its schema."""


class FeatureIndexError(ValidationError):
    """A flattened feature index is missing, stale or unusable for a query."""


class EntityNotFoundError(OperonError):
    """A referenced entity does not exist."""


class ChecksumError(OperonError):
    """A file checksum does not match the manifest."""


class ConflictError(OperonError):
    """An idempotent operation found conflicting data."""


class QCError(OperonError):
    """A QC stage could not be executed."""


class ExternalToolError(OperonError):
    """An external analysis tool is missing, misconfigured or failed."""


class RemoteError(OperonError):
    """A remote execution or storage operation failed."""


class RemoteUnavailableError(RemoteError):
    """The remote could not be reached, so nothing can be concluded about its artifacts.

    Callers must treat this as "unknown", never as "missing": a dropped
    connection, a closed socket or an expired session says nothing about
    whether an artifact still exists on the remote.
    """


class SecretError(OperonError):
    """A user-scoped secret backend is unavailable or a secret operation failed."""


class ExternalCommandFailed(RuntimeError):
    """An external command run failed; ``record`` carries the recorded run row.

    The failed run is already written to ``workflow_runs`` before this is
    raised.  It subclasses ``RuntimeError`` so existing ``except RuntimeError``
    callers are unaffected.  ``record["exit_code"]`` is a non-zero ``int`` only
    when the tool actually executed; infrastructure failures (timeout,
    transport, OSError) leave it ``None``.
    """

    def __init__(self, message: str, record: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.record: dict[str, Any] = record or {}
