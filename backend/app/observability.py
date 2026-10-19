"""Shared structured context for workflow and background-job logs."""

import logging
from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class LogContext:
    project_id: Optional[str] = None
    user_id: Optional[str] = None
    workflow: Optional[str] = None
    job_id: Optional[str] = None
    run_id: Optional[str] = None

    def fields(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items() if value is not None}


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    context: LogContext,
    *,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    """Emit a searchable event with consistent identifiers and structured extras."""
    values = {
        **context.fields(),
        **{key: value for key, value in fields.items() if value is not None},
    }
    suffix = " ".join(f"{key}={value}" for key, value in sorted(values.items()))
    logger.log(
        level,
        "%s%s",
        event,
        f" {suffix}" if suffix else "",
        extra={"event": event, **values},
        exc_info=exc_info,
    )
