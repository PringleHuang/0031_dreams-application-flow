"""State machine for DREAMS case status transitions.

Defines valid transition paths and provides functions to validate and execute
case status transitions with logging.
"""

from __future__ import annotations

from typing import Protocol

from dreams_workflow.shared.exceptions import InvalidTransitionError
from dreams_workflow.shared.logger import get_logger, log_operation
from dreams_workflow.shared.models import CaseStatus

logger = get_logger(__name__)

# 合法狀態轉換定義
VALID_TRANSITIONS: dict[CaseStatus, list[CaseStatus]] = {
    CaseStatus.NEW_CASE_CREATED: [
        CaseStatus.PENDING_QUESTIONNAIRE,  # 發送問卷通知後更新
    ],
    CaseStatus.PENDING_QUESTIONNAIRE: [
        CaseStatus.PENDING_MANUAL_CONFIRM,  # 新約 AI 判定完成
        CaseStatus.RENEWAL_PROCESSING,  # 續約案件分流
    ],
    CaseStatus.RENEWAL_PROCESSING: [
        CaseStatus.CASE_CLOSED,  # 續約完成直接結案
    ],
    CaseStatus.PENDING_MANUAL_CONFIRM: [
        CaseStatus.TAIPOWER_REVIEW,  # 人工在 RAGIC 改狀態（合格）
        CaseStatus.INFO_SUPPLEMENT,  # 人工在 RAGIC 改狀態（不合格）
    ],
    CaseStatus.INFO_SUPPLEMENT: [
        CaseStatus.PENDING_MANUAL_CONFIRM,  # 補件後 AI 判定完成
    ],
    CaseStatus.TAIPOWER_REVIEW: [
        CaseStatus.PRE_SEND_CONFIRM,  # 台電回覆後進入發送前人工確認
    ],
    CaseStatus.PRE_SEND_CONFIRM: [
        CaseStatus.INSTALLATION_PHASE,  # 人工確認核准，進入安裝階段
        CaseStatus.TAIPOWER_SUPPLEMENT,  # 人工確認需補件
    ],
    CaseStatus.TAIPOWER_SUPPLEMENT: [
        CaseStatus.PENDING_MANUAL_CONFIRM,  # 補件AI判讀完成→待人工確認
    ],
    CaseStatus.INSTALLATION_PHASE: [
        CaseStatus.ONLINE_COMPLETED,  # 自主檢查通過
    ],
    CaseStatus.ONLINE_COMPLETED: [
        CaseStatus.CASE_CLOSED,  # 資料同步完成
    ],
}


def validate_transition(current: CaseStatus, target: CaseStatus) -> bool:
    """Validate whether a state transition is allowed.

    Args:
        current: The current case status.
        target: The desired target status.

    Returns:
        True if the transition is valid, False otherwise.
    """
    allowed_targets = VALID_TRANSITIONS.get(current, [])
    return target in allowed_targets


class CaseStatusStore(Protocol):
    """Protocol for case status persistence operations."""

    def get_case_status(self, case_id: str) -> CaseStatus: ...

    def update_case_status(self, case_id: str, status: str) -> None: ...


def transition_case_status(
    case_id: str,
    new_status: CaseStatus,
    reason: str,
    *,
    current_status: CaseStatus | None = None,
    store: CaseStatusStore | None = None,
) -> bool:
    """Execute a case status transition with validation and logging.

    Validates that the transition is legal according to VALID_TRANSITIONS,
    logs the operation, and returns success status.

    The current status can be provided directly via ``current_status`` to avoid
    an external lookup, or it will be fetched from ``store``. If neither is
    provided, the function imports CloudRagicClient as the default store.

    Args:
        case_id: The case identifier.
        new_status: The target status to transition to.
        reason: The reason for the status change.
        current_status: Optional current status (avoids store lookup if given).
        store: Optional store implementing get/update case status.

    Returns:
        True if the transition was successful.

    Raises:
        InvalidTransitionError: When the transition path is not allowed.
    """
    if current_status is None:
        if store is None:
            from dreams_workflow.shared.ragic_client import CloudRagicClient

            store = CloudRagicClient()
        current_status = store.get_case_status(case_id)

    if not validate_transition(current_status, new_status):
        log_operation(
            logger,
            case_id=case_id,
            operation_type="state_transition_rejected",
            message=(
                f"Invalid transition from '{current_status.value}' "
                f"to '{new_status.value}': {reason}"
            ),
            level="warning",
        )
        raise InvalidTransitionError(
            current_status=current_status.value,
            target_status=new_status.value,
        )

    # Update status in RAGIC (source of truth) if store is available
    if store is not None:
        store.update_case_status(case_id, new_status.value)

    log_operation(
        logger,
        case_id=case_id,
        operation_type="state_transition",
        message=(
            f"Status transitioned from '{current_status.value}' "
            f"to '{new_status.value}': {reason}"
        ),
        level="info",
    )

    return True
