from pathlib import Path

from fastapi.testclient import TestClient

from pa_agent.config.settings import Settings
from pa_server.api import create_app
from pa_server.config import ServerConfig
from pa_server.store import Store


def app_for(tmp_path: Path):
    cfg = ServerConfig(
        root=Path(__file__).resolve().parents[1], data_dir=tmp_path,
        settings_path=tmp_path / "settings.json", api_token="test-token",
        bind_host="127.0.0.1", port=8765, poll_seconds=60, timezone="Asia/Taipei",
    )
    settings = Settings()
    settings.provider.api_key = "test-ai-key"
    return create_app(cfg, settings, Store(tmp_path / "db.sqlite3"))


def test_management_api_requires_bearer_token(tmp_path: Path):
    with TestClient(app_for(tmp_path)) as client:
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "PA Server" in dashboard.text
        assert client.get("/health").status_code == 200
        assert client.get("/v1/watches").status_code == 401
        assert client.get("/v1/watches", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/v1/watches", headers={"Authorization": "Bearer test-token"}).status_code == 200


def test_watch_crud_does_not_allow_identity_mutation(tmp_path: Path):
    body = {"exchange": "okx", "symbol": "ethusdt", "timeframe": "15m"}
    headers = {"Authorization": "Bearer test-token", "Idempotency-Key": "watch-one"}
    with TestClient(app_for(tmp_path)) as client:
        created = client.post("/v1/watches", json=body, headers=headers)
        assert created.status_code == 201
        watch = created.json()
        assert watch["exchange"] == "OKX"
        assert client.post("/v1/watches", json=body, headers=headers).status_code == 200
        assert client.patch(
            f"/v1/watches/{watch['id']}", json={"state": "paused"},
            headers={"Authorization": "Bearer test-token"},
        ).json()["state"] == "paused"
        assert client.patch(
            f"/v1/watches/{watch['id']}", json={"symbol": "BTCUSDT"},
            headers={"Authorization": "Bearer test-token"},
        ).status_code == 422


def test_trading_settings_hide_credentials_and_default_safe(tmp_path: Path):
    app = app_for(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        response = client.get("/v1/trading/settings", headers=headers)
        assert response.status_code == 200
        value = response.json()
        assert value["enabled"] is False
        assert value["profile"] == "demo"
        assert value["fixed_notional_usdt"] == 0
        assert value["max_notional_usdt"] == 0
        assert value["credentials_configured"] == {"demo": False, "live": False}
        text = response.text.lower()
        assert "api_key" not in text
        assert "passphrase" not in text

        assert client.get("/v1/trading/status").status_code == 401
        update = {key: value[key] for key in (
            "enabled", "profile", "api_region", "margin_mode", "leverage",
            "sizing_mode", "fixed_notional_usdt", "fixed_margin_usdt",
            "risk_percent", "max_notional_usdt", "pending_expiry_bars",
            "poll_interval_seconds",
        )}
        update["leverage"] = 2
        saved = client.put("/v1/trading/settings", json=update, headers=headers)
        assert saved.status_code == 200
        assert saved.json()["leverage"] == 2


def test_watch_trading_requires_okx_usdt_swap(tmp_path: Path):
    headers = {"Authorization": "Bearer test-token", "Idempotency-Key": "bad-trade"}
    with TestClient(app_for(tmp_path)) as client:
        response = client.post("/v1/watches", json={
            "exchange": "BINANCE", "symbol": "BTCUSDT", "timeframe": "15m",
            "trading_enabled": True, "okx_instrument": "BTC-USDT-SWAP",
        }, headers=headers)
        assert response.status_code == 422
