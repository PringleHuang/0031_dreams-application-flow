"""AI Determination Service - Lambda handler for document comparison.

This module implements the AI-powered supporting document comparison logic.
It receives questionnaire data and supporting documents, uses AWS Bedrock
to extract structured information from documents, and compares them against
the questionnaire form values.

The main entry point is `compare_documents()` which returns a ComparisonReport
containing exactly 5 DocumentComparisonResult entries (one per document).

Requirements: 13.1, 13.2, 13.3, 13.4
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import boto3

from dreams_workflow.ai_determination.bedrock_client import (
    BedrockInvocationError,
    build_extract_prompt,
    detect_media_type,
    invoke_bedrock_extract,
    invoke_bedrock_normalize,
)
from dreams_workflow.ai_determination.comparator import (
    compare_inverters,
    compare_values,
)
from dreams_workflow.ai_determination.config import (
    ALLOWED_VALUES,
    ATTACHMENTS_CONFIG,
    get_bedrock_config,
)
from dreams_workflow.shared.logger import get_logger, log_operation

logger = get_logger(__name__)

# Number of expected documents (always 5)
EXPECTED_DOCUMENT_COUNT = 5


@dataclass
class DocumentComparisonResult:
    """Result of comparing a single supporting document against form data.

    Attributes:
        document_id: Unique identifier for the document.
        document_name: Human-readable document name.
        status: "pass" if all fields match, "fail" otherwise.
        reason: Non-empty explanation of the comparison result.
    """

    document_id: str
    document_name: str
    status: Literal["pass", "fail"]
    reason: str


@dataclass
class ComparisonReport:
    """Complete comparison report for all 5 supporting documents.

    Attributes:
        case_id: The case identifier.
        overall_status: "all_pass" if all documents pass, "has_failures" otherwise.
        results: Exactly 5 DocumentComparisonResult entries.
        timestamp: ISO 8601 timestamp of when the comparison was performed.
    """

    case_id: str
    overall_status: Literal["all_pass", "has_failures"]
    results: list[DocumentComparisonResult]
    timestamp: str

    def to_dict(self) -> dict:
        """Convert to a JSON-serializable dictionary."""
        return {
            "case_id": self.case_id,
            "overall_status": self.overall_status,
            "results": [asdict(r) for r in self.results],
            "timestamp": self.timestamp,
        }


def compare_documents(
    questionnaire_data: dict,
    supporting_documents: list[tuple[str, bytes]],
    document_metadata: list[dict],
    case_id: str = "",
    bedrock_client: Any | None = None,
    extracted_values_out: dict[str, dict[str, str]] | None = None,
    field_comparisons_out: dict[str, list[dict]] | None = None,
) -> ComparisonReport:
    """Compare supporting documents against questionnaire form data.

    Uses AWS Bedrock to extract structured information from each document,
    then compares extracted values against the questionnaire form values.

    Always returns exactly 5 DocumentComparisonResult entries, one for each
    expected document. Documents that are missing or fail to process are
    marked as "fail" with an appropriate reason.

    Args:
        questionnaire_data: Questionnaire form data (field_id -> value mapping).
        supporting_documents: List of (filename, file_bytes) tuples.
            Should contain up to 5 documents.
        document_metadata: List of metadata dicts for each document, containing
            at minimum 'field_id' to match against ATTACHMENTS_CONFIG.
        case_id: Case identifier for logging.
        bedrock_client: Optional pre-configured boto3 bedrock-runtime client.
            If None, creates one from environment configuration.
        extracted_values_out: Optional dict to populate with LLM-extracted values
            per document. Format: {document_name: {questionnaire_field_id: extracted_value}}.
            If provided, will be populated during comparison.
        field_comparisons_out: Optional dict to populate with per-field comparison
            results per document. Format: {document_name: [comparison_dict, ...]}.
            Each comparison_dict has: form_field_id, match, extracted, form_value, note.

    Returns:
        ComparisonReport with exactly 5 results and overall status.
    """
    log_operation(
        logger,
        case_id=case_id,
        operation_type="document_comparison_start",
        message=f"Starting document comparison for case {case_id} "
        f"with {len(supporting_documents)} documents",
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
    max_tokens = bedrock_cfg["max_tokens"]

    # Build a lookup from field_id to (filename, bytes)
    doc_lookup: dict[str, tuple[str, bytes]] = {}
    for i, meta in enumerate(document_metadata):
        field_id = meta.get("field_id", "")
        if i < len(supporting_documents):
            doc_lookup[field_id] = supporting_documents[i]

    # Process each of the 5 expected documents
    results: list[DocumentComparisonResult] = []

    for att_cfg in ATTACHMENTS_CONFIG:
        doc_id = att_cfg["document_id"]
        doc_name = att_cfg["document_name"]
        field_id = att_cfg["field_id"]

        # Check-upload-only documents (e.g., 併聯審查意見書)
        if att_cfg.get("check_upload_only"):
            has_doc = field_id in doc_lookup and doc_lookup[field_id][1]
            if has_doc:
                results.append(DocumentComparisonResult(
                    document_id=doc_id,
                    document_name=doc_name,
                    status="pass",
                    reason="文件已上傳確認",
                ))
            else:
                results.append(DocumentComparisonResult(
                    document_id=doc_id,
                    document_name=doc_name,
                    status="fail",
                    reason="未上傳必要文件",
                ))
            continue

        # Check if document is available
        if field_id not in doc_lookup:
            results.append(DocumentComparisonResult(
                document_id=doc_id,
                document_name=doc_name,
                status="fail",
                reason=f"缺少文件：{doc_name}",
            ))
            continue

        file_name, file_bytes = doc_lookup[field_id]
        if not file_bytes:
            results.append(DocumentComparisonResult(
                document_id=doc_id,
                document_name=doc_name,
                status="fail",
                reason=f"文件內容為空：{doc_name}",
            ))
            continue

        # Extract data from document using Bedrock
        try:
            media_type = detect_media_type(file_name, file_bytes)
            prompt = build_extract_prompt(att_cfg, ALLOWED_VALUES)
            extracted = invoke_bedrock_extract(
                bedrock_client, model_id, max_tokens, file_bytes, media_type, prompt
            )
        except BedrockInvocationError as e:
            log_operation(
                logger,
                case_id=case_id,
                operation_type="bedrock_extract_failed",
                message=f"Bedrock extraction failed for {doc_name}: {e}",
                level="error",
            )
            results.append(DocumentComparisonResult(
                document_id=doc_id,
                document_name=doc_name,
                status="fail",
                reason=f"AI 文件判讀失敗：{str(e)[:100]}",
            ))
            continue

        if not extracted:
            results.append(DocumentComparisonResult(
                document_id=doc_id,
                document_name=doc_name,
                status="fail",
                reason="AI 文件判讀結果為空",
            ))
            continue

        # Capture extracted values if output dict is provided
        if extracted_values_out is not None:
            doc_extracted_fields: dict[str, str] = {}
            for extract_field in att_cfg.get("extract_fields", []):
                extract_key = extract_field.get("extract_key", "")
                form_field_id = extract_field.get("form_field_id", "")

                # Special handling for inverter_array
                if extract_field.get("type") == "inverter_array":
                    inv_data = extracted.get(extract_key, [])
                    if isinstance(inv_data, list) and inv_data:
                        # Format as {brand}|{model}|{quantity}, joined by ", "
                        parts = []
                        evidences = []
                        for item in inv_data:
                            if isinstance(item, dict):
                                brand = str(item.get("brand", "")).strip()
                                model = str(item.get("model", "")).strip()
                                qty = str(item.get("quantity", "")).strip()
                                ev = item.get("evidence", "")
                                if model and qty:
                                    if brand:
                                        parts.append(f"{brand}|{model}|{qty}")
                                    else:
                                        parts.append(f"{model}|{qty}")
                                    if ev:
                                        evidences.append(ev)
                        if parts:
                            formatted = ", ".join(parts)
                            if evidences:
                                formatted += f"\n[依據] {'; '.join(evidences)}"
                            # Use 1016553 as the key for inverter summary in LLM results
                            doc_extracted_fields["1016553"] = formatted
                    continue

                if extract_key and form_field_id:
                    raw = extracted.get(extract_key, "")
                    # Format: "value\n[依據] evidence" for writing to RAGIC
                    if isinstance(raw, dict):
                        value = raw.get("value", "")
                        evidence = raw.get("evidence", "")
                        if value:
                            formatted = str(value)
                            if evidence:
                                formatted += f"\n[依據] {evidence}"
                            doc_extracted_fields[form_field_id] = formatted
                    elif raw and not isinstance(raw, (list, dict)):
                        doc_extracted_fields[form_field_id] = str(raw)
            if doc_extracted_fields:
                extracted_values_out[doc_name] = doc_extracted_fields

        # Normalize form values using LLM
        form_normalized = _normalize_form_values(
            bedrock_client, model_id, questionnaire_data, att_cfg, case_id
        )

        # Compare extracted values against form values
        comparisons = _compare_document_fields(
            extracted, questionnaire_data, form_normalized, att_cfg
        )

        # Capture per-field comparison results if output dict is provided
        if field_comparisons_out is not None:
            field_comparisons_out[doc_name] = comparisons

        # Determine document pass/fail
        all_match = all(c.get("match", False) for c in comparisons)
        if all_match:
            results.append(DocumentComparisonResult(
                document_id=doc_id,
                document_name=doc_name,
                status="pass",
                reason="所有欄位比對一致",
            ))
        else:
            # Build failure reason from mismatched fields
            failed_fields = [
                c.get("form_field_name", c.get("extract_key", "unknown"))
                for c in comparisons
                if not c.get("match", False)
            ]
            reason = f"以下欄位不一致：{'、'.join(failed_fields)}"
            results.append(DocumentComparisonResult(
                document_id=doc_id,
                document_name=doc_name,
                status="fail",
                reason=reason,
            ))

        log_operation(
            logger,
            case_id=case_id,
            operation_type="document_comparison_result",
            message=f"Document '{doc_name}': {results[-1].status} - {results[-1].reason}",
        )

    # Ensure exactly 5 results
    assert len(results) == EXPECTED_DOCUMENT_COUNT, (
        f"Expected {EXPECTED_DOCUMENT_COUNT} results, got {len(results)}"
    )

    # Determine overall status
    overall_status: Literal["all_pass", "has_failures"] = (
        "all_pass" if all(r.status == "pass" for r in results) else "has_failures"
    )

    timestamp = datetime.now(timezone.utc).isoformat()

    report = ComparisonReport(
        case_id=case_id,
        overall_status=overall_status,
        results=results,
        timestamp=timestamp,
    )

    log_operation(
        logger,
        case_id=case_id,
        operation_type="document_comparison_complete",
        message=f"Comparison complete: {overall_status} "
        f"({sum(1 for r in results if r.status == 'pass')}/5 passed)",
    )

    return report


def _normalize_form_values(
    bedrock_client: Any,
    model_id: str,
    questionnaire_data: dict,
    att_cfg: dict,
    case_id: str,
) -> dict[str, Any]:
    """Normalize form values using LLM for better comparison accuracy.

    Args:
        bedrock_client: boto3 bedrock-runtime client.
        model_id: Bedrock model ID.
        questionnaire_data: Raw questionnaire form data.
        att_cfg: Attachment configuration with extract_fields.
        case_id: Case ID for logging.

    Returns:
        Dict mapping extract_key to normalized value.
    """
    fields_with_values: list[dict] = []
    for field_cfg in att_cfg["extract_fields"]:
        if field_cfg.get("type") == "inverter_array":
            continue
        ext_key = field_cfg["extract_key"]
        form_value = _get_form_value(questionnaire_data, field_cfg)
        if form_value is not None and str(form_value).strip():
            fields_with_values.append({
                "key": ext_key,
                "value": str(form_value).strip(),
                "description": field_cfg["description"],
            })

    if not fields_with_values:
        return {}

    try:
        return invoke_bedrock_normalize(bedrock_client, model_id, fields_with_values)
    except BedrockInvocationError as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="form_normalize_failed",
            message=f"Form normalization failed, using raw values: {e}",
            level="warning",
        )
        return {}


def _get_form_value(record: dict, field_cfg: dict) -> Any:
    """Get a form field value from the questionnaire record.

    Supports both main table fields and subtable fields.

    Args:
        record: Questionnaire record data.
        field_cfg: Field configuration with form_field_id and optional subtable.

    Returns:
        The field value, or None if not found.
    """
    subtable = field_cfg.get("subtable")
    field_id = field_cfg["form_field_id"]
    if subtable:
        sub_data = record.get(subtable, {})
        if sub_data and isinstance(sub_data, dict):
            first_row = next(iter(sub_data.values()), {})
            return first_row.get(field_id) if isinstance(first_row, dict) else None
        return None
    return record.get(field_id)


def _compare_document_fields(
    extracted: dict,
    questionnaire_data: dict,
    form_normalized: dict,
    att_cfg: dict,
) -> list[dict]:
    """Compare all extracted fields from a document against form values.

    Args:
        extracted: LLM-extracted data from the document.
        questionnaire_data: Raw questionnaire form data.
        form_normalized: LLM-normalized form values.
        att_cfg: Attachment configuration.

    Returns:
        List of comparison result dicts.
    """
    comparisons: list[dict] = []

    for field_cfg in att_cfg["extract_fields"]:
        ext_key = field_cfg["extract_key"]

        # Handle inverter array comparison
        if field_cfg.get("type") == "inverter_array":
            inv_results = compare_inverters(extracted.get(ext_key), questionnaire_data, field_cfg)
            # Add form_field_id for inverter summary (1016553 = 逆變器匯總)
            for inv_comp in inv_results:
                inv_comp["form_field_id"] = "1016553"
            comparisons.extend(inv_results)
            continue

        # Get extracted value and evidence
        raw = extracted.get(ext_key)
        if isinstance(raw, dict):
            ext_value = raw.get("value")
            evidence = raw.get("evidence", "")
        else:
            ext_value = raw
            evidence = ""

        # Dual voltage values (e.g. "11.4/22.8kV") are kept as-is
        # to let comparison naturally Fail for human judgment
        # (Previously fix_dual_voltage would pick one side, causing incorrect corrections)

        # Strip list elements
        if isinstance(ext_value, list):
            ext_value = [str(v).strip() for v in ext_value if v and str(v).strip()]

        # Get form value (prefer normalized, fallback to raw)
        form_raw = _get_form_value(questionnaire_data, field_cfg)
        form_value = form_normalized.get(ext_key, form_raw)

        if isinstance(form_value, list):
            form_value = [str(v).strip() for v in form_value if v and str(v).strip()]

        # Compare
        comp = compare_values(ext_value, form_value, ext_key)
        comp.update({
            "extract_key": ext_key,
            "form_field_id": field_cfg.get("form_field_id", ""),
            "form_field_name": field_cfg["form_field_name"],
            "evidence": evidence,
        })
        comparisons.append(comp)

    return comparisons


def lambda_handler(event: dict, context: Any) -> dict:
    """AWS Lambda handler for AI document comparison.

    Supports two event formats:
    1. Webhook format (from webhook_handler):
        {"event_type": str, "payload": dict, "case_id": str, "ragic_meta": dict}
    2. Direct invocation format:
        {"case_id": str, "questionnaire_data": dict, "supporting_documents": [...]}

    For webhook format, resolves case context from DREAMS_APPLY_ID, fetches
    documents from RAGIC, then performs AI comparison.

    Returns:
        Dict with statusCode and ComparisonReport in body.
    """
    log_operation(
        logger,
        case_id=event.get("case_id", "unknown"),
        operation_type="ai_determination_start",
        message="AI Determination Lambda started",
    )

    # Detect event format
    event_type_str = event.get("event_type", "")

    if event_type_str:
        # Webhook format — resolve case context first
        return _handle_webhook_event(event)
    else:
        # Direct invocation format (existing logic)
        return _handle_direct_invocation(event)


def _handle_webhook_event(event: dict) -> dict:
    """Handle webhook-format events from webhook_handler.

    Resolves case context from DREAMS_APPLY_ID, fetches questionnaire data
    and documents from RAGIC, then delegates to AI comparison.

    For SUPPLEMENTARY_QUESTIONNAIRE, also checks case status to determine
    if this is 資訊補件 or 台電補件.
    """
    from dreams_workflow.shared.case_resolver import resolve_case_context
    from dreams_workflow.shared.models import WebhookEventType

    event_type_str = event.get("event_type", "")
    payload = event.get("payload", {})
    case_id = event.get("case_id", "unknown")

    log_operation(
        logger,
        case_id=case_id,
        operation_type="ai_determination_webhook",
        message=f"Processing webhook event: {event_type_str}",
    )

    # Resolve case context (ragicId, status, etc.) from DREAMS_APPLY_ID
    case_context = resolve_case_context(payload)

    if not case_context.get("resolved"):
        error_msg = case_context.get("error", "Failed to resolve case context")
        log_operation(
            logger,
            case_id=case_id,
            operation_type="ai_determination_error",
            message=f"Cannot resolve case context: {error_msg}",
            level="error",
        )
        return {
            "statusCode": 400,
            "body": json.dumps({"error": error_msg}),
        }

    resolved_case_id = case_context["ragic_id"]
    case_status = case_context.get("case_status", "")

    # For SUPPLEMENTARY_QUESTIONNAIRE, check if it's 資訊補件 or 台電補件
    if event_type_str == WebhookEventType.SUPPLEMENTARY_QUESTIONNAIRE.value:
        log_operation(
            logger,
            case_id=resolved_case_id,
            operation_type="ai_determination_supplement_check",
            message=f"Supplement questionnaire received, case status='{case_status}'",
        )
        # Both 資訊補件 and 台電補件 trigger AI re-determination
        # The difference is in what happens after (handled by workflow_engine)

    # For NEW_CONTRACT_FULL_QUESTIONNAIRE, verify case status is "待填問卷"
    if event_type_str == WebhookEventType.NEW_CONTRACT_FULL_QUESTIONNAIRE.value:
        from dreams_workflow.shared.models import CaseStatus as _CS
        if case_status and case_status != _CS.PENDING_QUESTIONNAIRE.value:
            log_operation(
                logger,
                case_id=resolved_case_id,
                operation_type="ai_determination_status_mismatch",
                message=f"Status mismatch: expected '待填問卷' but got '{case_status}', skipping AI determination",
                level="warning",
            )
            # Send anomaly notification
            try:
                import boto3 as _boto3
                _lc = _boto3.client("lambda")
                _email_fn = os.environ.get("EMAIL_SERVICE_FUNCTION_NAME", "")
                if _email_fn:
                    _lc.invoke(
                        FunctionName=_email_fn,
                        InvocationType="Event",
                        Payload=json.dumps({
                            "email_type": "異常通知",
                            "case_id": resolved_case_id,
                            "recipient_email": "pringle.huang@gmail.com",
                            "template_data": {
                                "case_id": resolved_case_id,
                                "dreams_apply_id": case_context.get("dreams_apply_id", ""),
                                "anomaly_message": f"問卷回覆觸發但案件狀態不正確：預期「待填問卷」，實際「{case_status}」",
                            },
                        }, ensure_ascii=False).encode("utf-8"),
                    )
            except Exception as notify_err:
                logger.warning(f"Failed to send anomaly notification: {notify_err}")

            return {
                "statusCode": 200,
                "body": json.dumps({
                    "message": "Status mismatch, AI determination skipped",
                    "expected_status": "待填問卷",
                    "actual_status": case_status,
                    "resolved_case_id": resolved_case_id,
                }, ensure_ascii=False),
            }

    # Fetch questionnaire data and documents from RAGIC
    from dreams_workflow.shared.ragic_client import CloudRagicClient

    try:
        # Determine data source based on event type
        is_supplement = (event_type_str == WebhookEventType.SUPPLEMENTARY_QUESTIONNAIRE.value)

        if is_supplement:
            # SUPPLEMENT FLOW:
            # 1. Fetch original data & documents from case management form
            # 2. Overlay supplement payload fields onto original data
            # 3. Use merged data for AI determination
            questionnaire_data, supporting_documents, document_metadata = (
                _prepare_supplement_data(resolved_case_id, payload)
            )
        else:
            # NORMAL FLOW (NEW_CONTRACT_FULL_QUESTIONNAIRE):
            # Use webhook payload directly as questionnaire data
            questionnaire_data = payload

            # Download supporting documents directly from payload attachment fields
            ragic_client = CloudRagicClient()
            try:
                from dreams_workflow.shared.ragic_fields_config import get_document_attachment_fields

                doc_fields = get_document_attachment_fields()
                attachment_field_ids = list(doc_fields.values())

                supporting_documents: list[tuple[str, bytes]] = []
                document_metadata: list[dict] = []
                for field_id in attachment_field_ids:
                    file_value = payload.get(field_id, "")
                    if not file_value or "@" not in str(file_value):
                        continue

                    file_bytes, file_name = ragic_client._download_attachment(str(file_value))
                    if file_bytes is not None:
                        supporting_documents.append((file_name, file_bytes))
                        document_metadata.append({"field_id": field_id})
            finally:
                ragic_client.close()

        log_operation(
            logger,
            case_id=resolved_case_id,
            operation_type="ai_determination_data_fetched",
            message=f"Fetched questionnaire data and {len(supporting_documents)} documents",
        )

        if not supporting_documents:
            log_operation(
                logger,
                case_id=resolved_case_id,
                operation_type="ai_determination_no_docs",
                message="No supporting documents found, skipping AI determination",
                level="warning",
            )
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "message": "No supporting documents found",
                    "resolved_case_id": resolved_case_id,
                }, ensure_ascii=False),
            }

        # Perform AI comparison
        llm_extracted_values: dict[str, dict[str, str]] = {}
        field_comparisons: dict[str, list[dict]] = {}
        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id=resolved_case_id,
            extracted_values_out=llm_extracted_values,
            field_comparisons_out=field_comparisons,
        )

        log_operation(
            logger,
            case_id=resolved_case_id,
            operation_type="ai_determination_complete",
            message=f"AI Determination completed: {report.overall_status}",
        )

        # Write results to RAGIC case management form
        _write_result_and_update_status(
            resolved_case_id, report, questionnaire_data, llm_extracted_values,
            field_comparisons=field_comparisons,
            drop_empty=is_supplement,
        )

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": f"AI determination completed for case {resolved_case_id}",
                "overall_status": report.overall_status,
                "event_type": event_type_str,
                "resolved_case_id": resolved_case_id,
            }, ensure_ascii=False),
        }

    except Exception as e:
        log_operation(
            logger,
            case_id=resolved_case_id,
            operation_type="ai_determination_error",
            message=f"AI determination failed: {e}",
            level="error",
        )
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": str(e),
                "resolved_case_id": resolved_case_id,
            }, ensure_ascii=False),
        }


def _handle_direct_invocation(event: dict) -> dict:
    """Handle direct invocation format (existing logic)."""

    try:
        case_id = event.get("case_id", "")
        questionnaire_data = event.get("questionnaire_data", {})
        raw_documents = event.get("supporting_documents", [])

        # Decode base64 documents
        import base64

        supporting_documents: list[tuple[str, bytes]] = []
        document_metadata: list[dict] = []

        for doc in raw_documents:
            file_name = doc.get("file_name", "")
            content_b64 = doc.get("content_b64", "")
            field_id = doc.get("field_id", "")

            if content_b64:
                file_bytes = base64.b64decode(content_b64)
            else:
                file_bytes = b""

            supporting_documents.append((file_name, file_bytes))
            document_metadata.append({"field_id": field_id})

        # Perform comparison and capture extracted values
        llm_extracted_values: dict[str, dict[str, str]] = {}
        report = compare_documents(
            questionnaire_data=questionnaire_data,
            supporting_documents=supporting_documents,
            document_metadata=document_metadata,
            case_id=case_id,
            extracted_values_out=llm_extracted_values,
        )

        log_operation(
            logger,
            case_id=case_id,
            operation_type="ai_determination_complete",
            message=f"AI Determination completed: {report.overall_status}",
        )

        # Write determination result to RAGIC and update status
        if case_id:
            try:
                _write_result_and_update_status(
                    case_id, report, questionnaire_data, llm_extracted_values
                )
            except Exception as write_err:
                # Write failure should not prevent returning the report
                log_operation(
                    logger,
                    case_id=case_id,
                    operation_type="ai_post_processing_error",
                    message=f"Failed to write results to RAGIC: {write_err}",
                    level="error",
                )

        return {
            "statusCode": 200,
            "body": json.dumps(report.to_dict(), ensure_ascii=False),
        }

    except Exception as e:
        error_case_id = event.get("case_id", "unknown")
        log_operation(
            logger,
            case_id=error_case_id,
            operation_type="ai_determination_error",
            message=f"AI Determination failed: {e}",
            level="error",
        )
        return {
            "statusCode": 500,
            "body": json.dumps(
                {"error": str(e), "case_id": error_case_id}, ensure_ascii=False
            ),
        }


def _write_result_and_update_status(
    case_id: str,
    report: "ComparisonReport",
    questionnaire_data: dict,
    llm_extracted_values: dict[str, dict[str, str]],
    field_comparisons: dict[str, list[dict]] | None = None,
    drop_empty: bool = False,
) -> None:
    """Write all determination results to RAGIC case management form in a single POST.

    After AI determination completes, builds a complete payload containing:
    1. Direct mapping fields (questionnaire values)
    2. LLM extracted values (AI-determined values per document)
    3. Pass/Fail results (per-field determination based on individual field comparisons)
    4. Status update to "待人工確認"

    All data is written in a single RAGIC POST call.

    Args:
        case_id: The RAGIC case record ID.
        report: The ComparisonReport from AI determination.
        questionnaire_data: Original questionnaire form data (field_id → value).
        llm_extracted_values: AI-extracted values per document
            (document_name → {questionnaire_field_id → extracted_value}).
        field_comparisons: Per-field comparison results per document
            (document_name → [comparison_dict, ...]).
        drop_empty: If True, remove empty values from payload before writing.
            Used for supplement re-determination to avoid clearing existing values.

    Requirements: 2.7, 2.8
    """
    from dreams_workflow.ai_determination.field_mapping_loader import (
        build_complete_write_payload,
        get_questionnaire_result_mapping,
    )
    from dreams_workflow.shared.models import CaseStatus
    from dreams_workflow.shared.ragic_client import CloudRagicClient

    # Build per-field Pass/Fail results from the ComparisonReport
    field_results = _build_field_results(report, field_comparisons)

    # Build the complete payload for a single RAGIC POST
    payload = build_complete_write_payload(
        questionnaire_data=questionnaire_data,
        llm_extracted_values=llm_extracted_values,
        field_results=field_results,
        new_status=CaseStatus.PENDING_MANUAL_CONFIRM.value,
    )

    # Also include the full ComparisonReport as JSON for reference
    payload["ai_determination_result"] = json.dumps(
        report.to_dict(), ensure_ascii=False
    )

    # Drop empty values if requested (supplement re-determination mode)
    if drop_empty:
        payload = {k: v for k, v in payload.items() if v}

    ragic_client = CloudRagicClient()
    try:
        # Single POST to write everything at once
        ragic_client.update_case_record(case_id, payload)

        log_operation(
            logger,
            case_id=case_id,
            operation_type="ai_result_written",
            message=(
                f"All determination results written to RAGIC in single POST "
                f"({len(payload)} fields, status=待人工確認)"
            ),
        )
    except Exception as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="ai_post_processing_error",
            message=f"Failed to write results to RAGIC: {e}",
            level="error",
        )
        # Don't re-raise — the determination itself succeeded
    finally:
        ragic_client.close()


def _build_field_results(
    report: "ComparisonReport",
    field_comparisons: dict[str, list[dict]] | None = None,
) -> dict[str, str]:
    """Build per-field Pass/Fail results from individual field comparisons.

    Uses per-field comparison data when available (granular per-field Pass/Fail).
    Falls back to per-document status if field_comparisons is not provided.

    Logic:
    - If LLM returned null (not found) → ignore (doesn't affect result)
    - If extracted value matches form → Pass
    - If extracted value doesn't match → Fail
    - If multiple documents verify same field: Fail wins over Pass
    - If all documents return null → field not included in results

    Args:
        report: The ComparisonReport containing per-document results.
        field_comparisons: Per-field comparison results per document.

    Returns:
        Dict of questionnaire_field_id → "Pass" or "Fail".
    """
    from dreams_workflow.ai_determination.config import ATTACHMENTS_CONFIG

    field_results: dict[str, str] = {}

    if field_comparisons:
        # Track which fields were checked by any document
        fields_checked: set[str] = set()

        # Use granular per-field comparison results
        # Logic:
        # - If extracted value matches form → Pass
        # - If extracted value doesn't match → Fail (overrides Pass)
        # - If all documents return null/not found → Fail (no evidence found)
        for doc_name, comparisons in field_comparisons.items():
            for comp in comparisons:
                form_field_id = comp.get("form_field_id", "")
                if not form_field_id:
                    # Try to get from extract_key → form_field_id mapping
                    extract_key = comp.get("extract_key", "")
                    # Find form_field_id from ATTACHMENTS_CONFIG
                    for cfg in ATTACHMENTS_CONFIG:
                        if cfg["document_name"] == doc_name:
                            for ef in cfg.get("extract_fields", []):
                                if ef.get("extract_key") == extract_key:
                                    form_field_id = ef.get("form_field_id", "")
                                    break
                            break
                if not form_field_id:
                    continue

                fields_checked.add(form_field_id)

                is_match = comp.get("match", False)
                extracted = comp.get("extracted")
                note = comp.get("note", "")

                # Skip if LLM didn't find the field (null/not found/empty)
                if extracted is None or extracted == "" or "未找到" in note or "皆為空" in note or "表單欄位為空" in note:
                    continue

                if is_match:
                    # Match found → Pass (only if not already Fail from another doc)
                    if field_results.get(form_field_id) != "Fail":
                        field_results[form_field_id] = "Pass"
                else:
                    # Mismatch found → Fail (overrides Pass)
                    field_results[form_field_id] = "Fail"

        # Fields that were checked but never got a result → Fail (no evidence found)
        for fid in fields_checked:
            if fid not in field_results:
                field_results[fid] = "Fail"
    else:
        # Fallback: use per-document status (old behavior)
        for doc_result in report.results:
            att_cfg = None
            for cfg in ATTACHMENTS_CONFIG:
                if cfg["document_name"] == doc_result.document_name:
                    att_cfg = cfg
                    break
            if att_cfg is None:
                continue
            for extract_field in att_cfg.get("extract_fields", []):
                form_field_id = extract_field.get("form_field_id", "")
                if form_field_id:
                    field_results[form_field_id] = (
                        "Pass" if doc_result.status == "pass" else "Fail"
                    )

    return field_results


def _prepare_supplement_data(
    case_id: str, supplement_payload: dict
) -> tuple[dict, list[tuple[str, bytes]], list[dict]]:
    """Prepare merged data for supplement questionnaire AI re-determination.

    Flow:
    1. Fetch original case record from case management form (business-process2/2)
    2. Convert supplement payload fields to original questionnaire field IDs
    3. Overlay non-empty supplement values onto original data
    4. Download documents (prefer supplement's new uploads, fallback to original)

    Args:
        case_id: The case management form record ID.
        supplement_payload: The supplement questionnaire webhook payload.

    Returns:
        Tuple of (merged_questionnaire_data, supporting_documents, document_metadata).
    """
    from dreams_workflow.ai_determination.field_mapping_loader import load_field_mapping
    from dreams_workflow.shared.ragic_client import CloudRagicClient
    from dreams_workflow.shared.ragic_fields_config import get_document_attachment_fields

    config = load_field_mapping()
    supplement_to_q_mapping = config.get("supplement_to_questionnaire_mapping", {})
    supplement_to_case_mapping = config.get("supplement_to_case_mapping", {})

    log_operation(
        logger,
        case_id=case_id,
        operation_type="supplement_prepare_start",
        message=f"Preparing supplement data: fetching case record {case_id}",
    )

    # Step 1: Fetch original case record from RAGIC
    ragic_client = CloudRagicClient()
    try:
        original_record = ragic_client.get_case_record(case_id)
    except Exception as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="supplement_fetch_error",
            message=f"Failed to fetch case record: {e}",
            level="error",
        )
        raise

    # Step 2: Build merged questionnaire data
    # Start with original record (case management form field IDs)
    # The AI comparison uses questionnaire field IDs (1014xxx), so we need to
    # reverse-map case management fields back to questionnaire field IDs
    from dreams_workflow.ai_determination.field_mapping_loader import get_direct_mapping
    direct_map = get_direct_mapping()  # questionnaire_field_id → case_field_id
    reverse_direct_map = {v: k for k, v in direct_map.items()}  # case_field_id → questionnaire_field_id

    # Build questionnaire_data from original case record
    merged_data: dict = {}
    for case_field_id, value in original_record.items():
        q_field_id = reverse_direct_map.get(case_field_id)
        if q_field_id and value:
            merged_data[q_field_id] = value
        # Also keep the case field ID version for document download
        merged_data[case_field_id] = value

    # Step 3: Overlay supplement payload values (convert to questionnaire field IDs)
    overlay_count = 0
    for supp_field_id, q_field_id in supplement_to_q_mapping.items():
        supp_value = supplement_payload.get(supp_field_id, "")
        if supp_value:  # Only overlay non-empty values
            merged_data[q_field_id] = supp_value
            overlay_count += 1

    # Also overlay into case management field IDs (for document download)
    for supp_field_id, case_field_id in supplement_to_case_mapping.items():
        supp_value = supplement_payload.get(supp_field_id, "")
        if supp_value:
            merged_data[case_field_id] = supp_value

    log_operation(
        logger,
        case_id=case_id,
        operation_type="supplement_data_merged",
        message=f"Merged supplement data: {overlay_count} fields overlaid from supplement payload",
    )

    # Step 4: Download documents
    # Use document attachment fields from the merged data
    # (supplement's new uploads override original if present)
    doc_fields = get_document_attachment_fields()
    attachment_field_ids = list(doc_fields.values())

    supporting_documents: list[tuple[str, bytes]] = []
    document_metadata: list[dict] = []

    try:
        for field_id in attachment_field_ids:
            file_value = merged_data.get(field_id, "")
            if not file_value or "@" not in str(file_value):
                continue

            file_bytes, file_name = ragic_client._download_attachment(str(file_value))
            if file_bytes is not None:
                supporting_documents.append((file_name, file_bytes))
                document_metadata.append({"field_id": field_id})
    finally:
        ragic_client.close()

    log_operation(
        logger,
        case_id=case_id,
        operation_type="supplement_docs_downloaded",
        message=f"Downloaded {len(supporting_documents)} documents for re-determination",
    )

    return merged_data, supporting_documents, document_metadata
