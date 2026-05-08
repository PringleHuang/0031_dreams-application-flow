"""Property-based tests for external service retry mechanism consistency.

# Feature: dreams-application-flow, Property 8: 外部服務重試機制一致性

Validates: Requirements 12.4, 15.2, 15.3

Uses hypothesis to generate random failure scenarios and verifies:
- Retry count never exceeds the configured maximum
- Each retry attempt is logged
- After final failure, no further retries are attempted
"""

import logging

import pytest
from hypothesis import given, settings, strategies as st
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from dreams_workflow.shared.exceptions import (
    DreamsConnectionError,
    EmailSendError,
    RagicCommunicationError,
)
from dreams_workflow.shared.retry_config import (
    RetryConfig,
    _after_final_failure,
    _before_retry_log,
)


# =============================================================================
# Strategies
# =============================================================================

# Strategy for error messages
error_message_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    min_size=1,
    max_size=50,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_ragic_decorated(fn):
    """Create a fresh RAGIC-retry-decorated function."""
    return retry(
        retry=retry_if_exception_type(RagicCommunicationError),
        stop=stop_after_attempt(RetryConfig.RAGIC_MAX_RETRIES),
        wait=wait_fixed(0),  # No wait in tests
        before_sleep=_before_retry_log("RAGIC"),
        retry_error_callback=_after_final_failure(
            "RAGIC", RetryConfig.RAGIC_MAX_RETRIES
        ),
    )(fn)


def _make_dreams_decorated(fn):
    """Create a fresh DREAMS-retry-decorated function."""
    return retry(
        retry=retry_if_exception_type(DreamsConnectionError),
        stop=stop_after_attempt(RetryConfig.DREAMS_MAX_RETRIES),
        wait=wait_fixed(0),
        before_sleep=_before_retry_log("DREAMS"),
        retry_error_callback=_after_final_failure(
            "DREAMS", RetryConfig.DREAMS_MAX_RETRIES
        ),
    )(fn)


def _make_ses_decorated(fn):
    """Create a fresh SES-retry-decorated function."""
    return retry(
        retry=retry_if_exception_type(EmailSendError),
        stop=stop_after_attempt(RetryConfig.SES_MAX_RETRIES),
        wait=wait_fixed(0),
        before_sleep=_before_retry_log("SES"),
        retry_error_callback=_after_final_failure(
            "SES", RetryConfig.SES_MAX_RETRIES
        ),
    )(fn)


def _make_bedrock_decorated(fn):
    """Create a fresh Bedrock-retry-decorated function."""
    return retry(
        stop=stop_after_attempt(RetryConfig.BEDROCK_MAX_RETRIES),
        wait=wait_fixed(0),
        before_sleep=_before_retry_log("Bedrock"),
        retry_error_callback=_after_final_failure(
            "Bedrock", RetryConfig.BEDROCK_MAX_RETRIES
        ),
    )(fn)


def _make_always_failing(exception_class, error_msg: str = "test error"):
    """Create a function that always fails, tracking call count."""
    call_count = {"value": 0}

    def fn():
        call_count["value"] += 1
        raise exception_class("test_service", error_msg)

    return fn, call_count


def _make_failing_n_times(exception_class, fail_count: int, error_msg: str = "test"):
    """Create a function that fails N times then succeeds."""
    call_count = {"value": 0}

    def fn():
        call_count["value"] += 1
        if call_count["value"] <= fail_count:
            raise exception_class("test_service", error_msg)
        return "success"

    return fn, call_count


class LogCapture(logging.Handler):
    """Logging handler that captures log records for assertion."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# =============================================================================
# Property Tests
# =============================================================================


class TestRetryMechanismConsistency:
    """Property 8: 外部服務重試機制一致性"""

    # Feature: dreams-application-flow, Property 8: 外部服務重試機制一致性

    @settings(max_examples=100)
    @given(error_msg=error_message_strategy)
    def test_ragic_retry_count_never_exceeds_max(self, error_msg: str):
        """RAGIC retry count never exceeds configured maximum (3)."""
        fn, call_count = _make_always_failing(RagicCommunicationError, error_msg)
        decorated = _make_ragic_decorated(fn)

        with pytest.raises(RagicCommunicationError):
            decorated()

        assert call_count["value"] == RetryConfig.RAGIC_MAX_RETRIES
        assert call_count["value"] <= 3

    @settings(max_examples=100)
    @given(error_msg=error_message_strategy)
    def test_dreams_retry_count_never_exceeds_max(self, error_msg: str):
        """DREAMS retry count never exceeds configured maximum (3)."""
        fn, call_count = _make_always_failing(DreamsConnectionError, error_msg)
        decorated = _make_dreams_decorated(fn)

        with pytest.raises(DreamsConnectionError):
            decorated()

        assert call_count["value"] == RetryConfig.DREAMS_MAX_RETRIES
        assert call_count["value"] <= 3

    @settings(max_examples=100)
    @given(error_msg=error_message_strategy)
    def test_ses_retry_count_never_exceeds_max(self, error_msg: str):
        """SES retry count never exceeds configured maximum (3)."""
        fn, call_count = _make_always_failing(EmailSendError, error_msg)
        decorated = _make_ses_decorated(fn)

        with pytest.raises(EmailSendError):
            decorated()

        assert call_count["value"] == RetryConfig.SES_MAX_RETRIES
        assert call_count["value"] <= 3

    @settings(max_examples=100)
    @given(error_msg=error_message_strategy)
    def test_bedrock_retry_count_never_exceeds_max(self, error_msg: str):
        """Bedrock retry count never exceeds configured maximum (2)."""
        call_count = {"value": 0}

        def fn():
            call_count["value"] += 1
            raise RuntimeError(error_msg)

        decorated = _make_bedrock_decorated(fn)

        with pytest.raises(RuntimeError):
            decorated()

        assert call_count["value"] == RetryConfig.BEDROCK_MAX_RETRIES
        assert call_count["value"] <= 2

    @settings(max_examples=100)
    @given(error_msg=error_message_strategy)
    def test_ragic_each_retry_logs_warning(self, error_msg: str):
        """Each RAGIC retry attempt produces a warning log entry."""
        fn, _ = _make_always_failing(RagicCommunicationError, error_msg)

        log_capture = LogCapture()
        retry_logger = logging.getLogger("dreams_workflow.shared.retry_config")
        retry_logger.addHandler(log_capture)
        retry_logger.setLevel(logging.DEBUG)

        try:
            decorated = _make_ragic_decorated(fn)
            with pytest.raises(RagicCommunicationError):
                decorated()

            # before_sleep fires between attempts: (max_retries - 1) times
            warning_logs = [
                r for r in log_capture.records if r.levelno == logging.WARNING
            ]
            expected_retry_logs = RetryConfig.RAGIC_MAX_RETRIES - 1
            assert len(warning_logs) == expected_retry_logs
        finally:
            retry_logger.removeHandler(log_capture)
            log_capture.records.clear()

    @settings(max_examples=100)
    @given(error_msg=error_message_strategy)
    def test_dreams_each_retry_logs_warning(self, error_msg: str):
        """Each DREAMS retry attempt produces a warning log entry."""
        fn, _ = _make_always_failing(DreamsConnectionError, error_msg)

        log_capture = LogCapture()
        retry_logger = logging.getLogger("dreams_workflow.shared.retry_config")
        retry_logger.addHandler(log_capture)
        retry_logger.setLevel(logging.DEBUG)

        try:
            decorated = _make_dreams_decorated(fn)
            with pytest.raises(DreamsConnectionError):
                decorated()

            warning_logs = [
                r for r in log_capture.records if r.levelno == logging.WARNING
            ]
            expected_retry_logs = RetryConfig.DREAMS_MAX_RETRIES - 1
            assert len(warning_logs) == expected_retry_logs
        finally:
            retry_logger.removeHandler(log_capture)
            log_capture.records.clear()

    @settings(max_examples=100)
    @given(error_msg=error_message_strategy)
    def test_ses_each_retry_logs_warning(self, error_msg: str):
        """Each SES retry attempt produces a warning log entry."""
        fn, _ = _make_always_failing(EmailSendError, error_msg)

        log_capture = LogCapture()
        retry_logger = logging.getLogger("dreams_workflow.shared.retry_config")
        retry_logger.addHandler(log_capture)
        retry_logger.setLevel(logging.DEBUG)

        try:
            decorated = _make_ses_decorated(fn)
            with pytest.raises(EmailSendError):
                decorated()

            warning_logs = [
                r for r in log_capture.records if r.levelno == logging.WARNING
            ]
            expected_retry_logs = RetryConfig.SES_MAX_RETRIES - 1
            assert len(warning_logs) == expected_retry_logs
        finally:
            retry_logger.removeHandler(log_capture)
            log_capture.records.clear()

    @settings(max_examples=100)
    @given(error_msg=error_message_strategy)
    def test_final_failure_logs_error(self, error_msg: str):
        """After exhausting all retries, an error-level log is produced."""
        fn, _ = _make_always_failing(RagicCommunicationError, error_msg)

        log_capture = LogCapture()
        retry_logger = logging.getLogger("dreams_workflow.shared.retry_config")
        retry_logger.addHandler(log_capture)
        retry_logger.setLevel(logging.DEBUG)

        try:
            decorated = _make_ragic_decorated(fn)
            with pytest.raises(RagicCommunicationError):
                decorated()

            error_logs = [
                r for r in log_capture.records if r.levelno == logging.ERROR
            ]
            assert len(error_logs) == 1
            assert "Final failure" in error_logs[0].getMessage()
            assert "RAGIC" in error_logs[0].getMessage()
        finally:
            retry_logger.removeHandler(log_capture)
            log_capture.records.clear()

    @settings(max_examples=100)
    @given(
        fail_count=st.integers(min_value=0, max_value=2),
        error_msg=error_message_strategy,
    )
    def test_ragic_succeeds_after_fewer_failures_than_max(
        self, fail_count: int, error_msg: str
    ):
        """If failures < max retries, the function eventually succeeds."""
        fn, call_count = _make_failing_n_times(
            RagicCommunicationError, fail_count, error_msg
        )
        decorated = _make_ragic_decorated(fn)

        result = decorated()

        assert result == "success"
        assert call_count["value"] == fail_count + 1
        assert call_count["value"] <= RetryConfig.RAGIC_MAX_RETRIES

    @settings(max_examples=100)
    @given(
        fail_count=st.integers(min_value=0, max_value=2),
        error_msg=error_message_strategy,
    )
    def test_dreams_succeeds_after_fewer_failures_than_max(
        self, fail_count: int, error_msg: str
    ):
        """If failures < max retries, DREAMS call eventually succeeds."""
        fn, call_count = _make_failing_n_times(
            DreamsConnectionError, fail_count, error_msg
        )
        decorated = _make_dreams_decorated(fn)

        result = decorated()

        assert result == "success"
        assert call_count["value"] == fail_count + 1
        assert call_count["value"] <= RetryConfig.DREAMS_MAX_RETRIES

    @settings(max_examples=100)
    @given(error_msg=error_message_strategy)
    def test_no_further_calls_after_max_retries(self, error_msg: str):
        """After max retries exhausted, the function is not called again."""
        fn, call_count = _make_always_failing(RagicCommunicationError, error_msg)
        decorated = _make_ragic_decorated(fn)

        with pytest.raises(RagicCommunicationError):
            decorated()

        final_count = call_count["value"]
        assert final_count == RetryConfig.RAGIC_MAX_RETRIES

        # Verify no additional calls happen (count stays the same)
        assert call_count["value"] == final_count

    @settings(max_examples=100)
    @given(error_msg=error_message_strategy)
    def test_retry_log_contains_service_name(self, error_msg: str):
        """Retry warning logs contain the service name for identification."""
        fn, _ = _make_always_failing(DreamsConnectionError, error_msg)

        log_capture = LogCapture()
        retry_logger = logging.getLogger("dreams_workflow.shared.retry_config")
        retry_logger.addHandler(log_capture)
        retry_logger.setLevel(logging.DEBUG)

        try:
            decorated = _make_dreams_decorated(fn)
            with pytest.raises(DreamsConnectionError):
                decorated()

            warning_logs = [
                r for r in log_capture.records if r.levelno == logging.WARNING
            ]
            for log_record in warning_logs:
                assert "DREAMS" in log_record.getMessage()
        finally:
            retry_logger.removeHandler(log_capture)
            log_capture.records.clear()

    @settings(max_examples=100)
    @given(error_msg=error_message_strategy)
    def test_retry_log_contains_attempt_number(self, error_msg: str):
        """Retry warning logs contain the attempt number."""
        fn, _ = _make_always_failing(EmailSendError, error_msg)

        log_capture = LogCapture()
        retry_logger = logging.getLogger("dreams_workflow.shared.retry_config")
        retry_logger.addHandler(log_capture)
        retry_logger.setLevel(logging.DEBUG)

        try:
            decorated = _make_ses_decorated(fn)
            with pytest.raises(EmailSendError):
                decorated()

            warning_logs = [
                r for r in log_capture.records if r.levelno == logging.WARNING
            ]
            for i, log_record in enumerate(warning_logs, start=1):
                assert str(i) in log_record.getMessage()
        finally:
            retry_logger.removeHandler(log_capture)
            log_capture.records.clear()
