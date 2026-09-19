# PA Server

PA Server 是 PA_Agent 的無介面常駐版本。它透過 TradingView 監控指定商品，將每根新收盤
K 棒保存成固定快照，依序執行原本的兩階段 PA 分析，並在出現交易機會且結果仍屬於最新
收盤棒時推送飛書。服務只提供分析建議，不連接券商，也不下單。

## 啟動

需要 Docker 及 Docker Compose。先建立本機設定：

```powershell
New-Item -ItemType Directory -Force config, data, records, logs
Copy-Item settings.example.json config/settings.json
Copy-Item .env.example .env
```

模型候補清單已寫在 `config/settings.json`。敏感值只需放在 `.env`：

- `PA_API_TOKEN`：管理 API 的長隨機 Token。
- `PA_GEMINI_API_KEYS`：一至多組 Gemini API Key，以逗號分隔；前面的 Key 優先。
- `PA_FEISHU_WEBHOOK_URL`：飛書自訂機器人的 Webhook URL。
- `PA_FEISHU_SECRET`：飛書簽名 Secret，可留空。

每組 Key 都依序嘗試 Gemini 2.5 Flash、2.5 Flash Lite、3 Flash、3.1 Flash Lite、
3.5 Flash、3.5 Flash Lite、3.6 Flash、3.7 Flash、3.8 Flash。同一組的模型全部失敗後
才切換下一組 Key；成功組合會在本次服務執行期間成為下一次分析的第一候補。

舊版的 `PA_AI_API_KEY` 與 `PA_FALLBACK_API_KEYS` 仍可使用，但新的部署不需要設定。
飛書通知固定使用文字互動卡片，不需要 App ID 或 App Secret。

啟動並檢查：

```powershell
docker compose up -d --build
Invoke-RestMethod http://127.0.0.1:8765/health
Invoke-RestMethod http://127.0.0.1:8765/ready
```

Compose 只將連接埠綁定到 `127.0.0.1`。需要遠端使用時，請在前方配置 HTTPS 反向代理，
不要直接把 Uvicorn 暴露到公網。

## API 範例

```powershell
$headers = @{ Authorization = "Bearer $env:PA_API_TOKEN" }
$body = @{
  source = 'tradingview'
  exchange = 'OKX'
  symbol = 'ETHUSDT'
  timeframe = '15m'
  bar_count = 100
  extended_session = $false
} | ConvertTo-Json

$watch = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/v1/watches `
  -Headers ($headers + @{'Idempotency-Key'='okx-ethusdt-15m'}) `
  -ContentType application/json -Body $body

Invoke-RestMethod -Headers $headers -Uri "http://127.0.0.1:8765/v1/watches/$($watch.id)"
Invoke-RestMethod -Headers $headers -Uri "http://127.0.0.1:8765/v1/watches/$($watch.id)/analyses"
```

暫停或恢復只接受 `{"state":"paused"}` 或 `{"state":"active"}`。刪除監控不刪除歷史分析。
單次分析使用 `POST /v1/analyses`，飛書測試使用 `POST /v1/notifications/feishu/test`。
Swagger 文件在 `/docs`，受保護端點需輸入 Bearer Token。

Linux 上也可以直接使用：

```bash
curl -H "Authorization: Bearer $PA_API_TOKEN" http://127.0.0.1:8765/v1/watches
curl -X POST -H "Authorization: Bearer $PA_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"exchange":"OKX","symbol":"ETHUSDT","timeframe":"15m","bar_count":100}' \
  http://127.0.0.1:8765/v1/watches
```

## 執行語義

- 建立監控後只分析最近一根已收盤棒；之後逐棒排隊，不合併積壓。
- 每筆工作保存自己的行情快照，因此排隊時不會偷看後續行情。
- 佇列中工作的設定版本不會被靜默替換；若停機時更改模型設定，舊版本的未完成工作會標記
  `settings_changed`，可用單次分析重新提交，避免用錯模型卻記成舊版本。
- 同一監控依 K 棒時間執行；不同監控由單一工作者輪流選取。
- 分析查詢結果的 `ai_provider` 會顯示實際成功的 API 組別與模型 ID，但不會回傳 API Key。
- 重啟會恢復進行中的分析與通知。若停機太久而 TradingView 已無法提供完整缺口，監控會
  自動暫停並顯示錯誤，避免假裝補齊。
- 只有限價單、突破單或市價單且達到 PA 設定置信度門檻時建立通知。推送前若已有更新的
  收盤棒，結果標記為 `stale` 且不推送。
- SQLite、快照狀態與通知佇列保存在 `data/pa-server.sqlite3`；完整分析記錄保存在
  `records/<analysis-id>/`。建議一起備份 `data/`、`records/`、`logs/` 與設定檔；恢復時停止
  服務、還原這些目錄，再以相同 `.env` 啟動，進行中的工作會自動回到佇列。

## 更新與授權

PA 分析核心的來源版本記錄在 `SOURCE.md`。此專案沿用上游 AGPL-3.0-or-later 授權；若透過
網路提供修改後的服務，應依授權條款提供對應原始碼。
