"""Unit tests for AI determination service.

Tests cover:
- Document comparison: all pass scenario
- Document comparison: has failures scenario
- Semantic analysis: approved scenario
- Semantic analysis: rejected scenario
- Bedrock invocation failure and retry behavior

Requirements: 13.1, 13.2, 14.1, 14.2
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from dreams_workflow.ai_determination.app import (
    ComparisonReport,
    DocumentComparisonResult,
    compare_documents,
    lambda_handler,
)
from dreams_workflow.ai_determination.bedrock_client import (
    BedrockInvocationError,
    detect_media_type,
    fix_dual_voltage,
)
from dreams_workflow.ai_determination.config import ATTACHMENTS_CONFIG
from dreams_workflow.ai_determination.semantic_analyzer import (
    SemanticAnalysisError,
    SemanticAnalysisResult,
    analyze_taipower_reply,
)


# =============================================================================
# Helpers
# =============================================================================

_ATTACHMENT_FIELD_IDS = [att["field_id"] for att in ATTACHMENTS_CONFIG]

_FORM_FIELD_IDS = []
for att_cfg in ATTACHMENTS_CONFIG:
    for field_cfg in att_cfg.get("extract_fields", []):
        fid = field_cfg.get("form_field_id", "")
        if fid:
            _FORM_FIELD_IDS.append(fid)


def _make_all_pass_bedrock_client() -> MagicMock:
    """Create a mock Bedrock client that returns matching extraction results."""
    # Build extracted data that matches the form values
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

    # Normalization response (returns same values)
    normalized_data = {
        "site_address": {"value": ["臺北市中正區忠孝東路一段1號"]},
        "capacity_kwp": {"value": "100"},
        "connection_method": {"value": "內線"},
        "connection_voltage_type": {"value": "三相三線"},
        "connection_voltage_volt": {"value": "11.4kV"},
        "demarcation_voltage_type": {"value": "三相三線"},
        "demarcation_voltage_volt": {"value": "22.8kV"},
        "site_type": {"value": "屋頂型太陽能"},
        "approval_number": {"value": "ABC-123"},
        "selling_method": {"value": "全額躉售"},
        "power_purchase_number": {"value": "18-38-7389-77-0"},
    }

    mock_client = MagicMock()
    # Alternate between extraction and normalization responses
    extract_body = MagicMock()
    extract_body.read.return_value = json.dumps({
        "content": [{"text": json.dumps(extracted_data, ensure_ascii=False)}]
    }).encode("utf-8")

    normalize_body = MagicMock()
    normalize_body.read.return_value = json.dumps({
        "content": [{"text": json.dumps(normalized_data, ensure_ascii=False)}]
    }).encode("utf-8")

    # Each document requires 1 extract + 1 normalize call
    # 4 documents need LLM (doc_5 is check_upload_only)
    mock_client.invoke_model.side_effect = [
        {"body": extract_body},  # doc_1 extract
        {"body": normalize_body},  # doc_1 normalize
        {"body": extract_body},  # doc_2 extract
        {"body": normalize_body},  # doc_2 normalize
        {"body": extract_body},  # doc_3 extract
        {"body": normalize_body},  # doc_3 normalize
        {"body": extract_body},  # doc_4 extract
        {"body": normalize_body},  # doc_4 normalize
    ]
    return mock_client


def _make_questionnaire_data_matching() -> dict:
    """Create questionnaire data that matches the mock extraction results."""
    return {
        "1014595": "臺北市中正區忠孝東路一段1號",  # site_address
        "1014749": "100",  # capacity_kwp
        "1014619": "內線",  # connection_method
        "1014621": "三相三線",  # connection_voltage_type
        "1014644": "11.4kV",  # connection_voltage_volt
        "1014622": "三相三線",  # demarcation_voltage_type
        "1014645": "22.8kV",  # demarcation_voltage_volt
        "1014618": "屋頂型太陽能",  # site_type
        "1014623": "ABC-123",  # approval_number
        "1014620": "全額躉售",  # selling_method
        "1014590": "18-38-7389-77-0",  # power_purchase_number
        "_subtable_1014629": {
            "row1": {
                "1014624": "SUN2000-40KTL-M3",  # inverter model
                "1014635": "10",  # inverter quantity
            }
        },
    }


# =============================================================================
# Test: Document comparison - all pass
# =============================================================================


class TestDocumentComparisonAllPass:
    """Tests for document comparison when all documents pass."""

    def test_all_documents_pass_returns_all_pass(self):
        """When all extracted values match form values, overall_status is 'all_pass'."""
        mock_client = _make_all_pass_bedrock_client()
        questionnaire_data = _make_questionnaire_data_matching()

        supporting_documents = [
            (f"doc_{i}.pdf", b"%PDF-1.4\nfake content")
            for i in range(5)
        ]
        document_metadata = [{"field_id": fid} for fid in _ATTACHMENT_FIELD_IDS]

        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id="CASE-001",
            bedrock_client=mock_client,
        )

        assert report.overall_status == "all_pass"
        assert len(report.results) == 5
        assert all(r.status == "pass" for r in report.results)

    def test_all_pass_report_has_correct_case_id(self):
        """Report contains the correct case_id."""
        mock_client = _make_all_pass_bedrock_client()
        questionnaire_data = _make_questionnaire_data_matching()

        supporting_documents = [
            (f"doc_{i}.pdf", b"%PDF-1.4\nfake content")
            for i in range(5)
        ]
        document_metadata = [{"field_id": fid} for fid in _ATTACHMENT_FIELD_IDS]

        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id="CASE-XYZ",
            bedrock_client=mock_client,
        )

        assert report.case_id == "CASE-XYZ"

    def test_check_upload_only_document_passes_when_present(self):
        """Document 5 (併聯審查意見書) passes when file is uploaded."""
        mock_client = _make_all_pass_bedrock_client()
        questionnaire_data = _make_questionnaire_data_matching()

        supporting_documents = [
            (f"doc_{i}.pdf", b"%PDF-1.4\nfake content")
            for i in range(5)
        ]
        document_metadata = [{"field_id": fid} for fid in _ATTACHMENT_FIELD_IDS]

        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id="CASE-001",
            bedrock_client=mock_client,
        )

        # doc_5 is check_upload_only
        doc5_result = report.results[4]
        assert doc5_result.document_name == "併聯審查意見書"
        assert doc5_result.status == "pass"
        assert "已上傳" in doc5_result.reason


# =============================================================================
# Test: Document comparison - has failures
# =============================================================================


class TestDocumentComparisonHasFailures:
    """Tests for document comparison when some documents fail."""

    def test_missing_document_produces_fail(self):
        """Missing documents are marked as 'fail'."""
        mock_client = _make_all_pass_bedrock_client()
        questionnaire_data = _make_questionnaire_data_matching()

        # Only provide 3 documents (missing doc_4 and doc_5)
        supporting_documents = [
            (f"doc_{i}.pdf", b"%PDF-1.4\nfake content")
            for i in range(3)
        ]
        document_metadata = [
            {"field_id": _ATTACHMENT_FIELD_IDS[i]}
            for i in range(3)
        ]

        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id="CASE-002",
            bedrock_client=mock_client,
        )

        assert report.overall_status == "has_failures"
        # doc_4 and doc_5 should be fail
        assert report.results[3].status == "fail"
        assert report.results[4].status == "fail"
        assert "缺少" in report.results[3].reason or "未上傳" in report.results[4].reason

    def test_empty_document_content_produces_fail(self):
        """Documents with empty content are marked as 'fail'."""
        mock_client = _make_all_pass_bedrock_client()
        questionnaire_data = _make_questionnaire_data_matching()

        # First document has empty content
        supporting_documents = [
            ("doc_0.pdf", b""),  # Empty!
            ("doc_1.pdf", b"%PDF-1.4\ncontent"),
            ("doc_2.pdf", b"%PDF-1.4\ncontent"),
            ("doc_3.pdf", b"%PDF-1.4\ncontent"),
            ("doc_4.pdf", b"%PDF-1.4\ncontent"),
        ]
        document_metadata = [{"field_id": fid} for fid in _ATTACHMENT_FIELD_IDS]

        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id="CASE-003",
            bedrock_client=mock_client,
        )

        assert report.results[0].status == "fail"
        assert "為空" in report.results[0].reason

    def test_mismatched_values_produce_fail(self):
        """Documents with mismatched extracted values are marked as 'fail'."""
        # Create a mock that returns different values from form
        mismatched_data = {
            "site_address": {"value": ["高雄市前鎮區中山路100號"], "evidence": "第1頁"},
            "capacity_kwp": {"value": "999", "evidence": "第2頁"},
            "connection_method": {"value": "外線", "evidence": "第3頁"},
            "connection_voltage_type": {"value": "單相三線", "evidence": "第3頁"},
            "connection_voltage_volt": {"value": "380V", "evidence": "第3頁"},
            "demarcation_voltage_type": {"value": "單相三線", "evidence": "第3頁"},
            "demarcation_voltage_volt": {"value": "380V", "evidence": "第3頁"},
            "inverters": [{"model": "PV-15000T-U", "quantity": "5", "evidence": "第4頁"}],
            "site_type": {"value": "地面型太陽能", "evidence": "第1頁"},
            "approval_number": {"value": "XYZ-999", "evidence": "第1頁"},
            "selling_method": {"value": "餘電躉售", "evidence": "第2頁"},
            "power_purchase_number": {"value": "99-99-9999-99-9", "evidence": "第1頁"},
        }

        mock_client = MagicMock()
        extract_body = MagicMock()
        extract_body.read.return_value = json.dumps({
            "content": [{"text": json.dumps(mismatched_data, ensure_ascii=False)}]
        }).encode("utf-8")

        normalize_body = MagicMock()
        normalize_body.read.return_value = json.dumps({
            "content": [{"text": json.dumps({
                "site_address": {"value": ["臺北市中正區忠孝東路一段1號"]},
            }, ensure_ascii=False)}]
        }).encode("utf-8")

        mock_client.invoke_model.side_effect = [
            {"body": extract_body},
            {"body": normalize_body},
            {"body": extract_body},
            {"body": normalize_body},
            {"body": extract_body},
            {"body": normalize_body},
            {"body": extract_body},
            {"body": normalize_body},
        ]

        questionnaire_data = _make_questionnaire_data_matching()
        supporting_documents = [
            (f"doc_{i}.pdf", b"%PDF-1.4\nfake content")
            for i in range(5)
        ]
        document_metadata = [{"field_id": fid} for fid in _ATTACHMENT_FIELD_IDS]

        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id="CASE-004",
            bedrock_client=mock_client,
        )

        assert report.overall_status == "has_failures"
        # At least some documents should fail due to mismatched values
        failed_count = sum(1 for r in report.results if r.status == "fail")
        assert failed_count > 0


# =============================================================================
# Test: Semantic analysis - approved
# =============================================================================


class TestSemanticAnalysisApproved:
    """Tests for semantic analysis of approved Taipower replies."""

    def test_approved_reply_returns_approved(self):
        """Clearly approved email returns category='approved'."""
        mock_client = MagicMock()
        response_body = MagicMock()
        response_body.read.return_value = json.dumps({
            "content": [{"text": json.dumps({
                "category": "approved",
                "confidence_score": 0.95,
                "rejection_reason_summary": "",
                "analysis": "郵件明確表示同意併聯申請",
            }, ensure_ascii=False)}]
        }).encode("utf-8")
        mock_client.invoke_model.return_value = {"body": response_body}

        result = analyze_taipower_reply(
            email_content="經審查，貴公司太陽能併聯申請案符合規定，同意核准併聯。",
            email_subject="RE: 太陽能併聯申請 - 審核結果通知",
            bedrock_client=mock_client,
        )

        assert result.category == "approved"
        assert result.confidence_score == 0.95

    def test_approved_reply_has_valid_structure(self):
        """Approved result has all required fields."""
        mock_client = MagicMock()
        response_body = MagicMock()
        response_body.read.return_value = json.dumps({
            "content": [{"text": json.dumps({
                "category": "approved",
                "confidence_score": 0.9,
                "rejection_reason_summary": "",
                "analysis": "核准",
            }, ensure_ascii=False)}]
        }).encode("utf-8")
        mock_client.invoke_model.return_value = {"body": response_body}

        result = analyze_taipower_reply(
            email_content="同意核准",
            email_subject="核准通知",
            bedrock_client=mock_client,
        )

        assert isinstance(result, SemanticAnalysisResult)
        assert result.category == "approved"
        assert 0.0 <= result.confidence_score <= 1.0
        assert isinstance(result.raw_analysis, str)


# =============================================================================
# Test: Semantic analysis - rejected
# =============================================================================


class TestSemanticAnalysisRejected:
    """Tests for semantic analysis of rejected Taipower replies."""

    def test_rejected_reply_returns_rejected(self):
        """Clearly rejected email returns category='rejected'."""
        mock_client = MagicMock()
        response_body = MagicMock()
        response_body.read.return_value = json.dumps({
            "content": [{"text": json.dumps({
                "category": "rejected",
                "confidence_score": 0.92,
                "rejection_reason_summary": "裝置容量超過核定容量，併聯點電壓不符",
                "analysis": "郵件要求補正兩項資料",
            }, ensure_ascii=False)}]
        }).encode("utf-8")
        mock_client.invoke_model.return_value = {"body": response_body}

        result = analyze_taipower_reply(
            email_content="經審查，貴公司申請案有以下問題需補正：1. 裝置容量超過核定容量 2. 併聯點電壓不符規定。請補正後重新送件。",
            email_subject="RE: 太陽能併聯申請 - 退件通知",
            bedrock_client=mock_client,
        )

        assert result.category == "rejected"
        assert result.confidence_score == 0.92
        assert "裝置容量" in result.rejection_reason_summary

    def test_rejected_reply_always_has_reason(self):
        """Rejected result always has non-empty rejection_reason_summary."""
        mock_client = MagicMock()
        response_body = MagicMock()
        response_body.read.return_value = json.dumps({
            "content": [{"text": json.dumps({
                "category": "rejected",
                "confidence_score": 0.8,
                "rejection_reason_summary": "文件不齊全，需補正",
                "analysis": "駁回",
            }, ensure_ascii=False)}]
        }).encode("utf-8")
        mock_client.invoke_model.return_value = {"body": response_body}

        result = analyze_taipower_reply(
            email_content="文件不齊全，請補正。",
            email_subject="退件",
            bedrock_client=mock_client,
        )

        assert result.category == "rejected"
        assert len(result.rejection_reason_summary) > 0


# =============================================================================
# Test: Bedrock failure and retry
# =============================================================================


class TestBedrockFailureAndRetry:
    """Tests for Bedrock invocation failure scenarios."""

    def test_bedrock_extract_failure_marks_document_as_fail(self):
        """When Bedrock extraction fails, the document is marked as 'fail'."""
        with patch(
            "dreams_workflow.ai_determination.app.invoke_bedrock_extract",
            side_effect=BedrockInvocationError("Model throttled"),
        ):
            mock_client = MagicMock()
            questionnaire_data = _make_questionnaire_data_matching()

            supporting_documents = [
                (f"doc_{i}.pdf", b"%PDF-1.4\nfake content")
                for i in range(5)
            ]
            document_metadata = [{"field_id": fid} for fid in _ATTACHMENT_FIELD_IDS]

            report = compare_documents(
                questionnaire_data=questionnaire_data,
                supporting_documents=supporting_documents,
                document_metadata=document_metadata,
                case_id="CASE-FAIL",
                bedrock_client=mock_client,
            )

            # Documents requiring LLM should fail (doc_1 through doc_4)
            # doc_5 is check_upload_only so it passes
            for i in range(4):
                assert report.results[i].status == "fail"
                assert "AI" in report.results[i].reason or "失敗" in report.results[i].reason

    def test_semantic_analysis_failure_raises_error(self):
        """When semantic analysis Bedrock call fails, SemanticAnalysisError is raised."""
        with patch(
            "dreams_workflow.ai_determination.semantic_analyzer._invoke_bedrock_analysis",
            side_effect=SemanticAnalysisError("Bedrock throttled"),
        ):
            mock_client = MagicMock()

            with pytest.raises(SemanticAnalysisError):
                analyze_taipower_reply(
                    email_content="測試內容",
                    email_subject="測試主旨",
                    bedrock_client=mock_client,
                )


# =============================================================================
# Test: Lambda handler
# =============================================================================


class TestLambdaHandler:
    """Tests for the Lambda handler entry point."""

    def test_lambda_handler_success(self):
        """Lambda handler returns 200 with ComparisonReport on success."""
        import base64

        with patch(
            "dreams_workflow.ai_determination.app.compare_documents"
        ) as mock_compare:
            mock_compare.return_value = ComparisonReport(
                case_id="CASE-001",
                overall_status="all_pass",
                results=[
                    DocumentComparisonResult(
                        document_id=f"doc_{i+1}",
                        document_name=f"Document {i+1}",
                        status="pass",
                        reason="所有欄位比對一致",
                    )
                    for i in range(5)
                ],
                timestamp="2025-01-01T00:00:00+00:00",
            )

            event = {
                "case_id": "CASE-001",
                "questionnaire_data": {"field1": "value1"},
                "supporting_documents": [
                    {
                        "field_id": "1014650",
                        "file_name": "doc.pdf",
                        "content_b64": base64.b64encode(b"fake").decode(),
                    }
                ],
            }

            response = lambda_handler(event, None)

            assert response["statusCode"] == 200
            body = json.loads(response["body"])
            assert body["overall_status"] == "all_pass"
            assert len(body["results"]) == 5

    def test_lambda_handler_error_returns_500(self):
        """Lambda handler returns 500 on unexpected error."""
        with patch(
            "dreams_workflow.ai_determination.app.compare_documents",
            side_effect=RuntimeError("Unexpected error"),
        ):
            event = {
                "case_id": "CASE-ERR",
                "questionnaire_data": {},
                "supporting_documents": [],
            }

            response = lambda_handler(event, None)

            assert response["statusCode"] == 500
            body = json.loads(response["body"])
            assert "error" in body


# =============================================================================
# Test: Utility functions
# =============================================================================


class TestUtilityFunctions:
    """Tests for utility functions in bedrock_client."""

    def test_detect_media_type_pdf_by_extension(self):
        """PDF extension is detected correctly."""
        assert detect_media_type("document.pdf", b"") == "application/pdf"

    def test_detect_media_type_jpeg_by_extension(self):
        """JPEG extension is detected correctly."""
        assert detect_media_type("photo.jpg", b"") == "image/jpeg"
        assert detect_media_type("photo.jpeg", b"") == "image/jpeg"

    def test_detect_media_type_png_by_extension(self):
        """PNG extension is detected correctly."""
        assert detect_media_type("image.png", b"") == "image/png"

    def test_detect_media_type_pdf_by_magic_bytes(self):
        """PDF magic bytes are detected correctly."""
        assert detect_media_type("unknown", b"%PDF-1.4\n") == "application/pdf"

    def test_detect_media_type_jpeg_by_magic_bytes(self):
        """JPEG magic bytes are detected correctly."""
        assert detect_media_type("unknown", b"\xff\xd8\xff\xe0") == "image/jpeg"

    def test_detect_media_type_png_by_magic_bytes(self):
        """PNG magic bytes are detected correctly."""
        assert detect_media_type("unknown", b"\x89PNG\r\n\x1a\n") == "image/png"

    def test_fix_dual_voltage_corrects_wrong_extraction(self):
        """fix_dual_voltage corrects when LLM extracts the wrong voltage."""
        # Evidence shows 11.4/22.8kV, but LLM extracted 22.8kV
        result = fix_dual_voltage("22.8kV", "併聯點電壓：3Ø3W 11.4/22.8kV")
        assert result == "11.4kV"

    def test_fix_dual_voltage_keeps_correct_extraction(self):
        """fix_dual_voltage keeps value when LLM extracted correctly."""
        result = fix_dual_voltage("11.4kV", "併聯點電壓：3Ø3W 11.4/22.8kV")
        assert result == "11.4kV"

    def test_fix_dual_voltage_no_dual_in_evidence(self):
        """fix_dual_voltage returns original when no dual voltage in evidence."""
        result = fix_dual_voltage("380V", "電壓：380V")
        assert result == "380V"
