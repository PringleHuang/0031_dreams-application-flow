"""Cloud RAGIC API client for DREAMS workflow system.

Handles all interactions with the cloud RAGIC platform (https://ap13.ragic.com),
including questionnaire data retrieval, attachment downloads, case status updates,
and determination result writes.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import requests
import requests.adapters

from dreams_workflow.shared.exceptions import RagicCommunicationError
from dreams_workflow.shared.logger import get_logger, log_operation
from dreams_workflow.shared.models import CaseStatus
from dreams_workflow.shared.retry_config import retry_ragic

logger = get_logger(__name__)

# RAGIC API constants
_DEFAULT_TIMEOUT = 30
_RETRY_BACKOFF_FACTOR = 1
_RETRY_STATUS_FORCELIST = [500, 502, 503, 504]


class CloudRagicClient:
    """Cloud RAGIC API client (https://ap13.ragic.com).

    Provides methods for reading/writing case management form data,
    downloading attachments, and managing case status.

    Configuration is read from environment variables:
        - RAGIC_BASE_URL: Base URL (default: https://ap13.ragic.com)
        - RAGIC_ACCOUNT_NAME: Account name (default: solarcs)
        - RAGIC_API_KEY: API key for authentication
        - RAGIC_TIMEOUT: Request timeout in seconds (default: 30)
    """

    def __init__(
        self,
        base_url: str | None = None,
        account_name: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ):
        self.base_url = base_url or os.environ.get(
            "RAGIC_BASE_URL", "https://ap13.ragic.com"
        )
        self.account_name = account_name or os.environ.get(
            "RAGIC_ACCOUNT_NAME", "solarcs"
        )
        self.api_key = api_key or os.environ.get("RAGIC_API_KEY", "")
        self.timeout = timeout or int(
            os.environ.get("RAGIC_TIMEOUT", str(_DEFAULT_TIMEOUT))
        )

        # Case management form path
        self.case_form_path = "business-process2"
        self.case_form_index = 2

        # Questionnaire form path
        self.questionnaire_form_path = "work-survey"
        self.questionnaire_form_index = 7

        # File download endpoint
        self.file_download_url = f"{self.base_url}/sims/file.jsp"

        self._session = self._create_session()

    def _create_session(self) -> requests.Session:
        """Create a requests session with retry strategy and auth headers."""
        session = requests.Session()
        session.headers.update({"Authorization": f"Basic {self.api_key}"})

        retry_strategy = requests.adapters.Retry(
            total=3,
            backoff_factor=_RETRY_BACKOFF_FACTOR,
            status_forcelist=_RETRY_STATUS_FORCELIST,
        )
        adapter = requests.adapters.HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        return session

    def _build_url(self, form_path: str, form_index: int, record_id: str = "") -> str:
        """Build the full RAGIC API URL for a form/record."""
        url = f"{self.base_url}/{self.account_name}/{form_path}/{form_index}"
        if record_id:
            url = f"{url}/{record_id}"
        return url

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict:
        """Execute a GET request with error handling.

        Args:
            url: Full request URL.
            params: Query parameters.

        Returns:
            Parsed JSON response as dict.

        Raises:
            RagicCommunicationError: On request failure.
        """
        if params is None:
            params = {}
        params.setdefault("api", "")
        params.setdefault("v", 3)
        params.setdefault("naming", "EID")

        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            raise RagicCommunicationError(
                service_name="RAGIC",
                message=f"HTTP {e.response.status_code}: {e.response.text[:200]}",
            ) from e
        except requests.exceptions.RequestException as e:
            raise RagicCommunicationError(
                service_name="RAGIC",
                message=f"Request failed: {e}",
            ) from e

    def _post(self, url: str, data: dict[str, Any]) -> dict:
        """Execute a POST request with error handling.

        Args:
            url: Full request URL.
            data: JSON payload (field_id: value pairs only).

        Returns:
            Parsed JSON response as dict.

        Raises:
            RagicCommunicationError: On request failure.
        """
        # RAGIC API control parameters go in query string, not body
        params = {
            "api": "",
            "v": 3,
            "doFormula": "true",
            "doDefaultValue": "true",
            "doLinkLoad": "first",
        }

        try:
            # Log request details for debugging
            import json as _json
            logger.warning(
                f"[RAGIC_DEBUG] POST url={url} params={params} data={_json.dumps(data, ensure_ascii=False)}",
                extra={"case_id": "N/A", "operation_type": "ragic_debug_request"},
            )

            resp = self._session.post(
                url, params=params, json=data, timeout=self.timeout
            )

            # Log full response regardless of status for debugging
            logger.warning(
                f"[RAGIC_DEBUG] RESPONSE status={resp.status_code} body={repr(resp.text[:500])}",
                extra={"case_id": "N/A", "operation_type": "ragic_debug_response"},
            )

            resp.raise_for_status()

            # RAGIC may return HTTP 200 but with error in body
            # e.g. {'status': 'INVALID', 'code': 202, 'msg': '欄位 X 為必填'}
            if resp.text:
                try:
                    result = resp.json()
                except ValueError:
                    return {"_raw_response": resp.text[:500]}

                # Check for RAGIC-level error in response
                if isinstance(result, dict) and result.get("status") == "INVALID":
                    error_msg = result.get("msg", "Unknown RAGIC error")
                    raise RagicCommunicationError(
                        service_name="RAGIC",
                        message=f"RAGIC rejected write: {error_msg} (code={result.get('code')})",
                    )
                return result
            return {}
        except requests.exceptions.HTTPError as e:
            raise RagicCommunicationError(
                service_name="RAGIC",
                message=f"HTTP {e.response.status_code}: {e.response.text[:200]}",
            ) from e
        except requests.exceptions.RequestException as e:
            raise RagicCommunicationError(
                service_name="RAGIC",
                message=f"Request failed: {e}",
            ) from e

    # =========================================================================
    # Questionnaire & Document Methods
    # =========================================================================

    @retry_ragic
    def get_questionnaire_data(self, record_id: str) -> dict:
        """Retrieve questionnaire form data for a given record.

        Args:
            record_id: RAGIC record ID in the questionnaire form.

        Returns:
            Dict containing the questionnaire field data.
        """
        url = self._build_url(
            self.questionnaire_form_path, self.questionnaire_form_index, record_id
        )
        log_operation(
            logger,
            case_id=record_id,
            operation_type="ragic_get_questionnaire",
            message=f"Fetching questionnaire data for record {record_id}",
        )
        result = self._get(url)
        return result

    @retry_ragic
    def get_supporting_documents(
        self,
        record_id: str,
        form_path: str | None = None,
        form_index: int | None = None,
    ) -> list[tuple[str, bytes]]:
        """Download all supporting document attachments for a record.

        Reads attachment field values from the specified form and downloads
        each file. The attachment field value format is '{fileKey}@{fileName}'.

        Args:
            record_id: RAGIC record ID.
            form_path: Form path (default: questionnaire_form_path = "work-survey").
            form_index: Form index (default: questionnaire_form_index = 7).

        Returns:
            List of (filename, file_bytes) tuples for each successfully
            downloaded attachment.
        """
        if form_path is None:
            form_path = self.questionnaire_form_path
        if form_index is None:
            form_index = self.questionnaire_form_index

        log_operation(
            logger,
            case_id=record_id,
            operation_type="ragic_get_documents",
            message=f"Downloading supporting documents for record {record_id} from {form_path}/{form_index}",
        )

        # Get the record data to find attachment fields
        url = self._build_url(form_path, form_index, record_id)
        record_data = self._get(url)

        # Known attachment field IDs (from ragic_fields.yaml)
        from dreams_workflow.shared.ragic_fields_config import get_document_attachment_fields

        doc_fields = get_document_attachment_fields()
        attachment_field_ids = list(doc_fields.values())

        documents: list[tuple[str, bytes]] = []
        for field_id in attachment_field_ids:
            file_value = record_data.get(field_id, "")
            if not file_value or "@" not in str(file_value):
                continue

            file_bytes, file_name = self._download_attachment(str(file_value))
            if file_bytes is not None:
                documents.append((file_name, file_bytes))

        log_operation(
            logger,
            case_id=record_id,
            operation_type="ragic_get_documents",
            message=f"Downloaded {len(documents)} documents for record {record_id}",
        )
        return documents

    def _download_attachment(self, file_value: str) -> tuple[bytes | None, str]:
        """Download a single RAGIC attachment file.

        Args:
            file_value: RAGIC attachment field value in format '{fileKey}@{fileName}'.

        Returns:
            (file_bytes, file_name) or (None, file_name) on failure.
        """
        if not file_value or "@" not in file_value:
            return None, ""

        file_name = file_value.split("@", 1)[1]
        encoded_value = quote(file_value, safe="")
        url = f"{self.file_download_url}?a={self.account_name}&f={encoded_value}"

        try:
            resp = self._session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            if not resp.content:
                logger.warning(
                    f"Attachment download empty: {file_name}",
                    extra={"operation_type": "ragic_download_attachment"},
                )
                return None, file_name
            return resp.content, file_name
        except requests.exceptions.RequestException as e:
            logger.error(
                f"Attachment download failed: {file_name}: {e}",
                extra={"operation_type": "ragic_download_attachment"},
            )
            return None, file_name

    # =========================================================================
    # Case Management Methods
    # =========================================================================

    @retry_ragic
    def get_case_status(self, case_id: str) -> CaseStatus:
        """Get the current status of a case from RAGIC case management form.

        Args:
            case_id: The case record ID in RAGIC.

        Returns:
            Current CaseStatus enum value.

        Raises:
            RagicCommunicationError: On API failure.
            ValueError: If the status value doesn't match any CaseStatus.
        """
        url = self._build_url(self.case_form_path, self.case_form_index, case_id)
        result = self._get(url)

        # Read status using configurable field ID
        try:
            from dreams_workflow.shared.ragic_fields_config import get_field_id
            status_field_id = get_field_id("case_management", "case_status", "1015456")
        except Exception:
            status_field_id = "1015456"

        status_value = result.get(status_field_id, result.get("status", result.get("案件狀態", "")))

        # Try to match the status value to CaseStatus enum
        for status in CaseStatus:
            if status.value == status_value:
                return status

        raise ValueError(
            f"Unknown case status '{status_value}' for case {case_id}"
        )

    @retry_ragic
    def get_case_record(self, case_id: str) -> dict:
        """Get the full case record from RAGIC case management form.

        Used by downstream Lambdas to resolve case context when processing
        questionnaire (work-survey/7) or supplement (work-survey/9) webhooks,
        which don't include case status in their payload.

        Args:
            case_id: The case record ID (ragicId) in RAGIC.

        Returns:
            Full record dict with all field values.

        Raises:
            RagicCommunicationError: On API failure.
        """
        url = self._build_url(self.case_form_path, self.case_form_index, case_id)
        return self._get(url)

    @retry_ragic
    def update_case_status(self, case_id: str, status: str) -> None:
        """Update the case status in RAGIC case management form.

        Writes to the case status field (field ID from ragic_fields.yaml).
        This will trigger a RAGIC Webhook for status change events.

        Args:
            case_id: The case record ID in RAGIC.
            status: The new status value string.
        """
        url = self._build_url(self.case_form_path, self.case_form_index, case_id)
        # Use the configurable case status field ID
        try:
            from dreams_workflow.shared.ragic_fields_config import get_field_id
            status_field_id = get_field_id("case_management", "case_status", "1015456")
        except Exception:
            status_field_id = "1015456"

        data = {status_field_id: status}

        log_operation(
            logger,
            case_id=case_id,
            operation_type="ragic_update_status",
            message=f"Updating case {case_id} field {status_field_id} to '{status}'",
        )
        self._post(url, data)

    @retry_ragic
    def write_determination_result(self, case_id: str, result: dict) -> None:
        """Write AI determination result to RAGIC case management form.

        Args:
            case_id: The case record ID in RAGIC.
            result: The ComparisonReport or SemanticAnalysisResult as dict.
        """
        url = self._build_url(self.case_form_path, self.case_form_index, case_id)

        import json

        data = {"ai_determination_result": json.dumps(result, ensure_ascii=False)}

        log_operation(
            logger,
            case_id=case_id,
            operation_type="ragic_write_determination",
            message=f"Writing AI determination result for case {case_id}",
        )
        self._post(url, data)

    @retry_ragic
    def create_supplement_questionnaire(
        self, case_id: str, failed_items: list[str]
    ) -> str:
        """Create a supplement questionnaire containing only failed items.

        Args:
            case_id: The case record ID.
            failed_items: List of failed item descriptions/IDs.

        Returns:
            URL link to the created supplement questionnaire.
        """
        log_operation(
            logger,
            case_id=case_id,
            operation_type="ragic_create_supplement",
            message=f"Creating supplement questionnaire for case {case_id} "
            f"with {len(failed_items)} failed items",
        )

        # Create a new record in the supplement form with failed items
        url = self._build_url(
            self.questionnaire_form_path, self.questionnaire_form_index
        )
        data = {
            "case_id": case_id,
            "supplement_items": ", ".join(failed_items),
            "is_supplement": "Y",
        }
        result = self._post(url, data)

        # Return the questionnaire link
        new_record_id = result.get("ragicTempRecordKey", "")
        questionnaire_url = (
            f"{self.base_url}/{self.account_name}/"
            f"{self.questionnaire_form_path}/{self.questionnaire_form_index}"
            f"/{new_record_id}"
        )
        return questionnaire_url

    @retry_ragic
    def update_case_record(self, case_id: str, update_data: dict) -> None:
        """Update arbitrary fields in the case management form.

        Used for renewal closure write-back, rejection reason writes, etc.

        Args:
            case_id: The case record ID in RAGIC.
            update_data: Dict of field names/IDs to values to update.
        """
        url = self._build_url(self.case_form_path, self.case_form_index, case_id)

        log_operation(
            logger,
            case_id=case_id,
            operation_type="ragic_update_record",
            message=f"Updating case record {case_id} with {len(update_data)} fields",
        )
        self._post(url, update_data)

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> "CloudRagicClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
