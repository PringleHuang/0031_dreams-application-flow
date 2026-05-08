"""Property-based tests for Taipower reply semantic analysis.

Property 4: 台電回覆語意分析結果有效性
Validates: Requirements 14.1, 14.2, 14.3, 14.5

Uses hypothesis to generate random email content and verifies:
- category is always "approved" or "rejected"
- confidence_score is always between 0.0 and 1.0
- When category is "rejected", rejection_reason_summary is non-empty
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, strategies as st

from dreams_workflow.ai_determination.semantic_analyzer import (
    SemanticAnalysisResult,
    _validate_and_build_result,
    analyze_taipower_reply,
)


# =============================================================================
# Strategies for generating test data
# =============================================================================

# Strategy for generating random email content (non-empty)
email_content_strategy = st.text(min_size=1, max_size=200).filter(
    lambda s: s.strip()
)

# Strategy for generating random email subjects
email_subject_strategy = st.text(min_size=0, max_size=100)

# Strategy for generating valid LLM response categories
category_strategy = st.sampled_from(["approved", "rejected"])

# Strategy for generating confidence scores (valid range)
confidence_strategy = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)

# Strategy for generating rejection reasons (non-empty for rejected)
rejection_reason_strategy = st.text(min_size=1, max_size=100)

# Strategy for generating a valid LLM response dict
valid_llm_response_strategy = st.fixed_dictionaries({
    "category": category_strategy,
    "confidence_score": confidence_strategy,
    "rejection_reason_summary": st.text(min_size=0, max_size=100),
    "analysis": st.text(min_size=0, max_size=100),
})


def _make_mock_bedrock_response(response_data: dict) -> MagicMock:
    """Create a mock Bedrock client that returns the given response data."""
    mock_client = MagicMock()
    response_body = MagicMock()
    response_body.read.return_value = json.dumps({
        "content": [{"text": json.dumps(response_data, ensure_ascii=False)}]
    }).encode("utf-8")
    mock_client.invoke_model.return_value = {"body": response_body}
    return mock_client


# =============================================================================
# Property Tests
# =============================================================================


class TestSemanticAnalysisResultProperty:
    """Property 4: 台電回覆語意分析結果有效性"""

    # Feature: dreams-application-flow, Property 4: 台電回覆語意分析結果有效性

    @settings(max_examples=100)
    @given(
        email_content=email_content_strategy,
        email_subject=email_subject_strategy,
        category=category_strategy,
        confidence=confidence_strategy,
    )
    def test_category_is_always_approved_or_rejected(
        self,
        email_content: str,
        email_subject: str,
        category: str,
        confidence: float,
    ):
        """analyze_taipower_reply always returns category 'approved' or 'rejected'."""
        # Build a valid response with the generated category
        reason = "駁回原因" if category == "rejected" else ""
        mock_client = _make_mock_bedrock_response({
            "category": category,
            "confidence_score": confidence,
            "rejection_reason_summary": reason,
            "analysis": "分析結果",
        })

        result = analyze_taipower_reply(
            email_content=email_content,
            email_subject=email_subject,
            bedrock_client=mock_client,
        )

        assert result.category in ("approved", "rejected")

    @settings(max_examples=100)
    @given(
        email_content=email_content_strategy,
        email_subject=email_subject_strategy,
        category=category_strategy,
        confidence=confidence_strategy,
    )
    def test_confidence_score_between_0_and_1(
        self,
        email_content: str,
        email_subject: str,
        category: str,
        confidence: float,
    ):
        """confidence_score is always between 0.0 and 1.0 inclusive."""
        reason = "駁回原因" if category == "rejected" else ""
        mock_client = _make_mock_bedrock_response({
            "category": category,
            "confidence_score": confidence,
            "rejection_reason_summary": reason,
            "analysis": "分析結果",
        })

        result = analyze_taipower_reply(
            email_content=email_content,
            email_subject=email_subject,
            bedrock_client=mock_client,
        )

        assert 0.0 <= result.confidence_score <= 1.0

    @settings(max_examples=100)
    @given(
        email_content=email_content_strategy,
        email_subject=email_subject_strategy,
        confidence=confidence_strategy,
        reason=rejection_reason_strategy,
    )
    def test_rejected_always_has_non_empty_reason(
        self,
        email_content: str,
        email_subject: str,
        confidence: float,
        reason: str,
    ):
        """When category is 'rejected', rejection_reason_summary is non-empty."""
        mock_client = _make_mock_bedrock_response({
            "category": "rejected",
            "confidence_score": confidence,
            "rejection_reason_summary": reason,
            "analysis": "駁回分析",
        })

        result = analyze_taipower_reply(
            email_content=email_content,
            email_subject=email_subject,
            bedrock_client=mock_client,
        )

        if result.category == "rejected":
            assert result.rejection_reason_summary is not None
            assert len(result.rejection_reason_summary) > 0

    @settings(max_examples=100)
    @given(
        email_content=email_content_strategy,
        email_subject=email_subject_strategy,
        confidence=confidence_strategy,
    )
    def test_rejected_with_empty_reason_gets_fallback(
        self,
        email_content: str,
        email_subject: str,
        confidence: float,
    ):
        """When LLM returns rejected with empty reason, a fallback reason is provided."""
        mock_client = _make_mock_bedrock_response({
            "category": "rejected",
            "confidence_score": confidence,
            "rejection_reason_summary": "",  # Empty reason
            "analysis": "分析說明",
        })

        result = analyze_taipower_reply(
            email_content=email_content,
            email_subject=email_subject,
            bedrock_client=mock_client,
        )

        # Should still have non-empty reason (fallback mechanism)
        assert result.category == "rejected"
        assert len(result.rejection_reason_summary) > 0

    @settings(max_examples=100)
    @given(
        confidence=st.floats(
            min_value=-100.0, max_value=100.0, allow_nan=False, allow_infinity=False
        ),
        category=category_strategy,
    )
    def test_confidence_score_clamped_to_valid_range(
        self,
        confidence: float,
        category: str,
    ):
        """Any confidence_score from LLM is clamped to [0.0, 1.0]."""
        reason = "原因" if category == "rejected" else ""
        mock_client = _make_mock_bedrock_response({
            "category": category,
            "confidence_score": confidence,
            "rejection_reason_summary": reason,
            "analysis": "分析",
        })

        result = analyze_taipower_reply(
            email_content="測試郵件內容",
            email_subject="測試主旨",
            bedrock_client=mock_client,
        )

        assert 0.0 <= result.confidence_score <= 1.0

    @settings(max_examples=50)
    @given(
        category=category_strategy,
        confidence=confidence_strategy,
        reason=st.text(min_size=0, max_size=50),
        analysis=st.text(min_size=0, max_size=50),
    )
    def test_validate_and_build_result_invariants(
        self,
        category: str,
        confidence: float,
        reason: str,
        analysis: str,
    ):
        """_validate_and_build_result always produces valid SemanticAnalysisResult."""
        parsed = {
            "category": category,
            "confidence_score": confidence,
            "rejection_reason_summary": reason,
            "analysis": analysis,
        }

        result = _validate_and_build_result(parsed)

        # Category is always valid
        assert result.category in ("approved", "rejected")
        # Confidence is always in range
        assert 0.0 <= result.confidence_score <= 1.0
        # Rejected always has reason
        if result.category == "rejected":
            assert len(result.rejection_reason_summary) > 0

    @settings(max_examples=50)
    @given(
        email_content=email_content_strategy,
        email_subject=email_subject_strategy,
        category=category_strategy,
        confidence=confidence_strategy,
    )
    def test_result_is_json_serializable(
        self,
        email_content: str,
        email_subject: str,
        category: str,
        confidence: float,
    ):
        """The result's to_dict() output is always JSON-serializable."""
        reason = "駁回原因" if category == "rejected" else ""
        mock_client = _make_mock_bedrock_response({
            "category": category,
            "confidence_score": confidence,
            "rejection_reason_summary": reason,
            "analysis": "分析",
        })

        result = analyze_taipower_reply(
            email_content=email_content,
            email_subject=email_subject,
            bedrock_client=mock_client,
        )

        # Should not raise
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        assert len(serialized) > 0
