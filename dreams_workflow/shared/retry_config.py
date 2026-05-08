"""Retry configuration and tenacity decorators for external service calls.

Provides unified retry decorators for all external services with:
- Configurable max retries and wait intervals per service
- Error logging on each retry attempt
- Final failure marking after exhausting retries
"""

import logging
from typing import Any

from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from dreams_workflow.shared.exceptions import (
    DreamsConnectionError,
    EmailSendError,
    ExternalServiceError,
    RagicCommunicationError,
)

logger = logging.getLogger(__name__)


class RetryConfig:
    """統一重試配置常數"""

    RAGIC_MAX_RETRIES = 3
    RAGIC_WAIT_SECONDS = 5

    DREAMS_MAX_RETRIES = 3
    DREAMS_WAIT_SECONDS = 10

    SES_MAX_RETRIES = 3
    SES_WAIT_SECONDS = 30

    BEDROCK_MAX_RETRIES = 2
    BEDROCK_WAIT_SECONDS = 5


def _before_retry_log(service_name: str):
    """Create a before_sleep callback that logs each retry attempt.

    Args:
        service_name: Name of the external service being retried.

    Returns:
        A callback function compatible with tenacity's before_sleep parameter.
    """

    def _log_retry(retry_state: RetryCallState) -> None:
        attempt = retry_state.attempt_number
        exception = retry_state.outcome.exception() if retry_state.outcome else None
        error_msg = str(exception) if exception else "Unknown error"
        logger.warning(
            "Retry attempt %d for service '%s': %s",
            attempt,
            service_name,
            error_msg,
            extra={
                "service_name": service_name,
                "attempt_number": attempt,
                "error_message": error_msg,
            },
        )

    return _log_retry


def _after_final_failure(service_name: str, max_retries: int):
    """Create a callback that logs final failure after all retries exhausted.

    Args:
        service_name: Name of the external service.
        max_retries: Maximum number of retry attempts configured.

    Returns:
        A callback function compatible with tenacity's retry_error_callback parameter.
    """

    def _log_final_failure(retry_state: RetryCallState) -> Any:
        exception = retry_state.outcome.exception() if retry_state.outcome else None
        error_msg = str(exception) if exception else "Unknown error"
        logger.error(
            "Final failure for service '%s' after %d attempts: %s",
            service_name,
            max_retries,
            error_msg,
            extra={
                "service_name": service_name,
                "max_retries": max_retries,
                "final_error": error_msg,
                "status": "final_failure",
            },
        )
        # Re-raise the original exception
        raise retry_state.outcome.exception()

    return _log_final_failure


# Tenacity retry decorators for each external service

retry_ragic = retry(
    retry=retry_if_exception_type(RagicCommunicationError),
    stop=stop_after_attempt(RetryConfig.RAGIC_MAX_RETRIES),
    wait=wait_fixed(RetryConfig.RAGIC_WAIT_SECONDS),
    before_sleep=_before_retry_log("RAGIC"),
    retry_error_callback=_after_final_failure(
        "RAGIC", RetryConfig.RAGIC_MAX_RETRIES
    ),
)

retry_dreams = retry(
    retry=retry_if_exception_type(DreamsConnectionError),
    stop=stop_after_attempt(RetryConfig.DREAMS_MAX_RETRIES),
    wait=wait_fixed(RetryConfig.DREAMS_WAIT_SECONDS),
    before_sleep=_before_retry_log("DREAMS"),
    retry_error_callback=_after_final_failure(
        "DREAMS", RetryConfig.DREAMS_MAX_RETRIES
    ),
)

retry_ses = retry(
    retry=retry_if_exception_type(EmailSendError),
    stop=stop_after_attempt(RetryConfig.SES_MAX_RETRIES),
    wait=wait_fixed(RetryConfig.SES_WAIT_SECONDS),
    before_sleep=_before_retry_log("SES"),
    retry_error_callback=_after_final_failure(
        "SES", RetryConfig.SES_MAX_RETRIES
    ),
)

retry_bedrock = retry(
    stop=stop_after_attempt(RetryConfig.BEDROCK_MAX_RETRIES),
    wait=wait_fixed(RetryConfig.BEDROCK_WAIT_SECONDS),
    before_sleep=_before_retry_log("Bedrock"),
    retry_error_callback=_after_final_failure(
        "Bedrock", RetryConfig.BEDROCK_MAX_RETRIES
    ),
)
