from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pa_agent.notify.feishu_notifier import send_order_signal
from pa_agent.ai.client_factory import create_ai_client
from pa_agent.config.settings import provider_api_key_configured

from .analysis import AnalysisError, has_order_opportunity, run_snapshot_analysis
from .market import closed_bars, discover_snapshots, fetch_bars, latest_closed_ts, serialize_snapshot, watch_request

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, config, settings, version, store) -> None:
        self.config, self.settings, self.version, self.store = config, settings, version, store
        self.ai_client = create_ai_client(settings.provider)
        self.stop = threading.Event()
        self.wake_scheduler = threading.Event()
        self.threads: list[threading.Thread] = []
        self.last_scheduler_run: str | None = None

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc)
        secrets = [
            self.config.api_token,
            self.settings.provider.api_key,
            self.settings.feishu.webhook_url,
            self.settings.feishu.secret,
            *[group.api_key for group in self.settings.provider.fallback_groups],
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
                self.store.finish_job(job["id"], result, record, notify)
            except AnalysisError as exc:
                self.store.fail_job(job["id"], exc.code, str(exc))
            except Exception as exc:
                logger.exception("analysis job=%s failed", job["id"])
                self.store.fail_job(job["id"], "internal_error", self._safe_error(exc))

    def _notification_loop(self) -> None:
        while not self.stop.is_set():
            note = self.store.claim_notification()
            if not note:
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

    def ready(self) -> tuple[bool, dict]:
        alive = {t.name: t.is_alive() for t in self.threads}
        ok = bool(self.config.api_token) and all(alive.values()) and provider_api_key_configured(self.settings)
        return ok, {"ready": ok, "workers": alive, "scheduler_last_run": self.last_scheduler_run}
