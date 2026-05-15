"""DREAMS 台電 API Client.

Calls the GCP-hosted DREAMS TPDirect API to:
- CreatePlantApplication (API #10): Submit a new plant application to Taipower
- GetPlantApplicationPdf (API #11): Download the application PDF

Base URL: http://35.236.137.19/DREAMS_Service_TP_V3/api/TPDirect
Authentication: Handled by GCP side (token auto-refresh via Hangfire Job)

Requirements: 5.1, 5.2, 5.3, 5.4
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any

import requests

from dreams_workflow.shared.exceptions import DreamsConnectionError
from dreams_workflow.shared.logger import get_logger, log_operation
from dreams_workflow.shared.retry_config import retry_dreams

logger = get_logger(__name__)

# Error codes
ERROR_NO_ELECTRICITY_NUMBER = "MANUAL_CREATE"  # plantNoStatus from ValidatePlantNo

# Default API base URL (GCP direct)
DEFAULT_API_BASE_URL = "http://35.236.137.19/DREAMS_Service_TP/api/TPDirect"


@dataclass
class DreamsApiResponse:
    """DREAMS API response.

    Attributes:
        success: Whether the application was submitted successfully.
        case_number: DREAMS plant number (plantNo) on success.
        pdf_base64: Application PDF in base64 on success.
        error_code: Error code on failure.
        error_message: Human-readable error message on failure.
        raw_response: Full API response dict for debugging.
    """

    success: bool
    case_number: str | None = None
    pdf_base64: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw_response: dict | None = None


# =============================================================================
# Value Conversion Maps (RAGIC values → DREAMS API values)
# =============================================================================

# 案場類型: RAGIC display value → API plantType
PLANT_TYPE_MAP = {
    "屋頂型": "pv-rooftop",
    "地面型": "pv-ground",
    "水面型": "pv-floating",
}

# 併聯方式: RAGIC display value → API parallelType
PARALLEL_TYPE_MAP = {
    "低壓內線": "inner",
    "低壓外線": "outer",
    "高壓": "outer",
    "特高壓": "outer",
}

# 售電方式: RAGIC display value → API retailingPolicy
RETAILING_POLICY_MAP = {
    "躉售": "fit",
    "自用餘電躉售": "non_fit",
    "自發自用": "non_fit",
}

# 併聯點型式 / 責任分界點型式: RAGIC display value → API phaseType
PHASE_TYPE_MAP = {
    "單相二線": "1-2",
    "單相三線": "1-3",
    "三相三線": "3-3",
    "三相四線": "3-4",
}

# 監控設備廠商 ID (友達=3, 或依實際設定)
MONITORING_DEVICE_VENDOR_ID = 3


class DreamsApiClient:
    """Client for the DREAMS TPDirect API (GCP direct).

    Calls CreatePlantApplication and GetPlantApplicationPdf.

    Configuration:
        - DREAMS_API_URL: Base URL (default: http://35.236.137.19/DREAMS_Service_TP_V3/api/TPDirect)
        - DREAMS_API_TIMEOUT: Request timeout in seconds (default: 60)
    """

    def __init__(
        self,
        api_url: str | None = None,
        timeout: int | None = None,
    ):
        self.api_url = api_url or os.environ.get("DREAMS_API_URL", DEFAULT_API_BASE_URL)
        self.timeout = timeout or int(os.environ.get("DREAMS_API_TIMEOUT", "60"))

    @retry_dreams
    def submit_application(
        self, case_id: str, case_data: dict[str, Any]
    ) -> DreamsApiResponse:
        """Submit a new plant application via CreatePlantApplication API.

        Converts RAGIC field values to API format and calls the API.
        On success, also downloads the application PDF.

        Args:
            case_id: RAGIC case record ID (for logging).
            case_data: Case data from RAGIC, containing:
                - electricity_number: 電號 (may contain "-", will be stripped)
                - site_name: 案場名稱
                - customer_name: 客戶名稱
                - owner_name: 負責人姓名
                - owner_phone: 負責人電話
                - site_address: 案場詳細地址
                - capacity_kw: 裝置量(kW)
                - plant_type: 案場類型 (RAGIC display value)
                - parallel_type: 併聯方式 (RAGIC display value)
                - retailing_policy: 售電方式 (RAGIC display value)
                - agreement_number: 縣府同意備案函文編號
                - parallel_phase_type: 併聯點型式 (RAGIC display value)
                - parallel_voltage: 併聯點電壓 (V or kV)
                - service_phase_type: 責任分界點型式 (RAGIC display value)
                - service_voltage: 責任分界點電壓 (V or kV)
                - inverters: 逆變器匯總 (format: "brand|model|qty, brand|model|qty")
                - install_date: 市電併聯日 (YYYY-MM-DD)

        Returns:
            DreamsApiResponse with success/failure info and PDF.

        Raises:
            DreamsConnectionError: On API communication failure (triggers retry).
        """
        log_operation(
            logger,
            case_id=case_id,
            operation_type="dreams_api_submit",
            message=f"Submitting plant application to DREAMS API",
        )

        # Build API request body
        api_body = self._build_api_body(case_data)

        log_operation(
            logger,
            case_id=case_id,
            operation_type="dreams_api_body",
            message=f"API body: {json.dumps(api_body, ensure_ascii=False)}",
        )

        # Call CreatePlantApplication
        url = f"{self.api_url}/CreatePlantApplication"
        try:
            resp = requests.post(url, json=api_body, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else None
            error_text = e.response.text[:200] if e.response else str(e)
            log_operation(
                logger, case_id=case_id,
                operation_type="dreams_api_error",
                message=f"CreatePlantApplication HTTP {status_code}: {error_text}",
                level="error",
            )
            raise DreamsConnectionError(
                service_name="DREAMS_API",
                message=f"HTTP {status_code}: {error_text}",
            ) from e
        except requests.exceptions.RequestException as e:
            log_operation(
                logger, case_id=case_id,
                operation_type="dreams_api_error",
                message=f"CreatePlantApplication connection error: {e}",
                level="error",
            )
            raise DreamsConnectionError(
                service_name="DREAMS_API",
                message=f"Connection failed: {e}",
            ) from e

        # Parse response
        is_success = data.get("IsSuccess", False)

        if not is_success:
            error_msg = data.get("ErrorMessage", "Unknown error")
            # Determine error code from message
            if "已被其他案場使用" in error_msg or "VIRTUAL_EXHAUSTED" in error_msg:
                error_code = "VIRTUAL_EXHAUSTED"
            elif "不存在" in error_msg or "MANUAL_CREATE" in error_msg:
                error_code = ERROR_NO_ELECTRICITY_NUMBER
            else:
                error_code = "API_ERROR"

            log_operation(
                logger, case_id=case_id,
                operation_type="dreams_api_failed",
                message=f"CreatePlantApplication failed: {error_code} - {error_msg}",
                level="warning",
            )
            return DreamsApiResponse(
                success=False,
                error_code=error_code,
                error_message=error_msg,
                raw_response=data,
            )

        # Success — extract case info from response
        response_data = data.get("Data", {})
        plant_no = api_body["plantNo"]
        application_id = response_data.get("id", 0)
        plant_id = response_data.get("plantId", 0)

        log_operation(
            logger, case_id=case_id,
            operation_type="dreams_api_success",
            message=f"CreatePlantApplication success: id={application_id}, plantId={plant_id}, plantNo={plant_no}",
        )

        # Download application PDF
        pdf_base64 = self._get_application_pdf(case_id, plant_no)

        return DreamsApiResponse(
            success=True,
            case_number=plant_no,
            pdf_base64=pdf_base64,
            raw_response=data,
        )

    def get_application_pdf(self, case_id: str, plant_no: str) -> str | None:
        """Public method to download application PDF (for re-sending emails).

        Args:
            case_id: Case ID for logging.
            plant_no: Plant number (11-digit, no dashes).

        Returns:
            Base64-encoded PDF string, or None on failure.
        """
        return self._get_application_pdf(case_id, plant_no)

    def _get_application_pdf(self, case_id: str, plant_no: str) -> str | None:
        """Download the plant application PDF via GetPlantApplicationPdf API.

        Args:
            case_id: Case ID for logging.
            plant_no: Plant number (11-digit, no dashes).

        Returns:
            Base64-encoded PDF string, or None on failure.
        """
        url = f"{self.api_url}/GetPlantApplicationPdf"
        params = {"plantNo": plant_no}

        log_operation(
            logger, case_id=case_id,
            operation_type="dreams_api_get_pdf",
            message=f"Downloading application PDF for plantNo={plant_no}",
        )

        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()

            if resp.headers.get("Content-Type", "").startswith("application/pdf"):
                pdf_base64 = base64.b64encode(resp.content).decode("utf-8")
                log_operation(
                    logger, case_id=case_id,
                    operation_type="dreams_api_pdf_success",
                    message=f"PDF downloaded: {len(resp.content)} bytes",
                )
                return pdf_base64
            else:
                # API might return JSON error
                try:
                    error_data = resp.json()
                    log_operation(
                        logger, case_id=case_id,
                        operation_type="dreams_api_pdf_error",
                        message=f"PDF API returned non-PDF: {error_data}",
                        level="warning",
                    )
                except Exception:
                    log_operation(
                        logger, case_id=case_id,
                        operation_type="dreams_api_pdf_error",
                        message=f"PDF API returned unexpected content-type: {resp.headers.get('Content-Type')}",
                        level="warning",
                    )
                return None

        except requests.exceptions.RequestException as e:
            log_operation(
                logger, case_id=case_id,
                operation_type="dreams_api_pdf_error",
                message=f"Failed to download PDF: {e}",
                level="error",
            )
            return None

    def _build_api_body(self, case_data: dict[str, Any]) -> dict[str, Any]:
        """Convert RAGIC case data to DREAMS CreatePlantApplication request body.

        Handles:
        - Electricity number: strip dashes
        - Plant type, parallel type, retailing policy: value conversion
        - Phase types: value conversion
        - Voltages: convert kV to V if needed
        - Inverters: parse "brand|model|qty" format to {"_model": "qty"}

        Args:
            case_data: Case data dict from RAGIC.

        Returns:
            API request body dict.
        """
        # Electricity number: strip dashes
        plant_no = str(case_data.get("electricity_number", "")).replace("-", "").strip()

        # Value conversions
        plant_type = PLANT_TYPE_MAP.get(
            case_data.get("plant_type", ""), "pv-rooftop"
        )
        parallel_type = PARALLEL_TYPE_MAP.get(
            case_data.get("parallel_type", ""), "outer"
        )
        retailing_policy = RETAILING_POLICY_MAP.get(
            case_data.get("retailing_policy", ""), "non_fit"
        )
        parallel_phase_type = PHASE_TYPE_MAP.get(
            case_data.get("parallel_phase_type", ""), "3-3"
        )
        service_phase_type = PHASE_TYPE_MAP.get(
            case_data.get("service_phase_type", ""), "3-3"
        )

        # Voltage conversion (kV → V)
        parallel_voltage = self._parse_voltage(case_data.get("parallel_voltage", ""))
        service_voltage = self._parse_voltage(case_data.get("service_voltage", ""))

        # Capacity
        capacity = self._parse_float(case_data.get("capacity_kw", "0"))

        # Inverters: parse "brand|model|qty, brand|model|qty" → {"_model": "qty"}
        inverters = self._parse_inverters(case_data.get("inverters", ""))

        # Install date
        install_date = case_data.get("install_date", "")

        body = {
            "plantNo": plant_no,
            "plantName": case_data.get("site_name", ""),
            "company": case_data.get("customer_name", ""),
            "ownerName": case_data.get("owner_name", ""),
            "ownerPhoneNumber": case_data.get("owner_phone", ""),
            "toInstallAt": install_date,
            "plantType": plant_type,
            "parallelType": parallel_type,
            "retailingPolicy": retailing_policy,
            "agreementNumber": case_data.get("agreement_number", ""),
            "address": case_data.get("site_address", ""),
            "totalCapacity": capacity,
            "parallelPhaseType": parallel_phase_type,
            "parallelVoltage": parallel_voltage,
            "servicePhaseType": service_phase_type,
            "serviceVoltage": service_voltage,
            "inverters": inverters,
            "monitoringDeviceVendorId": MONITORING_DEVICE_VENDOR_ID,
        }

        return body

    @staticmethod
    def _parse_voltage(value: str | int | float) -> int:
        """Parse voltage value to integer (in Volts).

        Handles formats like "22.8kV", "22800", "11.4kV", "380V".
        """
        if not value:
            return 0

        s = str(value).strip().upper()

        # Remove "V" suffix
        if s.endswith("V"):
            s = s[:-1]

        # Handle kV
        if s.endswith("K"):
            s = s[:-1]
            try:
                return int(float(s) * 1000)
            except ValueError:
                return 0

        # Plain number
        try:
            f = float(s)
            # If value is small (< 100), assume it's in kV
            if f < 100:
                return int(f * 1000)
            return int(f)
        except ValueError:
            return 0

    @staticmethod
    def _parse_float(value: str | int | float) -> float:
        """Parse a numeric value to float."""
        if not value:
            return 0.0
        try:
            return float(str(value).strip())
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_inverters(inverter_str: str) -> dict[str, str]:
        """Parse inverter summary string to API format.

        Input format: "brand|model|qty, brand|model|qty"
        Output format: {"_model": "qty", "_model": "qty"}

        The API uses "_" prefix for model names.
        """
        if not inverter_str:
            return {}

        result: dict[str, str] = {}
        parts = str(inverter_str).split(",")

        for part in parts:
            part = part.strip()
            if not part:
                continue

            segments = part.split("|")
            if len(segments) == 3:
                # brand|model|qty
                _, model, qty = segments
                model = model.strip()
                qty = qty.strip()
                if model and qty:
                    result[f"_{model}"] = qty
            elif len(segments) == 2:
                # model|qty (no brand)
                model, qty = segments
                model = model.strip()
                qty = qty.strip()
                if model and qty:
                    result[f"_{model}"] = qty

        return result
