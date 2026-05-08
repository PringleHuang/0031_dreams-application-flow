"""Property-based tests for workflow operation audit log completeness.

# Feature: dreams-application-flow, Property 9: 流程操作日誌完整性

Validates: Requirements 15.4

Uses hypothesis to generate random operation events and verifies:
- Every operation creates an audit log entry
- Timestamps are valid ISO 8601 format
- All required fields (timestamp, operation_type, case_id, result) are present
"""

from datetime import datetime, timezone

from hypothesis import given, settings, strategies as st

from dreams_workflow.shared.audit_logger import AuditEntry, AuditLogger


# =============================================================================
# Strategies
# =============================================================================

# Strategy for case IDs
case_id_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "Pd")),
    min_size=1,
    max_size=30,
)

# Strategy for operation types
operation_type_strategy = st.sampled_from(
    [
        "state_transition",
        "ai_determination",
        "email_send",
        "external_api_call",
        "webhook_received",
        "document_comparison",
        "semantic_analysis",
        "supplement_generation",
        "case_closure",
        "renewal_processing",
    ]
)

# Strategy for result values
result_strategy = st.sampled_from(
    ["success", "failure", "retry", "skipped", "partial"]
)

# Strategy for detail dictionaries
details_strategy = st.fixed_dictionaries(
    {},
    optional={
        "error_message": st.text(min_size=0, max_size=50),
        "retry_count": st.integers(min_value=0, max_value=5),
        "duration_ms": st.integers(min_value=0, max_value=60000),
        "target_status": st.text(min_size=1, max_size=20),
    },
)

# Strategy for multiple operations (batch)
batch_size_strategy = st.integers(min_value=1, max_value=20)


# =============================================================================
# Helpers
# =============================================================================


def _is_valid_iso8601(timestamp: str) -> bool:
    """Check if a string is a valid ISO 8601 timestamp."""
    try:
        dt = datetime.fromisoformat(timestamp)
        return True
    except (ValueError, TypeError):
        return False


# =============================================================================
# Property Tests
# =============================================================================


class TestAuditLogCompleteness:
    """Property 9: 流程操作日誌完整性"""

    # Feature: dreams-application-flow, Property 9: 流程操作日誌完整性

    @settings(max_examples=100)
    @given(
        case_id=case_id_strategy,
        operation_type=operation_type_strategy,
        result=result_strategy,
    )
    def test_every_operation_creates_audit_entry(
        self, case_id: str, operation_type: str, result: str
    ):
        """Every log_operation call creates exactly one AuditEntry."""
        audit_logger = AuditLogger(logger_name=f"test.audit.{id(self)}")
        audit_logger.clear()

        entry = audit_logger.log_operation(
            case_id=case_id,
            operation_type=operation_type,
            result=result,
        )

        entries = audit_logger.get_entries()
        assert len(entries) == 1
        assert entries[0] is entry

    @settings(max_examples=100)
    @given(
        case_id=case_id_strategy,
        operation_type=operation_type_strategy,
        result=result_strategy,
    )
    def test_timestamp_is_valid_iso8601(
        self, case_id: str, operation_type: str, result: str
    ):
        """Every audit entry has a valid ISO 8601 timestamp."""
        audit_logger = AuditLogger(logger_name=f"test.audit.ts.{id(self)}")
        audit_logger.clear()

        entry = audit_logger.log_operation(
            case_id=case_id,
            operation_type=operation_type,
            result=result,
        )

        assert _is_valid_iso8601(entry.timestamp), (
            f"Timestamp '{entry.timestamp}' is not valid ISO 8601"
        )

    @settings(max_examples=100)
    @given(
        case_id=case_id_strategy,
        operation_type=operation_type_strategy,
        result=result_strategy,
    )
    def test_timestamp_is_utc(
        self, case_id: str, operation_type: str, result: str
    ):
        """Every audit entry timestamp is in UTC timezone."""
        audit_logger = AuditLogger(logger_name=f"test.audit.utc.{id(self)}")
        audit_logger.clear()

        entry = audit_logger.log_operation(
            case_id=case_id,
            operation_type=operation_type,
            result=result,
        )

        dt = datetime.fromisoformat(entry.timestamp)
        # UTC timestamps end with +00:00 or Z
        assert dt.tzinfo is not None, "Timestamp must include timezone info"
        assert dt.utcoffset().total_seconds() == 0, "Timestamp must be UTC"

    @settings(max_examples=100)
    @given(
        case_id=case_id_strategy,
        operation_type=operation_type_strategy,
        result=result_strategy,
    )
    def test_entry_contains_all_required_fields(
        self, case_id: str, operation_type: str, result: str
    ):
        """Every audit entry contains timestamp, operation_type, case_id, and result."""
        audit_logger = AuditLogger(logger_name=f"test.audit.fields.{id(self)}")
        audit_logger.clear()

        entry = audit_logger.log_operation(
            case_id=case_id,
            operation_type=operation_type,
            result=result,
        )

        assert entry.case_id == case_id
        assert entry.operation_type == operation_type
        assert entry.result == result
        assert entry.timestamp is not None and entry.timestamp != ""

    @settings(max_examples=100)
    @given(
        case_id=case_id_strategy,
        operation_type=operation_type_strategy,
        result=result_strategy,
        details=details_strategy,
    )
    def test_entry_preserves_details(
        self, case_id: str, operation_type: str, result: str, details: dict
    ):
        """Audit entry preserves all provided detail fields."""
        audit_logger = AuditLogger(logger_name=f"test.audit.details.{id(self)}")
        audit_logger.clear()

        entry = audit_logger.log_operation(
            case_id=case_id,
            operation_type=operation_type,
            result=result,
            details=details,
        )

        assert entry.details == details

    @settings(max_examples=100)
    @given(
        case_id=case_id_strategy,
        operation_type=operation_type_strategy,
        result=result_strategy,
    )
    def test_entry_serializes_to_valid_json(
        self, case_id: str, operation_type: str, result: str
    ):
        """Every audit entry can be serialized to valid JSON."""
        import json

        audit_logger = AuditLogger(logger_name=f"test.audit.json.{id(self)}")
        audit_logger.clear()

        entry = audit_logger.log_operation(
            case_id=case_id,
            operation_type=operation_type,
            result=result,
        )

        json_str = entry.to_json()
        parsed = json.loads(json_str)

        assert parsed["case_id"] == case_id
        assert parsed["operation_type"] == operation_type
        assert parsed["result"] == result
        assert "timestamp" in parsed

    @settings(max_examples=50)
    @given(
        case_id=case_id_strategy,
        operations=st.lists(
            st.tuples(operation_type_strategy, result_strategy),
            min_size=1,
            max_size=10,
        ),
    )
    def test_multiple_operations_all_recorded(
        self, case_id: str, operations: list[tuple[str, str]]
    ):
        """Multiple operations for the same case are all recorded."""
        audit_logger = AuditLogger(logger_name=f"test.audit.multi.{id(self)}")
        audit_logger.clear()

        for op_type, result in operations:
            audit_logger.log_operation(
                case_id=case_id,
                operation_type=op_type,
                result=result,
            )

        entries = audit_logger.get_entries()
        assert len(entries) == len(operations)

        for entry, (expected_op, expected_result) in zip(entries, operations):
            assert entry.case_id == case_id
            assert entry.operation_type == expected_op
            assert entry.result == expected_result

    @settings(max_examples=50)
    @given(
        case_ids=st.lists(case_id_strategy, min_size=2, max_size=5, unique=True),
        operation_type=operation_type_strategy,
        result=result_strategy,
    )
    def test_entries_filterable_by_case_id(
        self, case_ids: list[str], operation_type: str, result: str
    ):
        """Entries can be correctly filtered by case_id."""
        audit_logger = AuditLogger(logger_name=f"test.audit.filter.{id(self)}")
        audit_logger.clear()

        for cid in case_ids:
            audit_logger.log_operation(
                case_id=cid,
                operation_type=operation_type,
                result=result,
            )

        for cid in case_ids:
            filtered = audit_logger.get_entries_for_case(cid)
            assert len(filtered) == 1
            assert filtered[0].case_id == cid

    @settings(max_examples=100)
    @given(
        case_id=case_id_strategy,
        operation_type=operation_type_strategy,
        result=result_strategy,
    )
    def test_timestamp_is_recent(
        self, case_id: str, operation_type: str, result: str
    ):
        """Audit entry timestamp is close to current time (within 5 seconds)."""
        audit_logger = AuditLogger(logger_name=f"test.audit.recent.{id(self)}")
        audit_logger.clear()

        before = datetime.now(timezone.utc)
        entry = audit_logger.log_operation(
            case_id=case_id,
            operation_type=operation_type,
            result=result,
        )
        after = datetime.now(timezone.utc)

        entry_time = datetime.fromisoformat(entry.timestamp)
        assert before <= entry_time <= after

    @settings(max_examples=100)
    @given(
        case_id=case_id_strategy,
        operation_type=operation_type_strategy,
        result=result_strategy,
    )
    def test_to_dict_contains_all_fields(
        self, case_id: str, operation_type: str, result: str
    ):
        """to_dict() returns a dictionary with all required keys."""
        audit_logger = AuditLogger(logger_name=f"test.audit.dict.{id(self)}")
        audit_logger.clear()

        entry = audit_logger.log_operation(
            case_id=case_id,
            operation_type=operation_type,
            result=result,
        )

        d = entry.to_dict()
        assert "timestamp" in d
        assert "operation_type" in d
        assert "case_id" in d
        assert "result" in d
        assert "details" in d
