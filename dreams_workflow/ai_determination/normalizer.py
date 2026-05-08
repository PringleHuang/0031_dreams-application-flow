"""Address, voltage, and value normalization utilities.

Provides normalization functions for comparing extracted document values
against questionnaire form values, including:
- Taiwan address/land number normalization
- Voltage value normalization
- Numeric value normalization
"""

from __future__ import annotations

import re
import unicodedata

# Number to Chinese character mapping for section/subsection names
NUM_TO_CN: dict[str, str] = {
    "1": "一", "2": "二", "3": "三", "4": "四", "5": "五",
    "6": "六", "7": "七", "8": "八", "9": "九", "10": "十",
}
CN_TO_NUM: dict[str, str] = {v: k for k, v in NUM_TO_CN.items()}


def normalize_address(addr: str) -> str:
    """Normalize a Taiwan address or land number string.

    Applies common normalization rules:
    - Unicode NFKC normalization
    - Full-width to half-width conversion
    - '台' → '臺' unification
    - Numeric section names to Chinese characters
    - Land number zero-padding and sorting
    - Street address cleanup (remove village/neighborhood, normalize floors)

    Args:
        addr: Raw address or land number string.

    Returns:
        Normalized address string.
    """
    if not addr:
        return ""
    result = addr.strip()

    # Common normalization
    result = unicodedata.normalize("NFKC", result)
    for ch in "\uff0d\u2010\u2011\u2012\u2013\u2014":
        result = result.replace(ch, "-")
    result = result.replace("台", "臺")
    result = re.sub(r"\s+", "", result)
    result = re.sub(r"(\d+)之(\d+)", r"\1-\2", result)

    # Convert numeric section/subsection names to Chinese
    for num, cn in NUM_TO_CN.items():
        result = re.sub(f"(?<!\\d){re.escape(num)}段", f"{cn}段", result)
        result = re.sub(f"(?<!\\d){re.escape(num)}小段", f"{cn}小段", result)

    if "地號" in result:
        return _normalize_land_number(result)
    return _normalize_street_address(result)


def _normalize_land_number(text: str) -> str:
    """Normalize land number format."""
    text = text.replace(",", "、").replace(";", "、")
    text = re.sub(r"地號([、,])", r"\1", text)
    text = re.sub(r"等[\d一二三四五六七八九十]*筆?", "", text)

    parts = re.split(r"地號[、,;]?", text)
    groups = []
    for part in parts:
        part = part.strip("、 ")
        if not part:
            continue
        groups.append(_normalize_single_land_group(part) + "地號")
    return "\n".join(groups) if groups else text


def _normalize_single_land_group(text: str) -> str:
    """Normalize a single land number group (same section prefix)."""
    match = re.match(r"(.+?段(?:.+?小段)?)(.*)", text)
    if not match:
        return text
    prefix, numbers_part = match.group(1), match.group(2)
    if not numbers_part:
        return text

    raw_numbers = re.split(r"[、,]", numbers_part)
    normalized = []
    for num_str in raw_numbers:
        num_str = re.sub(r"[^\d\-]", "", num_str.strip())
        if not num_str:
            continue
        if "-" in num_str:
            p = num_str.split("-", 1)
            mother, child = p[0].zfill(4), p[1].zfill(4)
            normalized.append(
                mother if child == "0000" else f"{mother}-{child}"
            )
        else:
            normalized.append(num_str.zfill(4))

    def sort_key(n: str) -> tuple[int, int]:
        p = n.split("-")
        return (int(p[0]), int(p[1]) if len(p) > 1 else -1)

    normalized.sort(key=sort_key)
    return prefix + "、".join(normalized)


def _normalize_street_address(text: str) -> str:
    """Normalize a street address."""
    # Remove parenthetical notes
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"（[^）]*）", "", text)
    # Remove village/neighborhood
    text = re.sub(r"[\u4e00-\u9fa5]{1,4}[村里]", "", text)
    text = re.sub(r"\d{1,3}鄰", "", text)
    # Normalize floor notation
    text = re.sub(r"(\d+)[Ff]", r"\1樓", text)
    for cn, num in CN_TO_NUM.items():
        text = text.replace(f"{cn}樓", f"{num}樓")
    # Merge house numbers
    text = re.sub(r"(\d+)號([、,])(\d+)號", r"\1\2\3號", text)
    text = re.sub(r"(\d+)號([、,])(\d+)號", r"\1\2\3號", text)
    return text


def normalize_voltage(value: str) -> float | None:
    """Normalize a voltage value to volts (V).

    Rules:
    - Dual voltage format (e.g. "11.4/22.8 kV") takes the first value
    - 220V and 380V are unified to 380V

    Args:
        value: Raw voltage string (e.g. "11.4kV", "380V", "11.4/22.8kV").

    Returns:
        Voltage in volts as float, or None if parsing fails.
    """
    if not value:
        return None
    value = unicodedata.normalize("NFKC", value).strip().upper()

    # Dual voltage format: take the first voltage
    dual_match = re.match(
        r"^([\d.]+)\s*(KV|V)?\s*/\s*[\d.]+\s*(KV|V)?$", value
    )
    if dual_match:
        num = float(dual_match.group(1))
        unit = dual_match.group(2) or dual_match.group(3) or "V"
        if unit == "KV":
            num *= 1000
        return _unify_low_voltage(num)

    match = re.match(r"^([\d.]+)\s*(KV|V)?$", value)
    if not match:
        return None
    num = float(match.group(1))
    if (match.group(2) or "V") == "KV":
        num *= 1000
    return _unify_low_voltage(num)


def _unify_low_voltage(volts: float) -> float:
    """Unify 220V and 380V to 380V."""
    if volts == 220.0 or volts == 380.0:
        return 380.0
    return volts


def format_voltage(volts: float) -> str:
    """Format voltage value for display (kV preferred for >= 1000V).

    Args:
        volts: Voltage in volts.

    Returns:
        Formatted string like "11.4kV" or "380V".
    """
    if volts >= 1000:
        kv = volts / 1000
        if kv == int(kv):
            return f"{int(kv)}kV"
        return f"{kv}kV"
    v = int(volts) if volts == int(volts) else volts
    return f"{v}V"
