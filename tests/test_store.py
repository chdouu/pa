from pathlib import Path

from pa_server.store import Store


REQUEST = {
    "source": "tradingview", "exchange": "OKX", "symbol": "ETHUSDT",
    "timeframe": "15m", "bar_count": 100, "extended_session": False,
}


def test_watch_idempotency_and_job_deduplication(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    watch, created = store.create_watch(REQUEST, "v1", "same-key")
    again, created_again = store.create_watch(REQUEST, "v1", "same-key")
    assert created is True
    assert created_again is False
    assert again["id"] == watch["id"]

    snapshot = [{"seq": 1, "ts_open": 1000, "open": 1, "high": 2, "low": 1, "close": 2, "volume": 1, "amount": 0, "pct_chg": None, "closed": True}]
    assert store.enqueue_watch_jobs(watch["id"], REQUEST, [(1000, snapshot)], "v1") == 1
    assert store.enqueue_watch_jobs(watch["id"], REQUEST, [(1000, snapshot)], "v1") == 0


def test_running_work_is_requeued_after_restart(tmp_path: Path):
    path = tmp_path / "db.sqlite3"
    store = Store(path)
    job, _ = store.create_manual_job(REQUEST, "v1", None)
    assert store.claim_job()["id"] == job["id"]
    recovered = Store(path)
    assert recovered.claim_job()["id"] == job["id"]


def test_success_and_notification_are_committed_together(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    job, _ = store.create_manual_job(REQUEST, "v1", None)
    claimed = store.claim_job()
    store.finish_job(claimed["id"], {"notification_status": "pending"}, {"ok": True}, True)
    note = store.claim_notification()
    assert note["job_id"] == job["id"]
    store.finish_notification(note["id"], "stale")
    result = store.get_job(job["id"])["result"]
    assert result["notification_status"] == "stale"
    assert result["stale"] is True


def test_paused_watch_holds_queue_and_delete_cancels_it(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    watch, _ = store.create_watch(REQUEST, "v1", None)
    snapshot = [{"seq": 1, "ts_open": 1000, "open": 1, "high": 2, "low": 1, "close": 2, "volume": 1, "amount": 0, "pct_chg": None, "closed": True}]
    store.enqueue_watch_jobs(watch["id"], REQUEST, [(1000, snapshot)], "v1")
    store.set_watch_state(watch["id"], "paused")
    assert store.claim_job() is None
    store.set_watch_state(watch["id"], "active")
    assert store.claim_job()["watch_id"] == watch["id"]

    store.enqueue_watch_jobs(watch["id"], REQUEST, [(2000, snapshot)], "v1")
    store.delete_watch(watch["id"])
    jobs = store.list_analyses(watch["id"], 10, 0)
    deleted_job = next(item for item in jobs if item["target_ts"] == 2000)
    assert deleted_job["error"]["code"] == "watch_deleted"
    assert store.enqueue_watch_jobs(watch["id"], REQUEST, [(3000, snapshot)], "v1") == 0


def test_active_thesis_snapshot_is_captured_only_once(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    watch, _ = store.create_watch(REQUEST, "v1", None)
    snapshot = [{"seq": 1, "ts_open": 1000, "open": 1, "high": 2, "low": 1,
                 "close": 2, "volume": 1, "amount": 0, "pct_chg": None, "closed": True}]
    store.enqueue_watch_jobs(watch["id"], REQUEST, [(1000, snapshot)], "v1")
    job = store.claim_job()
    first = store.capture_job_active_thesis(job["id"], {"id": 7, "current_tp1": "110"})
    second = store.capture_job_active_thesis(job["id"], {"id": 8, "current_tp1": "105"})
    assert first["active_thesis_captured"] is True
    assert second["active_thesis"] == {"id": 7, "current_tp1": "110"}


def test_analysis_and_trade_task_are_committed_and_result_is_updated(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    request = {**REQUEST, "trading_enabled": True, "okx_instrument": "ETH-USDT-SWAP"}
    watch, _ = store.create_watch(request, "v2", None)
    snapshot = [{"seq": 1, "ts_open": 1000, "open": 1, "high": 2, "low": 1,
                 "close": 2, "volume": 1, "amount": 0, "pct_chg": None, "closed": True}]
    store.enqueue_watch_jobs(watch["id"], request, [(1000, snapshot)], "v2")
    job = store.claim_job()
    store.finish_job(job["id"], {"trading_status": "pending"}, {"ok": True}, False, True)

    task = store.claim_trade_task()
    assert task["job_id"] == job["id"]
    store.finish_trade_task(task["id"], "succeeded", result={"action": "submitted"})
    result = store.get_job(job["id"])["result"]
    assert result["trading_status"] == "succeeded"
    assert result["trading_result"] == {"action": "submitted"}


def test_running_trade_work_is_requeued_after_restart(tmp_path: Path):
    path = tmp_path / "db.sqlite3"
    store = Store(path)
    request = {**REQUEST, "trading_enabled": True, "okx_instrument": "ETH-USDT-SWAP"}
    watch, _ = store.create_watch(request, "v2", None)
    snapshot = [{"seq": 1, "ts_open": 1000, "open": 1, "high": 2, "low": 1,
                 "close": 2, "volume": 1, "amount": 0, "pct_chg": None, "closed": True}]
    store.enqueue_watch_jobs(watch["id"], request, [(1000, snapshot)], "v2")
    job = store.claim_job()
    store.finish_job(job["id"], {"trading_status": "pending"}, {"ok": True}, False, True)
    task = store.claim_trade_task()
    assert task["status"] == "running"
    recovered = Store(path).claim_trade_task()
    assert recovered["id"] == task["id"]
    assert recovered["attempts"] == 2


def test_only_one_enabled_watch_can_own_a_contract(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    first, _ = store.create_watch({
        **REQUEST, "trading_enabled": True, "okx_instrument": "ETH-USDT-SWAP",
    }, "v1", None)
    second, _ = store.create_watch({
        **REQUEST, "timeframe": "1h", "trading_enabled": False,
        "okx_instrument": "ETH-USDT-SWAP",
    }, "v1", None)
    assert first["trading_enabled"] is True
    try:
        store.update_watch_trading(
            second["id"], enabled=True, okx_instrument="ETH-USDT-SWAP"
        )
    except ValueError as exc:
        assert "already assigned" in str(exc)
    else:
        raise AssertionError("duplicate contract assignment should fail")


def test_existing_database_is_migrated_with_trading_disabled(tmp_path: Path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE watches (
          id TEXT PRIMARY KEY, source TEXT NOT NULL, exchange TEXT NOT NULL,
          symbol TEXT NOT NULL, timeframe TEXT NOT NULL, bar_count INTEGER NOT NULL,
          extended_session INTEGER NOT NULL, state TEXT NOT NULL,
          last_seen_ts INTEGER, settings_version TEXT NOT NULL, error TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT
        )""")
        db.execute("""INSERT INTO watches VALUES
          ('old','tradingview','OKX','BTCUSDT','15m',100,0,'active',NULL,'v1',NULL,
           '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',NULL)""")
    store = Store(path)
    watch = store.get_watch("old")
    assert watch["trading_enabled"] is False
    assert watch["okx_instrument"] == ""
    assert store.get_trading_settings()["enabled"] is False
