---
inclusion: auto
---

# AWS CLI 使用規則

本專案使用 `awslogin` 工具管理 AWS session。在 awslogin 開啟的子 shell 中，credentials 透過環境變數注入。

## 重要規則

- **所有 AWS CLI 指令不加 `--profile` 參數**
- **所有 SAM CLI 指令不加 `--profile` 參數**
- 錯誤示範：`aws logs filter-log-events --profile greenplatform ...`
- 正確示範：`aws logs filter-log-events ...`
- 錯誤示範：`sam deploy --profile greenplatform ...`
- 正確示範：`sam deploy ...`

## 部署指令

```powershell
# Build
sam build

# Deploy to dev (default in samconfig.toml)
sam deploy --no-confirm-changeset --region ap-northeast-1

# Deploy to prod (需手動確認)
sam deploy --config-env prod --region ap-northeast-1
```

## 查詢 CloudWatch Logs

```powershell
# Webhook handler
aws logs filter-log-events --log-group-name "/aws/lambda/dreams-workflow-dev-webhook-handler" --start-time $startTime --region ap-northeast-1

# AI determination
aws logs filter-log-events --log-group-name "/aws/lambda/dreams-workflow-dev-ai-determination" --start-time $startTime --region ap-northeast-1

# Workflow engine
aws logs filter-log-events --log-group-name "/aws/lambda/dreams-workflow-dev-workflow-engine" --start-time $startTime --region ap-northeast-1

# Email service
aws logs filter-log-events --log-group-name "/aws/lambda/dreams-workflow-dev-email-service" --start-time $startTime --region ap-northeast-1
```

## 時間戳轉換（+8 時區 → epoch milliseconds）

```powershell
$startTime = [DateTimeOffset]::new(2026, 5, 11, 15, 0, 0, [TimeSpan]::FromHours(8)).ToUnixTimeMilliseconds()
```
