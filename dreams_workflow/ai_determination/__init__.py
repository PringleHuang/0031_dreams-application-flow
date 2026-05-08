"""AI determination service Lambda function.

Provides document comparison and semantic analysis capabilities
using AWS Bedrock for the DREAMS workflow system.
"""

from dreams_workflow.ai_determination.app import (
    ComparisonReport,
    DocumentComparisonResult,
    compare_documents,
    lambda_handler,
)
from dreams_workflow.ai_determination.semantic_analyzer import (
    SemanticAnalysisError,
    SemanticAnalysisResult,
    analyze_taipower_reply,
)

__all__ = [
    "ComparisonReport",
    "DocumentComparisonResult",
    "SemanticAnalysisError",
    "SemanticAnalysisResult",
    "analyze_taipower_reply",
    "compare_documents",
    "lambda_handler",
]
