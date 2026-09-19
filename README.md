# PA Server

PA Server 是依本機 PA_Agent 重建的 Linux 常駐版。它監控 TradingView 的收盤 K 棒，依序執行相同的市場診斷、策略路由、決策與校驗，保存固定行情快照，並可用飛書通知。繁體中文管理頁可管理監控、分析紀錄與 OKX USDT 永續模擬／實盤交易。

交易預設關閉，金額與每筆名目上限預設為 0。只有全域交易開關與監控交易開關都啟用、設定驗證通過，而且分析仍屬最新收盤棒時，交易工作者才會建立新交易。

## 快速啟動

需要 Docker Engine 與 Docker Compose。Linux 執行：

```bash
mkdir -p config data records logs
cp settings.example.json config/settings.json
cp .env.example .env
chmod 600 .env
```

編輯 `.env`：

- `PA_API_TOKEN`：長度足夠的隨機管理 Token。
- `PA_GEMINI_API_KEYS`：一至多組 Gemini Key，以逗號分隔。
- `PA_FEISHU_WEBHOOK_URL`、`PA_FEISHU_SECRET`：飛書自訂機器人；Secret 可留空。
- `PA_OKX_DEMO_*`：OKX 模擬環境 API Key、Secret、Passphrase。
- `PA_OKX_LIVE_*`：OKX 實盤環境憑證，驗收模擬流程前請保持空白。

```bash
docker compose up -d --build
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/ready
```

開啟 `http://127.0.0.1:8765/`，輸入 `PA_API_TOKEN`。Token 只存在目前頁面的 JavaScript 記憶體，重新整理後需要再次登入。

Compose 只發布到主機 `127.0.0.1:8765`。遠端操作請使用 HTTPS 反向代理；完整 Linux、Caddy、更新、備份與恢復流程見 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)。

## Gemini 切換

每個 Key 依設定順序嘗試九個模型，再切換下一個 Key。成功組合會成為本次程序下一次請求的第一候補；市場診斷與交易決策分別記錄實際 API 組別和模型 ID。Key 不寫入分析結果、API 回應或日誌。

模型清單位於 `settings.example.json`。候補是否可用由 Google API 的實際回應決定，因此不存在、停用或無配額的模型會安全落到下一候補。舊的 `PA_AI_API_KEY` 與 `PA_FALLBACK_API_KEYS` 仍可載入。

## 建立監控與交易

網頁可直接建立監控。API 範例：

```bash
curl -X POST http://127.0.0.1:8765/v1/watches \
  -H "Authorization: Bearer $PA_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: okx-btc-15m' \
  -d '{"exchange":"OKX","symbol":"BTCUSDT","timeframe":"15m","bar_count":100,"extended_session":false,"okx_instrument":"BTC-USDT-SWAP","trading_enabled":false}'
```

建立時只分析最近一根已收盤棒；之後逐根保存與分析，不合併積壓。停機恢復時補抓缺漏；來源無法完整補回會暫停監控並顯示缺口。

安全啟用 OKX 模擬交易：

1. 在 `.env` 填入 `PA_OKX_DEMO_*`，重啟容器。
2. 先建立 OKX 行情監控，合約格式使用 `BTC-USDT-SWAP`，交易開關先保持關閉。
3. 到「OKX 交易」選擇模擬、單向持倉帳戶對應的保證金模式、槓桿和倉位方式，設定非零金額及非零每筆名目上限。
4. 按「測試設定」。通過後才啟用全域交易，再啟用指定監控的交易。
5. 確認送單、成交、保護單、退出與重啟對帳流程後，才考慮另外配置實盤憑證。

同一合約只能有一個自動交易監控。切換模擬／實盤、API 區域、刪除監控或修改合約映射時，如仍有未完成交易方案，API 會拒絕。關閉交易或暫停監控只停止新入場；既有方案仍會對帳並維持保護單。取消掛單和平倉是兩個獨立且具冪等鍵的操作，網頁會先顯示確認視窗。

已有持倉會把固定的 Active Thesis 快照交給下一根最新收盤棒分析。AI 只能選擇收近 TP1 或延伸 TP2，修改成功後會查詢 OKX 確認並持久化目前 TP；關閉交易開關仍會管理既有持倉，暫停監控則停止新的 AI TP 建議。監控清單可刪除監控並保留歷史，但有未完成交易方案時會拒絕刪除。

## API

所有 `/v1` 管理及交易端點均要求 `Authorization: Bearer <token>`。

- `POST/GET /v1/watches`、`GET/PATCH/DELETE /v1/watches/{id}`
- `GET /v1/watches/{id}/analyses`
- `POST /v1/analyses`、`GET /v1/analyses/{id}`
- `POST /v1/notifications/feishu/test`
- `GET/PUT /v1/trading/settings`、`POST /v1/trading/validate`
- `GET /v1/trading/status`、`GET /v1/trading/theses/{id}/orders`
- `POST /v1/trading/theses/{id}/cancel`、`POST /v1/trading/theses/{id}/close`
- 公開的 `GET /health`、`GET /ready`

Swagger 文件在 `/docs`。交易操作需額外提供 `Idempotency-Key`。

## 持久化與一致性

- 主服務資料在 `data/pa-server.sqlite3`，OKX 狀態機在 `data/okx-trading.sqlite3`，完整分析輸出在 `records/<analysis-id>/`。
- 成功分析與通知／交易待處理事件在同一 SQLite 交易提交；工作以分析 ID 去重。
- 送單回應不確定時先以 client order ID 查單，不盲目重送。重啟會先恢復執行中工作並持續對帳。
- 飛書固定發送文字互動卡片。分析通知發送前重新核對最新收盤棒；過時補分析保留歷史但不通知或交易。
- 交易憑證只來自容器環境，網頁只回傳是否已配置。

核心來源提交、來源工作樹狀態及關鍵檔案雜湊記錄在 [`SOURCE.md`](SOURCE.md)。專案沿用 AGPL-3.0-or-later。
