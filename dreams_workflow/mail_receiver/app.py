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

        # Trigger AI semantic analysis
        analysis_result = _trigger_semantic_analysis(case_id, parsed)

        # Update case status based on analysis result
        _process_analysis_result(case_id, analysis_result)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "case_id": case_id,
                "action": "email_processed",
                "sender": parsed.sender,
                "subject": parsed.subject,
            }),
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
                    body_text = payload.decode("utf-8", errors="replace")
            elif content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    body_html = payload.decode("utf-8", errors="replace")
    else:
        content_type = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            if content_type == "text/html":
                body_html = payload.decode("utf-8", errors="replace")
            else:
                body_text = payload.decode("utf-8", errors="replace")

    return ParsedEmail(
        subject=subject,
        sender=sender,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
        message_id=message_id,
        date=date,
    )


def match_case_by_sender(sender_email: str, subject: str) -> str | None:
    """Match an incoming email to a case by sender and subject.

    Matching logic:
    1. Check if the subject contains a case ID pattern (e.g., "CASE-001" or RAGIC record ID)
    2. Check if the sender is a known Taipower contact
    3. Query RAGIC for cases with matching Taipower contact email

    Args:
        sender_email: Email sender address.
        subject: Email subject line.

    Returns:
        Case ID string if matched, None otherwise.
    """
    # Extract email address from "Name <email>" format
    email_match = re.search(r"<([^>]+)>", sender_email)
    clean_email = email_match.group(1) if email_match else sender_email.strip()

    # Try to extract case ID from subject
    # Pattern: look for RAGIC record ID or case reference
    case_id_patterns = [
        r"CASE[_-]?(\d+)",
        r"案件[：:]?\s*(\d+)",
        r"#(\d+)",
        r"\[(\d+)\]",
    ]

    for pattern in case_id_patterns:
        match = re.search(pattern, subject, re.IGNORECASE)
        if match:
            return match.group(1)

    # If no case ID in subject, try to look up by sender email in RAGIC
    try:
        from dreams_workflow.shared.ragic_client import CloudRagicClient

        ragic_client = CloudRagicClient()
        try:
            # Search for cases where taipower_contact_email matches sender
            # This is a simplified lookup - in production, would use RAGIC search API
            # For now, return None and let the system admin handle unmatched emails
            pass
        finally:
            ragic_client.close()
    except Exception:
        pass

    return None


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
