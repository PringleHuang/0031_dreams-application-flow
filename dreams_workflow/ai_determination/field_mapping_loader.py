"""Loader for RAGIC field mapping configuration.

Reads field_mapping.yaml and provides structured access to the mapping
between questionnaire fields and case management form fields.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dreams_workflow.shared.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_CONFIG_PATH = str(Path(__file__).parent / "field_mapping.yaml")

# Module-level cache
_field_mapping: dict[str, Any] | None = None


def load_field_mapping(config_path: str | None = None) -> dict[str, Any]:
    """Load field mapping configuration from YAML file.

    Args:
        config_path: Path to field_mapping.yaml. Defaults to the file
            in the same directory as this module.

    Returns:
        Parsed YAML configuration dict.
    """
    global _field_mapping
    if _field_mapping is not None:
        return _field_mapping

    path = config_path or _DEFAULT_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        _field_mapping = yaml.safe_load(f)

    return _field_mapping


def get_direct_mapping() -> dict[str, str]:
    """Get direct field mapping (questionnaire field ID → case form field ID).

    These fields are written directly without AI determination.
    """
    config = load_field_mapping()
    return config.get("direct_mapping", {})


def get_llm_result_mapping() -> dict[str, dict[str, str]]:
    """Get LLM result mapping (document name → {questionnaire field ID → case form field ID}).

    These fields contain the values extracted by AI from each document.
    """
    config = load_field_mapping()
    return config.get("llm_result_mapping", {})


def get_questionnaire_result_mapping() -> dict[str, str]:
    """Get questionnaire result mapping (questionnaire field ID → case form result field ID).

    These fields contain Pass/Fail for each verified field.
    """
    config = load_field_mapping()
    return config.get("questionnaire_result_mapping", {})


def get_taipower_result_mapping() -> dict[str, str]:
    """Get taipower result mapping (questionnaire field ID → case form result field ID).

    These fields contain Pass/Fail from taipower reply semantic analysis.
    """
    config = load_field_mapping()
    return config.get("taipower_result_mapping", {})


def get_status_field_id() -> str:
    """Get the case status field ID in the case management form."""
    config = load_field_mapping()
    if "status_field_id" in config:
        return config["status_field_id"]
    # Fallback to ragic_fields.yaml
    try:
        from dreams_workflow.shared.ragic_fields_config import get_field_id
        return get_field_id("case_management", "case_status", "1015456")
    except Exception:
        return "1015456"


def build_complete_write_payload(
    questionnaire_data: dict[str, str],
    llm_extracted_values: dict[str, dict[str, str]],
    field_results: dict[str, str],
    new_status: str,
) -> dict[str, Any]:
    """Build the complete payload for a single RAGIC POST to the case management form.

    Combines:
    1. Direct mapping fields (questionnaire values → case form fields)
    2. LLM extracted values (AI-determined values → case form fields)
    3. Pass/Fail results (per-field determination results)
    4. Status update

    Args:
        questionnaire_data: Questionnaire form data (questionnaire field ID → value).
        llm_extracted_values: AI-extracted values per document
            (document_name → {questionnaire_field_id → extracted_value}).
        field_results: Per-field Pass/Fail results
            (questionnaire field ID → "Pass" or "Fail").
        new_status: New case status value to set.

    Returns:
        Complete payload dict ready for RAGIC POST (case form field ID → value).
    """
    payload: dict[str, Any] = {}

    # 1. Direct mapping: questionnaire field values → case form fields
    direct_map = get_direct_mapping()
    for q_field_id, case_field_id in direct_map.items():
        value = questionnaire_data.get(q_field_id, "")
        if value:
            payload[case_field_id] = value

    # 2. LLM extracted values → case form fields
    llm_map = get_llm_result_mapping()
    for doc_name, field_map in llm_map.items():
        doc_extracted = llm_extracted_values.get(doc_name, {})
        for q_field_id, case_field_id in field_map.items():
            value = doc_extracted.get(q_field_id, "")
            if value:
                payload[case_field_id] = value

    # 3. Pass/Fail results → questionnaire result fields
    result_map = get_questionnaire_result_mapping()
    for q_field_id, result_field_id in result_map.items():
        result_value = field_results.get(q_field_id, "")
        if result_value:
            payload[result_field_id] = result_value

    # 4. Status update
    status_field = get_status_field_id()
    payload[status_field] = new_status

    return payload


def get_supplement_param_codes() -> dict[str, str]:
    """Get supplement parameter code mapping (questionnaire field ID → code letter A~Q).

    Used to build the supplement params string when a field is Fail/Yes.
    """
    config = load_field_mapping()
    return config.get("supplement_param_codes", {})


def get_supplement_params_separator() -> str:
    """Get the separator for multi-select supplement params (default '|')."""
    config = load_field_mapping()
    return config.get("supplement_params_separator", "|")


def get_supplement_form_path() -> str:
    """Get the supplement form path (e.g., 'work-survey/9')."""
    config = load_field_mapping()
    return config.get("supplement_form_path", "work-survey/9")


def get_supplement_params_field_id() -> str:
    """Get the supplement params field ID in the supplement form."""
    config = load_field_mapping()
    if "supplement_params_field_id" in config:
        return config["supplement_params_field_id"]
    # Fallback to ragic_fields.yaml
    try:
        from dreams_workflow.shared.ragic_fields_config import get_field_id
        return get_field_id("determination_results", "supplement_params", "1016697")
    except Exception:
        return "1016697"


def get_dreams_apply_id_fields() -> dict[str, str]:
    """Get DREAMS_APPLY_ID field IDs for each form."""
    config = load_field_mapping()
    return config.get("dreams_apply_id_field", {})


def get_shipment_order_id_fields() -> dict[str, str]:
    """Get shipment order ID field IDs for each form."""
    config = load_field_mapping()
    return config.get("shipment_order_id_field", {})


def get_supplement_param_groups() -> dict[str, list[str]]:
    """Get supplement parameter group definitions.

    Groups define fields that must be supplemented together — if any field
    in a group is Fail/Yes, all fields in that group trigger the same
    supplement code.

    Returns:
        Dict of code letter → list of questionnaire field IDs in that group.
    """
    config = load_field_mapping()
    return config.get("supplement_param_groups", {})


def build_supplement_params(
    result_fields: dict[str, str],
    result_type: str = "questionnaire",
) -> str:
    """Build the supplement params string from Fail/Yes result fields.

    Reads the result fields (questionnaire_result or taipower_result),
    finds fields with value "Fail" or "Yes", maps them to supplement
    parameter codes (A~N), and joins with '|'.

    Group logic: For grouped fields (e.g., 併聯方式/併聯點型式/併聯點電壓 all
    map to code "L"), if ANY field in the group is Fail/Yes, the group code
    is included once.

    Args:
        result_fields: Dict of field_id → result value ("Pass"/"Fail"/"Yes").
        result_type: "questionnaire" or "taipower" (determines which result mapping to use).

    Returns:
        Pipe-separated string of supplement codes, e.g. "A|F|L".
    """
    param_codes = get_supplement_param_codes()
    separator = get_supplement_params_separator()

    # Get the result mapping to find which questionnaire field IDs correspond to result fields
    if result_type == "questionnaire":
        result_mapping = get_questionnaire_result_mapping()
    else:
        result_mapping = get_taipower_result_mapping()

    # Reverse map: case_form_result_field_id → questionnaire_field_id
    reverse_map = {v: k for k, v in result_mapping.items()}

    # Get group definitions for group-aware supplement logic
    param_groups = get_supplement_param_groups()
    # Build reverse group map: questionnaire_field_id → group_code
    field_to_group: dict[str, str] = {}
    for group_code, field_ids in param_groups.items():
        for fid in field_ids:
            field_to_group[fid] = group_code

    failed_codes: set[str] = set()
    for field_id, value in result_fields.items():
        if value in ("Fail", "Yes"):
            # field_id might be the case form result field ID
            q_field_id = reverse_map.get(field_id, field_id)

            # Check if this field belongs to a group
            group_code = field_to_group.get(q_field_id)
            if group_code:
                # Add the group code (handles deduplication via set)
                failed_codes.add(group_code)
            else:
                # Non-grouped field: use its individual code
                code = param_codes.get(q_field_id, "")
                if code:
                    failed_codes.add(code)

    # Sort alphabetically for consistency
    sorted_codes = sorted(failed_codes)
    return separator.join(sorted_codes)
