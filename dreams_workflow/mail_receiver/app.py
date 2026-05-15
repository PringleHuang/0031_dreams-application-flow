"""Mail Receiver Lambda Function.

Processes incoming emails received by AWS SES. When Taipower replies to
a review request, SES stores the raw email in S3 and triggers this Lambda.
The handler parses the email, matches it to a case, triggers AI semantic
analysis, and updates the case status based on the result.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5
"""

from __future__ import annotations

import email
import json
import os
import re
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from typing import Any

import boto3

from dreams_workflow.shared.logger import get_logger, log_operation
from dreams_workflow.shared.models import CaseStatus

logger = get_logger(__name__)

# Environment variables
S3_BUCKET = os.environ.get("EMAIL_STORAGE_BUCKET", os.environ.get("SES_EMAIL_BUCKET", ""))
AI_DETERMINATION_FUNCTION = os.environ.get("AI_DETERMINATION_FUNCTION_NAME", "")

# Lazy-initialized clients
_s3_client = None
_lambda_client = None


def _get_s3_client():
    """Get or create the boto3 S3 client."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _get_lambda_client():
    """Get or create the boto3 Lambda client."""
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


@dataclass
class ParsedEmail:
    """Parsed email content."""

    subject: str
    sender: str
    body_text: str
    body_html: str
    attachments: list[tuple[str, bytes]]  # (filename, content)
    message_id: str
    date: str


def lambda_handler(event: dict, context: Any) -> dict:
    """Mail Receiver Lambda entry point.

    Triggered by SES Receipt Rule → S3 → Lambda.

    Event structure (SES notification via S3):
        {
            "Records": [{
                "ses": {
                    "mail": {
                        "messageId": "...",
                        "source": "sender@example.com",
                        "commonHeaders": {"subject": "..."}
                    },
                    "receipt": {...}
                }
            }]
        }

    Or direct S3 event:
        {
            "Records": [{
                "s3": {
                    "bucket": {"name": "..."},
                    "object": {"key": "..."}
                }
            }]
        }

    Args:
        event: SES/S3 event.
        context: Lambda execution context.

    Returns:
        Dict with statusCode and processing result.
    """
    log_operation(
        logger,
        case_id="N/A",
        operation_type="mail_receiver_start",
        message="Mail receiver Lambda triggered",
    )

    try:
        # Extract S3 object info from event
        bucket, key = _extract_s3_info(event)

        if not bucket or not key:
            logger.error(
                "Could not extract S3 bucket/key from event",
                extra={"case_id": "N/A", "operation_type": "mail_receiver_error"},
            )
            return {"statusCode": 400, "body": json.dumps({"error": "Invalid event"})}

        # Read raw email from S3
        raw_email = _read_email_from_s3(bucket, key)

        if not raw_email:
            return {"statusCode": 404, "body": json.dumps({"error": "Email not found in S3"})}

        # Parse email content
        parsed = parse_email_content(raw_email)

        log_operation(
            logger,
            case_id="N/A",
            operation_type="email_parsed",
            message=f"Email parsed: from={parsed.sender}, subject={parsed.subject}",
        )

        # Match sender to a case
        case_id = match_case_by_sender(parsed.sender, parsed.subject)

        if not case_id:
            log_operation(
                logger,
                case_id="N/A",
                operation_type="case_match_failed",
                message=f"Could not match email to a case: sender={parsed.sender}, subject={parsed.subject}",
                level="warning",
            )
            return {
                "statusCode": 200,
                "body": json.dumps({"message": "No matching case found", "sender": parsed.sender}),
            }

        log_operation(
            logger,
            case_id=case_id,
            operation_type="case_matched",
            message=f"Email matched to case {case_id}",
        )

        # Verify case status is "台電審核" before processing
        try:
            from dreams_workflow.shared.ragic_client import CloudRagicClient
            from dreams_workflow.shared.ragic_fields_config import get_case_management_fields

            cm_fields = get_case_management_fields()
            status_field = cm_fields.get("case_status", "1015456")

            ragic_client = CloudRagicClient()
            try:
                record = ragic_client.get_case_record(case_id)
                current_status = record.get(status_field, "")
            finally:
                ragic_client.close()

            if current_status != "台電審核":
                log_operation(
                    logger,
                    case_id=case_id,
                    operation_type="mail_status_mismatch",
                    message=f"Expected status '台電審核' but got '{current_status}', sending anomaly notification",
                    level="warning",
                )
                # Send anomaly notification
                email_fn = os.environ.get("EMAIL_SERVICE_FUNCTION_NAME", "")
                if email_fn:
                    _get_lambda_client().invoke(
                        FunctionName=email_fn,
                        InvocationType="Event",
                        Payload=json.dumps({
                            "email_type": "異常通知",
                            "case_id": case_id,
                            "recipient_email": "pringle.huang@gmail.com",
                            "template_data": {
                                "case_id": case_id,
                                "dreams_apply_id": "",
                                "anomaly_message": f"收到台電回信但案件狀態不正確：預期「台電審核」，實際「{current_status}」",
                            },
                        }, ensure_ascii=False).encode("utf-8"),
                    )
                return {
                    "statusCode": 200,
                    "body": json.dumps({
                        "case_id": case_id,
                        "action": "status_mismatch",
                        "expected": "台電審核",
                        "actual": current_status,
                    }),
                }
        except Exception as e:
            log_operation(
                logger,
                case_id=case_id,
                operation_type="status_check_error",
                message=f"Failed to verify case status: {e}, proceeding anyway",
                level="warning",
            )

        # Classify email type by keywords
        email_content = parsed.body_text or parsed.body_html or ""
        email_type = _classify_email(email_content)

        log_operation(
            logger,
            case_id=case_id,
            operation_type="email_classified",
            message=f"Email classified as: {email_type}",
        )

        if email_type == "electricity_number_created":
            # Electricity number created → re-trigger CreatePlantApplication
            return _handle_electricity_number_created(case_id, parsed)
        elif email_type == "approved":
            # Case approved → update status to 發送前人工確認
            return _handle_case_approved(case_id, parsed)
        elif email_type == "rejected":
            # Case rejected → use LLM to extract rejection reason, then update RAGIC
            return _handle_case_rejected(case_id, parsed, email_content)
        else:
            # Unknown type
            log_operation(
                logger,
                case_id=case_id,
                operation_type="email_unknown_type",
                message=f"Could not classify email, skipping. Content preview: {email_content[:100]}",
                level="warning",
            )
            return {
                "statusCode": 200,
                "body": json.dumps({"case_id": case_id, "action": "unknown_email_type"}),
            }

    except Exception as e:
        logger.error(
            f"Mail receiver error: {e}",
            extra={"case_id": "N/A", "operation_type": "mail_receiver_error"},
            exc_info=True,
        )
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def parse_email_content(raw_email: bytes) -> ParsedEmail:
    """Parse raw email bytes into structured content.

    Extracts subject, sender, body (text and HTML), and attachments.
    Handles multiple character encodings (UTF-8, Big5, ISO-8859-1, etc.).

    Args:
        raw_email: Raw email bytes (RFC 822 format).

    Returns:
        ParsedEmail with extracted fields.
    """
    msg = email.message_from_bytes(raw_email, policy=policy.default)

    subject = msg.get("Subject", "")
    sender = msg.get("From", "")
    message_id = msg.get("Message-ID", "")
    date = msg.get("Date", "")

    body_text = ""
    body_html = ""
    attachments: list[tuple[str, bytes]] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            if "attachment" in content_disposition:
                filename = part.get_filename() or "attachment"
                content = part.get_payload(decode=True) or b""
                attachments.append((filename, content))
            elif content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body_text = _decode_payload(payload, charset)
            elif content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body_html = _decode_payload(payload, charset)
    else:
        content_type = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            if content_type == "text/html":
                body_html = _decode_payload(payload, charset)
            else:
                body_text = _decode_payload(payload, charset)

    return ParsedEmail(
        subject=subject,
        sender=sender,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
        message_id=message_id,
        date=date,
    )


def _decode_payload(payload: bytes, charset: str) -> str:
    """Decode email payload bytes with charset detection fallback.

    Tries the declared charset first, then common Chinese encodings,
    then UTF-8 with replacement.

    Args:
        payload: Raw bytes to decode.
        charset: Declared charset from email headers.

    Returns:
        Decoded string.
    """
    # Normalize charset name
    charset = (charset or "utf-8").lower().strip()
    charset_aliases = {
        "big5": "cp950",  # cp950 is a superset of big5
        "x-big5": "cp950",
    }
    charset = charset_aliases.get(charset, charset)

    # Try declared charset first
    try:
        return payload.decode(charset)
    except (UnicodeDecodeError, LookupError):
        pass

    # Try common encodings (prioritize Chinese encodings)
    for enc in ["utf-8", "cp950", "big5", "gb2312", "gbk", "gb18030", "iso-8859-1"]:
        try:
            decoded = payload.decode(enc)
            # Verify it looks like valid text (has some CJK characters or ASCII)
            if any('\u4e00' <= c <= '\u9fff' for c in decoded[:100]) or decoded.isascii():
                return decoded
        except (UnicodeDecodeError, LookupError):
            continue

    # Last resort: UTF-8 with replacement
    return payload.decode("utf-8", errors="replace")


def match_case_by_sender(sender_email: str, subject: str) -> str | None:
    """Match an incoming email to a case by sender whitelist and subject parsing.

    Matching logic:
    1. Verify sender is in the allowed_senders whitelist (configurable)
    2. Parse DREAMS_APPLY_ID from subject (e.g. "Re: 【DREAMS審核】_TEST0011-26/...")
    3. Split DREAMS_APPLY_ID by "-" to get ragicId

    Both conditions must be met for a match.

    Args:
        sender_email: Email sender address (may be "Name <email>" format).
        subject: Email subject line.

    Returns:
        ragicId string if matched, None otherwise.
    """
    # Load config
    config = _get_mail_config()
    allowed_senders = config.get("allowed_senders", [])
    subject_patterns = config.get("subject_patterns", [])

    # Extract clean email address from "Name <email>" format
    email_match = re.search(r"<([^>]+)>", sender_email)
    clean_email = email_match.group(1).lower() if email_match else sender_email.strip().lower()

    # Step 1: Verify sender is in whitelist
    allowed_lower = [s.lower() for s in allowed_senders]
    if clean_email not in allowed_lower:
        log_operation(
            logger,
            case_id="N/A",
            operation_type="sender_not_allowed",
            message=f"Sender '{clean_email}' not in allowed_senders whitelist",
            level="warning",
        )
        return None

    # Step 2: Parse DREAMS_APPLY_ID from subject
    # Strip common reply/forward prefixes
    clean_subject = re.sub(r"^(Re|Fwd|FW|回覆|轉寄)[：:]\s*", "", subject, flags=re.IGNORECASE).strip()

    dreams_apply_id = None
    for pattern in subject_patterns:
        match = re.search(pattern, clean_subject)
        if match:
            dreams_apply_id = match.group(1)
            break

    if not dreams_apply_id:
        log_operation(
            logger,
            case_id="N/A",
            operation_type="subject_parse_failed",
            message=f"Could not extract DREAMS_APPLY_ID from subject: '{subject}'",
            level="warning",
        )
        return None

    # Step 3: Extract ragicId from DREAMS_APPLY_ID (split by "-", take last segment)
    parts = dreams_apply_id.split("-")
    if len(parts) >= 2:
        ragic_id = parts[-1]
    else:
        ragic_id = dreams_apply_id

    log_operation(
        logger,
        case_id=ragic_id,
        operation_type="case_matched",
        message=f"Email matched: sender={clean_email}, dreams_apply_id={dreams_apply_id}, ragicId={ragic_id}",
    )

    return ragic_id


# Mail config singleton
_mail_config: dict | None = None


def _get_mail_config() -> dict:
    """Load mail_config.yaml (cached after first load)."""
    global _mail_config
    if _mail_config is None:
        from pathlib import Path
        config_path = Path(__file__).parent / "mail_config.yaml"
        with open(config_path, "r", encoding="utf-8") as f:
            import yaml
            _mail_config = yaml.safe_load(f)
    return _mail_config


def _extract_s3_info(event: dict) -> tuple[str, str]:
    """Extract S3 bucket and key from the Lambda event.

    Handles both SES notification format and direct S3 event format.

    Args:
        event: Lambda event dict.

    Returns:
        Tuple of (bucket_name, object_key).
    """
    records = event.get("Records", [])
    if not records:
        return "", ""

    record = records[0]

    # SES notification format
    if "ses" in record:
        message_id = record["ses"]["mail"]["messageId"]
        bucket = S3_BUCKET
        key = f"incoming/{message_id}"
        return bucket, key

    # Direct S3 event format
    if "s3" in record:
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        return bucket, key

    return "", ""


def _read_email_from_s3(bucket: str, key: str) -> bytes | None:
    """Read raw email content from S3.

    Args:
        bucket: S3 bucket name.
        key: S3 object key.

    Returns:
        Raw email bytes, or None if not found.
    """
    try:
        s3 = _get_s3_client()
        response = s3.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
    except Exception as e:
        logger.error(
            f"Failed to read email from S3: s3://{bucket}/{key}: {e}",
            extra={"case_id": "N/A", "operation_type": "s3_read_error"},
        )
        return None


def _trigger_semantic_analysis(case_id: str, parsed: ParsedEmail) -> dict:
    """Trigger AI semantic analysis on the parsed email.

    Invokes the ai_determination Lambda synchronously to get the analysis result.

    Args:
        case_id: The matched case ID.
        parsed: Parsed email content.

    Returns:
        Analysis result dict from ai_determination.
    """
    if not AI_DETERMINATION_FUNCTION:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="ai_invoke_skip",
            message="AI_DETERMINATION_FUNCTION_NAME not configured",
            level="warning",
        )
        return {}

    # Use the text body for analysis, fall back to HTML
    email_content = parsed.body_text or parsed.body_html

    invoke_payload = {
        "analysis_type": "semantic_analysis",
        "case_id": case_id,
        "email_content": email_content,
        "email_subject": parsed.subject,
    }

    try:
        response = _get_lambda_client().invoke(
            FunctionName=AI_DETERMINATION_FUNCTION,
            InvocationType="RequestResponse",  # Synchronous
            Payload=json.dumps(invoke_payload, ensure_ascii=False).encode("utf-8"),
        )

        response_payload = json.loads(response["Payload"].read().decode("utf-8"))

        if response_payload.get("statusCode") == 200:
            body = response_payload.get("body", "{}")
            if isinstance(body, str):
                return json.loads(body)
            return body

        log_operation(
            logger,
            case_id=case_id,
            operation_type="ai_analysis_error",
            message=f"AI analysis returned error: {response_payload}",
            level="error",
        )
        return {}

    except Exception as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="ai_invoke_error",
            message=f"Failed to invoke AI analysis: {e}",
            level="error",
        )
        return {}


def _process_analysis_result(case_id: str, analysis_result: dict) -> None:
    """Process the semantic analysis result and update case status.

    - Approved: Update status to 發送前人工確認, write Pass/Fail results
    - Rejected: Write rejection reason + Pass/Fail results, update status to 發送前人工確認

    Both cases go to 發送前人工確認 for manual review before proceeding.

    Args:
        case_id: The case ID.
        analysis_result: Result from AI semantic analysis.
    """
    if not analysis_result:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="analysis_result_empty",
            message="No analysis result to process",
            level="warning",
        )
        return

    from dreams_workflow.ai_determination.field_mapping_loader import (
        get_status_field_id,
        get_taipower_result_mapping,
    )
    from dreams_workflow.shared.ragic_client import CloudRagicClient

    category = analysis_result.get("category", "")
    field_results = analysis_result.get("field_results", {})
    rejection_reason = analysis_result.get("rejection_reason_summary", "")

    # Build the update payload for RAGIC
    update_data: dict[str, Any] = {}

    # Write per-field Pass/Fail to taipower result fields
    taipower_mapping = get_taipower_result_mapping()
    for q_field_id, result_field_id in taipower_mapping.items():
        # Default to "Pass" if not in field_results
        value = field_results.get(q_field_id, field_results.get(result_field_id, "Pass"))
        update_data[result_field_id] = value

    # Write rejection reason if rejected
    if category == "rejected" and rejection_reason:
        update_data["taipower_rejection_reason"] = rejection_reason

    # Update status to 發送前人工確認 (both approved and rejected go here)
    status_field = get_status_field_id()
    update_data[status_field] = CaseStatus.PRE_SEND_CONFIRM.value

    # Write to RAGIC
    ragic_client = CloudRagicClient()
    try:
        ragic_client.update_case_record(case_id, update_data)
        log_operation(
            logger,
            case_id=case_id,
            operation_type="taipower_result_written",
            message=f"Taipower analysis result written: category={category}, status→發送前人工確認",
        )
    except Exception as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="taipower_result_write_error",
            message=f"Failed to write taipower result: {e}",
            level="error",
        )
    finally:
        ragic_client.close()


def _classify_email(content: str) -> str:
    """Classify email type by keyword matching from config.

    Checks email content against configured keywords for each type.
    Returns the first matching type.

    Args:
        content: Email body text.

    Returns:
        One of: "electricity_number_created", "approved", "rejected", "unknown"
    """
    config = _get_mail_config()
    classification = config.get("email_classification", {})

    # Check each type in order
    for email_type, type_config in classification.items():
        keywords = type_config.get("keywords", [])
        for keyword in keywords:
            if keyword in content:
                return email_type

    return "unknown"


def _handle_electricity_number_created(case_id: str, parsed: "ParsedEmail") -> dict:
    """Handle 'electricity number created' email.

    Re-triggers the Taipower review flow (CreatePlantApplication).
    Changes case status back to trigger the API call.

    Args:
        case_id: The RAGIC case record ID.
        parsed: Parsed email content.

    Returns:
        Result dict.
    """
    log_operation(
        logger,
        case_id=case_id,
        operation_type="electricity_number_created",
        message="Electricity number created by Taipower, re-triggering CreatePlantApplication",
    )

    # Invoke workflow_engine to re-trigger taipower review
    # The case should already be in "台電審核" status
    try:
        from dreams_workflow.shared.models import WebhookEventType

        invoke_payload = {
            "event_type": WebhookEventType.CASE_STATUS_CHANGED.value,
            "payload": {},  # Will be fetched from RAGIC by workflow_engine
            "case_id": case_id,
            "ragic_meta": {"trigger": "mail_receiver_electricity_created"},
        }

        # Get case data from RAGIC to pass as payload
        from dreams_workflow.shared.ragic_client import CloudRagicClient
        ragic_client = CloudRagicClient()
        try:
            record = ragic_client.get_case_record(case_id)
            invoke_payload["payload"] = record
            # Ensure status field is set to 台電審核
            from dreams_workflow.shared.ragic_fields_config import get_case_management_fields
            cm_fields = get_case_management_fields()
            status_field = cm_fields.get("case_status", "1015456")
            invoke_payload["payload"][status_field] = "台電審核"
        finally:
            ragic_client.close()

        # Invoke workflow_engine
        workflow_fn = os.environ.get("WORKFLOW_ENGINE_FUNCTION_NAME", "")
        if workflow_fn:
            _get_lambda_client().invoke(
                FunctionName=workflow_fn,
                InvocationType="Event",
                Payload=json.dumps(invoke_payload, ensure_ascii=False).encode("utf-8"),
            )
            log_operation(
                logger,
                case_id=case_id,
                operation_type="workflow_engine_invoked",
                message="Workflow engine invoked to re-trigger CreatePlantApplication",
            )
        else:
            log_operation(
                logger,
                case_id=case_id,
                operation_type="workflow_invoke_skip",
                message="WORKFLOW_ENGINE_FUNCTION_NAME not configured",
                level="warning",
            )

    except Exception as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="electricity_created_error",
            message=f"Failed to re-trigger taipower review: {e}",
            level="error",
        )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "case_id": case_id,
            "action": "electricity_number_created_handled",
        }),
    }


def _handle_case_approved(case_id: str, parsed: "ParsedEmail") -> dict:
    """Handle 'case approved' email.

    Updates case status to '發送前人工確認' in RAGIC.

    Args:
        case_id: The RAGIC case record ID.
        parsed: Parsed email content.

    Returns:
        Result dict.
    """
    log_operation(
        logger,
        case_id=case_id,
        operation_type="case_approved",
        message="Taipower approved the case",
    )

    # Update RAGIC status to 發送前人工確認
    try:
        from dreams_workflow.shared.ragic_client import CloudRagicClient
        from dreams_workflow.shared.ragic_fields_config import get_case_management_fields

        cm_fields = get_case_management_fields()
        status_field = cm_fields.get("case_status", "1015456")

        ragic_client = CloudRagicClient()
        try:
            ragic_client.update_case_record(case_id, {
                status_field: "發送前人工確認",
            })
        finally:
            ragic_client.close()

        log_operation(
            logger,
            case_id=case_id,
            operation_type="taipower_result_written",
            message="Taipower approved: status → 發送前人工確認",
        )
    except Exception as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="case_approved_error",
            message=f"Failed to update status: {e}",
            level="error",
        )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "case_id": case_id,
            "action": "case_approved",
            "new_status": "發送前人工確認",
        }),
    }


def _handle_case_rejected(case_id: str, parsed: "ParsedEmail", email_content: str) -> dict:
    """Handle 'case rejected' email.

    Uses LLM (Bedrock) to analyze rejection reason and determine which fields
    failed the Taipower review. Writes per-field Pass/Fail results to the
    taipower_result fields in RAGIC, then updates status to '發送前人工確認'.

    Taipower result fields (1016560~1016570, 1016700):
    - Default: "Pass" (Taipower doesn't mention = no issue)
    - If LLM identifies an issue with a field: "Fail"

    Args:
        case_id: The RAGIC case record ID.
        parsed: Parsed email content.
        email_content: Raw email body text for LLM analysis.

    Returns:
        Result dict.
    """
    log_operation(
        logger,
        case_id=case_id,
        operation_type="case_rejected",
        message="Taipower rejected the case, analyzing rejection with LLM",
    )

    # Use Bedrock to analyze which fields failed
    field_results, rejection_summary = _analyze_rejection_fields(case_id, email_content)

    # If LLM analysis failed (empty field_results), send anomaly notification and don't write to RAGIC
    if not field_results:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="case_rejected_llm_failed",
            message=f"LLM analysis failed, not writing to RAGIC. Summary: {rejection_summary[:200]}",
            level="error",
        )
        # Send anomaly notification
        email_fn = os.environ.get("EMAIL_SERVICE_FUNCTION_NAME", "")
        if email_fn:
            try:
                _get_lambda_client().invoke(
                    FunctionName=email_fn,
                    InvocationType="Event",
                    Payload=json.dumps({
                        "email_type": "異常通知",
                        "case_id": case_id,
                        "recipient_email": "pringle.huang@gmail.com",
                        "template_data": {
                            "case_id": case_id,
                            "dreams_apply_id": "",
                            "anomaly_message": f"台電駁回信 LLM 分析失敗，請手動處理。原始內容：{email_content[:300]}",
                        },
                    }, ensure_ascii=False).encode("utf-8"),
                )
            except Exception:
                pass
        return {
            "statusCode": 200,
            "body": json.dumps({
                "case_id": case_id,
                "action": "case_rejected_llm_failed",
                "rejection_summary": rejection_summary,
            }, ensure_ascii=False),
        }

    # Build RAGIC update payload
    # Taipower result field IDs — expand group results to individual fields
    taipower_result_fields = {
        "site_address": ["1016560"],
        "electricity_number": ["1016561"],
        "capacity_kw": ["1016562"],
        "site_type": ["1016563"],
        "approval_number": ["1016564"],
        "selling_method": ["1016565"],
        "parallel_group": ["1016566", "1016567", "1016568"],  # 併聯方式+型式+電壓
        "demarcation_group": ["1016569", "1016570"],           # 責任分界點型式+電壓
        "inverter_summary": ["1016700"],
    }

    # Default all to "Pass", then set Fail for identified issues
    update_data = {}
    for field_key, field_ids in taipower_result_fields.items():
        result_value = "Fail" if field_results.get(field_key) == "Fail" else "Pass"
        for field_id in field_ids:
            update_data[field_id] = result_value

    # Add status update + rejection comment
    from dreams_workflow.shared.ragic_fields_config import get_case_management_fields
    cm_fields = get_case_management_fields()
    status_field = cm_fields.get("case_status", "1015456")
    update_data[status_field] = "發送前人工確認"

    # Write to RAGIC
    try:
        from dreams_workflow.shared.ragic_client import CloudRagicClient

        ragic_client = CloudRagicClient()
        try:
            ragic_client.update_case_record(case_id, update_data)
        finally:
            ragic_client.close()

        fail_count = sum(1 for v in update_data.values() if v == "Fail")
        log_operation(
            logger,
            case_id=case_id,
            operation_type="taipower_result_written",
            message=f"Taipower rejected: {fail_count} fields Fail, reason='{rejection_summary[:100]}', status → 發送前人工確認",
        )
    except Exception as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="case_rejected_error",
            message=f"Failed to write rejection result: {e}",
            level="error",
        )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "case_id": case_id,
            "action": "case_rejected",
            "rejection_summary": rejection_summary,
            "fail_fields": [k for k, v in field_results.items() if v == "Fail"],
            "new_status": "發送前人工確認",
        }, ensure_ascii=False),
    }


def _analyze_rejection_fields(case_id: str, email_content: str) -> tuple[dict[str, str], str]:
    """Analyze rejection email to determine which fields failed.

    Uses Bedrock LLM to identify which of the 12 verification fields
    are mentioned as problematic in the rejection email.

    Args:
        case_id: Case ID for logging.
        email_content: Full email body text.

    Returns:
        Tuple of (field_results dict, rejection_summary string).
        field_results: {"field_key": "Pass" or "Fail"}
        rejection_summary: Human-readable summary of rejection reason.
    """
    import boto3 as _boto3

    bedrock_region = os.environ.get("BEDROCK_REGION", "ap-northeast-1")
    model_id = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-6")

    prompt = f"""你是台電 DREAMS 系統的郵件分析助手。以下是台電回覆的審核駁回郵件。

請分析郵件內容，判斷以下項目中，哪些被台電指出有問題需要修正。
台電沒有提到的項目視為通過（Pass）。

判定項目：
1. site_address（案場詳細地址）
2. electricity_number（電號）
3. capacity_kw（裝置量 kW）
4. site_type（案場類型）
5. approval_number（縣府同意備案函文編號）
6. selling_method（售電方式）
7. parallel_group（併聯相關：併聯方式、併聯點型式、併聯點電壓，三者為一組）
8. demarcation_group（責任分界點相關：責任分界點型式、責任分界點電壓，兩者為一組）
9. inverter_summary（逆變器匯總）

請以 JSON 格式回覆：
{{
  "field_results": {{
    "site_address": "Pass 或 Fail",
    "electricity_number": "Pass 或 Fail",
    "capacity_kw": "Pass 或 Fail",
    "site_type": "Pass 或 Fail",
    "approval_number": "Pass 或 Fail",
    "selling_method": "Pass 或 Fail",
    "parallel_group": "Pass 或 Fail",
    "demarcation_group": "Pass 或 Fail",
    "inverter_summary": "Pass 或 Fail"
  }},
  "rejection_summary": "駁回原因摘要（一句話）"
}}

郵件內容：
---
{email_content}
---

JSON 回覆："""

    try:
        client = _boto3.client("bedrock-runtime", region_name=bedrock_region)
        response = client.invoke_model(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )

        result = json.loads(response["body"].read())
        text = result.get("content", [{}])[0].get("text", "").strip()

        # Parse JSON from LLM response
        # Handle potential markdown code block wrapping
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]

        parsed_result = json.loads(text)
        field_results = parsed_result.get("field_results", {})
        rejection_summary = parsed_result.get("rejection_summary", "")

        log_operation(
            logger,
            case_id=case_id,
            operation_type="rejection_analysis_complete",
            message=f"LLM analysis: {sum(1 for v in field_results.values() if v == 'Fail')} fields Fail, summary='{rejection_summary[:100]}'",
        )

        return field_results, rejection_summary

    except Exception as e:
        log_operation(
            logger,
            case_id=case_id,
            operation_type="llm_analysis_error",
            message=f"Failed to analyze rejection via LLM: {e}",
            level="error",
        )
        # Fallback: mark all as Pass, return raw content as summary
        return {}, f"LLM 分析失敗，請查看原始郵件：{email_content[:200]}"
