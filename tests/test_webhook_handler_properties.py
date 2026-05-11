"""Property-based tests for Webhook event classification.

Property 6: Webhook 事件分類正確性
Validates: Requirements 11.2

Uses hypothesis to generate valid payload combinations, verifying that:
- The same payload always produces the same classification result (determinism)
- Classification is total: every valid payload maps to exactly one event type
- is_supplement flag always takes priority regardless of other fields
- Form path determines classification when is_supplement is absent
- Case type determines questionnaire sub-classification
"""

from __future__ import annotations

import importlib
import json
import os
from unittest.mock import patch

from hypothesis import given, settings, assume, strategies as st

os.environ.setdefault("AI_DETERMINATION_FUNCTION_NAME", "test-ai-function")
os.environ.setdefault("WORKFLOW_ENGINE_FUNCTION_NAME", "test-workflow-function")

import dreams_workflow.webhook_handler.app as app_module

importlib.reload(app_module)

from dreams_workflow.shared.models import WebhookEventType
from dreams_workflow.webhook_handler.app import classify_webhook_event


# =============================================================================
# Strategies for generating test data
# =============================================================================

# Known form paths
CASE_MANAGEMENT_FORM_PATHS = [
    "business-process2/2",
    "/solarcs/business-process2/2",
    "https://ap13.ragic.com/solarcs/business-process2/2",
]

QUESTIONNAIRE_FORM_PATHS = [
    "work-survey/7",
    "/solarcs/work-survey/7",
    "https://ap13.ragic.com/solarcs/work-survey/7",
]

UNKNOWN_FORM_PATHS = [
    "unknown/form/99",
    "other-sheet/1",
    "",
    "business-process/1",
    "work-survey/8",
]

# Actions
ACTIONS = ["create", "update", "edit", "delete", ""]

# Case types
CASE_TYPES = ["新約", "續約", "其他", ""]

# Strategy for case management form payloads
case_management_form_path_strategy = st.sampled_from(CASE_MANAGEMENT_FORM_PATHS)
questionnaire_form_path_strategy = st.sampled_from(QUESTIONNAIRE_FORM_PATHS)
unknown_form_path_strategy = st.sampled_from(UNKNOWN_FORM_PATHS)
action_strategy = st.sampled_from(ACTIONS)
case_type_strategy = st.sampled_from(CASE_TYPES)

# Strategy for case IDs
case_id_strategy = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(whitelist_categories=("L", "N", "Pd")),
)

# Strategy for arbitrary extra fields (should not affect classification)
extra_fields_strategy = st.fixed_dictionaries(
    {},
    optional={
        "customer_name": st.text(min_size=0, max_size=20),
        "customer_email": st.emails(),
        "electricity_number": st.text(min_size=0, max_size=15),
        "case_status": st.text(min_size=0, max_size=10),
        "timestamp": st.text(min_size=0, max_size=30),
    },
)

# Strategy for a complete valid payload
all_form_paths_strategy = st.one_of(
    case_management_form_path_strategy,
    questionnaire_form_path_strategy,
    unknown_form_path_strategy,
)


def _build_payload(
    form_path: str,
    action: str = "",
    case_type: str = "",
    is_supplement: bool | None = None,
    is_new_record: bool | None = None,
    case_id: str = "TEST-001",
    extra: dict | None = None,
) -> tuple[dict, dict]:
    """Build ragic_meta and record_data from components for classify_webhook_event.

    Returns:
        (ragic_meta, record_data) tuple matching the new function signature.
    """
    # Determine path and sheetIndex from form_path string
    path = ""
    sheet_index = 0
    if "business-process2" in form_path:
        path = "/business-process2"
        sheet_index = 2
    elif "work-survey/7" in form_path:
        path = "/work-survey"
        sheet_index = 7
    elif "work-survey/9" in form_path:
        path = "/work-survey"
        sheet_index = 9
    elif "work-survey/8" in form_path:
        path = "/work-survey"
        sheet_index = 8
    else:
        path = form_path
        sheet_index = 0

    ragic_meta = {
        "path": path,
        "sheetIndex": sheet_index,
        "eventType": action or "update",
        "apname": "solarcs",
    }

    record_data: dict = {}
    if case_type:
        record_data["case_type"] = case_type
    if case_id:
        record_data["_ragicId"] = case_id
    # For case management form, set status field for NEW_CASE_CREATED detection
    if "business-process2" in form_path and is_new_record:
        record_data["1015456"] = "新開案件"
    if extra:
        record_data.update(extra)
    return ragic_meta, record_data


# =============================================================================
# Property Tests
# =============================================================================


class TestWebhookEventClassificationDeterminism:
    """Property 6: Webhook 事件分類正確性 — 確定性"""

    # Feature: dreams-application-flow, Property 6: Webhook 事件分類正確性

    @settings(max_examples=200)
    @given(
        form_path=all_form_paths_strategy,
        action=action_strategy,
        case_type=case_type_strategy,
        is_supplement=st.one_of(st.none(), st.booleans()),
        is_new_record=st.one_of(st.none(), st.booleans()),
        case_id=case_id_strategy,
    )
    def test_same_payload_always_produces_same_classification(
        self,
        form_path: str,
        action: str,
        case_type: str,
        is_supplement: bool | None,
        is_new_record: bool | None,
        case_id: str,
    ):
        """Determinism: identical payloads always yield the same event type."""
        ragic_meta, record_data = _build_payload(
            form_path=form_path,
            action=action,
            case_type=case_type,
            is_supplement=is_supplement,
            is_new_record=is_new_record,
            case_id=case_id,
        )

        result1 = classify_webhook_event(ragic_meta, record_data)
        result2 = classify_webhook_event(ragic_meta, record_data)

        assert result1 == result2

    @settings(max_examples=200)
    @given(
        form_path=all_form_paths_strategy,
        action=action_strategy,
        case_type=case_type_strategy,
        is_supplement=st.one_of(st.none(), st.booleans()),
        is_new_record=st.one_of(st.none(), st.booleans()),
        case_id=case_id_strategy,
    )
    def test_classification_always_returns_valid_event_type(
        self,
        form_path: str,
        action: str,
        case_type: str,
        is_supplement: bool | None,
        is_new_record: bool | None,
        case_id: str,
    ):
        """Totality: every payload maps to exactly one valid WebhookEventType."""
        ragic_meta, record_data = _build_payload(
            form_path=form_path,
            action=action,
            case_type=case_type,
            is_supplement=is_supplement,
            is_new_record=is_new_record,
            case_id=case_id,
        )

        result = classify_webhook_event(ragic_meta, record_data)

        assert isinstance(result, WebhookEventType)
        assert result in list(WebhookEventType)


class TestWebhookEventClassificationPriority:
    """Property 6: Webhook 事件分類正確性 — is_supplement 優先順序"""

    # Feature: dreams-application-flow, Property 6: Webhook 事件分類正確性

    @settings(max_examples=100)
    @given(
        form_path=all_form_paths_strategy,
        action=action_strategy,
        case_type=case_type_strategy,
        is_new_record=st.one_of(st.none(), st.booleans()),
        case_id=case_id_strategy,
        extra=extra_fields_strategy,
    )
    def test_is_supplement_true_always_yields_supplementary(
        self,
        form_path: str,
        action: str,
        case_type: str,
        is_new_record: bool | None,
        case_id: str,
        extra: dict,
    ):
        """work-survey/9 path always classifies as SUPPLEMENTARY_QUESTIONNAIRE,
        regardless of action, case_type, or other fields."""
        # Override form_path to work-survey/9 (supplement form)
        ragic_meta = {
            "path": "/work-survey",
            "sheetIndex": 9,
            "eventType": action or "update",
            "apname": "solarcs",
        }
        record_data: dict = {}
        if case_type:
            record_data["case_type"] = case_type
        if extra:
            record_data.update(extra)

        result = classify_webhook_event(ragic_meta, record_data)
        assert result == WebhookEventType.SUPPLEMENTARY_QUESTIONNAIRE

    @settings(max_examples=100)
    @given(
        form_path=all_form_paths_strategy,
        action=action_strategy,
        case_type=case_type_strategy,
        is_new_record=st.one_of(st.none(), st.booleans()),
        case_id=case_id_strategy,
    )
    def test_is_supplement_false_does_not_force_supplementary(
        self,
        form_path: str,
        action: str,
        case_type: str,
        is_new_record: bool | None,
        case_id: str,
    ):
        """Non work-survey/9 paths should NOT classify as SUPPLEMENTARY_QUESTIONNAIRE."""
        # Ensure we're not using work-survey/9
        ragic_meta, record_data = _build_payload(
            form_path=form_path,
            action=action,
            case_type=case_type,
            is_supplement=False,
            is_new_record=is_new_record,
            case_id=case_id,
        )
        # _build_payload with is_supplement=False won't set sheetIndex=9
        assume(ragic_meta.get("sheetIndex") != 9)

        result = classify_webhook_event(ragic_meta, record_data)
        assert result != WebhookEventType.SUPPLEMENTARY_QUESTIONNAIRE


class TestWebhookEventClassificationFormPath:
    """Property 6: Webhook 事件分類正確性 — 表單路徑決定分類"""

    # Feature: dreams-application-flow, Property 6: Webhook 事件分類正確性

    @settings(max_examples=100)
    @given(
        form_path=case_management_form_path_strategy,
        action=st.sampled_from(["create"]),
        case_id=case_id_strategy,
    )
    def test_case_management_form_with_create_yields_new_case(
        self,
        form_path: str,
        action: str,
        case_id: str,
    ):
        """Case management form + status=新開案件 → NEW_CASE_CREATED."""
        ragic_meta, record_data = _build_payload(
            form_path=form_path,
            action=action,
            is_new_record=True,  # This sets 1015456=新開案件
            case_id=case_id,
        )

        result = classify_webhook_event(ragic_meta, record_data)
        assert result == WebhookEventType.NEW_CASE_CREATED

    @settings(max_examples=100)
    @given(
        form_path=case_management_form_path_strategy,
        case_id=case_id_strategy,
    )
    def test_case_management_form_with_is_new_record_yields_new_case(
        self,
        form_path: str,
        case_id: str,
    ):
        """Case management form + is_new_record=True → NEW_CASE_CREATED."""
        ragic_meta, record_data = _build_payload(
            form_path=form_path,
            is_new_record=True,
            case_id=case_id,
        )

        result = classify_webhook_event(ragic_meta, record_data)
        assert result == WebhookEventType.NEW_CASE_CREATED

    @settings(max_examples=100)
    @given(
        form_path=case_management_form_path_strategy,
        action=st.sampled_from(["update", "edit", "delete", ""]),
        case_id=case_id_strategy,
    )
    def test_case_management_form_without_create_yields_status_changed(
        self,
        form_path: str,
        action: str,
        case_id: str,
    ):
        """Case management form + non-create action → CASE_STATUS_CHANGED."""
        ragic_meta, record_data = _build_payload(
            form_path=form_path,
            action=action,
            is_new_record=False,
            case_id=case_id,
        )

        result = classify_webhook_event(ragic_meta, record_data)
        assert result == WebhookEventType.CASE_STATUS_CHANGED

    @settings(max_examples=100)
    @given(
        form_path=questionnaire_form_path_strategy,
        case_id=case_id_strategy,
    )
    def test_questionnaire_form_with_renewal_type_yields_renewal(
        self,
        form_path: str,
        case_id: str,
    ):
        """Questionnaire form + case_type=續約 → RENEWAL_QUESTIONNAIRE."""
        ragic_meta, record_data = _build_payload(
            form_path=form_path,
            case_type="續約",
            case_id=case_id,
        )

        result = classify_webhook_event(ragic_meta, record_data)
        assert result == WebhookEventType.RENEWAL_QUESTIONNAIRE

    @settings(max_examples=100)
    @given(
        form_path=questionnaire_form_path_strategy,
        case_type=st.sampled_from(["新約", "其他", ""]),
        case_id=case_id_strategy,
    )
    def test_questionnaire_form_without_renewal_yields_new_contract(
        self,
        form_path: str,
        case_type: str,
        case_id: str,
    ):
        """Questionnaire form + case_type != 續約 → NEW_CONTRACT_FULL_QUESTIONNAIRE."""
        assume(case_type != "續約")

        ragic_meta, record_data = _build_payload(
            form_path=form_path,
            case_type=case_type,
            case_id=case_id,
        )

        result = classify_webhook_event(ragic_meta, record_data)
        assert result == WebhookEventType.NEW_CONTRACT_FULL_QUESTIONNAIRE


class TestWebhookEventClassificationExtraFieldsIrrelevant:
    """Property 6: Webhook 事件分類正確性 — 額外欄位不影響分類"""

    # Feature: dreams-application-flow, Property 6: Webhook 事件分類正確性

    @settings(max_examples=100)
    @given(
        form_path=case_management_form_path_strategy,
        action=st.sampled_from(["create"]),
        case_id=case_id_strategy,
        extra=extra_fields_strategy,
    )
    def test_extra_fields_do_not_affect_case_management_classification(
        self,
        form_path: str,
        action: str,
        case_id: str,
        extra: dict,
    ):
        """Extra fields (customer_name, email, etc.) do not change classification."""
        ragic_meta_without, record_data_without = _build_payload(
            form_path=form_path,
            action=action,
            case_id=case_id,
        )
        ragic_meta_with, record_data_with = _build_payload(
            form_path=form_path,
            action=action,
            case_id=case_id,
            extra=extra,
        )

        result_without = classify_webhook_event(ragic_meta_without, record_data_without)
        result_with = classify_webhook_event(ragic_meta_with, record_data_with)

        assert result_without == result_with

    @settings(max_examples=100)
    @given(
        form_path=questionnaire_form_path_strategy,
        case_type=st.sampled_from(["新約", "續約"]),
        case_id=case_id_strategy,
        extra=extra_fields_strategy,
    )
    def test_extra_fields_do_not_affect_questionnaire_classification(
        self,
        form_path: str,
        case_type: str,
        case_id: str,
        extra: dict,
    ):
        """Extra fields do not change questionnaire classification."""
        ragic_meta_without, record_data_without = _build_payload(
            form_path=form_path,
            case_type=case_type,
            case_id=case_id,
        )
        ragic_meta_with, record_data_with = _build_payload(
            form_path=form_path,
            case_type=case_type,
            case_id=case_id,
            extra=extra,
        )

        result_without = classify_webhook_event(ragic_meta_without, record_data_without)
        result_with = classify_webhook_event(ragic_meta_with, record_data_with)

        assert result_without == result_with
