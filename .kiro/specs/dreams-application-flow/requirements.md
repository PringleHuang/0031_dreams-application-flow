# 需求文件

## 簡介

本系統為 DREAMS（太陽能管理系統）申請流程自動化工作流系統。目前整個申請流程仰賴人工電子郵件溝通與手動操作，本專案旨在透過 AWS 雲端服務（Lambda、API Gateway、Bedrock、SES）整合 RAGIC 表單平台與 DREAMS 系統，實現從客戶付款後的資料收集、台電審核、安裝階段到案件結案的全流程自動化。系統將運用 AI（AWS Bedrock）進行佐證文件比對判定與台電回覆語意分析，同時保留關鍵節點的人工確認機制，確保流程準確性。

## 詞彙表

- **工作流系統（Workflow_System）**：本專案開發的自動化流程系統，負責協調各階段的事件觸發、通知發送與流程推進
- **DREAMS**：既有的太陽能管理系統，用於管理電號選單、表單填寫及站點資料。僅在台電審核階段才寫入正確的申請資訊
- **SunVeillance**：既有的站點資訊系統，用於儲存已上線站點的相關資料
- **RAGIC**：表單與問卷平台，支援 Webhook 通知，用於客戶問卷填寫與案件管理
- **RAGIC 案件管理表單（Case_Management_Form）**：雲端 RAGIC 案件管理表單（https://ap13.ragic.com/solarcs/business-process2/2），作為案件狀態的唯一 source of truth，所有狀態變更與判定結果皆記錄於此
- **RAGIC 出貨管理表單（Shipment_Management_Form）**：公司內網 RAGIC 出貨管理資料表單（http://10.248.12.102/default/business-process/29），記錄訂單出貨狀態與料號資訊
- **公司聯絡人（Company_Contact）**：負責與客戶溝通、確認佐證文件的公司內部人員
- **客戶（Customer）**：申請太陽能設備安裝的付款客戶
- **台電業務聯絡人（Taipower_Business_Contact）**：台電端負責接收審核申請的業務人員
- **台電審核聯絡人（Taipower_Review_Contact）**：台電端負責審核申請與管理電號的人員
- **佐證文件（Supporting_Documents）**：客戶提供的 PDF 文件，用於證明問卷資料的正確性
- **問卷（Questionnaire）**：客戶透過 RAGIC 填寫的申請資料表單，首先詢問是否為續約案件以決定後續流程
- **補件問卷（Supplementary_Questionnaire）**：當資料不合格時，要求客戶補正的問卷，僅包含不合格項目
- **續約案件（Renewal_Case）**：既有客戶的合約續約，僅需提供電號即可，走簡化流程
- **新約案件（New_Contract_Case）**：全新申請案件，需填寫完整問卷資料與上傳 5 份佐證文件
- **電號（Electricity_Number）**：台電系統中用於識別用電戶的唯一編號
- **申請資料 PDF（Application_Data_PDF）**：DREAMS 系統產出的申請資料彙整文件
- **資料收集器（Data_Collector）**：客戶安裝於現場的資料收集硬體設備
- **自主檢查（Self_Regulation_Check）**：DREAMS 系統中的設備自主檢查流程
- **案件狀態（Case_Status）**：案件在流程中的當前階段標記
- **案件類型（Case_Type）**：區分案件為「續約」或「新約」，決定後續流程路徑
- **AI 判定服務（AI_Determination_Service）**：使用 AWS Bedrock 的 AI 服務，負責佐證文件比對與台電回覆語意分析
- **RPA 表單填寫（RPA_Form_Filling）**：DREAMS 系統中的自動化表單填寫流程
- **AWS API Gateway**：AWS 的 API 閘道服務，用於接收 RAGIC Webhook 與外部請求
- **AWS Lambda**：AWS 的無伺服器運算服務，用於執行 Python 3.12 業務邏輯
- **AWS SES**：AWS 的電子郵件發送服務
- **AWS Bedrock**：AWS 的生成式 AI 服務，用於文件比對與語意分析
- **出貨掃描服務（Shipment_Scanner）**：部署於公司內網電腦的常駐程式，負責定時掃描內網 RAGIC 出貨管理表單，並透過雲端 RAGIC API 建立新案件。獨立 repository：`ragic-shipment-scanner`
- **內網 RAGIC（Intranet_RAGIC）**：公司內網的 RAGIC 系統（http://10.248.12.102），僅能從公司內網存取
- **雲端 RAGIC（Cloud_RAGIC）**：公司雲端的 RAGIC 系統（https://ap13.ragic.com），可從外部網路存取，用於客戶問卷、案件管理與續約回寫

## 需求

### 需求 1：每日出貨掃描與案件自動建立

**使用者故事：** 身為公司聯絡人，我希望系統能每天自動掃描出貨管理資料，自動建立新案件並發送問卷通知，以免遺漏已出貨的訂單。

#### 驗收條件

1. THE Shipment_Scanner SHALL 每天固定時間掃描公司內網 RAGIC 出貨管理表單（http://10.248.12.102/default/business-process/29），查詢狀態為「已出貨」且料號為「19.D1M01.007」的訂單
2. WHEN Shipment_Scanner 發現符合條件的訂單，THE Shipment_Scanner SHALL 透過雲端 RAGIC API 在案件管理表單（https://ap13.ragic.com/solarcs/business-process2/2）建立新案件記錄，案件狀態設為「新開案件」
3. WHEN 雲端 RAGIC 新案件記錄建立完成，THE Cloud_RAGIC SHALL 透過 Webhook 通知 AWS API Gateway 觸發 Lambda 函式
4. WHEN Lambda 函式接收到新案件建立事件，THE Workflow_System SHALL 透過 AWS SES 發送問卷通知電子郵件給客戶（收件人從 payload 欄位 ID 取得，可設定），郵件主旨為「【DREAMS申辦】_{DREAMS_APPLY_ID}_請填寫問卷提供案場的建置資訊」，郵件內容包含雲端 RAGIC 問卷連結（僅帶入 DREAMS_APPLY_ID 作為 pfv 預填值）與個資聲明，同時抄送（CC）設定的靜態名單與對應案件的 RAGIC 表單 mail loop 地址
5. WHEN 問卷通知電子郵件發送完成，THE Workflow_System SHALL 將案件狀態更新為「待填問卷」
6. THE Shipment_Scanner SHALL 記錄每次掃描結果，包含掃描時間、發現的訂單數量與建立的案件數量

### 需求 2：問卷續約/新約分流與 AI 佐證文件判定

**使用者故事：** 身為公司聯絡人，我希望系統能區分續約與新約案件，續約案件走簡化流程，新約案件才需要完整的佐證文件判定。

#### 驗收條件

1. WHEN 客戶在 RAGIC 開啟問卷，THE 問卷 SHALL 首先詢問客戶是否為續約案件
2. IF 客戶選擇「續約」，THEN THE 問卷 SHALL 僅要求客戶填寫電號，不需填寫完整資訊與上傳佐證文件
3. IF 客戶選擇「非續約（新約）」，THEN THE 問卷 SHALL 要求客戶填寫完整申請資料並上傳 5 份佐證文件
4. WHEN 客戶在 RAGIC 完成問卷填寫，THE RAGIC 平台 SHALL 透過 Webhook 通知 AWS API Gateway 觸發 Lambda 函式
5. WHEN Lambda 函式被觸發且案件類型為「新約」，THE AI_Determination_Service SHALL 讀取客戶提交的佐證文件（共 5 份文件），並與問卷資料進行比對判定
6. WHEN Lambda 函式被觸發且案件類型為「續約」，THE Workflow_System SHALL 將案件狀態更新為「續約處理」，進入續約簡化流程（需求 16）
7. WHEN AI 判定完成（新約案件），THE Workflow_System SHALL 將判定結果（含各項目通過/不通過狀態與理由）寫入雲端 RAGIC 案件管理表單
8. WHEN AI 判定結果寫入完成，THE Workflow_System SHALL 等待公司聯絡人在 RAGIC 案件管理表單中手動變更案件狀態
9. WHEN 公司聯絡人在 RAGIC 手動變更案件狀態，THE Cloud_RAGIC SHALL 透過 Webhook 觸發對應的下一階段流程

### 需求 3：人工確認與狀態推進機制

**使用者故事：** 身為公司聯絡人，我希望在 RAGIC 上直接確認 AI 判定結果並推進流程，不需要額外的系統操作。

#### 驗收條件

1. WHEN AI 判定結果寫入 RAGIC 後，THE 公司聯絡人 SHALL 在 RAGIC 案件管理表單中檢視判定結果
2. WHEN 公司聯絡人確認資料合格，THE 公司聯絡人 SHALL 在 RAGIC 中將案件狀態手動變更為「台電審核」
3. WHEN 公司聯絡人判定資料不合格，THE 公司聯絡人 SHALL 在 RAGIC 中將案件狀態手動變更為「資訊補件」
4. WHEN RAGIC 案件狀態變更，THE Cloud_RAGIC SHALL 透過 Webhook 通知 AWS API Gateway 觸發對應的 Lambda 函式
5. WHEN Webhook 觸發且新狀態為「台電審核」，THE Workflow_System SHALL 進入台電站點申請流程（需求 5）
6. WHEN Webhook 觸發且新狀態為「資訊補件」，THE Workflow_System SHALL 發送補件通知給客戶（需求 4）

### 需求 4：資訊補件流程

**使用者故事：** 身為客戶，我希望在資料不合格時能收到明確的補件通知，以便我能針對不合格項目進行補正。

#### 驗收條件

1. WHEN 案件狀態由「待人工確認」變更為「資訊補件」，THE Workflow_System SHALL 讀取案件管理表單中「問卷結果」欄位，篩選值為 "Fail" 或 "Yes" 的項目
2. THE Workflow_System SHALL 將篩選出的項目對應的「補件參數對應」值（A~N）匯總為 `|` 分隔字串，以 pfv 參數帶入補件問卷（DREAMS案場-補單，work-survey/9）連結的「補件參數」多選欄位（1016697）。其中，併聯方式、併聯點型式、併聯點電壓三項為同一群組（代碼 L），任一項 Fail 則三項一起補件；責任分界點型式、責任分界點電壓兩項為同一群組（代碼 M），任一項 Fail 則兩項一起補件
3. THE Workflow_System SHALL 透過 AWS SES 發送補件通知電子郵件給客戶，郵件內容包含不合格項目說明與補件問卷連結（含 pfv 預填值：補件參數、出貨編號、DREAMS_APPLY_ID）
4. WHEN 客戶完成補件問卷填寫，THE Workflow_System SHALL 重新觸發 AI 佐證文件判定流程（回到需求 2 的流程）

### 需求 5：台電站點申請流程

**使用者故事：** 身為公司聯絡人，我希望系統能自動處理台電站點申請的前置作業，以減少手動操作 DREAMS 系統的時間。

#### 驗收條件

1. WHEN 案件進入台電審核階段（由 RAGIC Webhook 觸發），THE Workflow_System SHALL 呼叫 DREAMS 填單 API（位址由環境變數配置），傳入案件相關資料
2. IF DREAMS 填單 API 回應成功（含案號與申請資料 PDF base64），THEN THE Workflow_System SHALL 透過 AWS SES 發送審核申請電子郵件給台電業務聯絡人，郵件附件包含所有佐證文件與申請資料 PDF
3. IF DREAMS 填單 API 回應「無電號」，THEN THE Workflow_System SHALL 發送電號建立請求通知給台電審核聯絡人
4. THE Workflow_System SHALL 將 DREAMS 填單 API 回傳的案號寫入 RAGIC 案件管理表單

### 需求 6：台電審核回覆處理

**使用者故事：** 身為公司聯絡人，我希望系統能自動判讀台電的審核回覆，核准時自動推進流程，駁回時等待人工處理。

#### 驗收條件

1. WHEN 台電業務聯絡人回覆審核結果電子郵件，THE AWS SES SHALL 接收該郵件並觸發 Lambda 函式進行處理
2. WHEN Lambda 函式被觸發，THE AI_Determination_Service SHALL 對台電回覆郵件內容進行語意分析，判定審核結果為「核准」或「駁回」
3. WHEN AI 判定審核結果完成，THE Workflow_System SHALL 將語意分析結果（含各欄位 Pass/Fail）寫入 RAGIC 案件管理表單，並將案件狀態更新為「發送前人工確認」
4. WHEN 公司聯絡人在 RAGIC 中確認台電審核結果為核准（將狀態改為「安裝階段」），THE Cloud_RAGIC SHALL 透過 Webhook 觸發安裝階段流程（需求 8）
5. WHEN 公司聯絡人在 RAGIC 中確認台電審核結果為駁回（將狀態改為「台電補件」），THE Workflow_System SHALL 讀取「台電審核結果」欄位中值為 "Fail" 或 "Yes" 的項目，將對應的「補件參數對應」值（A~N，含群組邏輯）匯總為 `|` 分隔字串，以 pfv 參數帶入補件問卷連結的「補件參數」多選欄位，並發送台電補件通知給客戶

### 需求 7：台電駁回補件流程

**使用者故事：** 身為公司聯絡人，我希望在台電駁回時能快速啟動補件流程，以縮短重新申請的時間。

#### 驗收條件

1. WHEN 公司聯絡人在「發送前人工確認」狀態將案件改為「台電補件」，THE Cloud_RAGIC SHALL 透過 Webhook 觸發台電補件流程
2. WHEN 案件狀態變更為「台電補件」，THE Workflow_System SHALL 讀取「台電審核結果」欄位中值為 "Fail" 或 "Yes" 的項目，將對應的「補件參數對應」值（A~N，含群組邏輯）匯總為 `|` 分隔字串
3. THE Workflow_System SHALL 透過 AWS SES 發送台電補件通知電子郵件給客戶，郵件內容包含駁回原因說明與補件問卷連結（含 pfv 預填值：補件參數、出貨編號、DREAMS_APPLY_ID）
4. WHEN 客戶完成台電補件問卷填寫，THE Workflow_System SHALL 重新進入台電站點申請流程（回到需求 5 的流程）

### 需求 8：安裝階段通知與自主檢查

**使用者故事：** 身為客戶，我希望在台電核准後能收到明確的安裝指引，以便我能順利完成設備安裝與上線。

#### 驗收條件

1. WHEN 公司聯絡人在「發送前人工確認」狀態確認核准（將狀態改為「安裝階段」），THE Cloud_RAGIC SHALL 透過 Webhook 觸發安裝階段流程
2. WHEN 案件狀態變更為「安裝階段」，THE Workflow_System SHALL 透過 AWS SES 發送核准通知與自主檢查清單電子郵件給客戶
3. WHEN 客戶完成資料收集器安裝並聯繫客服，THE Workflow_System SHALL 協助執行 DREAMS 系統的自主檢查流程
4. WHEN 自主檢查通過，THE Workflow_System SHALL 執行 DREAMS 系統上線程序
5. IF 自主檢查未通過，THEN THE Workflow_System SHALL 通知客戶問題項目，待客戶解決後重新執行自主檢查

### 需求 9：案件結案與資料同步（新約案件）

**使用者故事：** 身為公司聯絡人，我希望新約案件完成上線後能自動將站點資料同步至 SunVeillance 系統，以確保站點資訊的完整性。

#### 驗收條件

1. WHEN DREAMS 系統上線程序完成，THE Workflow_System SHALL 將案件狀態更新為「完成上線」
2. WHEN 案件狀態變更為「完成上線」，THE Workflow_System SHALL 將 DREAMS 系統中的線上站點資料寫入 SunVeillance 站點資訊系統
3. WHEN 資料同步完成，THE Workflow_System SHALL 發送帳號啟用通知電子郵件給客戶
4. WHEN 帳號啟用通知發送完成，THE Workflow_System SHALL 將 DREAMS 到期資訊寫入系統並標記案件為已結案

### 需求 10：案件狀態管理

**使用者故事：** 身為公司聯絡人，我希望能隨時掌握每個案件的當前狀態，以便有效管理所有進行中的案件。

#### 驗收條件

1. THE Workflow_System SHALL 以雲端 RAGIC 案件管理表單（https://ap13.ragic.com/solarcs/business-process2/2）作為案件狀態的唯一 source of truth
2. THE Workflow_System SHALL 維護以下案件狀態：「新開案件」、「待填問卷」、「待人工確認」、「資訊補件」、「台電審核」、「發送前人工確認」、「台電補件」、「安裝階段」、「完成上線」、「已結案」、「續約處理」
3. THE Workflow_System SHALL 維護以下案件類型：「新約」、「續約」
4. WHEN 案件狀態發生變更（無論由系統自動或人工手動），THE Cloud_RAGIC SHALL 透過 Webhook 通知 AWS API Gateway 觸發對應的流程
5. THE Workflow_System SHALL 確保案件狀態轉換僅遵循定義的合法轉換路徑

### 需求 11：RAGIC Webhook 整合

**使用者故事：** 身為系統管理員，我希望 RAGIC 表單事件能可靠地觸發工作流程，以確保流程的即時性與連貫性。

#### 驗收條件

1. WHEN RAGIC 發送 Webhook 請求至 AWS API Gateway，THE API Gateway SHALL 驗證請求來源並將請求轉發至對應的 Lambda 函式
2. THE Workflow_System SHALL 根據 Webhook payload 中的 `path` 和 `sheetIndex` 進行第一層事件分類：
   - `business-process2/2`：根據 `1015456`（案件狀態）欄位值區分「新案件建立」或「案件狀態變更」
   - `work-survey/7`：分類為「問卷回覆」（新約/續約的區分由下游 Lambda 查詢案件管理表決定）
   - `work-survey/9`：分類為「補件問卷回覆」（資訊補件/台電補件的區分由下游 Lambda 查詢案件管理表的 `1015456` 決定）
3. WHEN 事件來自 `work-survey/7` 或 `work-survey/9`，THE 下游 Lambda SHALL 從 payload 的 DREAMS_APPLY_ID 欄位以 "-" split 取最後一段作為案件 ragicId，再呼叫 RAGIC API 查詢 `business-process2/2/{ragicId}` 取得案件狀態與類型
4. IF Webhook 請求驗證失敗，THEN THE API Gateway SHALL 回傳 HTTP 401 錯誤碼並記錄異常事件
5. THE Workflow_System SHALL 對同一 case_id + event_type 在 60 秒內僅處理一次，避免 RAGIC 重複觸發造成重複操作

### 需求 12：電子郵件通知服務

**使用者故事：** 身為系統管理員，我希望所有電子郵件通知能透過統一的服務發送，以便管理郵件範本與發送紀錄。

#### 驗收條件

1. THE Workflow_System SHALL 透過 AWS SES 發送所有流程相關的電子郵件通知
2. THE Workflow_System SHALL 從 Webhook payload 中以可設定的欄位 ID（預設 1000005）取得收件人電子郵件地址，欄位 ID 定義於 `email_config.yaml` 的 `payload_field_ids.customer_email`
3. THE Workflow_System SHALL 支援抄送（CC）功能，CC 名單由靜態名單（定義於 `email_config.yaml`）與動態產生的 RAGIC 表單 mail loop 地址（格式：`{account_id}.{tab_name}.{sheet_id}.{record_id}@tickets.ragic.com`）組成
4. WHEN 電子郵件發送完成，THE Workflow_System SHALL 記錄發送時間、收件人與郵件類型
5. IF 電子郵件發送失敗，THEN THE Workflow_System SHALL 記錄失敗原因並在指定間隔後重試發送，重試次數上限為 3 次
6. THE Workflow_System SHALL 支援可設定的郵件主旨範本，主旨中可引用 payload 欄位值（如 DREAMS_APPLY_ID），所有 payload 欄位 ID 定義於 `email_config.yaml` 的 `payload_field_ids`，RAGIC 表單設計變更時僅需修改配置檔

### 需求 13：AI 佐證文件比對服務

**使用者故事：** 身為公司聯絡人，我希望 AI 能準確比對佐證文件與問卷資料，以提升資料審核的效率與一致性。

#### 驗收條件

1. WHEN AI_Determination_Service 接收到佐證文件與問卷資料，THE AI_Determination_Service SHALL 逐項比對 5 份佐證文件與對應的問卷欄位
2. THE AI_Determination_Service SHALL 針對每份佐證文件產出「通過」或「不通過」的判定結果，並附上判定理由
3. THE AI_Determination_Service SHALL 使用 AWS Bedrock 作為 AI 推論引擎
4. WHEN AI_Determination_Service 完成判定，THE AI_Determination_Service SHALL 回傳結構化的判定結果，包含各項目的判定狀態與理由

### 需求 14：台電回覆語意分析服務

**使用者故事：** 身為公司聯絡人，我希望 AI 能自動判讀台電的回覆郵件，以減少人工閱讀與判斷的時間。

#### 驗收條件

1. WHEN AI_Determination_Service 接收到台電回覆郵件內容，THE AI_Determination_Service SHALL 進行語意分析以判定審核結果
2. THE AI_Determination_Service SHALL 將語意分析結果分類為「核准」或「駁回」
3. WHEN 語意分析結果為「駁回」，THE AI_Determination_Service SHALL 擷取並摘要駁回原因
4. THE AI_Determination_Service SHALL 使用 AWS Bedrock 作為語意分析引擎
5. THE AI_Determination_Service SHALL 回傳結構化的分析結果，包含判定類別、信心分數與原因摘要

### 需求 15：錯誤處理與日誌記錄

**使用者故事：** 身為系統管理員，我希望系統能妥善處理異常狀況並記錄完整日誌，以便問題排查與系統監控。

#### 驗收條件

1. IF Lambda 函式執行過程中發生未預期錯誤，THEN THE Workflow_System SHALL 記錄錯誤詳情至 AWS CloudWatch Logs 並通知系統管理員
2. IF 與 DREAMS 填單 API 的通訊失敗，THEN THE Workflow_System SHALL 記錄連線錯誤並在指定間隔後重試，重試次數上限為 3 次
3. IF 與 RAGIC 平台的通訊失敗，THEN THE Workflow_System SHALL 記錄通訊錯誤並在指定間隔後重試，重試次數上限為 3 次
4. THE Workflow_System SHALL 為每個案件的每次流程操作記錄操作日誌，包含時間戳記、操作類型與執行結果

### 需求 16：續約案件簡化流程

**使用者故事：** 身為續約客戶，我希望能透過簡化流程快速完成續約，不需要重新提交完整的申請資料。

#### 驗收條件

1. WHEN 案件類型為「續約」且客戶已填寫電號，THE Workflow_System SHALL 將案件狀態更新為「續約處理」
2. WHEN 案件狀態為「續約處理」，THE Workflow_System SHALL 提供 SunVeillance 系統登入資訊給客戶，引導客戶選擇續約案場
3. WHEN 客戶在 SunVeillance 完成續約案場選擇，THE Workflow_System SHALL 將續約結果回寫至雲端 RAGIC 案件管理表單（https://ap13.ragic.com/solarcs/business-process2/2）
4. WHEN 回寫完成，THE Workflow_System SHALL 將案件狀態更新為「已結案」並記錄結案原因為「續約完成」

### 需求 17：出貨掃描服務

**使用者故事：** 身為系統管理員，我希望有一個部署於公司內網的出貨掃描服務，能定時掃描內網出貨資料並自動在雲端 RAGIC 建立案件，以實現內網與雲端的資料串接。

#### 驗收條件

1. THE Shipment_Scanner SHALL 以常駐程式形式部署於公司內網電腦，使用 Python 3.12 開發
2. THE Shipment_Scanner SHALL 每天固定時間（可設定）自動執行出貨掃描任務
3. WHEN Shipment_Scanner 執行掃描任務，THE Shipment_Scanner SHALL 透過 HTTP 存取內網 RAGIC 出貨管理表單（http://10.248.12.102/default/business-process/29），查詢狀態為「已出貨」且料號為「19.D1M01.007」的訂單
4. WHEN 發現符合條件的訂單，THE Shipment_Scanner SHALL 透過雲端 RAGIC API（https://ap13.ragic.com）在案件管理表單建立新案件記錄
5. THE Shipment_Scanner SHALL 僅需要 outbound 網路連線（內網 RAGIC HTTP + 雲端 RAGIC HTTPS），不需要開放 inbound 連線
6. IF Shipment_Scanner 與內網 RAGIC 通訊失敗，THEN THE Shipment_Scanner SHALL 記錄錯誤至本地日誌並在指定間隔後重試，重試次數上限為 3 次
7. IF Shipment_Scanner 與雲端 RAGIC API 通訊失敗，THEN THE Shipment_Scanner SHALL 記錄錯誤至本地日誌並在指定間隔後重試，重試次數上限為 3 次
8. THE Shipment_Scanner SHALL 記錄每次任務執行的日誌，包含任務類型、執行時間與執行結果
