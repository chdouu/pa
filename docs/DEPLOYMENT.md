# Linux 部署、更新與復原

## 安裝與 HTTPS

安裝 Docker Engine 與 Compose plugin，將專案放到 `/opt/pa-server`，依 README 建立 `config/`、`.env` 與資料目錄，再執行 `docker compose up -d --build`。Compose 預設只監聽 `127.0.0.1`。

Caddy 反向代理範例：

```caddyfile
pa.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

DNS 指向伺服器後由 Caddy 取得 TLS 憑證。防火牆只開放 80/443，不開放 8765。管理 Token 應使用密碼管理器保存並定期輪替。

## 更新

```bash
cd /opt/pa-server
git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose ps
curl http://127.0.0.1:8765/ready
```

啟動時會以 `CREATE TABLE IF NOT EXISTS` 和欄位遷移保留既有監控與分析；新增交易欄位預設關閉。桌面 PA_Agent 的交易資料不會自動匯入。

## 一致性備份

先停止服務，避免複製到一半的 SQLite 檔案：

```bash
docker compose stop pa-server
tar -czf "pa-backup-$(date +%Y%m%d-%H%M%S).tar.gz" data records logs config .env
docker compose start pa-server
```

備份包含敏感 `.env`，必須加密保存。也可使用 SQLite online backup 取代停機；不得直接複製正在寫入的 WAL 資料庫而遺漏 `-wal` 檔。

## 恢復

```bash
docker compose down
tar -xzf pa-backup-YYYYMMDD-HHMMSS.tar.gz
docker compose up -d --build
curl http://127.0.0.1:8765/ready
```

恢復後檢查網頁的分析積壓、交易工作與進行中方案。服務會把中斷中的分析、通知和交易工作恢復到待處理，OKX 工作者先查單、倉位及保護單狀態再繼續。

## 模擬與實盤切換

模擬與實盤使用各自的三個 `PA_OKX_*` 變數。設定檔只保存環境名稱和非敏感風控值。任何環境都要求 OKX 單向持倉模式、TradingView OKX 行情及 USDT 永續映射。

保持全域交易關閉即可安全檢查憑證與帳戶。設定非零金額和非零上限後按「測試設定」；測試只讀帳戶與商品設定，不建立訂單。存在未完成方案時不可切換環境或 API 區域。實盤切換應在沒有進行中方案時進行，切換後再次按「測試設定」，再手動啟用全域與單一監控開關。
