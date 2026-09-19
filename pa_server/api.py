from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from pa_agent.notify.feishu_notifier import send_order_signal

from .config import ServerConfig, load_runtime_settings, settings_version
from .service import Runtime
from .store import Store

TIMEFRAMES = frozenset(("1m", "3m", "5m", "15m", "30m", "45m", "1h", "2h", "3h", "4h", "1d", "1w", "1M"))


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["tradingview"] = "tradingview"
    exchange: str = Field(min_length=1, max_length=40)
    symbol: str = Field(min_length=1, max_length=100)
    timeframe: str
    bar_count: int = Field(default=100, ge=20, le=5000)
    extended_session: bool = False

    @field_validator("exchange", "symbol")
    @classmethod
    def clean(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value.upper()

    @field_validator("timeframe")
    @classmethod
    def valid_timeframe(cls, value: str) -> str:
        if value not in TIMEFRAMES:
            raise ValueError("unsupported timeframe")
        return value


class WatchPatch(BaseModel):
    state: Literal["active", "paused"]


def create_app(config: ServerConfig | None = None, settings=None, store: Store | None = None) -> FastAPI:
    cfg = config or ServerConfig.from_env()
    settings = settings or load_runtime_settings(cfg)
    version = settings_version(settings)
    store = store or Store(cfg.data_dir / "pa-server.sqlite3")
    runtime = Runtime(cfg, settings, version, store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if not cfg.api_token:
            raise RuntimeError("PA_API_TOKEN must be configured")
        runtime.start()
        try:
            yield
        finally:
            runtime.close()

    app = FastAPI(title="PA Server", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime

    bearer = HTTPBearer(auto_error=False)

    def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(credentials.credentials, cfg.api_token)
        ):
            raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    auth = Depends(authorize)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", **store.health_counts()}

    @app.get("/ready")
    def ready(response: Response) -> dict:
        ok, detail = runtime.ready()
        if not ok:
            response.status_code = 503
        return detail

    @app.post("/v1/watches", status_code=201, dependencies=[auth])
    def create_watch(body: AnalysisRequest, response: Response, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
        try:
            watch, created = store.create_watch(body.model_dump(), version, idempotency_key)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        response.status_code = 201 if created else 200
        if created:
            runtime.wake_scheduler.set()
        return watch

    @app.get("/v1/watches", dependencies=[auth])
    def list_watches() -> list[dict]:
        return store.list_watches()

    @app.get("/v1/watches/{watch_id}", dependencies=[auth])
    def get_watch(watch_id: str) -> dict:
        watch = store.get_watch(watch_id)
        if not watch:
            raise HTTPException(404, "Watch not found")
        return watch

    @app.patch("/v1/watches/{watch_id}", dependencies=[auth])
    def patch_watch(watch_id: str, body: WatchPatch) -> dict:
        if not store.set_watch_state(watch_id, body.state):
            raise HTTPException(404, "Watch not found")
        if body.state == "active":
            runtime.wake_scheduler.set()
        return store.get_watch(watch_id)

    @app.delete("/v1/watches/{watch_id}", status_code=204, dependencies=[auth])
    def delete_watch(watch_id: str) -> Response:
        if not store.delete_watch(watch_id):
            raise HTTPException(404, "Watch not found")
        return Response(status_code=204)

    @app.get("/v1/watches/{watch_id}/analyses", dependencies=[auth])
    def watch_analyses(watch_id: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> list[dict]:
        if not store.get_watch(watch_id):
            raise HTTPException(404, "Watch not found")
        items = store.list_analyses(watch_id, limit, offset)
        for item in items:
            item.pop("record", None)
            item.pop("snapshot", None)
        return items

    @app.post("/v1/analyses", status_code=202, dependencies=[auth])
    def create_analysis(body: AnalysisRequest, response: Response, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
        try:
            job, created = store.create_manual_job(body.model_dump(), version, idempotency_key)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        response.status_code = 202 if created else 200
        return {"job_id": job["id"], "status": job["status"], "status_url": f"/v1/analyses/{job['id']}"}

    @app.get("/v1/analyses/{job_id}", dependencies=[auth])
    def get_analysis(job_id: str) -> dict:
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(404, "Analysis not found")
        job.pop("record", None)
        job.pop("snapshot", None)
        return job

    @app.post("/v1/notifications/feishu/test", dependencies=[auth])
    def test_feishu() -> dict:
        ok = send_order_signal(
            decision_inner={"order_type": "市价单", "order_direction": "测试", "reasoning": "PA Server 飞书连接测试"},
            stage2_full={}, symbol="PA-SERVER", timeframe="TEST", settings=settings,
            exchange="TEST", analysis_id="connection-test",
        )
        if not ok:
            raise HTTPException(502, "Feishu test message failed")
        return {"sent": True}

    return app


def main() -> None:
    cfg = ServerConfig.from_env()
    settings = load_runtime_settings(cfg)
    from pa_agent.util.logging import configure_logging, update_api_keys

    secrets = [
        cfg.api_token,
        settings.provider.api_key,
        settings.feishu.webhook_url,
        settings.feishu.secret,
        *[group.api_key for group in settings.provider.fallback_groups],
    ]
    configure_logging(settings.provider.api_key)
    update_api_keys(secrets)
    import uvicorn
    uvicorn.run(create_app(cfg, settings), host=cfg.bind_host, port=cfg.port, workers=1)
