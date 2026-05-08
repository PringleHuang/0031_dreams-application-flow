"""Unified audit logging module for DREAMS workflow system.

Records every workflow operation with:
- Timestamp (ISO 8601 format, UTC)
- Operation type
- Case ID
- Execution result

Outputs to CloudWatch Logs (Lambda environment) or local file (Agent environment).
"""

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class AuditEntry:
    """A single audit log entry for a workflow operation.

    Attributes:
        timestamp: ISO 8601 formatted UTC timestamp.
        operation_type: Type of operation performed.
        case_id: Identifier of the case being processed.
        result: Execution result (success, failure, etc.).
        details: Optional additional context about the operation.
    """

    timestamp: str
    operation_type: str
    case_id: str
    result: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False)


class AuditLogger:
    """Unified audit logger for workflow operations.

    Automatically detects the execution environment:
    - Lambda (AWS_LAMBDA_FUNCTION_NAME set): logs to stdout (CloudWatch Logs)
    - Local/Agent: logs to a local file

    Args:
        logger_name: Name for the underlying Python logger.
        log_file: Path to local log file (used only in non-Lambda environments).
    """

    def __init__(
        self,
        logger_name: str = "dreams_workflow.audit",
        log_file: str | None = None,
    ):
        self._logger = logging.getLogger(logger_name)
        self._entries: list[AuditEntry] = []

        if not self._logger.handlers:
            self._logger.setLevel(logging.INFO)

            if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
                # Lambda environment: output to stdout for CloudWatch
                handler = logging.StreamHandler(sys.stdout)
            else:
                # Local/Agent environment: output to file or stderr
                if log_file:
                    handler = logging.FileHandler(log_file, encoding="utf-8")
                else:
                    handler = logging.StreamHandler(sys.stderr)

            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    def log_operation(
        self,
        case_id: str,
        operation_type: str,
        result: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Record a workflow operation.

        Args:
            case_id: Identifier of the case being processed.
            operation_type: Type of operation (e.g., 'state_transition',
                'ai_determination', 'email_send', 'external_api_call').
            result: Execution result (e.g., 'success', 'failure', 'retry').
            details: Optional additional context.

        Returns:
            The created AuditEntry instance.
        """
        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            operation_type=operation_type,
            case_id=case_id,
            result=result,
            details=details or {},
        )

        self._entries.append(entry)
        self._logger.info(entry.to_json())

        return entry

    def get_entries(self) -> list[AuditEntry]:
        """Return all recorded audit entries (useful for testing).

        Returns:
            List of all AuditEntry instances recorded by this logger.
        """
        return list(self._entries)

    def get_entries_for_case(self, case_id: str) -> list[AuditEntry]:
        """Return audit entries for a specific case.

        Args:
            case_id: The case identifier to filter by.

        Returns:
            List of AuditEntry instances for the given case.
        """
        return [e for e in self._entries if e.case_id == case_id]

    def clear(self) -> None:
        """Clear all recorded entries (useful for testing)."""
        self._entries.clear()


# Module-level singleton for convenience
_default_audit_logger: AuditLogger | None = None


def get_audit_logger() -> AuditLogger:
    """Get the default audit logger singleton.

    Returns:
        The shared AuditLogger instance.
    """
    global _default_audit_logger
    if _default_audit_logger is None:
        _default_audit_logger = AuditLogger()
    return _default_audit_logger


def log_operation(
    case_id: str,
    operation_type: str,
    result: str,
    details: dict[str, Any] | None = None,
) -> AuditEntry:
    """Convenience function to log an operation using the default audit logger.

    Args:
        case_id: Identifier of the case being processed.
        operation_type: Type of operation performed.
        result: Execution result.
        details: Optional additional context.

    Returns:
        The created AuditEntry instance.
    """
    return get_audit_logger().log_operation(
        case_id=case_id,
        operation_type=operation_type,
        result=result,
        details=details,
    )
