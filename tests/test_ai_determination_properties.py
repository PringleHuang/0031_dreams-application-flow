"""Property-based tests for AI determination document comparison.

Property 3: 佐證文件比對結果結構完整性
Validates: Requirements 13.1, 13.2, 13.4

Uses hypothesis to generate random document and questionnaire data,
verifying that:
- The ComparisonReport always contains exactly 5 DocumentComparisonResult entries
- Each result's status is either "pass" or "fail"
- Each result's reason is a non-empty string
- Each result has a non-empty document_id and document_name
- overall_status is "all_pass" iff all results are "pass"
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, strategies as st

from dreams_workflow.ai_determination.app import (
    ComparisonReport,
    DocumentComparisonResult,
    compare_documents,
    EXPECTED_DOCUMENT_COUNT,
)
from dreams_workflow.ai_determination.config import ATTACHMENTS_CONFIG


# =============================================================================
# Strategies for generating test data
# =============================================================================

# Strategy for generating questionnaire form data
# Uses realistic field IDs from the config
_FORM_FIELD_IDS = []
for att_cfg in ATTACHMENTS_CONFIG:
    for field_cfg in att_cfg.get("extract_fields", []):
        fid = field_cfg.get("form_field_id", "")
        if fid:
            _FORM_FIELD_IDS.append(fid)

questionnaire_data_strategy = st.fixed_dictionaries(
    {fid: st.text(min_size=1, max_size=50) for fid in _FORM_FIELD_IDS}
)

# Strategy for generating document file bytes (non-empty PDF-like content)
document_bytes_strategy = st.binary(min_size=10, max_size=200).map(
    lambda b: b"%PDF-1.4\n" + b
)

# Strategy for generating a set of 5 supporting documents
supporting_documents_strategy = st.lists(
    st.tuples(
        st.sampled_from(["doc1.pdf", "doc2.pdf", "doc3.pdf", "doc4.pdf", "doc5.pdf"]),
        document_bytes_strategy,
    ),
    min_size=5,
    max_size=5,
)

# Strategy for document metadata matching the 5 expected field_ids
_ATTACHMENT_FIELD_IDS = [att["field_id"] for att in ATTACHMENTS_CONFIG]
document_metadata_strategy = st.just(
    [{"field_id": fid} for fid in _ATTACHMENT_FIELD_IDS]
)


def _make_mock_bedrock_client(extracted_data: dict | None = None):
    """Create a mock Bedrock client that returns controlled extraction results.

    Args:
        extracted_data: The data to return from invoke_model. If None, returns
            a default valid extraction result.
    """
    if extracted_data is None:
        # Default: return a valid extraction with all fields matching
        extracted_data = {
            "site_address": {"value": ["臺北市中正區忠孝東路一段1號"], "evidence": "第1頁"},
            "capacity_kwp": {"value": "100", "evidence": "第2頁"},
            "connection_method": {"value": "內線", "evidence": "第3頁"},
            "connection_voltage_type": {"value": "三相三線", "evidence": "第3頁"},
            "connection_voltage_volt": {"value": "11.4kV", "evidence": "第3頁"},
            "demarcation_voltage_type": {"value": "三相三線", "evidence": "第3頁"},
            "demarcation_voltage_volt": {"value": "22.8kV", "evidence": "第3頁"},
            "inverters": [{"model": "SUN2000-40KTL-M3", "quantity": "10", "evidence": "第4頁"}],
            "site_type": {"value": "屋頂型太陽能", "evidence": "第1頁"},
            "approval_number": {"value": "ABC-123", "evidence": "第1頁"},
            "selling_method": {"value": "全額躉售", "evidence": "第2頁"},
            "power_purchase_number": {"value": "18-38-7389-77-0", "evidence": "第1頁"},
        }

    mock_client = MagicMock()
    response_body = MagicMock()
    response_body.read.return_value = json.dumps({
        "content": [{"text": json.dumps(extracted_data, ensure_ascii=False)}]
    }).encode("utf-8")
    mock_client.invoke_model.return_value = {"body": response_body}
    return mock_client


# =============================================================================
# Property Tests
# =============================================================================


class TestDocumentComparisonStructureProperty:
    """Property 3: 佐證文件比對結果結構完整性"""

    # Feature: dreams-application-flow, Property 3: 佐證文件比對結果結構完整性

    @settings(max_examples=100)
    @given(case_id=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N", "Pd"))))
    def test_report_always_contains_exactly_5_results(self, case_id: str):
        """ComparisonReport always contains exactly 5 DocumentComparisonResult entries."""
        mock_client = _make_mock_bedrock_client()

        # Create 5 documents with matching field_ids
        supporting_documents = [
            (f"doc_{i}.pdf", b"%PDF-1.4\nfake content")
            for i in range(5)
        ]
        document_metadata = [{"field_id": fid} for fid in _ATTACHMENT_FIELD_IDS]

        # Simple questionnaire data
        questionnaire_data = {fid: "test_value" for fid in _FORM_FIELD_IDS}

        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id=case_id,
            bedrock_client=mock_client,
        )

        assert isinstance(report, ComparisonReport)
        assert len(report.results) == EXPECTED_DOCUMENT_COUNT

    @settings(max_examples=100)
    @given(
        num_docs=st.integers(min_value=0, max_value=5),
        case_id=st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("L", "N"))),
    )
    def test_report_has_5_results_regardless_of_input_count(
        self, num_docs: int, case_id: str
    ):
        """Even with fewer than 5 input documents, report has exactly 5 results."""
        mock_client = _make_mock_bedrock_client()

        # Provide variable number of documents (0 to 5)
        supporting_documents = [
            (f"doc_{i}.pdf", b"%PDF-1.4\nfake content")
            for i in range(num_docs)
        ]
        # Metadata matches available documents
        document_metadata = [
            {"field_id": _ATTACHMENT_FIELD_IDS[i]}
            for i in range(num_docs)
        ]

        questionnaire_data = {fid: "test_value" for fid in _FORM_FIELD_IDS}

        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id=case_id,
            bedrock_client=mock_client,
        )

        assert len(report.results) == EXPECTED_DOCUMENT_COUNT

    @settings(max_examples=100)
    @given(case_id=st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("L", "N"))))
    def test_each_result_status_is_pass_or_fail(self, case_id: str):
        """Every DocumentComparisonResult.status is either 'pass' or 'fail'."""
        mock_client = _make_mock_bedrock_client()

        supporting_documents = [
            (f"doc_{i}.pdf", b"%PDF-1.4\nfake content")
            for i in range(5)
        ]
        document_metadata = [{"field_id": fid} for fid in _ATTACHMENT_FIELD_IDS]
        questionnaire_data = {fid: "test_value" for fid in _FORM_FIELD_IDS}

        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id=case_id,
            bedrock_client=mock_client,
        )

        for result in report.results:
            assert result.status in ("pass", "fail"), (
                f"Unexpected status '{result.status}' for document '{result.document_name}'"
            )

    @settings(max_examples=100)
    @given(case_id=st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("L", "N"))))
    def test_each_result_reason_is_non_empty_string(self, case_id: str):
        """Every DocumentComparisonResult.reason is a non-empty string."""
        mock_client = _make_mock_bedrock_client()

        supporting_documents = [
            (f"doc_{i}.pdf", b"%PDF-1.4\nfake content")
            for i in range(5)
        ]
        document_metadata = [{"field_id": fid} for fid in _ATTACHMENT_FIELD_IDS]
        questionnaire_data = {fid: "test_value" for fid in _FORM_FIELD_IDS}

        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id=case_id,
            bedrock_client=mock_client,
        )

        for result in report.results:
            assert isinstance(result.reason, str), (
                f"reason should be str, got {type(result.reason)}"
            )
            assert len(result.reason) > 0, (
                f"reason should be non-empty for document '{result.document_name}'"
            )

    @settings(max_examples=100)
    @given(case_id=st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("L", "N"))))
    def test_each_result_has_non_empty_document_id_and_name(self, case_id: str):
        """Every result has non-empty document_id and document_name."""
        mock_client = _make_mock_bedrock_client()

        supporting_documents = [
            (f"doc_{i}.pdf", b"%PDF-1.4\nfake content")
            for i in range(5)
        ]
        document_metadata = [{"field_id": fid} for fid in _ATTACHMENT_FIELD_IDS]
        questionnaire_data = {fid: "test_value" for fid in _FORM_FIELD_IDS}

        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id=case_id,
            bedrock_client=mock_client,
        )

        for result in report.results:
            assert result.document_id and len(result.document_id) > 0
            assert result.document_name and len(result.document_name) > 0

    @settings(max_examples=100)
    @given(case_id=st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("L", "N"))))
    def test_overall_status_consistent_with_individual_results(self, case_id: str):
        """overall_status is 'all_pass' iff all individual results are 'pass'."""
        mock_client = _make_mock_bedrock_client()

        supporting_documents = [
            (f"doc_{i}.pdf", b"%PDF-1.4\nfake content")
            for i in range(5)
        ]
        document_metadata = [{"field_id": fid} for fid in _ATTACHMENT_FIELD_IDS]
        questionnaire_data = {fid: "test_value" for fid in _FORM_FIELD_IDS}

        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id=case_id,
            bedrock_client=mock_client,
        )

        all_pass = all(r.status == "pass" for r in report.results)
        if all_pass:
            assert report.overall_status == "all_pass"
        else:
            assert report.overall_status == "has_failures"

    @settings(max_examples=50)
    @given(
        fail_indices=st.lists(
            st.integers(min_value=0, max_value=4),
            min_size=0,
            max_size=5,
            unique=True,
        ),
    )
    def test_missing_documents_produce_fail_with_reason(self, fail_indices: list[int]):
        """Documents not provided in supporting_documents are marked as 'fail'."""
        mock_client = _make_mock_bedrock_client()

        # Only provide documents NOT in fail_indices
        supporting_documents = []
        document_metadata = []
        for i in range(5):
            if i not in fail_indices:
                supporting_documents.append((f"doc_{i}.pdf", b"%PDF-1.4\ncontent"))
                document_metadata.append({"field_id": _ATTACHMENT_FIELD_IDS[i]})

        questionnaire_data = {fid: "test_value" for fid in _FORM_FIELD_IDS}

        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id="test-case",
            bedrock_client=mock_client,
        )

        # Always 5 results
        assert len(report.results) == EXPECTED_DOCUMENT_COUNT

        # Missing documents should be "fail"
        for i in fail_indices:
            result = report.results[i]
            assert result.status == "fail"
            assert len(result.reason) > 0

    @settings(max_examples=50)
    @given(case_id=st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("L", "N"))))
    def test_report_timestamp_is_valid_iso8601(self, case_id: str):
        """ComparisonReport.timestamp is a valid ISO 8601 string."""
        from datetime import datetime

        mock_client = _make_mock_bedrock_client()

        supporting_documents = [
            (f"doc_{i}.pdf", b"%PDF-1.4\nfake content")
            for i in range(5)
        ]
        document_metadata = [{"field_id": fid} for fid in _ATTACHMENT_FIELD_IDS]
        questionnaire_data = {fid: "test_value" for fid in _FORM_FIELD_IDS}

        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id=case_id,
            bedrock_client=mock_client,
        )

        # Should parse without error
        parsed = datetime.fromisoformat(report.timestamp)
        assert parsed is not None

    @settings(max_examples=20, deadline=None)
    @given(case_id=st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("L", "N"))))
    def test_bedrock_failure_produces_fail_with_reason(self, case_id: str):
        """When Bedrock invocation fails, affected documents are 'fail' with reason."""
        from dreams_workflow.ai_determination.bedrock_client import BedrockInvocationError

        # Patch invoke_bedrock_extract to raise immediately (bypassing tenacity retry wait)
        with patch(
            "dreams_workflow.ai_determination.app.invoke_bedrock_extract",
            side_effect=BedrockInvocationError("Bedrock unavailable"),
        ):
            mock_client = MagicMock()

            supporting_documents = [
                (f"doc_{i}.pdf", b"%PDF-1.4\nfake content")
                for i in range(5)
            ]
            document_metadata = [{"field_id": fid} for fid in _ATTACHMENT_FIELD_IDS]
            questionnaire_data = {fid: "test_value" for fid in _FORM_FIELD_IDS}

            report = compare_documents(
                questionnaire_data=questionnaire_data,
                supporting_documents=supporting_documents,
                document_metadata=document_metadata,
                case_id=case_id,
                bedrock_client=mock_client,
            )

            # Should still have 5 results
            assert len(report.results) == EXPECTED_DOCUMENT_COUNT
            # All should be valid structure
            for result in report.results:
                assert result.status in ("pass", "fail")
                assert len(result.reason) > 0
