"""Exception types used across Operon."""


class OperonError(Exception):
    """Base class for all Operon errors."""


class ConfigError(OperonError):
    """Configuration file is missing or invalid."""


class ValidationError(OperonError):
    """Metadata does not conform to its schema."""


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
