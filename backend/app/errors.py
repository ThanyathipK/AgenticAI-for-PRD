"""Explicit application error taxonomy and failure policies."""

from dataclasses import dataclass
from enum import Enum

from app.llm_client import (
    LMStudioGatewayError,
    LMStudioOutputParsingError,
    LMStudioUnavailableError,
)


class ErrorKind(str, Enum):
    DOMAIN = "domain"
    TRANSIENT_INFRASTRUCTURE = "transient_infrastructure"
    OPTIONAL_FEATURE = "optional_feature"
    PERSISTENCE = "persistence"
    INVALID_MODEL_OUTPUT = "invalid_model_output"
    UNEXPECTED = "unexpected"


class ApplicationError(Exception):
    """Base for failures with an explicit application policy."""


class ExpectedDomainError(ApplicationError):
    """A valid request that violates a business invariant."""


class TransientInfrastructureError(ApplicationError):
    """A retryable database, network, model-gateway, or converter failure."""


class OptionalFeatureError(ApplicationError):
    """An auxiliary feature failed and the primary operation may continue."""


class PersistenceError(ApplicationError):
    """A required write failed and the active transaction must roll back."""


class InvalidModelOutputError(ApplicationError):
    """The model completed but did not satisfy its structured contract."""


@dataclass(frozen=True)
class FailurePolicy:
    kind: ErrorKind
    http_status: int
    retryable: bool
    rollback: bool
    fail_open: bool = False


def classify_exception(exc: BaseException) -> FailurePolicy:
    """Map known failures to one observable handling policy."""
    if isinstance(exc, ExpectedDomainError):
        return FailurePolicy(ErrorKind.DOMAIN, 400, False, False)
    if isinstance(exc, OptionalFeatureError):
        return FailurePolicy(ErrorKind.OPTIONAL_FEATURE, 200, False, False, True)
    if isinstance(exc, PersistenceError):
        return FailurePolicy(ErrorKind.PERSISTENCE, 500, False, True)
    if isinstance(exc, (InvalidModelOutputError, LMStudioOutputParsingError, RuntimeError)):
        return FailurePolicy(ErrorKind.INVALID_MODEL_OUTPUT, 503, True, True)
    if isinstance(
        exc,
        (
            TransientInfrastructureError,
            LMStudioUnavailableError,
            LMStudioGatewayError,
            TimeoutError,
        ),
    ):
        return FailurePolicy(ErrorKind.TRANSIENT_INFRASTRUCTURE, 503, True, True)
    return FailurePolicy(ErrorKind.UNEXPECTED, 500, False, True)
