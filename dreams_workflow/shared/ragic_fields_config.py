"""Centralized RAGIC field ID configuration loader.

All RAGIC field IDs are defined in ragic_fields.yaml and accessed through
this module. When RAGIC form design changes, only the YAML file needs updating.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_config: dict[str, Any] | None = None


def _load_config() -> dict[str, Any]:
    """Load ragic_fields.yaml (cached after first load)."""
    global _config
    if _config is None:
        config_path = Path(__file__).parent / "ragic_fields.yaml"
        with open(config_path, "r", encoding="utf-8") as f:
            _config = yaml.safe_load(f)
    return _config


def get_case_management_fields() -> dict[str, str]:
    """Get case management form field ID mapping.

    Returns:
        Dict mapping logical name → RAGIC field ID.
        e.g. {"case_status": "1015456", "dreams_apply_id": "1016557", ...}
    """
    return _load_config().get("case_management", {})


def get_document_attachment_fields() -> dict[str, str]:
    """Get document attachment field ID mapping.

    Returns:
        Dict mapping document logical name → RAGIC field ID.
        e.g. {"审竣图": "1014650", ...}
    """
    return _load_config().get("document_attachments", {})


def get_questionnaire_fields() -> dict[str, str]:
    """Get questionnaire form field ID mapping.

    Returns:
        Dict mapping logical name → RAGIC field ID.
        e.g. {"site_address": "1014595", ...}
    """
    return _load_config().get("questionnaire_fields", {})


def get_determination_result_fields() -> dict[str, str]:
    """Get determination result field ID mapping."""
    return _load_config().get("determination_results", {})


def get_status_values() -> dict[str, str]:
    """Get status value definitions.

    Returns:
        Dict mapping logical name → status string value.
        e.g. {"new_case": "新開案件", "pending_questionnaire": "待填問卷"}
    """
    return _load_config().get("status_values", {})


def get_field_id(section: str, key: str, default: str = "") -> str:
    """Get a single field ID by section and key.

    Args:
        section: Config section name (e.g. "case_management").
        key: Field logical name (e.g. "case_status").
        default: Default value if not found.

    Returns:
        The RAGIC field ID string.
    """
    return _load_config().get(section, {}).get(key, default)
