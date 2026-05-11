"""Configuration management for AI determination service.

Loads document comparison configuration including attachment field mappings,
allowed values for normalization, and Bedrock model settings.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Default Bedrock configuration
DEFAULT_BEDROCK_MODEL_ID = "jp.anthropic.claude-sonnet-4-6"
DEFAULT_BEDROCK_REGION = "ap-northeast-1"
DEFAULT_BEDROCK_MAX_TOKENS = 4096

# Allowed values for field normalization and comparison
ALLOWED_VALUES: dict[str, Any] = {
    "site_type": [
        "屋頂型太陽能",
        "地面型太陽能",
        "水面型太陽能",
        "地熱能發電",
        "生質能發電",
    ],
    "selling_method": [
        "全額躉售",
        "餘電躉售",
        "轉供餘躉",
        "全額轉供(不躉售)",
        "餘電轉供(不躉售)",
        "僅併聯不躉售",
    ],
    "connection_method": ["內線", "外線"],
    "connection_voltage_type": ["單相三線", "三相三線", "三相四線"],
    "demarcation_voltage_type": ["單相三線", "三相三線", "三相四線"],
    "connection_voltage_options": [
        "110V", "220V", "230V", "380V", "390V", "400V", "440V",
        "460V", "480V", "600V", "800V", "3.3kV", "4.16kV", "6.9kV",
        "11.4kV", "22.8kV", "33kV", "69kV",
    ],
    "demarcation_voltage_options": [
        "110V", "220V", "380V", "6.9kV", "11.4kV",
        "22.8kV", "69kV", "161kV", "345kV",
    ],
    "power_number_format": (
        "電號（售電號或用電電號）為 11 碼數字，中間可能以空格或-隔開，"
        "請統一以 XX-XX-XXXX-XX-X 格式輸出（如 18-38-7389-77-0）。"
        "注意不要將案號、備案編號誤認為電號，如果找不到符合 11 碼格式的電號請回傳 null"
    ),
    "inverter_models": [
        "SUN2000-40KTL-M3", "PV-15000T-U", "SUN2000-60KTL-M0",
        "PV-50000S2-U", "PV-10000S-U", "PV-300000S2-U",
        "SUN2000-100KTL-M1", "M100_283", "PV-20000T-U",
        "PV-75000H-U", "PV-60000H-U", "PV-30000S2-U",
        "PV-30000H-U", "PV-30000S-U", "PV-22000S2-U",
        "PV-22000S-U", "PV-125KH-U", "PV-15000S-U",
        "PV-110KH-U", "PV-60000T-U", "PV-75000T-U",
        "PV-30000T-U", "SG125CX-P2", "SG30CX-P2",
        "PV-5000S-HV", "SG50CX-P2", "TOUGH Pro 5K",
        "TRINERGY MAX", "TRINERGY MAX 30KW", "TOUGH-20K-3P",
        "Trinergy Plus 60KW", "TOUGH-60K-3P", "TOUGH-50K-3P",
        "TOUGH-10K-3P", "TOUGH-15K-3P", "SG33CX", "SG250HX",
        "SG125CX", "SG110CX", "BUI 100", "SE 66.6K Manager",
        "SE 40K Manager", "SE 33.3K Manager", "SE 25K Manager",
        "SE 17K Manager", "SE 120K Manager", "SE 100K Manager",
        "SE Unit", "M88H_121", "M70A_263", "M70A_262",
        "M30A_230", "M30A_121", "M20A_220", "M20A",
        "M125HV_111", "M10A", "M100_280", "M100_210",
        "H5E_220", "H5A_220", "H5", "H4A_221", "H3_210",
    ],
}

# Document attachment configuration: defines which fields to extract from each document
def _build_attachments_config() -> list[dict[str, Any]]:
    """Build ATTACHMENTS_CONFIG using field IDs from ragic_fields.yaml."""
    try:
        from dreams_workflow.shared.ragic_fields_config import (
            get_document_attachment_fields,
            get_questionnaire_fields,
        )
        doc_fields = get_document_attachment_fields()
        q_fields = get_questionnaire_fields()
    except Exception:
        # Fallback to hardcoded values if config loading fails
        doc_fields = {
            "审竣图": "1014650",
            "county_approval": "1014651",
            "detailed_negotiation": "1014652",
            "power_purchase_contract": "1014653",
            "connection_review": "1014654",
        }
        q_fields = {
            "site_address": "1014595",
            "capacity_kwp": "1014749",
            "connection_method": "1014619",
            "connection_voltage_type": "1014621",
            "connection_voltage_volt": "1014644",
            "demarcation_voltage_type": "1014622",
            "demarcation_voltage_volt": "1014645",
            "site_type": "1014618",
            "approval_number": "1014623",
            "selling_method": "1014620",
            "power_purchase_number": "1014590",
            "inverter_subtable": "_subtable_1014629",
            "inverter_model": "1014624",
            "inverter_quantity": "1014635",
        }

    return [
        {
            "document_id": "doc_1",
            "document_name": "審訖圖",
            "field_id": doc_fields.get("审竣图", "1014650"),
            "file_types": ["pdf", "image"],
            "require_llm": True,
            "extract_fields": [
                {
                    "extract_key": "site_address",
                    "description": "案場地址或地號（太陽能設置場所的完整地址或土地地號）",
                    "form_field_id": q_fields.get("site_address", "1014595"),
                    "form_field_name": "案場詳細地址",
                },
                {
                    "extract_key": "capacity_kwp",
                    "description": "案場裝置容量，單位為 kWp（太陽能板的總裝置容量，不是變流器或逆變器的容量），只回傳數字",
                    "form_field_id": q_fields.get("capacity_kwp", "1014749"),
                    "form_field_name": "裝置量(kW)",
                },
                {
                    "extract_key": "connection_method",
                    "description": "併聯方式",
                    "form_field_id": q_fields.get("connection_method", "1014619"),
                    "form_field_name": "併聯方式",
                    "allowed_values_key": "connection_method",
                },
                {
                    "extract_key": "connection_voltage_type",
                    "description": "併聯點型式",
                    "form_field_id": q_fields.get("connection_voltage_type", "1014621"),
                    "form_field_name": "併聯點型式",
                    "allowed_values_key": "connection_voltage_type",
                },
                {
                    "extract_key": "connection_voltage_volt",
                    "description": "併聯點電壓",
                    "form_field_id": q_fields.get("connection_voltage_volt", "1014644"),
                    "form_field_name": "併聯點電壓",
                    "allowed_values_key": "connection_voltage_options",
                },
                {
                    "extract_key": "demarcation_voltage_type",
                    "description": "責任分界點型式",
                    "form_field_id": q_fields.get("demarcation_voltage_type", "1014622"),
                    "form_field_name": "責任分界點型式",
                    "allowed_values_key": "demarcation_voltage_type",
                },
                {
                    "extract_key": "demarcation_voltage_volt",
                    "description": "責任分界點電壓",
                    "form_field_id": q_fields.get("demarcation_voltage_volt", "1014645"),
                    "form_field_name": "責任分界點電壓",
                    "allowed_values_key": "demarcation_voltage_options",
                },
                {
                    "extract_key": "inverters",
                    "description": "所有變流器（逆變器）的型號與數量，可能有多組",
                    "type": "inverter_array",
                    "subtable": q_fields.get("inverter_subtable", "_subtable_1014629"),
                    "model_field_id": q_fields.get("inverter_model", "1014624"),
                    "quantity_field_id": q_fields.get("inverter_quantity", "1014635"),
                    "form_field_name": "變流器",
                },
            ],
        },
        {
            "document_id": "doc_2",
            "document_name": "縣府同意備案函文",
            "field_id": doc_fields.get("county_approval", "1014651"),
            "file_types": ["pdf", "image"],
            "require_llm": True,
            "extract_fields": [
                {
                    "extract_key": "site_type",
                    "description": "案場類型",
                    "form_field_id": q_fields.get("site_type", "1014618"),
                    "form_field_name": "案場類型",
                    "allowed_values_key": "site_type",
                },
                {
                    "extract_key": "approval_number",
                    "description": "縣府同意備案函文編號（格式為：3碼英文-3碼數字PV4碼流水號，例如 KHH-112PV0748、TYU-114PV0107。注意不要將發文字號誤認為備案編號）",
                    "form_field_id": q_fields.get("approval_number", "1014623"),
                    "form_field_name": "縣府同意備案函文編號",
                },
                {
                    "extract_key": "selling_method",
                    "description": "售電方式",
                    "form_field_id": q_fields.get("selling_method", "1014620"),
                    "form_field_name": "售電方式",
                    "allowed_values_key": "selling_method",
                },
            ],
        },
        {
            "document_id": "doc_3",
            "document_name": "細部協商",
            "field_id": doc_fields.get("detailed_negotiation", "1014652"),
            "file_types": ["pdf", "image"],
            "require_llm": True,
            "extract_fields": [
                {
                    "extract_key": "site_type",
                    "description": "案場類型",
                    "form_field_id": q_fields.get("site_type", "1014618"),
                    "form_field_name": "案場類型",
                    "allowed_values_key": "site_type",
                },
                {
                    "extract_key": "selling_method",
                    "description": "售電方式",
                    "form_field_id": q_fields.get("selling_method", "1014620"),
                    "form_field_name": "售電方式",
                    "allowed_values_key": "selling_method",
                },
                {
                    "extract_key": "connection_method",
                    "description": "併聯方式",
                    "form_field_id": q_fields.get("connection_method", "1014619"),
                    "form_field_name": "併聯方式",
                    "allowed_values_key": "connection_method",
                },
                {
                    "extract_key": "connection_voltage_type",
                    "description": "併聯點型式",
                    "form_field_id": q_fields.get("connection_voltage_type", "1014621"),
                    "form_field_name": "併聯點型式",
                    "allowed_values_key": "connection_voltage_type",
                },
                {
                    "extract_key": "connection_voltage_volt",
                    "description": "併聯點電壓",
                    "form_field_id": q_fields.get("connection_voltage_volt", "1014644"),
                    "form_field_name": "併聯點電壓",
                    "allowed_values_key": "connection_voltage_options",
                },
                {
                    "extract_key": "demarcation_voltage_type",
                    "description": "責任分界點型式",
                    "form_field_id": q_fields.get("demarcation_voltage_type", "1014622"),
                    "form_field_name": "責任分界點型式",
                    "allowed_values_key": "demarcation_voltage_type",
                },
                {
                    "extract_key": "demarcation_voltage_volt",
                    "description": "責任分界點電壓",
                    "form_field_id": q_fields.get("demarcation_voltage_volt", "1014645"),
                    "form_field_name": "責任分界點電壓",
                    "allowed_values_key": "demarcation_voltage_options",
                },
                {
                    "extract_key": "inverters",
                    "description": "所有變流器（逆變器）的型號與數量，可能有多組",
                    "type": "inverter_array",
                    "subtable": q_fields.get("inverter_subtable", "_subtable_1014629"),
                    "model_field_id": q_fields.get("inverter_model", "1014624"),
                    "quantity_field_id": q_fields.get("inverter_quantity", "1014635"),
                    "form_field_name": "變流器",
                },
            ],
        },
        {
            "document_id": "doc_4",
            "document_name": "購售電契約封面及內文第一頁",
            "field_id": doc_fields.get("power_purchase_contract", "1014653"),
            "file_types": ["pdf", "image"],
            "require_llm": True,
            "extract_fields": [
                {
                    "extract_key": "site_address",
                    "description": "案場地址或地號（太陽能設置場所的完整地址或土地地號）",
                    "form_field_id": q_fields.get("site_address", "1014595"),
                    "form_field_name": "案場詳細地址",
                },
                {
                    "extract_key": "power_purchase_number",
                    "description": "躉售電號（台電電號）",
                    "form_field_id": q_fields.get("power_purchase_number", "1014590"),
                    "form_field_name": "電號",
                    "allowed_values_key": "power_number_format",
                },
                {
                    "extract_key": "site_type",
                    "description": "案場類型",
                    "form_field_id": q_fields.get("site_type", "1014618"),
                    "form_field_name": "案場類型",
                    "allowed_values_key": "site_type",
                },
                {
                    "extract_key": "selling_method",
                    "description": "售電方式",
                    "form_field_id": q_fields.get("selling_method", "1014620"),
                    "form_field_name": "售電方式",
                    "allowed_values_key": "selling_method",
                },
            ],
        },
        {
            "document_id": "doc_5",
            "document_name": "併聯審查意見書",
            "field_id": doc_fields.get("connection_review", "1014654"),
            "file_types": ["pdf", "image"],
            "require_llm": False,
            "check_upload_only": True,
            "extract_fields": [],
        },
    ]


# Build config on module load (cached)
ATTACHMENTS_CONFIG: list[dict[str, Any]] = _build_attachments_config()


def get_bedrock_config() -> dict[str, str | int]:
    """Get Bedrock configuration from environment or defaults.

    Environment variables:
        - BEDROCK_MODEL_ID: Model ID (default: jp.anthropic.claude-sonnet-4-6)
        - BEDROCK_REGION: AWS region (default: ap-northeast-1)
        - BEDROCK_MAX_TOKENS: Max tokens (default: 4096)
    """
    return {
        "model_id": os.environ.get("BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL_ID),
        "region": os.environ.get("BEDROCK_REGION", DEFAULT_BEDROCK_REGION),
        "max_tokens": int(
            os.environ.get("BEDROCK_MAX_TOKENS", str(DEFAULT_BEDROCK_MAX_TOKENS))
        ),
    }
