"""DREAMS Form API Client.

Calls the external DREAMS Form API (maintained by another team) to submit
application forms. The API handles the actual RPA/crawler form filling and
PDF generation within the DREAMS system.

Requirements: 5.1, 15.2
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests

from dreams_workflow.shared.exceptions import DreamsConnectionError
from dreams_workflow.shared.logger import get_logger, log_operation
from dreams_workflow.shared.retry_config import retry_dreams

logger = get_logger(__name__)

# Error codes returned by the DREAMS Form API
ERROR_NO_ELECTRICITY_NUMBER = "NO_ELECTRICITY_NUMBER"


@dataclass
class DreamsApiResponse:
    """DREAMS Form API response.

    Attributes:
        success: Whether the form submission was successful.
        case_number: DREAMS case number (returned on success).
        pdf_base64: Application PDF in base64 (returned on success).
        error_code: Error code on failure (e.g., "NO_ELECTRICITY_NUMBER").
        error_message: Human-readable error message on failure.
    """

    success: bool
    case_number: str | None = None
    pdf_base64: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class DreamsApiClient:
    """Client for the DREAMS Form API.

    The DREAMS Form API is an external service maintained by another team.
    It handles RPA/crawler form filling in the DREAMS system and returns
    the case number and application PDF on success.

    Configuration is read from environment variables:
        - DREAMS_API_URL: Base URL of the DREAMS Form API
        - DREAMS_API_TIMEOUT: Request timeout in seconds (default: 120)
    """

    def __init__(
        self,
        api_url: str | None = None,
        timeout: int | None = None,
    ):
        """Initialize the DREAMS API client.

        Args:
            api_url: DREAMS Form API URL. Defaults to DREAMS_API_URL env var.
            timeout: Request timeout in seconds. Defaults to 120 (crawler is slow).
        """
        self.api_url = api_url or os.environ.get("DREAMS_API_URL", "")
        self.timeout = timeout or int(os.environ.get("DREAMS_API_TIMEOUT", "120"))

        if not self.api_url:
            logger.warning(
                "DREAMS_API_URL not configured",
                extra={"case_id": "N/A", "operation_type": "dreams_api_init"},
            )

    @retry_dreams
    def submit_application(
        self, case_id: str, case_data: dict[str, Any]
    ) -> DreamsApiResponse:
        """Submit an application to the DREAMS Form API.

        Calls the external API which performs RPA form filling in DREAMS.
        The API checks if the electricity number exists, fills the form,
        and returns the case number + PDF on success.

        Args:
            case_id: RAGIC case record ID (for logging/correlation).
            case_data: Case data to submit, including:
                - electricity_number: The electricity number for the case
                - customer_name: Customer name
                - site_address: Site address
                - Other fields as required by the DREAMS form

        Returns:
            DreamsApiResponse with:
                - success=True: case_number and pdf_base64 populated
                - success=False, error_code="NO_ELECTRICITY_NUMBER": electricity number not found
                - success=False, other error_code: other failures

        Raises:
            DreamsConnectionError: On API communication failure (triggers retry).
        """
        log_operation(
            logger,
            case_id=case_id,
            operation_type="dreams_api_submit",
            message=f"Submitting application to DREAMS API: {self.api_url}",
        )

        if not self.api_url:
            return DreamsApiResponse(
                success=False,
                error_code="NOT_CONFIGURED",
                error_message="DREAMS_API_URL not configured",
            )

        payload = {
            "case_id": case_id,
            **case_data,
        }

        try:
            resp = requests.post(
                self.api_url,
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else None
            error_text = e.response.text[:200] if e.response else str(e)

            log_operation(
                logger,
                case_id=case_id,
                operation_type="dreams_api_error",
                message=f"DREAMS API HTTP error {status_code}: {error_text}",
                level="error",
            )
            raise DreamsConnectionError(
                service_name="DREAMS_API",
                message=f"HTTP {status_code}: {error_text}",
            ) from e

        except requests.exceptions.RequestException as e:
            log_operation(
                logger,
                case_id=case_id,
                operation_type="dreams_api_error",
                message=f"DREAMS API connection error: {e}",
                level="error",
            )
            raise DreamsConnectionError(
                service_name="DREAMS_API",
                message=f"Connection failed: {e}",
            ) from e

        # Parse API response
        success = data.get("success", False)

        if success:
            response = DreamsApiResponse(
                success=True,
                case_number=data.get("case_number", ""),
                pdf_base64=data.get("pdf_base64", ""),
            )
            log_operation(
                logger,
                case_id=case_id,
                operation_type="dreams_api_success",
                message=f"DREAMS API success: case_number={response.case_number}",
            )
        else:
            error_code = data.get("error_code", "UNKNOWN")
            error_message = data.get("error_message", "Unknown error")
            response = DreamsApiResponse(
                success=False,
                error_code=error_code,
                error_message=error_message,
            )
            log_operation(
                logger,
                case_id=case_id,
                operation_type="dreams_api_failed",
                message=f"DREAMS API failed: {error_code} - {error_message}",
                level="warning",
            )

        return response
