from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    trading_enabled: bool = False
    okx_instrument: str = ""

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

    @field_validator("okx_instrument")
    @classmethod
    def clean_instrument(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def validate_trading(self):
        if self.trading_enabled:
            if self.exchange != "OKX":
                raise ValueError("自動交易監控必須使用 TradingView OKX 行情")
            if not self.okx_instrument.endswith("-USDT-SWAP"):
                raise ValueError("自動交易需要有效的 OKX USDT 永續合約")
        return self


class WatchPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["active", "paused"] | None = None
    trading_enabled: bool | None = None
    okx_instrument: str | None = None

    @model_validator(mode="after")
    def not_empty(self):
        if self.state is None and self.trading_enabled is None and self.okx_instrument is None:
            raise ValueError("至少需要一個修改欄位")
        return self


class TradingSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    profile: Literal["demo", "live"] = "demo"
    api_region: Literal["global", "eea", "us"] = "global"
    margin_mode: Literal["isolated", "cross"] = "isolated"
    leverage: int = Field(default=1, ge=1, le=125)
    sizing_mode: Literal["fixed_notional", "fixed_margin", "risk_percent"] = "fixed_notional"
    fixed_notional_usdt: float = Field(default=0, ge=0)
    fixed_margin_usdt: float = Field(default=0, ge=0)
    risk_percent: float = Field(default=0, ge=0, le=100)
    max_notional_usdt: float = Field(default=0, ge=0)
    pending_expiry_bars: int = Field(default=3, ge=1, le=100)
    poll_interval_seconds: int = Field(default=2, ge=1, le=60)


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

    app = FastAPI(title="PA Server", version="0.2.0", lifespan=lifespan)
    app.state.runtime = runtime
    web_dir = Path(__file__).resolve().parent / "web"
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(web_dir / "index.html")

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
            watch, created = store.create_watch(body.model_dump(), runtime.version, idempotency_key)
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
        watch = store.get_watch(watch_id)
        if not watch:
            raise HTTPException(404, "Watch not found")
        if body.state is not None:
            store.set_watch_state(watch_id, body.state)
        if body.trading_enabled is not None or body.okx_instrument is not None:
            instrument = (
                body.okx_instrument.strip().upper()
                if body.okx_instrument is not None
                else watch.get("okx_instrument", "")
            )
            enabled = (
                body.trading_enabled
                if body.trading_enabled is not None
                else bool(watch.get("trading_enabled"))
            )
            if enabled and (
                watch["exchange"] != "OKX" or not instrument.endswith("-USDT-SWAP")
            ):
                raise HTTPException(422, "Trading requires OKX market data and a USDT swap")
            if instrument != watch.get("okx_instrument"):
                active = runtime.trading.service.store.active_all()
                if any(row["inst_id"] == watch.get("okx_instrument") for row in active):
                    raise HTTPException(409, "Cannot change mapping while its thesis is active")
            try:
                store.update_watch_trading(
                    watch_id, enabled=enabled, okx_instrument=instrument
                )
                runtime.trading.refresh_mappings()
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        if body.state == "active":
            runtime.wake_scheduler.set()
        return store.get_watch(watch_id)

    @app.delete("/v1/watches/{watch_id}", status_code=204, dependencies=[auth])
    def delete_watch(watch_id: str) -> Response:
        watch = store.get_watch(watch_id)
        if not watch:
            raise HTTPException(404, "Watch not found")
        instrument = watch.get("okx_instrument") or ""
        if instrument and any(
            row["inst_id"] == instrument
            for row in runtime.trading.service.store.active_all()
        ):
            raise HTTPException(409, "Cannot delete a watch while its trading thesis is active")
        if not store.delete_watch(watch_id):
            raise HTTPException(404, "Watch not found")
        runtime.trading.refresh_mappings()
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
            job, created = store.create_manual_job(body.model_dump(), runtime.version, idempotency_key)
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

    @app.get("/v1/trading/settings", dependencies=[auth])
    def get_trading_settings() -> dict:
        return runtime.trading.public_settings()

    @app.put("/v1/trading/settings", dependencies=[auth])
    def put_trading_settings(body: TradingSettingsRequest) -> dict:
        try:
            runtime.update_trading_settings(body.model_dump())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, runtime._safe_error(exc)) from exc
        return runtime.trading.public_settings()

    @app.post("/v1/trading/validate", dependencies=[auth])
    def validate_trading() -> dict:
        try:
            return runtime.trading.validate()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, runtime._safe_error(exc)) from exc

    @app.get("/v1/trading/status", dependencies=[auth])
    def trading_status() -> dict:
        return {
            **runtime.trading.status(),
            "tasks": store.list_trade_tasks(100),
            "events": store.list_trade_events(100),
            "last_error": runtime.last_trading_error,
        }

    @app.get("/v1/trading/theses/{thesis_id}/orders", dependencies=[auth])
    def thesis_orders(thesis_id: int) -> list[dict]:
        return runtime.trading.orders(thesis_id)

    def perform_action(
        action: str, thesis_id: int, idempotency_key: str | None,
    ) -> dict:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key is required")
        try:
            prior, created = store.begin_trade_action(idempotency_key, action, thesis_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not created:
            return prior
        try:
            result = (
                runtime.trading.cancel_pending(thesis_id)
                if action == "cancel"
                else runtime.trading.close(thesis_id)
            )
            return store.finish_trade_action(idempotency_key, "succeeded", result=result)
        except Exception as exc:
            error = runtime._safe_error(exc)
            store.finish_trade_action(idempotency_key, "failed", error=error)
            raise HTTPException(409, error) from exc

    @app.post("/v1/trading/theses/{thesis_id}/cancel", dependencies=[auth])
    def cancel_thesis(
        thesis_id: int,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        return perform_action("cancel", thesis_id, idempotency_key)

    @app.post("/v1/trading/theses/{thesis_id}/close", dependencies=[auth])
    def close_thesis(
        thesis_id: int,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        return perform_action("close", thesis_id, idempotency_key)

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
        cfg.okx_demo_api_key,
        cfg.okx_demo_secret_key,
        cfg.okx_demo_passphrase,
        cfg.okx_live_api_key,
        cfg.okx_live_secret_key,
        cfg.okx_live_passphrase,
    ]
    configure_logging(settings.provider.api_key)
    update_api_keys(secrets)
    import uvicorn
    uvicorn.run(create_app(cfg, settings), host=cfg.bind_host, port=cfg.port, workers=1)
