"""Data models and enumerations for DREAMS workflow system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class CaseStatus(str, Enum):
    """案件狀態列舉"""

    NEW_CASE_CREATED = "新開案件"
    PENDING_QUESTIONNAIRE = "待填問卷"
    PENDING_MANUAL_CONFIRM = "待人工確認"
    INFO_SUPPLEMENT = "資訊補件"
    TAIPOWER_REVIEW = "台電審核"
    PRE_SEND_CONFIRM = "發送前人工確認"
    TAIPOWER_SUPPLEMENT = "台電補件"
    INSTALLATION_PHASE = "安裝階段"
    ONLINE_COMPLETED = "完成上線"
    CASE_CLOSED = "已結案"
    RENEWAL_PROCESSING = "續約處理"


class CaseType(str, Enum):
    """案件類型列舉"""

    NEW_CONTRACT = "新約"
    RENEWAL = "續約"


class WebhookEventType(str, Enum):
    """Webhook 事件類型列舉"""

    NEW_CASE_CREATED = "NEW_CASE_CREATED"
    CASE_STATUS_CHANGED = "CASE_STATUS_CHANGED"
    RENEWAL_QUESTIONNAIRE = "RENEWAL_QUESTIONNAIRE"
    NEW_CONTRACT_FULL_QUESTIONNAIRE = "NEW_CONTRACT_FULL_QUESTIONNAIRE"
    SUPPLEMENTARY_QUESTIONNAIRE = "SUPPLEMENTARY_QUESTIONNAIRE"


class EmailType(str, Enum):
    """郵件類型列舉"""

    QUESTIONNAIRE_NOTIFICATION = "問卷通知"
    SUPPLEMENT_NOTIFICATION = "補件通知"
    TAIPOWER_APPLICATION = "台電審核申請"
    TAIPOWER_SUPPLEMENT = "台電補件通知"
    APPROVAL_NOTIFICATION = "核准通知"
    ACCOUNT_ACTIVATION = "帳號啟用通知"


@dataclass
class CaseRecord:
    """對應 RAGIC 案件管理表單的一筆記錄"""

    ragic_id: str
    case_type: CaseType
    customer_name: str
    customer_email: str
    electricity_number: str | None
    current_status: CaseStatus
    dreams_case_id: str | None
    taipower_contact_email: str | None
    company_contact_email: str
    renewal_site_id: str | None
    ai_determination_result: dict | None
    taipower_reply_result: dict | None
    created_at: str
    updated_at: str


@dataclass
class AIJudgmentRecord:
    """AI 判定結果，寫入 RAGIC 案件管理表單的 JSON 欄位"""

    case_id: str
    judgment_type: Literal["document_comparison", "semantic_analysis"]
    timestamp: str
    result: dict
    model_id: str


@dataclass
class EmailLog:
    """郵件發送紀錄"""

    log_id: str
    case_id: str
    email_type: EmailType
    recipient: str
    sent_at: str | None
    status: Literal["sent", "failed", "retrying"]
    message_id: str | None
    retry_count: int = 0
    error_message: str | None = None
