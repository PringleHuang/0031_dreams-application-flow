"""Custom exceptions for DREAMS workflow system."""


class InvalidTransitionError(Exception):
    """當案件狀態轉換路徑不合法時拋出"""

    def __init__(self, current_status: str, target_status: str, message: str = ""):
        self.current_status = current_status
        self.target_status = target_status
        if not message:
            message = (
                f"Invalid state transition from '{current_status}' to '{target_status}'"
            )
        super().__init__(message)


class ExternalServiceError(Exception):
    """外部服務呼叫失敗基礎例外"""

    def __init__(self, service_name: str, message: str, retry_count: int = 0):
        self.service_name = service_name
        self.message = message
        self.retry_count = retry_count
        super().__init__(f"[{service_name}] {message} (retries: {retry_count})")


class DreamsConnectionError(ExternalServiceError):
    """DREAMS 系統連線失敗"""

    pass


class RagicCommunicationError(ExternalServiceError):
    """RAGIC 平台通訊失敗"""

    pass


class EmailSendError(ExternalServiceError):
    """電子郵件發送失敗"""

    pass
