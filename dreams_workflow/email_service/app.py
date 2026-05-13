"""Email Service Lambda Function.

Provides unified email sending via AWS SES with template rendering,
attachment support, dynamic link generation, and send logging.

Requirements: 12.1, 12.2, 12.4
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import boto3
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from dreams_workflow.shared.exceptions import EmailSendError
from dreams_workflow.shared.logger import get_logger, log_operation
from dreams_workflow.shared.models import EmailType
from dreams_workflow.shared.retry_config import retry_ses

logger = get_logger(__name__)

# Lazy-initialized AWS clients
_ses_client = None
_s3_client = None


def _get_ses_client():
    """Get or create the boto3 SES client."""
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client("ses", region_name=os.environ.get("AWS_REGION", "ap-northeast-1"))
    return _ses_client


def _get_s3_client():
    """Get or create the boto3 S3 client."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


# =============================================================================
# Data Models
# =============================================================================


@dataclass
class Attachment:
    """Email attachment."""

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass
class EmailRequest:
    """Email send request."""

    email_type: EmailType
    case_id: str
    recipient_email: str
    template_data: dict
    attachments: list[Attachment] | None = None
    cc_emails: list[str] | None = None


@dataclass
class EmailResult:
    """Email send result."""

    success: bool
    message_id: str | None = None
    error_message: str | None = None
    sent_at: str | None = None


@dataclass
class EmailLog:
    """Email send log record (stored in S3)."""

    log_id: str
    case_id: str
    email_type: str
    recipient: str
    sent_at: str | None
    status: str  # "sent", "failed", "retrying"
    message_id: str | None
    retry_count: int = 0
    error_message: str | None = None


# =============================================================================
# Configuration
# =============================================================================


class EmailConfig:
    """Loads and provides access to email template configuration."""

    def __init__(self, config_path: str | None = None):
        if config_path is None:
            config_path = str(Path(__file__).parent / "email_config.yaml")
        self._config = self._load_config(config_path)
        self._templates_dir = str(Path(__file__).parent / "templates")
        self._jinja_env = Environment(
            loader=FileSystemLoader(self._templates_dir),
            autoescape=select_autoescape(["html"]),
        )

    @staticmethod
    def _load_config(config_path: str) -> dict:
        """Load YAML configuration file."""
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    @property
    def sender_name(self) -> str:
        return self._config.get("sender", {}).get("name", "DREAMS")

    @property
    def sender_email(self) -> str:
        raw = self._config.get("sender", {}).get("email", "")
        # Resolve environment variable references like ${SES_SENDER_EMAIL}
        if raw.startswith("${") and raw.endswith("}"):
            env_var = raw[2:-1]
            return os.environ.get(env_var, "")
        return raw

    def get_template_config(self, email_type: EmailType) -> dict | None:
        """Get template configuration for a given email type."""
        type_key = self._email_type_to_config_key(email_type)
        return self._config.get("templates", {}).get(type_key)

    def render_template(self, template_file: str, template_data: dict) -> str:
        """Render an HTML template with the given data."""
        template = self._jinja_env.get_template(template_file)
        return template.render(**template_data)

    def build_link_url(self, link_config: dict, template_data: dict) -> str:
        """Build a complete URL from link configuration and template data.

        Combines base_url + static_params + dynamic_params (resolved from
        template_data) into a full URL with query string.

        Args:
            link_config: Link configuration dict with base_url, static_params,
                         and dynamic_params.
            template_data: Data dict to resolve dynamic parameter values from.

        Returns:
            Complete URL string with query parameters.
        """
        base_url = link_config.get("base_url", "")
        params: dict[str, str] = {}

        # Add static params
        static_params = link_config.get("static_params") or {}
        params.update(static_params)

        # Add dynamic params (resolve values from template_data)
        dynamic_params = link_config.get("dynamic_params") or {}
        for url_param_name, data_key in dynamic_params.items():
            value = template_data.get(data_key, "")
            if value:
                params[url_param_name] = str(value)

        if params:
            return f"{base_url}?{urlencode(params)}"
        return base_url

    def render_subject(self, subject_template: str, template_data: dict) -> str:
        """Render email subject with template data substitution."""
        try:
            result = subject_template.format(**template_data)
            return result
        except (KeyError, IndexError) as e:
            import logging
            logging.getLogger(__name__).warning(
                f"Subject render failed: {e}, template={subject_template}, keys={list(template_data.keys())}"
            )
            return subject_template

    @property
    def recipient_field_id(self) -> str:
        """Get the RAGIC field ID for recipient email in webhook payload."""
        return self._config.get("recipient_field_id", "1000005")

    def get_payload_field_ids(self) -> dict[str, str]:
        """Get configurable payload field IDs.

        Returns a mapping of logical name → RAGIC field ID, allowing
        the system to adapt when RAGIC form design changes.

        Returns:
            Dict like {"dreams_apply_id": "1016557", "customer_email": "1000005", ...}
        """
        return self._config.get("payload_field_ids", {})

    def get_cc_list(self, case_id: str) -> list[str]:
        """Build the CC recipient list for a given case.

        Combines:
        1. Static CC list from config
        2. RAGIC mail loop address (dynamic, based on case record ID)

        Args:
            case_id: The RAGIC record ID (used for mail loop address).

        Returns:
            List of CC email addresses.
        """
        cc_config = self._config.get("cc", {})
        cc_list: list[str] = []

        # Static CC list
        static_list = cc_config.get("static_list") or []
        cc_list.extend(static_list)

        # RAGIC mail loop (dynamic)
        mail_loop = cc_config.get("ragic_mail_loop", {})
        if mail_loop.get("enabled") and case_id:
            account_id = mail_loop.get("account_id", "")
            tab_name = mail_loop.get("tab_name", "")
            sheet_id = mail_loop.get("sheet_id", "")
            domain = mail_loop.get("domain", "tickets.ragic.com")
            # Format: {account_id}.{tab_name}.{sheet_id}.{record_id}@tickets.ragic.com
            ragic_email = f"{account_id}.{tab_name}.{sheet_id}.{case_id}@{domain}"
            cc_list.append(ragic_email)

        return cc_list

    @staticmethod
    def _email_type_to_config_key(email_type: EmailType) -> str:
        """Map EmailType enum to config YAML key."""
        mapping = {
            EmailType.ANOMALY_NOTIFICATION: "anomaly_notification",
            EmailType.QUESTIONNAIRE_NOTIFICATION: "questionnaire_notification",
            EmailType.SUPPLEMENT_NOTIFICATION: "supplement_notification",
            EmailType.TAIPOWER_APPLICATION: "taipower_application",
            EmailType.TAIPOWER_ELECTRICITY_REQUEST: "taipower_electricity_request",
            EmailType.TAIPOWER_SUPPLEMENT: "taipower_supplement",
            EmailType.APPROVAL_NOTIFICATION: "approval_notification",
            EmailType.ACCOUNT_ACTIVATION: "account_activation",
        }
        return mapping.get(email_type, "")


# =============================================================================
# Email Sending
# =============================================================================


# Module-level config instance (loaded once per Lambda cold start)
_email_config: EmailConfig | None = None


def _get_email_config() -> EmailConfig:
    """Get or create the EmailConfig singleton."""
    global _email_config
    if _email_config is None:
        _email_config = EmailConfig()
    return _email_config


@retry_ses
def send_email(request: EmailRequest) -> EmailResult:
    """Send an email via AWS SES with template rendering and optional attachments.

    Resolves the template configuration, builds dynamic links, renders the
    HTML body, and sends via SES. Logs the result to S3.

    Args:
        request: EmailRequest containing type, recipient, template data,
                 and optional attachments.

    Returns:
        EmailResult with success status, message_id, and timing.

    Raises:
        EmailSendError: On SES send failure (triggers retry via decorator).
    """
    config = _get_email_config()
    template_config = config.get_template_config(request.email_type)

    if not template_config:
        error_msg = f"No template configuration for email type: {request.email_type.value}"
        logger.error(error_msg, extra={"case_id": request.case_id, "operation_type": "email_send_error"})
        return EmailResult(success=False, error_message=error_msg)

    # Build questionnaire/link URL if configured
    link_config = template_config.get("link")
    enriched_data = dict(request.template_data)
    enriched_data.setdefault("case_id", request.case_id)

    if link_config:
        questionnaire_url = config.build_link_url(link_config, enriched_data)
        enriched_data["questionnaire_url"] = questionnaire_url
        # Also provide login_url alias for account_activation template
        enriched_data.setdefault("login_url", questionnaire_url)

    # Render subject and body
    logger.info(
        f"DEBUG enriched_data keys: {list(enriched_data.keys())}, dreams_apply_id={enriched_data.get('dreams_apply_id', 'NOT_FOUND')}",
        extra={"case_id": request.case_id, "operation_type": "email_debug_template_data"},
    )
    subject = config.render_subject(template_config["subject"], enriched_data)
    html_body = config.render_template(template_config["template_file"], enriched_data)

    # Build sender address
    sender = f"{config.sender_name} <{config.sender_email}>"

    # Build CC list: request-level CC + config-level CC (static + RAGIC mail loop)
    cc_list = list(request.cc_emails or [])
    config_cc = config.get_cc_list(request.case_id)
    for cc_addr in config_cc:
        if cc_addr not in cc_list:
            cc_list.append(cc_addr)

    log_operation(
        logger,
        case_id=request.case_id,
        operation_type="email_sending",
        message=f"Sending {request.email_type.value} to {request.recipient_email}, cc={cc_list}",
    )

    try:
        if request.attachments:
            message_id = _send_raw_email(
                sender=sender,
                recipient=request.recipient_email,
                subject=subject,
                html_body=html_body,
                attachments=request.attachments,
                cc=cc_list,
            )
        else:
            message_id = _send_simple_email(
                sender=sender,
                recipient=request.recipient_email,
                subject=subject,
                html_body=html_body,
                cc=cc_list,
            )

        sent_at = datetime.now(timezone.utc).isoformat()

        log_operation(
            logger,
            case_id=request.case_id,
            operation_type="email_sent",
            message=f"Email sent successfully: {message_id}",
        )

        result = EmailResult(
            success=True,
            message_id=message_id,
            sent_at=sent_at,
        )

        # Log success
        _save_email_log(
            case_id=request.case_id,
            email_type=request.email_type,
            recipient=request.recipient_email,
            status="sent",
            message_id=message_id,
            sent_at=sent_at,
        )

        return result

    except Exception as e:
        error_msg = str(e)
        logger.error(
            f"Email send failed: {error_msg}",
            extra={"case_id": request.case_id, "operation_type": "email_send_error"},
        )

        # Log failure
        _save_email_log(
            case_id=request.case_id,
            email_type=request.email_type,
            recipient=request.recipient_email,
            status="failed",
            error_message=error_msg,
        )

        raise EmailSendError(
            service_name="SES",
            message=f"Failed to send {request.email_type.value}: {error_msg}",
        ) from e


def _send_simple_email(
    sender: str,
    recipient: str,
    subject: str,
    html_body: str,
    cc: list[str] | None = None,
) -> str:
    """Send a simple HTML email without attachments via SES.

    Returns:
        SES MessageId.
    """
    ses = _get_ses_client()
    destination: dict = {"ToAddresses": [recipient]}
    if cc:
        destination["CcAddresses"] = cc

    response = ses.send_email(
        Source=sender,
        Destination=destination,
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Html": {"Data": html_body, "Charset": "UTF-8"}},
        },
    )
    return response["MessageId"]


def _send_raw_email(
    sender: str,
    recipient: str,
    subject: str,
    html_body: str,
    attachments: list[Attachment],
    cc: list[str] | None = None,
) -> str:
    """Send a raw MIME email with attachments via SES.

    Returns:
        SES MessageId.
    """
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    if cc:
        msg["Cc"] = ", ".join(cc)

    # HTML body part
    body_part = MIMEText(html_body, "html", "utf-8")
    msg.attach(body_part)

    # Attachment parts
    for attachment in attachments:
        att_part = MIMEApplication(attachment.content)
        att_part.add_header(
            "Content-Disposition",
            "attachment",
            filename=attachment.filename,
        )
        att_part.add_header("Content-Type", attachment.content_type)
        msg.attach(att_part)

    ses = _get_ses_client()
    all_recipients = [recipient] + (cc or [])
    response = ses.send_raw_email(
        Source=sender,
        Destinations=all_recipients,
        RawMessage={"Data": msg.as_string()},
    )
    return response["MessageId"]


# =============================================================================
# Recipient Resolution
# =============================================================================


def get_recipient_email(case_id: str) -> str:
    """Retrieve recipient email address from RAGIC case management form.

    Args:
        case_id: Case record ID in RAGIC.

    Returns:
        Customer email address string.
    """
    from dreams_workflow.shared.ragic_client import CloudRagicClient

    client = CloudRagicClient()
    try:
        record = client.get_questionnaire_data(case_id)
        return record.get("customer_email", record.get("email", ""))
    finally:
        client.close()


# =============================================================================
# Email Log Persistence (S3)
# =============================================================================

EMAIL_LOG_BUCKET = os.environ.get("EMAIL_LOG_BUCKET", "")


def _save_email_log(
    case_id: str,
    email_type: EmailType,
    recipient: str,
    status: str,
    message_id: str | None = None,
    sent_at: str | None = None,
    retry_count: int = 0,
    error_message: str | None = None,
) -> None:
    """Save an email log record to S3.

    Logs are stored as JSON files under:
        s3://{bucket}/email-logs/{case_id}/{log_id}.json

    If EMAIL_LOG_BUCKET is not configured, logs are written to CloudWatch only.
    """
    log_record = EmailLog(
        log_id=str(uuid.uuid4()),
        case_id=case_id,
        email_type=email_type.value,
        recipient=recipient,
        sent_at=sent_at,
        status=status,
        message_id=message_id,
        retry_count=retry_count,
        error_message=error_message,
    )

    # Always log to CloudWatch
    log_operation(
        logger,
        case_id=case_id,
        operation_type="email_log",
        message=f"EmailLog: type={email_type.value}, status={status}, "
        f"recipient={recipient}, message_id={message_id}",
    )

    # Persist to S3 if bucket is configured
    if EMAIL_LOG_BUCKET:
        try:
            s3 = _get_s3_client()
            key = f"email-logs/{case_id}/{log_record.log_id}.json"
            s3.put_object(
                Bucket=EMAIL_LOG_BUCKET,
                Key=key,
                Body=json.dumps(
                    {
                        "log_id": log_record.log_id,
                        "case_id": log_record.case_id,
                        "email_type": log_record.email_type,
                        "recipient": log_record.recipient,
                        "sent_at": log_record.sent_at,
                        "status": log_record.status,
                        "message_id": log_record.message_id,
                        "retry_count": log_record.retry_count,
                        "error_message": log_record.error_message,
                    },
                    ensure_ascii=False,
                ),
                ContentType="application/json",
            )
        except Exception as e:
            # Log persistence failure should not break email sending
            logger.warning(
                f"Failed to save email log to S3: {e}",
                extra={"case_id": case_id, "operation_type": "email_log_error"},
            )


# =============================================================================
# Lambda Handler
# =============================================================================


def lambda_handler(event: dict, context: Any) -> dict:
    """Email Service Lambda entry point.

    Accepts an EmailRequest-like event and sends the email.

    Event format:
        {
            "email_type": "問卷通知",
            "case_id": "CASE-001",
            "recipient_email": "customer@example.com",
            "template_data": {...},
            "attachments": [{"filename": "...", "content_base64": "...", "content_type": "..."}]
        }

    Returns:
        Dict with statusCode and body containing EmailResult.
    """
    import base64

    logger.info(
        "Email service invoked",
        extra={"case_id": event.get("case_id", "N/A"), "operation_type": "email_service_invoke"},
    )

    try:
        # Parse email type
        email_type_value = event.get("email_type", "")
        email_type = None
        for et in EmailType:
            if et.value == email_type_value:
                email_type = et
                break

        if email_type is None:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": f"Unknown email type: {email_type_value}"}),
            }

        # Parse attachments (base64 encoded in Lambda event)
        attachments = None
        raw_attachments = event.get("attachments")
        if raw_attachments:
            attachments = []
            for att in raw_attachments:
                attachments.append(
                    Attachment(
                        filename=att["filename"],
                        content=base64.b64decode(att["content_base64"]),
                        content_type=att.get("content_type", "application/octet-stream"),
                    )
                )

        request = EmailRequest(
            email_type=email_type,
            case_id=event["case_id"],
            recipient_email=event["recipient_email"],
            template_data=event.get("template_data", {}),
            attachments=attachments,
        )

        result = send_email(request)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "success": result.success,
                "message_id": result.message_id,
                "sent_at": result.sent_at,
                "error_message": result.error_message,
            }),
        }

    except EmailSendError as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "success": False,
                "error_message": str(e),
            }),
        }
    except Exception as e:
        logger.error(
            f"Unexpected error in email service: {e}",
            extra={"case_id": event.get("case_id", "N/A"), "operation_type": "email_service_error"},
        )
        return {
            "statusCode": 500,
            "body": json.dumps({
                "success": False,
                "error_message": f"Internal error: {e}",
            }),
        }
