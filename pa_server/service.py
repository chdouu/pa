from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pa_agent.notify.feishu_notifier import send_order_signal, send_trade_event
from pa_agent.ai.client_factory import create_ai_client
from pa_agent.config.settings import provider_api_key_configured

from .analysis import AnalysisError, has_order_opportunity, run_snapshot_analysis
from .config import settings_version
from .market import closed_bars, discover_snapshots, fetch_bars, latest_closed_ts, serialize_snapshot, watch_request
from .trading import ServerTradingController

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, config, settings, version, store) -> None:
        self.config, self.settings, self.version, self.store = config, settings, version, store
        self.ai_client = create_ai_client(settings.provider)
        self.trading = ServerTradingController(config, settings, store)
        # The controller loads persisted non-secret trading settings before the
        # effective version is calculated. Jobs can therefore prove which
        # analysis and execution configuration they were created under.
        self.version = settings_version(self.settings)
        self.stop = threading.Event()
        self.wake_scheduler = threading.Event()
        self.threads: list[threading.Thread] = []
        self.last_scheduler_run: str | None = None
        self.last_trading_error: str | None = None

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc)
        secrets = [
            self.config.api_token,
            self.settings.provider.api_key,
            self.settings.feishu.webhook_url,
            self.settings.feishu.secret,
            *[group.api_key for group in self.settings.provider.fallback_groups],
            self.config.okx_demo_api_key,
            self.config.okx_demo_secret_key,
            self.config.okx_demo_passphrase,
            self.config.okx_live_api_key,
            self.config.okx_live_secret_key,
            self.config.okx_live_passphrase,
        ]
        for secret in secrets:
            if secret:
                message = message.replace(secret, "****")
        return message

    def start(self) -> None:
        for name, target in (
            ("market-scheduler", self._scheduler_loop),
            ("analysis-worker", self._analysis_loop),
            ("notification-worker", self._notification_loop),
            ("trading-worker", self._trading_loop),
        ):
            thread = threading.Thread(name=name, target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self) -> None:
        self.stop.set()
        self.wake_scheduler.set()
        for thread in self.threads:
            thread.join(timeout=5)

    def _scheduler_loop(self) -> None:
        while not self.stop.is_set():
            self.scan_watches()
            self.last_scheduler_run = datetime.now(timezone.utc).isoformat()
            self.wake_scheduler.wait(self.config.poll_seconds)
            self.wake_scheduler.clear()

    def scan_watches(self) -> None:
        for watch in self.store.list_watches():
            if self.stop.is_set() or watch["state"] != "active":
                continue
            try:
                snapshots = discover_snapshots(watch)
                self.store.enqueue_watch_jobs(watch["id"], watch_request(watch), snapshots, self.version)
            except Exception as exc:
                message = self._safe_error(exc)
                logger.warning("watch=%s market scan failed: %s", watch["id"], message)
                if "gap" in message.lower():
                    self.store.set_watch_state(watch["id"], "paused", message)
                else:
                    self.store.set_watch_state(watch["id"], "active", message)

    def _analysis_loop(self) -> None:
        while not self.stop.is_set():
            job = self.store.claim_job()
            if not job:
                self.stop.wait(1)
                continue
            try:
                if job["settings_version"] != self.version:
                    raise AnalysisError(
                        "settings_changed",
                        "Job was queued under a different model/settings version; resubmit it explicitly",
                    )
                if not job["snapshot"]:
                    self.store.update_job_progress(job["id"], "fetching_market_data")
                    request = job["request"]
                    count = request["bar_count"] + 51
                    bars = closed_bars(fetch_bars(request, count), request)
                    job["snapshot"] = serialize_snapshot(bars)
                    if not bars:
                        raise AnalysisError("market_data_error", "No closed TradingView bars")
                    job["target_ts"] = int(bars[0].ts_open)
                    self.store.set_job_snapshot(job["id"], job["target_ts"], job["snapshot"])
                if job.get("watch_id"):
                    job["previous_record"] = self.store.previous_record(job["watch_id"], int(job["target_ts"]), job["settings_version"])
                result, record = run_snapshot_analysis(
                    job, self.settings, self.config.root,
                    lambda stage: self.store.update_job_progress(job["id"], stage),
                    self.config.timezone,
                    self.ai_client,
                )
                threshold = int(getattr(self.settings.general, "decision_confidence_threshold", 0))
                notify = bool(
                    self.settings.feishu.enabled
                    and self.settings.feishu.webhook_url.strip()
                    and has_order_opportunity(result["stage2_decision"], threshold)
                    and job.get("watch_id")
                )
                result["notification_status"] = "pending" if notify else "not_required"
                watch = self.store.get_watch(job["watch_id"]) if job.get("watch_id") else None
                trade = bool(
                    watch
                    and watch.get("trading_enabled")
                    and self.settings.okx.enabled
                    and watch.get("state") == "active"
                )
                result["trading_status"] = "pending" if trade else "not_required"
                self.store.finish_job(job["id"], result, record, notify, trade)
            except AnalysisError as exc:
                self.store.fail_job(job["id"], exc.code, str(exc))
            except Exception as exc:
                logger.exception("analysis job=%s failed", job["id"])
                self.store.fail_job(job["id"], "internal_error", self._safe_error(exc))

    def _notification_loop(self) -> None:
        while not self.stop.is_set():
            note = self.store.claim_notification()
            if not note:
                self._notify_trade_event()
                self.stop.wait(1)
                continue
            job = self.store.get_job(note["job_id"])
            if not job or not job.get("result"):
                self.store.finish_notification(note["id"], "failed", "Analysis result missing")
                continue
            request, result = job["request"], job["result"]
            try:
                current_ts = latest_closed_ts(request)
                if current_ts > int(job["target_ts"]):
                    self.store.finish_notification(note["id"], "stale")
                    continue
                if current_ts < int(job["target_ts"]):
                    raise RuntimeError("TradingView has not confirmed the target bar as latest")
                stage2 = result["stage2_decision"]
                decision = stage2.get("decision") or {}
                ok = send_order_signal(
                    decision_inner=decision, stage2_full=stage2,
                    symbol=request["symbol"], timeframe=request["timeframe"],
                    settings=self.settings,
                    exchange=request["exchange"], bar_time_ms=int(job["target_ts"]),
                    analysis_id=job["id"], timezone_name=self.config.timezone,
                )
                if ok:
                    self.store.finish_notification(note["id"], "sent")
                else:
                    raise RuntimeError("Feishu rejected the notification")
            except Exception as exc:
                safe_error = self._safe_error(exc)
                attempts = int(note["attempts"])
                if attempts >= 8:
                    self.store.finish_notification(note["id"], "failed", safe_error)
                else:
                    delay = min(3600, 30 * (2 ** max(0, attempts - 1)))
                    retry = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                    self.store.finish_notification(note["id"], "pending", safe_error, retry)

    def _notify_trade_event(self) -> None:
        event = self.store.claim_trade_event_notification()
        if not event:
            return
        if not self.settings.feishu.enabled or not self.settings.feishu.webhook_url.strip():
            self.store.finish_trade_event_notification(event["id"], "not_required")
            return
        try:
            ok = send_trade_event(
                event=event["event"], data=event["data"], settings=self.settings
            )
            if ok:
                self.store.finish_trade_event_notification(event["id"], "sent")
            else:
                raise RuntimeError("Feishu rejected the OKX event")
        except Exception as exc:
            error = self._safe_error(exc)
            attempts = int(event["attempts"])
            if attempts >= 8:
                self.store.finish_trade_event_notification(event["id"], "failed", error)
            else:
                retry = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=min(3600, 30 * (2 ** max(0, attempts - 1))))
                ).isoformat()
                self.store.finish_trade_event_notification(event["id"], "pending", error, retry)

    def _trading_loop(self) -> None:
        next_reconcile = 0.0
        while not self.stop.is_set():
            task = self.store.claim_trade_task()
            if task:
                self._run_trade_task(task)
                continue
            now = time.monotonic()
            if now >= next_reconcile:
                try:
                    self.trading.reconcile()
                    self.last_trading_error = None
                except Exception as exc:
                    self.last_trading_error = self._safe_error(exc)
                    logger.warning("OKX reconciliation failed: %s", self.last_trading_error)
                next_reconcile = now + max(1, int(self.settings.okx.poll_interval_seconds))
            self.stop.wait(0.5)

    def _run_trade_task(self, task: dict) -> None:
        job = self.store.get_job(task["job_id"])
        watch = self.store.get_watch(task["watch_id"])
        if not job or not watch or not job.get("result"):
            self.store.finish_trade_task(task["id"], "failed", error="Analysis or watch missing")
            return
        try:
            if not self.settings.okx.enabled:
                self.store.finish_trade_task(task["id"], "skipped", result={"reason": "global_disabled"})
                return
            if watch["state"] != "active" or not watch.get("trading_enabled"):
                self.store.finish_trade_task(task["id"], "skipped", result={"reason": "watch_disabled"})
                return
            if job["settings_version"] != self.version:
                self.store.finish_trade_task(
                    task["id"], "skipped", result={"reason": "settings_changed"}
                )
                return
            current_ts = latest_closed_ts(job["request"])
            if current_ts != int(job["target_ts"]):
                self.store.finish_trade_task(task["id"], "stale", result={"latest_bar_ts": current_ts})
                return
            result = self.trading.submit_job(job, watch)
            self.store.finish_trade_task(task["id"], "succeeded", result=result)
        except Exception as exc:
            error = self._safe_error(exc)
            attempts = int(task["attempts"])
            if attempts >= 8:
                self.store.finish_trade_task(task["id"], "failed", error=error)
                self.store.record_trade_event(
                    "EXECUTION_FAILED",
                    {"analysis_id": task["job_id"], "watch_id": task["watch_id"], "error": error},
                )
            else:
                retry = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=min(300, 5 * (2 ** max(0, attempts - 1))))
                ).isoformat()
                self.store.finish_trade_task(task["id"], "pending", error=error, next_attempt=retry)

    def update_trading_settings(self, values: dict) -> dict:
        previous = self.trading.public_settings()
        previous.pop("credentials_configured", None)
        try:
            applied = self.trading.apply_settings(values)
            if applied.get("enabled"):
                profile = applied["profile"]
                if not self.trading.credential_status().get(profile):
                    raise ValueError(f"{profile} 環境的 OKX 憑證尚未完整設定")
                self.trading.validate()
            stored = self.store.save_trading_settings(applied)
            self.trading.refresh_mappings()
            self.version = settings_version(self.settings)
            return stored
        except Exception:
            self.trading.apply_settings(previous, check_identity=False)
            raise

    def ready(self) -> tuple[bool, dict]:
        alive = {t.name: t.is_alive() for t in self.threads}
        ok = bool(self.config.api_token) and all(alive.values()) and provider_api_key_configured(self.settings)
        return ok, {
            "ready": ok,
            "workers": alive,
            "scheduler_last_run": self.last_scheduler_run,
            "trading_error": self.last_trading_error,
        }
