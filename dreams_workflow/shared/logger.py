"""Unified logging configuration for DREAMS workflow system.

Provides structured logging with case_id, operation_type, and ISO 8601 timestamps.
"""

import json
import logging
import sys
from datetime import datetime, timezone


class StructuredFormatter(logging.Formatter):
    """JSON structured log formatter with case_id and operation_type support."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }

        # Add case_id if present
        if hasattr(record, "case_id"):
            log_entry["case_id"] = record.case_id

        # Add operation_type if present
        if hasattr(record, "operation_type"):
            log_entry["operation_type"] = record.operation_type

        # Add exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    """Get a configured logger with structured JSON output.

    Args:
        name: Logger name, typically the module name.

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger(name)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

    return logger


def log_operation(
    logger: logging.Logger,
    case_id: str,
    operation_type: str,
    message: str,
    level: str = "info",
    **kwargs,
) -> None:
    """Log a workflow operation with structured context.

    Args:
        logger: Logger instance.
        case_id: Case identifier.
        operation_type: Type of operation being performed.
        message: Log message.
        level: Log level (debug, info, warning, error, critical).
        **kwargs: Additional context fields.
    """
    extra = {"case_id": case_id, "operation_type": operation_type, **kwargs}
    log_method = getattr(logger, level, logger.info)
    log_method(message, extra=extra)
