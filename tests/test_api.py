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
