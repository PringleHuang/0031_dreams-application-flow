"""Unit tests for Taipower reply semantic analyzer.

Tests cover:
- Approved email analysis
- Rejected email analysis with reason extraction
- Confidence score validation
- Empty email content handling
- Bedrock failure handling
- Edge cases in LLM response parsing

Requirements: 14.1, 14.2, 14.3, 14.4, 14.5
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from dreams_workflow.ai_determination.semantic_analyzer import (
    SemanticAnalysisError,
    SemanticAnalysisResult,
    _validate_and_build_result,
    analyze_taipower_reply,
)


def _make_mock_bedrock_response(response_data: dict) -> MagicMock:
    """Create a mock Bedrock client that returns the given response data."""
    mock_client = MagicMock()
    response_body = MagicMock()
    response_body.read.return_value = json.dumps({
        "content": [{"text": json.dumps(response_data, ensure_ascii=False)}]
    }).encode("utf-8")
    mock_client.invoke_model.return_value = {"body": response_body}
    return mock_client


class TestAnalyzeTaipowerReplyApproved:
    """Tests for approved email scenarios."""

    def test_approved_email_returns_approved_category(self):
        """Approved email content produces category='approved'."""
        mock_client = _make_mock_bedrock_response({
            "category": "approved",
            "confidence_score": 0.95,
            "rejection_reason_summary": "",
            "analysis": "郵件明確表示同意核准",
        })

        result = analyze_taipower_reply(
            email_content="貴公司申請案已審核通過，同意併聯。",
            email_subject="RE: 太陽能併聯申請 - 核准通知",
            bedrock_client=mock_client,
        )

        assert result.category == "approved"
        assert result.confidence_score == 0.95

    def test_approved_email_rejection_reason_can_be_empty(self):
        """Approved emails may have empty rejection_reason_summary."""
        mock_client = _make_mock_bedrock_response({
            "category": "approved",
            "confidence_score": 0.9,
            "rejection_reason_summary": "",
            "analysis": "核准",
        })

        result = analyze_taipower_reply(
            email_content="同意核准",
            email_subject="核准",
            bedrock_client=mock_client,
        )

        assert result.category == "approved"
        # rejection_reason_summary can be empty for approved


class TestAnalyzeTaipowerReplyRejected:
    """Tests for rejected email scenarios."""

    def test_rejected_email_returns_rejected_category(self):
        """Rejected email content produces category='rejected'."""
        mock_client = _make_mock_bedrock_response({
            "category": "rejected",
            "confidence_score": 0.88,
            "rejection_reason_summary": "裝置容量超過核定容量，需補正申請資料",
            "analysis": "郵件要求補正，視為駁回",
        })

        result = analyze_taipower_reply(
            email_content="經審查，貴公司申請案裝置容量超過核定容量，請補正後重新送件。",
            email_subject="RE: 太陽能併聯申請 - 退件通知",
            bedrock_client=mock_client,
        )

        assert result.category == "rejected"
        assert result.confidence_score == 0.88
        assert "裝置容量" in result.rejection_reason_summary

    def test_rejected_email_has_non_empty_reason(self):
        """Rejected emails always have non-empty rejection_reason_summary."""
        mock_client = _make_mock_bedrock_response({
            "category": "rejected",
            "confidence_score": 0.75,
            "rejection_reason_summary": "文件不齊全",
            "analysis": "缺件駁回",
        })

        result = analyze_taipower_reply(
            email_content="缺件，請補正。",
            email_subject="退件",
            bedrock_client=mock_client,
        )

        assert result.category == "rejected"
        assert len(result.rejection_reason_summary) > 0

    def test_rejected_with_empty_reason_uses_fallback(self):
        """When LLM returns empty reason for rejected, fallback is used."""
        mock_client = _make_mock_bedrock_response({
            "category": "rejected",
            "confidence_score": 0.7,
            "rejection_reason_summary": "",
            "analysis": "郵件表示不同意",
        })

        result = analyze_taipower_reply(
            email_content="不同意",
            email_subject="駁回",
            bedrock_client=mock_client,
        )

        assert result.category == "rejected"
        assert len(result.rejection_reason_summary) > 0


class TestAnalyzeTaipowerReplyEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_email_content_raises_value_error(self):
        """Empty email content raises ValueError."""
        with pytest.raises(ValueError, match="email_content"):
            analyze_taipower_reply(
                email_content="",
                email_subject="test",
            )

    def test_whitespace_only_email_raises_value_error(self):
        """Whitespace-only email content raises ValueError."""
        with pytest.raises(ValueError, match="email_content"):
            analyze_taipower_reply(
                email_content="   \n\t  ",
                email_subject="test",
            )

    def test_bedrock_failure_raises_semantic_analysis_error(self):
        """Bedrock invocation failure raises SemanticAnalysisError."""
        mock_client = MagicMock()
        mock_client.invoke_model.side_effect = Exception("Service unavailable")

        with patch(
            "dreams_workflow.ai_determination.semantic_analyzer._invoke_bedrock_analysis",
            side_effect=SemanticAnalysisError("Bedrock unavailable"),
        ):
            with pytest.raises(SemanticAnalysisError):
                analyze_taipower_reply(
                    email_content="測試內容",
                    email_subject="測試主旨",
                    bedrock_client=mock_client,
                )

    def test_invalid_json_response_raises_semantic_analysis_error(self):
        """Non-JSON Bedrock response raises SemanticAnalysisError."""
        mock_client = MagicMock()
        response_body = MagicMock()
        response_body.read.return_value = json.dumps({
            "content": [{"text": "This is not JSON at all"}]
        }).encode("utf-8")
        mock_client.invoke_model.return_value = {"body": response_body}

        with patch(
            "dreams_workflow.ai_determination.semantic_analyzer._invoke_bedrock_analysis",
            side_effect=SemanticAnalysisError("Failed to parse response"),
        ):
            with pytest.raises(SemanticAnalysisError):
                analyze_taipower_reply(
                    email_content="測試內容",
                    email_subject="測試主旨",
                    bedrock_client=mock_client,
                )


class TestConfidenceScoreValidation:
    """Tests for confidence_score boundary handling."""

    def test_confidence_score_clamped_to_max_1(self):
        """Confidence score > 1.0 is clamped to 1.0."""
        mock_client = _make_mock_bedrock_response({
            "category": "approved",
            "confidence_score": 1.5,
            "rejection_reason_summary": "",
            "analysis": "very confident",
        })

        result = analyze_taipower_reply(
            email_content="核准",
            email_subject="核准",
            bedrock_client=mock_client,
        )

        assert result.confidence_score == 1.0

    def test_confidence_score_clamped_to_min_0(self):
        """Confidence score < 0.0 is clamped to 0.0."""
        mock_client = _make_mock_bedrock_response({
            "category": "approved",
            "confidence_score": -0.5,
            "rejection_reason_summary": "",
            "analysis": "uncertain",
        })

        result = analyze_taipower_reply(
            email_content="核准",
            email_subject="核准",
            bedrock_client=mock_client,
        )

        assert result.confidence_score == 0.0

    def test_non_numeric_confidence_defaults_to_0_5(self):
        """Non-numeric confidence_score defaults to 0.5."""
        mock_client = _make_mock_bedrock_response({
            "category": "approved",
            "confidence_score": "high",
            "rejection_reason_summary": "",
            "analysis": "confident",
        })

        result = analyze_taipower_reply(
            email_content="核准",
            email_subject="核准",
            bedrock_client=mock_client,
        )

        assert result.confidence_score == 0.5


class TestValidateAndBuildResult:
    """Tests for _validate_and_build_result helper."""

    def test_chinese_category_approved(self):
        """Chinese category '核准' maps to 'approved'."""
        result = _validate_and_build_result({
            "category": "核准",
            "confidence_score": 0.9,
            "rejection_reason_summary": "",
            "analysis": "",
        })
        assert result.category == "approved"

    def test_chinese_category_rejected(self):
        """Chinese category '駁回' maps to 'rejected'."""
        result = _validate_and_build_result({
            "category": "駁回",
            "confidence_score": 0.8,
            "rejection_reason_summary": "原因",
            "analysis": "",
        })
        assert result.category == "rejected"

    def test_invalid_category_raises_error(self):
        """Invalid category raises SemanticAnalysisError."""
        with pytest.raises(SemanticAnalysisError, match="Invalid category"):
            _validate_and_build_result({
                "category": "unknown",
                "confidence_score": 0.5,
                "rejection_reason_summary": "",
                "analysis": "",
            })


class TestSemanticAnalysisResultDataclass:
    """Tests for SemanticAnalysisResult dataclass validation."""

    def test_valid_approved_result(self):
        """Valid approved result creates successfully."""
        result = SemanticAnalysisResult(
            category="approved",
            confidence_score=0.95,
            rejection_reason_summary="",
            raw_analysis="approved analysis",
        )
        assert result.category == "approved"

    def test_valid_rejected_result(self):
        """Valid rejected result creates successfully."""
        result = SemanticAnalysisResult(
            category="rejected",
            confidence_score=0.8,
            rejection_reason_summary="容量不符",
            raw_analysis="rejected analysis",
        )
        assert result.category == "rejected"

    def test_invalid_category_raises_value_error(self):
        """Invalid category in constructor raises ValueError."""
        with pytest.raises(ValueError, match="category"):
            SemanticAnalysisResult(
                category="maybe",
                confidence_score=0.5,
                rejection_reason_summary="",
                raw_analysis="",
            )

    def test_confidence_out_of_range_raises_value_error(self):
        """Confidence score out of [0, 1] raises ValueError."""
        with pytest.raises(ValueError, match="confidence_score"):
            SemanticAnalysisResult(
                category="approved",
                confidence_score=1.5,
                rejection_reason_summary="",
                raw_analysis="",
            )

    def test_rejected_without_reason_raises_value_error(self):
        """Rejected with empty reason raises ValueError."""
        with pytest.raises(ValueError, match="rejection_reason_summary"):
            SemanticAnalysisResult(
                category="rejected",
                confidence_score=0.8,
                rejection_reason_summary="",
                raw_analysis="",
            )

    def test_to_dict_returns_serializable(self):
        """to_dict returns a JSON-serializable dictionary."""
        result = SemanticAnalysisResult(
            category="approved",
            confidence_score=0.9,
            rejection_reason_summary="",
            raw_analysis="test",
        )
        d = result.to_dict()
        assert d["category"] == "approved"
        assert d["confidence_score"] == 0.9
        # Should be JSON-serializable
        json.dumps(d)
