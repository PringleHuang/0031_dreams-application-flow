"""Taipower reply semantic analysis using AWS Bedrock.

Analyzes email replies from Taipower to determine whether an application
has been approved or rejected, extracting rejection reasons when applicable.

Uses AWS Bedrock Claude for semantic analysis with retry mechanism
(max 2 attempts, 5 second interval).

Requirements: 14.1, 14.2, 14.3, 14.4, 14.5
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Literal

import boto3
from tenacity import retry, stop_after_attempt, wait_fixed

from dreams_workflow.ai_determination.config import get_bedrock_config
from dreams_workflow.shared.logger import get_logger, log_operation

logger = get_logger(__name__)

# Bedrock retry configuration: max 2 attempts, 5 second interval
_BEDROCK_MAX_RETRIES = 2
_BEDROCK_WAIT_SECONDS = 5


class SemanticAnalysisError(Exception):
    """Raised when semantic analysis fails after retries."""

    pass


@dataclass
class SemanticAnalysisResult:
    """Result of analyzing a Taipower reply email.

    Attributes:
        category: "approved" if the application was approved,
            "rejected" if it was rejected.
        confidence_score: Confidence level between 0.0 and 1.0.
        rejection_reason_summary: Summary of rejection reasons.
            Non-empty string when category is "rejected", may be empty/None
            when category is "approved".
        raw_analysis: Raw analysis text from the LLM for debugging.
    """

    category: Literal["approved", "rejected"]
    confidence_score: float
    rejection_reason_summary: str
    raw_analysis: str

    def __post_init__(self):
        """Validate invariants after initialization."""
        if self.category not in ("approved", "rejected"):
            raise ValueError(
                f"category must be 'approved' or 'rejected', got '{self.category}'"
            )
        if not (0.0 <= self.confidence_score <= 1.0):
            raise ValueError(
                f"confidence_score must be between 0.0 and 1.0, got {self.confidence_score}"
            )
        if self.category == "rejected" and not self.rejection_reason_summary:
            raise ValueError(
                "rejection_reason_summary must be non-empty when category is 'rejected'"
            )

    def to_dict(self) -> dict:
        """Convert to a JSON-serializable dictionary."""
        return asdict(self)


def _build_analysis_prompt(email_content: str, email_subject: str) -> str:
    """Build the LLM prompt for Taipower reply semantic analysis.

    Args:
        email_content: The email body text.
        email_subject: The email subject line.

    Returns:
        Formatted prompt string for Bedrock Claude.
    """
    return f"""你是一個台電審核回覆郵件分析助手。請分析以下台電回覆郵件，判定審核結果為「核准」或「駁回」。

郵件主旨：{email_subject}

郵件內容：
{email_content}

分析規則：
1. 判定郵件內容表達的審核結果是「核准」還是「駁回」
2. 核准的常見關鍵詞：同意、核准、通過、准予、許可、合格、符合規定
3. 駁回的常見關鍵詞：駁回、不同意、退件、補正、不符、缺件、未符合、請補、需補
4. 如果郵件要求補件或補正，視為「駁回」
5. 給出信心分數（0.0~1.0），表示你對判定結果的確信程度
6. 如果判定為「駁回」，請摘要駁回原因

回覆格式（JSON）：
{{
    "category": "approved" 或 "rejected",
    "confidence_score": 0.0~1.0 的數值,
    "rejection_reason_summary": "駁回原因摘要（核准時填空字串）",
    "analysis": "簡短分析說明"
}}

只回覆 JSON，不要加任何其他說明文字。"""


def _create_retry_decorator():
    """Create a retry decorator for Bedrock semantic analysis calls."""
    return retry(
        stop=stop_after_attempt(_BEDROCK_MAX_RETRIES),
        wait=wait_fixed(_BEDROCK_WAIT_SECONDS),
        retry=lambda retry_state: (
            isinstance(retry_state.outcome.exception(), SemanticAnalysisError)
            if retry_state.outcome and retry_state.outcome.failed
            else False
        ),
        reraise=True,
    )


@_create_retry_decorator()
def _invoke_bedrock_analysis(
    client: Any,
    model_id: str,
    prompt: str,
) -> dict:
    """Invoke Bedrock Claude for semantic analysis.

    Args:
        client: boto3 bedrock-runtime client.
        model_id: Bedrock model ID.
        prompt: Analysis prompt.

    Returns:
        Parsed JSON dict with analysis results.

    Raises:
        SemanticAnalysisError: On invocation failure (triggers retry).
    """
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": prompt}]}
        ],
    }

    try:
        response = client.invoke_model(
            modelId=model_id,
            contentType="application/json",
            body=json.dumps(body),
        )
        result = json.loads(response["body"].read())
        text = result["content"][0]["text"]

        # Strip markdown code fences if present
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0].strip()

        return json.loads(text)
    except json.JSONDecodeError as e:
        raise SemanticAnalysisError(
            f"Failed to parse Bedrock response as JSON: {e}"
        ) from e
    except Exception as e:
        raise SemanticAnalysisError(
            f"Bedrock semantic analysis invocation failed: {e}"
        ) from e


def _validate_and_build_result(parsed: dict) -> SemanticAnalysisResult:
    """Validate parsed LLM response and build SemanticAnalysisResult.

    Applies defensive normalization to handle edge cases in LLM output.

    Args:
        parsed: Parsed JSON dict from LLM response.

    Returns:
        Validated SemanticAnalysisResult.

    Raises:
        SemanticAnalysisError: If the response cannot be normalized to valid result.
    """
    # Extract and normalize category
    raw_category = str(parsed.get("category", "")).strip().lower()
    if raw_category in ("approved", "核准", "通過", "同意"):
        category = "approved"
    elif raw_category in ("rejected", "駁回", "不同意", "退件"):
        category = "rejected"
    else:
        raise SemanticAnalysisError(
            f"Invalid category in LLM response: '{raw_category}'"
        )

    # Extract and normalize confidence_score
    try:
        confidence_score = float(parsed.get("confidence_score", 0.0))
    except (TypeError, ValueError):
        confidence_score = 0.5  # Default to moderate confidence

    # Clamp to valid range
    confidence_score = max(0.0, min(1.0, confidence_score))

    # Extract rejection reason summary
    rejection_reason_summary = str(
        parsed.get("rejection_reason_summary", "")
    ).strip()

    # Ensure non-empty reason for rejected cases
    if category == "rejected" and not rejection_reason_summary:
        # Try to extract from analysis field as fallback
        analysis = str(parsed.get("analysis", "")).strip()
        if analysis:
            rejection_reason_summary = analysis
        else:
            rejection_reason_summary = "台電審核未通過（具體原因未明確說明）"

    # Extract raw analysis
    raw_analysis = str(parsed.get("analysis", "")).strip()

    return SemanticAnalysisResult(
        category=category,
        confidence_score=confidence_score,
        rejection_reason_summary=rejection_reason_summary,
        raw_analysis=raw_analysis,
    )


def analyze_taipower_reply(
    email_content: str,
    email_subject: str,
    bedrock_client: Any | None = None,
) -> SemanticAnalysisResult:
    """Analyze a Taipower reply email to determine approval/rejection.

    Uses AWS Bedrock Claude to perform semantic analysis on the email content,
    determining whether the application was approved or rejected.

    Args:
        email_content: The email body text content.
        email_subject: The email subject line.
        bedrock_client: Optional pre-configured boto3 bedrock-runtime client.
            If None, creates one from environment configuration.

    Returns:
        SemanticAnalysisResult with category, confidence_score,
        and rejection_reason_summary.

    Raises:
        SemanticAnalysisError: If analysis fails after retries.
        ValueError: If email_content is empty.
    """
    if not email_content or not email_content.strip():
        raise ValueError("email_content must be a non-empty string")

    log_operation(
        logger,
        case_id="",
        operation_type="semantic_analysis_start",
        message=f"Starting Taipower reply analysis, subject: {email_subject[:50]}",
    )

    # Initialize Bedrock client if not provided
    if bedrock_client is None:
        bedrock_cfg = get_bedrock_config()
        bedrock_client = boto3.client(
            "bedrock-runtime", region_name=bedrock_cfg["region"]
        )
    else:
        bedrock_cfg = get_bedrock_config()

    model_id = bedrock_cfg["model_id"]

    # Build prompt and invoke Bedrock
    prompt = _build_analysis_prompt(email_content, email_subject)
    parsed = _invoke_bedrock_analysis(bedrock_client, model_id, prompt)

    # Validate and build result
    result = _validate_and_build_result(parsed)

    log_operation(
        logger,
        case_id="",
        operation_type="semantic_analysis_complete",
        message=(
            f"Analysis complete: {result.category} "
            f"(confidence: {result.confidence_score:.2f})"
        ),
    )

    return result
