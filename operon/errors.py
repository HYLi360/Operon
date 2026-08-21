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
