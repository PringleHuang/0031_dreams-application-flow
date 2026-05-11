"""Document comparison logic for AI determination service.

Compares LLM-extracted values from supporting documents against
questionnaire form values, with field-specific normalization.
"""

from __future__ import annotations

import ast
import re
import unicodedata
from typing import Any

from dreams_workflow.ai_determination.normalizer import (
    format_voltage,
    normalize_address,
    normalize_voltage,
)

# Site type short-form to full-form mapping
_SITE_TYPE_MAP: dict[str, str] = {
    "屋頂型": "屋頂型太陽能",
    "地面型": "地面型太陽能",
    "水面型": "水面型太陽能",
    "地熱": "地熱能發電",
    "生質": "生質能發電",
}
_SITE_TYPE_VALID: set[str] = {
    "屋頂型太陽能", "地面型太陽能", "水面型太陽能", "地熱能發電", "生質能發電"
}


def _normalize_site_type(value: str) -> str:
    """Normalize site type short-form to full allowed value."""
    v = value.strip()
    if v in _SITE_TYPE_VALID:
        return v
    for short, full in _SITE_TYPE_MAP.items():
        if short in v:
            return full
    return v


def _to_address_list(val: Any) -> list[str]:
    """Convert a value to a list of address strings."""
    if isinstance(val, list):
        return [str(v).strip() for v in val if v and str(v).strip()]
    s = str(val).strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            parsed = ast.literal_eval(s)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if v and str(v).strip()]
        except (ValueError, SyntaxError):
            pass
    if "\n" in s:
        return [x.strip() for x in s.split("\n") if x.strip()]
    return [s] if s else []


def compare_values(extracted: Any, form_value: Any, extract_key: str = "") -> dict:
    """Compare an LLM-extracted value against a form value.

    Applies field-specific normalization before comparison.

    Args:
        extracted: Value extracted by LLM from the document.
        form_value: Value from the questionnaire form.
        extract_key: Field key for applying specific normalization rules.

    Returns:
        Dict with keys: match (bool), extracted, form_value, note.
    """
    if extracted is None and form_value is None:
        return {
            "match": True,
            "extracted": None,
            "form_value": None,
            "note": "兩者皆為空",
        }
    if extracted is None:
        return {
            "match": False,
            "extracted": None,
            "form_value": str(form_value),
            "note": "附件中未找到此欄位",
        }
    if form_value is None or str(form_value).strip() == "":
        return {
            "match": False,
            "extracted": str(extracted),
            "form_value": None,
            "note": "表單欄位為空",
        }

    ext_str = str(extracted).strip()
    form_str = str(form_value).strip()

    # Site type normalization
    if extract_key == "site_type":
        ext_str = _normalize_site_type(ext_str)
        form_str = _normalize_site_type(form_str)

    # Address normalization (supports list comparison)
    if extract_key in ("site_address",):
        return _compare_addresses(extracted, form_value)

    # Voltage normalization
    if extract_key in ("connection_voltage_volt", "demarcation_voltage_volt"):
        ext_v = normalize_voltage(ext_str)
        form_v = normalize_voltage(form_str)
        if ext_v is not None and form_v is not None and ext_v == form_v:
            display = format_voltage(ext_v)
            return {
                "match": True,
                "extracted": display,
                "form_value": display,
                "note": "完全一致（電壓正規化後）",
            }

    # Power number normalization
    if extract_key in ("power_purchase_number",):
        ext_digits = re.sub(r"[^\d]", "", ext_str)
        form_digits = re.sub(r"[^\d]", "", form_str)
        if ext_digits and form_digits and ext_digits == form_digits:
            formatted = _format_power_number(ext_digits)
            return {
                "match": True,
                "extracted": formatted,
                "form_value": formatted,
                "note": "完全一致（電號正規化後）",
            }

    # Numeric normalization
    if extract_key in ("capacity_kwp", "inverter_quantity"):
        try:
            ext_num = str(float(ext_str)).rstrip("0").rstrip(".")
            form_num = str(float(form_str)).rstrip("0").rstrip(".")
            if ext_num == form_num:
                return {
                    "match": True,
                    "extracted": ext_str,
                    "form_value": form_str,
                    "note": "完全一致（數值正規化後）",
                }
        except (ValueError, TypeError):
            pass

    # General normalization
    def _basic(s: str) -> str:
        s = unicodedata.normalize("NFKC", s)
        s = s.replace("台", "臺")
        return s.strip()

    ext_norm = _basic(ext_str)
    form_norm = _basic(form_str)

    if ext_norm == form_norm:
        return {
            "match": True,
            "extracted": ext_str,
            "form_value": form_str,
            "note": "完全一致",
        }
    if ext_norm in form_norm or form_norm in ext_norm:
        return {
            "match": True,
            "extracted": ext_str,
            "form_value": form_str,
            "note": "部分一致（包含關係）",
        }

    return {
        "match": False,
        "extracted": ext_str,
        "form_value": form_str,
        "note": "不一致",
    }


def _compare_addresses(extracted: Any, form_value: Any) -> dict:
    """Compare address values with normalization."""
    ext_list = [
        normalize_address(v).strip()
        for v in _to_address_list(extracted)
        if normalize_address(v).strip()
    ]
    form_list = [
        normalize_address(v).strip()
        for v in _to_address_list(form_value)
        if normalize_address(v).strip()
    ]

    ext_display = "\n".join(ext_list) if ext_list else "(null)"
    form_display = "\n".join(form_list) if form_list else "(null)"

    if not ext_list:
        return {
            "match": False,
            "extracted": ext_display,
            "form_value": form_display,
            "note": "附件中未找到此欄位",
        }
    if not form_list:
        return {
            "match": False,
            "extracted": ext_display,
            "form_value": form_display,
            "note": "表單欄位為空",
        }

    # Match: each form item must be found in extracted list
    matched = 0
    for fv in form_list:
        for ev in ext_list:
            if fv == ev or fv in ev or ev in fv:
                matched += 1
                break

    total = len(form_list)
    if matched == total:
        note = "完全一致（地址正規化後）"
    elif matched > 0:
        note = f"部分一致（{matched}/{total}項吻合）"
    else:
        note = "不一致"

    return {
        "match": matched == total,
        "extracted": ext_display,
        "form_value": form_display,
        "note": note,
    }


def _format_power_number(digits: str) -> str:
    """Format power number digits to XX-XX-XXXX-XX-X format."""
    if len(digits) == 11:
        return f"{digits[0:2]}-{digits[2:4]}-{digits[4:8]}-{digits[8:10]}-{digits[10]}"
    return digits


def compare_inverters(extracted_list: Any, record: dict, field_cfg: dict) -> list[dict]:
    """Compare inverter arrays (multiple model+quantity groups).

    Args:
        extracted_list: LLM-extracted inverter data (list of dicts).
        record: Full questionnaire record data.
        field_cfg: Field configuration with subtable/field ID info.

    Returns:
        List of comparison result dicts.
    """
    subtable_key = field_cfg.get("subtable", "")
    model_fid = field_cfg.get("model_field_id", "")
    qty_fid = field_cfg.get("quantity_field_id", "")

    # Extract form inverters from subtable
    form_inverters: list[dict[str, str]] = []
    sub_data = record.get(subtable_key, {})
    brand_fid = field_cfg.get("brand_field_id", "1014628")
    if sub_data and isinstance(sub_data, dict):
        for row in sub_data.values():
            if isinstance(row, dict):
                m = row.get(model_fid, "")
                q = row.get(qty_fid, "")
                b = row.get(brand_fid, "")
                if m or q:
                    form_inverters.append(
                        {"brand": str(b).strip(), "model": str(m).strip(), "quantity": str(q).strip()}
                    )

    # Extract LLM inverters
    llm_inverters: list[dict[str, str]] = []
    if isinstance(extracted_list, list):
        for item in extracted_list:
            if isinstance(item, dict):
                m = item.get("model") or ""
                q = item.get("quantity") or ""
                b = item.get("brand") or ""
                ev = item.get("evidence", "")
                if str(m).strip() and str(q).strip():
                    llm_inverters.append({
                        "brand": str(b).strip(),
                        "model": str(m).strip(),
                        "quantity": str(q).strip(),
                        "evidence": ev,
                    })

    def fmt(inv_list: list[dict[str, str]]) -> str:
        """Format as {brand}|{model}|{quantity}, joined by ', '"""
        parts = []
        for i in inv_list:
            brand = i.get("brand", "")
            model = i.get("model", "")
            qty = i.get("quantity", "")
            if brand:
                parts.append(f"{brand}|{model}|{qty}")
            else:
                parts.append(f"{model}|{qty}")
        return ", ".join(parts) if parts else "(無)"

    form_str = fmt(form_inverters)
    ext_str = fmt([{"model": i["model"], "quantity": i["quantity"]} for i in llm_inverters])
    evidence = "; ".join(
        i.get("evidence", "") for i in llm_inverters if i.get("evidence")
    )

    base: dict[str, Any] = {
        "extract_key": "inverters",
        "form_field_name": "變流器",
        "evidence": evidence,
    }

    if not llm_inverters and not form_inverters:
        return [{**base, "match": True, "extracted": None, "form_value": None, "note": "兩者皆為空"}]
    if not llm_inverters:
        return [{**base, "match": False, "extracted": None, "form_value": form_str, "note": "附件中未找到此欄位"}]
    if not form_inverters:
        return [{**base, "match": False, "extracted": ext_str, "form_value": None, "note": "表單欄位為空"}]

    def nq(q: str) -> str:
        try:
            return str(float(q)).rstrip("0").rstrip(".")
        except (ValueError, TypeError):
            return str(q).strip()

    matched = 0
    for fi in form_inverters:
        for li in llm_inverters:
            m_match = (
                fi["model"] == li["model"]
                or fi["model"] in li["model"]
                or li["model"] in fi["model"]
            )
            if m_match and nq(fi["quantity"]) == nq(li["quantity"]):
                matched += 1
                break

    total = len(form_inverters)
    if matched == total:
        note = "完全一致"
    elif matched > 0:
        note = f"部分一致（{matched}/{total}組吻合）"
    else:
        note = "不一致"

    return [{**base, "match": matched == total, "extracted": ext_str, "form_value": form_str, "note": note}]
