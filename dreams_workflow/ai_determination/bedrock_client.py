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
            desc += '\n  回傳陣列格式：[{"brand": "廠牌", "model": "型號", "quantity": "數量", "evidence": "依據"}, ...]'
            desc += "\n  列出文件中所有變流器（逆變器），每組必須同時包含廠牌、型號和數量"
            desc += "\n  數量判定方式（依優先順序）："
            desc += "\n    1. 文件中直接寫明數量（如「x8台」「共8台」）"
            desc += "\n    2. 從 INV 編號推算（如 INV#1~INV#8 表示 8 台，INV#9~INV#10 表示 2 台）"
            desc += "\n    3. 從系統單線圖中計算同型號的 INV 數量"
            desc += "\n  若以上方式都無法確定數量，該組不要輸出（不可假設數量為1）"
            desc += "\n  數量只回傳數字，去除小數尾零"
            desc += "\n  注意：變流器規格表中可能列出多種型號，每種型號的數量可能不同"
            desc += "\n  廠牌常見值：HUAWEI, PrimeVOLT, SolarEdge, DELTA, Sungrow 等"
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
4. 嚴禁猜測：只提取文件中明確寫出的資訊。如果文件中沒有明確提到某個欄位的資訊，該欄位值必須設為 null。不要從上下文推測、不要根據文件類型猜測、不要填入預設值。寧可回傳 null 也不要猜錯。
5. 案場類型（site_type）判讀規則：
   - 必須根據文件中明確記載的分類文字判斷，例如文件中直接寫「屋頂型」「地面型」「水面型」等字樣。
   - 嚴禁推測：不可從設置地點、地址類型、地號、建物有無等任何線索推測案場類型。寫地號不代表沒有建築，設在屋頂不代表是屋頂型。
   - 台灣再生能源設備分類是依「裝置容量」與「設備屬性」，而非設置地點。第三型（未達 2,000 瓩）可設置在屋頂、地面、水面等任何地點。
   - 如果文件中沒有直接寫出「屋頂型」「地面型」「水面型」等明確分類名稱，請回傳 null。
   - evidence 中也不可出現推理過程（如「地址為地號所以判斷為地面型」），只引用文件原文。
6. 併聯點與責任分界點是不同的資訊，不可混淆：
   - 併聯點（connection_method + connection_voltage_type + connection_voltage_volt）必須來自文件中明確標示為「併聯點」的同一處資訊，三項必須一致。不可將責任分界點的資料填入併聯點欄位。如果文件中只有責任分界點而沒有併聯點，三個欄位都填 null。
   - 責任分界點（demarcation_voltage_type + demarcation_voltage_volt）必須來自文件中明確標示為「責任分界點」的同一處資訊。不可將併聯點的資料填入責任分界點欄位。如果文件中只有併聯點而沒有責任分界點，兩個欄位都填 null。
   - 併聯點和責任分界點的型式和電壓通常不同，如果兩者完全相同請再次確認是否讀取正確。
7. 售電方式（selling_method）判讀規則：
   - 若文件中出現「無需與經營電力網之電業簽約躉售電能」或「得免與經營電力網之電業簽約」等類似文字，應判定為「僅併聯不躉售」。

格式正規化指引：
- 全形符號統一為半形（如「－」→「-」、「１」→「1」）
- 去除多餘空白
- 地名：「台」統一為「臺」
- 地號格式：{{縣市}}{{鄉鎮市區}}{{段名}}段{{小段名}}小段{{地號1}}、{{地號2}}、...地號
  - 段與小段前用中文數字（如「2小段」→「二小段」）
  - 母號補零至4碼（如 765 → 0765）
  - 有子號時子號補零至4碼（如 1153-11 → 1153-0011）
  - 無子號不加 -0000
  - 多筆地號以「、」分隔，並按數字排序
  - 「地號」二字只在最後出現一次
  - 行政區不可重複（錯誤：新竹市新竹市，正確：新竹市）
  - 移除「等」「等X筆」後綴
  - 若有多組不同段的地號，以換行分隔
- 地址格式：
  - 段前用中文數字（如「2段」→「二段」），路名不轉換（如「雙十路」保持原樣）
  - 只有夾在數字中的「之」統一為「-」（如「10之1」→「10-1」）
  - 移除村里名稱和鄰（如「成德村12鄰溪寮路」→「溪寮路」）
  - 移除括號備註（如「(屋頂)」移除）
  - 門牌合併（如「15號、18號」→「15、18號」）
  - 連續數字不可拆開：全形數字轉半形後，若數字之間沒有明確分隔符（、或,），應視為同一組數字（如「３６號」→「36號」，不可拆成「3、6號」）
  - 樓層統一為「X樓」（如「4F」→「4樓」、「四樓」→「4樓」）
- 數值欄位：只回傳數字，去除小數尾零（如 1248.390 → 1248.39）
- 電壓欄位（connection_voltage_volt、demarcation_voltage_volt）：
  - 從可選值中選擇最接近的
  - 【重要】雙電壓格式一律只取斜線前面（左邊）的電壓值：
    - 「3Ø3W 11.4/22.8kV」→ 取 11.4kV（不是 22.8kV）
    - 「11.4kV/22.8kV」→ 取 11.4kV
    - 「3Ø4W 220/380V」→ 取 380V（因 220V 和 380V 統一為 380V）
    - 「400/230V」→ 取 400V
    - 「400V/230V」→ 取 400V
    斜線後面的數字是線電壓/相電壓的另一個值，不是你要提取的電壓。
  - 220V 和 380V 統一輸出為 380V
- 電號格式：XX-XX-XXXX-XX-X 或 XXXXXXXXXX（全數字），不可將案號、備案編號誤認為電號

回覆格式：
- 每個欄位回傳一個物件，包含 value（提取值）和 evidence（文件中的原文依據）
- evidence 請引用文件中的原始文字，標明出處位置（如「第X頁：原文內容」）
- 如果該欄位值為 null，evidence 請填寫 "文件中未找到相關資訊"
- site_address 欄位：value 回傳陣列，每個地址/地號獨立一個元素（不同段的地號分開、地址和地號分開）

回覆範例：
{{"site_address": {{"value": ["臺南市東山區東安路一段100號"], "evidence": "第1頁：設置場所地址：臺南市東山區東安路一段100號"}}, "capacity_kwp": {{"value": "1087.275", "evidence": "第2頁：裝置容量 1,087.275 kWp"}}, "site_type": {{"value": null, "evidence": "文件中未找到相關資訊"}}, "inverters": [{{"model": "H3_210", "quantity": "10", "evidence": "第3頁：變流器型號 H3_210，數量 10台"}}]}}

site_address 回覆範例（地號）：
{{"site_address": {{"value": ["高雄市大寮區大寮段二小段1153、1153-0011地號", "高雄市岡山區新本洲段0024、0025地號"], "evidence": "第1頁：基地座落：..."}}}}
注意：每個 list 元素是一個完整的地號字串（含「地號」二字），不要在 list 外面再加「地號」"""


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

正規化規則（必須嚴格遵守每一條）：

【通用】
- 全形符號統一為半形（如「－」→「-」、「１」→「1」）
- 去除多餘空白
- 「台」統一為「臺」

【地號格式】
- 格式：{{縣市}}{{鄉鎮市區}}{{段名}}段{{小段名}}小段{{地號1}}、{{地號2}}、...地號
- 段與小段前用中文數字（如「2小段」→「二小段」）
- 母號補零至4碼（如 765 → 0765）
- 有子號時子號補零至4碼（如 1153-11 → 1153-0011）
- 無子號不加 -0000
- 多筆地號以「、」分隔，「地號」二字只在最後出現一次
- 行政區不可重複（錯誤：新竹市新竹市，正確：新竹市）
- 若有多組不同段的地號，必須分開保留各自的前綴，以換行分隔（不可合併成一組）
- 移除「等」「等X筆」後綴

【地址格式】
- 必須移除村里名稱（如「成德村」→ 移除、「建興里」→ 移除）
- 必須移除鄰（如「12鄰」→ 移除）
- 必須移除括號備註（如「(屋頂)」→ 移除）
- 只有夾在數字中的「之」統一為「-」（如「8之2號」→「8-2號」）
- 段前用中文數字（如「2段」→「二段」），路名不轉換（如「雙十路」保持原樣）
- 門牌合併（如「15號、18號」→「15、18號」）
- 連續數字不可拆開：全形數字轉半形後，若數字之間沒有明確分隔符（、或,），應視為同一組數字（如「３６號」→「36號」，不可拆成「3、6號」）
- 樓層統一為「X樓」（如「4F」→「4樓」、「四樓」→「4樓」）

【數值】
- 去除小數尾零（如 1248.390 → 1248.39）

【重要】保留原始語意，只做格式統一，不要改變內容。

範例：
- 「屏東縣萬巒鄉成德村12鄰溪寮路8之2號」→「屏東縣萬巒鄉溪寮路8-2號」
- 「高雄市大寮區大寮段二小段1153、1153-11地號(高雄市大寮區上發一路2號)」→「高雄市大寮區大寮段二小段1153、1153-0011地號」
- 「桃園市楊梅區民富路2段199巷15號、18號」→「桃園市楊梅區民富路二段199巷15、18號」
- 「高雄市路竹區北嶺段298、302-2地號、高雄市岡山區新本洲段24、25地號」→ ["高雄市路竹區北嶺段0298、0302-0002地號", "高雄市岡山區新本洲段0024、0025地號"]（不同段必須分開為 list 元素，不可合併）

回覆規則：
1. 用 JSON 格式回覆，key 使用指定的 key
2. 每個欄位回傳 {{"value": "正規化後的值"}}
3. site_address 欄位：value 回傳陣列，每個地址/地號獨立一個 list 元素，每個元素是完整字串（含「地號」二字），不要在 list 外面再加「地號」
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

        # Log response for debugging
        logger.info(
            "Bedrock response (first 500 chars): %s",
            text[:500],
            extra={"operation_type": "bedrock_response"},
        )

        # Check if response was truncated (stop_reason)
        stop_reason = result.get("stop_reason", "")
        if stop_reason == "max_tokens":
            logger.warning(
                "Bedrock response was TRUNCATED (max_tokens reached)",
                extra={"operation_type": "bedrock_truncated"},
            )

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
