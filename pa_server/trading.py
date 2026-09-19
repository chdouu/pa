from __future__ import annotations

import threading
from typing import Any

from pa_agent.config.settings import OKXSettings
from pa_agent.trading.okx_client import OKXCredentials
from pa_agent.trading.okx_service import OKXTradingService
from pa_agent.trading.sizing import D


class ServerTradingController:
    """Bind the desktop OKX state machine to env-only Server credentials."""

    def __init__(self, config, settings, store, client_factory=None) -> None:
        self.config = config
        self.settings = settings
        self.store = store
        self._lock = threading.RLock()
        self.apply_settings(store.get_trading_settings(), check_identity=False)
        self.service = OKXTradingService(
            settings,
            db_path=config.data_dir / "okx-trading.sqlite3",
            client_factory=client_factory,
            event_callback=self._record_event,
        )
        # Instance override deliberately bypasses desktop DPAPI.
        self.service.credentials = self.credentials  # type: ignore[method-assign]
        self.refresh_mappings()

    def _record_event(self, event: str, data: dict[str, Any]) -> None:
        self.store.record_trade_event(event, data)

    def credentials(self) -> OKXCredentials:
        if self.settings.okx.profile == "live":
            return OKXCredentials(
                self.config.okx_live_api_key,
                self.config.okx_live_secret_key,
                self.config.okx_live_passphrase,
            )
        return OKXCredentials(
            self.config.okx_demo_api_key,
            self.config.okx_demo_secret_key,
            self.config.okx_demo_passphrase,
        )

    def credential_status(self) -> dict[str, bool]:
        return {
            "demo": bool(
                self.config.okx_demo_api_key
                and self.config.okx_demo_secret_key
                and self.config.okx_demo_passphrase
            ),
            "live": bool(
                self.config.okx_live_api_key
                and self.config.okx_live_secret_key
                and self.config.okx_live_passphrase
            ),
        }

    def refresh_mappings(self) -> None:
        mappings = {
            watch["symbol"].upper(): watch["okx_instrument"].upper()
            for watch in self.store.list_watches()
            if watch.get("okx_instrument")
        }
        self.settings.okx.symbol_mappings = mappings

    def apply_settings(self, values: dict, *, check_identity: bool = True) -> dict:
        with self._lock:
            current = getattr(self.settings, "okx", OKXSettings())
            merged = {**current.model_dump(exclude={"demo_credentials", "live_credentials"}), **values}
            merged.pop("symbol_mappings", None)
            candidate = OKXSettings.model_validate({
                **merged,
                "symbol_mappings": getattr(current, "symbol_mappings", {}),
            })
            if check_identity and hasattr(self, "service"):
                active = self.service.store.active_all()
                if active and (
                    candidate.profile != current.profile
                    or candidate.api_region != current.api_region
                ):
                    raise ValueError("有未完成交易方案，不能切換交易環境或 API 區域")
            self.settings.okx = candidate
            if hasattr(self, "service"):
                self.service.settings = self.settings
                self.refresh_mappings()
            return candidate.model_dump(exclude={"demo_credentials", "live_credentials", "symbol_mappings"})

    def public_settings(self) -> dict:
        values = self.settings.okx.model_dump(
            exclude={"demo_credentials", "live_credentials", "symbol_mappings"}
        )
        values["credentials_configured"] = self.credential_status()
        return values

    def validate(self) -> dict:
        self.refresh_mappings()
        account = self.service.validate_configuration()
        return {
            "valid": True,
            "profile": self.settings.okx.profile,
            "position_mode": account.get("posMode"),
            "credentials_configured": self.credential_status(),
        }

    def submit_job(self, job: dict, watch: dict) -> dict:
        self.refresh_mappings()
        instrument = str(watch.get("okx_instrument") or "").upper()
        if not instrument:
            return {"action": "ignored", "reason": "監控未設定 OKX 合約"}
        self.settings.okx.symbol_mappings[watch["symbol"].upper()] = instrument
        decision = (job["result"].get("stage2_decision") or {}).get("decision") or {}
        target = next(
            (bar for bar in (job.get("snapshot") or []) if int(bar["ts_open"]) == int(job["target_ts"])),
            (job.get("snapshot") or [{}])[0],
        )
        return self.service.submit_analysis(
            exchange=watch["exchange"],
            symbol=watch["symbol"],
            timeframe=watch["timeframe"],
            bar_ts=int(job["target_ts"]),
            decision=decision,
            stage2_full=job["result"].get("stage2_decision") or {},
            bar_high=target.get("high"),
            bar_low=target.get("low"),
        )

    def reconcile(self) -> None:
        self.service.reconcile_all()

    def status(self) -> dict:
        active = self.service.store.active_all()
        return {
            "settings": self.public_settings(),
            "active": active,
            "recent": self.service.store.recent(100),
        }

    def orders(self, thesis_id: int) -> list[dict]:
        return self.service.store.orders(thesis_id)

    def cancel_pending(self, thesis_id: int) -> dict:
        thesis = next((row for row in self.service.store.active_all() if row["id"] == thesis_id), None)
        if not thesis:
            raise ValueError("找不到進行中的交易方案")
        if thesis["status"] not in {"PENDING_SUBMIT", "PENDING_ENTRY", "RECONCILE"}:
            raise ValueError("只有尚未成交的交易方案可以取消掛單")
        if D(thesis.get("filled_size") or 0) > 0:
            raise ValueError("交易方案已有成交，請使用平倉操作")
        self.service.close_thesis(thesis_id)
        return {"thesis_id": thesis_id, "action": "cancel_requested"}

    def close(self, thesis_id: int) -> dict:
        self.service.close_thesis(thesis_id)
        return {"thesis_id": thesis_id, "action": "close_requested"}
