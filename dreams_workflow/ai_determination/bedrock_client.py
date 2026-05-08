"""AWS Bedrock client for document extraction and form normalization.

Provides functions to invoke Bedrock Claude models for:
- Extracting structured data from supporting documents (PDF/images)
- Normalizing form values using LLM

Includes retry mechanism (max 2 attempts, 5s interval) via tenacity.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from tenacity import retry, stop_after_attempt, wait_fixed

from dreams_workflow.ai_determination.normalizer import normalize_voltage
from dreams_workflow.shared.logger import get_logger

logger = get_logger(__name__)

# Bedrock retry: max 2 attempts, 5 second interval
_BEDROCK_MAX_RETRIES = 2
_BEDROCK_WAIT_SECONDS = 5


class BedrockInvocationError(Exception):
    """Raised when Bedrock model invocation fails."""

    pass


def _retry_bedrock():
    """Create a retry decorator for Bedrock calls."""
    return retry(
        stop=stop_after_attempt(_BEDROCK_MAX_RETRIES),
        wait=wait_fixed(_BEDROCK_WAIT_SECONDS),
        retry=lambda retry_state: isinstance(
            retry_state.outcome.exception(), BedrockInvocationError
        ) if retry_state.outcome and retry_state.outcome.failed else False,
        reraise=True,
    )


def build_extract_prompt(attachment_cfg: dict, allowed_values: dict) -> str:
    """Build the LLM extraction prompt based on attachment configuration.

    Args:
        attachment_cfg: Attachment configuration with extract_fields.
        allowed_values: Dict of allowed values for constrained fields.

    Returns:
        Formatted prompt string for Bedrock Claude.
    """
    fields_desc = []
    for field in attachment_cfg["extract_fields"]:
        if field.get("type") == "inverter_array":
            desc = f'- {field["extract_key"]}: {field["description"]}'
            desc += '\n  回傳陣列格式：[{"model": "型號", "quantity": "數量", "evidence": "依據"}, ...]'
            desc += "\n  列出文件中所有變流器，每組必須同時包含型號和數量"
            desc += "\n  若文件中只提到型號但沒有明確數量，該組不要輸出（不可假設數量為1）"
            desc += "\n  數量只回傳數字，去除小數尾零"
            if "inverter_models" in allowed_values:
                models = allowed_values["inverter_models"]
                desc += f"\n  型號請從以下清單中選擇最接近的（共{len(models)}個）："
                desc += f'\n  {", ".join(models)}'
                desc += "\n  （如果文件中的型號不在清單中，請回傳文件中的原始型號）"
        else:
            desc = f'- {field["extract_key"]}: {field["description"]}'
        av_key = field.get("allowed_values_key")
        if av_key and av_key in allowed_values:
            av = allowed_values[av_key]
            if isinstance(av, list):
                desc += f'\n  可選值：{", ".join(av)}'
                desc += "\n  （請從可選值中選擇語意最接近的，不要照抄附件原文）"
            elif isinstance(av, str):
                desc += f"\n  {av}"
        fields_desc.append(desc)
    fields_text = "\n".join(fields_desc)

    return f"""你是一個文件資訊提取助手。請從附件中提取以下欄位資訊。

需要提取的欄位：
{fields_text}

回覆規則：
1. 用 JSON 格式回覆，key 使用指定的 extract_key
2. 只回覆 JSON，不要加任何說明文字
3. 有可選值的欄位，必須從可選值中選擇最接近的輸出，不要照抄附件原文
4. 嚴禁猜測：只提取文件中明確寫出的資訊。如果文件中沒有明確提到某個欄位的資訊，該欄位值必須設為 null。
5. 電壓欄位：雙電壓格式一律只取斜線前面（左邊）的電壓值
6. 電號格式：XX-XX-XXXX-XX-X 或 XXXXXXXXXX（全數字），不可將案號、備案編號誤認為電號

回覆格式：
- 每個欄位回傳一個物件，包含 value（提取值）和 evidence（文件中的原文依據）
- evidence 請引用文件中的原始文字
- 如果該欄位值為 null，evidence 請填寫 "文件中未找到相關資訊"
- site_address 欄位：value 回傳陣列"""


def build_form_normalize_prompt(fields_with_values: list[dict]) -> str:
    """Build the LLM prompt for form value normalization.

    Args:
        fields_with_values: List of dicts with key, value, description.

    Returns:
        Formatted prompt string.
    """
    fields_desc = []
    for item in fields_with_values:
        fields_desc.append(
            f'- {item["key"]}: 原始值=「{item["value"]}」，欄位說明={item["description"]}'
        )
    fields_text = "\n".join(fields_desc)

    return f"""你是一個資料正規化助手。請將以下表單欄位的原始值正規化為標準格式。

需要正規化的欄位：
{fields_text}

正規化規則：
- 全形符號統一為半形
- 去除多餘空白
- 「台」統一為「臺」
- 地號格式：母號補零至4碼，有子號時子號補零至4碼，多筆以「、」分隔
- 地址格式：移除村里名稱和鄰，段前用中文數字，門牌合併
- 數值：去除小數尾零

回覆規則：
1. 用 JSON 格式回覆，key 使用指定的 key
2. 每個欄位回傳 {{"value": "正規化後的值"}}
3. site_address 欄位：value 回傳陣列
4. 如果原始值為空，回傳 {{"value": null}}
5. 只回覆 JSON，不要加任何說明文字"""


@_retry_bedrock()
def invoke_bedrock_extract(
    client: Any,
    model_id: str,
    max_tokens: int,
    file_bytes: bytes,
    media_type: str,
    prompt: str,
) -> dict:
    """Invoke Bedrock Claude to extract structured data from a document.

    Args:
        client: boto3 bedrock-runtime client.
        model_id: Bedrock model ID.
        max_tokens: Maximum tokens for response.
        file_bytes: Document file bytes (PDF or image).
        media_type: MIME type of the file.
        prompt: Extraction prompt.

    Returns:
        Parsed JSON dict of extracted fields.

    Raises:
        BedrockInvocationError: On invocation failure (triggers retry).
    """
    file_b64 = base64.b64encode(file_bytes).decode("utf-8")

    if media_type == "application/pdf":
        content_block = {
            "type": "document",
            "source": {"type": "base64", "media_type": media_type, "data": file_b64},
        }
    else:
        content_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": file_b64},
        }

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [
            {"role": "user", "content": [content_block, {"type": "text", "text": prompt}]}
        ],
    }

    logger.info(
        "Invoking Bedrock Claude: model=%s, media_type=%s",
        model_id,
        media_type,
        extra={"operation_type": "bedrock_invoke"},
    )

    try:
        response = client.invoke_model(
            modelId=model_id,
            contentType="application/json",
            body=json.dumps(body),
        )
        result = json.loads(response["body"].read())
        text = result["content"][0]["text"]

        # Strip markdown code fences if present
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0].strip()

        return json.loads(text)
    except json.JSONDecodeError as e:
        raise BedrockInvocationError(
            f"Failed to parse Bedrock response as JSON: {e}"
        ) from e
    except Exception as e:
        raise BedrockInvocationError(
            f"Bedrock invocation failed: {e}"
        ) from e


@_retry_bedrock()
def invoke_bedrock_normalize(
    client: Any,
    model_id: str,
    fields_with_values: list[dict],
) -> dict[str, Any]:
    """Invoke Bedrock Claude to normalize form values.

    Args:
        client: boto3 bedrock-runtime client.
        model_id: Bedrock model ID.
        fields_with_values: List of field dicts with key, value, description.

    Returns:
        Dict mapping extract_key to normalized value.

    Raises:
        BedrockInvocationError: On invocation failure (triggers retry).
    """
    if not fields_with_values:
        return {}

    prompt = build_form_normalize_prompt(fields_with_values)
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    }

    try:
        response = client.invoke_model(
            modelId=model_id,
            contentType="application/json",
            body=json.dumps(body),
        )
        result = json.loads(response["body"].read())
        text = result["content"][0]["text"].strip()

        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0].strip()

        parsed = json.loads(text)
        normalized: dict[str, Any] = {}
        for key, val in parsed.items():
            normalized[key] = val.get("value") if isinstance(val, dict) else val
        return normalized
    except json.JSONDecodeError as e:
        raise BedrockInvocationError(
            f"Failed to parse normalization response as JSON: {e}"
        ) from e
    except Exception as e:
        raise BedrockInvocationError(
            f"Bedrock normalization invocation failed: {e}"
        ) from e


def detect_media_type(file_name: str, file_bytes: bytes) -> str:
    """Detect the MIME type of a file based on name and content.

    Args:
        file_name: Original file name.
        file_bytes: File content bytes.

    Returns:
        MIME type string.
    """
    name_lower = file_name.lower()
    if name_lower.endswith(".pdf"):
        return "application/pdf"
    if name_lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if name_lower.endswith(".png"):
        return "image/png"
    if name_lower.endswith(".gif"):
        return "image/gif"
    if name_lower.endswith(".webp"):
        return "image/webp"

    # Check magic bytes
    if file_bytes[:4] == b"%PDF":
        return "application/pdf"
    if file_bytes[:2] == b"\xff\xd8":
        return "image/jpeg"
    if file_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"

    # Default to PDF
    return "application/pdf"


def fix_dual_voltage(ext_value: str, evidence: str) -> str:
    """Fix dual voltage extraction errors using evidence text.

    When evidence contains a dual voltage format (e.g. '11.4/22.8kV'),
    ensures the extracted value is the first (left) voltage.

    Args:
        ext_value: LLM-extracted voltage value.
        evidence: Evidence text from the document.

    Returns:
        Corrected voltage value string.
    """
    patterns = [
        r"([\d.]+)\s*(kV|KV|v|V)?\s*/\s*([\d.]+)\s*(kV|KV|v|V)",
        r"([\d.]+)\s*/\s*([\d.]+)\s*(kV|KV|v|V)",
    ]
    for pat in patterns:
        m = re.search(pat, evidence, re.IGNORECASE)
        if m:
            groups = m.groups()
            first_num = groups[0]
            if len(groups) == 4:
                first_unit = groups[1] or groups[3]
            else:
                first_unit = groups[2]

            first_unit = first_unit.upper() if first_unit else "V"
            if first_unit == "KV":
                corrected = f"{first_num}kV"
            else:
                corrected = f"{first_num}V"

            ext_v = normalize_voltage(ext_value)
            corrected_v = normalize_voltage(corrected)
            if ext_v is not None and corrected_v is not None and ext_v != corrected_v:
                return corrected
            break
    return ext_value
