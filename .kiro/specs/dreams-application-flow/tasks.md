# 實作計畫：DREAMS 申請流程自動化工作流系統

## 概述

建構完整的事件驅動工作流系統。實作採用 Python 3.12、AWS Lambda、API Gateway、Bedrock、SES 架構，以 RAGIC 為案件狀態唯一 source of truth。

## Tasks

- [x] 1. 建立專案結構與核心模組
  - [x] 1.1 建立新專案目錄結構與共用模組
    - 建立 `dreams_workflow/` 專案根目錄
    - 建立子目錄：`shared/`（共用模組）、`webhook_handler/`、`ai_determination/`、`workflow_engine/`、`email_service/`、`mail_receiver/`、`dreams_client/`
    - 建立 `shared/models.py` 定義 CaseStatus、CaseType、WebhookEventType 等列舉與資料模型（CaseRecord、AIJudgmentRecord、EmailLog）
    - 建立 `shared/exceptions.py` 定義 InvalidTransitionError、ExternalServiceError、DreamsConnectionError、RagicCommunicationError、EmailSendError
    - 建立 `shared/retry_config.py` 定義 RetryConfig 常數與 tenacity 重試裝飾器
    - 建立 `shared/logger.py` 定義統一日誌格式（含案件編號、操作類型、時間戳記）
    - 建立 `requirements.txt` 與各 Lambda 函式的 `requirements.txt`
    - _Requirements: 10.2, 10.3, 15.4_

  - [x] 1.2 實作狀態機轉換邏輯
    - 在 `shared/state_machine.py` 中實作 VALID_TRANSITIONS 定義
    - 實作 `validate_transition(current: CaseStatus, target: CaseStatus) -> bool` 函式
    - 實作 `transition_case_status(case_id, new_status, reason)` 函式，包含合法性驗證與日誌記錄
    - 不合法轉換拋出 InvalidTransitionError
    - _Requirements: 10.3, 10.4_

  - [x] 1.3 撰寫狀態機屬性測試
    - **Property 1: 狀態機轉換合法性**
    - **Validates: Requirements 10.3**
    - 使用 hypothesis 生成隨機 (current_status, target_status) 組合
    - 驗證：合法路徑中的轉換成功、非合法路徑的轉換拋出 InvalidTransitionError

  - [x] 1.4 撰寫狀態機單元測試
    - 測試所有 10 個合法轉換路徑的正向案例
    - 測試非法轉換的拒絕行為
    - 測試邊界條件（相同狀態轉換、None 值）
    - _Requirements: 10.3_

- [x] 2. 實作 RAGIC 整合介面（ragic_client）
  - [x] 2.1 實作 CloudRagicClient 類別
    - 在 `shared/ragic_client.py` 中實作 CloudRagicClient
    - 依據 design.md 介面規格實作附件下載邏輯（RAGIC file.jsp API + Authorization: Basic {key}）
    - 實作 `get_questionnaire_data(record_id)` 方法
    - 實作 `get_supporting_documents(record_id)` 方法（回傳 list[tuple[str, bytes]]）
    - 實作 `write_determination_result(case_id, result)` 方法，將 AI 判定結果寫入 RAGIC
    - 實作 `update_case_status(case_id, status)` 方法
    - 實作 `create_supplement_questionnaire(case_id, failed_items)` 方法
    - 實作 `update_case_record(case_id, update_data)` 方法
    - 所有方法加入 tenacity 重試裝飾器（最多 3 次，間隔 5 秒）
    - _Requirements: 2.7, 10.1, 15.3_

  - [x] 2.2 撰寫 CloudRagicClient 單元測試
    - 使用 unittest.mock 模擬 HTTP 回應
    - 測試成功與失敗場景
    - 測試重試機制觸發
    - _Requirements: 15.3_

- [x] 3. 實作 Webhook 事件處理器（webhook_handler）
  - [x] 3.1 實作 Webhook 接收與事件分類
    - 在 `webhook_handler/app.py` 中實作 lambda_handler
    - 依據 design.md 介面規格實作 Webhook 接收邏輯（API Gateway event 解析 + HMAC 驗證）
    - 實作 `validate_webhook_source(headers, body)` 驗證請求來源
    - 實作 `classify_webhook_event(payload)` 根據表單 ID 與欄位內容分類為 5 種事件類型：NEW_CASE_CREATED（狀態欄位為「新開案件」）、CASE_STATUS_CHANGED、RENEWAL_QUESTIONNAIRE、NEW_CONTRACT_FULL_QUESTIONNAIRE、SUPPLEMENTARY_QUESTIONNAIRE
    - 根據事件類型非同步呼叫對應的 Lambda 函式（ai_determination 或 workflow_engine）
    - 驗證失敗回傳 HTTP 401
    - _Requirements: 11.1, 11.2, 11.3_

  - [x] 3.2 撰寫 Webhook 事件分類屬性測試
    - **Property 6: Webhook 事件分類正確性**
    - **Validates: Requirements 11.2**
    - 使用 hypothesis 生成合法 payload 組合
    - 驗證：相同 payload 永遠產生相同分類結果（確定性）

  - [x] 3.3 撰寫 Webhook 處理器單元測試
    - 測試 5 種事件類型的正確分類
    - 測試驗證失敗回傳 401
    - 測試 payload 解析（JSON、Base64）
    - _Requirements: 11.1, 11.2, 11.3_

- [x] 4. Checkpoint - 確認核心模組與 Webhook 處理正常
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. 實作 AI 判定服務（ai_determination）
  - [x] 5.1 重構佐證文件比對邏輯
    - 在 `ai_determination/app.py` 中實作 lambda_handler
    - 依據 design.md 介面規格與 ai_determination/config.py 中的欄位定義實作文件比對邏輯
    - 實作 `compare_documents(questionnaire_data, supporting_documents, document_metadata) -> ComparisonReport`
    - 確保回傳的 ComparisonReport 包含恰好 5 筆 DocumentComparisonResult
    - 每筆結果包含 document_id、document_name、status（pass/fail）、reason（非空字串）
    - 加入 Bedrock 重試機制（最多 2 次，間隔 5 秒）
    - _Requirements: 13.1, 13.2, 13.3, 13.4_

  - [x] 5.2 撰寫佐證文件比對結果結構屬性測試
    - **Property 3: 佐證文件比對結果結構完整性**
    - **Validates: Requirements 13.1, 13.2, 13.4**
    - 使用 hypothesis 生成隨機文件與問卷資料
    - 驗證：回傳恰好 5 筆結果、status 為 pass/fail、reason 非空

  - [x] 5.3 實作台電回覆語意分析
    - 在 `ai_determination/semantic_analyzer.py` 中實作 `analyze_taipower_reply(email_content, email_subject) -> SemanticAnalysisResult`
    - SemanticAnalysisResult 包含 category（approved/rejected）、confidence_score（0.0~1.0）、rejection_reason_summary
    - 當 category 為 rejected 時，rejection_reason_summary 為非空字串
    - 使用 Bedrock 進行語意分析，加入重試機制
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5_

  - [x] 5.4 撰寫台電回覆語意分析屬性測試
    - **Property 4: 台電回覆語意分析結果有效性**
    - **Validates: Requirements 14.1, 14.2, 14.3, 14.5**
    - 使用 hypothesis 生成隨機郵件內容
    - 驗證：category 為 approved/rejected、confidence_score 介於 0.0~1.0、rejected 時 rejection_reason_summary 非空

  - [x] 5.5 撰寫 AI 判定服務單元測試
    - 測試文件比對全通過場景
    - 測試文件比對有不合格場景
    - 測試語意分析核准/駁回場景
    - 測試 Bedrock 呼叫失敗重試
    - _Requirements: 13.1, 13.2, 14.1, 14.2_

- [x] 6. 實作郵件通知服務（email_service）
  - [x] 6.1 實作 SES 郵件發送與範本管理
    - 在 `email_service/app.py` 中實作 lambda_handler
    - 實作 `send_email(request: EmailRequest) -> EmailResult`
    - 支援 6 種郵件類型：問卷通知、補件通知、台電審核申請、台電補件通知、核准通知、帳號啟用通知
    - 實作郵件範本渲染（使用 Jinja2 或字串格式化）
    - 支援附件發送（佐證文件 + 申請資料 PDF）
    - 實作 `get_recipient_email(case_id)` 從 RAGIC 取得收件人
    - 加入 SES 重試機制（最多 3 次，間隔 30 秒）
    - _Requirements: 12.1, 12.2, 12.4_

  - [x] 6.2 實作郵件發送紀錄
    - 實作 EmailLog 紀錄建立邏輯
    - 每次發送（成功或失敗）皆建立 EmailLog 並存入 S3
    - 紀錄包含 case_id、email_type、recipient、sent_at、status、message_id、retry_count、error_message
    - _Requirements: 12.3_

  - [x] 6.3 撰寫郵件發送紀錄屬性測試
    - **Property 7: 郵件發送紀錄完整性**
    - **Validates: Requirements 12.3**
    - 使用 hypothesis 生成隨機郵件發送場景（成功/失敗）
    - 驗證：每次發送皆建立 EmailLog、成功時包含 sent_at 與 message_id

  - [x] 6.4 撰寫郵件服務單元測試
    - 使用 moto mock SES
    - 測試各類型郵件發送
    - 測試附件發送
    - 測試發送失敗重試
    - _Requirements: 12.1, 12.4_

- [x] 7. Checkpoint - 確認 AI 判定與郵件服務正常
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. 實作工作流引擎（workflow_engine）
  - [x] 8.1 實作新案件建立流程
    - 在 `workflow_engine/app.py` 中實作 lambda_handler，根據事件類型路由至對應處理函式
    - 實作 `handle_new_case(payload)` 處理 NEW_CASE_CREATED 事件（判斷依據：案件狀態欄位為「新開案件」）
    - 流程：發送問卷通知郵件（含 RAGIC 問卷連結與個資聲明）→ 更新案件狀態為「待填問卷」
    - 注意：案件已由出貨掃描服務（Task 13）在雲端 RAGIC 案件管理表單建立，此處僅處理後續通知與狀態更新
    - _Requirements: 1.5, 1.6_

  - [x] 8.2 實作問卷回覆處理與分流邏輯
    - 實作 `handle_questionnaire_response(payload)` 處理問卷回覆事件
    - 根據案件類型分流：新約 → 觸發 AI 判定、續約 → 更新狀態為「續約處理」
    - 新約案件完整流程（依 `ai_determination/field_mapping.yaml` 配置）：
      1. 收到 Webhook（含問卷資料，但缺附件），從 RAGIC 下載 5 份佐證文件附件
      2. 逐份佐證文件進行 AI 判定（審訖圖、細部協商、縣府同意備案函文、購售電契約、併聯審查意見書）
      3. 組合完整寫入資料：直接欄位值（direct_mapping）+ AI 判定值（llm_result_mapping）+ Pass/Fail（questionnaire_result_mapping）+ 狀態「待人工確認」
      4. 一次性將所有資料寫入案件管理表單（單次 RAGIC POST）
    - _Requirements: 2.4, 2.5, 2.6, 2.7, 2.8_

  - [x] 8.3 實作補件問卷產生邏輯
    - 實作 `handle_supplement_flow(payload)` 處理 CASE_STATUS_CHANGED 為「資訊補件」的事件
    - 從案件管理表單讀取「問卷結果」欄位，篩選值為 "Fail" 或 "Yes" 的項目
    - 將篩選出的項目對應的「補件參數對應」代碼（A~N，含群組邏輯：併聯方式/併聯點型式/併聯點電壓同組代碼 L，責任分界點型式/責任分界點電壓同組代碼 M）匯總為 `|` 分隔字串
    - 產生補件問卷連結，以 pfv 帶入：補件參數（多選欄位，pfv_1016697=A,B,F）、出貨編號、DREAMS_APPLY_ID
    - 發送補件通知郵件給客戶（含補件問卷連結）
    - _Requirements: 4.1, 4.2, 4.3_

  - [x] 8.4 撰寫補件問卷過濾屬性測試
    - **Property 5: 補件問卷僅包含不合格項目**
    - **Validates: Requirements 4.2**
    - 使用 hypothesis 生成隨機 ComparisonReport（含 pass/fail 組合）
    - 驗證：補件參數僅包含 fail 項目對應代碼（A~N）、不包含 pass 項目代碼、群組欄位去重後數量一致

  - [x] 8.5 實作案件狀態變更處理
    - 實作 `handle_status_change(payload)` 處理 CASE_STATUS_CHANGED 事件
    - 根據新狀態路由至對應流程：
      - 台電審核 → 需求 5 流程（呼叫 DREAMS 填單 API）
      - 資訊補件 → 需求 4 流程（讀取問卷結果 Fail/Yes → 匯總補件參數 → 發送補件通知）
      - 發送前人工確認 → 台電回覆語意分析結果寫入後等待人工確認
      - 台電補件 → 需求 7 流程（讀取台電審核結果 Fail/Yes → 匯總補件參數 → 發送台電補件通知）
      - 安裝階段 → 需求 8 流程
    - 從 Webhook payload 中的 DREAMS_APPLY_ID 以 "-" split 取第二段作為目標 RAGIC record ID
    - 驗證狀態轉換合法性（使用 state_machine 模組）
    - _Requirements: 3.4, 3.5, 3.6, 10.4_

  - [x] 8.6 撰寫狀態變更紀錄屬性測試
    - **Property 2: 狀態變更紀錄完整性**
    - **Validates: Requirements 10.4, 15.4**
    - 使用 hypothesis 生成隨機合法狀態轉換
    - 驗證：每次成功轉換皆記錄日誌（含案件編號、原始狀態、目標狀態、變更原因）

- [x] 9. 實作台電審核流程
  - [x] 9.1 實作 DREAMS 填單 API 客戶端（dreams_api_client）
    - 在 `dreams_workflow/dreams_client/client.py` 中實作 DreamsApiClient 類別
    - 實作 `DreamsApiResponse` dataclass（success、case_number、pdf_base64、error_code、error_message）
    - 實作 `submit_application(case_id, case_data) -> DreamsApiResponse` 方法
    - API URL 從環境變數 `DREAMS_API_URL` 讀取
    - 加入重試機制（最多 3 次，間隔 10 秒）
    - 判讀回應：成功（含案號+PDF）、無電號、其他錯誤
    - _Requirements: 5.1, 15.2_

  - [x] 9.2 實作台電站點申請流程
    - 在 `workflow_engine/taipower_flow.py` 中實作 `handle_taipower_review(case_id)`
    - 流程：呼叫 DREAMS 填單 API → 判讀回應
    - 成功：將案號寫入 RAGIC → 發送審核郵件（含佐證文件 + PDF 附件）給台電業務聯絡人
    - 無電號：發送電號建立請求通知給台電審核聯絡人
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [x] 9.3 實作台電補件流程
    - 在 `workflow_engine/taipower_flow.py` 中實作 `handle_taipower_supplement(case_id)`
    - 流程：根據駁回原因判定補正項目 → 發送台電補件通知給客戶 → 客戶補件後重新進入台電申請流程
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

  - [x] 9.4 撰寫台電審核流程單元測試
    - 測試 API 回應成功場景（含案號+PDF）
    - 測試 API 回應無電號場景
    - 測試 API 呼叫失敗重試
    - 測試審核郵件發送（含附件）
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [x] 10. 實作郵件接收處理器（mail_receiver）
  - [x] 10.1 實作 SES 郵件接收與解析
    - 在 `mail_receiver/app.py` 中實作 lambda_handler
    - 從 S3 讀取 SES 接收的原始郵件
    - 實作 `parse_email_content(raw_email)` 解析郵件主旨、本文、附件
    - 實作 `match_case_by_sender(sender_email, subject)` 比對寄件人與案件
    - 觸發 AI 語意分析 → 根據結果更新案件狀態
    - 核准：自動更新狀態為「安裝階段」
    - 駁回：將駁回原因寫入 RAGIC，等待人工觸發補件
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x] 10.2 撰寫郵件接收處理器單元測試
    - 使用 moto mock S3
    - 測試郵件解析（含附件/不含附件）
    - 測試寄件人比對邏輯
    - 測試核准/駁回後的狀態更新
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

- [x] 11. Checkpoint - 確認工作流引擎與台電審核流程正常
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. 實作安裝階段與結案流程
  - [x] 12.1 實作安裝階段通知與自主檢查
    - 在 `workflow_engine/installation_flow.py` 中實作 `handle_installation_phase(case_id)`
    - 流程：發送核准通知與自主檢查清單郵件 → 執行 DREAMS 自主檢查 → 通過則執行上線程序
    - 實作 DreamsClient 的 `execute_self_regulation_check(case_id)` 與 `execute_online_procedure(case_id)`
    - 自主檢查未通過時通知客戶問題項目
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

  - [x] 12.2 實作案件結案與資料同步（新約）
    - 在 `workflow_engine/closure_flow.py` 中實作 `handle_case_closure(case_id)`
    - 流程：更新狀態為「完成上線」→ 同步站點資料至 SunVeillance → 發送帳號啟用通知 → 寫入到期資訊 → 標記已結案
    - 實作 DreamsClient 的 `get_site_data(case_id)` 與 SunVeillance 資料寫入
    - _Requirements: 9.1, 9.2, 9.3, 9.4_

  - [x] 12.3 實作續約案件簡化流程
    - 在 `workflow_engine/renewal_flow.py` 中實作 `handle_renewal(case_id)`
    - 流程：提供 SunVeillance 登入資訊 → 客戶選擇續約案場 → 回寫 RAGIC → 更新狀態為「已結案」
    - _Requirements: 16.1, 16.2, 16.3, 16.4_

  - [x] 12.4 撰寫續約流程屬性測試
    - **Property 10: 續約案件流程完整性**
    - **Validates: Requirements 2.6, 16.1, 16.3, 16.4**
    - 使用 hypothesis 生成隨機續約案件資料
    - 驗證：續約案件不觸發 AI 判定、直接進入續約處理、完成後直接結案

  - [x] 12.5 撰寫安裝與結案流程單元測試
    - 測試自主檢查通過/未通過場景
    - 測試資料同步至 SunVeillance
    - 測試續約結案回寫
    - _Requirements: 8.3, 8.4, 9.2, 16.3_

- [x] 13. 【獨立專案】出貨掃描服務（ragic-shipment-scanner）
  - 此任務為獨立專案（獨立 repository：`ragic-shipment-scanner`），部署於公司內網電腦，已於獨立專案中完成實作。
  - 職責：定時掃描內網 RAGIC 出貨管理表單，將符合條件的訂單透過雲端 RAGIC API 建立新案件（狀態設為「新開案件」），觸發本專案的 Webhook 流程。
  - 相關需求：需求 1（出貨掃描）、需求 17（出貨掃描服務）

- [x] 14. 實作重試機制與操作日誌
  - [x] 14.1 實作統一重試裝飾器
    - 在 `shared/retry_config.py` 中使用 tenacity 實作各服務的重試裝飾器
    - RAGIC：最多 3 次，間隔 5 秒
    - DREAMS：最多 3 次，間隔 10 秒
    - SES：最多 3 次，間隔 30 秒
    - Bedrock：最多 2 次，間隔 5 秒
    - 每次重試記錄錯誤日誌，第 N 次失敗後標記最終失敗
    - _Requirements: 12.4, 15.2, 15.3_

  - [x] 14.2 撰寫重試機制屬性測試
    - **Property 8: 外部服務重試機制一致性**
    - **Validates: Requirements 12.4, 15.2, 15.3**
    - 使用 hypothesis 生成隨機失敗場景
    - 驗證：重試次數不超過上限、每次重試記錄日誌、最終失敗後不再重試

  - [x] 14.3 實作操作日誌記錄模組
    - 在 `shared/audit_logger.py` 中實作統一操作日誌
    - 每次流程操作記錄：時間戳記（ISO 8601）、操作類型、案件編號、執行結果
    - 日誌輸出至 CloudWatch Logs（Lambda）或本地檔案（Agent）
    - _Requirements: 15.4_

  - [x] 14.4 撰寫操作日誌屬性測試
    - **Property 9: 流程操作日誌完整性**
    - **Validates: Requirements 15.4**
    - 使用 hypothesis 生成隨機操作事件
    - 驗證：每次操作皆建立日誌、時間戳記為有效 ISO 8601 格式

- [x] 15. 整合測試與端對端驗證
  - [x] 15.1 實作整合測試
    - 使用 moto mock AWS 服務（SES、S3、Lambda、Bedrock）
    - 測試 Webhook → Lambda 觸發鏈路
    - 測試 SES 郵件接收 → Lambda 處理鏈路
    - 測試完整新約案件流程（從建立到結案）
    - 測試完整續約案件流程
    - _Requirements: 1.1~1.7, 2.1~2.9, 16.1~16.4_

  - [x] 15.2 建立 AWS SAM/CDK 部署配置
    - 建立 `template.yaml`（SAM）或 CDK 定義
    - 定義所有 Lambda 函式、API Gateway、SES 規則、S3 Bucket、IAM Role
    - 設定環境變數（RAGIC API Key、Bedrock Model ID 等）
    - 設定 CloudWatch Logs 保留策略
    - _Requirements: 全部_

- [x] 16. Final Checkpoint - 確認所有測試通過
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- 標記 `*` 的任務為選擇性任務，可跳過以加速 MVP 開發
- 每個任務皆參照具體需求編號，確保需求覆蓋完整
- Checkpoint 確保漸進式驗證
- 屬性測試驗證系統的通用正確性屬性（使用 hypothesis 框架）
- 單元測試驗證具體範例與邊界條件
- 原參考資料 `0031_CreateNewDreams/` 已移除，相關規格已記錄於 design.md 與 ai_determination/config.py
- **Task 13（出貨掃描服務）為獨立專案**，repository 名稱為 `ragic-shipment-scanner`，部署於公司內網電腦，不在本 AWS 專案中實作
- 本專案（AWS 部分）與出貨掃描服務的介面為雲端 RAGIC Webhook — Scanner 寫入案件 → RAGIC 觸發 Webhook → 本專案接手處理
